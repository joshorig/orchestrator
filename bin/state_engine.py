#!/usr/bin/env python3
"""SQLite-backed state engine bootstrap for the orchestrator.

Wave B.0.1 lands the state engine behind a feature flag. The production
orchestrator remains filesystem-authoritative until later sub-waves flip read
and write paths. This module only provides:

- migration discovery and application
- SQLite WAL/bootstrap helpers
- integrity/status inspection

It is intentionally safe to import while the state engine mode is ``off``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import pathlib
import sqlite3
import threading
import time
from typing import Any, Iterable


DEFAULT_DB_BASENAME = "orchestrator.db"


@dataclass(frozen=True)
class StateEngineConfig:
    root: pathlib.Path
    db_path: pathlib.Path
    migrations_dir: pathlib.Path
    mode: str = "off"
    checkpoint_interval_sec: int = 300

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


class StateEngine:
    """SQLite migration/bootstrap wrapper.

    >>> import tempfile
    >>> root = pathlib.Path(tempfile.mkdtemp(prefix="state-engine-"))
    >>> migrations = root / "migrations"
    >>> migrations.mkdir()
    >>> _ = (migrations / "0001_initial.sql").write_text(
    ...     "CREATE TABLE demo (id INTEGER PRIMARY KEY, name TEXT);\\n",
    ...     encoding="utf-8",
    ... )
    >>> engine = StateEngine(StateEngineConfig(
    ...     root=root,
    ...     db_path=root / "runtime" / "orchestrator.db",
    ...     migrations_dir=migrations,
    ...     mode="mirror",
    ... ))
    >>> status = engine.initialize()
    >>> status["enabled"], status["applied_count"], status["integrity_check"]
    (True, 1, 'ok')
    >>> status["pending_migrations"]
    []
    """

    def __init__(self, config: StateEngineConfig):
        self.config = config
        self._local = threading.local()

    def initialize(self) -> dict[str, Any]:
        if not self.config.enabled:
            return {
                "enabled": False,
                "mode": self.config.mode,
                "db_path": str(self.config.db_path),
                "migrations_dir": str(self.config.migrations_dir),
            }
        conn = self.connect()
        self._ensure_bootstrap_tables(conn)
        applied = self.apply_migrations(conn)
        return self.status(conn=conn, applied_in_run=applied)

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.config.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA temp_store = MEMORY")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def status(self, *, conn: sqlite3.Connection | None = None, applied_in_run: int = 0) -> dict[str, Any]:
        conn = conn or self.connect()
        applied = self._applied_migrations(conn)
        pending = [
            item["version"]
            for item in self.discover_migrations()
            if item["version"] not in applied
        ]
        return {
            "enabled": self.config.enabled,
            "mode": self.config.mode,
            "db_path": str(self.config.db_path),
            "migrations_dir": str(self.config.migrations_dir),
            "applied_count": len(applied),
            "applied_in_run": applied_in_run,
            "applied_migrations": sorted(applied),
            "pending_migrations": pending,
            "integrity_check": self.integrity_check(conn=conn),
        }

    def integrity_check(self, *, conn: sqlite3.Connection | None = None) -> str:
        conn = conn or self.connect()
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row:
            return "unknown"
        return str(row[0])

    def checkpoint(self, *, conn: sqlite3.Connection | None = None) -> tuple[Any, ...]:
        conn = conn or self.connect()
        return tuple(conn.execute("PRAGMA wal_checkpoint(RESTART)").fetchone() or ())

    def discover_migrations(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self.config.migrations_dir.exists():
            return out
        for path in sorted(self.config.migrations_dir.glob("*.sql")):
            out.append(
                {
                    "version": path.stem,
                    "path": path,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        return out

    def apply_migrations(self, conn: sqlite3.Connection | None = None) -> int:
        conn = conn or self.connect()
        applied = self._applied_migrations(conn)
        count = 0
        for item in self.discover_migrations():
            version = item["version"]
            if version in applied:
                continue
            sql = item["path"].read_text(encoding="utf-8")
            with conn:
                conn.executescript(sql)
                conn.execute(
                    """
                    INSERT INTO schema_migrations (version, sha256, applied_at_epoch)
                    VALUES (?, ?, ?)
                    """,
                    (version, item["sha256"], int(time.time())),
                )
            count += 1
        return count

    def seed_blocker_codes(self, codes: Iterable[str], *, conn: sqlite3.Connection | None = None) -> int:
        conn = conn or self.connect()
        inserted = 0
        with conn:
            for code in codes:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO blocker_codes (code, name, severity, retryable, category, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (code, code.replace("_", " "), "unknown", None, "runtime", json.dumps({})),
                )
                inserted += cur.rowcount or 0
        return inserted

    def upsert_task_from_fs(self, task: dict[str, Any], *, state: str, conn: sqlite3.Connection | None = None) -> None:
        conn = conn or self.connect()
        blocker = task.get("blocker") or {}
        payload = dict(task)
        payload["state"] = state
        with conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, state, created_at, created_at_epoch, state_updated_at,
                    engine, role, project, feature_id, parent_task_id,
                    braid_template, braid_template_hash, summary,
                    claimed_at, claimed_pid, claimed_slot,
                    started_at, finished_at, finished_at_epoch,
                    attempt,
                    blocker_code, blocker_summary, blocker_detail, blocker_retryable, blocker_updated_at,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    state=excluded.state,
                    state_updated_at=excluded.state_updated_at,
                    engine=excluded.engine,
                    role=excluded.role,
                    project=excluded.project,
                    feature_id=excluded.feature_id,
                    parent_task_id=excluded.parent_task_id,
                    braid_template=excluded.braid_template,
                    braid_template_hash=excluded.braid_template_hash,
                    summary=excluded.summary,
                    claimed_at=excluded.claimed_at,
                    claimed_pid=excluded.claimed_pid,
                    claimed_slot=excluded.claimed_slot,
                    started_at=excluded.started_at,
                    finished_at=excluded.finished_at,
                    finished_at_epoch=excluded.finished_at_epoch,
                    attempt=excluded.attempt,
                    blocker_code=excluded.blocker_code,
                    blocker_summary=excluded.blocker_summary,
                    blocker_detail=excluded.blocker_detail,
                    blocker_retryable=excluded.blocker_retryable,
                    blocker_updated_at=excluded.blocker_updated_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    task.get("task_id"),
                    state,
                    task.get("created_at") or "",
                    _iso_to_epoch(task.get("created_at")),
                    task.get("finished_at") or task.get("started_at") or task.get("claimed_at") or task.get("created_at") or "",
                    task.get("engine") or "",
                    task.get("role") or "",
                    task.get("project"),
                    task.get("feature_id"),
                    task.get("parent_task_id"),
                    task.get("braid_template"),
                    task.get("braid_template_hash"),
                    task.get("summary") or "",
                    task.get("claimed_at"),
                    _int_or_none(task.get("claimed_pid")),
                    task.get("claimed_slot"),
                    task.get("started_at"),
                    task.get("finished_at"),
                    _iso_to_epoch(task.get("finished_at")),
                    int(task.get("attempt") or 1),
                    blocker.get("code"),
                    blocker.get("summary"),
                    blocker.get("detail"),
                    _bool_to_int_or_none(blocker.get("retryable")),
                    blocker.get("updated_at"),
                    json.dumps(payload, sort_keys=True),
                ),
            )

    def upsert_feature_from_fs(self, feature: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> None:
        conn = conn or self.connect()
        metadata = dict(feature)
        feature_id = str(feature.get("feature_id") or "")
        self_repair = feature.get("self_repair") or {}
        child_ids = list(feature.get("child_task_ids") or [])
        issues = list(self_repair.get("issues") or [])
        with conn:
            conn.execute(
                """
                INSERT INTO features (
                    feature_id, created_at, created_at_epoch, status, project,
                    pr_body_path, pr_number, pr_url, pr_final_state,
                    frontier_task_id, frontier_state, frontier_updated_at,
                    self_repair_status, version, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(feature_id) DO UPDATE SET
                    created_at=excluded.created_at,
                    created_at_epoch=excluded.created_at_epoch,
                    status=excluded.status,
                    project=excluded.project,
                    pr_body_path=excluded.pr_body_path,
                    pr_number=excluded.pr_number,
                    pr_url=excluded.pr_url,
                    pr_final_state=excluded.pr_final_state,
                    frontier_task_id=excluded.frontier_task_id,
                    frontier_state=excluded.frontier_state,
                    frontier_updated_at=excluded.frontier_updated_at,
                    self_repair_status=excluded.self_repair_status,
                    version=excluded.version,
                    metadata_json=excluded.metadata_json
                """,
                (
                    feature_id,
                    feature.get("created_at") or "",
                    _iso_to_epoch(feature.get("created_at")),
                    feature.get("status") or "open",
                    feature.get("project"),
                    feature.get("pr_body_path"),
                    _int_or_none(feature.get("pr_number") or feature.get("final_pr_number")),
                    feature.get("pr_url") or feature.get("final_pr_url"),
                    ((feature.get("final_pr_sweep") or {}).get("last_merge_state")) or feature.get("pr_final_state"),
                    feature.get("frontier_task_id"),
                    feature.get("frontier_state"),
                    feature.get("frontier_updated_at"),
                    self_repair.get("status"),
                    int(feature.get("version") or 1),
                    json.dumps(metadata, sort_keys=True),
                ),
            )
            conn.execute("DELETE FROM feature_children WHERE feature_id = ?", (feature_id,))
            for idx, task_id in enumerate(child_ids):
                conn.execute(
                    """
                    INSERT INTO feature_children (feature_id, task_id, role, order_idx, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (feature_id, task_id, None, idx, feature.get("created_at") or ""),
                )
            conn.execute("DELETE FROM self_repair_deliberations WHERE issue_id IN (SELECT issue_id FROM self_repair_issues WHERE feature_id = ?)", (feature_id,))
            conn.execute("DELETE FROM self_repair_issues WHERE feature_id = ?", (feature_id,))
            for issue_idx, issue in enumerate(issues):
                issue_id = str(issue.get("issue_id") or issue.get("issue_key") or f"{feature_id}:issue:{issue_idx}")
                conn.execute(
                    """
                    INSERT INTO self_repair_issues (
                        issue_id, feature_id, created_at, created_at_epoch, status,
                        last_deliberation_at, planner_task_id, attempts, max_attempts,
                        blocker_code, blocker_summary, superseded_task_ids, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        issue_id,
                        feature_id,
                        issue.get("created_at") or feature.get("created_at") or "",
                        _iso_to_epoch(issue.get("created_at") or feature.get("created_at")),
                        issue.get("status") or "pending",
                        issue.get("last_deliberation_at"),
                        issue.get("planner_task_id"),
                        int(issue.get("attempts") or 0),
                        int(issue.get("max_attempts") or 3),
                        issue.get("blocker_code") or ((issue.get("blocker") or {}).get("code")),
                        issue.get("blocker_summary") or ((issue.get("blocker") or {}).get("summary")),
                        json.dumps(issue.get("superseded_task_ids") or []),
                        json.dumps(issue, sort_keys=True),
                    ),
                )
                for deliberation in issue.get("deliberations") or []:
                    conn.execute(
                        """
                        INSERT INTO self_repair_deliberations (
                            issue_id, created_at, created_at_epoch, stage, verdict, panel, summary
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            issue_id,
                            deliberation.get("created_at") or "",
                            _iso_to_epoch(deliberation.get("created_at")),
                            deliberation.get("stage"),
                            deliberation.get("verdict"),
                            deliberation.get("panel"),
                            deliberation.get("summary") or "",
                        ),
                    )

    def record_transition(self, row: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> None:
        conn = conn or self.connect()
        with conn:
            conn.execute(
                """
                INSERT INTO task_transitions (task_id, from_state, to_state, reason, created_at, created_at_epoch)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("task_id"),
                    row.get("from_state") or "",
                    row.get("to_state") or "",
                    row.get("reason"),
                    row.get("ts") or "",
                    _iso_to_epoch(row.get("ts")),
                ),
            )

    def record_event(self, row: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> None:
        conn = conn or self.connect()
        with conn:
            conn.execute(
                """
                INSERT INTO events (kind, created_at, created_at_epoch, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    f"{row.get('role') or 'runtime'}:{row.get('event') or ''}",
                    row.get("ts") or "",
                    _iso_to_epoch(row.get("ts")),
                    json.dumps(row, sort_keys=True),
                ),
            )

    def record_metric(self, row: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> None:
        conn = conn or self.connect()
        with conn:
            conn.execute(
                """
                INSERT INTO metrics (name, value, metric_type, created_at_epoch, tags_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row.get("name"),
                    float(row.get("value") or 0),
                    row.get("type") or "gauge",
                    _iso_to_epoch(row.get("ts")),
                    json.dumps({"tags": row.get("tags") or {}, "source": row.get("source")}, sort_keys=True),
                ),
            )

    def queue_state_counts(self, *, conn: sqlite3.Connection | None = None) -> dict[str, int]:
        conn = conn or self.connect()
        rows = conn.execute("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state").fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}

    def feature_count(self, *, conn: sqlite3.Connection | None = None) -> int:
        conn = conn or self.connect()
        row = conn.execute("SELECT COUNT(*) AS n FROM features").fetchone()
        return int(row["n"] if row else 0)

    def find_task(self, task_id: str, *, states: Iterable[str] | None = None, conn: sqlite3.Connection | None = None) -> tuple[str, dict[str, Any]] | None:
        conn = conn or self.connect()
        if states:
            placeholders = ",".join("?" for _ in states)
            row = conn.execute(
                f"SELECT state, metadata_json FROM tasks WHERE task_id = ? AND state IN ({placeholders}) LIMIT 1",
                (task_id, *list(states)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT state, metadata_json FROM tasks WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone()
        if not row:
            return None
        return str(row["state"]), json.loads(row["metadata_json"])

    def read_feature(self, feature_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        conn = conn or self.connect()
        row = conn.execute(
            "SELECT metadata_json FROM features WHERE feature_id = ? LIMIT 1",
            (feature_id,),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["metadata_json"])

    def read_features(self, *, status: str | None = None, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        conn = conn or self.connect()
        if status:
            rows = conn.execute(
                "SELECT metadata_json FROM features WHERE status = ? ORDER BY created_at_epoch ASC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT metadata_json FROM features ORDER BY created_at_epoch ASC"
            ).fetchall()
        return [json.loads(row["metadata_json"]) for row in rows]

    def read_events(
        self,
        *,
        feature_id: str | None = None,
        task_id: str | None = None,
        role: str | None = None,
        limit: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        conn = conn or self.connect()
        clauses = []
        params: list[Any] = []
        if feature_id is not None:
            clauses.append("json_extract(payload_json, '$.feature_id') = ?")
            params.append(feature_id)
        if task_id is not None:
            clauses.append("json_extract(payload_json, '$.task_id') = ?")
            params.append(task_id)
        if role is not None:
            clauses.append("json_extract(payload_json, '$.role') = ?")
            params.append(role)
        sql = "SELECT payload_json FROM events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at_epoch ASC"
        if limit is not None and limit >= 0:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, params).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def read_transitions(self, *, task_id: str | None = None, limit: int | None = None, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        conn = conn or self.connect()
        params: list[Any] = []
        sql = "SELECT task_id, from_state, to_state, reason, created_at FROM task_transitions"
        if task_id is not None:
            sql += " WHERE task_id = ?"
            params.append(task_id)
        sql += " ORDER BY created_at_epoch ASC"
        if limit is not None and limit >= 0:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "ts": row["created_at"],
                "task_id": row["task_id"],
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "reason": row["reason"],
            }
            for row in rows
        ]

    def read_metrics(self, *, name: str | None = None, limit: int | None = None, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        conn = conn or self.connect()
        params: list[Any] = []
        sql = "SELECT name, value, metric_type, created_at_epoch, tags_json FROM metrics"
        if name is not None:
            sql += " WHERE name = ?"
            params.append(name)
        sql += " ORDER BY created_at_epoch ASC"
        if limit is not None and limit >= 0:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            payload = json.loads(row["tags_json"] or "{}")
            epoch = int(row["created_at_epoch"] or 0)
            out.append(
                {
                    "ts": datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") if epoch else "",
                    "name": row["name"],
                    "type": row["metric_type"],
                    "value": row["value"],
                    "tags": payload.get("tags") or {},
                    "source": payload.get("source"),
                }
            )
        return out

    def read_tasks(
        self,
        *,
        states: Iterable[str] | None = None,
        project: str | None = None,
        engine: str | None = None,
        role: str | None = None,
        limit: int | None = None,
        newest_first: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        conn = conn or self.connect()
        clauses = []
        params: list[Any] = []
        if states:
            states = list(states)
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            params.extend(states)
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        if engine is not None:
            clauses.append("engine = ?")
            params.append(engine)
        if role is not None:
            clauses.append("role = ?")
            params.append(role)
        sql = "SELECT metadata_json FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        order = "DESC" if newest_first else "ASC"
        sql += f" ORDER BY created_at_epoch {order}"
        if limit is not None and limit >= 0:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, params).fetchall()
        return [json.loads(row["metadata_json"]) for row in rows]

    def claim_task(self, task_id: str, *, slot_engine: str, claimed_at: str, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        conn = conn or self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT metadata_json FROM tasks WHERE task_id = ? AND state = 'queued' LIMIT 1",
                (task_id,),
            ).fetchone()
            if not row:
                conn.rollback()
                return None
            task = json.loads(row["metadata_json"])
            task["state"] = "claimed"
            task["claimed_at"] = claimed_at
            task["claimed_slot"] = slot_engine
            cur = conn.execute(
                """
                UPDATE tasks
                   SET state = 'claimed',
                       state_updated_at = ?,
                       claimed_at = ?,
                       claimed_slot = ?,
                       metadata_json = ?
                 WHERE task_id = ?
                   AND state = 'queued'
                """,
                (
                    claimed_at,
                    claimed_at,
                    slot_engine,
                    json.dumps(task, sort_keys=True),
                    task_id,
                ),
            )
            if (cur.rowcount or 0) <= 0:
                conn.rollback()
                return None
            conn.commit()
            return task
        except Exception:
            conn.rollback()
            raise

    def _ensure_bootstrap_tables(self, conn: sqlite3.Connection) -> None:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version          TEXT PRIMARY KEY,
                    sha256           TEXT NOT NULL,
                    applied_at_epoch INTEGER NOT NULL
                )
                """
            )

    def _applied_migrations(self, conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        return {str(row["version"]) for row in rows}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="state_engine")
    parser.add_argument("--root", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--migrations-dir", required=True)
    parser.add_argument("--mode", default="off", choices=("off", "mirror", "primary"))
    args = parser.parse_args(argv)

    engine = StateEngine(
        StateEngineConfig(
            root=pathlib.Path(args.root),
            db_path=pathlib.Path(args.db_path),
            migrations_dir=pathlib.Path(args.migrations_dir),
            mode=args.mode,
        )
    )
    print(json.dumps(engine.initialize(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def _iso_to_epoch(value: Any) -> int:
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0

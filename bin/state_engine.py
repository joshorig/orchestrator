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
import importlib
import json
import math
import pathlib
import re
import sqlite3
import struct
import threading
import time
from typing import Any, Iterable


DEFAULT_DB_BASENAME = "orchestrator.db"
DEFAULT_MEMORY_VEC_DIMENSIONS = 24
DEFAULT_MEMORY_RRF_K = 60
DEFAULT_METRICS_RETENTION_DAYS = 14
MAX_REASONABLE_FUTURE_EPOCH_SKEW = 30 * 24 * 3600
REPO_MEMORY_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


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
        self._vec_enabled = False
        self._vec_error: str | None = None
        self._initialized_once = False

    def _current_db_identity(self) -> tuple[int, int] | None:
        try:
            stat = self.config.db_path.stat()
        except FileNotFoundError:
            return None
        return (int(stat.st_dev), int(stat.st_ino))

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
        self._initialized_once = True
        return self.status(conn=conn, applied_in_run=applied)

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        current_identity = self._current_db_identity()
        cached_identity = getattr(self._local, "db_identity", None)
        if conn is not None and cached_identity != current_identity:
            try:
                conn.close()
            finally:
                self._local.conn = None
                self._local.db_identity = None
            conn = None
        if conn is None:
            self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
            if self._initialized_once and not self.config.db_path.exists():
                raise sqlite3.DatabaseError("state engine database missing")
            conn = sqlite3.connect(self.config.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA temp_store = MEMORY")
            self._try_enable_sqlite_vec(conn)
            self._local.conn = conn
            self._local.db_identity = self._current_db_identity()
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
            self._local.db_identity = None

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
            "vec_enabled": self._vec_enabled,
            "vec_error": self._vec_error,
        }

    def integrity_check(self, *, conn: sqlite3.Connection | None = None) -> str:
        conn = conn or self.connect()
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row:
            return "unknown"
        result = str(row[0])
        if "memory_obs_fts" in result or "fts5:" in result:
            try:
                self.rebuild_memory_fts(conn=conn)
                row = conn.execute("PRAGMA integrity_check").fetchone()
                if row:
                    result = str(row[0])
            except sqlite3.Error:
                pass
        return result

    def checkpoint(self, *, conn: sqlite3.Connection | None = None) -> tuple[Any, ...]:
        conn = conn or self.connect()
        return tuple(conn.execute("PRAGMA wal_checkpoint(RESTART)").fetchone() or ())

    def purge_old_metrics(self, *, cutoff_epoch: int, conn: sqlite3.Connection | None = None) -> int:
        conn = conn or self.connect()
        future_cutoff = int(time.time()) + MAX_REASONABLE_FUTURE_EPOCH_SKEW
        with conn:
            cur = conn.execute(
                "DELETE FROM metrics WHERE created_at_epoch < ? OR created_at_epoch > ?",
                (int(cutoff_epoch), future_cutoff),
            )
        return int(cur.rowcount or 0)

    def backup_into(self, backup_path: str | pathlib.Path, *, conn: sqlite3.Connection | None = None) -> str:
        conn = conn or self.connect()
        target = pathlib.Path(backup_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        escaped = str(target).replace("'", "''")
        conn.execute(f"VACUUM INTO '{escaped}'")
        return str(target)

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
        self.validate_migrations(conn=conn)
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

    def validate_migrations(self, conn: sqlite3.Connection | None = None) -> None:
        conn = conn or self.connect()
        discovered = {item["version"]: item for item in self.discover_migrations()}
        applied = self._applied_migration_rows(conn)
        unknown = sorted(set(applied) - set(discovered))
        if unknown:
            raise RuntimeError(f"state engine migration forward drift: {', '.join(unknown)}")
        mismatched = [
            version
            for version, row in applied.items()
            if version in discovered and str(row.get("sha256") or "") not in ("", discovered[version]["sha256"])
        ]
        if mismatched:
            raise RuntimeError(f"state engine migration sha mismatch: {', '.join(sorted(mismatched))}")

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

    def upsert_task(self, task: dict[str, Any], *, state: str | None = None, conn: sqlite3.Connection | None = None) -> None:
        self.upsert_task_from_fs(task, state=state or task.get("state") or "queued", conn=conn)

    def delete_task(self, task_id: str, *, conn: sqlite3.Connection | None = None) -> None:
        conn = conn or self.connect()
        with conn:
            conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))

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
                    _sqlite_text(feature.get("project")),
                    _sqlite_text(feature.get("pr_body_path")),
                    _int_or_none(feature.get("pr_number") or feature.get("final_pr_number")),
                    _sqlite_text(feature.get("pr_url") or feature.get("final_pr_url")),
                    _sqlite_text(((feature.get("final_pr_sweep") or {}).get("last_merge_state")) or feature.get("pr_final_state")),
                    _sqlite_text(feature.get("frontier_task_id")),
                    _sqlite_text(feature.get("frontier_state")),
                    _sqlite_text(feature.get("frontier_updated_at")),
                    _sqlite_text(self_repair.get("status")),
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
                        _sqlite_text(issue.get("status") or "pending"),
                        _sqlite_text(issue.get("last_deliberation_at")),
                        _sqlite_text(issue.get("planner_task_id")),
                        int(issue.get("attempts") or 0),
                        int(issue.get("max_attempts") or 3),
                        _sqlite_text(issue.get("blocker_code") or ((issue.get("blocker") or {}).get("code"))),
                        _sqlite_text(issue.get("blocker_summary") or ((issue.get("blocker") or {}).get("summary"))),
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
                            _sqlite_text(deliberation.get("stage")),
                            _sqlite_text(deliberation.get("verdict")),
                            _sqlite_text(deliberation.get("panel")),
                            _sqlite_text(deliberation.get("summary") or ""),
                        ),
                    )

    def upsert_feature(self, feature: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> None:
        self.upsert_feature_from_fs(feature, conn=conn)

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
        return str(row["state"]), _json_loads_or_default(row["metadata_json"], {}, field_name="tasks.metadata_json")

    def read_feature(self, feature_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        conn = conn or self.connect()
        row = conn.execute(
            "SELECT metadata_json FROM features WHERE feature_id = ? LIMIT 1",
            (feature_id,),
        ).fetchone()
        if not row:
            return None
        return _json_loads_or_default(row["metadata_json"], {}, field_name="features.metadata_json")

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
        return [_json_loads_or_default(row["metadata_json"], {}, field_name="features.metadata_json") for row in rows]

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
        if limit is not None and limit >= 0:
            sql += " ORDER BY created_at_epoch DESC, id DESC"
            sql += f" LIMIT {int(limit)}"
            rows = list(reversed(conn.execute(sql, params).fetchall()))
        else:
            sql += " ORDER BY created_at_epoch ASC, id ASC"
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
        if limit is not None and limit >= 0:
            sql += " ORDER BY created_at_epoch DESC, id DESC"
            sql += f" LIMIT {int(limit)}"
            rows = list(reversed(conn.execute(sql, params).fetchall()))
        else:
            sql += " ORDER BY created_at_epoch ASC, id ASC"
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
        return [_json_loads_or_default(row["metadata_json"], {}, field_name="tasks.metadata_json") for row in rows]

    def upsert_memory_observation(self, row: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> int:
        conn = conn or self.connect()
        tags = list(row.get("tags") or [])
        metadata = dict(row.get("metadata") or {})
        with conn:
            conn.execute(
                """
                INSERT INTO memory_observations (
                    project, source_doc, section_key, type, title, content,
                    created_at, created_at_epoch, importance, tags_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project, source_doc, section_key) DO UPDATE SET
                    type=excluded.type,
                    title=excluded.title,
                    content=excluded.content,
                    created_at=excluded.created_at,
                    created_at_epoch=excluded.created_at_epoch,
                    importance=excluded.importance,
                    tags_json=excluded.tags_json,
                    metadata_json=excluded.metadata_json
                """,
                (
                    row["project"],
                    row["source_doc"],
                    row["section_key"],
                    row["type"],
                    row["title"],
                    row["content"],
                    row["created_at"],
                    _iso_to_epoch(row["created_at"]),
                    int(row.get("importance") or 5),
                    json.dumps(tags, sort_keys=True),
                    json.dumps(metadata, sort_keys=True),
                ),
            )
            obs_row = conn.execute(
                """
                SELECT id
                  FROM memory_observations
                 WHERE project = ? AND source_doc = ? AND section_key = ?
                 LIMIT 1
                """,
                (row["project"], row["source_doc"], row["section_key"]),
            ).fetchone()
            obs_id = int(obs_row["id"])
            self._upsert_memory_vector(conn, obs_id, f"{row['title']}\n{row['content']}")
            return obs_id

    def rebuild_memory_fts(self, *, conn: sqlite3.Connection | None = None) -> None:
        conn = conn or self.connect()
        with conn:
            conn.execute("DELETE FROM memory_obs_fts")
            conn.execute(
                """
                INSERT INTO memory_obs_fts(rowid, title, content, tags)
                SELECT id, title, content, tags_json
                  FROM memory_observations
                """
            )

    def memory_index(
        self,
        *,
        project: str,
        obs_type: str | None = None,
        limit: int = 5,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        conn = conn or self.connect()
        sql = """
            SELECT *
              FROM memory_observations
             WHERE project = ?
        """
        params: list[Any] = [project]
        if obs_type:
            sql += " AND type = ?"
            params.append(obs_type)
        sql += """
             ORDER BY importance DESC, access_count DESC, created_at_epoch DESC
             LIMIT ?
        """
        params.append(int(limit))
        return [self._memory_row_to_dict(row, full=False) for row in conn.execute(sql, params).fetchall()]

    def memory_search(
        self,
        query: str,
        *,
        project: str,
        limit: int = 10,
        semantic_candidates: Iterable[int] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        conn = conn or self.connect()
        terms = (query or "").strip()
        if not terms:
            return []
        scores: dict[int, float] = {}
        rrf_k = DEFAULT_MEMORY_RRF_K
        fts_query = _fts_query_for(terms)
        try:
            fts_rows = conn.execute(
                """
                SELECT rowid
                  FROM memory_obs_fts
                 WHERE memory_obs_fts MATCH ?
                   AND rowid IN (SELECT id FROM memory_observations WHERE project = ?)
                 LIMIT ?
                """,
                (fts_query, project, int(limit) * 4),
            ).fetchall()
        except sqlite3.Error:
            self.rebuild_memory_fts(conn=conn)
            fts_rows = conn.execute(
                """
                SELECT rowid
                  FROM memory_obs_fts
                 WHERE memory_obs_fts MATCH ?
                   AND rowid IN (SELECT id FROM memory_observations WHERE project = ?)
                 LIMIT ?
                """,
                (fts_query, project, int(limit) * 4),
            ).fetchall()
        for rank, row in enumerate(fts_rows, start=1):
            scores[int(row["rowid"])] = scores.get(int(row["rowid"]), 0.0) + (1.0 / (rrf_k + rank))

        if semantic_candidates is not None:
            for rank, obs_id in enumerate(list(semantic_candidates)[: int(limit) * 4], start=1):
                scores[int(obs_id)] = scores.get(int(obs_id), 0.0) + (1.0 / (rrf_k + rank))
        elif self._vec_enabled:
            query_vector = json.dumps(_semantic_embedding(terms))
            try:
                vec_rows = conn.execute(
                    """
                    SELECT obs_id
                      FROM memory_vectors
                     WHERE project = ?
                       AND embedding MATCH ?
                     ORDER BY distance
                     LIMIT ?
                    """,
                    (project, query_vector, int(limit) * 4),
                ).fetchall()
                for rank, row in enumerate(vec_rows, start=1):
                    scores[int(row["obs_id"])] = scores.get(int(row["obs_id"]), 0.0) + (1.0 / (rrf_k + rank))
            except sqlite3.Error as exc:
                self._vec_error = str(exc)

        normalized_query = _slugify(terms)
        exact_rows = conn.execute(
            "SELECT id, title FROM memory_observations WHERE project = ?",
            (project,),
        ).fetchall()
        for row in exact_rows:
            if _slugify(str(row["title"])) == normalized_query:
                scores[int(row["id"])] = scores.get(int(row["id"]), 0.0) + 1.0

        if not scores:
            return []
        obs_ids = sorted(scores, key=scores.get, reverse=True)[: int(limit)]
        placeholders = ",".join("?" for _ in obs_ids)
        rows = conn.execute(
            f"SELECT * FROM memory_observations WHERE id IN ({placeholders})",
            obs_ids,
        ).fetchall()
        by_id = {int(row["id"]): self._memory_row_to_dict(row, full=False) for row in rows}
        return [by_id[obs_id] for obs_id in obs_ids if obs_id in by_id]

    def memory_get(self, obs_id: int, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        conn = conn or self.connect()
        now_epoch = int(time.time())
        with conn:
            row = conn.execute(
                "SELECT * FROM memory_observations WHERE id = ? LIMIT 1",
                (int(obs_id),),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """
                UPDATE memory_observations
                   SET access_count = access_count + 1,
                       last_accessed_epoch = ?
                 WHERE id = ?
                """,
                (now_epoch, int(obs_id)),
            )
        return self._memory_row_to_dict(row, full=True)

    def memory_count(self, *, project: str | None = None, conn: sqlite3.Connection | None = None) -> int:
        conn = conn or self.connect()
        if project is None:
            row = conn.execute("SELECT COUNT(*) AS n FROM memory_observations").fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM memory_observations WHERE project = ?", (project,)).fetchone()
        return int(row["n"] if row else 0)

    def record_environment_check(self, *, ts: str, project: str | None, result: str, blocker_summary: str | None, conn: sqlite3.Connection | None = None) -> None:
        conn = conn or self.connect()
        with conn:
            conn.execute(
                """
                INSERT INTO environment_check_log (ts, ts_epoch, project, result, blocker_summary)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ts, _iso_to_epoch(ts), project, result, blocker_summary),
            )

    def record_orphan_recovery(self, *, ts: str, task_id: str, from_state: str, age_seconds: int, conn: sqlite3.Connection | None = None) -> None:
        conn = conn or self.connect()
        with conn:
            conn.execute(
                """
                INSERT INTO orphan_recovery_log (ts, ts_epoch, task_id, from_state, age_seconds)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ts, _iso_to_epoch(ts), task_id, from_state, int(age_seconds)),
            )

    def record_task_bypass(self, *, ts: str, task_id: str, gate: str, reason: str, conn: sqlite3.Connection | None = None) -> None:
        conn = conn or self.connect()
        with conn:
            conn.execute(
                """
                INSERT INTO task_bypass_log (ts, ts_epoch, task_id, gate, reason)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ts, _iso_to_epoch(ts), task_id, gate, reason),
            )

    def record_task_cost(
        self,
        *,
        ts: str,
        task_id: str,
        engine: str,
        model: str | None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_tokens: int = 0,
        search_tokens: int = 0,
        cost_usd: float = 0.0,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        conn = conn or self.connect()
        with conn:
            conn.execute(
                """
                INSERT INTO task_costs (
                    ts, ts_epoch, task_id, engine, model,
                    input_tokens, output_tokens, cache_tokens, search_tokens, cost_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    _iso_to_epoch(ts),
                    task_id,
                    engine,
                    model,
                    int(input_tokens or 0),
                    int(output_tokens or 0),
                    int(cache_tokens or 0),
                    int(search_tokens or 0),
                    float(cost_usd or 0.0),
                ),
            )

    def count_table(self, table: str, *, conn: sqlite3.Connection | None = None) -> int:
        conn = conn or self.connect()
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"] if row else 0)

    def read_orphan_recoveries(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        conn = conn or self.connect()
        rows = conn.execute(
            "SELECT ts, task_id, from_state, age_seconds FROM orphan_recovery_log ORDER BY ts_epoch ASC"
        ).fetchall()
        return [
            {
                "ts": row["ts"],
                "task_id": row["task_id"],
                "from_state": row["from_state"],
                "age_seconds": int(row["age_seconds"]),
            }
            for row in rows
        ]

    def read_environment_checks(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        conn = conn or self.connect()
        rows = conn.execute(
            "SELECT ts, project, result, blocker_summary FROM environment_check_log ORDER BY ts_epoch ASC"
        ).fetchall()
        return [
            {
                "ts": row["ts"],
                "project": row["project"],
                "result": row["result"],
                "blocker_summary": row["blocker_summary"],
            }
            for row in rows
        ]

    def read_task_bypasses(self, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        conn = conn or self.connect()
        rows = conn.execute(
            "SELECT ts, task_id, gate, reason FROM task_bypass_log ORDER BY ts_epoch ASC"
        ).fetchall()
        return [
            {
                "ts": row["ts"],
                "task_id": row["task_id"],
                "gate": row["gate"],
                "reason": row["reason"],
            }
            for row in rows
        ]

    def read_task_costs(
        self,
        *,
        task_id: str | None = None,
        limit: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        conn = conn or self.connect()
        sql = """
            SELECT ts, task_id, engine, model, input_tokens, output_tokens, cache_tokens, search_tokens, cost_usd
              FROM task_costs
        """
        params: list[Any] = []
        if task_id is not None:
            sql += " WHERE task_id = ?"
            params.append(task_id)
        sql += " ORDER BY ts_epoch DESC"
        if limit is not None and limit >= 0:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "ts": row["ts"],
                "task_id": row["task_id"],
                "engine": row["engine"],
                "model": row["model"],
                "input_tokens": int(row["input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
                "cache_tokens": int(row["cache_tokens"] or 0),
                "search_tokens": int(row["search_tokens"] or 0),
                "cost_usd": float(row["cost_usd"] or 0.0),
            }
            for row in rows
        ]

    def aggregate_task_costs(self, *, hours: int = 24, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        conn = conn or self.connect()
        since_epoch = int(time.time()) - max(int(hours), 1) * 3600
        summary_row = conn.execute(
            """
            SELECT
                COUNT(*) AS rows_count,
                COUNT(DISTINCT task_id) AS task_count,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_tokens), 0) AS cache_tokens,
                COALESCE(SUM(search_tokens), 0) AS search_tokens,
                COALESCE(SUM(cost_usd), 0.0) AS cost_usd
              FROM task_costs
             WHERE ts_epoch >= ?
            """,
            (since_epoch,),
        ).fetchone()
        by_engine_rows = conn.execute(
            """
            SELECT
                engine,
                COUNT(*) AS rows_count,
                COUNT(DISTINCT task_id) AS task_count,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_tokens), 0) AS cache_tokens,
                COALESCE(SUM(search_tokens), 0) AS search_tokens,
                COALESCE(SUM(cost_usd), 0.0) AS cost_usd
              FROM task_costs
             WHERE ts_epoch >= ?
             GROUP BY engine
             ORDER BY cost_usd DESC, engine ASC
            """,
            (since_epoch,),
        ).fetchall()
        recent = self.read_task_costs(limit=12, conn=conn)
        return {
            "window_hours": max(int(hours), 1),
            "summary": {
                "rows_count": int(summary_row["rows_count"] or 0) if summary_row else 0,
                "task_count": int(summary_row["task_count"] or 0) if summary_row else 0,
                "input_tokens": int(summary_row["input_tokens"] or 0) if summary_row else 0,
                "output_tokens": int(summary_row["output_tokens"] or 0) if summary_row else 0,
                "cache_tokens": int(summary_row["cache_tokens"] or 0) if summary_row else 0,
                "search_tokens": int(summary_row["search_tokens"] or 0) if summary_row else 0,
                "cost_usd": float(summary_row["cost_usd"] or 0.0) if summary_row else 0.0,
            },
            "by_engine": [
                {
                    "engine": row["engine"],
                    "rows_count": int(row["rows_count"] or 0),
                    "task_count": int(row["task_count"] or 0),
                    "input_tokens": int(row["input_tokens"] or 0),
                    "output_tokens": int(row["output_tokens"] or 0),
                    "cache_tokens": int(row["cache_tokens"] or 0),
                    "search_tokens": int(row["search_tokens"] or 0),
                    "cost_usd": float(row["cost_usd"] or 0.0),
                }
                for row in by_engine_rows
            ],
            "recent": recent,
        }

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
            task = _json_loads_or_default(row["metadata_json"], {}, field_name="tasks.metadata_json")
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

    def _try_enable_sqlite_vec(self, conn: sqlite3.Connection) -> None:
        if getattr(self._local, "vec_checked", False):
            return
        try:
            conn.enable_load_extension(True)
            sqlite_vec = importlib.import_module("sqlite_vec")
            sqlite_vec.load(conn)  # type: ignore[attr-defined]
            conn.enable_load_extension(False)
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0(
                    obs_id INTEGER PRIMARY KEY,
                    project TEXT,
                    embedding FLOAT[{DEFAULT_MEMORY_VEC_DIMENSIONS}]
                )
                """
            )
            self._vec_enabled = True
            self._vec_error = None
        except Exception as exc:
            self._vec_enabled = False
            self._vec_error = str(exc)
        finally:
            self._local.vec_checked = True

    def _upsert_memory_vector(self, conn: sqlite3.Connection, obs_id: int, text: str) -> None:
        if not self._vec_enabled:
            return
        try:
            project_row = conn.execute(
                "SELECT project FROM memory_observations WHERE id = ? LIMIT 1",
                (int(obs_id),),
            ).fetchone()
            embedding = _semantic_embedding(text)
            if len(embedding) != DEFAULT_MEMORY_VEC_DIMENSIONS:
                raise sqlite3.DataError(
                    f"vec_dimension_mismatch: expected {DEFAULT_MEMORY_VEC_DIMENSIONS}, got {len(embedding)}"
                )
            conn.execute("DELETE FROM memory_vectors WHERE obs_id = ?", (int(obs_id),))
            conn.execute(
                "INSERT INTO memory_vectors(obs_id, project, embedding) VALUES (?, ?, ?)",
                (int(obs_id), project_row["project"] if project_row else None, json.dumps(embedding)),
            )
        except sqlite3.Error as exc:
            self._vec_error = str(exc)
            self._vec_enabled = False

    def rebuild_memory_vectors(self, *, conn: sqlite3.Connection | None = None) -> int:
        conn = conn or self.connect()
        if not self._vec_enabled:
            return 0
        rows = conn.execute("SELECT id, title, content FROM memory_observations ORDER BY id ASC").fetchall()
        rebuilt = 0
        with conn:
            conn.execute("DELETE FROM memory_vectors")
            for row in rows:
                self._upsert_memory_vector(conn, int(row["id"]), f"{row['title']}\n{row['content']}")
                rebuilt += 1
        return rebuilt

    def _memory_row_to_dict(self, row: sqlite3.Row, *, full: bool) -> dict[str, Any]:
        data = {
            "id": int(row["id"]),
            "project": row["project"],
            "source_doc": row["source_doc"],
            "section_key": row["section_key"],
            "type": row["type"],
            "title": row["title"],
            "importance": int(row["importance"]),
            "access_count": int(row["access_count"]),
            "created_at": row["created_at"],
            "tags": json.loads(row["tags_json"] or "[]"),
            "metadata": _json_loads_or_default(row["metadata_json"], {}, field_name="memory_observations.metadata_json"),
        }
        content = str(row["content"] or "")
        data["excerpt"] = content[:280] + ("..." if len(content) > 280 else "")
        if full:
            data["content"] = content
        return data

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

    def _applied_migration_rows(self, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        rows = conn.execute("SELECT version, sha256, applied_at_epoch FROM schema_migrations").fetchall()
        return {
            str(row["version"]): {
                "version": str(row["version"]),
                "sha256": row["sha256"],
                "applied_at_epoch": row["applied_at_epoch"],
            }
            for row in rows
        }


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


def _sqlite_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, dict, tuple, set)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def parse_repo_memory_markdown(project: str, source_doc: str, text: str) -> list[dict[str, Any]]:
    """Split repo-memory markdown into one observation per level-2 section.

    >>> rows = parse_repo_memory_markdown(
    ...     "demo",
    ...     "DECISIONS.md",
    ...     "# Demo\\n\\n## 2026-04-19 — Title: With Colon\\n**Context:** A\\n\\n## 2026-04-20 - Another Title\\nBody\\n",
    ... )
    >>> [row["title"] for row in rows]
    ['Title: With Colon', 'Another Title']
    >>> [row["section_key"] for row in rows]
    ['2026-04-19-title-with-colon', '2026-04-20-another-title']
    """
    matches = list(REPO_MEMORY_SECTION_RE.finditer(text or ""))
    if not matches:
        return []
    rows: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    source_type = _memory_type_for_doc(source_doc)
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        header = match.group(1).strip()
        body = (text[start:end] or "").strip()
        if not body:
            continue
        created_at, title = _parse_repo_memory_header(header)
        base_key = _slugify(header)
        seen[base_key] = seen.get(base_key, 0) + 1
        rows.append(
            {
                "project": project,
                "source_doc": source_doc,
                "section_key": _unique_section_key(base_key, seen[base_key]),
                "type": source_type,
                "title": title,
                "content": body,
                "created_at": created_at,
                "importance": _memory_importance_for_doc(source_doc),
                "tags": [source_type, pathlib.Path(source_doc).stem.lower()],
                "metadata": {"header": header},
            }
        )
    return rows


def _parse_repo_memory_header(header: str) -> tuple[str, str]:
    match = re.match(r"(?P<date>\d{4}-\d{2}-\d{2})\s+[—-]\s+(?P<title>.+)$", header)
    if not match:
        return "1970-01-01T00:00:00+00:00", header.strip()
    return f"{match.group('date')}T00:00:00+00:00", match.group("title").strip()


def _memory_type_for_doc(source_doc: str) -> str:
    name = pathlib.Path(source_doc).name.upper()
    if name == "DECISIONS.md".upper():
        return "decision"
    if name == "RECENT_WORK.md".upper():
        return "recent_work"
    if name == "FAILURES.md".upper():
        return "failure"
    if name == "RESEARCH.md".upper():
        return "research"
    return "reference"


def _memory_importance_for_doc(source_doc: str) -> int:
    return {
        "DECISIONS.md": 9,
        "FAILURES.md": 8,
        "RECENT_WORK.md": 7,
        "RESEARCH.md": 6,
    }.get(pathlib.Path(source_doc).name, 5)


def _unique_section_key(base: str, count: int) -> str:
    return base if count <= 1 else f"{base}-{count}"


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "section"


def _fts_query_for(query: str) -> str:
    terms = [part for part in re.split(r"\s+", (query or "").strip()) if part]
    if not terms:
        return ""
    return " OR ".join(f'"{term}"' for term in terms[:12])


def _semantic_embedding(text: str, *, dims: int = DEFAULT_MEMORY_VEC_DIMENSIONS) -> list[float]:
    vec = [0.0] * dims
    for token in re.findall(r"[a-z0-9_]+", (text or "").lower()):
        bucket = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16) % dims
        vec[bucket] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 6) for v in vec]


def _json_loads_or_default(payload: str | None, default: Any, *, field_name: str = "json") -> Any:
    if payload in (None, ""):
        return default
    try:
        return json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return default


def _float32_blob(values: Iterable[float]) -> bytes:
    items = list(values)
    return struct.pack(f"{len(items)}f", *items)

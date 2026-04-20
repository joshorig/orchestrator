#!/usr/bin/env python3
"""One-shot migration from filesystem state to the SQLite state engine."""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sqlite3
import sys
import tempfile


def _load_module(name: str, path: pathlib.Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sqlite_sidecars(db_path: pathlib.Path) -> list[pathlib.Path]:
    return [
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ]


def _backup_live_db(db_path: pathlib.Path, backup_path: pathlib.Path) -> None:
    """Create a self-contained SQLite backup from a live WAL database."""
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        backup_path.unlink()
    except FileNotFoundError:
        pass
    src = sqlite3.connect(db_path, timeout=30)
    dst = sqlite3.connect(backup_path, timeout=30)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _restore_backup_db(db_path: pathlib.Path, backup_path: pathlib.Path) -> None:
    """Restore a self-contained backup, removing stale WAL sidecars first."""
    for path in _sqlite_sidecars(db_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    shutil.copy2(backup_path, db_path)


def _replace_db_from_temp(db_path: pathlib.Path, temp_path: pathlib.Path) -> None:
    for path in _sqlite_sidecars(db_path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    temp_path.replace(db_path)


def migrate_from_fs(repo_root: pathlib.Path, db_path: pathlib.Path, *, backup_path: pathlib.Path | None = None) -> dict[str, int | str]:
    orchestrator = _load_module("orchestrator", repo_root / "bin" / "orchestrator.py")
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")

    backup_file = backup_path
    if backup_file is None:
        backup_file = db_path.with_suffix(db_path.suffix + ".bak")
    if db_path.exists():
        _backup_live_db(db_path, backup_file)

    temp_db = db_path.with_name(db_path.name + ".tmp")
    for path in _sqlite_sidecars(temp_db):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    engine = state_engine.StateEngine(
        state_engine.StateEngineConfig(
            root=repo_root,
            db_path=temp_db,
            migrations_dir=repo_root / "state" / "migrations",
            mode="mirror",
        )
    )
    engine.initialize()
    conn = engine.connect()
    try:
        _clear_engine_tables(conn)
        counts = {
            "tasks": 0,
            "features": 0,
            "transitions": 0,
            "events": 0,
            "metrics": 0,
            "memory_observations": 0,
        }
        for state in orchestrator.STATES:
            for path in (repo_root / "queue" / state).glob("*.json"):
                task = json.loads(path.read_text())
                engine.upsert_task_from_fs(task, state=state, conn=conn)
                counts["tasks"] += 1
        for path in (repo_root / "state" / "features").glob("*.json"):
            feature = json.loads(path.read_text())
            engine.upsert_feature_from_fs(feature, conn=conn)
            counts["features"] += 1
        for row in orchestrator.read_transitions():
            engine.record_transition(row, conn=conn)
            counts["transitions"] += 1
        for row in orchestrator.read_events():
            engine.record_event(row, conn=conn)
            counts["events"] += 1
        for row in orchestrator.read_metrics():
            engine.record_metric(row, conn=conn)
            counts["metrics"] += 1
        config = orchestrator.load_config()
        for project in config.get("projects", []):
            memdir = pathlib.Path(project["path"]) / "repo-memory"
            if not memdir.exists():
                continue
            for path in sorted(memdir.glob("*.md")):
                for observation in state_engine.parse_repo_memory_markdown(
                    project["name"],
                    path.name,
                    path.read_text(encoding="utf-8"),
                ):
                    engine.upsert_memory_observation(observation, conn=conn)
                    counts["memory_observations"] += 1
        engine.rebuild_memory_fts(conn=conn)
        engine.checkpoint(conn=conn)
        integrity = engine.integrity_check(conn=conn)
        if integrity != "ok":
            raise RuntimeError(f"integrity_check failed: {integrity}")
        engine.close()
        _replace_db_from_temp(db_path, temp_db)
        return {**counts, "integrity_check": integrity, "db_path": str(db_path), "backup_path": str(backup_file)}
    except Exception:
        engine.close()
        for path in _sqlite_sidecars(temp_db):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        if backup_file.exists():
            _restore_backup_db(db_path, backup_file)
        raise


def _clear_engine_tables(conn: sqlite3.Connection) -> None:
    with conn:
        for table in (
            "artifacts",
            "task_transitions",
            "feature_children",
            "self_repair_deliberations",
            "self_repair_issues",
            "tasks",
            "features",
            "memory_observations",
            "metrics",
            "events",
        ):
            conn.execute(f"DELETE FROM {table}")
        try:
            conn.execute("DELETE FROM memory_vectors")
        except sqlite3.Error:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="migrate_fs_to_engine")
    parser.add_argument("--repo-root", default=str(pathlib.Path(__file__).resolve().parent.parent))
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--backup-path", default=None)
    args = parser.parse_args(argv)

    repo_root = pathlib.Path(args.repo_root).resolve()
    db_path = pathlib.Path(args.db_path).resolve() if args.db_path else repo_root / "state" / "runtime" / "orchestrator.db"
    backup_path = pathlib.Path(args.backup_path).resolve() if args.backup_path else None
    print(json.dumps(migrate_from_fs(repo_root, db_path, backup_path=backup_path), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

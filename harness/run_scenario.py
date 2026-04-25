#!/usr/bin/env python3
import importlib.util
import hashlib
import json
import os
import pathlib
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
from datetime import datetime, timedelta, timezone


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_repo_modules(repo_root):
    orchestrator = _load_module("orchestrator", repo_root / "bin" / "orchestrator.py")
    worker = _load_module("worker", repo_root / "bin" / "worker.py")
    return orchestrator, worker


def _trace_root_base(repo_root):
    return repo_root / "harness" / "runs"


def _new_trace_root(repo_root, scenario_dir):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return _trace_root_base(repo_root) / stamp / scenario_dir.name


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _scenario_cluster(name, scenario_kind):
    if name.startswith("runner-r") or scenario_kind in {"runner_fixture_restore", "runner_trace_dirs", "runner_version_budgets", "runner_summary"}:
        return "runner"
    if scenario_kind == "workflow_e2e_story":
        return "workflow-e2e"
    if scenario_kind in {
        "state_engine_mirror", "fs_to_engine_migration", "atomic_claim_concurrency", "atomic_claim_concurrency_10",
        "kill9_integrity", "backup_roundtrip", "wal_backup_restore", "corrupt_db_fallback", "disk_full_insert",
        "eio_read", "db_deleted", "restore_active_rejected", "wal_growth_stalls", "migration_forward_drift",
        "migration_sha_mismatch", "migration_partial", "migration_idempotence", "fts5_recovery",
        "vector_rowid_divergence", "checkpoint_starved", "retention_purge_safe", "fk_constraint_violation",
        "clean_state_wipe_and_restart",
    }:
        return "state-engine"
    if scenario_kind in {"telegram_surface", "task_cost_capture", "supply_chain_gate", "security_secret_gate", "untrusted_skill_refusal", "launchd_runtime_env_contract", "template_contract_yaml_validates", "blocker_tier_routing"}:
        return "wave-c"
    if scenario_kind in {"review_feedback_exhaustion", "issue_replan_cap", "self_repair_resolution", "self_repair_observation",
                         "observation_orphan", "observation_idempotent", "clock_skew_backward", "council_timeout",
                         "council_malformed", "council_deleted_task_ref", "false_blocker_attach", "qa_preflight",
                         "project_main_dirty_cap", "regression_clear", "missing_child", "canary_fallback", "qa_contract_scoped",
                         "qa_contract_full_tick", "self_repair_issue_backfill", "attempt_cap", "fix2_reopen_after_manual_abandon",
                         "r16_override", "review_feedback_reaper_stale_target", "atomic_claim_feature_race_requeues_newer"}:
        return "self-repair"
    return "other"


def summarize_runs(repo_root, *, runs_dir=None):
    runs_root = pathlib.Path(runs_dir) if runs_dir else _trace_root_base(repo_root)
    latest_by_scenario = {}
    for result_path in sorted(runs_root.glob("*/**/result.json")):
        scenario_dir = result_path.parent
        name = scenario_dir.name
        latest_by_scenario[name] = result_path
    total = {"passed": 0, "failed": 0, "scenarios": 0}
    clusters = {}
    for name, result_path in sorted(latest_by_scenario.items()):
        result = json.loads(result_path.read_text())
        scenario = json.loads((result_path.parent / "scenario.json").read_text())
        cluster = _scenario_cluster(name, scenario.get("kind"))
        clusters.setdefault(cluster, {"passed": 0, "failed": 0, "scenarios": 0})
        passed = bool(result.get("passed"))
        total["scenarios"] += 1
        total["passed" if passed else "failed"] += 1
        clusters[cluster]["scenarios"] += 1
        clusters[cluster]["passed" if passed else "failed"] += 1
    for bucket in [total, *clusters.values()]:
        bucket["pass_rate"] = round((bucket["passed"] / bucket["scenarios"]) * 100.0, 1) if bucket["scenarios"] else 0.0
    return {"runs_dir": str(runs_root), "total": total, "clusters": clusters}


def _trace_enabled():
    return bool(os.environ.get("HARNESS_TRACE_ROOT"))


def _trace_trial_dir(index):
    root = os.environ.get("HARNESS_TRACE_ROOT")
    if not root:
        return None
    return pathlib.Path(root) / f"trial-{index + 1:02d}"


def _trace_workspace_snapshot(trial_dir, label, workspace_dir):
    if trial_dir is None:
        return
    snap_dir = trial_dir / "state_snapshots" / label
    if snap_dir.exists():
        shutil.rmtree(snap_dir)
    _copy_tree_contents(workspace_dir, snap_dir)


def _normalize_scenario_contract(scenario):
    out = dict(scenario)
    out.setdefault("scenario_version", 1)
    return out


def _extract_token_usage(actual):
    if isinstance(actual.get("token_usage"), int):
        return int(actual["token_usage"])
    if isinstance(actual.get("token_usage"), dict):
        value = actual["token_usage"].get("total")
        if isinstance(value, int):
            return value
    if isinstance(actual.get("total_tokens"), int):
        return int(actual["total_tokens"])
    return None


def _budget_report(actual, scenario, elapsed_seconds):
    report = {
        "scenario_version": int(scenario.get("scenario_version") or 1),
        "wall_time_seconds": elapsed_seconds,
        "wall_time_budget": scenario.get("wall_time_budget"),
        "token_budget": scenario.get("token_budget"),
        "token_usage": _extract_token_usage(actual),
        "wall_time_budget_exceeded": False,
        "token_budget_exceeded": False,
        "token_budget_check": "not_requested",
    }
    wall = scenario.get("wall_time_budget")
    if wall is not None:
        report["wall_time_budget_exceeded"] = elapsed_seconds > float(wall)
    tokens = scenario.get("token_budget")
    if tokens is not None:
        usage = report["token_usage"]
        if usage is None:
            report["token_budget_check"] = "unavailable"
        else:
            report["token_budget_check"] = "enforced"
            report["token_budget_exceeded"] = usage > int(tokens)
    return report


def _copy_tree_contents(src, dst):
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dst / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


def _clear_dir_contents(path):
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _run_trials_with_fixture_snapshot(scenario_dir, scenario, fn):
    fixture_dir = scenario_dir / "fixture"
    trial_count = int(scenario.get("trial_count") or 1)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        snapshot_dir = root / "_snapshot"
        workspace_dir = root / "workspace"
        _copy_tree_contents(fixture_dir, snapshot_dir)
        results = []
        for index in range(trial_count):
            workspace_dir.mkdir(parents=True, exist_ok=True)
            _clear_dir_contents(workspace_dir)
            _copy_tree_contents(snapshot_dir, workspace_dir)
            trial_dir = _trace_trial_dir(index)
            if trial_dir is not None:
                _write_json(trial_dir / "meta.json", {"trial_index": index + 1, "scenario_kind": scenario.get("kind")})
                _trace_workspace_snapshot(trial_dir, "before", workspace_dir)
            results.append(fn(workspace_dir, index))
            if trial_dir is not None:
                _trace_workspace_snapshot(trial_dir, "after", workspace_dir)
        return results


def _run_attempt_cap(repo_root, scenario_dir, scenario):
    orchestrator, worker = _load_repo_modules(repo_root)
    mock = _load_module("mock_reviewer", scenario_dir / "mock_reviewer.py")
    review = mock.review()
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        old = {
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "REPORT_DIR": orchestrator.REPORT_DIR,
            "should_push_alert": orchestrator.should_push_alert,
            "max_task_attempts": orchestrator.max_task_attempts,
        }
        orchestrator.QUEUE_ROOT = root / "queue"
        orchestrator.REPORT_DIR = root / "reports"
        orchestrator.should_push_alert = lambda key, seconds: True
        orchestrator.max_task_attempts = lambda cfg=None: int(scenario["max_task_attempts"])
        try:
            task = orchestrator.new_task(
                role="implementer",
                engine="codex",
                project=scenario["project"],
                summary=scenario["summary"],
                source=scenario.get("source") or "scenario-attempt-cap",
                feature_id=scenario["feature_id"],
                braid_template=scenario["braid_template"],
            )
            task["task_id"] = scenario["task_id"]
            task["state"] = scenario["initial_state"]
            task["attempt"] = int(scenario["initial_attempt"])
            task["review_verdict"] = review["review_verdict"]
            task["review_feedback_rounds"] = int(scenario["review_feedback_rounds"])
            task["policy_review_findings"] = list(review["policy_review_findings"])
            orchestrator.write_json_atomic(orchestrator.task_path(task["task_id"], scenario["initial_state"]), task)

            ratio_cfg = scenario.get("ratio_cfg") or {
                "review_policy": {
                    "test_to_code_ratio": {
                        "min_ratio": 0.5,
                        "accept_doctest": False,
                        "template_overrides": {},
                    }
                }
            }
            ratio_findings = worker._test_ratio_findings(
                review["changed_files_text"],
                task=task,
                worktree=root,
                cfg=ratio_cfg,
            )
            out = orchestrator.reset_task_for_retry(
                task["task_id"],
                scenario["initial_state"],
                reason=scenario["retry_reason"],
                source=scenario.get("retry_source") or "scenario-attempt-cap",
            )
        finally:
            orchestrator.QUEUE_ROOT = old["QUEUE_ROOT"]
            orchestrator.REPORT_DIR = old["REPORT_DIR"]
            orchestrator.should_push_alert = old["should_push_alert"]
            orchestrator.max_task_attempts = old["max_task_attempts"]

        alerts = list((root / "reports").glob("workflow-alert_*.md"))
        return {
            "ratio_findings": ratio_findings,
            "final_state": out["state"],
            "blocker_code": (out.get("blocker") or {}).get("code"),
            "alert_count": len(alerts),
            "review_verdict": out.get("review_verdict"),
            "policy_review_findings": out.get("policy_review_findings") or [],
        }


def _run_fix2_reopen(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        feats = root / "features"
        feats.mkdir()
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old = {
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "new_task": orchestrator.new_task,
            "enqueue_task": orchestrator.enqueue_task,
            "append_feature_child": orchestrator.append_feature_child,
            "append_event": orchestrator.append_event,
        }
        old_env = {
            "STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"),
            "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH"),
        }
        captured = {}
        os.environ["STATE_ENGINE_MODE"] = "off"
        os.environ["STATE_ENGINE_PATH"] = str(root / "runtime" / "scenario.db")
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        orchestrator.FEATURES_DIR = feats
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.new_task = lambda **kwargs: {"task_id": scenario["new_task_id"], **kwargs}
        orchestrator.enqueue_task = lambda task: captured.setdefault("task", dict(task))
        orchestrator.append_feature_child = (
            lambda fid, tid: orchestrator.update_feature(fid, lambda f: f.setdefault("child_task_ids", []).append(tid))
        )
        orchestrator.append_event = lambda *args, **kwargs: None
        try:
            abandoned = {
                "task_id": scenario["old_task_id"],
                "state": "abandoned",
                "attempt": 1,
                "finished_at": "2026-04-19T16:22:47",
            }
            orchestrator.write_json_atomic(queue_root / "abandoned" / f"{scenario['old_task_id']}.json", abandoned)
            feature = {
                "feature_id": scenario["feature_id"],
                "project": scenario["project"],
                "status": "open",
                "summary": scenario["summary"],
                "source": "self-repair:test",
                "child_task_ids": [],
                "self_repair": {
                    "enabled": True,
                    "issues": [
                        {
                            "issue_key": scenario["issue_key"],
                            "summary": scenario["summary"],
                            "evidence": "synthetic",
                            "source": "workflow-check",
                            "issue_kind": "frontier_task_blocked",
                            "status": "pending",
                            "planner_task_id": scenario["old_task_id"],
                            "superseded_task_ids": [],
                        }
                    ],
                },
            }
            orchestrator.write_json_atomic(feats / f"{scenario['feature_id']}.json", feature)
            out = orchestrator.tick_self_repair_queue()
            saved = orchestrator.read_json(feats / f"{scenario['feature_id']}.json", {})
        finally:
            orchestrator.FEATURES_DIR = old["FEATURES_DIR"]
            orchestrator.QUEUE_ROOT = old["QUEUE_ROOT"]
            orchestrator.new_task = old["new_task"]
            orchestrator.enqueue_task = old["enqueue_task"]
            orchestrator.append_feature_child = old["append_feature_child"]
            orchestrator.append_event = old["append_event"]
            orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]
            if old_env["STATE_ENGINE_PATH"] is None:
                os.environ.pop("STATE_ENGINE_PATH", None)
            else:
                os.environ["STATE_ENGINE_PATH"] = old_env["STATE_ENGINE_PATH"]

        issue = saved["self_repair"]["issues"][0]
        return {
            "scheduled": out["scheduled"],
            "planner_task_id": issue.get("planner_task_id"),
            "status": issue.get("status"),
            "superseded_task_ids": issue.get("superseded_task_ids") or [],
            "execution_task_ids": issue.get("execution_task_ids") or [],
            "captured_task_id": (captured.get("task") or {}).get("task_id"),
        }


def _run_r16_override(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    cfg = scenario["cfg"]
    task = {"braid_template": scenario["braid_template"]}
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        for rel_path, body in (scenario.get("worktree_files") or {}).items():
            target = root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        ratio_findings = worker._test_ratio_findings(
            scenario["changed_files_text"],
            task=task,
            worktree=root,
            cfg=cfg,
        )
        validation_findings = worker._validation_evidence_findings(
            scenario["changed_files_text"],
            task=task,
            worktree=root,
            cfg=cfg,
        )
        hard_findings = worker._hard_review_gate_findings([], [], [], [], validation_findings)
    return {
        "ratio_findings": ratio_findings,
        "validation_findings": validation_findings,
        "hard_findings": hard_findings,
        "review_verdict": "approve" if not hard_findings else "request_change",
    }


def _run_migration_forward_drift(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        migrations = root / "migrations"
        migrations.mkdir(parents=True, exist_ok=True)
        (migrations / "0001_initial.sql").write_text("CREATE TABLE demo(id INTEGER PRIMARY KEY);\n", encoding="utf-8")
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(
                root=root,
                db_path=root / "runtime" / "orchestrator.db",
                migrations_dir=migrations,
                mode="mirror",
            )
        )
        engine.initialize()
        conn = engine.connect()
        with conn:
            conn.execute(
                "INSERT INTO schema_migrations (version, sha256, applied_at_epoch) VALUES (?, ?, ?)",
                ("0004_future", "deadbeef", 1),
            )
        error = None
        try:
            engine.validate_migrations(conn=conn)
        except Exception as exc:
            error = str(exc)
        return {
            "error_has_forward_drift": "forward drift" in (error or ""),
            "mentions_version": "0004_future" in (error or ""),
        }


def _run_migration_sha_mismatch(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        migrations = root / "migrations"
        migrations.mkdir(parents=True, exist_ok=True)
        path = migrations / "0001_initial.sql"
        path.write_text("CREATE TABLE demo(id INTEGER PRIMARY KEY);\n", encoding="utf-8")
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(
                root=root,
                db_path=root / "runtime" / "orchestrator.db",
                migrations_dir=migrations,
                mode="mirror",
            )
        )
        engine.initialize()
        path.write_text("CREATE TABLE demo(id INTEGER PRIMARY KEY, name TEXT);\n", encoding="utf-8")
        error = None
        try:
            engine.validate_migrations(conn=engine.connect())
        except Exception as exc:
            error = str(exc)
        return {
            "error_has_sha_mismatch": "sha mismatch" in (error or ""),
            "mentions_version": "0001_initial" in (error or ""),
        }


def _run_self_repair_resolution(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        feats = root / "features"
        feats.mkdir()
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old = {
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "append_event": orchestrator.append_event,
            "append_transition": orchestrator.append_transition,
        }
        old_env = {
            "STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"),
            "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH"),
        }
        events = []
        transitions = []
        os.environ["STATE_ENGINE_MODE"] = "off"
        os.environ["STATE_ENGINE_PATH"] = str(root / "runtime" / "scenario.db")
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        orchestrator.FEATURES_DIR = feats
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.append_event = lambda *args, **kwargs: events.append({"args": args, "kwargs": kwargs})
        orchestrator.append_transition = lambda *args: transitions.append(args)
        try:
            for task_state, task in scenario.get("queue_tasks", {}).items():
                for task_obj in task:
                    orchestrator.write_json_atomic(
                        queue_root / task_state / f"{task_obj['task_id']}.json",
                        dict(task_obj),
                    )
            feature = {
                "feature_id": scenario["feature_id"],
                "project": scenario["project"],
                "status": "open",
                "self_repair": {
                    "enabled": True,
                    "issues": [dict(scenario["issue"])],
                },
            }
            orchestrator.write_json_atomic(feats / f"{scenario['feature_id']}.json", feature)
            out = orchestrator.tick_self_repair_resolution()
            saved = orchestrator.read_json(feats / f"{scenario['feature_id']}.json", {})
        finally:
            orchestrator.FEATURES_DIR = old["FEATURES_DIR"]
            orchestrator.QUEUE_ROOT = old["QUEUE_ROOT"]
            orchestrator.append_event = old["append_event"]
            orchestrator.append_transition = old["append_transition"]
            orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]
            if old_env["STATE_ENGINE_PATH"] is None:
                os.environ.pop("STATE_ENGINE_PATH", None)
            else:
                os.environ["STATE_ENGINE_PATH"] = old_env["STATE_ENGINE_PATH"]
        issue = saved["self_repair"]["issues"][0]
        return {
            "tick": out,
            "status": issue.get("status"),
            "has_resolved_at": bool(issue.get("resolved_at")),
            "has_stalled_at": bool(issue.get("stalled_at")),
            "resolution": issue.get("resolution"),
            "completed_execution_task_ids": issue.get("completed_execution_task_ids") or [],
            "execution_task_ids": issue.get("execution_task_ids") or [],
            "stalled_reason": issue.get("stalled_reason"),
            "transition_labels": [list(row[1:4]) for row in transitions],
            "event_names": [row["args"][1] for row in events],
        }


def _run_review_feedback_exhaustion(repo_root, scenario):
    orchestrator, worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old_orch = {
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "REPORT_DIR": orchestrator.REPORT_DIR,
            "_write_pr_alert": orchestrator._write_pr_alert,
        }
        old_worker = {
            "o": worker.o,
        }
        alerts = []
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.REPORT_DIR = root / "reports"
        worker.o = orchestrator
        worker.o._write_pr_alert = lambda project_name, target_id, pr_number, reason, pr_url: alerts.append(
            {"project": project_name, "task_id": target_id, "reason": reason}
        )
        try:
            task = orchestrator.new_task(
                role="implementer",
                engine="codex",
                project=scenario["project"],
                summary=scenario["summary"],
                source="scenario-review-feedback-exhaustion",
                feature_id=scenario["feature_id"],
                braid_template=scenario["braid_template"],
            )
            task["task_id"] = scenario["task_id"]
            task["state"] = "awaiting-review"
            task["review_feedback_rounds"] = int(scenario["initial_review_feedback_rounds"])
            orchestrator.write_json_atomic(orchestrator.task_path(task["task_id"], "awaiting-review"), task)
            worker._handle_review_request_change(
                scenario["reviewer_task_id"],
                scenario["project"],
                task,
                scenario["review_findings"],
                lambda t: t.update({"reviewed_by": scenario["reviewer_task_id"]}),
            )
            abandoned = orchestrator.read_json(orchestrator.task_path(task["task_id"], "abandoned"), {})
            planners = list(orchestrator.iter_tasks(states=("queued",), engine="claude"))
        finally:
            orchestrator.QUEUE_ROOT = old_orch["QUEUE_ROOT"]
            orchestrator.REPORT_DIR = old_orch["REPORT_DIR"]
            orchestrator._write_pr_alert = old_orch["_write_pr_alert"]
            worker.o = old_worker["o"]
        planner = planners[0] if planners else {}
        return {
            "state": abandoned.get("state"),
            "planner_mode": ((planner.get("engine_args") or {}).get("mode")),
            "planner_origin_task_id": (((planner.get("engine_args") or {}).get("planner_refine") or {}).get("origin_task_id")),
            "review_feedback_rounds": abandoned.get("review_feedback_rounds"),
            "abandoned_reason": abandoned.get("abandoned_reason"),
            "alert_count": len(alerts),
        }


def _run_issue_replan_cap(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        feats = root / "features"
        feats.mkdir()
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old = {
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "new_task": orchestrator.new_task,
            "enqueue_task": orchestrator.enqueue_task,
            "append_feature_child": orchestrator.append_feature_child,
            "_write_workflow_alert": orchestrator._write_workflow_alert,
        }
        old_env = {
            "STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"),
            "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH"),
        }
        alerts = []
        enqueued = []
        os.environ["STATE_ENGINE_MODE"] = "off"
        os.environ["STATE_ENGINE_PATH"] = str(root / "runtime" / "scenario.db")
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        orchestrator.FEATURES_DIR = feats
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.new_task = lambda **kwargs: {"task_id": "unexpected-task", **kwargs}
        orchestrator.enqueue_task = lambda task: enqueued.append(task["task_id"])
        orchestrator.append_feature_child = lambda fid, tid: None
        orchestrator._write_workflow_alert = lambda issue, reason: alerts.append({"issue_key": issue["issue_key"], "reason": reason}) or "alert.md"
        try:
            feature = {
                "feature_id": scenario["feature_id"],
                "project": scenario["project"],
                "status": "open",
                "summary": scenario["summary"],
                "source": "self-repair:test",
                "child_task_ids": [],
                "self_repair": {
                    "enabled": True,
                    "issues": [dict(scenario["issue"])],
                },
            }
            orchestrator.write_json_atomic(feats / f"{scenario['feature_id']}.json", feature)
            out = orchestrator.tick_self_repair_queue()
            saved = orchestrator.read_json(feats / f"{scenario['feature_id']}.json", {})
        finally:
            orchestrator.FEATURES_DIR = old["FEATURES_DIR"]
            orchestrator.QUEUE_ROOT = old["QUEUE_ROOT"]
            orchestrator.new_task = old["new_task"]
            orchestrator.enqueue_task = old["enqueue_task"]
            orchestrator.append_feature_child = old["append_feature_child"]
            orchestrator._write_workflow_alert = old["_write_workflow_alert"]
            orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]
            if old_env["STATE_ENGINE_PATH"] is None:
                os.environ.pop("STATE_ENGINE_PATH", None)
            else:
                os.environ["STATE_ENGINE_PATH"] = old_env["STATE_ENGINE_PATH"]
        issue = saved["self_repair"]["issues"][0]
        return {
            "tick": out,
            "status": issue.get("status"),
            "has_escalated_at": bool(issue.get("escalated_at")),
            "escalated_reason": issue.get("escalated_reason"),
            "alerts": len(alerts),
            "enqueued": enqueued,
        }


def _run_false_blocker_attach(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        feats = root / "features"
        feats.mkdir()
        old = {
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
        }
        orchestrator.FEATURES_DIR = feats
        try:
            orchestrator.write_json_atomic(
                feats / "feature-self-repair.json",
                {
                    "feature_id": "feature-self-repair",
                    "project": "devmini-orchestrator",
                    "status": "open",
                    "summary": "active self repair",
                    "source": "self-repair:test",
                    "self_repair": {"enabled": True, "issues": []},
                },
            )
            issue = {
                "kind": "frontier_task_blocked",
                "blocker": {"code": "false_blocker_claim", "detail": scenario["detail"]},
            }
            task = {"source": scenario["source"]}
            action, diagnosis, policy = orchestrator._workflow_policy_decision(issue, task, {"name": "demo"})
        finally:
            orchestrator.FEATURES_DIR = old["FEATURES_DIR"]
        return {
            "action": action,
            "policy_name": policy,
            "diagnosis": diagnosis,
        }


def _run_qa_preflight(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "qa").mkdir()
        (root / "config").mkdir()
        script = root / "qa" / scenario["script_name"]
        script.write_text("#!/usr/bin/env bash\nexit 0\n")
        script.chmod(0o755 if scenario.get("executable", True) else 0o644)
        if scenario.get("bad_config"):
            (root / "config" / "orchestrator.local.json").write_text("{bad json")
        result = worker._qa_contract_preflight(
            {"name": scenario["project_name"], "path": str(root)},
            scenario["contract_kind"],
            f"qa/{scenario['script_name']}",
        )
        error = result.get("error")
        if isinstance(error, str) and error.startswith("invalid orchestrator config: "):
            error = "invalid orchestrator config: __DYNAMIC__"
        return {
            "ok": result.get("ok"),
            "summary": result.get("summary"),
            "error": error,
        }


def _run_project_main_dirty_cap(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        feats = root / "features"
        feats.mkdir()
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old = {
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "REPORT_DIR": orchestrator.REPORT_DIR,
            "EVENTS_LOG": orchestrator.EVENTS_LOG,
            "PROJECT_HARD_STOPS_PATH": orchestrator.PROJECT_HARD_STOPS_PATH,
            "load_config": orchestrator.load_config,
            "emit_runtime_metrics_snapshot": orchestrator.emit_runtime_metrics_snapshot,
            "write_agent_status": orchestrator.write_agent_status,
            "reap": orchestrator.reap,
            "_repair_project_main_checkout": orchestrator._repair_project_main_checkout,
            "_workflow_check_retry_task": orchestrator._workflow_check_retry_task,
            "_write_workflow_check_report": orchestrator._write_workflow_check_report,
            "_write_workflow_alert": orchestrator._write_workflow_alert,
            "tick_self_repair_resolution": orchestrator.tick_self_repair_resolution,
            "tick_self_repair_queue": orchestrator.tick_self_repair_queue,
        }
        alerts = []
        orchestrator.FEATURES_DIR = feats
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.REPORT_DIR = root / "reports"
        orchestrator.EVENTS_LOG = root / "events.jsonl"
        orchestrator.PROJECT_HARD_STOPS_PATH = root / "project-hard-stops.json"
        orchestrator.load_config = lambda: {
            "workflow_check_max_attempts": int(scenario["max_attempts"]),
            "projects": [
                {"name": scenario["project"], "path": str(root / "repo")},
                {"name": "devmini-orchestrator", "path": str(root / "orchestrator")},
            ],
            "synthetic_canary": {"enabled": False},
        }
        orchestrator.emit_runtime_metrics_snapshot = lambda **kwargs: None
        orchestrator.write_agent_status = lambda *args, **kwargs: None
        orchestrator.reap = lambda: 0
        orchestrator._repair_project_main_checkout = lambda project: {"fixed": False, "detail": "still dirty"}
        orchestrator._workflow_check_retry_task = lambda *args, **kwargs: False
        orchestrator._write_workflow_check_report = lambda issues, reaped=0: None
        orchestrator._write_workflow_alert = lambda issue, reason: alerts.append(reason) or "alert.md"
        orchestrator.tick_self_repair_resolution = lambda: {"resolved": 0, "stalled": 0}
        orchestrator.tick_self_repair_queue = lambda: {"scheduled": 0}
        try:
            task = orchestrator.new_task(
                role="implementer",
                engine="codex",
                project=scenario["project"],
                summary="dirty main task",
                source="scenario-28",
                feature_id=scenario["feature_id"],
            )
            task["task_id"] = scenario["task_id"]
            task["state"] = "blocked"
            task["blocker"] = orchestrator.make_blocker(
                "project_main_dirty",
                summary="project main checkout dirty",
                detail="dirty main",
                source="scenario",
                retryable=True,
            )
            orchestrator.write_json_atomic(orchestrator.task_path(task["task_id"], "blocked"), task)
            feature = {
                "feature_id": scenario["feature_id"],
                "project": scenario["project"],
                "status": "open",
                "summary": "dirty main feature",
                "child_task_ids": [scenario["task_id"]],
            }
            orchestrator.write_json_atomic(feats / f"{scenario['feature_id']}.json", feature)
            snapshots = []
            for _ in range(4):
                orchestrator.tick_workflow_check()
                saved = orchestrator.read_json(feats / f"{scenario['feature_id']}.json", {})
                attempts = ((saved.get("workflow_check") or {}).get("attempts") or {}).get(
                    f"task:{scenario['task_id']}:blocked:project_main_dirty",
                    0,
                )
                snapshots.append(
                    {
                        "attempts": attempts,
                        "hard_stopped": orchestrator.project_hard_stopped(scenario["project"]),
                    }
                )
            events = orchestrator.read_events(role="workflow-check")
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
        return {
            "ticks": snapshots,
            "alert_count": len(alerts),
            "hard_stop_event": any(row.get("event") == "project_hard_stopped" for row in events),
        }


def _run_regression_clear(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    results = {}
    for mode in ("green_run", "human_push"):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            feats = root / "features"
            feats.mkdir()
            queue_root = root / "queue"
            for state in orchestrator.STATES:
                (queue_root / state).mkdir(parents=True, exist_ok=True)
            old = {
                "FEATURES_DIR": orchestrator.FEATURES_DIR,
                "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
                "REPORT_DIR": orchestrator.REPORT_DIR,
                "EVENTS_LOG": orchestrator.EVENTS_LOG,
                "PROJECT_HARD_STOPS_PATH": orchestrator.PROJECT_HARD_STOPS_PATH,
                "load_config": orchestrator.load_config,
                "emit_runtime_metrics_snapshot": orchestrator.emit_runtime_metrics_snapshot,
                "write_agent_status": orchestrator.write_agent_status,
                "reap": orchestrator.reap,
                "_write_workflow_check_report": orchestrator._write_workflow_check_report,
                "tick_self_repair_resolution": orchestrator.tick_self_repair_resolution,
                "tick_self_repair_queue": orchestrator.tick_self_repair_queue,
                "_project_green_regression_after": orchestrator._project_green_regression_after,
                "_project_human_push_after": orchestrator._project_human_push_after,
            }
            orchestrator.FEATURES_DIR = feats
            orchestrator.QUEUE_ROOT = queue_root
            orchestrator.REPORT_DIR = root / "reports"
            orchestrator.EVENTS_LOG = root / "events.jsonl"
            orchestrator.PROJECT_HARD_STOPS_PATH = root / "project-hard-stops.json"
            orchestrator.load_config = lambda: {
                "workflow_check_max_attempts": 3,
                "projects": [
                    {"name": scenario["project"], "path": str(root / "repo")},
                    {"name": "devmini-orchestrator", "path": str(root / "orchestrator")},
                ],
                "synthetic_canary": {"enabled": False},
            }
            orchestrator.emit_runtime_metrics_snapshot = lambda **kwargs: None
            orchestrator.write_agent_status = lambda *args, **kwargs: None
            orchestrator.reap = lambda: 0
            orchestrator._write_workflow_check_report = lambda issues, reaped=0: None
            orchestrator.tick_self_repair_resolution = lambda: {"resolved": 0, "stalled": 0}
            orchestrator.tick_self_repair_queue = lambda: {"scheduled": 0}
            orchestrator._project_green_regression_after = lambda project_name, failed_at: mode == "green_run"
            orchestrator._project_human_push_after = lambda project, failed_at: mode == "human_push"
            try:
                task = orchestrator.new_task(
                    role="implementer",
                    engine="codex",
                    project=scenario["project"],
                    summary="regression block",
                    source="scenario-29",
                    feature_id=scenario["feature_id"],
                )
                task["task_id"] = scenario["task_id"]
                task["state"] = "blocked"
                task["finished_at"] = scenario["failed_at"]
                task["blocker"] = orchestrator.make_blocker(
                    "project_regression_failed",
                    summary="project regression failed",
                    detail="regression failure",
                    source="scenario",
                    retryable=False,
                )
                task["topology_error"] = "regression-failure"
                orchestrator.write_json_atomic(orchestrator.task_path(task["task_id"], "blocked"), task)
                feature = {
                    "feature_id": scenario["feature_id"],
                    "project": scenario["project"],
                    "status": "open",
                    "summary": "regression feature",
                    "child_task_ids": [scenario["task_id"]],
                }
                orchestrator.write_json_atomic(feats / f"{scenario['feature_id']}.json", feature)
                orchestrator.tick_workflow_check()
                found = orchestrator.find_task(task["task_id"])
                events = orchestrator.read_events(role="workflow-check")
                hard_stopped = orchestrator.project_hard_stopped(scenario["project"])
            finally:
                for key, value in old.items():
                    setattr(orchestrator, key, value)
            results[mode] = {
                "state": found[0] if found else None,
                "hard_stopped": hard_stopped,
                "cleared_event": next(
                    (row.get("details", {}).get("cleared_by") for row in events if row.get("event") == "project_regression_cleared"),
                    None,
                ),
            }
    return results


def _run_missing_child(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    results = {}
    for mode in ("reconstruct", "unrecoverable"):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            feats = root / "features"
            feats.mkdir()
            queue_root = root / "queue"
            for state in orchestrator.STATES:
                (queue_root / state).mkdir(parents=True, exist_ok=True)
            old = {
                "FEATURES_DIR": orchestrator.FEATURES_DIR,
                "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
                "REPORT_DIR": orchestrator.REPORT_DIR,
                "EVENTS_LOG": orchestrator.EVENTS_LOG,
                "TRANSITIONS_LOG": orchestrator.TRANSITIONS_LOG,
                "load_config": orchestrator.load_config,
                "emit_runtime_metrics_snapshot": orchestrator.emit_runtime_metrics_snapshot,
                "write_agent_status": orchestrator.write_agent_status,
                "reap": orchestrator.reap,
                "_write_workflow_check_report": orchestrator._write_workflow_check_report,
                "_write_workflow_alert": orchestrator._write_workflow_alert,
                "tick_self_repair_resolution": orchestrator.tick_self_repair_resolution,
                "tick_self_repair_queue": orchestrator.tick_self_repair_queue,
            }
            alerts = []
            orchestrator.FEATURES_DIR = feats
            orchestrator.QUEUE_ROOT = queue_root
            orchestrator.REPORT_DIR = root / "reports"
            orchestrator.EVENTS_LOG = root / "events.jsonl"
            orchestrator.TRANSITIONS_LOG = root / "transitions.log"
            orchestrator.load_config = lambda: {
                "workflow_check_max_attempts": 3,
                "projects": [
                    {"name": scenario["project"], "path": str(root / "repo")},
                    {"name": "devmini-orchestrator", "path": str(root / "orchestrator")},
                ],
                "synthetic_canary": {"enabled": False},
            }
            orchestrator.emit_runtime_metrics_snapshot = lambda **kwargs: None
            orchestrator.write_agent_status = lambda *args, **kwargs: None
            orchestrator.reap = lambda: 0
            orchestrator._write_workflow_check_report = lambda issues, reaped=0: None
            orchestrator._write_workflow_alert = lambda issue, reason: alerts.append(reason) or "alert.md"
            orchestrator.tick_self_repair_resolution = lambda: {"resolved": 0, "stalled": 0}
            orchestrator.tick_self_repair_queue = lambda: {"scheduled": 0}
            try:
                if mode == "reconstruct":
                    orchestrator.TRANSITIONS_LOG.write_text(
                        f"2026-04-15T10:00:00\t{scenario['task_id']}\tqueued\t->\tclaimed\tclaim\n"
                        f"2026-04-15T10:01:00\t{scenario['task_id']}\tclaimed\t->\tdone\tdone\n"
                    )
                feature = {
                    "feature_id": scenario["feature_id"],
                    "project": scenario["project"],
                    "status": "open",
                    "summary": "missing child feature",
                    "child_task_ids": [scenario["task_id"]],
                }
                orchestrator.write_json_atomic(feats / f"{scenario['feature_id']}.json", feature)
                orchestrator.tick_workflow_check()
                saved = orchestrator.read_json(feats / f"{scenario['feature_id']}.json", {})
                reconstructed = orchestrator.read_json(queue_root / "done" / f"{scenario['task_id']}.json", {})
            finally:
                for key, value in old.items():
                    setattr(orchestrator, key, value)
            results[mode] = {
                "feature_status": saved.get("status"),
                "blocker_code": ((saved.get("blocker") or {}).get("code")),
                "reconstructed_state": reconstructed.get("state"),
                "alert_count": len(alerts),
            }
    return results


def _run_canary_fallback(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    results = {}
    for mode in ("fallback_ok", "fallback_stale"):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            primary_repo = root / "primary"
            fallback_repo = root / "fallback"
            for repo in (primary_repo, fallback_repo):
                (repo / "repo-memory").mkdir(parents=True, exist_ok=True)
                (repo / "repo-memory" / "CURRENT_STATE.md").write_text("ok\n")
            old = {
                "FEATURES_DIR": orchestrator.FEATURES_DIR,
                "EVENTS_LOG": orchestrator.EVENTS_LOG,
                "_write_workflow_alert": orchestrator._write_workflow_alert,
                "load_config": orchestrator.load_config,
                "engine_outstanding": orchestrator.engine_outstanding,
                "write_agent_status": orchestrator.write_agent_status,
                "create_feature": orchestrator.create_feature,
                "new_task": orchestrator.new_task,
                "enqueue_task": orchestrator.enqueue_task,
                "append_event": orchestrator.append_event,
                "project_environment_ok": orchestrator.project_environment_ok,
                "_project_has_open_feature": orchestrator._project_has_open_feature,
                "planner_disabled": orchestrator.planner_disabled,
                "project_hard_stopped": orchestrator.project_hard_stopped,
                "_canary_success_overdue": orchestrator._canary_success_overdue,
                "list_canary_features": orchestrator.list_canary_features,
                "_canary_interval_due": orchestrator._canary_interval_due,
                "project_historian_template": orchestrator.project_historian_template,
            }
            alerts = []
            calls = []
            orchestrator.FEATURES_DIR = root / "features"
            orchestrator.FEATURES_DIR.mkdir()
            orchestrator.EVENTS_LOG = root / "events.jsonl"
            orchestrator._write_workflow_alert = lambda issue, reason: alerts.append(reason) or "alert.md"
            orchestrator.load_config = lambda: {
                "projects": [
                    {"name": "primary", "path": str(primary_repo)},
                    {"name": "fallback", "path": str(fallback_repo)},
                ],
                "synthetic_canary": {
                    "enabled": True,
                    "project": "primary",
                    "fallback_project": "fallback",
                    "summary": "Canary",
                    "roadmap_entry_id": "C-1",
                    "roadmap_title": "Canary",
                    "roadmap_body": "body",
                    "interval_hours": 6,
                    "success_sla_hours": 24,
                },
            }
            orchestrator.engine_outstanding = lambda: {"claude": 0, "codex": 0, "qa": 0}
            orchestrator.write_agent_status = lambda *args, **kwargs: None
            orchestrator.create_feature = lambda **kwargs: {"feature_id": "feature-canary", **kwargs}
            orchestrator.new_task = lambda **kwargs: {"task_id": "task-canary", **kwargs}
            orchestrator.enqueue_task = lambda task: calls.append(task)
            orchestrator.append_event = lambda *args, **kwargs: None
            orchestrator.project_environment_ok = lambda name, refresh=True: True
            orchestrator._project_has_open_feature = lambda name: False
            orchestrator.planner_disabled = lambda name: False
            orchestrator.project_hard_stopped = lambda name: False
            orchestrator._canary_success_overdue = lambda cfg, project_name: (mode == "fallback_stale", None) if project_name == "fallback" else (True, None)
            orchestrator.list_canary_features = lambda **kwargs: []
            orchestrator._canary_interval_due = lambda cfg, project_name: (True, None)
            orchestrator.project_historian_template = lambda project_name: "template"
            try:
                issue = {"project": "primary", "feature_id": "feature-primary", "issue_key": "canary:primary"}
                out = orchestrator._enqueue_canary_fallback(issue, orchestrator.load_config())
            finally:
                for key, value in old.items():
                    setattr(orchestrator, key, value)
            results[mode] = {
                "reason": out.get("reason"),
                "blocker_code": out.get("blocker_code"),
                "enqueued_project": (calls[0] if calls else {}).get("project"),
                "alert_count": len(alerts),
            }
    return results


def _run_qa_contract_scoped(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        feats = root / "features"
        feats.mkdir()
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old = {
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "REPORT_DIR": orchestrator.REPORT_DIR,
            "EVENTS_LOG": orchestrator.EVENTS_LOG,
            "load_config": orchestrator.load_config,
            "emit_runtime_metrics_snapshot": orchestrator.emit_runtime_metrics_snapshot,
            "write_agent_status": orchestrator.write_agent_status,
            "reap": orchestrator.reap,
            "_write_workflow_check_report": orchestrator._write_workflow_check_report,
            "tick_self_repair_resolution": orchestrator.tick_self_repair_resolution,
            "tick_self_repair_queue": orchestrator.tick_self_repair_queue,
            "project_environment_ok": orchestrator.project_environment_ok,
            "project_hard_stopped": orchestrator.project_hard_stopped,
            "enqueue_task": orchestrator.enqueue_task,
        }
        orchestrator.FEATURES_DIR = feats
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.REPORT_DIR = root / "reports"
        orchestrator.EVENTS_LOG = root / "events.jsonl"
        orchestrator.load_config = lambda: {
            "workflow_check_max_attempts": 3,
            "projects": [
                {"name": scenario["project"], "path": str(root / "repo")},
                {"name": "devmini-orchestrator", "path": str(root / "orchestrator")},
            ],
            "synthetic_canary": {"enabled": False},
        }
        orchestrator.emit_runtime_metrics_snapshot = lambda **kwargs: None
        orchestrator.write_agent_status = lambda *args, **kwargs: None
        orchestrator.reap = lambda: 0
        orchestrator._write_workflow_check_report = lambda issues, reaped=0: None
        orchestrator.tick_self_repair_resolution = lambda: {"resolved": 0, "stalled": 0}
        orchestrator.tick_self_repair_queue = lambda: {"scheduled": 0}
        orchestrator.project_environment_ok = lambda project_name, refresh=False: True
        orchestrator.project_hard_stopped = lambda project_name: False
        orchestrator.enqueue_task = lambda task: orchestrator.write_json_atomic(orchestrator.task_path(task["task_id"], "queued"), task)
        try:
            t1 = orchestrator.new_task(
                role="implementer",
                engine="codex",
                project=scenario["project"],
                summary="qa contract fail",
                source="scenario-32",
                feature_id=scenario["feature_id"],
            )
            t1["task_id"] = scenario["failed_task_id"]
            t1["state"] = "failed"
            t1["blocker"] = orchestrator.make_blocker(
                "qa_contract_error",
                summary="QA config invalid",
                detail="bad config",
                source="scenario",
                retryable=False,
            )
            orchestrator.write_json_atomic(orchestrator.task_path(t1["task_id"], "failed"), t1)
            t2 = orchestrator.new_task(
                role="implementer",
                engine="codex",
                project=scenario["project"],
                summary="normal queued task",
                source="scenario-32",
            )
            t2["task_id"] = scenario["queued_task_id"]
            t2["state"] = "queued"
            orchestrator.write_json_atomic(orchestrator.task_path(t2["task_id"], "queued"), t2)
            feature = {
                "feature_id": scenario["feature_id"],
                "project": scenario["project"],
                "status": "open",
                "summary": "qa contract feature",
                "child_task_ids": [t1["task_id"]],
            }
            orchestrator.write_json_atomic(feats / f"{scenario['feature_id']}.json", feature)
            orchestrator._enqueue_qa_contract_repair(t1)
            env_ok = orchestrator.project_environment_ok(scenario["project"])
            repair_tasks = []
            queued_ids = []
            for state in ("queued", "claimed"):
                for path in (queue_root / state).glob("*.json"):
                    row = orchestrator.read_json(path, {})
                    if state == "queued":
                        queued_ids.append(row.get("task_id"))
                    if str(row.get("source") or "").startswith("fix-qa-contract:"):
                        repair_tasks.append(row.get("task_id"))
            failed_state = orchestrator.find_task(scenario["failed_task_id"])[0]
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
        return {
            "failed_state": failed_state,
            "repair_task_count": len(repair_tasks),
            "claimed_ids": [scenario["queued_task_id"]] if scenario["queued_task_id"] in queued_ids else [],
            "project_environment_ok": env_ok,
        }


def _run_qa_contract_full_tick(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        feats = root / "features"
        feats.mkdir()
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old = {
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "REPORT_DIR": orchestrator.REPORT_DIR,
            "EVENTS_LOG": orchestrator.EVENTS_LOG,
            "load_config": orchestrator.load_config,
            "emit_runtime_metrics_snapshot": orchestrator.emit_runtime_metrics_snapshot,
            "write_agent_status": orchestrator.write_agent_status,
            "reap": orchestrator.reap,
            "_write_workflow_check_report": orchestrator._write_workflow_check_report,
            "tick_self_repair_resolution": orchestrator.tick_self_repair_resolution,
            "tick_self_repair_queue": orchestrator.tick_self_repair_queue,
            "project_environment_ok": orchestrator.project_environment_ok,
            "project_hard_stopped": orchestrator.project_hard_stopped,
            "environment_health": orchestrator.environment_health,
            "subprocess": orchestrator.subprocess,
        }
        kickstarts = []
        orchestrator.FEATURES_DIR = feats
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.REPORT_DIR = root / "reports"
        orchestrator.EVENTS_LOG = root / "events.jsonl"
        orchestrator.load_config = lambda: {
            "workflow_check_max_attempts": 3,
            "projects": [
                {"name": scenario["project"], "path": str(root / "repo")},
                {"name": "devmini-orchestrator", "path": str(root / "orchestrator")},
            ],
            "synthetic_canary": {"enabled": False},
        }
        orchestrator.emit_runtime_metrics_snapshot = lambda **kwargs: None
        orchestrator.write_agent_status = lambda *args, **kwargs: None
        orchestrator.reap = lambda: 0
        orchestrator._write_workflow_check_report = lambda issues, reaped=0: None
        orchestrator.tick_self_repair_resolution = lambda: {"resolved": 0, "stalled": 0}
        orchestrator.tick_self_repair_queue = lambda: {"scheduled": 0}
        orchestrator.project_environment_ok = lambda project_name, refresh=False: True
        orchestrator.project_hard_stopped = lambda project_name: False
        orchestrator.environment_health = lambda refresh=False: {"ok": True, "issues": []}
        class _Proc:
            def __init__(self, returncode=0):
                self.returncode = returncode
                self.stdout = ""
                self.stderr = ""
        orchestrator.subprocess.run = lambda cmd, **kwargs: (kickstarts.append(cmd) if cmd and cmd[0] == "launchctl" else None) or _Proc(0)
        try:
            t1 = orchestrator.new_task(
                role="implementer",
                engine="codex",
                project=scenario["project"],
                summary="qa contract fail",
                source="scenario-33",
                feature_id=scenario["feature_id"],
            )
            t1["task_id"] = scenario["failed_task_id"]
            t1["state"] = "failed"
            t1["blocker"] = orchestrator.make_blocker(
                "qa_contract_error",
                summary="QA config invalid",
                detail="bad config",
                source="scenario",
                retryable=False,
            )
            orchestrator.write_json_atomic(orchestrator.task_path(t1["task_id"], "failed"), t1)
            feature = {
                "feature_id": scenario["feature_id"],
                "project": scenario["project"],
                "status": "open",
                "summary": "qa contract feature",
                "child_task_ids": [t1["task_id"]],
            }
            orchestrator.write_json_atomic(feats / f"{scenario['feature_id']}.json", feature)
            out = orchestrator.tick_workflow_check()
            repair_tasks = []
            for state in ("queued", "claimed"):
                for path in (queue_root / state).glob("*.json"):
                    row = orchestrator.read_json(path, {})
                    if str(row.get("source") or "").startswith("fix-qa-contract:"):
                        repair_tasks.append(row.get("task_id"))
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
        return {
            "issues": out.get("issues"),
            "repair_task_count": len(repair_tasks),
            "kickstart_calls": len(kickstarts),
        }


def _run_self_repair_issue_backfill(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        feats = root / "features"
        feats.mkdir()
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old = {
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "load_config": orchestrator.load_config,
            "append_event": orchestrator.append_event,
            "append_transition": orchestrator.append_transition,
        }
        orchestrator.FEATURES_DIR = feats
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.load_config = lambda: {
            "projects": [{"name": "devmini-orchestrator", "path": str(root / "orchestrator")}],
            "synthetic_canary": {"enabled": False},
            "self_repair_issue_max_attempts": 3,
        }
        orchestrator.append_event = lambda *args, **kwargs: None
        orchestrator.append_transition = lambda *args, **kwargs: None
        try:
            feature = {
                "feature_id": scenario["feature_id"],
                "project": "devmini-orchestrator",
                "status": "open",
                "self_repair": {
                    "enabled": True,
                    "issues": [
                        {"issue_key": "legacy-false", "status": "pending"},
                        {"issue_key": "new-env", "status": "pending", "attempts": 0, "max_attempts": 3},
                    ],
                },
            }
            orchestrator.write_json_atomic(feats / f"{scenario['feature_id']}.json", feature)
            queue_out = orchestrator.tick_self_repair_queue()
            saved = orchestrator.read_json(feats / f"{scenario['feature_id']}.json", {})
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
        issues = {row["issue_key"]: row for row in saved["self_repair"]["issues"]}
        return {
            "scheduled": queue_out.get("scheduled"),
            "legacy_attempts": issues["legacy-false"].get("attempts"),
            "legacy_max_attempts": issues["legacy-false"].get("max_attempts"),
            "legacy_execution_task_ids": issues["legacy-false"].get("execution_task_ids"),
            "legacy_superseded_task_ids": issues["legacy-false"].get("superseded_task_ids"),
            "new_attempts": issues["new-env"].get("attempts"),
            "new_max_attempts": issues["new-env"].get("max_attempts"),
        }


def _run_self_repair_review_state_live(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        feats = root / "features"
        feats.mkdir()
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old = {
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "load_config": orchestrator.load_config,
            "acquire_lock": orchestrator.acquire_lock,
        }
        orchestrator.FEATURES_DIR = feats
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.load_config = lambda: {
            "self_repair": {"enabled": True, "project": "devmini-orchestrator"},
            "projects": [{"name": "devmini-orchestrator", "path": str(root / "orchestrator")}],
        }
        orchestrator.acquire_lock = lambda *args, **kwargs: type("L", (), {"close": lambda self: None})()
        try:
            feature = {
                "feature_id": "feature-sr",
                "project": "devmini-orchestrator",
                "status": "open",
                "self_repair": {
                    "enabled": True,
                    "issues": [
                        {"issue_key": "active", "status": "planned", "planner_task_id": "task-plan", "execution_task_ids": []},
                    ],
                },
            }
            orchestrator.write_json_atomic(feats / "feature-sr.json", feature)
            orchestrator.write_json_atomic(
                queue_root / "awaiting-review" / "task-codex.json",
                {
                    "task_id": "task-codex",
                    "project": "devmini-orchestrator",
                    "feature_id": "feature-sr",
                    "state": "awaiting-review",
                    "engine": "codex",
                    "engine_args": {"issue_key": "active", "evidence": "old"},
                },
            )
            saved = orchestrator.read_json(feats / "feature-sr.json", {})
            issue = saved["self_repair"]["issues"][0]
            live_before = orchestrator._self_repair_issue_live(issue, feature_id="feature-sr")
            active_before = orchestrator._self_repair_has_active_work(saved)
            out = orchestrator.enqueue_self_repair(
                summary="new summary",
                evidence="new evidence",
                issue_key="new",
                source="workflow-check",
            )
            saved_feature = orchestrator.read_json(feats / "feature-sr.json", {})
            saved_task = orchestrator.read_json(queue_root / "awaiting-review" / "task-codex.json", {})
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
        issues = {row["issue_key"]: row for row in saved_feature["self_repair"]["issues"]}
        return {
            "live_before": live_before,
            "active_before": active_before,
            "enqueue_reason": out.get("reason"),
            "appended_issue_status": issues["new"].get("status"),
            "active_updates": len(saved_task["engine_args"].get("self_repair_updates") or []),
        }


def _run_orchestrator_template_candidate_only(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        state_root = root / "state"
        braid_templates = root / "braid" / "templates"
        state_root.mkdir(parents=True, exist_ok=True)
        braid_templates.mkdir(parents=True, exist_ok=True)
        old = {
            "STATE_ROOT": orchestrator.STATE_ROOT,
            "RUNTIME_DIR": orchestrator.RUNTIME_DIR,
            "BRAID_DIR": orchestrator.BRAID_DIR,
            "BRAID_TEMPLATES": orchestrator.BRAID_TEMPLATES,
            "BRAID_INDEX": orchestrator.BRAID_INDEX,
            "load_config": orchestrator.load_config,
            "append_event": orchestrator.append_event,
        }
        events = []
        orchestrator.STATE_ROOT = state_root
        orchestrator.RUNTIME_DIR = state_root / "runtime"
        orchestrator.BRAID_DIR = root / "braid"
        orchestrator.BRAID_TEMPLATES = braid_templates
        orchestrator.BRAID_INDEX = state_root / "braid-index.json"
        orchestrator.load_config = lambda: {"projects": [{"name": "devmini-orchestrator", "path": str(root)}]}
        orchestrator.append_event = lambda *args, **kwargs: events.append((args, kwargs))
        try:
            body = "flowchart TD;\nStart[Start] -- \"always\" --> C1[Check: ok];\nC1 -- \"pass\" --> End[End];\n"
            orchestrator.braid_template_write("orchestrator-self-repair", body, "claude-opus-refine")
            candidate = orchestrator.RUNTIME_DIR / "braid-template-candidates" / "orchestrator-self-repair.mmd"
            canonical = braid_templates / "orchestrator-self-repair.mmd"
            idx = orchestrator.load_braid_index().get("orchestrator-self-repair", {})
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
        return {
            "candidate_exists": candidate.exists(),
            "canonical_exists": canonical.exists(),
            "pending_candidate_path": idx.get("pending_candidate_path"),
            "event_kind": events[0][0][1] if events else None,
        }


def _run_telegram_health_dedupe(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    telegram = _load_module("telegram_bot", repo_root / "bin" / "telegram_bot.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        old = {
            "TELEGRAM_PUSH_STATE_PATH": orchestrator.TELEGRAM_PUSH_STATE_PATH,
            "load_config": orchestrator.load_config,
            "_health_payload": orchestrator._health_payload,
            "telegram_health_card": orchestrator.telegram_health_card,
        }
        orchestrator.TELEGRAM_PUSH_STATE_PATH = root / "telegram-pushes.json"
        orchestrator.load_config = lambda: {}
        rows = [
            {
                "environment_ok": 0,
                "environment_error_count": 2,
                "workflow_check_issue_count": 4,
                "feature_open_count": 2,
                "feature_frontier_blocked_count": 1,
                "queue": {},
                "generated_at": "2026-04-23T16:00:00",
            },
            {
                "environment_ok": 0,
                "environment_error_count": 2,
                "workflow_check_issue_count": 4,
                "feature_open_count": 2,
                "feature_frontier_blocked_count": 1,
                "queue": {},
                "generated_at": "2026-04-23T16:00:30",
            },
        ]
        try:
            first_payload = rows[0]
            second_payload = rows[1]
            first_key = f"health:{telegram.health_dedupe_key(first_payload)}"
            second_key = f"health:{telegram.health_dedupe_key(second_payload)}"
            first = orchestrator.should_push_alert(first_key, 6 * 3600)
            second = orchestrator.should_push_alert(second_key, 6 * 3600)
            saved = orchestrator.load_telegram_push_state()
        finally:
            for key_name, value in old.items():
                setattr(orchestrator, key_name, value)
        return {
            "first_allowed": first,
            "second_allowed": second,
            "stored_events": len((saved.get("events") or {})),
            "same_key": first_key == second_key,
        }


def _run_telegram_health_backoff(repo_root, scenario):
    telegram = _load_module("telegram_bot", repo_root / "bin" / "telegram_bot.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        old = {
            "PUSHED_STATE_PATH": telegram.PUSHED_STATE_PATH,
        }
        telegram.PUSHED_STATE_PATH = root / "telegram-pushed.json"
        try:
            first = telegram.category_backoff_allows("health", 600)
            second = telegram.category_backoff_allows("health", 600)
            saved = telegram.load_pushed_state()
        finally:
            for key_name, value in old.items():
                setattr(telegram, key_name, value)
        return {
            "first_allowed": first,
            "second_allowed": second,
            "stored_categories": len((saved.get("categories") or {})),
        }


def _run_template_owner_project(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    cfg = {
        "projects": [
            {"name": "lvc-standard"},
            {"name": "dag-framework"},
            {"name": "trade-research-platform"},
            {"name": "devmini-orchestrator"},
        ]
    }
    return {
        "orchestrator_self_repair_owner": orchestrator._template_owner_project(cfg, "orchestrator-self-repair"),
        "lvc_owner": orchestrator._template_owner_project(cfg, "lvc-reviewer-pass"),
        "dag_owner": orchestrator._template_owner_project(cfg, "dag-implement-node"),
    }


def _run_fetch_failure_cached_remote_ok(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        project = {"name": "dag-framework", "path": str(pathlib.Path(tmp) / "dag-framework")}
        pathlib.Path(project["path"]).mkdir(parents=True, exist_ok=True)
        old = orchestrator._git_ok
        calls = []
        def fake_git_ok(repo_path, *args):
            calls.append(args)
            if args == ("rev-parse", "--is-inside-work-tree"):
                return True, "true"
            if args == ("fetch", "origin", "main"):
                return False, "ERROR: transient auth"
            if args == ("rev-parse", "--verify", "refs/remotes/origin/main"):
                return True, "refs/remotes/origin/main"
            if args == ("status", "--porcelain"):
                return True, ""
            if args == ("merge-base", "--is-ancestor", "main", "origin/main"):
                return True, ""
            return True, ""
        orchestrator._git_ok = fake_git_ok
        try:
            issue = orchestrator._project_main_preflight_issue(project)
        finally:
            orchestrator._git_ok = old
    return {
        "issue_is_none": issue is None,
        "checked_cached_remote": ("rev-parse", "--verify", "refs/remotes/origin/main") in calls,
    }


def _run_state_engine_mirror(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    import os

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        queue_root = root / "queue"
        runtime_dir = root / "state" / "runtime"
        features_dir = root / "state" / "features"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        features_dir.mkdir(parents=True, exist_ok=True)
        db_path = runtime_dir / "orchestrator.db"
        old = {
            "STATE_ROOT": orchestrator.STATE_ROOT,
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "RUNTIME_DIR": orchestrator.RUNTIME_DIR,
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
            "TRANSITIONS_LOG": orchestrator.TRANSITIONS_LOG,
            "EVENTS_LOG": orchestrator.EVENTS_LOG,
            "METRICS_LOG": orchestrator.METRICS_LOG,
            "STATE_ENGINE_DB_PATH": orchestrator.STATE_ENGINE_DB_PATH,
            "_STATE_ENGINE_CACHE": orchestrator._STATE_ENGINE_CACHE,
            "_STATE_ENGINE_RECONCILE": orchestrator._STATE_ENGINE_RECONCILE,
        }
        old_env = {
            "STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"),
            "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH"),
        }
        orchestrator.STATE_ROOT = root
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.RUNTIME_DIR = runtime_dir
        orchestrator.FEATURES_DIR = features_dir
        orchestrator.TRANSITIONS_LOG = runtime_dir / "transitions.log"
        orchestrator.EVENTS_LOG = runtime_dir / "events.jsonl"
        orchestrator.METRICS_LOG = runtime_dir / "metrics.jsonl"
        orchestrator.STATE_ENGINE_DB_PATH = db_path
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        orchestrator._STATE_ENGINE_RECONCILE = {"ts": 0.0, "active": False, "last": None}
        os.environ["STATE_ENGINE_MODE"] = "mirror"
        os.environ["STATE_ENGINE_PATH"] = str(db_path)
        try:
            for idx in range(int(scenario["write_count"])):
                task = {
                    "task_id": f"task-{idx:03d}",
                    "engine": "codex",
                    "role": "implementer",
                    "project": "demo",
                    "summary": f"task {idx}",
                    "source": "scenario-35",
                    "state": "queued",
                    "blocker": None,
                    "attempt": 1,
                    "created_at": orchestrator.now_iso(),
                    "claimed_at": None,
                    "started_at": None,
                    "finished_at": None,
                }
                orchestrator.write_json_atomic(orchestrator.task_path(task["task_id"], "queued"), task)
            feature = {
                "feature_id": scenario["feature_id"],
                "project": "demo",
                "status": "open",
                "branch": f"feature/{scenario['feature_id']}",
                "summary": "state engine mirror scenario",
                "source": "scenario-35",
                "created_at": orchestrator.now_iso(),
                "child_task_ids": [f"task-{idx:03d}" for idx in range(int(scenario["write_count"]))],
                "self_repair": {"enabled": False, "issues": []},
            }
            orchestrator.write_json_atomic(orchestrator.feature_path(scenario["feature_id"]), feature)
            out = orchestrator.state_engine_reconcile()
        finally:
            orchestrator.STATE_ROOT = old["STATE_ROOT"]
            orchestrator.QUEUE_ROOT = old["QUEUE_ROOT"]
            orchestrator.RUNTIME_DIR = old["RUNTIME_DIR"]
            orchestrator.FEATURES_DIR = old["FEATURES_DIR"]
            orchestrator.TRANSITIONS_LOG = old["TRANSITIONS_LOG"]
            orchestrator.EVENTS_LOG = old["EVENTS_LOG"]
            orchestrator.METRICS_LOG = old["METRICS_LOG"]
            orchestrator.STATE_ENGINE_DB_PATH = old["STATE_ENGINE_DB_PATH"]
            orchestrator._STATE_ENGINE_CACHE = old["_STATE_ENGINE_CACHE"]
            orchestrator._STATE_ENGINE_RECONCILE = old["_STATE_ENGINE_RECONCILE"]
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]
            if old_env["STATE_ENGINE_PATH"] is None:
                os.environ.pop("STATE_ENGINE_PATH", None)
            else:
                os.environ["STATE_ENGINE_PATH"] = old_env["STATE_ENGINE_PATH"]
        return {
            "queue_queued_db": out["queue"]["queued"]["db"],
            "queue_queued_fs": out["queue"]["queued"]["fs"],
            "queue_queued_diff": out["queue"]["queued"]["diff"],
            "features_db": out["features"]["db"],
            "features_fs": out["features"]["fs"],
            "features_diff": out["features"]["diff"],
            "integrity_check": out["integrity_check"],
        }


def _run_fs_to_engine_migration(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    migrate = _load_module("migrate_fs_to_engine", repo_root / "bin" / "migrate_fs_to_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        for state in orchestrator.STATES:
            (root / "queue" / state).mkdir(parents=True, exist_ok=True)
        (root / "state" / "features").mkdir(parents=True, exist_ok=True)
        (root / "state" / "runtime").mkdir(parents=True, exist_ok=True)
        (root / "state" / "migrations").mkdir(parents=True, exist_ok=True)
        (root / "bin").mkdir(parents=True, exist_ok=True)
        (root / "config").mkdir(parents=True, exist_ok=True)
        shutil_copy = __import__("shutil").copy2
        shutil_copy(repo_root / "bin" / "orchestrator.py", root / "bin" / "orchestrator.py")
        shutil_copy(repo_root / "bin" / "state_engine.py", root / "bin" / "state_engine.py")
        shutil_copy(repo_root / "bin" / "migrate_fs_to_engine.py", root / "bin" / "migrate_fs_to_engine.py")
        shutil_copy(repo_root / "state" / "migrations" / "0001_initial.sql", root / "state" / "migrations" / "0001_initial.sql")
        if (repo_root / "state" / "migrations" / "0002_aux_runtime_logs.sql").exists():
            shutil_copy(repo_root / "state" / "migrations" / "0002_aux_runtime_logs.sql", root / "state" / "migrations" / "0002_aux_runtime_logs.sql")
        if (repo_root / "state" / "migrations" / "0003_memory_surface.sql").exists():
            shutil_copy(repo_root / "state" / "migrations" / "0003_memory_surface.sql", root / "state" / "migrations" / "0003_memory_surface.sql")
        shutil_copy(repo_root / "config" / "orchestrator.example.json", root / "config" / "orchestrator.example.json")
        (root / "config" / "orchestrator.local.json").write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "name": "demo",
                            "path": str(root),
                            "qa": {"smoke": "qa/smoke.sh", "regression": "qa/regression.sh"},
                        }
                    ]
                },
                indent=2,
                sort_keys=True,
            )
        )
        (root / "repo-memory").mkdir(parents=True, exist_ok=True)
        (root / "repo-memory" / "DECISIONS.md").write_text(
            "# demo\n\n## 2026-04-19 — Migration decision\n**Decision:** move memory into sqlite.\n",
            encoding="utf-8",
        )
        task = {
            "task_id": "task-36",
            "engine": "codex",
            "role": "implementer",
            "project": "demo",
            "summary": "migrate task",
            "source": "scenario-36",
            "state": "queued",
            "blocker": None,
            "attempt": 1,
            "created_at": "2026-04-19T19:10:00",
            "claimed_at": None,
            "started_at": None,
            "finished_at": None,
        }
        (root / "queue" / "queued" / "task-36.json").write_text(json.dumps(task, indent=2, sort_keys=True))
        feature = {
            "feature_id": "feature-36",
            "project": "demo",
            "status": "open",
            "summary": "migrate feature",
            "source": "scenario-36",
            "created_at": "2026-04-19T19:10:00",
            "child_task_ids": ["task-36"],
            "self_repair": {"enabled": True, "issues": [{"issue_key": "issue-36", "status": "pending"}]},
        }
        (root / "state" / "features" / "feature-36.json").write_text(json.dumps(feature, indent=2, sort_keys=True))
        (root / "state" / "runtime" / "transitions.log").write_text(
            "2026-04-19T19:10:00\ttask-36\tnew\t->\tqueued\tscenario-36\n"
        )
        (root / "state" / "runtime" / "events.jsonl").write_text(
            json.dumps({"ts": "2026-04-19T19:10:01", "role": "implementer", "event": "task_enqueued", "task_id": "task-36", "feature_id": "feature-36", "details": {}}, sort_keys=True) + "\n"
        )
        (root / "state" / "runtime" / "metrics.jsonl").write_text(
            json.dumps({"ts": "2026-04-19T19:10:02", "name": "task.enqueued", "type": "counter", "value": 1, "tags": {}, "source": "scenario-36"}, sort_keys=True) + "\n"
        )
        out = migrate.migrate_from_fs(root, root / "state" / "runtime" / "orchestrator.db")
        return {
            "tasks": out["tasks"],
            "features": out["features"],
            "transitions": out["transitions"],
            "events": out["events"],
            "metrics": out["metrics"],
            "memory_observations": out["memory_observations"],
            "integrity_check": out["integrity_check"],
        }


def _run_atomic_claim_concurrency(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        queue_root = root / "queue"
        claims_dir = root / "state" / "runtime" / "claims"
        runtime_dir = root / "state" / "runtime"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        claims_dir.mkdir(parents=True, exist_ok=True)
        old = {
            "STATE_ROOT": orchestrator.STATE_ROOT,
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "RUNTIME_DIR": orchestrator.RUNTIME_DIR,
            "CLAIMS_DIR": orchestrator.CLAIMS_DIR,
            "STATE_ENGINE_DB_PATH": orchestrator.STATE_ENGINE_DB_PATH,
            "now_iso": orchestrator.now_iso,
            "project_environment_ok": orchestrator.project_environment_ok,
            "project_hard_stopped": orchestrator.project_hard_stopped,
            "load_braid_index": orchestrator.load_braid_index,
            "crash_loop_guard_status": orchestrator.crash_loop_guard_status,
            "slot_paused": orchestrator.slot_paused,
        }
        old_env = {name: os.environ.get(name) for name in ("STATE_ENGINE_MODE", "STATE_ENGINE_PATH")}
        cache_old = dict(orchestrator._STATE_ENGINE_CACHE)
        orchestrator.STATE_ROOT = root
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.RUNTIME_DIR = runtime_dir
        orchestrator.CLAIMS_DIR = claims_dir
        orchestrator.STATE_ENGINE_DB_PATH = runtime_dir / "orchestrator.db"
        orchestrator.now_iso = lambda: "2026-04-19T20:10:00"
        orchestrator.project_environment_ok = lambda *args, **kwargs: True
        orchestrator.project_hard_stopped = lambda *args, **kwargs: False
        orchestrator.load_braid_index = lambda: {}
        orchestrator.crash_loop_guard_status = lambda *args, **kwargs: {"suppressed": False, "crashes": [], "window_seconds": 180, "max_crashes": 2}
        orchestrator.slot_paused = lambda *args, **kwargs: None
        os.environ["STATE_ENGINE_MODE"] = "primary"
        os.environ["STATE_ENGINE_PATH"] = str(orchestrator.STATE_ENGINE_DB_PATH)
        orchestrator._STATE_ENGINE_CACHE["key"] = None
        orchestrator._STATE_ENGINE_CACHE["engine"] = None
        try:
            orchestrator.get_state_engine().initialize()
            task_count = int(scenario["task_count"])
            worker_count = int(scenario["worker_count"])
            for idx in range(task_count):
                task = {
                    "task_id": f"task-37-{idx:02d}",
                    "engine": scenario["engine"],
                    "role": "implementer",
                    "project": "demo",
                    "summary": f"claim me {idx}",
                    "source": "scenario-37",
                    "state": "queued",
                    "blocker": None,
                    "attempt": 1,
                    "created_at": f"2026-04-19T20:10:{idx:02d}",
                    "claimed_at": None,
                    "started_at": None,
                    "finished_at": None,
                }
                orchestrator.write_json_atomic(orchestrator.task_path(task["task_id"], "queued"), task)

            barrier = threading.Barrier(worker_count)
            lock = threading.Lock()
            claimed_ids = []
            claim_states = []

            def worker_claim(worker_idx):
                barrier.wait()
                task = orchestrator.atomic_claim(scenario["engine"])
                if task:
                    orchestrator.write_claim_pid(task["task_id"], scenario["engine"], worktree=f"worker-{worker_idx}")
                    with lock:
                        claimed_ids.append(task["task_id"])
                        claim_states.append(task["state"])

            threads = [
                threading.Thread(target=worker_claim, args=(idx,), daemon=True)
                for idx in range(worker_count)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            queued_ids = sorted(p.stem for p in (queue_root / "queued").glob("*.json"))
            claimed_ids_fs = sorted(p.stem for p in (queue_root / "claimed").glob("*.json"))
            pid_task_ids = sorted(p.stem for p in claims_dir.glob("*.pid"))
            states_seen = {}
            for state in orchestrator.STATES:
                for path in (queue_root / state).glob("*.json"):
                    states_seen.setdefault(path.stem, []).append(state)
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
            orchestrator._STATE_ENGINE_CACHE.clear()
            orchestrator._STATE_ENGINE_CACHE.update(cache_old)
            for name, value in old_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        duplicate_state_tasks = sorted(task_id for task_id, states in states_seen.items() if len(states) > 1)
        return {
            "claimed_count": len(claimed_ids),
            "claimed_ids_distinct": len(set(claimed_ids)),
            "queued_remaining": len(queued_ids),
            "claimed_fs_count": len(claimed_ids_fs),
            "pid_file_count": len(pid_task_ids),
            "pid_matches_claimed": pid_task_ids == claimed_ids_fs,
            "duplicate_state_tasks": duplicate_state_tasks,
            "claim_states": sorted(set(claim_states)),
        }


def _run_atomic_claim_concurrency_10(repo_root, scenario):
    return _run_atomic_claim_concurrency(repo_root, {"task_count": 10, "worker_count": 10, "engine": scenario["engine"]})


def _run_kill9_integrity(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        db_path = root / "orchestrator.db"
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(
                root=root,
                db_path=db_path,
                migrations_dir=repo_root / "state" / "migrations",
                mode="mirror",
            )
        )
        engine.initialize()
        engine.upsert_task_from_fs(
            {
                "task_id": "task-38",
                "engine": "codex",
                "role": "implementer",
                "project": "demo",
                "summary": "kill me mid-transaction",
                "source": "scenario-38",
                "state": "queued",
                "blocker": None,
                "attempt": 1,
                "created_at": "2026-04-19T22:10:00",
                "claimed_at": None,
                "started_at": None,
                "finished_at": None,
            },
            state="queued",
        )
        script = (
            "import sqlite3, sys, time;"
            "db = sys.argv[1];"
            "conn = sqlite3.connect(db, timeout=30);"
            "conn.execute('BEGIN IMMEDIATE');"
            "conn.execute(\"UPDATE tasks SET state='claimed', state_updated_at='2026-04-19T22:10:01' WHERE task_id='task-38'\");"
            "time.sleep(30)"
        )
        proc = subprocess.Popen([sys.executable, "-c", script, str(db_path)])
        try:
            time_limit = __import__('time').time() + 5
            while __import__('time').time() < time_limit:
                state = engine.find_task("task-38")
                if state and state[0] == "queued":
                    __import__('time').sleep(0.05)
                else:
                    break
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        finally:
            engine.close()
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(
                root=root,
                db_path=db_path,
                migrations_dir=repo_root / "state" / "migrations",
                mode="mirror",
            )
        )
        engine.initialize()
        found = engine.find_task("task-38")
        return {
            "integrity_check": engine.integrity_check(),
            "task_state": found[0] if found else None,
        }


def _run_orphan_recovery_log(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        queue_root = root / "queue"
        claims_dir = root / "state" / "runtime" / "claims"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        claims_dir.mkdir(parents=True, exist_ok=True)
        old = {
            "STATE_ROOT": orchestrator.STATE_ROOT,
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "RUNTIME_DIR": orchestrator.RUNTIME_DIR,
            "CLAIMS_DIR": orchestrator.CLAIMS_DIR,
            "TRANSITIONS_LOG": orchestrator.TRANSITIONS_LOG,
            "EVENTS_LOG": orchestrator.EVENTS_LOG,
            "METRICS_LOG": orchestrator.METRICS_LOG,
            "STATE_ENGINE_DB_PATH": orchestrator.STATE_ENGINE_DB_PATH,
            "STATE_MIGRATIONS_DIR": orchestrator.STATE_MIGRATIONS_DIR,
            "_STATE_ENGINE_CACHE": orchestrator._STATE_ENGINE_CACHE,
            "_STATE_ENGINE_RECONCILE": orchestrator._STATE_ENGINE_RECONCILE,
            "now_iso": orchestrator.now_iso,
            "pid_alive": orchestrator.pid_alive,
            "_seconds_since_iso": orchestrator._seconds_since_iso,
        }
        old_env = {
            "STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"),
            "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH"),
        }
        orchestrator.STATE_ROOT = root
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.RUNTIME_DIR = root / "state" / "runtime"
        orchestrator.CLAIMS_DIR = claims_dir
        orchestrator.TRANSITIONS_LOG = orchestrator.RUNTIME_DIR / "transitions.log"
        orchestrator.EVENTS_LOG = orchestrator.RUNTIME_DIR / "events.jsonl"
        orchestrator.METRICS_LOG = orchestrator.RUNTIME_DIR / "metrics.jsonl"
        orchestrator.STATE_ENGINE_DB_PATH = orchestrator.RUNTIME_DIR / "orchestrator.db"
        orchestrator.STATE_MIGRATIONS_DIR = repo_root / "state" / "migrations"
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        orchestrator._STATE_ENGINE_RECONCILE = {"ts": 0.0, "active": False, "last": None}
        orchestrator.now_iso = lambda: "2026-04-19T22:20:00"
        orchestrator.pid_alive = lambda pid: False
        orchestrator._seconds_since_iso = lambda ts: 120
        os.environ["STATE_ENGINE_MODE"] = "mirror"
        os.environ["STATE_ENGINE_PATH"] = str(orchestrator.STATE_ENGINE_DB_PATH)
        try:
            task = {
                "task_id": "task-39",
                "engine": "codex",
                "role": "implementer",
                "project": "demo",
                "summary": "orphan me",
                "source": "scenario-39",
                "state": "claimed",
                "blocker": None,
                "attempt": 1,
                "created_at": "2026-04-19T22:15:00",
                "claimed_at": "2026-04-19T22:18:00",
                "started_at": None,
                "finished_at": None,
            }
            orchestrator.write_json_atomic(orchestrator.task_path("task-39", "claimed"), task)
            orchestrator.write_claim_pid("task-39", "codex", worktree="wt-39")
            # overwrite pid with impossible pid while keeping file shape
            (claims_dir / "task-39.pid").write_text("999999\ncodex\nwt-39\n")
            reaped = orchestrator.reap()
            engine = orchestrator.get_state_engine()
            rows = engine.read_orphan_recoveries()
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]
            if old_env["STATE_ENGINE_PATH"] is None:
                os.environ.pop("STATE_ENGINE_PATH", None)
            else:
                os.environ["STATE_ENGINE_PATH"] = old_env["STATE_ENGINE_PATH"]
        row = rows[-1]
        return {
            "reaped": reaped,
            "log_rows": len(rows),
            "task_id": row["task_id"],
            "from_state": row["from_state"],
            "age_seconds": row["age_seconds"],
        }


def _run_environment_check_log(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        old = {
            "STATE_ROOT": orchestrator.STATE_ROOT,
            "RUNTIME_DIR": orchestrator.RUNTIME_DIR,
            "STATE_ENGINE_DB_PATH": orchestrator.STATE_ENGINE_DB_PATH,
            "STATE_MIGRATIONS_DIR": orchestrator.STATE_MIGRATIONS_DIR,
            "_STATE_ENGINE_CACHE": orchestrator._STATE_ENGINE_CACHE,
            "_STATE_ENGINE_RECONCILE": orchestrator._STATE_ENGINE_RECONCILE,
            "environment_health": orchestrator.environment_health,
            "now_iso": orchestrator.now_iso,
        }
        old_env = {
            "STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"),
            "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH"),
        }
        orchestrator.STATE_ROOT = root
        orchestrator.RUNTIME_DIR = root / "state" / "runtime"
        orchestrator.STATE_ENGINE_DB_PATH = orchestrator.RUNTIME_DIR / "orchestrator.db"
        orchestrator.STATE_MIGRATIONS_DIR = repo_root / "state" / "migrations"
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        orchestrator._STATE_ENGINE_RECONCILE = {"ts": 0.0, "active": False, "last": None}
        orchestrator.now_iso = lambda: "2026-04-19T22:30:00"
        os.environ["STATE_ENGINE_MODE"] = "mirror"
        os.environ["STATE_ENGINE_PATH"] = str(orchestrator.STATE_ENGINE_DB_PATH)
        calls = [
            {"ok": False, "issues": [{"severity": "error", "project": "demo", "code": "project_main_dirty", "summary": "dirty main", "detail": "git status"}]},
            {"ok": True, "issues": []},
        ]
        def fake_environment_health(refresh=False):
            return calls.pop(0)
        orchestrator.environment_health = fake_environment_health
        try:
            blocked = orchestrator.project_environment_blockers("demo", refresh=True)
            clear = orchestrator.project_environment_blockers("demo", refresh=True)
            rows = orchestrator.get_state_engine().read_environment_checks()
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]
            if old_env["STATE_ENGINE_PATH"] is None:
                os.environ.pop("STATE_ENGINE_PATH", None)
            else:
                os.environ["STATE_ENGINE_PATH"] = old_env["STATE_ENGINE_PATH"]
        return {
            "blocked_count": len(blocked),
            "clear_count": len(clear),
            "log_rows": len(rows),
            "first_result": rows[0]["result"],
            "first_summary": rows[0]["blocker_summary"],
            "second_result": rows[1]["result"],
        }


def _run_backup_roundtrip(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        old = {
            "STATE_ROOT": orchestrator.STATE_ROOT,
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
            "RUNTIME_DIR": orchestrator.RUNTIME_DIR,
            "STATE_ENGINE_DB_PATH": orchestrator.STATE_ENGINE_DB_PATH,
            "STATE_MIGRATIONS_DIR": orchestrator.STATE_MIGRATIONS_DIR,
            "_STATE_ENGINE_CACHE": orchestrator._STATE_ENGINE_CACHE,
            "_STATE_ENGINE_RECONCILE": orchestrator._STATE_ENGINE_RECONCILE,
        }
        old_env = {
            "STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"),
            "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH"),
        }
        orchestrator.STATE_ROOT = root
        orchestrator.QUEUE_ROOT = root / "queue"
        orchestrator.FEATURES_DIR = root / "state" / "features"
        orchestrator.RUNTIME_DIR = root / "state" / "runtime"
        orchestrator.STATE_ENGINE_DB_PATH = orchestrator.RUNTIME_DIR / "orchestrator.db"
        orchestrator.STATE_MIGRATIONS_DIR = repo_root / "state" / "migrations"
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        orchestrator._STATE_ENGINE_RECONCILE = {"ts": 0.0, "active": False, "last": None}
        for state in orchestrator.STATES:
            orchestrator.queue_dir(state)
        orchestrator.FEATURES_DIR.mkdir(parents=True, exist_ok=True)
        os.environ["STATE_ENGINE_MODE"] = "primary"
        os.environ["STATE_ENGINE_PATH"] = str(orchestrator.STATE_ENGINE_DB_PATH)
        try:
            feature = orchestrator.create_feature(project="demo", summary="backup", source="scenario-42")
            task = orchestrator.new_task(
                role="implementer",
                engine="codex",
                project="demo",
                summary="backup task",
                source="scenario-42",
                feature_id=feature["feature_id"],
            )
            orchestrator.enqueue_task(task)
            backup = orchestrator.state_engine_backup(backup_path=orchestrator.RUNTIME_DIR / "state.backup.db")
            orchestrator.STATE_ENGINE_DB_PATH.write_text("corrupt", encoding="utf-8")
            corrupted_marker = orchestrator.STATE_ENGINE_DB_PATH.read_text(encoding="utf-8")
            orchestrator.STATE_ENGINE_DB_PATH.write_bytes(pathlib.Path(backup["backup_path"]).read_bytes())
            orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
            engine = orchestrator.get_state_engine()
            status = engine.initialize()
            found = engine.find_task(task["task_id"])
            feature_row = engine.read_feature(feature["feature_id"])
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
        return {
            "backup_exists": pathlib.Path(backup["backup_path"]).exists(),
            "corrupted_marker": corrupted_marker,
            "integrity_check": status.get("integrity_check"),
            "task_state": found[0] if found else None,
            "feature_status": (feature_row or {}).get("status"),
        }


def _run_wal_backup_restore(repo_root, scenario):
    migrate = _load_module("migrate_fs_to_engine", repo_root / "bin" / "migrate_fs_to_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        db_path = root / "orchestrator.db"
        backup_path = root / "orchestrator.backup.db"

        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE sentinel(value TEXT)")
            conn.execute("INSERT INTO sentinel(value) VALUES ('keepme')")
            conn.commit()
        finally:
            conn.close()

        migrate._backup_live_db(db_path, backup_path)

        # Simulate a bad rollback target plus stale sidecars.
        db_path.write_text("not-a-db")
        (db_path.parent / f"{db_path.name}-wal").write_text("stale wal")
        (db_path.parent / f"{db_path.name}-shm").write_text("stale shm")
        migrate._restore_backup_db(db_path, backup_path)

        verify = sqlite3.connect(db_path, timeout=30)
        try:
            value = verify.execute("SELECT value FROM sentinel").fetchone()[0]
        finally:
            verify.close()
        return {
            "backup_size_gt_4k": backup_path.stat().st_size > 4096,
            "restored_value": value,
            "wal_exists_after_restore": (db_path.parent / f"{db_path.name}-wal").exists(),
            "shm_exists_after_restore": (db_path.parent / f"{db_path.name}-shm").exists(),
        }


def _run_corrupt_db_fallback(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        runtime = root / "state" / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        metrics_log = runtime / "metrics.jsonl"
        db_path = runtime / "broken.db"
        metrics_log.write_text(
            json.dumps(
                {
                    "ts": "2026-04-20T21:00:00",
                    "name": "task.enqueued",
                    "type": "counter",
                    "value": 1,
                    "tags": {},
                    "source": "scenario-50",
                },
                sort_keys=True,
            )
            + "\n"
        )
        db_path.write_text("not-a-db")
        old = {
            "METRICS_LOG": orchestrator.METRICS_LOG,
            "STATE_ROOT": orchestrator.STATE_ROOT,
            "RUNTIME_DIR": orchestrator.RUNTIME_DIR,
            "CONFIG_LOCAL_PATH": orchestrator.CONFIG_LOCAL_PATH,
        }
        env_old = {name: os.environ.get(name) for name in ("STATE_ENGINE_MODE", "STATE_ENGINE_PATH")}
        cache_old = dict(orchestrator._STATE_ENGINE_CACHE)
        try:
            orchestrator.METRICS_LOG = metrics_log
            orchestrator.STATE_ROOT = root / "state"
            orchestrator.RUNTIME_DIR = runtime
            os.environ["STATE_ENGINE_MODE"] = "primary"
            os.environ["STATE_ENGINE_PATH"] = str(db_path)
            orchestrator._STATE_ENGINE_CACHE["key"] = None
            orchestrator._STATE_ENGINE_CACHE["engine"] = None

            rows = orchestrator.read_metrics(limit=10)
            lines = metrics_log.read_text(errors="replace").splitlines()
            fallback_rows = [json.loads(line) for line in lines if '"name": "state_engine.fs_fallback"' in line]
            return {
                "read_count": len(rows),
                "first_metric_name": rows[0]["name"] if rows else None,
                "fallback_count": len(fallback_rows),
                "fallback_scope": (fallback_rows[-1].get("tags") or {}).get("scope") if fallback_rows else None,
            }
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
            orchestrator._STATE_ENGINE_CACHE.clear()
            orchestrator._STATE_ENGINE_CACHE.update(cache_old)
            for name, value in env_old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _run_disk_full_insert(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        db_path = root / "orchestrator.db"
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(
                root=root,
                db_path=db_path,
                migrations_dir=repo_root / "state" / "migrations",
                mode="mirror",
            )
        )
        engine.initialize()
        engine.close()

        real_connect = state_engine.sqlite3.connect

        class FaultyConnection(sqlite3.Connection):
            def execute(self, sql, params=(), /):
                if "INSERT INTO task_transitions" in sql:
                    raise sqlite3.OperationalError("database or disk is full")
                return super().execute(sql, params)

        def faulty_connect(*args, **kwargs):
            kwargs["factory"] = FaultyConnection
            return real_connect(*args, **kwargs)

        state_engine.sqlite3.connect = faulty_connect
        try:
            failing = state_engine.StateEngine(
                state_engine.StateEngineConfig(
                    root=root,
                    db_path=db_path,
                    migrations_dir=repo_root / "state" / "migrations",
                    mode="mirror",
                )
            )
            error = None
            try:
                failing.record_transition(
                    {
                        "ts": "2026-04-20T21:10:00",
                        "task_id": "task-51",
                        "from_state": "queued",
                        "to_state": "claimed",
                        "reason": "scenario-51",
                    }
                )
            except Exception as exc:
                error = str(exc)
        finally:
            state_engine.sqlite3.connect = real_connect

        verify = state_engine.StateEngine(
            state_engine.StateEngineConfig(
                root=root,
                db_path=db_path,
                migrations_dir=repo_root / "state" / "migrations",
                mode="mirror",
            )
        )
        verify.initialize()
        return {
            "integrity_check": verify.integrity_check(),
            "transition_count": verify.count_table("task_transitions"),
            "error_has_disk_full": "disk is full" in (error or ""),
        }


def _run_eio_read(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        runtime = root / "state" / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        metrics_log = runtime / "metrics.jsonl"
        db_path = runtime / "orchestrator.db"
        metrics_log.write_text(
            json.dumps(
                {
                    "ts": "2026-04-20T21:20:00",
                    "name": "task.enqueued",
                    "type": "counter",
                    "value": 1,
                    "tags": {},
                    "source": "scenario-52",
                },
                sort_keys=True,
            )
            + "\n"
        )
        old = {
            "METRICS_LOG": orchestrator.METRICS_LOG,
            "STATE_ROOT": orchestrator.STATE_ROOT,
            "RUNTIME_DIR": orchestrator.RUNTIME_DIR,
        }
        env_old = {name: os.environ.get(name) for name in ("STATE_ENGINE_MODE", "STATE_ENGINE_PATH")}
        cache_old = dict(orchestrator._STATE_ENGINE_CACHE)
        try:
            orchestrator.METRICS_LOG = metrics_log
            orchestrator.STATE_ROOT = root / "state"
            orchestrator.RUNTIME_DIR = runtime
            os.environ["STATE_ENGINE_MODE"] = "primary"
            os.environ["STATE_ENGINE_PATH"] = str(db_path)
            orchestrator._STATE_ENGINE_CACHE["key"] = None
            orchestrator._STATE_ENGINE_CACHE["engine"] = None
            engine = orchestrator.get_state_engine()
            engine.initialize()
            engine = orchestrator.get_state_engine()
            old_read_metrics = engine.read_metrics
            engine.read_metrics = lambda **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error"))
            rows = orchestrator.read_metrics(limit=10)
            engine.read_metrics = old_read_metrics
            lines = metrics_log.read_text(errors="replace").splitlines()
            fallback_rows = [json.loads(line) for line in lines if '"name": "state_engine.fs_fallback"' in line]
            return {
                "read_count": len(rows),
                "first_metric_name": rows[0]["name"] if rows else None,
                "fallback_count": len(fallback_rows),
                "fallback_scope": (fallback_rows[-1].get("tags") or {}).get("scope") if fallback_rows else None,
            }
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
            orchestrator._STATE_ENGINE_CACHE.clear()
            orchestrator._STATE_ENGINE_CACHE.update(cache_old)
            for name, value in env_old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _run_db_deleted(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        runtime = root / "state" / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        metrics_log = runtime / "metrics.jsonl"
        db_path = runtime / "orchestrator.db"
        metrics_log.write_text(
            json.dumps(
                {
                    "ts": "2026-04-20T21:30:00",
                    "name": "task.enqueued",
                    "type": "counter",
                    "value": 1,
                    "tags": {},
                    "source": "scenario-53",
                },
                sort_keys=True,
            )
            + "\n"
        )
        old = {
            "METRICS_LOG": orchestrator.METRICS_LOG,
            "STATE_ROOT": orchestrator.STATE_ROOT,
            "RUNTIME_DIR": orchestrator.RUNTIME_DIR,
        }
        env_old = {name: os.environ.get(name) for name in ("STATE_ENGINE_MODE", "STATE_ENGINE_PATH")}
        cache_old = dict(orchestrator._STATE_ENGINE_CACHE)
        try:
            orchestrator.METRICS_LOG = metrics_log
            orchestrator.STATE_ROOT = root / "state"
            orchestrator.RUNTIME_DIR = runtime
            os.environ["STATE_ENGINE_MODE"] = "primary"
            os.environ["STATE_ENGINE_PATH"] = str(db_path)
            orchestrator._STATE_ENGINE_CACHE["key"] = None
            orchestrator._STATE_ENGINE_CACHE["engine"] = None
            engine = orchestrator.get_state_engine()
            engine.initialize()
            engine.close()
            db_path.unlink()
            rows = orchestrator.read_metrics(limit=10)
            lines = metrics_log.read_text(errors="replace").splitlines()
            fallback_rows = [json.loads(line) for line in lines if '"name": "state_engine.fs_fallback"' in line]
            return {
                "db_exists_after_read": db_path.exists(),
                "read_count": len(rows),
                "fallback_count": len(fallback_rows),
                "fallback_scope": (fallback_rows[-1].get("tags") or {}).get("scope") if fallback_rows else None,
            }
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
            orchestrator._STATE_ENGINE_CACHE.clear()
            orchestrator._STATE_ENGINE_CACHE.update(cache_old)
            for name, value in env_old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _run_restore_active_rejected(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        runtime = root / "state" / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        db_path = runtime / "orchestrator.db"
        backup_path = runtime / "state.backup.db"
        old = {
            "STATE_ROOT": orchestrator.STATE_ROOT,
            "RUNTIME_DIR": orchestrator.RUNTIME_DIR,
            "STATE_ENGINE_DB_PATH": orchestrator.STATE_ENGINE_DB_PATH,
            "_active_orchestrator_launch_agent_labels": orchestrator._active_orchestrator_launch_agent_labels,
        }
        env_old = {name: os.environ.get(name) for name in ("STATE_ENGINE_MODE", "STATE_ENGINE_PATH")}
        cache_old = dict(orchestrator._STATE_ENGINE_CACHE)
        try:
            orchestrator.STATE_ROOT = root / "state"
            orchestrator.RUNTIME_DIR = runtime
            orchestrator.STATE_ENGINE_DB_PATH = db_path
            os.environ["STATE_ENGINE_MODE"] = "primary"
            os.environ["STATE_ENGINE_PATH"] = str(db_path)
            orchestrator._STATE_ENGINE_CACHE["key"] = None
            orchestrator._STATE_ENGINE_CACHE["engine"] = None
            orchestrator.get_state_engine().initialize()
            backup = orchestrator.state_engine_backup(backup_path=backup_path)
            orchestrator._active_orchestrator_launch_agent_labels = lambda: ["com.devmini.orchestrator.telegram-bot"]
            out = orchestrator.state_engine_restore(backup_path=backup["backup_path"])
            return {
                "restored": bool(out.get("restored")),
                "error": out.get("error"),
                "active_count": len(out.get("active_labels") or []),
            }
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
            orchestrator._STATE_ENGINE_CACHE.clear()
            orchestrator._STATE_ENGINE_CACHE.update(cache_old)
            for name, value in env_old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _run_council_timeout(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        task = {"task_id": "task-57", "engine_args": {}}
        old = {
            "_run_bounded": worker._run_bounded,
            "_claude_subprocess_env": worker._claude_subprocess_env,
            "_claude_budget_flag": worker._claude_budget_flag,
        }
        worker._run_bounded = lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd=["claude"], timeout=60))
        worker._claude_subprocess_env = lambda: {}
        worker._claude_budget_flag = lambda *args, **kwargs: "1.0"
        try:
            out = worker._run_self_repair_council(
                task=task,
                stage="pre_execute",
                panel=("socrates",),
                prompt_body="Decide.",
                worktree=root,
                timeout=60,
                last_msg_path=None,
            )
        finally:
            for key, value in old.items():
                setattr(worker, key, value)
        return {
            "error_has_timeout": "timeout" in (out.get("error") or ""),
            "blocker_code": out.get("blocker_code"),
        }


def _run_wal_growth_stalls(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(
                root=root,
                db_path=root / "runtime" / "orchestrator.db",
                migrations_dir=repo_root / "state" / "migrations",
                mode="primary",
            )
        )
        engine.initialize()
        writer = engine.connect()
        writer.execute("BEGIN IMMEDIATE")
        for idx in range(20):
            writer.execute(
                "INSERT INTO metrics(name, value, metric_type, created_at_epoch, tags_json) VALUES (?, ?, ?, ?, ?)",
                ("wal.test", float(idx), "gauge", idx + 1, "{}"),
            )
        chk = engine.checkpoint(conn=sqlite3.connect(engine.config.db_path))
        writer.rollback()
        post = engine.checkpoint()
        return {
            "checkpoint_busy": int(chk[0] or 0) > 0,
            "post_checkpoint_ok": int(post[0] or 0) == 0,
            "integrity_ok": engine.integrity_check() == "ok",
        }


def _run_migration_partial(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        migrations = root / "migrations"
        migrations.mkdir()
        (migrations / "0001_initial.sql").write_text("CREATE TABLE ok(id INTEGER PRIMARY KEY);\n", encoding="utf-8")
        (migrations / "0002_broken.sql").write_text("CREAT TABLE nope(\n", encoding="utf-8")
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=migrations, mode="primary")
        )
        try:
            engine.initialize()
        except Exception as exc:
            error = str(exc)
        else:
            error = ""
        conn = engine.connect()
        applied = sorted(engine._applied_migrations(conn))
        return {
            "error_present": bool(error),
            "applied": applied,
            "broken_applied": "0002_broken" in applied,
        }


def _run_migration_idempotence(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        first = engine.initialize()
        second = engine.initialize()
        return {
            "first_applied": first["applied_in_run"],
            "second_applied": second["applied_in_run"],
            "same_applied_count": first["applied_count"] == second["applied_count"],
        }


def _run_council_malformed(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        task = {"task_id": "task-62", "engine_args": {}}
        old = {
            "_run_bounded": worker._run_bounded,
            "_claude_subprocess_env": worker._claude_subprocess_env,
            "_claude_budget_flag": worker._claude_budget_flag,
        }
        worker._run_bounded = lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="not-json", stderr="")
        worker._claude_subprocess_env = lambda: {}
        worker._claude_budget_flag = lambda *args, **kwargs: "1.0"
        try:
            out = worker._run_self_repair_council(
                task=task, stage="pre_execute", panel=("socrates",), prompt_body="Decide.", worktree=root, timeout=60, last_msg_path=None
            )
        finally:
            for key, value in old.items():
                setattr(worker, key, value)
        return {
            "error_has_invalid": "invalid council output" in (out.get("error") or ""),
            "blocker_code": out.get("blocker_code"),
        }


def _run_fts5_recovery(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        engine.initialize()
        engine.upsert_memory_observation({
            "project": "devmini-orchestrator", "source_doc": "DECISIONS.md", "section_key": "s1", "type": "decision",
            "title": "Vector memory title", "content": "fts recovers cleanly", "importance": 5, "created_at": "2026-04-20T00:00:00",
        })
        conn = engine.connect()
        with conn:
            conn.execute("DROP TABLE memory_obs_fts")
            conn.execute("CREATE VIRTUAL TABLE memory_obs_fts USING fts5(title, content, tags)")
        rows = engine.memory_search("vector memory", project="devmini-orchestrator", limit=3)
        return {"titles": [r["title"] for r in rows], "integrity_ok": engine.integrity_check() == "ok"}


def _run_vector_rowid_divergence(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        status = engine.initialize()
        keep_id = engine.upsert_memory_observation({
            "project": "devmini-orchestrator", "source_doc": "DECISIONS.md", "section_key": "keep", "type": "decision",
            "title": "Healthy title", "content": "healthy content", "importance": 5, "created_at": "2026-04-20T00:00:00",
        })
        stale_id = engine.upsert_memory_observation({
            "project": "devmini-orchestrator", "source_doc": "DECISIONS.md", "section_key": "stale", "type": "decision",
            "title": "Stale title", "content": "stale content", "importance": 5, "created_at": "2026-04-20T00:00:01",
        })
        conn = engine.connect()
        with conn:
            conn.execute("DELETE FROM memory_observations WHERE id = ?", (stale_id,))
        rows = engine.memory_search("healthy", project="devmini-orchestrator", limit=3, semantic_candidates=[stale_id, keep_id])
        return {
            "vec_enabled": status.get("vec_enabled"),
            "titles": [r["title"] for r in rows],
            "no_crash": True,
        }


def _run_observation_orphan(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        feats = root / "features"; feats.mkdir()
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old = {k: orchestrator.tick_self_repair_observation_window.__globals__[k] for k in ("FEATURES_DIR", "QUEUE_ROOT", "append_event", "append_transition")}
        old_env = {"STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"), "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH")}
        os.environ["STATE_ENGINE_MODE"] = "off"
        os.environ["STATE_ENGINE_PATH"] = str(root / "runtime" / "scenario.db")
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        orchestrator.FEATURES_DIR = feats
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.append_event = lambda *args, **kwargs: None
        orchestrator.append_transition = lambda *args: None
        try:
            orchestrator.write_json_atomic(feats / "feature-65.json", {
                "feature_id": "feature-65", "project": "devmini-orchestrator", "status": "open",
                "self_repair": {"enabled": True, "issues": [{
                    "issue_key": "task:task-missing:blocked:template_refine_exhausted", "status": "resolved",
                    "observation_target": {"task_id": "task-missing", "blocker_code": "template_refine_exhausted"},
                    "observation_due_at": "2000-01-01T00:00:00", "observation_status": "pending",
                }]}
            })
            out = orchestrator.tick_self_repair_observation_window()
            saved = orchestrator.read_json(feats / "feature-65.json", {})
        finally:
            for key, value in old.items():
                orchestrator.tick_self_repair_observation_window.__globals__[key] = value
            orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]
            if old_env["STATE_ENGINE_PATH"] is None:
                os.environ.pop("STATE_ENGINE_PATH", None)
            else:
                os.environ["STATE_ENGINE_PATH"] = old_env["STATE_ENGINE_PATH"]
        issue = saved["self_repair"]["issues"][0]
        return {"reopened": out["reopened"], "observation_status": issue.get("observation_status"), "status": issue.get("status")}


def _run_observation_idempotent(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        feats = root / "features"; feats.mkdir()
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old = {k: orchestrator.tick_self_repair_observation_window.__globals__[k] for k in ("FEATURES_DIR", "QUEUE_ROOT", "append_event", "append_transition")}
        old_env = {"STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"), "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH")}
        events = []; transitions = []
        os.environ["STATE_ENGINE_MODE"] = "off"
        os.environ["STATE_ENGINE_PATH"] = str(root / "runtime" / "scenario.db")
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        orchestrator.FEATURES_DIR = feats
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.append_event = lambda *args, **kwargs: events.append((args, kwargs))
        orchestrator.append_transition = lambda *args: transitions.append(args)
        try:
            orchestrator.write_json_atomic(feats / "feature-66.json", {
                "feature_id": "feature-66", "status": "open", "self_repair": {"enabled": True, "issues": [{
                    "issue_key": "task:task-66:blocked:template_refine_exhausted", "status": "resolved",
                    "observation_target": {"task_id": "task-66", "blocker_code": "template_refine_exhausted"},
                    "observation_due_at": "2000-01-01T00:00:00",
                }]}
            })
            orchestrator.write_json_atomic(queue_root / "blocked" / "task-66.json", {"task_id": "task-66", "blocker": {"code": "template_refine_exhausted"}})
            first = orchestrator.tick_self_repair_observation_window()
            second = orchestrator.tick_self_repair_observation_window()
            saved = orchestrator.read_json(feats / "feature-66.json", {})
        finally:
            for key, value in old.items():
                orchestrator.tick_self_repair_observation_window.__globals__[key] = value
            orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]
            if old_env["STATE_ENGINE_PATH"] is None:
                os.environ.pop("STATE_ENGINE_PATH", None)
            else:
                os.environ["STATE_ENGINE_PATH"] = old_env["STATE_ENGINE_PATH"]
        issue = saved["self_repair"]["issues"][0]
        return {
            "first_reopened": first["reopened"],
            "second_reopened": second["reopened"],
            "transition_count": len(transitions),
            "checked_at_present": bool(issue.get("observation_checked_at")),
        }


def _run_clock_skew_backward(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        feats = root / "features"; feats.mkdir()
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old = {k: orchestrator.tick_self_repair_observation_window.__globals__[k] for k in ("FEATURES_DIR", "QUEUE_ROOT", "append_event", "append_transition", "OBSERVATION_SKEW_TOLERANCE_SECONDS")}
        old_env = {"STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"), "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH")}
        os.environ["STATE_ENGINE_MODE"] = "off"
        os.environ["STATE_ENGINE_PATH"] = str(root / "runtime" / "scenario.db")
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        orchestrator.FEATURES_DIR = feats
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.append_event = lambda *args, **kwargs: None
        orchestrator.append_transition = lambda *args: None
        orchestrator.OBSERVATION_SKEW_TOLERANCE_SECONDS = 120
        try:
            future_due = (datetime.now() + timedelta(seconds=30)).isoformat(timespec="seconds")
            orchestrator.write_json_atomic(feats / "feature-67.json", {
                "feature_id": "feature-67", "status": "open", "self_repair": {"enabled": True, "issues": [{
                    "issue_key": "task:task-67:blocked:template_refine_exhausted", "status": "resolved",
                    "observation_target": {"task_id": "task-67", "blocker_code": "template_refine_exhausted"},
                    "observation_due_at": future_due,
                }]}
            })
            orchestrator.write_json_atomic(queue_root / "blocked" / "task-67.json", {"task_id": "task-67", "blocker": {"code": "template_refine_exhausted"}})
            out = orchestrator.tick_self_repair_observation_window()
        finally:
            for key, value in old.items():
                orchestrator.tick_self_repair_observation_window.__globals__[key] = value
            orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]
            if old_env["STATE_ENGINE_PATH"] is None:
                os.environ.pop("STATE_ENGINE_PATH", None)
            else:
                os.environ["STATE_ENGINE_PATH"] = old_env["STATE_ENGINE_PATH"]
        return {"reopened": out["reopened"], "checked": out["checked"]}


def _run_checkpoint_starved(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        engine.initialize()
        stop = {"flag": False}
        def writer():
            conn = sqlite3.connect(engine.config.db_path, timeout=30)
            while not stop["flag"]:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO metrics(name, value, metric_type, created_at_epoch, tags_json) VALUES ('starve', 1, 'gauge', 1, '{}')")
                time.sleep(0.2)
                conn.commit()
            conn.close()
        t = threading.Thread(target=writer)
        t.start()
        time.sleep(0.05)
        chk = engine.checkpoint(conn=sqlite3.connect(engine.config.db_path))
        stop["flag"] = True
        t.join()
        return {"busy_seen": int(chk[0] or 0) > 0, "integrity_ok": engine.integrity_check() == "ok"}


def _run_metrics_rotation(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        log = root / "metrics.jsonl"
        old = {"METRICS_LOG": orchestrator.METRICS_LOG}
        old_env = {"STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"), "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH")}
        os.environ["STATE_ENGINE_MODE"] = "off"
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        orchestrator.METRICS_LOG = log
        try:
            log.write_text(
                json.dumps({"ts": "2000-01-01T00:00:00", "name": "old", "type": "gauge", "value": 1}) + "\n" +
                json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "name": "new", "type": "gauge", "value": 1}) + "\n",
                encoding="utf-8",
            )
            out = orchestrator.purge_old_metrics(now=datetime.now(), retention_days=1)
            rows = orchestrator.read_metrics()
        finally:
            orchestrator.METRICS_LOG = old["METRICS_LOG"]
            orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]
            if old_env["STATE_ENGINE_PATH"] is None:
                os.environ.pop("STATE_ENGINE_PATH", None)
            else:
                os.environ["STATE_ENGINE_PATH"] = old_env["STATE_ENGINE_PATH"]
        return {"removed_fs": out["removed_fs"], "remaining_names": [r["name"] for r in rows]}


def _run_retention_purge_safe(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        log = root / "metrics.jsonl"
        cfg = {"state_engine": {"mode": "primary", "path": str(root / "runtime" / "orchestrator.db"), "migrations_dir": str(repo_root / "state" / "migrations")}}
        old = {"METRICS_LOG": orchestrator.METRICS_LOG, "_STATE_ENGINE_CACHE": dict(orchestrator._STATE_ENGINE_CACHE)}
        orchestrator.METRICS_LOG = log
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        try:
            engine = orchestrator.get_state_engine(cfg=cfg)
            engine.initialize()
            engine.record_metric({"ts": "2000-01-01T00:00:00", "name": "old.db", "type": "gauge", "value": 1, "tags": {}, "source": "test"})
            engine.record_metric({"ts": datetime.now().isoformat(timespec="seconds"), "name": "new.db", "type": "gauge", "value": 1, "tags": {}, "source": "test"})
            out = orchestrator.purge_old_metrics(now=datetime.now(), retention_days=1, cfg=cfg)
            rows = engine.read_metrics()
        finally:
            orchestrator.METRICS_LOG = old["METRICS_LOG"]
            orchestrator._STATE_ENGINE_CACHE = dict(old["_STATE_ENGINE_CACHE"])
        return {"removed_db": out["removed_db"], "remaining_names": [r["name"] for r in rows]}


def _run_fk_violation(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        engine.initialize()
        conn = engine.connect()
        with conn:
            conn.execute("CREATE TABLE feature_parent(fid TEXT PRIMARY KEY)")
            conn.execute("CREATE TABLE feature_child(fid TEXT NOT NULL REFERENCES feature_parent(fid) ON DELETE CASCADE, tid TEXT)")
        try:
            with conn:
                conn.execute("INSERT INTO feature_child(fid, tid) VALUES ('missing', 'task-x')")
        except sqlite3.IntegrityError as exc:
            error = str(exc)
        else:
            error = ""
        return {"integrity_error": bool(error), "mentions_fk": "FOREIGN KEY" in error.upper()}


def _run_skill_prompt_injection(repo_root, scenario):
    orchestrator = _load_module("orchestrator", repo_root / "bin" / "orchestrator.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "skills"
        skill = root / "evil-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(scenario["body"], encoding="utf-8")
        old = {
            "AGENT_SCAN_DIR": orchestrator.AGENT_SCAN_DIR,
            "MCP_AUDIT_DIR": orchestrator.MCP_AUDIT_DIR,
            "append_event": orchestrator.append_event,
        }
        orchestrator.AGENT_SCAN_DIR = root.parent / "agent_scans"
        orchestrator.MCP_AUDIT_DIR = root.parent / "mcp_audits"
        orchestrator.append_event = lambda *args, **kwargs: None
        try:
            report = orchestrator.scan_agent_roots([root], kind="skills", opt_out=True)
        finally:
            orchestrator.AGENT_SCAN_DIR = old["AGENT_SCAN_DIR"]
            orchestrator.MCP_AUDIT_DIR = old["MCP_AUDIT_DIR"]
            orchestrator.append_event = old["append_event"]
        return {
            "accepted": report["accepted"],
            "high_count": report["counts"]["high"],
            "has_prompt_injection_reason": any("prompt-injection" in f["reason"] for f in report["findings"]),
        }


def _run_council_deleted_task_ref(repo_root, scenario):
    orchestrator, worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        task = {"task_id": "task-73", "engine_args": {}}
        old = {
            "_run_bounded": worker._run_bounded,
            "_claude_subprocess_env": worker._claude_subprocess_env,
            "_claude_budget_flag": worker._claude_budget_flag,
            "find_task": orchestrator.find_task,
        }
        worker._run_bounded = lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout=json.dumps({"result": json.dumps({"referenced_task_id": "task-deleted", "verdict": "approve"})}), stderr=""
        )
        worker._claude_subprocess_env = lambda: {}
        worker._claude_budget_flag = lambda *args, **kwargs: "1.0"
        orchestrator.find_task = lambda task_id, states=None: None
        try:
            out = worker._run_self_repair_council(
                task=task, stage="pre_execute", panel=("socrates",), prompt_body="Decide.", worktree=root, timeout=60, last_msg_path=None
            )
        finally:
            for key, value in old.items():
                if key == "find_task":
                    orchestrator.find_task = value
                else:
                    setattr(worker, key, value)
        return {
            "error_has_missing_task": "missing task" in (out.get("error") or ""),
            "blocker_code": out.get("blocker_code"),
        }


def _run_state_engine_reconnect_after_replace(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        db_path = root / "runtime" / "orchestrator.db"
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=db_path, migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        engine.initialize()
        conn1 = engine.connect()
        with conn1:
            conn1.execute("CREATE TABLE marker(name TEXT)")
            conn1.execute("INSERT INTO marker(name) VALUES ('old')")
        engine.checkpoint(conn=conn1)

        replacement = root / "runtime" / "replacement.db"
        conn2 = sqlite3.connect(replacement)
        conn2.execute("CREATE TABLE marker(name TEXT)")
        conn2.execute("INSERT INTO marker(name) VALUES ('new')")
        conn2.commit()
        conn2.close()
        for suffix in ("-wal", "-shm"):
            stale = pathlib.Path(f"{db_path}{suffix}")
            if stale.exists():
                stale.unlink()
        os.replace(replacement, db_path)

        conn3 = engine.connect()
        value = conn3.execute("SELECT name FROM marker LIMIT 1").fetchone()[0]
        return {
            "reconnected": conn1 is not conn3,
            "marker": value,
        }


def _run_clean_state_wipe_and_restart(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        state_root = root / "state"
        runtime = state_root / "runtime"
        features_dir = state_root / "features"
        queue_root = root / "queue"
        claims_dir = runtime / "claims"
        agent_scans = runtime / "agent_scans"
        mcp_audits = runtime / "mcp_audits"
        runtime.mkdir(parents=True, exist_ok=True)
        features_dir.mkdir(parents=True, exist_ok=True)
        claims_dir.mkdir(parents=True, exist_ok=True)
        agent_scans.mkdir(parents=True, exist_ok=True)
        mcp_audits.mkdir(parents=True, exist_ok=True)
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)

        db_path = runtime / "orchestrator.db"
        old = {
            name: orchestrator.state_engine_wipe_runtime_state.__globals__[name]
            for name in (
                "STATE_ROOT", "RUNTIME_DIR", "FEATURES_DIR", "QUEUE_ROOT", "CLAIMS_DIR",
                "EVENTS_LOG", "METRICS_LOG", "TRANSITIONS_LOG", "ALLOWLIST_PATH",
                "AGENT_SCAN_DIR", "MCP_AUDIT_DIR", "STATE_ENGINE_DB_PATH",
                "_active_orchestrator_launch_agent_labels",
            )
        }
        old_env = {
            "STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"),
            "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH"),
        }
        os.environ["STATE_ENGINE_MODE"] = "primary"
        os.environ["STATE_ENGINE_PATH"] = str(db_path)
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        orchestrator.STATE_ROOT = root
        orchestrator.RUNTIME_DIR = runtime
        orchestrator.FEATURES_DIR = features_dir
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.CLAIMS_DIR = claims_dir
        orchestrator.EVENTS_LOG = runtime / "events.jsonl"
        orchestrator.METRICS_LOG = runtime / "metrics.jsonl"
        orchestrator.TRANSITIONS_LOG = runtime / "transitions.log"
        orchestrator.ALLOWLIST_PATH = runtime / "allowlist.json"
        orchestrator.AGENT_SCAN_DIR = agent_scans
        orchestrator.MCP_AUDIT_DIR = mcp_audits
        orchestrator.STATE_ENGINE_DB_PATH = db_path
        orchestrator._active_orchestrator_launch_agent_labels = lambda: []
        try:
            cfg = orchestrator.load_config()
            engine = orchestrator.get_state_engine(cfg=cfg)
            engine.initialize()
            engine.seed_blocker_codes(orchestrator.BLOCKER_CODES)
            engine.upsert_memory_observation({
                "project": "devmini-orchestrator",
                "source_doc": "DECISIONS.md",
                "section_key": "wipe-74",
                "type": "decision",
                "title": "Preserved memory",
                "content": "This observation survives the runtime wipe.",
                "importance": 5,
                "created_at": "2026-04-21T00:00:00",
            })
            conn = engine.connect()
            with conn:
                conn.execute(
                    """
                    INSERT INTO tasks(task_id, state, created_at, created_at_epoch, state_updated_at, engine, role, project, summary, metadata_json)
                    VALUES ('task-74', 'queued', '2026-04-21T00:00:00', 1, '2026-04-21T00:00:00', 'codex', 'implementer', 'demo', 'wipe me', '{}')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO task_transitions(task_id, from_state, to_state, reason, created_at, created_at_epoch)
                    VALUES ('task-74', 'new', 'queued', 'seed', '2026-04-21T00:00:00', 1)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO features(feature_id, created_at, created_at_epoch, status, project, metadata_json)
                    VALUES ('feature-74', '2026-04-21T00:00:00', 1, 'open', 'demo', '{}')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO feature_children(feature_id, task_id, role, order_idx, created_at)
                    VALUES ('feature-74', 'task-74', 'implementer', 0, '2026-04-21T00:00:00')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO self_repair_issues(issue_id, feature_id, created_at, created_at_epoch, status, metadata_json)
                    VALUES ('issue-74', 'feature-74', '2026-04-21T00:00:00', 1, 'planned', '{}')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO self_repair_deliberations(issue_id, created_at, created_at_epoch, stage, verdict, panel, summary)
                    VALUES ('issue-74', '2026-04-21T00:00:01', 2, 'triage', 'approve', 'council', 'seed')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO artifacts(task_id, kind, file_path, created_at)
                    VALUES ('task-74', 'patch', 'artifacts/task-74.patch', '2026-04-21T00:00:00')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO events(kind, created_at, created_at_epoch, payload_json)
                    VALUES ('seed', '2026-04-21T00:00:00', 1, '{}')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO metrics(name, value, metric_type, created_at_epoch, tags_json)
                    VALUES ('seed.metric', 1.0, 'gauge', 1, '{}')
                    """
                )
            engine.record_task_cost(ts="2026-04-21T00:00:02", task_id="task-74", engine="codex", model="gpt-5.4", input_tokens=10, output_tokens=5, cost_usd=0.12)
            engine.record_environment_check(ts="2026-04-21T00:00:03", project="demo", result="blocked", blocker_summary="seed")
            engine.record_orphan_recovery(ts="2026-04-21T00:00:04", task_id="task-74", from_state="claimed", age_seconds=120)
            engine.record_task_bypass(ts="2026-04-21T00:00:05", task_id="task-74", gate="env", reason="self_repair")

            orchestrator.write_json_atomic(queue_root / "queued" / "task-74.json", {"task_id": "task-74", "state": "queued"})
            orchestrator.write_json_atomic(features_dir / "feature-74.json", {"feature_id": "feature-74", "status": "open"})
            orchestrator.TRANSITIONS_LOG.write_text("task-74 seed\n", encoding="utf-8")
            orchestrator.EVENTS_LOG.write_text("{\"kind\":\"seed\"}\n", encoding="utf-8")
            orchestrator.METRICS_LOG.write_text("{\"name\":\"seed.metric\"}\n", encoding="utf-8")
            orchestrator.ALLOWLIST_PATH.write_text("{\"operators\": [1]}\n", encoding="utf-8")
            (claims_dir / "task-74.pid").write_text("99999\n", encoding="utf-8")
            (agent_scans / "scan.json").write_text("{\"accepted\": true}\n", encoding="utf-8")
            (mcp_audits / "audit.json").write_text("{\"ok\": true}\n", encoding="utf-8")

            result = orchestrator.state_engine_wipe_runtime_state(cfg=cfg)
            status = orchestrator.state_engine_status(cfg=cfg)
            hits = engine.memory_search("preserved memory", project="devmini-orchestrator", limit=3)
        finally:
            for key, value in old.items():
                orchestrator.state_engine_wipe_runtime_state.__globals__[key] = value
            orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]
            if old_env["STATE_ENGINE_PATH"] is None:
                os.environ.pop("STATE_ENGINE_PATH", None)
            else:
                os.environ["STATE_ENGINE_PATH"] = old_env["STATE_ENGINE_PATH"]
        return {
            "runtime_state_wiped": bool(result.get("wiped")) and result["after"]["tasks"] == 0 and result["after"]["features"] == 0,
            "knowledge_preserved": result["after"]["memory_observations"] == 1 and result["after"]["schema_migrations"] >= 1 and result["after"]["blocker_codes"] >= 1 and bool(hits),
            "fs_mirror_wiped": result["fs_queue_count"] == 0 and result["fs_feature_count"] == 0 and not result["transitions_log_exists"] and not result["events_log_exists"] and not result["metrics_log_exists"],
            "restart_status_clean": status.get("integrity_check") == "ok" and result["allowlist_preserved"] and result["agent_scans_preserved"] and result["mcp_audits_preserved"],
        }


def _run_memory_hybrid(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(
                root=root,
                db_path=root / "runtime" / "orchestrator.db",
                migrations_dir=repo_root / "state" / "migrations",
                mode="primary",
            )
        )
        status = engine.initialize()
        title_to_id = {}
        for row in scenario["observations"]:
            obs_id = engine.upsert_memory_observation(dict(row))
            title_to_id[row["title"]] = obs_id
        engine.rebuild_memory_fts()
        fts_only = engine.memory_search(
            scenario["query"],
            project=scenario["project"],
            limit=int(scenario.get("limit") or 5),
            semantic_candidates=[],
        )
        hybrid = engine.memory_search(
            scenario["query"],
            project=scenario["project"],
            limit=int(scenario.get("limit") or 5),
            semantic_candidates=[title_to_id[title] for title in scenario.get("semantic_titles") or []],
        )
        return {
            "vec_enabled": status.get("vec_enabled"),
            "fts_titles": [row["title"] for row in fts_only],
            "hybrid_titles": [row["title"] for row in hybrid],
        }


def _run_memory_vec_missing(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(
                root=root,
                db_path=root / "runtime" / "orchestrator.db",
                migrations_dir=repo_root / "state" / "migrations",
                mode="primary",
            )
        )
        def _disable_vec(conn):
            engine._vec_enabled = False
            engine._vec_error = "sqlite-vec unavailable"
            engine._local.vec_checked = True
        engine._try_enable_sqlite_vec = _disable_vec
        status = engine.initialize()
        for row in scenario["observations"]:
            engine.upsert_memory_observation(dict(row))
        engine.rebuild_memory_fts()
        rows = engine.memory_search(
            scenario["query"],
            project=scenario["project"],
            limit=int(scenario.get("limit") or 5),
        )
        return {
            "vec_enabled": status.get("vec_enabled"),
            "result_titles": [row["title"] for row in rows],
            "vec_error_present": bool(status.get("vec_error")),
        }


def _run_metrics_scale_1m(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    total_rows = int(scenario.get("rows") or 1_000_000)
    limit = int(scenario.get("limit") or 100)
    max_ms = float(scenario.get("max_ms") or 500.0)
    query_name = scenario.get("metric_name") or "scenario.metric"
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(
                root=root,
                db_path=root / "runtime" / "orchestrator.db",
                migrations_dir=repo_root / "state" / "migrations",
                mode="primary",
            )
        )
        engine.initialize()
        conn = engine.connect()
        batch = []
        for idx in range(total_rows):
            name = query_name if idx % 2 == 0 else "noise.metric"
            batch.append((name, float(idx), "gauge", idx + 1, "{}"))
            if len(batch) >= 10_000:
                with conn:
                    conn.executemany(
                        "INSERT INTO metrics(name, value, metric_type, created_at_epoch, tags_json) VALUES (?, ?, ?, ?, ?)",
                        batch,
                    )
                batch.clear()
        if batch:
            with conn:
                conn.executemany(
                    "INSERT INTO metrics(name, value, metric_type, created_at_epoch, tags_json) VALUES (?, ?, ?, ?, ?)",
                    batch,
                )
        started = time.perf_counter()
        rows = engine.read_metrics(name=query_name, limit=limit)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        values = [int(row["value"]) for row in rows]
        return {
            "row_count": len(rows),
            "within_budget": elapsed_ms < max_ms,
            "newest_window": values == sorted(values)[-limit:],
        }


def _run_transitions_scale_100k(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    total_rows = int(scenario.get("rows") or 100_000)
    target_rows = int(scenario.get("target_rows") or 1_000)
    max_ms = float(scenario.get("max_ms") or 100.0)
    task_id = scenario.get("task_id") or "task-hot"
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(
                root=root,
                db_path=root / "runtime" / "orchestrator.db",
                migrations_dir=repo_root / "state" / "migrations",
                mode="primary",
            )
        )
        engine.initialize()
        conn = engine.connect()
        batch = []
        for idx in range(total_rows):
            current_task = task_id if idx < target_rows else f"task-noise-{idx % 200}"
            batch.append(
                (
                    current_task,
                    "queued",
                    "running",
                    f"reason-{idx}",
                    f"2026-01-01T00:00:{idx % 60:02d}",
                    idx + 1,
                )
            )
            if len(batch) >= 10_000:
                with conn:
                    conn.executemany(
                        """
                        INSERT INTO task_transitions(task_id, from_state, to_state, reason, created_at, created_at_epoch)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        batch,
                    )
                batch.clear()
        if batch:
            with conn:
                conn.executemany(
                    """
                    INSERT INTO task_transitions(task_id, from_state, to_state, reason, created_at, created_at_epoch)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
        started = time.perf_counter()
        rows = engine.read_transitions(task_id=task_id, limit=target_rows)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        reasons = [row["reason"] for row in rows]
        return {
            "row_count": len(rows),
            "within_budget": elapsed_ms < max_ms,
            "deterministic_first": reasons[0] == "reason-0",
            "deterministic_last": reasons[-1] == f"reason-{target_rows - 1}",
        }


def _run_memory_obs_scale_10k(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    total_rows = int(scenario.get("rows") or 10_000)
    limit = int(scenario.get("limit") or 10)
    max_ms = float(scenario.get("max_ms") or 200.0)
    project = scenario.get("project") or "devmini-orchestrator"
    semantic_titles = set(scenario.get("semantic_titles") or [])
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(
                root=root,
                db_path=root / "runtime" / "orchestrator.db",
                migrations_dir=repo_root / "state" / "migrations",
                mode="primary",
            )
        )
        engine.initialize()
        semantic_ids = []
        for idx in range(total_rows):
            title = f"Observation {idx}"
            if idx == total_rows - 1:
                title = scenario["title_hit"]
            if idx in (1, 7, 31):
                title = scenario["semantic_titles"][len(semantic_ids)]
            obs_id = engine.upsert_memory_observation(
                {
                    "project": project,
                    "source_doc": "DECISIONS.md",
                    "section_key": f"section-{idx}",
                    "type": "decision",
                    "title": title,
                    "content": scenario["semantic_content"] if title in semantic_titles else f"noise content {idx}",
                    "created_at": f"2026-01-01T00:{idx % 60:02d}:00",
                    "importance": 5,
                    "tags": ["memory"],
                    "metadata": {},
                }
            )
            if title in semantic_titles:
                semantic_ids.append(obs_id)
        engine.rebuild_memory_fts()
        started = time.perf_counter()
        rows = engine.memory_search(
            scenario["query"],
            project=project,
            limit=limit,
            semantic_candidates=semantic_ids,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        titles = [row["title"] for row in rows]
        return {
            "row_count": len(rows),
            "within_budget": elapsed_ms < max_ms,
            "title_hit_first": bool(titles) and titles[0] == scenario["title_hit"],
            "semantic_present": all(title in titles for title in semantic_titles),
        }


def _run_feature_fanout_50(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    feature_id = scenario.get("feature_id") or "feature-fanout"
    child_count = int(scenario.get("child_count") or 50)
    max_ms = float(scenario.get("max_ms") or 50.0)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        queue_root = root / "queue"
        features_dir = root / "features"
        runtime_dir = root / "runtime"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        features_dir.mkdir(parents=True, exist_ok=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        old_env = {
            "STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"),
            "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH"),
        }
        old = {
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
            "STATE_ROOT": orchestrator.STATE_ROOT,
            "RUNTIME_DIR": orchestrator.RUNTIME_DIR,
            "STATE_ENGINE_DB_PATH": orchestrator.STATE_ENGINE_DB_PATH,
        }
        os.environ["STATE_ENGINE_MODE"] = "primary"
        os.environ["STATE_ENGINE_PATH"] = str(runtime_dir / "orchestrator.db")
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.FEATURES_DIR = features_dir
        orchestrator.STATE_ROOT = root
        orchestrator.RUNTIME_DIR = runtime_dir
        orchestrator.STATE_ENGINE_DB_PATH = runtime_dir / "orchestrator.db"
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        try:
            engine = orchestrator.get_state_engine()
            engine.initialize()
            created_at = "2026-01-01T00:00:00"
            created_epoch = int(datetime.fromisoformat(created_at).timestamp())
            metadata = {
                "feature_id": feature_id,
                "status": "open",
                "project": "devmini-orchestrator",
                "summary": "fanout",
                "created_at": created_at,
                "child_task_ids": [f"task-{idx:03d}" for idx in range(child_count)],
            }
            conn = engine.connect()
            with conn:
                conn.execute(
                    """
                    INSERT INTO features(feature_id, created_at, created_at_epoch, status, project, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (feature_id, created_at, created_epoch, "open", "devmini-orchestrator", json.dumps(metadata, sort_keys=True)),
                )
                for idx in range(child_count):
                    task_id = f"task-{idx:03d}"
                    task_created = f"2026-01-01T00:{idx % 60:02d}:00"
                    task_epoch = created_epoch + idx
                    state = "done" if idx < child_count - 1 else "running"
                    task = {
                        "task_id": task_id,
                        "state": state,
                        "created_at": task_created,
                        "state_updated_at": task_created,
                        "engine": "codex",
                        "role": "implementer",
                        "project": "devmini-orchestrator",
                        "feature_id": feature_id,
                        "summary": f"child {idx}",
                        "attempt": 1,
                    }
                    conn.execute(
                        """
                        INSERT INTO tasks(
                            task_id, state, created_at, created_at_epoch, state_updated_at, engine, role, project, feature_id, summary, attempt, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            task_id,
                            state,
                            task_created,
                            task_epoch,
                            task_created,
                            "codex",
                            "implementer",
                            "devmini-orchestrator",
                            feature_id,
                            f"child {idx}",
                            1,
                            json.dumps(task, sort_keys=True),
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO feature_children(feature_id, task_id, role, order_idx, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (feature_id, task_id, "implementer", idx, task_created),
                    )
            started = time.perf_counter()
            workflow = orchestrator.open_feature_workflow_summaries()[0]
            elapsed_ms = (time.perf_counter() - started) * 1000.0
        finally:
            orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
            orchestrator.QUEUE_ROOT = old["QUEUE_ROOT"]
            orchestrator.FEATURES_DIR = old["FEATURES_DIR"]
            orchestrator.STATE_ROOT = old["STATE_ROOT"]
            orchestrator.RUNTIME_DIR = old["RUNTIME_DIR"]
            orchestrator.STATE_ENGINE_DB_PATH = old["STATE_ENGINE_DB_PATH"]
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]
            if old_env["STATE_ENGINE_PATH"] is None:
                os.environ.pop("STATE_ENGINE_PATH", None)
            else:
                os.environ["STATE_ENGINE_PATH"] = old_env["STATE_ENGINE_PATH"]
        return {
            "within_budget": elapsed_ms < max_ms,
            "frontier_task_id": workflow["frontier"]["task_id"],
            "child_state_count": len(workflow["child_states"]),
            "planner_task_id": workflow["planner"]["task_id"],
        }


def _run_vec_dimension_mismatch(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        status = engine.initialize()
        if not status.get("vec_enabled"):
            return {"vec_enabled": False, "dimension_error": True}
        old_embed = state_engine._semantic_embedding
        state_engine._semantic_embedding = lambda text, dims=state_engine.DEFAULT_MEMORY_VEC_DIMENSIONS: [0.1] * (dims + 1)
        try:
            engine.upsert_memory_observation(
                {
                    "project": "devmini-orchestrator",
                    "source_doc": "DECISIONS.md",
                    "section_key": "dim-drift",
                    "type": "decision",
                    "title": "dimension drift",
                    "content": "dimension drift",
                    "created_at": "2026-01-01T00:00:00",
                    "importance": 5,
                    "tags": [],
                    "metadata": {},
                }
            )
        finally:
            state_engine._semantic_embedding = old_embed
        return {
            "vec_enabled": True,
            "dimension_error": "vec_dimension_mismatch" in str(engine.status().get("vec_error") or ""),
        }


def _run_vec_rebuild_complete(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        status = engine.initialize()
        for idx in range(5):
            engine.upsert_memory_observation(
                {
                    "project": "devmini-orchestrator",
                    "source_doc": "DECISIONS.md",
                    "section_key": f"vec-{idx}",
                    "type": "decision",
                    "title": f"vec {idx}",
                    "content": f"vector rebuild {idx}",
                    "created_at": f"2026-01-01T00:00:0{idx}",
                    "importance": 5,
                    "tags": [],
                    "metadata": {},
                }
            )
        conn = engine.connect()
        before = after = rebuilt = 0
        if status.get("vec_enabled"):
            before = int(conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0])
            with conn:
                conn.execute("DELETE FROM memory_vectors")
            rebuilt = engine.rebuild_memory_vectors()
            after = int(conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0])
        return {
            "vec_enabled": bool(status.get("vec_enabled")),
            "rebuilt_all": (not status.get("vec_enabled")) or (before == 5 and rebuilt == 5 and after == 5),
        }


def _run_vec_version_drift(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        status = engine.initialize()
        engine.upsert_memory_observation(
            {
                "project": "devmini-orchestrator",
                "source_doc": "DECISIONS.md",
                "section_key": "vec-version",
                "type": "decision",
                "title": "Version Drift Title",
                "content": "Version drift query",
                "created_at": "2026-01-01T00:00:00",
                "importance": 5,
                "tags": [],
                "metadata": {},
            }
        )
        engine.rebuild_memory_fts()
        conn = engine.connect()

        class ProxyConn:
            def __init__(self, inner):
                self.inner = inner
            def execute(self, sql, params=()):
                if "FROM memory_vectors" in sql:
                    raise sqlite3.DatabaseError("sqlite-vec version mismatch on restore")
                return self.inner.execute(sql, params)
            def __enter__(self):
                self.inner.__enter__()
                return self
            def __exit__(self, exc_type, exc, tb):
                return self.inner.__exit__(exc_type, exc, tb)
            def __getattr__(self, name):
                return getattr(self.inner, name)

        rows = engine.memory_search("Version Drift Title", project="devmini-orchestrator", limit=3, conn=ProxyConn(conn))
        return {
            "vec_enabled": bool(status.get("vec_enabled")),
            "search_survived": bool(rows) and rows[0]["title"] == "Version Drift Title",
            "warning_present": "version mismatch" in str(engine.status().get("vec_error") or ""),
        }


def _run_fts_shadow_corrupt(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        engine.initialize()
        engine.upsert_memory_observation(
            {
                "project": "devmini-orchestrator",
                "source_doc": "DECISIONS.md",
                "section_key": "fts-corrupt",
                "type": "decision",
                "title": "FTS Corrupt Title",
                "content": "fts recovery",
                "created_at": "2026-01-01T00:00:00",
                "importance": 5,
                "tags": [],
                "metadata": {},
            }
        )
        conn = engine.connect()
        tripped = {"done": False}

        class ProxyConn:
            def __init__(self, inner):
                self.inner = inner
            def execute(self, sql, params=()):
                if "memory_obs_fts MATCH" in sql and not tripped["done"]:
                    tripped["done"] = True
                    raise sqlite3.DatabaseError("database disk image is malformed")
                return self.inner.execute(sql, params)
            def __enter__(self):
                self.inner.__enter__()
                return self
            def __exit__(self, exc_type, exc, tb):
                return self.inner.__exit__(exc_type, exc, tb)
            def __getattr__(self, name):
                return getattr(self.inner, name)

        rows = engine.memory_search("FTS Corrupt Title", project="devmini-orchestrator", limit=3, conn=ProxyConn(conn))
        return {
            "rebuild_retry_worked": bool(rows) and rows[0]["title"] == "FTS Corrupt Title",
            "single_corruption_simulated": tripped["done"],
        }


def _run_fts_rebuild_during_search(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        engine.initialize()
        for idx in range(200):
            engine.upsert_memory_observation(
                {
                    "project": "devmini-orchestrator",
                    "source_doc": "DECISIONS.md",
                    "section_key": f"fts-race-{idx}",
                    "type": "decision",
                    "title": f"Race Title {idx}",
                    "content": "concurrent rebuild search",
                    "created_at": f"2026-01-01T00:{idx % 60:02d}:00",
                    "importance": 5,
                    "tags": [],
                    "metadata": {},
                }
            )
        errors = []
        results = []
        barrier = threading.Barrier(2)

        def do_search():
            try:
                barrier.wait()
                results.extend(engine.memory_search("Race Title 199", project="devmini-orchestrator", limit=3))
            except Exception as exc:
                errors.append(str(exc))

        def do_rebuild():
            try:
                barrier.wait()
                engine.rebuild_memory_fts()
            except Exception as exc:
                errors.append(str(exc))

        t1 = threading.Thread(target=do_search)
        t2 = threading.Thread(target=do_rebuild)
        t1.start(); t2.start(); t1.join(); t2.join()
        return {
            "no_errors": not errors,
            "query_completed": bool(results),
        }


def _run_metadata_json_malformed(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        engine.initialize()
        conn = engine.connect()
        with conn:
            conn.execute(
                """
                INSERT INTO tasks(task_id, state, created_at, created_at_epoch, state_updated_at, engine, role, project, summary, attempt, metadata_json)
                VALUES ('task-bad-json', 'queued', '2026-01-01T00:00:00', 1, '2026-01-01T00:00:00', 'codex', 'implementer', 'demo', 'bad json', 1, '{incomplete')
                """
            )
        found = engine.find_task("task-bad-json")
        return {
            "found": found is not None,
            "metadata_defaulted": bool(found) and found[1] == {},
        }


def _run_timestamp_future_epoch(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        engine.initialize()
        conn = engine.connect()
        with conn:
            conn.execute("INSERT INTO metrics(name, value, metric_type, created_at_epoch, tags_json) VALUES ('old', 1, 'gauge', ?, '{}')", (1,))
            conn.execute("INSERT INTO metrics(name, value, metric_type, created_at_epoch, tags_json) VALUES ('future', 1, 'gauge', ?, '{}')", (99999999999,))
        deleted = engine.purge_old_metrics(cutoff_epoch=100)
        names = [row["name"] for row in engine.read_metrics()]
        return {
            "deleted_count": deleted,
            "future_removed": "future" not in names,
        }


def _run_blocker_code_missing(repo_root, scenario):
    orchestrator = _load_module("orchestrator", repo_root / "bin" / "orchestrator.py")
    dashboard = _load_module("dashboard_feed", repo_root / "bin" / "dashboard_feed.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old_queue = orchestrator.QUEUE_ROOT
        old_dash_queue = dashboard.o.QUEUE_ROOT
        old_env = os.environ.get("STATE_ENGINE_MODE")
        orchestrator.QUEUE_ROOT = queue_root
        dashboard.o.QUEUE_ROOT = queue_root
        os.environ["STATE_ENGINE_MODE"] = "off"
        try:
            orchestrator.write_json_atomic(
                queue_root / "blocked" / "task-unknown.json",
                {
                    "task_id": "task-unknown",
                    "state": "blocked",
                    "blocker": {"code": "new_unknown_blocker", "summary": "unknown blocker"},
                },
            )
            rows = dashboard._blocker_codes()
        finally:
            orchestrator.QUEUE_ROOT = old_queue
            dashboard.o.QUEUE_ROOT = old_dash_queue
            if old_env is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env
        return {
            "surface_unknown_blocker": any(row["code"] == "new_unknown_blocker" for row in rows),
        }


def _run_dashboard_environment_reads_db(repo_root, scenario):
    orchestrator = _load_module("orchestrator", repo_root / "bin" / "orchestrator.py")
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    dashboard = _load_module("dashboard_feed", repo_root / "bin" / "dashboard_feed.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        runtime = root / "state" / "runtime"
        db_path = runtime / "orchestrator.db"
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(
                root=root,
                db_path=db_path,
                migrations_dir=repo_root / "state" / "migrations",
                mode="primary",
            )
        )
        engine.initialize()
        engine.record_environment_check(ts="2026-04-23T19:00:00", project="global", result="ok", blocker_summary=None)
        engine.record_environment_check(ts="2026-04-23T19:00:01", project="demo", result="blocked", blocker_summary="project_main_dirty:dirty main")
        old_dash_state_engine = dashboard._state_engine
        old_dash_dashboard_server = dashboard._dashboard_server
        old_dash_load_config = dashboard.o.load_config
        old_orch_env_health = orchestrator.environment_health
        dashboard._state_engine = lambda: engine
        dashboard._dashboard_server = lambda: {"state_engine": {"mode": "primary", "integrity_check": "ok", "db_path": str(db_path), "applied_migrations": ["0001_initial"]}}
        dashboard.o.load_config = lambda: {"projects": [{"name": "demo"}]}
        orchestrator.environment_health = lambda refresh=False: {
            "ok": False,
            "generated_at": "2026-04-23T19:00:02",
            "issues": [
                {"project": None, "severity": "error", "code": "delivery_auth_expired", "summary": "required binary missing: gh"},
                {"project": "demo", "severity": "error", "code": "project_main_dirty", "summary": "dirty main"},
            ],
        }
        try:
            panel = dashboard._environment_panel({"environment_checks": []}, dashboard._dashboard_server())
        finally:
            dashboard._state_engine = old_dash_state_engine
            dashboard._dashboard_server = old_dash_dashboard_server
            dashboard.o.load_config = old_dash_load_config
            orchestrator.environment_health = old_orch_env_health
        projects = {row["project"]: row for row in panel["projects"]}
        return {
            "global_result": projects["global"]["result"],
            "global_blocker_summary": projects["global"]["blocker_summary"],
            "demo_result": projects["demo"]["result"],
            "demo_blocker_summary": projects["demo"]["blocker_summary"],
        }


def _run_env_health_launchctl_fallback(repo_root, scenario):
    orchestrator = _load_module("orchestrator", repo_root / "bin" / "orchestrator.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        config_dir = root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        fake_gh = root / "gh"
        fake_gh.write_text("#!/bin/sh\nexit 0\n")
        old = {
            "STATE_ROOT": orchestrator.STATE_ROOT,
            "GH_TOKEN_PATH": orchestrator.GH_TOKEN_PATH,
            "GH_CANDIDATE_PATHS": orchestrator.GH_CANDIDATE_PATHS,
            "CODEX_CANDIDATE_PATHS": orchestrator.CODEX_CANDIDATE_PATHS,
            "CLAUDE_CANDIDATE_PATHS": orchestrator.CLAUDE_CANDIDATE_PATHS,
            "_launchctl_getenv": orchestrator._launchctl_getenv,
            "_launchctl_loaded_labels": orchestrator._launchctl_loaded_labels,
            "_state_engine_write_enabled": orchestrator._state_engine_write_enabled,
        }
        old_env = {
            "GH_TOKEN": os.environ.get("GH_TOKEN"),
            "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN"),
        }
        orchestrator.STATE_ROOT = root
        orchestrator.GH_TOKEN_PATH = config_dir / "gh-token"
        orchestrator.GH_CANDIDATE_PATHS = (str(fake_gh),)
        orchestrator.CODEX_CANDIDATE_PATHS = (sys.executable,)
        orchestrator.CLAUDE_CANDIDATE_PATHS = (sys.executable,)
        orchestrator._launchctl_loaded_labels = lambda: set()
        orchestrator._launchctl_getenv = lambda name: {
            "GH_TOKEN": "gh-launchctl-token",
            "TELEGRAM_BOT_TOKEN": "tg-launchctl-token",
        }.get(name, "")
        orchestrator._state_engine_write_enabled = lambda cfg=None: False
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        try:
            health = orchestrator.environment_health(refresh=True)
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        summaries = [issue.get("summary") for issue in health.get("issues") or []]
        return {
            "gh_missing": "required binary missing: gh" in summaries,
            "gh_token_missing": "GH_TOKEN unavailable" in summaries,
            "telegram_missing": "telegram bot token unavailable" in summaries,
        }


def _run_planner_depends_on_ids(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    candidate_slices = [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]
    slice_ids, aliases = worker._slice_alias_maps(candidate_slices)
    dropped = []
    sibling_task_ids = ["task-s1", "task-s2"]
    resolved = worker._resolve_slice_depends(
        ["s1"],
        idx=1,
        sibling_task_ids=sibling_task_ids,
        dropped=dropped,
        raw_tpl="lvc-implement-operator",
        summ="slice 2",
        slice_ids=slice_ids,
        alias_to_canonical=aliases,
    )
    return {
        "resolved": resolved,
        "dropped": dropped,
    }


def _run_planner_depends_on_slice_alias(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    candidate_slices = [
        {"summary": "slice 1", "braid_template": "lvc-implement-operator"},
        {"summary": "slice 2", "braid_template": "lvc-implement-operator", "depends_on": ["slice-1"]},
        {"summary": "slice 3", "braid_template": "lvc-implement-operator", "depends_on": ["slice-2"]},
    ]
    slice_ids, aliases = worker._slice_alias_maps(candidate_slices)
    dropped = []
    normalized = worker._normalize_slice_depends(
        candidate_slices[2]["depends_on"],
        idx=2,
        dropped=dropped,
        raw_tpl="lvc-implement-operator",
        summ="slice 3",
        slice_ids=slice_ids,
        alias_to_canonical=aliases,
    )
    resolved = worker._resolve_slice_depends(
        candidate_slices[2]["depends_on"],
        idx=2,
        sibling_task_ids=["task-s1", "task-s2"],
        dropped=dropped,
        raw_tpl="lvc-implement-operator",
        summ="slice 3",
        slice_ids=slice_ids,
        alias_to_canonical=aliases,
    )
    return {
        "slice_ids": slice_ids,
        "normalized": normalized,
        "resolved": resolved,
        "dropped": dropped,
    }


def _run_depends_on_context_prompts(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    task = {
        "task_id": "task-s2",
        "summary": "Implement restore path",
        "engine_args": {
            "council": {
                "panel": ["aristotle"],
                "execution_path": "slice-1 -> slice-2 -> slice-3",
                "key_agreements": ["keep restore after writer"],
                "dissent": [],
            },
            "slice": {
                "id": "slice-2",
                "index": 2,
                "depends_on": ["slice-1"],
                "execution_path": "slice-1 -> slice-2 -> slice-3",
                "plan": [
                    {"id": "slice-1", "summary": "writer", "braid_template": "lvc-implement-operator", "depends_on": [], "state": "done"},
                    {"id": "slice-2", "summary": "restore", "braid_template": "lvc-implement-operator", "depends_on": ["slice-1"], "state": "enqueued"},
                    {"id": "slice-3", "summary": "historian", "braid_template": "lvc-historian", "depends_on": ["slice-2"], "state": "planned"},
                ],
            },
        },
    }
    block = worker._render_slice_context_block(task)
    prompt = worker.build_codex_prompt(task, "graph", "memory")
    planner_prompt = worker.planner_system_prompt("lvc-standard", "body")
    return {
        "block_has_slice_id": "slice_id: slice-2" in block,
        "block_has_depends": "depends_on: [slice-1]" in block,
        "block_has_plan": "slice-3 state=planned" in block,
        "codex_has_slice_context": "[SLICE CONTEXT]" in prompt,
        "codex_has_execution_path": "execution_path: slice-1 -> slice-2 -> slice-3" in prompt,
        "planner_uses_string_ids": "optional depends_on (list[str])" in planner_prompt and "slice-1" in planner_prompt,
    }


def _run_braid_trailer_markdown_wrapped(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    raw = """Some body

`BRAID_OK: APPROVE — minimal one-line historian append`
"""
    qa_raw = """Analysis

* `BRAID_OK: QA_SUFFICIENT — smoke and scope look adequate`
"""
    topo_lines = [
        "discussion",
        "`BRAID_TOPOLOGY_ERROR: patch_anchor_drift repeated apply_patch verification failures (2)`",
    ]
    return {
        "review_verdict": worker._extract_review_verdict(raw),
        "qa_verdict": worker._extract_review_verdict(qa_raw),
        "topology_trailer": worker._find_braid_trailer(topo_lines),
    }


def _run_braid_refine_and_pr_wrapped(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    refine_lines = [
        "body",
        "* `BRAID_REFINE: CheckBaseline: add baseline_red edge to End`",
    ]
    pr_ok_lines = [
        "body",
        "`BRAID_OK: rebased on feature branch and fixed review comments`",
    ]
    parsed_refine = worker.parse_braid_refine(worker._find_braid_trailer(refine_lines))
    return {
        "refine_trailer": worker._find_braid_trailer(refine_lines),
        "refine_node": (parsed_refine or {}).get("node_id"),
        "pr_ok_trailer": worker._find_braid_trailer(pr_ok_lines),
    }


def _run_council_payload_normalization(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    parsed = {"verdict": "approve", "reason": "looks good"}
    normalized = worker._normalize_council_payload(parsed, panel=("socrates", "ada"), stage="pre_execute")
    return {
        "panel": normalized.get("panel"),
        "stage": normalized.get("stage"),
        "execution_path": normalized.get("execution_path"),
        "chosen_strategy": normalized.get("chosen_strategy"),
        "retry_conditions": normalized.get("retry_conditions"),
        "rejected_strategies": normalized.get("rejected_strategies"),
    }


def _run_planner_output_normalization(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    raw_object = """{"execution_path":"slice-1 -> slice-2","slices":[{"id":"slice-1","summary":"writer","braid_template":"lvc-implement-operator"},{"id":"slice-2","summary":"restore","braid_template":"lvc-implement-operator","depends_on":["slice-1"]}]}"""
    plan1, slices1 = worker._parse_planner_output(raw_object, council_members=("aristotle",), self_repair=False)
    raw_array = """[{"id":"slice-1","summary":"writer","braid_template":"lvc-implement-operator"}]"""
    try:
        worker._parse_planner_output(raw_array, council_members=("aristotle",), self_repair=False)
        array_error = None
    except Exception as exc:
        array_error = str(exc)
    return {
        "object_execution_path": plan1.get("execution_path"),
        "object_slice_depends": slices1[1].get("depends_on"),
        "array_error": array_error,
    }


def _run_end_to_end_handoff_contract(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    raw = """
{"panel":["aristotle"],"execution_path":"slice-1 -> slice-2 -> slice-3","slices":[
 {"id":"slice-1","summary":"writer","braid_template":"lvc-implement-operator"},
 {"id":"slice-2","summary":"restore","braid_template":"lvc-implement-operator","depends_on":["slice-1"]},
 {"id":"slice-3","summary":"historian","braid_template":"lvc-historian-update","depends_on":["slice-2"]}
]}
"""
    plan, slices = worker._parse_planner_output(raw, council_members=("aristotle",), self_repair=False)
    slice_ids, aliases = worker._slice_alias_maps(slices)
    dropped = []
    normalized = worker._normalize_slice_depends(
        slices[2]["depends_on"],
        idx=2,
        dropped=dropped,
        raw_tpl=slices[2]["braid_template"],
        summ=slices[2]["summary"],
        slice_ids=slice_ids,
        alias_to_canonical=aliases,
    )
    task = {
        "task_id": "task-s3",
        "summary": slices[2]["summary"],
        "engine_args": {
            "council": {
                "panel": plan.get("panel"),
                "execution_path": plan.get("execution_path"),
                "key_agreements": [],
                "dissent": [],
            },
            "slice": {
                "id": "slice-3",
                "index": 3,
                "depends_on": normalized,
                "execution_path": plan.get("execution_path"),
                "plan": [
                    {"id": "slice-1", "summary": "writer", "braid_template": "lvc-implement-operator", "depends_on": [], "state": "done"},
                    {"id": "slice-2", "summary": "restore", "braid_template": "lvc-implement-operator", "depends_on": ["slice-1"], "state": "done"},
                    {"id": "slice-3", "summary": "historian", "braid_template": "lvc-historian-update", "depends_on": ["slice-2"], "state": "enqueued"},
                ],
            },
        },
    }
    block = worker._render_slice_context_block(task)
    prompt = worker.build_codex_prompt(task, "graph", "memory")
    pr_lines = ['{"status":"ok","summary":"rebased on feature branch and fixed review comments"}']
    return {
        "normalized_depends": normalized,
        "slice_block_has_chain": "slice-2" in block and "slice-3 state=enqueued" in block,
        "codex_prompt_has_slice_context": "[SLICE CONTEXT]" in prompt and "depends_on: [slice-2]" in prompt,
        "pr_feedback_ok": worker._find_braid_trailer(pr_lines),
    }


def _run_braid_result_json_envelope(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    review_raw = """
{"status":"ok","verdict":"approve","summary":"minimal historian append only"}
"""
    mixed_review_raw = """
**Council deliberation**

Earlier tool envelope:
{"type":"result","result":"not the handoff"}

{"status":"ok","verdict":"request_change","summary":"final reviewer handoff"}
"""
    refine_raw = """
{"status":"refine","refine_kind":"template","node_id":"CheckBaseline","condition":"add baseline_red edge to End"}
"""
    topo_raw = """
{"status":"topology_error","summary":"patch_anchor_drift repeated apply_patch verification failures (2)"}
"""
    return {
        "review_verdict": worker._extract_review_verdict(review_raw),
        "mixed_review_verdict": worker._extract_review_verdict(mixed_review_raw),
        "mixed_review_trailer": worker._extract_braid_result_trailer(mixed_review_raw),
        "review_trailer": worker._extract_braid_result_trailer(review_raw),
        "refine_trailer": worker._extract_braid_result_trailer(refine_raw),
        "topology_trailer": worker._extract_braid_result_trailer(topo_raw),
    }


def _run_braid_planner_refine_json_envelope(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    planner_refine_raw = """
{"status":"refine","refine_kind":"planner","summary":"snapshot api missing","question":"clarify where snapshot(Path)/restore(Path) should live and what checkpoint state must be persisted"}
"""
    parsed = worker.parse_braid_planner_refine(worker._extract_braid_result_trailer(planner_refine_raw))
    return {
        "planner_refine_trailer": worker._extract_braid_result_trailer(planner_refine_raw),
        "planner_refine_summary": (parsed or {}).get("summary"),
        "planner_refine_question": (parsed or {}).get("question"),
    }


def _run_template_output_json_envelope(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    wrapped = """
{"mermaid":"```mermaid\\nflowchart TD\\n  A-->B\\n```"}
"""
    fallback = """
noise
```mermaid
flowchart LR
  Start-->End
```
"""
    try:
        fallback_graph = worker._extract_template_graph(fallback)
        fallback_error = None
    except Exception as exc:
        fallback_graph = None
        fallback_error = str(exc)
    return {
        "wrapped_graph": worker._extract_template_graph(wrapped),
        "fallback_graph": fallback_graph,
        "fallback_error": fallback_error,
    }


def _run_claude_result_text_shared(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    structured = '{"type":"result","result":"hello from structured output"}'
    plain = "plain text output"
    try:
        plain_out = worker._extract_claude_result_text(plain)
        plain_error = None
    except Exception as exc:
        plain_out = None
        plain_error = str(exc)
    return {
        "structured": worker._extract_claude_result_text(structured),
        "plain": plain_out,
        "plain_error": plain_error,
    }


def _run_planner_refine_route(repo_root, scenario):
    orchestrator, worker = _load_repo_modules(repo_root)
    calls = {"enqueued": [], "moved": [], "removed_children": []}
    base_task = {
        "task_id": "task-impl",
        "project": "lvc-standard",
        "feature_id": "feature-1",
        "parent_task_id": "task-plan-1",
        "summary": "Implement snapshot writer",
        "braid_template": "lvc-implement-operator",
        "engine_args": {
            "slice": {"id": "slice-1", "depends_on": [], "execution_path": "slice-1 -> slice-2"},
            "council": {"panel": ["aristotle"], "execution_path": "slice-1 -> slice-2"},
        },
    }
    sibling = {
        "task_id": "task-sibling",
        "project": "lvc-standard",
        "feature_id": "feature-1",
        "parent_task_id": "task-plan-1",
        "state": "queued",
    }
    planner_parent = {
        "task_id": "task-plan-1",
        "engine_args": {"roadmap_entry": {"id": "R-002", "title": "Snapshot/restore", "body": "body"}},
    }
    feature = {"feature_id": "feature-1", "summary": "[R-002] Snapshot/restore API"}
    old = {
        name: getattr(worker.o, name)
        for name in (
            "iter_tasks",
            "new_task",
            "enqueue_task",
            "read_feature",
            "find_task",
            "remove_feature_children",
            "move_task",
            "now_iso",
            "set_task_blocker",
        )
    }
    try:
        worker.o.iter_tasks = lambda **kwargs: [dict(sibling)] if kwargs.get("states") == ("queued",) and kwargs.get("project") == "lvc-standard" else []
        worker.o.new_task = lambda **kwargs: {"task_id": "task-planner-refine-1", **kwargs}
        worker.o.enqueue_task = lambda task: calls["enqueued"].append(task)
        worker.o.read_feature = lambda feature_id: dict(feature) if feature_id == "feature-1" else None
        worker.o.find_task = lambda task_id, states=None: ("done", dict(planner_parent)) if task_id == "task-plan-1" else None
        worker.o.remove_feature_children = lambda feature_id, child_ids: calls["removed_children"].append((feature_id, list(child_ids)))
        def fake_move(task_id, from_state, to_state, reason="", mutator=None):
            task_obj = dict(base_task if task_id == "task-impl" else sibling)
            if mutator:
                mutator(task_obj)
            calls["moved"].append({"task_id": task_id, "to_state": to_state, "task": task_obj, "reason": reason})
        worker.o.move_task = fake_move
        worker.o.now_iso = lambda: "2026-04-24T01:00:00"
        worker.o.set_task_blocker = orchestrator.set_task_blocker
        worker.enqueue_braid_planner_refine(
            dict(base_task),
            "lvc-standard",
            "BRAID_PLANNER_REFINE: snapshot api missing :: clarify where snapshot(Path)/restore(Path) should live and what checkpoint state must be persisted",
            from_state="running",
        )
    finally:
        for key, value in old.items():
            setattr(worker.o, key, value)
    planner_task = calls["enqueued"][0]
    moved_impl = next(item for item in calls["moved"] if item["task_id"] == "task-impl")
    moved_sibling = next(item for item in calls["moved"] if item["task_id"] == "task-sibling")
    return {
        "planner_mode": ((planner_task.get("engine_args") or {}).get("mode")),
        "planner_feature_id": planner_task.get("feature_id"),
        "planner_refine_origin_task_id": (((planner_task.get("engine_args") or {}).get("planner_refine") or {}).get("origin_task_id")),
        "planner_refine_question": (((planner_task.get("engine_args") or {}).get("planner_refine") or {}).get("question")),
        "impl_to_state": moved_impl["to_state"],
        "impl_abandoned_reason": moved_impl["task"].get("abandoned_reason"),
        "impl_planner_refine_task_id": ((moved_impl["task"].get("planner_refine_request") or {}).get("planner_task_id")),
        "sibling_to_state": moved_sibling["to_state"],
        "removed_children": calls["removed_children"][0][1],
    }


def _run_template_refine_route_requires_template_context(repo_root, scenario):
    orchestrator, worker = _load_repo_modules(repo_root)
    enqueued = []
    moved = []
    old_move = worker.o.move_task
    old_now = worker.o.now_iso
    old_new_task = worker.o.new_task
    old_enqueue = worker.o.enqueue_task
    try:
        worker.o.new_task = lambda **kwargs: {"task_id": "task-template-refine", **kwargs}
        worker.o.enqueue_task = lambda task: enqueued.append(task)
        def fake_move(task_id, from_state, to_state, reason="", mutator=None):
            task_obj = {
                "task_id": task_id,
                "state": from_state,
                "braid_template": "demo-template",
                "braid_template_hash": "sha256:abc",
            }
            if mutator:
                mutator(task_obj)
            moved.append({"task_id": task_id, "to_state": to_state, "task": task_obj, "reason": reason})
        worker.o.move_task = fake_move
        worker.o.now_iso = lambda: "2026-04-24T01:00:00"
        worker.enqueue_braid_refine(
            {"task_id": "task-impl", "braid_template": "demo-template", "braid_template_hash": "sha256:abc"},
            "lvc-standard",
            "BRAID_REFINE: CheckBaseline: add baseline_red edge to End",
            from_state="running",
        )
    finally:
        worker.o.move_task = old_move
        worker.o.now_iso = old_now
        worker.o.new_task = old_new_task
        worker.o.enqueue_task = old_enqueue
    result = moved[0]
    blocker = orchestrator.task_blocker(result["task"])
    return {
        "to_state": result["to_state"],
        "blocker_code": (blocker or {}).get("code"),
        "summary": (blocker or {}).get("summary"),
        "refine_task_mode": ((enqueued[0].get("engine_args") or {}).get("mode")),
        "refine_task_hash": enqueued[0].get("braid_template_hash"),
        "request_hash": (((enqueued[0].get("engine_args") or {}).get("refine_request") or {}).get("template_hash")),
    }


def _run_template_refine_missing_hash_blocks(repo_root, scenario):
    orchestrator, worker = _load_repo_modules(repo_root)
    moved = []
    old_move = worker.o.move_task
    old_now = worker.o.now_iso
    try:
        def fake_move(task_id, from_state, to_state, reason="", mutator=None):
            task_obj = {"task_id": task_id, "state": from_state}
            if mutator:
                mutator(task_obj)
            moved.append({"task_id": task_id, "to_state": to_state, "task": task_obj, "reason": reason})
        worker.o.move_task = fake_move
        worker.o.now_iso = lambda: "2026-04-24T01:00:00"
        worker.enqueue_braid_refine(
            {"task_id": "task-impl"},
            "lvc-standard",
            "BRAID_REFINE: CheckBaseline: add baseline_red edge to End",
            from_state="running",
        )
    finally:
        worker.o.move_task = old_move
        worker.o.now_iso = old_now
    result = moved[0]
    blocker = orchestrator.task_blocker(result["task"])
    return {
        "to_state": result["to_state"],
        "blocker_code": (blocker or {}).get("code"),
        "summary": (blocker or {}).get("summary"),
        "retryable": bool((blocker or {}).get("retryable")),
    }


def _run_planner_json_parse_retryable_block(repo_root, scenario):
    orchestrator, worker = _load_repo_modules(repo_root)
    task = {
        "task_id": "task-plan",
        "project": "demo",
        "summary": "plan",
        "engine_args": {"roadmap_entry": {"id": "R-1", "title": "Title", "body": "Body"}},
    }
    moves = []
    old_worker = {
        "get_project": worker.o.get_project,
        "move_task": worker.o.move_task,
        "now_iso": worker.o.now_iso,
        "read_memory_context": worker.read_memory_context,
        "planner_council_prompt": worker.planner_council_prompt,
        "_run_bounded": worker._run_bounded,
        "_record_task_costs_from_text": worker._record_task_costs_from_text,
    }
    old_feature = {
        name: orchestrator.feature_finalize.__globals__[name]
        for name in (
            "shutil",
            "load_config",
            "list_features",
            "feature_workflow_summary",
            "_load_feature_children",
            "find_task",
            "update_feature",
            "append_transition",
            "append_event",
        )
    }
    try:
        worker.o.get_project = lambda cfg, project_name: {"name": project_name, "path": "/tmp/demo"}
        worker.o.now_iso = lambda: "2026-04-24T01:00:00"
        worker.read_memory_context = lambda *args, **kwargs: "memory"
        worker.planner_council_prompt = lambda *args, **kwargs: ("system", "user", ("aristotle",))
        worker._run_bounded = lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stdout='{"type":"result","result":"not json at all{"}')
        worker._record_task_costs_from_text = lambda *args, **kwargs: None
        def fake_move(task_id, from_state, to_state, reason="", mutator=None):
            task_obj = dict(task)
            task_obj["state"] = from_state
            if mutator:
                mutator(task_obj)
            moves.append({"task_id": task_id, "to_state": to_state, "task": task_obj, "reason": reason})
        worker.o.move_task = fake_move
        with tempfile.TemporaryDirectory() as tmp:
            log_path = pathlib.Path(tmp) / "planner.log"
            worker.run_claude_planner(dict(task), {"council": {}}, 30, log_path)
        blocked = moves[-1]
        blocker = orchestrator.task_blocker(blocked["task"])

        feature = {"feature_id": "feature-1", "status": "open", "project": "demo", "child_task_ids": []}
        orchestrator.feature_finalize.__globals__["shutil"] = types.SimpleNamespace(which=lambda _name: "/opt/homebrew/bin/gh")
        orchestrator.feature_finalize.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": "/tmp/demo"}]}
        orchestrator.feature_finalize.__globals__["list_features"] = lambda status="open": [dict(feature)] if status == "open" else []
        orchestrator.feature_finalize.__globals__["feature_workflow_summary"] = lambda _feature: {
            "has_live_work": False,
            "child_metadata_complete": True,
            "planner": {"task_id": "task-plan", "state": "blocked"},
        }
        orchestrator.feature_finalize.__globals__["_load_feature_children"] = lambda _feature: []
        orchestrator.feature_finalize.__globals__["find_task"] = lambda task_id, states=None: ("blocked", dict(blocked["task"])) if task_id == "task-plan" else None
        counts = {"updated": 0, "transitions": 0, "events": 0}
        orchestrator.feature_finalize.__globals__["update_feature"] = lambda *args, **kwargs: counts.__setitem__("updated", counts["updated"] + 1)
        orchestrator.feature_finalize.__globals__["append_transition"] = lambda *args, **kwargs: counts.__setitem__("transitions", counts["transitions"] + 1)
        orchestrator.feature_finalize.__globals__["append_event"] = lambda *args, **kwargs: counts.__setitem__("events", counts["events"] + 1)
        checked, opened, abandoned, skipped = orchestrator.feature_finalize()
    finally:
        worker.o.get_project = old_worker["get_project"]
        worker.o.move_task = old_worker["move_task"]
        worker.o.now_iso = old_worker["now_iso"]
        worker.read_memory_context = old_worker["read_memory_context"]
        worker.planner_council_prompt = old_worker["planner_council_prompt"]
        worker._run_bounded = old_worker["_run_bounded"]
        worker._record_task_costs_from_text = old_worker["_record_task_costs_from_text"]
        for key, value in old_feature.items():
            orchestrator.feature_finalize.__globals__[key] = value
    return {
        "final_state": blocked["to_state"],
        "blocker_code": blocker.get("code"),
        "retryable": bool(blocker.get("retryable")),
        "feature_finalize_abandoned": abandoned,
        "feature_finalize_skipped": skipped,
    }


def _run_planner_depends_on_format_error_blocks(repo_root, scenario):
    orchestrator, worker = _load_repo_modules(repo_root)
    task = {
        "task_id": "task-plan",
        "project": "lvc-standard",
        "summary": "plan",
        "attempt": 1,
        "engine_args": {"roadmap_entry": {"id": "R-2", "title": "Title", "body": "Body"}},
    }
    moves = []
    old = {
        "get_project": worker.o.get_project,
        "move_task": worker.o.move_task,
        "now_iso": worker.o.now_iso,
        "read_memory_context": worker.read_memory_context,
        "planner_council_prompt": worker.planner_council_prompt,
        "_run_bounded": worker._run_bounded,
        "_record_task_costs_from_text": worker._record_task_costs_from_text,
    }
    try:
        worker.o.get_project = lambda cfg, project_name: {"name": project_name, "path": "/tmp/demo"}
        worker.o.now_iso = lambda: "2026-04-24T01:00:00"
        worker.read_memory_context = lambda *args, **kwargs: "memory"
        worker.planner_council_prompt = lambda *args, **kwargs: ("system", "user", ("aristotle",))
        worker._run_bounded = lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stdout='{"type":"result","result":"{\\"execution_path\\":\\"slice-1\\",\\"slices\\":[{\\"id\\":\\"slice-1\\",\\"summary\\":\\"writer\\",\\"braid_template\\":\\"lvc-implement-operator\\",\\"depends_on\\":[42]}]}"}')
        worker._record_task_costs_from_text = lambda *args, **kwargs: None
        def fake_move(task_id, from_state, to_state, reason="", mutator=None):
            task_obj = dict(task)
            task_obj["state"] = from_state
            if mutator:
                mutator(task_obj)
            moves.append({"task_id": task_id, "to_state": to_state, "task": task_obj, "reason": reason})
        worker.o.move_task = fake_move
        with tempfile.TemporaryDirectory() as tmp:
            log_path = pathlib.Path(tmp) / "planner.log"
            worker.run_claude_planner(dict(task), {"council": {}}, 30, log_path)
        blocked = moves[-1]
        blocker = orchestrator.task_blocker(blocked["task"])
        prompt_task = dict(blocked["task"])
        prompt_task["attempt"] = 2
        prompt_task["attempt_history"] = [{"snapshot": {"blocker": {"code": "planner_slice_format_error", "detail": "lvc-implement-operator: depends_on index out of range: 42"}}}]
        prompt_task["engine_args"] = {"roadmap_entry": {"id": "R-2", "title": "Title", "body": "Body"}}
        _sys, prompt, _panel = old["planner_council_prompt"](prompt_task, {"name": "lvc-standard"}, "memory", {"council": {}})
    finally:
        for key, value in old.items():
            if key in {"get_project", "move_task", "now_iso"}:
                setattr(worker.o, key, value)
            else:
                setattr(worker, key, value)
    return {
        "final_state": blocked["to_state"],
        "blocker_code": blocker.get("code"),
        "retryable": bool(blocker.get("retryable")),
        "prompt_has_prior_errors": "[PRIOR_ERRORS]" in prompt and "depends_on index out of range: 42" in prompt,
    }


def _run_classify_slice_misroute_enqueues_router_clarify(repo_root, scenario):
    _orchestrator, worker = _load_repo_modules(repo_root)
    task = {
        "task_id": "task-plan",
        "project": "lvc-standard",
        "summary": "plan",
        "engine_args": {"roadmap_entry": {"id": "R-3", "title": "Title", "body": "Body"}},
        "feature_id": "feature-1",
    }
    enqueued = []
    moves = []
    old = {
        "get_project": worker.o.get_project,
        "move_task": worker.o.move_task,
        "new_task": worker.o.new_task,
        "enqueue_task": worker.o.enqueue_task,
        "append_feature_child": worker.o.append_feature_child,
        "now_iso": worker.o.now_iso,
        "read_memory_context": worker.read_memory_context,
        "planner_council_prompt": worker.planner_council_prompt,
        "_run_bounded": worker._run_bounded,
        "_record_task_costs_from_text": worker._record_task_costs_from_text,
    }
    try:
        worker.o.get_project = lambda cfg, project_name: {"name": project_name, "path": "/tmp/demo"}
        worker.o.now_iso = lambda: "2026-04-24T01:00:00"
        worker.read_memory_context = lambda *args, **kwargs: "memory"
        worker.planner_council_prompt = lambda *args, **kwargs: ("system", "user", ("aristotle",))
        worker._run_bounded = lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stdout='{"type":"result","result":"{\\"execution_path\\":\\"slice-1 -> slice-2\\",\\"slices\\":[{\\"id\\":\\"slice-1\\",\\"summary\\":\\"Build trade-research-platform UI dashboard card\\",\\"braid_template\\":\\"lvc-historian-update\\"},{\\"id\\":\\"slice-2\\",\\"summary\\":\\"Optimize hot path poller zero alloc jmh gate\\",\\"braid_template\\":\\"lvc-implement-operator\\"}]}"}')
        worker._record_task_costs_from_text = lambda *args, **kwargs: None
        worker.o.new_task = lambda **kwargs: {"task_id": f"task-{len(enqueued)+1}", **kwargs}
        worker.o.enqueue_task = lambda task_obj: enqueued.append(task_obj)
        worker.o.append_feature_child = lambda *args, **kwargs: None
        def fake_move(task_id, from_state, to_state, reason="", mutator=None):
            task_obj = dict(task)
            if mutator:
                mutator(task_obj)
            moves.append({"task_id": task_id, "to_state": to_state, "task": task_obj})
        worker.o.move_task = fake_move
        with tempfile.TemporaryDirectory() as tmp:
            worker.run_claude_planner(dict(task), {"council": {}}, 30, pathlib.Path(tmp) / "planner.log")
    finally:
        for key, value in old.items():
            if key in {"get_project", "move_task", "new_task", "enqueue_task", "append_feature_child", "now_iso"}:
                setattr(worker.o, key, value)
            else:
                setattr(worker, key, value)
    router = next(item for item in enqueued if str(item.get("source") or "").startswith("router-clarify-for:"))
    normal = next(item for item in enqueued if item.get("source") == "slice-of:task-plan")
    return {
        "router_mode": ((router.get("engine_args") or {}).get("mode")),
        "router_error": (((router.get("engine_args") or {}).get("router_clarify") or {}).get("classification_error")),
        "normal_template": normal.get("braid_template"),
        "planner_done": moves[-1]["to_state"] == "done",
    }


def _run_review_feedback_loop_detection(repo_root, scenario):
    _orchestrator, worker = _load_repo_modules(repo_root)
    calls = []
    class FakeO:
        STATES = ("queued", "claimed", "running", "blocked", "awaiting-review", "awaiting-qa", "done", "failed", "abandoned")
        def new_task(self, **kwargs):
            kind = (kwargs.get("engine_args") or {}).get("mode") or kwargs.get("braid_template")
            task_id = "task-planner-refine" if kind == "planner-refine" else "task-feedback"
            return {"task_id": task_id, **kwargs}
        def enqueue_task(self, task):
            calls.append(("enqueue", task.get("task_id"), (task.get("engine_args") or {}).get("mode"), task.get("summary")))
        def move_task(self, task_id, from_state, to_state, reason="", mutator=None):
            body = {
                "task_id": task_id,
                "review_feedback_rounds": 0,
                "review_feedback_signatures": [],
            }
            if mutator:
                mutator(body)
            calls.append(("move", task_id, from_state, to_state, reason, body.get("review_feedback_rounds"), list(body.get("review_feedback_signatures") or []), body.get("planner_refine_request")))
        def _write_pr_alert(self, *args):
            calls.append(("alert", args[3]))
        def update_task_in_place(self, path, mutator):
            body = {
                "task_id": "task-b",
                "review_feedback_rounds": 4,
                "review_feedback_signatures": [],
                "policy_review_findings": ["high-confidence secret pattern in diff: high-entropy token"],
            }
            mutator(body)
            calls.append(("update", body.get("review_feedback_rounds"), list(body.get("review_feedback_signatures") or [])))
        def task_path(self, task_id, state):
            return f"{state}/{task_id}.json"
        def now_iso(self):
            return "2026-04-25T06:00:00"
        def set_task_blocker(self, task, code, **kwargs):
            task["blocker"] = {"code": code, **kwargs}
        def read_feature(self, feature_id):
            return {"feature_id": feature_id, "summary": "Snapshot/restore API"}
        def find_task(self, task_id):
            if task_id == "task-plan":
                return ("done", {"task_id": "task-plan", "engine_args": {"roadmap_entry": {"id": "R-002", "title": "Snapshot/restore", "body": "body"}}})
            return None
        def iter_tasks(self, states=None, project=None):
            rows = [
                {
                    "task_id": "feedback-old",
                    "state": "done",
                    "source": "review-feedback:reviewer-old",
                    "engine_args": {"target_task_id": "task-b", "round": 3, "review_findings": "prior feedback: buffer pool release still missing"},
                },
                {
                    "task_id": "task-sibling",
                    "state": "queued",
                    "project": "demo",
                    "feature_id": "f2",
                    "parent_task_id": "task-plan",
                },
            ]
            if states == ("queued",) and project == "demo":
                return [rows[1]]
            return rows
        def remove_feature_children(self, feature_id, child_ids):
            calls.append(("remove_children", feature_id, list(child_ids)))
    old_o = worker.o
    worker.o = FakeO()
    try:
        findings = {"issue": "need docs"}
        sig = hashlib.sha256(json.dumps(findings, sort_keys=True, default=str).encode()).hexdigest()[:16]
        worker._handle_review_request_change(
            "reviewer-1",
            "demo",
            {"task_id": "task-a", "project": "demo", "feature_id": "f1", "base_branch": "main", "worktree": "/tmp/wt", "review_feedback_rounds": 1, "review_feedback_signatures": [sig]},
            findings,
            lambda t: t.update({"reviewed_by": "reviewer-1"}),
        )
        loop_planner = next(item for item in calls if item[0] == "enqueue" and item[2] == "planner-refine")
        loop_abandoned = next(item for item in calls if item[0] == "move" and item[1] == "task-a" and item[3] == "abandoned")
        loop_alert = next(item for item in calls if item[0] == "alert")
        loop_update = next(item for item in calls if item[0] == "update")
        calls.clear()
        worker._handle_review_request_change(
            "reviewer-2",
            "demo",
            {
                "task_id": "task-b",
                "project": "demo",
                "feature_id": "f2",
                "parent_task_id": "task-plan",
                "base_branch": "main",
                "worktree": "/tmp/wt",
                "review_feedback_rounds": 4,
                "review_feedback_signatures": [],
                "policy_review_findings": ["high-confidence secret pattern in diff: high-entropy token"],
                "engine_args": {"slice": {"id": "slice-1", "execution_path": "slice-1 -> slice-2"}},
            },
            {"issue": "need tests"},
            lambda t: t.update({"reviewed_by": "reviewer-2"}),
        )
        planner_task = next(item for item in calls if item[0] == "enqueue" and item[2] == "planner-refine")
        abandoned = next(item for item in calls if item[0] == "move" and item[1] == "task-b" and item[3] == "abandoned")
        removed = next(item for item in calls if item[0] == "remove_children")
    finally:
        worker.o = old_o
    return {
        "loop_enqueues_planner_refine": loop_planner[2] == "planner-refine",
        "loop_target_abandoned": loop_abandoned[3] == "abandoned",
        "loop_alert": loop_alert[1],
        "loop_rounds": loop_update[1],
        "exhausted_enqueues_planner_refine": planner_task[2] == "planner-refine",
        "exhausted_target_abandoned": abandoned[3] == "abandoned",
        "removed_children": removed[2],
    }


def _run_feature_abandon_cascades_children(repo_root, scenario):
    orchestrator, _worker = _load_repo_modules(repo_root)
    feature = {
        "feature_id": "feature-cascade",
        "status": "open",
        "project": "demo",
        "child_task_ids": ["task-queued", "task-claimed", "task-done"],
    }
    moved = []
    old = {
        name: orchestrator.feature_finalize.__globals__[name]
        for name in (
            "shutil", "load_config", "list_features", "feature_workflow_summary", "_load_feature_children",
            "_feature_all_children_failed_without_retry", "_feature_finalize_blocking_issue", "subprocess", "read_feature", "find_task", "move_task",
            "update_feature", "append_transition", "append_event",
        )
    }
    try:
        orchestrator.feature_finalize.__globals__["shutil"] = types.SimpleNamespace(which=lambda _name: "/opt/homebrew/bin/gh")
        orchestrator.feature_finalize.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": "/tmp/demo"}]}
        orchestrator.feature_finalize.__globals__["list_features"] = lambda status="open": [dict(feature)] if status == "open" else []
        orchestrator.feature_finalize.__globals__["feature_workflow_summary"] = lambda _feature: {"has_live_work": False, "child_metadata_complete": True, "planner": {}}
        orchestrator.feature_finalize.__globals__["_load_feature_children"] = lambda _feature: [
            {"task_id": "task-queued", "state": "failed"},
            {"task_id": "task-claimed", "state": "failed"},
            {"task_id": "task-done", "state": "failed"},
        ]
        orchestrator.feature_finalize.__globals__["_feature_all_children_failed_without_retry"] = lambda _feature: True
        orchestrator.feature_finalize.__globals__["_feature_finalize_blocking_issue"] = lambda _project_name: None
        orchestrator.feature_finalize.__globals__["subprocess"] = types.SimpleNamespace(run=lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stderr=""))
        orchestrator.feature_finalize.__globals__["read_feature"] = lambda feature_id: dict(feature)
        state_map = {"task-queued": ("queued", {"task_id": "task-queued"}), "task-claimed": ("claimed", {"task_id": "task-claimed"}), "task-done": ("done", {"task_id": "task-done"})}
        orchestrator.feature_finalize.__globals__["find_task"] = lambda task_id, states=None: state_map.get(task_id)
        orchestrator.feature_finalize.__globals__["move_task"] = lambda task_id, from_state, to_state, reason="", mutator=None: moved.append((task_id, from_state, to_state, reason))
        orchestrator.feature_finalize.__globals__["update_feature"] = lambda feature_id, mutator: mutator(feature)
        orchestrator.feature_finalize.__globals__["append_transition"] = lambda *args, **kwargs: None
        orchestrator.feature_finalize.__globals__["append_event"] = lambda *args, **kwargs: None
        checked, opened, abandoned, skipped = orchestrator.feature_finalize()
    finally:
        for key, value in old.items():
            orchestrator.feature_finalize.__globals__[key] = value
    return {
        "checked": checked,
        "abandoned": abandoned,
        "queued_child_state": next(item[2] for item in moved if item[0] == "task-queued"),
        "claimed_child_state": next(item[2] for item in moved if item[0] == "task-claimed"),
        "done_child_moved": any(item[0] == "task-done" for item in moved),
    }


def _run_review_feedback_blocks_on_inflight_target(repo_root, scenario):
    orchestrator, worker = _load_repo_modules(repo_root)
    moves = []
    old_state = worker._review_feedback_target_state
    old_move = worker.o.move_task
    old_now = worker.o.now_iso
    old_retry = orchestrator.reset_task_for_retry
    old_find = orchestrator.find_task
    try:
        worker._review_feedback_target_state = lambda target_id: ("inflight", "running", {"task_id": target_id})
        worker.o.now_iso = lambda: "2026-04-24T01:00:00"
        def fake_move(task_id, from_state, to_state, reason="", mutator=None):
            task_obj = {"task_id": task_id, "state": from_state}
            if mutator:
                mutator(task_obj)
            moves.append({"task_id": task_id, "to_state": to_state, "task": task_obj})
        worker.o.move_task = fake_move
        worker.run_review_feedback_task(
            {"task_id": "feedback-1", "project": "demo", "feature_id": "f1", "engine_args": {"target_task_id": "target-1", "round": 2, "review_findings": "x"}, "braid_template": "review-address-feedback"},
            {},
            30,
            pathlib.Path(tempfile.gettempdir()) / "feedback.log",
        )
        blocked = moves[0]
        blocker = orchestrator.task_blocker(blocked["task"])
        retried = {}
        orchestrator.reset_task_for_retry = lambda task_id, from_state, reason, source=None, mutator=None: retried.update({"task_id": task_id, "from_state": from_state, "reason": reason}) or {"task_id": task_id}
        orchestrator.find_task = lambda task_id, states=None: ("queued", {"task_id": task_id}) if task_id == "feedback-1" else None
        ok = orchestrator._workflow_check_retry_task({"task_id": "feedback-1"}, "blocked", "target stabilized")
    finally:
        worker._review_feedback_target_state = old_state
        worker.o.move_task = old_move
        worker.o.now_iso = old_now
        orchestrator.reset_task_for_retry = old_retry
        orchestrator.find_task = old_find
    return {
        "blocked_code": blocker.get("code"),
        "retryable": bool(blocker.get("retryable")),
        "workflow_retry_called": ok and retried.get("task_id") == "feedback-1",
    }


def _run_review_feedback_reaper_stale_target(repo_root, scenario):
    orchestrator, _worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old = {
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "now_iso": orchestrator.now_iso,
            "STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"),
        }
        os.environ["STATE_ENGINE_MODE"] = "off"
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.now_iso = lambda: "2026-04-25T10:40:00"
        try:
            feedback = orchestrator.new_task(
                role="implementer",
                engine="codex",
                project="lvc-standard",
                summary="Address reviewer findings",
                source="scenario",
                feature_id="feature-stale",
                braid_template="review-address-feedback",
            )
            feedback["task_id"] = "feedback-stale"
            feedback["state"] = "blocked"
            feedback["engine_args"] = {"target_task_id": "target-old"}
            orchestrator.set_task_blocker(
                feedback,
                "review_feedback_target_inflight",
                summary="target in-flight",
                detail="target state=running",
                source="scenario",
                retryable=True,
            )
            orchestrator.write_json_atomic(orchestrator.task_path("feedback-stale", "blocked"), feedback)
            target = orchestrator.new_task(
                role="implementer",
                engine="codex",
                project="lvc-standard",
                summary="Old target",
                source="scenario",
                feature_id="feature-stale",
            )
            target["task_id"] = "target-old"
            target["state"] = scenario.get("target_state", "abandoned")
            orchestrator.write_json_atomic(orchestrator.task_path("target-old", target["state"]), target)
            reaped = orchestrator.reap()
            found = orchestrator.find_task("feedback-stale")
        finally:
            orchestrator.QUEUE_ROOT = old["QUEUE_ROOT"]
            orchestrator.now_iso = old["now_iso"]
            if old["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old["STATE_ENGINE_MODE"]
    state, task = found
    return {
        "reaped": reaped,
        "feedback_state": state,
        "abandoned_reason": task.get("abandoned_reason"),
    }


def _run_atomic_claim_feature_race_requeues_newer(repo_root, scenario):
    orchestrator, _worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old = {
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "now_iso": orchestrator.now_iso,
            "project_environment_ok": orchestrator.project_environment_ok,
            "in_flight_feature_ids": orchestrator.in_flight_feature_ids,
            "load_braid_index": orchestrator.load_braid_index,
            "STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"),
        }
        os.environ["STATE_ENGINE_MODE"] = "off"
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.now_iso = lambda: "2026-04-25T10:41:00"
        orchestrator.project_environment_ok = lambda *args, **kwargs: True
        orchestrator.in_flight_feature_ids = lambda: set()
        orchestrator.load_braid_index = lambda: {}
        try:
            older = orchestrator.new_task(role="implementer", engine="codex", project="demo", summary="older", source="scenario", feature_id="feature-race")
            older["task_id"] = "task-older"
            older["state"] = "running"
            older["created_at"] = "2026-04-25T10:00:00"
            older["created_at_epoch"] = 1
            orchestrator.write_json_atomic(orchestrator.task_path("task-older", "running"), older)
            newer = orchestrator.new_task(role="implementer", engine="codex", project="demo", summary="newer", source="scenario", feature_id="feature-race")
            newer["task_id"] = "task-newer"
            newer["state"] = "queued"
            newer["created_at"] = "2026-04-25T10:00:01"
            newer["created_at_epoch"] = 2
            orchestrator.write_json_atomic(orchestrator.task_path("task-newer", "queued"), newer)
            claimed = orchestrator.atomic_claim("codex")
            found = orchestrator.find_task("task-newer")
        finally:
            orchestrator.QUEUE_ROOT = old["QUEUE_ROOT"]
            orchestrator.now_iso = old["now_iso"]
            orchestrator.project_environment_ok = old["project_environment_ok"]
            orchestrator.in_flight_feature_ids = old["in_flight_feature_ids"]
            orchestrator.load_braid_index = old["load_braid_index"]
            if old["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old["STATE_ENGINE_MODE"]
    return {
        "claimed_task": (claimed or {}).get("task_id"),
        "newer_state": found[0] if found else None,
    }


def _run_env_bypass_budget_triggers_self_repair(repo_root, scenario):
    orchestrator, _worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        task = {
            "task_id": "task-sr",
            "state": "queued",
            "engine": "claude",
            "role": "implementer",
            "project": "demo",
            "summary": "repair env",
            "source": "manual",
            "engine_args": {"self_repair": {"enabled": True}},
            "created_at": "2026-04-24T01:00:00",
        }
        (queue_root / "queued" / "task-sr.json").write_text(json.dumps(task))
        old = {
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "project_hard_stopped": orchestrator.project_hard_stopped,
            "project_environment_ok": orchestrator.project_environment_ok,
            "_task_allows_environment_bypass": orchestrator._task_allows_environment_bypass,
            "_state_engine_read_enabled": orchestrator._state_engine_read_enabled,
            "_record_task_bypass": orchestrator._record_task_bypass,
            "append_metric": orchestrator.append_metric,
            "env_bypass_rate_1h": orchestrator.env_bypass_rate_1h,
            "enqueue_self_repair": orchestrator.enqueue_self_repair,
        }
        calls = {"bypass": 0, "metric": 0, "repair": []}
        try:
            orchestrator.QUEUE_ROOT = queue_root
            orchestrator.project_hard_stopped = lambda project: False
            orchestrator.project_environment_ok = lambda project: False
            orchestrator._task_allows_environment_bypass = lambda task: True
            orchestrator._state_engine_read_enabled = lambda: False
            orchestrator._record_task_bypass = lambda *args, **kwargs: calls.__setitem__("bypass", calls["bypass"] + 1) or True
            orchestrator.append_metric = lambda *args, **kwargs: calls.__setitem__("metric", calls["metric"] + 1) or {}
            orchestrator.env_bypass_rate_1h = lambda project: 10
            orchestrator.enqueue_self_repair = lambda **kwargs: calls["repair"].append(kwargs) or {"enqueued": 1}
            claimed = orchestrator.atomic_claim("claude")
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
        return {
            "claimed_task_id": claimed.get("task_id"),
            "bypass_logged": calls["bypass"],
            "metric_logged": calls["metric"],
            "self_repair_issue_key": calls["repair"][0]["issue_key"],
        }


def _run_planner_refine_missing_origin_context(repo_root, scenario):
    _orchestrator, worker = _load_repo_modules(repo_root)
    failures = []
    old = {
        "get_project": worker.o.get_project,
        "move_task": worker.o.move_task,
        "now_iso": worker.o.now_iso,
        "read_memory_context": worker.read_memory_context,
        "fail_task": worker.fail_task,
    }
    try:
        worker.o.get_project = lambda cfg, project_name: {"name": project_name, "path": "/tmp/demo"}
        worker.o.move_task = lambda *args, **kwargs: None
        worker.o.now_iso = lambda: "2026-04-24T01:00:00"
        worker.read_memory_context = lambda *args, **kwargs: "memory"
        worker.fail_task = lambda task_id, from_state, reason, **kwargs: failures.append((kwargs.get("blocker_code"), kwargs.get("summary")))
        with tempfile.TemporaryDirectory() as tmp:
            worker.run_claude_planner(
                {"task_id": "task-plan", "project": "demo", "engine_args": {"mode": "planner-refine", "planner_refine": {}}},
                {"council": {}},
                30,
                pathlib.Path(tmp) / "planner.log",
            )
    finally:
        worker.o.get_project = old["get_project"]
        worker.o.move_task = old["move_task"]
        worker.o.now_iso = old["now_iso"]
        worker.read_memory_context = old["read_memory_context"]
        worker.fail_task = old["fail_task"]
    return {"blocker_code": failures[0][0], "summary": failures[0][1]}


def _run_slice_alias_custom_id_not_hijacked(repo_root, scenario):
    _orchestrator, worker = _load_repo_modules(repo_root)
    slice_ids, aliases = worker._slice_alias_maps([{"id": "my-custom-id", "summary": "x"}])
    return {
        "normalized_custom_id": worker._normalize_slice_ref("my-custom-id"),
        "canonical_first_id": slice_ids[0],
        "custom_alias_present": aliases.get("my-custom-id"),
    }


def _run_slice_context_block_handles_null_plan(repo_root, scenario):
    _orchestrator, worker = _load_repo_modules(repo_root)
    block = worker._render_slice_context_block({"engine_args": {"slice": {"id": "slice-1", "index": 1, "depends_on": [], "execution_path": "slice-1", "plan": None}}})
    return {
        "has_slice_id": "slice_id: slice-1" in block,
        "has_none_plan_fallback": "- (none)" in block,
    }


def _run_frontier_surfaces_blocked_dependency(repo_root, scenario):
    orchestrator, _worker = _load_repo_modules(repo_root)
    feature = {
        "feature_id": "feature-dep-blocked",
        "status": "open",
        "project": "lvc-standard",
        "summary": "dependency blocked",
        "child_task_ids": ["task-blocked", "task-dependent"],
    }
    tasks = {
        "task-blocked": (
            "blocked",
            {
                "task_id": "task-blocked",
                "state": "blocked",
                "project": "lvc-standard",
                "feature_id": "feature-dep-blocked",
                "role": "implementer",
                "engine": "codex",
                "depends_on": [],
                "braid_template": "lvc-implement-operator",
                "blocker": orchestrator.make_blocker(
                    "template_missing",
                    summary="BRAID template context unavailable",
                    detail="template exists; retry should be offered",
                    source="worker",
                    retryable=True,
                ),
            },
        ),
        "task-dependent": (
            "queued",
            {
                "task_id": "task-dependent",
                "state": "queued",
                "project": "lvc-standard",
                "feature_id": "feature-dep-blocked",
                "role": "implementer",
                "engine": "codex",
                "depends_on": ["task-blocked"],
            },
        ),
    }
    old = {
        "find_task": orchestrator.find_task,
        "task_state_entered_at": orchestrator.task_state_entered_at,
        "_latest_feature_planner_task": orchestrator._latest_feature_planner_task,
        "_feature_related_tasks": orchestrator._feature_related_tasks,
        "read_events": orchestrator.read_events,
        "BRAID_TEMPLATES": orchestrator.BRAID_TEMPLATES,
    }
    with tempfile.TemporaryDirectory() as tmp:
        try:
            template_dir = pathlib.Path(tmp) / "templates"
            template_dir.mkdir()
            (template_dir / "lvc-implement-operator.mmd").write_text("flowchart TD\nA[Start] --> B[End]\n")
            orchestrator.BRAID_TEMPLATES = template_dir
            orchestrator.find_task = lambda task_id, states=orchestrator.STATES: tasks.get(task_id)
            orchestrator.task_state_entered_at = lambda task_id, state: ("2026-04-25T06:00:00", "scenario")
            orchestrator._latest_feature_planner_task = lambda feature_id: None
            orchestrator._feature_related_tasks = lambda feature_id: [
                {"task_id": task_id, "state": state, "task": task}
                for task_id, (state, task) in tasks.items()
            ]
            orchestrator.read_events = lambda **kwargs: []
            workflow = orchestrator.feature_workflow_summary(feature)
            issue = orchestrator._workflow_issue_from_summary(
                feature,
                workflow,
                {"projects": [{"name": "lvc-standard", "path": "/tmp/lvc-standard"}]},
            )
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
    return {
        "frontier_task_id": workflow["frontier"]["task_id"],
        "frontier_state": workflow["frontier"]["state"],
        "frontier_blocker_code": (workflow["frontier"]["blocker"] or {}).get("code"),
        "issue_task_id": issue.get("task_id") if issue else None,
        "issue_action": issue.get("action") if issue else None,
        "issue_policy": issue.get("policy") if issue else None,
    }


def _run_workflow_e2e_story(repo_root, scenario):
    orchestrator, worker = _load_repo_modules(repo_root)
    story = scenario["story"]

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        queue_root = root / "queue"
        features_dir = root / "features"
        runtime_dir = root / "runtime"
        reports_dir = root / "reports"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        features_dir.mkdir(parents=True, exist_ok=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        reports_dir.mkdir(parents=True, exist_ok=True)
        templates_dir = root / "templates"
        templates_dir.mkdir()
        (templates_dir / "lvc-implement-operator.mmd").write_text("flowchart TD\nStart[Start] --> End[End]\n")
        (templates_dir / "review-address-feedback.mmd").write_text("flowchart TD\nStart[Start] --> End[End]\n")

        old_env = {"STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE")}
        old = {
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
            "RUNTIME_DIR": orchestrator.RUNTIME_DIR,
            "TRANSITIONS_LOG": orchestrator.TRANSITIONS_LOG,
            "EVENTS_LOG": orchestrator.EVENTS_LOG,
            "METRICS_LOG": orchestrator.METRICS_LOG,
            "REPORT_DIR": orchestrator.REPORT_DIR,
            "PROJECT_HARD_STOPS_PATH": orchestrator.PROJECT_HARD_STOPS_PATH,
            "SLOT_PAUSE_DIR": orchestrator.SLOT_PAUSE_DIR,
            "BRAID_TEMPLATES": orchestrator.BRAID_TEMPLATES,
            "load_config": orchestrator.load_config,
            "emit_runtime_metrics_snapshot": orchestrator.emit_runtime_metrics_snapshot,
            "write_agent_status": orchestrator.write_agent_status,
            "reap": orchestrator.reap,
            "environment_health": orchestrator.environment_health,
            "project_environment_ok": orchestrator.project_environment_ok,
            "project_hard_stopped": orchestrator.project_hard_stopped,
            "_nudge_engine_workers": orchestrator._nudge_engine_workers,
            "_write_pr_alert": orchestrator._write_pr_alert,
            "_write_workflow_check_report": orchestrator._write_workflow_check_report,
            "tick_self_repair_resolution": orchestrator.tick_self_repair_resolution,
            "tick_self_repair_queue": orchestrator.tick_self_repair_queue,
            "_workflow_check_restart_workers": orchestrator._workflow_check_restart_workers,
        }
        old_worker_o = worker.o
        alerts = []
        os.environ["STATE_ENGINE_MODE"] = "off"
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.FEATURES_DIR = features_dir
        orchestrator.RUNTIME_DIR = runtime_dir
        orchestrator.TRANSITIONS_LOG = runtime_dir / "transitions.log"
        orchestrator.EVENTS_LOG = runtime_dir / "events.jsonl"
        orchestrator.METRICS_LOG = runtime_dir / "metrics.jsonl"
        orchestrator.REPORT_DIR = reports_dir
        orchestrator.PROJECT_HARD_STOPS_PATH = runtime_dir / "project-hard-stops.json"
        orchestrator.SLOT_PAUSE_DIR = runtime_dir / "slot-paused"
        orchestrator.BRAID_TEMPLATES = templates_dir
        orchestrator.load_config = lambda: {
            "workflow_check_max_attempts": int(scenario.get("max_attempts", 3)),
            "projects": [
                {"name": "lvc-standard", "path": str(root / "lvc")},
                {"name": "devmini-orchestrator", "path": str(root / "orchestrator")},
            ],
            "synthetic_canary": {"enabled": False},
        }
        orchestrator.emit_runtime_metrics_snapshot = lambda **kwargs: None
        orchestrator.write_agent_status = lambda *args, **kwargs: None
        orchestrator.reap = lambda: 0
        orchestrator.environment_health = lambda refresh=False: {"ok": True, "issues": []}
        orchestrator.project_environment_ok = lambda project_name, refresh=False: True
        orchestrator.project_hard_stopped = lambda project_name: False
        orchestrator._nudge_engine_workers = lambda engine: []
        orchestrator._write_pr_alert = lambda *args, **kwargs: alerts.append(args) or "alert.md"
        orchestrator._write_workflow_check_report = lambda issues, reaped=0: None
        orchestrator.tick_self_repair_resolution = lambda: {"resolved": 0, "stalled": 0}
        orchestrator.tick_self_repair_queue = lambda: {"scheduled": 0}
        orchestrator._workflow_check_restart_workers = lambda: (["worker.codex"], [])
        worker.o = orchestrator

        def write_feature(feature):
            orchestrator.write_json_atomic(features_dir / f"{feature['feature_id']}.json", feature)

        def read_feature():
            return orchestrator.read_json(features_dir / "feature-e2e.json", {})

        def set_feature_status(status):
            orchestrator.update_feature("feature-e2e", lambda f: f.update({"status": status, "finished_at": orchestrator.now_iso()}))

        def add_task(task_id, state, *, role="implementer", engine="codex", source="scenario", depends_on=None, blocker=None, summary=None, parent_task_id=None, braid_template="lvc-implement-operator", engine_args=None):
            task = orchestrator.new_task(
                role=role,
                engine=engine,
                project="lvc-standard",
                summary=summary or task_id,
                source=source,
                feature_id="feature-e2e",
                parent_task_id=parent_task_id,
                braid_template=braid_template,
                engine_args=engine_args or {},
                depends_on=depends_on or [],
            )
            task["task_id"] = task_id
            task["state"] = state
            task["created_at"] = f"2026-04-25T06:{len(list(orchestrator.iter_tasks(states=orchestrator.STATES))) % 60:02d}:00"
            if blocker:
                task["blocker"] = blocker
            orchestrator.write_json_atomic(orchestrator.task_path(task_id, state), task)
            return task

        def move(task_id, from_state, to_state, reason):
            orchestrator.move_task(task_id, from_state, to_state, reason=reason)

        def snapshot(label):
            feature = read_feature()
            workflow = orchestrator.feature_workflow_summary(feature)
            issue = orchestrator._workflow_issue_from_summary(feature, workflow, orchestrator.load_config()) if feature else None
            return {
                "label": label,
                "frontier": (workflow.get("frontier") or {}).get("task_id"),
                "state": (workflow.get("frontier") or {}).get("state"),
                "issue_action": issue.get("action") if issue else None,
                "issue_policy": issue.get("policy") if issue else None,
            }

        def issue_snapshot(label):
            feature = read_feature()
            workflow = orchestrator.feature_workflow_summary(feature)
            issue = orchestrator._workflow_issue_from_summary(feature, workflow, orchestrator.load_config()) if feature else None
            blocker = (issue or {}).get("blocker") or {}
            return {
                "label": label,
                "kind": (issue or {}).get("kind"),
                "task_id": (issue or {}).get("task_id"),
                "task_state": (issue or {}).get("task_state"),
                "blocker_code": blocker.get("code"),
                "action": (issue or {}).get("action"),
                "policy": (issue or {}).get("policy"),
            }

        def policy_snapshot(label, task_id, state, project_name="lvc-standard"):
            task = orchestrator.find_task(task_id)[1]
            issue = {
                "kind": "frontier_task_blocked",
                "task_state": state,
                "blocker": orchestrator.task_blocker(task),
            }
            project = orchestrator.get_project(orchestrator.load_config(), project_name)
            action, diagnosis, policy = orchestrator._workflow_policy_decision(issue, task, project)
            return {
                "label": label,
                "task_id": task_id,
                "blocker_code": (issue["blocker"] or {}).get("code"),
                "action": action,
                "policy": policy,
                "diagnosis": bool(diagnosis),
            }

        try:
            feature = {
                "feature_id": "feature-e2e",
                "status": "open",
                "project": "lvc-standard",
                "summary": f"e2e {story}",
                "child_task_ids": [],
                "created_at": "2026-04-25T06:00:00",
            }
            write_feature(feature)
            add_task("task-plan", "done", role="planner", engine="claude", source="tick-planner", braid_template=None)

            snapshots = []
            if story == "happy_path_finalizes":
                add_task("task-1", "queued", summary="slice one")
                add_task("task-2", "queued", summary="slice two", depends_on=["task-1"])
                orchestrator.append_feature_child("feature-e2e", "task-1")
                orchestrator.append_feature_child("feature-e2e", "task-2")
                snapshots.append(snapshot("initial"))
                move("task-1", "queued", "running", "worker claimed")
                move("task-1", "running", "done", "implemented")
                snapshots.append(snapshot("after-slice-1"))
                move("task-2", "queued", "running", "dependency satisfied")
                move("task-2", "running", "awaiting-review", "ready for review")
                move("task-2", "awaiting-review", "awaiting-qa", "review approved")
                move("task-2", "awaiting-qa", "done", "qa passed")
                set_feature_status("done")

            elif story == "review_retry_then_approve":
                target = add_task("task-1", "awaiting-review", summary="needs review", engine_args={"slice": {"id": "slice-1"}})
                orchestrator.append_feature_child("feature-e2e", "task-1")
                worker._handle_review_request_change(
                    "reviewer-1",
                    "lvc-standard",
                    target,
                    "missing assertion",
                    lambda t: t.update({"reviewed_by": "reviewer-1"}),
                )
                feedback = next(t for t in orchestrator.iter_tasks(states=("queued",)) if str(t.get("source") or "").startswith("review-feedback:"))
                snapshots.append(snapshot("feedback-enqueued"))
                move(feedback["task_id"], "queued", "running", "feedback claimed")
                move(feedback["task_id"], "running", "done", "feedback applied")
                move("task-1", "queued", "awaiting-review", "feedback returned")
                move("task-1", "awaiting-review", "awaiting-qa", "review approved")
                move("task-1", "awaiting-qa", "done", "qa passed")
                set_feature_status("done")

            elif story == "review_exhaustion_replans":
                target = add_task(
                    "task-old-1",
                    "awaiting-review",
                    summary="old slice",
                    engine_args={"slice": {"id": "slice-1", "execution_path": "slice-1 -> slice-2"}},
                )
                target["parent_task_id"] = "task-plan"
                target["review_feedback_rounds"] = 4
                orchestrator.write_json_atomic(orchestrator.task_path("task-old-1", "awaiting-review"), target)
                add_task("task-old-2", "queued", summary="old dependent", depends_on=["task-old-1"], parent_task_id="task-plan")
                orchestrator.append_feature_child("feature-e2e", "task-old-1")
                orchestrator.append_feature_child("feature-e2e", "task-old-2")
                worker._handle_review_request_change(
                    "reviewer-1",
                    "lvc-standard",
                    target,
                    "still wrong",
                    lambda t: t.update({"reviewed_by": "reviewer-1"}),
                )
                planner = next(t for t in orchestrator.iter_tasks(states=("queued",), engine="claude") if (t.get("engine_args") or {}).get("mode") == "planner-refine")
                move(planner["task_id"], "queued", "done", "planner refined")
                orchestrator.remove_feature_children("feature-e2e", ["task-old-1", "task-old-2"])
                add_task("task-new-1", "queued", summary="replacement slice", parent_task_id=planner["task_id"])
                orchestrator.append_feature_child("feature-e2e", "task-new-1")
                move("task-new-1", "queued", "running", "replacement claimed")
                move("task-new-1", "running", "done", "replacement complete")
                orchestrator.update_feature("feature-e2e", lambda f: f.update({"child_task_ids": ["task-new-1"]}))
                set_feature_status("done")

            elif story == "template_refine_retry":
                blocker = orchestrator.make_blocker(
                    "template_missing",
                    summary="BRAID template context unavailable",
                    detail="template now exists",
                    source="worker",
                    retryable=True,
                )
                add_task("task-1", "blocked", summary="template blocked", blocker=blocker)
                orchestrator.append_feature_child("feature-e2e", "task-1")
                snapshots.append(snapshot("blocked"))
                out = orchestrator.tick_workflow_check()
                snapshots.append({"label": "workflow-check", "issues": out.get("issues")})
                move("task-1", "queued", "running", "retry claimed")
                move("task-1", "running", "done", "template retry complete")
                set_feature_status("done")

            elif story == "blocked_dependency_unblocks":
                blocker = orchestrator.make_blocker(
                    "template_missing",
                    summary="BRAID template context unavailable",
                    detail="template now exists",
                    source="worker",
                    retryable=True,
                )
                add_task("task-1", "blocked", summary="blocked upstream", blocker=blocker)
                add_task("task-2", "queued", summary="dependent", depends_on=["task-1"])
                orchestrator.append_feature_child("feature-e2e", "task-1")
                orchestrator.append_feature_child("feature-e2e", "task-2")
                snapshots.append(snapshot("blocked-dependency"))
                orchestrator.tick_workflow_check()
                move("task-1", "queued", "running", "retry claimed")
                move("task-1", "running", "done", "upstream done")
                snapshots.append(snapshot("after-upstream"))
                move("task-2", "queued", "running", "dependent claimed")
                move("task-2", "running", "done", "dependent done")
                set_feature_status("done")

            elif story == "qa_fail_retry_then_pass":
                blocker = orchestrator.make_blocker(
                    "qa_smoke_failed",
                    summary="QA smoke failed",
                    detail="first pass failed",
                    source="qa",
                    retryable=True,
                )
                add_task("task-1", "blocked", role="qa", engine="qa", summary="qa blocked", blocker=blocker, braid_template=None)
                orchestrator.append_feature_child("feature-e2e", "task-1")
                snapshots.append(snapshot("qa-blocked"))
                orchestrator.tick_workflow_check()
                move("task-1", "queued", "running", "qa retry claimed")
                move("task-1", "running", "done", "qa passed")
                set_feature_status("done")

            elif story == "deadend_sweep":
                blockers = [
                    ("task-template", "template_missing", "lvc-implement-operator"),
                    ("task-worker", "worker_crash", "lvc-implement-operator"),
                    ("task-model", "model_output_invalid", "lvc-implement-operator"),
                    ("task-qa", "qa_smoke_failed", None),
                ]
                for task_id, code, template in blockers:
                    add_task(
                        task_id,
                        "blocked",
                        role="qa" if code == "qa_smoke_failed" else "implementer",
                        engine="qa" if code == "qa_smoke_failed" else "codex",
                        summary=f"blocked {code}",
                        blocker=orchestrator.make_blocker(code, summary=code, detail=code, source="scenario", retryable=True),
                        braid_template=template,
                    )
                    orchestrator.append_feature_child("feature-e2e", task_id)
                checked = []
                for task_id, _, _ in blockers:
                    task = orchestrator.find_task(task_id)[1]
                    issue = {
                        "kind": "frontier_task_blocked",
                        "task_state": "blocked",
                        "blocker": orchestrator.task_blocker(task),
                    }
                    action, diagnosis, policy = orchestrator._workflow_policy_decision(issue, task, {"name": "lvc-standard", "path": str(root / "lvc")})
                    checked.append({"task_id": task_id, "action": action, "policy": policy, "diagnosis": bool(diagnosis)})
                return {
                    "story": story,
                    "checked": checked,
                    "all_have_action": all(item["action"] for item in checked),
                    "silent_deadends": [item["task_id"] for item in checked if not item["action"]],
                }
            elif story == "planner_invalid_output_recovers":
                blocker = orchestrator.make_blocker(
                    "planner_slice_format_error",
                    summary="planner emitted invalid slices",
                    detail="slice depends_on references missing alias; retry with prior parser error",
                    source="planner",
                    retryable=True,
                )
                planner = add_task(
                    "task-plan-bad",
                    "blocked",
                    role="planner",
                    engine="claude",
                    source="tick-planner",
                    blocker=blocker,
                    braid_template=None,
                )
                orchestrator.update_feature("feature-e2e", lambda f: f.update({"planner_task_id": planner["task_id"]}))
                snapshots.append(issue_snapshot("planner-blocked"))
                orchestrator.tick_workflow_check()
                snapshots.append(issue_snapshot("planner-requeued"))
                move("task-plan-bad", "queued", "done", "planner retry emitted valid slices")
                add_task("task-1", "queued", summary="valid replacement slice", parent_task_id="task-plan-bad")
                orchestrator.append_feature_child("feature-e2e", "task-1")
                snapshots.append(snapshot("valid-frontier"))
                move("task-1", "queued", "running", "claimed")
                move("task-1", "running", "done", "implemented")
                set_feature_status("done")

            elif story == "reviewer_protocol_error_recovers":
                blocker = orchestrator.make_blocker(
                    "review_gate_protocol_error",
                    summary="review gate protocol failed",
                    detail="reviewer output omitted BRAID_OK trailer",
                    source="reviewer",
                    retryable=True,
                )
                add_task("task-1", "awaiting-review", summary="review protocol error", blocker=blocker)
                orchestrator.append_feature_child("feature-e2e", "task-1")
                snapshots.append(issue_snapshot("review-protocol-error"))
                orchestrator.tick_workflow_check()
                snapshots.append(issue_snapshot("after-reviewer-sweep"))
                move("task-1", "awaiting-review", "awaiting-qa", "review approved")
                move("task-1", "awaiting-qa", "done", "qa passed")
                set_feature_status("done")

            elif story == "qa_target_missing_stale_lane_clears":
                add_task("task-target", "done", summary="superseded target")
                stale = add_task(
                    "task-feedback-old",
                    "blocked",
                    summary="old feedback lane",
                    source="review-feedback:task-target",
                    parent_task_id="task-target",
                    engine_args={"target_task_id": "task-target"},
                    blocker=orchestrator.make_blocker(
                        "qa_target_missing",
                        summary="review-feedback target missing",
                        detail="review-feedback: target task-target missing",
                        source="worker",
                        retryable=True,
                    ),
                )
                stale["created_at"] = "2026-04-25T06:01:00"
                orchestrator.write_json_atomic(orchestrator.task_path("task-feedback-old", "blocked"), stale)
                newer = add_task(
                    "task-feedback-new",
                    "queued",
                    summary="newer feedback lane",
                    source="review-feedback:task-target",
                    parent_task_id="task-target",
                    engine_args={"target_task_id": "task-target"},
                )
                newer["created_at"] = "2026-04-25T06:02:00"
                orchestrator.write_json_atomic(orchestrator.task_path("task-feedback-new", "queued"), newer)
                orchestrator.append_feature_child("feature-e2e", "task-target")
                orchestrator.append_feature_child("feature-e2e", "task-feedback-old")
                snapshots.append(policy_snapshot("stale-feedback", "task-feedback-old", "blocked"))
                orchestrator.tick_workflow_check()
                found_old = orchestrator.find_task("task-feedback-old")
                found_new = orchestrator.find_task("task-feedback-new")
                move("task-feedback-new", "queued", "running", "new feedback claimed")
                move("task-feedback-new", "running", "done", "new feedback done")
                set_feature_status("done")
                return {
                    "story": story,
                    "stale_state": found_old[0] if found_old else None,
                    "newer_state": found_new[0] if found_new else None,
                    "snapshots": snapshots,
                    "final_status": read_feature().get("status"),
                }

            elif story == "template_refine_exhaustion_replans":
                task = add_task(
                    "task-1",
                    "blocked",
                    summary="template refine exhausted",
                    blocker=orchestrator.make_blocker(
                        "template_refine_exhausted",
                        summary="template refinement exhausted",
                        detail="graph still missing CheckRestore edge",
                        source="worker",
                        retryable=True,
                    ),
                )
                _, current_hash = orchestrator.braid_template_load("lvc-implement-operator")
                task["braid_template_hash"] = current_hash
                orchestrator.write_json_atomic(orchestrator.task_path("task-1", "blocked"), task)
                orchestrator.append_feature_child("feature-e2e", "task-1")
                snapshots.append(issue_snapshot("template-still-broken"))
                (templates_dir / "lvc-implement-operator.mmd").write_text("flowchart TD\nStart --> CheckRestore --> End\n")
                snapshots.append(issue_snapshot("template-changed"))
                orchestrator.tick_workflow_check()
                snapshots.append(snapshot("after-retry"))
                move("task-1", "queued", "running", "retry claimed")
                move("task-1", "running", "done", "retry complete")
                set_feature_status("done")

            elif story == "feature_no_children_recovers":
                planner = add_task(
                    "task-plan-empty",
                    "done",
                    role="planner",
                    engine="claude",
                    source="tick-planner",
                    braid_template=None,
                )
                log_path = root / "planner-empty.log"
                log_path.write_text("decomposed into 0 runnable slices\n")
                planner["log_path"] = str(log_path)
                orchestrator.write_json_atomic(orchestrator.task_path("task-plan-empty", "done"), planner)
                orchestrator.update_feature("feature-e2e", lambda f: f.update({"planner_task_id": planner["task_id"]}))
                snapshots.append(issue_snapshot("planner-empty"))
                orchestrator.tick_workflow_check()
                snapshots.append(issue_snapshot("planner-requeued"))
                move("task-plan-empty", "queued", "done", "planner emitted children")
                add_task("task-1", "queued", summary="replacement after empty planner", parent_task_id="task-plan-empty")
                orchestrator.append_feature_child("feature-e2e", "task-1")
                move("task-1", "queued", "running", "claimed")
                move("task-1", "running", "done", "implemented")
                set_feature_status("done")

            elif story == "missing_child_reconstructs_continues":
                orchestrator.update_feature("feature-e2e", lambda f: f.update({"child_task_ids": ["task-missing"]}))
                snapshots.append(issue_snapshot("missing-child"))
                old_recover = orchestrator._recover_missing_child_task
                def recover_missing_child(task_id):
                    add_task(task_id, "queued", summary="reconstructed child")
                    return {"recovered": True, "state": "queued"}
                orchestrator._recover_missing_child_task = recover_missing_child
                try:
                    orchestrator.tick_workflow_check()
                finally:
                    orchestrator._recover_missing_child_task = old_recover
                snapshots.append(snapshot("reconstructed"))
                move("task-missing", "queued", "running", "claimed")
                move("task-missing", "running", "done", "done")
                set_feature_status("done")

            elif story == "canonical_dirty_root_cause_loop":
                blocker = orchestrator.make_blocker(
                    "project_main_dirty",
                    summary="project main checkout dirty",
                    detail="canonical checkout has uncommitted orchestrator.py changes",
                    source="worker",
                    retryable=True,
                )
                task = add_task("task-1", "blocked", summary="dirty main", blocker=blocker)
                orchestrator.append_feature_child("feature-e2e", "task-1")
                repair_calls = {"count": 0}
                old_repair = orchestrator._repair_project_main_checkout
                old_hard_stop = orchestrator.set_project_hard_stop
                hard_stops = []
                orchestrator._repair_project_main_checkout = lambda project: repair_calls.update({"count": repair_calls["count"] + 1}) or {"fixed": False, "detail": "still dirty: modified bin/orchestrator.py"}
                orchestrator.set_project_hard_stop = lambda project, code, detail: hard_stops.append({"project": project, "code": code, "detail": detail})
                try:
                    for _ in range(4):
                        orchestrator.tick_workflow_check()
                finally:
                    orchestrator._repair_project_main_checkout = old_repair
                    orchestrator.set_project_hard_stop = old_hard_stop
                final = orchestrator.find_task("task-1")
                return {
                    "story": story,
                    "repair_calls": repair_calls["count"],
                    "hard_stops": hard_stops,
                    "final_state": final[0] if final else None,
                    "attempts": (read_feature().get("workflow_check") or {}).get("attempts") or {},
                }

            elif story == "runtime_env_soft_vs_hard":
                env_calls = {"repair": 0, "self_repair": 0}
                old_env_issues = orchestrator._environment_health_issues
                old_repair_env = orchestrator.repair_environment
                old_enqueue = orchestrator.enqueue_self_repair
                orchestrator._environment_health_issues = lambda: [
                    {
                        "feature_id": "env:global",
                        "project": "global",
                        "summary": "required binary missing: gh",
                        "issue_key": "env:global:delivery_auth_expired:required binary missing: gh",
                        "kind": "environment_degraded",
                        "task_state": "idle",
                        "blocker": orchestrator.make_blocker("delivery_auth_expired", summary="required binary missing: gh", detail="install gh", source="env", retryable=False),
                        "action": "repair_environment",
                    }
                ]
                orchestrator.repair_environment = lambda: env_calls.update({"repair": env_calls["repair"] + 1}) or {"attempted": 1, "remaining_error_count": 1}
                orchestrator.enqueue_self_repair = lambda **kwargs: env_calls.update({"self_repair": env_calls["self_repair"] + 1}) or {"enqueued": 1, "feature_id": "feature-env", "task_id": "task-env"}
                add_task("task-1", "queued", summary="normal project work")
                orchestrator.append_feature_child("feature-e2e", "task-1")
                try:
                    out = orchestrator.tick_workflow_check()
                finally:
                    orchestrator._environment_health_issues = old_env_issues
                    orchestrator.repair_environment = old_repair_env
                    orchestrator.enqueue_self_repair = old_enqueue
                move("task-1", "queued", "running", "claimed despite env issue")
                move("task-1", "running", "done", "done")
                set_feature_status("done")
                return {
                    "story": story,
                    "issues": out.get("issues"),
                    "repair_calls": env_calls["repair"],
                    "self_repair_calls": env_calls["self_repair"],
                    "project_task_final": orchestrator.find_task("task-1")[0],
                    "final_status": read_feature().get("status"),
                }

            elif story == "regression_hard_stop_auto_clears":
                blocker = orchestrator.make_blocker(
                    "project_regression_failed",
                    summary="regression failed",
                    detail="stale regression failure",
                    source="qa",
                    retryable=True,
                )
                task = add_task("task-regression", "blocked", role="qa", engine="qa", summary="stale regression", blocker=blocker, braid_template=None)
                task["finished_at"] = "2026-04-25T01:00:00"
                orchestrator.write_json_atomic(orchestrator.task_path("task-regression", "blocked"), task)
                orchestrator.append_feature_child("feature-e2e", "task-regression")
                old_green = orchestrator._project_green_regression_after
                old_human = orchestrator._project_human_push_after
                old_clear = orchestrator.clear_project_hard_stop
                cleared = []
                orchestrator._project_green_regression_after = lambda project, failed_at: True
                orchestrator._project_human_push_after = lambda project, failed_at: False
                orchestrator.clear_project_hard_stop = lambda project, **kwargs: cleared.append({"project": project, "cleared_by": kwargs.get("cleared_by")})
                try:
                    snapshots.append(issue_snapshot("regression-blocked"))
                    orchestrator.tick_workflow_check()
                finally:
                    orchestrator._project_green_regression_after = old_green
                    orchestrator._project_human_push_after = old_human
                    orchestrator.clear_project_hard_stop = old_clear
                return {
                    "story": story,
                    "cleared": cleared,
                    "regression_state": orchestrator.find_task("task-regression")[0],
                    "snapshots": snapshots,
                }

            elif story == "false_blocker_challenge_path":
                add_task(
                    "task-1",
                    "blocked",
                    summary="false blocker",
                    source="review-feedback:task-target",
                    parent_task_id="task-target",
                    engine_args={"target_task_id": "task-target"},
                    blocker=orchestrator.make_blocker(
                        "false_blocker_claim",
                        summary="false blocker claim",
                        detail="unresolved_review_threads_requires_github_thread_resolution_not_repo_changes",
                        source="worker",
                        retryable=True,
                    ),
                )
                orchestrator.append_feature_child("feature-e2e", "task-1")
                enqueued = []
                old_enqueue = orchestrator.enqueue_self_repair
                orchestrator.enqueue_self_repair = lambda **kwargs: enqueued.append(kwargs) or {"enqueued": 1, "feature_id": "feature-sr", "task_id": "task-sr"}
                try:
                    snapshots.append(issue_snapshot("false-blocker"))
                    orchestrator.tick_workflow_check()
                finally:
                    orchestrator.enqueue_self_repair = old_enqueue
                return {
                    "story": story,
                    "self_repair_enqueued": len(enqueued),
                    "issue_kind": enqueued[0].get("issue_kind") if enqueued else None,
                    "snapshots": snapshots,
                }
            elif story == "model_output_invalid_retries":
                add_task(
                    "task-1",
                    "failed",
                    summary="malformed model output",
                    blocker=orchestrator.make_blocker(
                        "model_output_invalid",
                        summary="model output invalid",
                        detail="no BRAID trailer",
                        source="worker",
                        retryable=False,
                    ),
                )
                orchestrator.append_feature_child("feature-e2e", "task-1")
                snapshots.append(issue_snapshot("model-output-invalid"))
                orchestrator.tick_workflow_check()
                snapshots.append(snapshot("after-retry"))
                move("task-1", "queued", "running", "retry claimed with archived output error")
                move("task-1", "running", "done", "valid output")
                set_feature_status("done")

            elif story == "qa_target_missing_self_repairs":
                add_task(
                    "task-feedback",
                    "blocked",
                    summary="missing target feedback lane",
                    source="review-feedback:task-missing",
                    parent_task_id="task-missing",
                    engine_args={"target_task_id": "task-missing"},
                    blocker=orchestrator.make_blocker(
                        "qa_target_missing",
                        summary="review-feedback target missing",
                        detail="review-feedback: target task-missing missing from queue",
                        source="worker",
                        retryable=False,
                    ),
                )
                orchestrator.append_feature_child("feature-e2e", "task-feedback")
                enqueued = []
                old_enqueue = orchestrator.enqueue_self_repair
                orchestrator.enqueue_self_repair = lambda **kwargs: enqueued.append(kwargs) or {"enqueued": 1, "feature_id": "feature-sr", "task_id": "task-sr"}
                try:
                    snapshots.append(issue_snapshot("qa-target-missing"))
                    orchestrator.tick_workflow_check()
                finally:
                    orchestrator.enqueue_self_repair = old_enqueue
                return {
                    "story": story,
                    "self_repair_enqueued": len(enqueued),
                    "policy": snapshots[0].get("policy"),
                    "action": snapshots[0].get("action"),
                    "issue_kind": enqueued[0].get("issue_kind") if enqueued else None,
                }

            elif story == "template_missing_self_repairs":
                add_task(
                    "task-1",
                    "blocked",
                    summary="missing template",
                    braid_template="missing-template",
                    blocker=orchestrator.make_blocker(
                        "template_missing",
                        summary="BRAID template missing",
                        detail="missing-template",
                        source="worker",
                        retryable=False,
                    ),
                )
                orchestrator.append_feature_child("feature-e2e", "task-1")
                enqueued = []
                old_enqueue = orchestrator.enqueue_self_repair
                orchestrator.enqueue_self_repair = lambda **kwargs: enqueued.append(kwargs) or {"enqueued": 1, "feature_id": "feature-sr", "task_id": "task-sr"}
                try:
                    snapshots.append(issue_snapshot("template-missing"))
                    orchestrator.tick_workflow_check()
                finally:
                    orchestrator.enqueue_self_repair = old_enqueue
                return {
                    "story": story,
                    "self_repair_enqueued": len(enqueued),
                    "policy": snapshots[0].get("policy"),
                    "action": snapshots[0].get("action"),
                    "issue_kind": enqueued[0].get("issue_kind") if enqueued else None,
                }

            elif story == "false_blocker_generic_retries":
                add_task(
                    "task-1",
                    "failed",
                    summary="generic false blocker",
                    source="scenario",
                    blocker=orchestrator.make_blocker(
                        "false_blocker_claim",
                        summary="false blocker claim",
                        detail="solver claimed an unsupported topology deadend",
                        source="worker",
                        retryable=False,
                    ),
                )
                orchestrator.append_feature_child("feature-e2e", "task-1")
                snapshots.append(issue_snapshot("false-blocker-generic"))
                orchestrator.tick_workflow_check()
                snapshots.append(snapshot("after-retry"))
                move("task-1", "queued", "running", "retry claimed with false blocker challenge")
                move("task-1", "running", "done", "resolved")
                set_feature_status("done")

            elif story == "claude_budget_resumes_then_retries":
                add_task(
                    "task-1",
                    "failed",
                    role="planner",
                    engine="claude",
                    summary="budget exhausted planner",
                    blocker=orchestrator.make_blocker(
                        "claude_budget_exhausted",
                        summary="Claude budget exhausted",
                        detail="max_budget limit exhausted",
                        source="worker",
                        retryable=False,
                    ),
                    braid_template=None,
                )
                orchestrator.append_feature_child("feature-e2e", "task-1")
                orchestrator.set_slot_paused("claude", True, reason="budget exhausted", source="scenario")
                snapshots.append(issue_snapshot("budget-paused"))
                orchestrator.set_slot_paused("claude", False, reason="budget refreshed", source="scenario")
                snapshots.append(issue_snapshot("budget-resumed"))
                orchestrator.tick_workflow_check()
                snapshots.append(snapshot("after-retry"))
                move("task-1", "queued", "running", "budget retry claimed")
                move("task-1", "running", "done", "planner succeeded")
                set_feature_status("done")

            elif story == "feature_no_children_self_repairs":
                enqueued = []
                old_enqueue = orchestrator.enqueue_self_repair
                orchestrator.enqueue_self_repair = lambda **kwargs: enqueued.append(kwargs) or {"enqueued": 1, "feature_id": "feature-sr", "task_id": "task-sr"}
                try:
                    snapshots.append(issue_snapshot("feature-no-children"))
                    orchestrator.tick_workflow_check()
                finally:
                    orchestrator.enqueue_self_repair = old_enqueue
                return {
                    "story": story,
                    "self_repair_enqueued": len(enqueued),
                    "policy": snapshots[0].get("policy"),
                    "action": snapshots[0].get("action"),
                    "issue_kind": enqueued[0].get("issue_kind") if enqueued else None,
                }

            elif story == "worker_oom_retries":
                add_task(
                    "task-1",
                    "failed",
                    summary="worker oom",
                    blocker=orchestrator.make_blocker(
                        "worker_crash_oom_killed",
                        summary="worker process was killed",
                        detail="worker crash: oom killed by runtime",
                        source="worker",
                        retryable=False,
                    ),
                )
                orchestrator.append_feature_child("feature-e2e", "task-1")
                snapshots.append(issue_snapshot("oom-killed"))
                orchestrator.tick_workflow_check()
                snapshots.append(snapshot("after-retry"))
                move("task-1", "queued", "running", "retry claimed")
                move("task-1", "running", "done", "succeeded")
                set_feature_status("done")

            elif story == "invalid_braid_refine_retries":
                add_task(
                    "task-1",
                    "failed",
                    summary="invalid refine",
                    blocker=orchestrator.make_blocker(
                        "invalid_braid_refine",
                        summary="invalid BRAID_REFINE contract",
                        detail="BRAID_REFINE: missing separator",
                        source="worker",
                        retryable=False,
                    ),
                )
                orchestrator.append_feature_child("feature-e2e", "task-1")
                snapshots.append(issue_snapshot("invalid-refine"))
                orchestrator.tick_workflow_check()
                snapshots.append(snapshot("after-retry"))
                move("task-1", "queued", "running", "retry claimed")
                move("task-1", "running", "done", "valid refine")
                set_feature_status("done")

            elif story == "nonterminal_reentry_clears_terminal_fields":
                task = add_task(
                    "task-1",
                    "failed",
                    summary="failed then re-entered",
                    blocker=orchestrator.make_blocker(
                        "model_output_invalid",
                        summary="model output invalid",
                        detail="bad trailer",
                        source="worker",
                        retryable=True,
                    ),
                )
                task.update({
                    "finished_at": "2026-04-25T08:00:00",
                    "abandoned_reason": "old terminal reason",
                    "failure": "old failure",
                    "topology_error": "old topology",
                    "false_blocker_claim": "old false blocker",
                    "qa_failure": "old qa failure",
                    "review_verdict": "request_change",
                })
                orchestrator.write_json_atomic(orchestrator.task_path("task-1", "failed"), task)
                orchestrator.append_feature_child("feature-e2e", "task-1")
                move("task-1", "failed", "running", "direct re-entry")
                running = orchestrator.find_task("task-1")[1]
                return {
                    "story": story,
                    "state": running.get("state"),
                    "cleared": {
                        key: running.get(key) is None
                        for key in (
                            "finished_at",
                            "abandoned_reason",
                            "failure",
                            "topology_error",
                            "false_blocker_claim",
                            "qa_failure",
                            "review_verdict",
                        )
                    },
                    "blocker": running.get("blocker"),
                }

            elif story == "planner_refine_retires_followup_lanes":
                target = add_task(
                    "task-old-1",
                    "awaiting-review",
                    summary="old target",
                    parent_task_id="task-plan",
                    engine_args={"slice": {"id": "slice-1", "execution_path": "slice-1 -> slice-2"}},
                )
                target["review_feedback_rounds"] = 4
                orchestrator.write_json_atomic(orchestrator.task_path("task-old-1", "awaiting-review"), target)
                add_task("task-old-2", "queued", summary="old dependent", depends_on=["task-old-1"], parent_task_id="task-plan")
                add_task(
                    "task-followup",
                    "blocked",
                    summary="stale feedback lane",
                    source="review-feedback:reviewer-old",
                    parent_task_id="task-old-1",
                    engine_args={"target_task_id": "task-old-1"},
                    blocker=orchestrator.make_blocker(
                        "review_feedback_target_inflight",
                        summary="review-feedback target still in-flight",
                        detail="target state=claimed",
                        source="worker",
                        retryable=True,
                    ),
                )
                orchestrator.append_feature_child("feature-e2e", "task-old-1")
                orchestrator.append_feature_child("feature-e2e", "task-old-2")
                orchestrator.append_feature_child("feature-e2e", "task-followup")
                worker._handle_review_request_change(
                    "reviewer-1",
                    "lvc-standard",
                    target,
                    "still wrong",
                    lambda t: t.update({"reviewed_by": "reviewer-1"}),
                )
                feature_after = read_feature()
                return {
                    "story": story,
                    "target_state": orchestrator.find_task("task-old-1")[0],
                    "sibling_state": orchestrator.find_task("task-old-2")[0],
                    "followup_state": orchestrator.find_task("task-followup")[0],
                    "child_task_ids": feature_after.get("child_task_ids"),
                    "planner_refine_enqueued": any(
                        ((t.get("engine_args") or {}).get("mode") == "planner-refine")
                        for t in orchestrator.iter_tasks(states=("queued",), engine="claude")
                    ),
                }
            elif story == "planner_refine_retires_active_old_topology":
                target = add_task(
                    "task-old-1",
                    "awaiting-review",
                    summary="old target",
                    parent_task_id="task-plan",
                    engine_args={"slice": {"id": "slice-1", "execution_path": "slice-1 -> slice-2 -> slice-3"}},
                )
                target["review_feedback_rounds"] = 4
                orchestrator.write_json_atomic(orchestrator.task_path("task-old-1", "awaiting-review"), target)
                add_task("task-old-2", "awaiting-qa", summary="old qa sibling", parent_task_id="task-plan")
                add_task(
                    "task-old-3",
                    "blocked",
                    summary="old blocked sibling",
                    parent_task_id="task-plan",
                    blocker=orchestrator.make_blocker("qa_smoke_failed", summary="old qa red", detail="old topology", source="scenario", retryable=True),
                )
                orchestrator.append_feature_child("feature-e2e", "task-old-1")
                orchestrator.append_feature_child("feature-e2e", "task-old-2")
                orchestrator.append_feature_child("feature-e2e", "task-old-3")
                worker._handle_review_request_change(
                    "reviewer-1",
                    "lvc-standard",
                    target,
                    "still wrong",
                    lambda t: t.update({"reviewed_by": "reviewer-1"}),
                )
                feature_after = read_feature()
                return {
                    "story": story,
                    "target_state": orchestrator.find_task("task-old-1")[0],
                    "qa_sibling_state": orchestrator.find_task("task-old-2")[0],
                    "blocked_sibling_state": orchestrator.find_task("task-old-3")[0],
                    "child_task_ids": feature_after.get("child_task_ids"),
                    "planner_refine_enqueued": any(
                        ((t.get("engine_args") or {}).get("mode") == "planner-refine")
                        for t in orchestrator.iter_tasks(states=("queued",), engine="claude")
                    ),
                }
            elif story == "review_feedback_self_repair_abandon_cleans_child":
                task = add_task(
                    "task-feedback",
                    "claimed",
                    summary="review feedback missing target",
                    source="review-feedback:task-missing",
                    parent_task_id="task-missing",
                    braid_template="review-address-feedback",
                    engine_args={"target_task_id": "task-missing", "self_repair": {"enabled": True}},
                )
                orchestrator.append_feature_child("feature-e2e", "task-feedback")
                old_reopen = worker._self_repair_reopen_current_issue
                old_tick = orchestrator.tick_self_repair_queue
                reopened = []
                worker._self_repair_reopen_current_issue = lambda task_arg, **kwargs: reopened.append(kwargs)
                orchestrator.tick_self_repair_queue = lambda: {"scheduled": 1}
                try:
                    worker.run_review_feedback_task(task, orchestrator.load_config(), 5, runtime_dir / "task-feedback.log")
                finally:
                    worker._self_repair_reopen_current_issue = old_reopen
                    orchestrator.tick_self_repair_queue = old_tick
                feature_after = read_feature()
                return {
                    "story": story,
                    "feedback_state": orchestrator.find_task("task-feedback")[0],
                    "child_task_ids": feature_after.get("child_task_ids"),
                    "self_repair_reopened": len(reopened),
                }
            elif story == "qa_self_repair_abandon_sets_blocker":
                project_dir = root / "lvc"
                qa_dir = project_dir / "qa"
                qa_dir.mkdir(parents=True, exist_ok=True)
                smoke_path = qa_dir / "smoke.sh"
                smoke_path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
                smoke_path.chmod(0o755)
                target = add_task(
                    "task-target",
                    "awaiting-qa",
                    summary="qa target missing worktree",
                    engine_args={"self_repair": {"enabled": True}},
                )
                target["worktree"] = str(root / "missing-worktree")
                orchestrator.write_json_atomic(orchestrator.task_path("task-target", "awaiting-qa"), target)
                orchestrator.append_feature_child("feature-e2e", "task-target")
                driver = add_task("task-qa", "claimed", role="qa", engine="qa", summary="qa driver", braid_template=None)
                cfg = {
                    "projects": [{"name": "lvc-standard", "path": str(project_dir), "qa": {"smoke": "qa/smoke.sh"}}],
                    "slots": {"qa": {"timeout_sec": 5}},
                }
                old_reopen = worker._self_repair_reopen_current_issue
                old_tick = orchestrator.tick_self_repair_queue
                reopened = []
                worker._self_repair_reopen_current_issue = lambda task_arg, **kwargs: reopened.append(kwargs)
                orchestrator.tick_self_repair_queue = lambda: {"scheduled": 1}
                try:
                    worker.run_qa_slot(driver, cfg)
                finally:
                    worker._self_repair_reopen_current_issue = old_reopen
                    orchestrator.tick_self_repair_queue = old_tick
                state, updated = orchestrator.find_task("task-target")
                blocker = orchestrator.task_blocker(updated)
                return {
                    "story": story,
                    "target_state": state,
                    "blocker_code": (blocker or {}).get("code"),
                    "blocker_retryable": (blocker or {}).get("retryable"),
                    "driver_state": orchestrator.find_task("task-qa")[0],
                    "self_repair_reopened": len(reopened),
                }
            elif story == "atomic_claim_clears_terminal_fields":
                task = add_task(
                    "task-stale",
                    "queued",
                    summary="stale retry queued",
                    blocker=orchestrator.make_blocker("model_output_invalid", summary="old", detail="old", source="scenario", retryable=True),
                )
                task.update({
                    "finished_at": "2026-04-25T08:00:00",
                    "abandoned_reason": "old terminal reason",
                    "failure": "old failure",
                    "topology_error": "old topology",
                    "false_blocker_claim": "old false blocker",
                    "qa_failure": "old qa failure",
                    "review_verdict": "request_change",
                })
                orchestrator.write_json_atomic(orchestrator.task_path("task-stale", "queued"), task)
                claimed = orchestrator.atomic_claim("codex")
                state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
                engine = state_engine.StateEngine(
                    state_engine.StateEngineConfig(
                        root=root / "state-engine",
                        db_path=root / "state-engine" / "runtime" / "orchestrator.db",
                        migrations_dir=repo_root / "state" / "migrations",
                        mode="primary",
                    )
                )
                engine.initialize()
                db_task = dict(task)
                db_task["task_id"] = "task-stale-db"
                db_task["state"] = "queued"
                engine.upsert_task(db_task, state="queued")
                claimed_db = engine.claim_task("task-stale-db", slot_engine="codex", claimed_at="2026-04-25T08:30:00")
                return {
                    "story": story,
                    "claimed_task_id": (claimed or {}).get("task_id"),
                    "state": (claimed or {}).get("state"),
                    "cleared": {
                        key: (claimed or {}).get(key) is None
                        for key in (
                            "finished_at",
                            "abandoned_reason",
                            "failure",
                            "topology_error",
                            "false_blocker_claim",
                            "qa_failure",
                            "review_verdict",
                        )
                    },
                    "blocker": (claimed or {}).get("blocker"),
                    "state_engine_claimed_task_id": (claimed_db or {}).get("task_id"),
                    "state_engine_state": (claimed_db or {}).get("state"),
                    "state_engine_cleared": {
                        key: (claimed_db or {}).get(key) is None
                        for key in (
                            "finished_at",
                            "abandoned_reason",
                            "failure",
                            "topology_error",
                            "false_blocker_claim",
                            "qa_failure",
                            "review_verdict",
                        )
                    },
                    "state_engine_blocker": (claimed_db or {}).get("blocker"),
                }
            elif story == "finalize_dead_followup_mark_ready":
                orchestrator.update_feature(
                    "feature-e2e",
                    lambda f: f.update({
                        "status": "finalizing",
                        "final_pr_number": 42,
                        "final_pr_sweep": {"feedback_task_id": "task-followup"},
                    }),
                )
                add_task(
                    "task-followup",
                    "blocked",
                    summary="dead final PR feedback",
                    source="pr-feedback:feature-e2e",
                    parent_task_id="feature-e2e",
                    engine_args={"mode": "feature-pr-feedback", "target_task_id": "feature-e2e"},
                    blocker=orchestrator.make_blocker("qa_smoke_failed", summary="dead follow-up", detail="old follow-up failed", source="scenario", retryable=True),
                )
                old_pr_snapshot = orchestrator._workflow_pr_snapshot
                orchestrator._workflow_pr_snapshot = lambda project, feature: {
                    "state": "OPEN",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "reviewDecision": "",
                    "statusCheckRollup": [],
                }
                try:
                    before = issue_snapshot("before")
                    orchestrator.tick_workflow_check()
                    after_first = read_feature()
                    orchestrator.tick_workflow_check()
                    after_second = read_feature()
                finally:
                    orchestrator._workflow_pr_snapshot = old_pr_snapshot
                return {
                    "story": story,
                    "before_policy": before.get("policy"),
                    "followup_state": orchestrator.find_task("task-followup")[0],
                    "merge_ready": bool((after_first.get("final_pr_sweep") or {}).get("merge_ready_at")),
                    "idempotent": (after_first.get("final_pr_sweep") or {}).get("merge_ready_at") == (after_second.get("final_pr_sweep") or {}).get("merge_ready_at"),
                }
            else:
                raise AssertionError(f"unknown workflow e2e story: {story}")

            final_feature = read_feature()
            final_workflow = orchestrator.feature_workflow_summary(final_feature)
            final_issue = (
                orchestrator._workflow_issue_from_summary(final_feature, final_workflow, orchestrator.load_config())
                if final_feature.get("status") in ("open", "finalizing") else None
            )
            return {
                "story": story,
                "final_status": final_feature.get("status"),
                "final_issue": bool(final_issue),
                "has_live_work": final_workflow.get("has_live_work"),
                "frontier_state": (final_workflow.get("frontier") or {}).get("state") if final_issue else None,
                "snapshots": snapshots,
            }
        finally:
            for key, value in old.items():
                setattr(orchestrator, key, value)
            worker.o = old_worker_o
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]


def _run_ull_lock_guard_retries_with_context(repo_root, scenario):
    orchestrator, worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        old_root = orchestrator.QUEUE_ROOT
        old_subprocess = orchestrator.reset_task_for_retry.__globals__["subprocess"]
        old_cache = orchestrator._STATE_ENGINE_CACHE
        old_mode = os.environ.get("STATE_ENGINE_MODE")
        old_transitions = orchestrator.TRANSITIONS_LOG
        old_events = orchestrator.EVENTS_LOG
        old_append_metric = orchestrator.append_metric
        orchestrator.QUEUE_ROOT = root / "queue"
        orchestrator.TRANSITIONS_LOG = root / "state" / "runtime" / "transitions.jsonl"
        orchestrator.EVENTS_LOG = root / "state" / "runtime" / "events.jsonl"
        orchestrator.append_metric = lambda *args, **kwargs: {}
        orchestrator.reset_task_for_retry.__globals__["subprocess"] = subprocess
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        os.environ["STATE_ENGINE_MODE"] = "off"
        try:
            for state in orchestrator.STATES:
                (orchestrator.QUEUE_ROOT / state).mkdir(parents=True, exist_ok=True)
            task = orchestrator.new_task(
                role="implementer",
                engine="codex",
                project="lvc-standard",
                summary="Implement snapshot writer",
                source="scenario",
                braid_template="lvc-implement-operator",
                engine_args={"slice": {"id": "slice-1", "depends_on": [], "execution_path": "slice-1", "plan": []}},
            )
            task["task_id"] = "task-ull"
            task["state"] = "running"
            task["attempt"] = 1
            orchestrator.write_json_atomic(orchestrator.task_path("task-ull", "running"), task)
            findings = [
                "core/src/main/java/com/joshorig/ull/lvc/metrics/LvcMetricsRegistry.java: contains `synchronized`; ULL/hot-path code must avoid monitor-based locking.",
                "store/src/test/java/com/joshorig/ull/lvc/store/mmap/MmapStoreSnapshotTest.java: contains blocking coordination primitive; ULL hot paths must avoid blocking synchronization.",
            ]
            worker._retry_task_for_ull_lock_guard(task, findings, from_state="running")
            queued = orchestrator.read_json(orchestrator.task_path("task-ull", "queued"), {})
            prompt = worker.build_codex_prompt(queued, "graph", "memory")
        finally:
            orchestrator.QUEUE_ROOT = old_root
            orchestrator.TRANSITIONS_LOG = old_transitions
            orchestrator.EVENTS_LOG = old_events
            orchestrator.append_metric = old_append_metric
            orchestrator.reset_task_for_retry.__globals__["subprocess"] = old_subprocess
            orchestrator._STATE_ENGINE_CACHE = old_cache
            if old_mode is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_mode
    history = list(queued.get("attempt_history") or [])
    snap = (history[0] or {}).get("snapshot") or {}
    blocker = snap.get("blocker") or {}
    retry_ctx = ((queued.get("engine_args") or {}).get("retry_context") or {})
    return {
        "state": queued.get("state"),
        "attempt": queued.get("attempt"),
        "retry_kind": retry_ctx.get("kind"),
        "retry_findings_count": len(retry_ctx.get("findings") or []),
        "history_blocker_code": blocker.get("code"),
        "history_failure": snap.get("failure"),
        "prompt_has_retry_context": "[RETRY CONTEXT]" in prompt and "ULL lock guard" in prompt and "contains `synchronized`" in prompt,
    }


def _run_feature_finalize_planner_live_no_children(repo_root, scenario):
    orchestrator, _worker = _load_repo_modules(repo_root)
    feature = {
        "feature_id": "feature-live",
        "status": "open",
        "project": "demo",
        "child_task_ids": [],
    }
    calls = {"updated": 0, "transitioned": 0, "events": 0}
    old = {
        name: orchestrator.feature_finalize.__globals__[name]
        for name in (
            "shutil",
            "load_config",
            "list_features",
            "feature_workflow_summary",
            "_load_feature_children",
            "_feature_all_children_failed_without_retry",
            "update_feature",
            "append_transition",
            "append_event",
        )
    }
    try:
        orchestrator.feature_finalize.__globals__["shutil"] = types.SimpleNamespace(which=lambda _name: "/opt/homebrew/bin/gh")
        orchestrator.feature_finalize.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": "/tmp/demo"}]}
        orchestrator.feature_finalize.__globals__["list_features"] = lambda status="open": [dict(feature)] if status == "open" else []
        orchestrator.feature_finalize.__globals__["feature_workflow_summary"] = lambda _feature: {
            "has_live_work": True,
            "child_metadata_complete": True,
        }
        orchestrator.feature_finalize.__globals__["_load_feature_children"] = lambda _feature: []
        orchestrator.feature_finalize.__globals__["_feature_all_children_failed_without_retry"] = lambda _feature: False
        orchestrator.feature_finalize.__globals__["update_feature"] = lambda *args, **kwargs: calls.__setitem__("updated", calls["updated"] + 1)
        orchestrator.feature_finalize.__globals__["append_transition"] = lambda *args, **kwargs: calls.__setitem__("transitioned", calls["transitioned"] + 1)
        orchestrator.feature_finalize.__globals__["append_event"] = lambda *args, **kwargs: calls.__setitem__("events", calls["events"] + 1)
        checked, opened, abandoned, skipped = orchestrator.feature_finalize()
    finally:
        for key, value in old.items():
            orchestrator.feature_finalize.__globals__[key] = value
    return {
        "checked": checked,
        "opened": opened,
        "abandoned": abandoned,
        "skipped": skipped,
        "updated": calls["updated"],
        "transitioned": calls["transitioned"],
        "events": calls["events"],
    }


def _run_feature_finalize_untracked_live_no_children(repo_root, scenario):
    orchestrator, _worker = _load_repo_modules(repo_root)
    feature = {
        "feature_id": "feature-untracked-live",
        "status": "open",
        "project": "demo",
        "child_task_ids": [],
    }
    calls = {"updated": 0, "transitioned": 0, "events": 0}
    old = {
        name: orchestrator.feature_finalize.__globals__[name]
        for name in (
            "shutil",
            "load_config",
            "list_features",
            "feature_workflow_summary",
            "_load_feature_children",
            "_feature_all_children_failed_without_retry",
            "update_feature",
            "append_transition",
            "append_event",
        )
    }
    try:
        orchestrator.feature_finalize.__globals__["shutil"] = types.SimpleNamespace(which=lambda _name: "/opt/homebrew/bin/gh")
        orchestrator.feature_finalize.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": "/tmp/demo"}]}
        orchestrator.feature_finalize.__globals__["list_features"] = lambda status="open": [dict(feature)] if status == "open" else []
        orchestrator.feature_finalize.__globals__["feature_workflow_summary"] = lambda _feature: {
            "has_live_work": True,
            "child_metadata_complete": False,
        }
        orchestrator.feature_finalize.__globals__["_load_feature_children"] = lambda _feature: []
        orchestrator.feature_finalize.__globals__["_feature_all_children_failed_without_retry"] = lambda _feature: False
        orchestrator.feature_finalize.__globals__["update_feature"] = lambda *args, **kwargs: calls.__setitem__("updated", calls["updated"] + 1)
        orchestrator.feature_finalize.__globals__["append_transition"] = lambda *args, **kwargs: calls.__setitem__("transitioned", calls["transitioned"] + 1)
        orchestrator.feature_finalize.__globals__["append_event"] = lambda *args, **kwargs: calls.__setitem__("events", calls["events"] + 1)
        checked, opened, abandoned, skipped = orchestrator.feature_finalize()
    finally:
        for key, value in old.items():
            orchestrator.feature_finalize.__globals__[key] = value
    return {
        "checked": checked,
        "opened": opened,
        "abandoned": abandoned,
        "skipped": skipped,
        "updated": calls["updated"],
        "transitioned": calls["transitioned"],
        "events": calls["events"],
    }


def _run_feature_finalize_orphan_no_children(repo_root, scenario):
    orchestrator, _worker = _load_repo_modules(repo_root)
    feature = {
        "feature_id": "feature-orphan",
        "status": "open",
        "project": "demo",
        "child_task_ids": [],
    }
    calls = {"updated": 0, "transitioned": 0, "events": 0}
    old = {
        name: orchestrator.feature_finalize.__globals__[name]
        for name in (
            "shutil",
            "load_config",
            "list_features",
            "feature_workflow_summary",
            "_load_feature_children",
            "_feature_all_children_failed_without_retry",
            "update_feature",
            "append_transition",
            "append_event",
        )
    }
    try:
        orchestrator.feature_finalize.__globals__["shutil"] = types.SimpleNamespace(which=lambda _name: "/opt/homebrew/bin/gh")
        orchestrator.feature_finalize.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": "/tmp/demo"}]}
        orchestrator.feature_finalize.__globals__["list_features"] = lambda status="open": [dict(feature)] if status == "open" else []
        orchestrator.feature_finalize.__globals__["feature_workflow_summary"] = lambda _feature: {
            "has_live_work": False,
            "child_metadata_complete": True,
        }
        orchestrator.feature_finalize.__globals__["_load_feature_children"] = lambda _feature: []
        orchestrator.feature_finalize.__globals__["_feature_all_children_failed_without_retry"] = lambda _feature: False
        orchestrator.feature_finalize.__globals__["update_feature"] = lambda *args, **kwargs: calls.__setitem__("updated", calls["updated"] + 1)
        orchestrator.feature_finalize.__globals__["append_transition"] = lambda *args, **kwargs: calls.__setitem__("transitioned", calls["transitioned"] + 1)
        orchestrator.feature_finalize.__globals__["append_event"] = lambda *args, **kwargs: calls.__setitem__("events", calls["events"] + 1)
        checked, opened, abandoned, skipped = orchestrator.feature_finalize()
    finally:
        for key, value in old.items():
            orchestrator.feature_finalize.__globals__[key] = value
    return {
        "checked": checked,
        "opened": opened,
        "abandoned": abandoned,
        "skipped": skipped,
        "updated": calls["updated"],
        "transitioned": calls["transitioned"],
        "events": calls["events"],
    }


def _run_atomic_claim_skips_terminal_feature_children(repo_root, scenario):
    orchestrator, _worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        queue_root = root / "queue"
        features_dir = root / "features"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        features_dir.mkdir(parents=True, exist_ok=True)
        old = {
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
            "now_iso": orchestrator.now_iso,
            "project_environment_ok": orchestrator.project_environment_ok,
            "_STATE_ENGINE_CACHE": orchestrator._STATE_ENGINE_CACHE,
            "STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"),
        }
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.FEATURES_DIR = features_dir
        orchestrator.now_iso = lambda: "2026-04-23T23:40:00"
        orchestrator.project_environment_ok = lambda *_args, **_kwargs: True
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        os.environ["STATE_ENGINE_MODE"] = "off"
        try:
            orchestrator.write_json_atomic(features_dir / "feature-dead.json", {
                "feature_id": "feature-dead",
                "status": "abandoned",
                "project": "demo",
            })
            dead = orchestrator.new_task(role="implementer", engine="codex", project="demo", summary="dead", source="scenario", feature_id="feature-dead")
            dead["task_id"] = "task-dead"
            ready = orchestrator.new_task(role="implementer", engine="codex", project="demo", summary="ready", source="scenario")
            ready["task_id"] = "task-ready"
            orchestrator.write_json_atomic(orchestrator.task_path("task-dead", "queued"), dead)
            orchestrator.write_json_atomic(orchestrator.task_path("task-ready", "queued"), ready)
            claimed = orchestrator.atomic_claim("codex")
        finally:
            orchestrator.QUEUE_ROOT = old["QUEUE_ROOT"]
            orchestrator.FEATURES_DIR = old["FEATURES_DIR"]
            orchestrator.now_iso = old["now_iso"]
            orchestrator.project_environment_ok = old["project_environment_ok"]
            orchestrator._STATE_ENGINE_CACHE = old["_STATE_ENGINE_CACHE"]
            if old["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old["STATE_ENGINE_MODE"]
    return {
        "claimed_task_id": (claimed or {}).get("task_id"),
    }


def _run_health_payload_prefers_live_state(repo_root, scenario):
    orchestrator, _worker = _load_repo_modules(repo_root)
    old = {
        "_queue_count_map": orchestrator._health_payload.__globals__["_queue_count_map"],
        "environment_health": orchestrator._health_payload.__globals__["environment_health"],
        "open_feature_workflow_summaries": orchestrator._health_payload.__globals__["open_feature_workflow_summaries"],
        "_live_workflow_issue_count": orchestrator._health_payload.__globals__["_live_workflow_issue_count"],
        "load_config": orchestrator._health_payload.__globals__["load_config"],
        "now_iso": orchestrator._health_payload.__globals__["now_iso"],
    }
    try:
        orchestrator._health_payload.__globals__["_queue_count_map"] = lambda: {"queued": 0, "running": 1}
        orchestrator._health_payload.__globals__["environment_health"] = lambda: {"ok": True, "issues": []}
        orchestrator._health_payload.__globals__["open_feature_workflow_summaries"] = lambda: [
            {"frontier": {"state": "blocked"}},
            {"frontier": {"state": "running"}},
        ]
        orchestrator._health_payload.__globals__["_live_workflow_issue_count"] = lambda _cfg, workflows=None, health=None: 3
        orchestrator._health_payload.__globals__["load_config"] = lambda: {"projects": []}
        orchestrator._health_payload.__globals__["now_iso"] = lambda: "2026-04-23T23:41:00"
        payload = orchestrator._health_payload()
    finally:
        for key, value in old.items():
            orchestrator._health_payload.__globals__[key] = value
    return {
        "environment_ok": payload["environment_ok"],
        "environment_error_count": payload["environment_error_count"],
        "workflow_check_issue_count": payload["workflow_check_issue_count"],
        "feature_open_count": payload["feature_open_count"],
        "feature_frontier_blocked_count": payload["feature_frontier_blocked_count"],
    }


def _run_handoff_writer_contract_audit(repo_root, scenario):
    worker_path = pathlib.Path(repo_root) / "bin" / "worker.py"
    text = worker_path.read_text()
    forbidden = [
        "Emit EXACTLY one final line:",
        "Emit exactly one of these as the final line of your response:",
        "emit exactly one line `BRAID_TOPOLOGY_ERROR",
    ]
    return {
        "braid_helper_calls": text.count("_braid_output_contract_prompt("),
        "template_helper_calls": text.count("_template_output_contract_prompt("),
        "forbidden_present": any(item in text for item in forbidden),
        "review_writer_uses_helper": "_specialized_review_prompt" in text and 'ok_verdicts=("approve", "request_change")' in text,
        "qa_writer_uses_helper": 'ok_verdicts=("qa_sufficient", "qa_insufficient")' in text,
    }


def _run_planner_prompt_json_contract(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    system_prompt, user_prompt, _panel = worker.planner_council_prompt(
        {"engine_args": {"roadmap_entry": {"id": "R-002", "title": "Title", "body": "Body"}}},
        {"name": "lvc-standard"},
        "memory",
        {"council": {}},
    )
    repair_system, repair_user, _members = worker.self_repair_council_prompt(
        {
            "summary": "repair",
            "feature_id": "feature-1",
            "engine_args": {"issue_key": "issue-1", "evidence": "evidence", "self_repair": {}},
        },
        {"name": "devmini-orchestrator"},
        "memory",
    )
    needle = "Do not wrap the JSON object in ``` fences."
    return {
        "planner_has_strict_contract": needle in system_prompt and "Return the JSON object directly. Do not use ```json fences." in user_prompt,
        "self_repair_has_strict_contract": needle in repair_system and "Return the JSON object directly. Do not use ```json fences." in repair_user,
    }


def _run_planner_fenced_json_normalization(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    raw = """```json
{"execution_path":"slice-1 -> slice-2","slices":[{"id":"slice-1","summary":"writer","braid_template":"lvc-implement-operator"}]}
```"""
    plan, slices = worker._parse_planner_output(raw, council_members=("aristotle",), self_repair=False)
    return {
        "execution_path": plan.get("execution_path"),
        "slice_id": slices[0].get("id"),
    }


def _run_patch_anchor_failures_trigger_topology(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    trailer = worker._synthesize_patch_anchor_topology_error(
        "apply_patch verification failed: one\nnoise\napply_patch verification failed: two\n",
        "",
    )
    return {
        "trailer": trailer,
        "valid": worker.topology_reason_is_valid(trailer),
        "code": worker.topology_reason_code(trailer),
    }


def _run_ull_lock_guard_findings(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        path = root / "core" / "src" / "main" / "java" / "Demo.java"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("class Demo { void ok() {} }\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
        path.write_text(
            "import java.util.concurrent.locks.ReentrantLock;\n"
            "class Demo { private final ReentrantLock lock = new ReentrantLock(); synchronized void bad() {} }\n"
        )
        findings = worker._ull_lock_guard_findings("lvc-standard", str(root), "HEAD")
        return {
            "has_synchronized": any("synchronized" in item for item in findings),
            "has_reentrant_lock": any("ReentrantLock" in item for item in findings),
        }


def _run_circular_feature_lineage(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        feats = pathlib.Path(tmp)
        old = orchestrator.FEATURES_DIR
        orchestrator.FEATURES_DIR = feats
        try:
            orchestrator.write_json_atomic(feats / "A.json", {"feature_id": "A", "parent_feature_id": "B"})
            orchestrator.write_json_atomic(feats / "B.json", {"feature_id": "B", "parent_feature_id": "A"})
            lineage = orchestrator.feature_ancestor_ids("A", max_depth=10)
        finally:
            orchestrator.FEATURES_DIR = old
        return {
            "terminated": lineage == ["B", "A"],
        }


def _run_same_epoch_ordering(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        engine.initialize()
        conn = engine.connect()
        with conn:
            conn.execute("INSERT INTO task_transitions(task_id, from_state, to_state, reason, created_at, created_at_epoch) VALUES ('task-same', 'queued', 'running', 'first', '2026-01-01T00:00:00', 1)")
            conn.execute("INSERT INTO task_transitions(task_id, from_state, to_state, reason, created_at, created_at_epoch) VALUES ('task-same', 'running', 'done', 'second', '2026-01-01T00:00:00', 1)")
        reasons = [row["reason"] for row in engine.read_transitions(task_id="task-same")]
        return {"deterministic_order": reasons == ["first", "second"]}


def _run_epoch_2038(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    future_epoch = 2**31 + 1000
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=root / "runtime" / "orchestrator.db", migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        engine.initialize()
        conn = engine.connect()
        with conn:
            conn.execute("INSERT INTO metrics(name, value, metric_type, created_at_epoch, tags_json) VALUES ('epoch2038', 1, 'gauge', ?, '{}')", (future_epoch,))
        rows = engine.read_metrics(name="epoch2038", limit=1)
        return {
            "row_found": len(rows) == 1,
            "future_ts_rendered": bool(rows) and rows[0]["ts"].startswith("2038"),
        }


def _run_vacuum_bloat(repo_root, scenario):
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    total_rows = int(scenario.get("rows") or 100_000)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        db_path = root / "runtime" / "orchestrator.db"
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=db_path, migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        engine.initialize()
        conn = engine.connect()
        def total_size():
            total = 0
            for suffix in ("", "-wal", "-shm"):
                path = pathlib.Path(f"{db_path}{suffix}")
                if path.exists():
                    total += path.stat().st_size
            return total
        batch = []
        for idx in range(total_rows):
            batch.append((f"metric-{idx}", float(idx), "gauge", idx + 1, "{}"))
            if len(batch) >= 10000:
                with conn:
                    conn.executemany("INSERT INTO metrics(name, value, metric_type, created_at_epoch, tags_json) VALUES (?, ?, ?, ?, ?)", batch)
                batch.clear()
        if batch:
            with conn:
                conn.executemany("INSERT INTO metrics(name, value, metric_type, created_at_epoch, tags_json) VALUES (?, ?, ?, ?, ?)", batch)
        before = total_size()
        with conn:
            conn.execute("DELETE FROM metrics WHERE id % 2 = 0")
        engine.checkpoint(conn=conn)
        mid = total_size()
        conn.execute("VACUUM")
        engine.checkpoint(conn=conn)
        after = total_size()
        return {
            "shrunk_gt_10pct": after < (mid * 0.9),
            "reads_correct": len(engine.read_metrics(limit=10)) == 10,
        }


def _run_events_rotation(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        runtime = root / "runtime"
        runtime.mkdir(parents=True)
        events = runtime / "events.jsonl"
        events.write_text("x" * int(scenario.get("size_bytes") or (101 * 1024 * 1024)), encoding="utf-8")
        old_runtime = orchestrator.RUNTIME_DIR
        old_events = orchestrator.EVENTS_LOG
        orchestrator.RUNTIME_DIR = runtime
        orchestrator.EVENTS_LOG = events
        try:
            archive = orchestrator._rotate_events_mirror(max_bytes=int(scenario.get("threshold_bytes") or (100 * 1024 * 1024)))
        finally:
            orchestrator.RUNTIME_DIR = old_runtime
            orchestrator.EVENTS_LOG = old_events
        return {
            "archived": bool(archive),
            "mirror_truncated": events.exists() and events.stat().st_size == 0,
        }


def _run_nfs_wal_contention_spec(repo_root, scenario):
    return {"hardware_gated": True, "executed": False}


def _run_cross_process_claim(repo_root, scenario):
    script = """
import json, pathlib, sqlite3, sys
repo_root = pathlib.Path(sys.argv[1])
db_path = pathlib.Path(sys.argv[2])
task_id = sys.argv[3]
sys.path.insert(0, str(repo_root / 'bin'))
import state_engine
engine = state_engine.StateEngine(state_engine.StateEngineConfig(root=db_path.parent.parent, db_path=db_path, migrations_dir=repo_root / 'state' / 'migrations', mode='primary'))
engine.initialize()
task = engine.claim_task(task_id, slot_engine='codex', claimed_at='2026-01-01T00:00:00')
print(json.dumps({'claimed': bool(task), 'task_id': task.get('task_id') if task else None}))
"""
    state_engine = _load_module("state_engine", repo_root / "bin" / "state_engine.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        db_path = root / "runtime" / "orchestrator.db"
        engine = state_engine.StateEngine(
            state_engine.StateEngineConfig(root=root, db_path=db_path, migrations_dir=repo_root / "state" / "migrations", mode="primary")
        )
        engine.initialize()
        conn = engine.connect()
        with conn:
            for idx in range(4):
                task_id = f"task-{idx}"
                task = {
                    "task_id": task_id,
                    "state": "queued",
                    "created_at": "2026-01-01T00:00:00",
                    "state_updated_at": "2026-01-01T00:00:00",
                    "engine": "codex",
                    "role": "implementer",
                    "project": "demo",
                    "summary": "queued",
                    "attempt": 1,
                }
                conn.execute(
                    "INSERT INTO tasks(task_id, state, created_at, created_at_epoch, state_updated_at, engine, role, project, summary, attempt, metadata_json) VALUES (?, 'queued', '2026-01-01T00:00:00', ?, '2026-01-01T00:00:00', 'codex', 'implementer', 'demo', 'queued', 1, ?)",
                    (task_id, idx + 1, json.dumps(task, sort_keys=True)),
                )
        procs = [
            subprocess.run([sys.executable, "-c", script, str(repo_root), str(db_path), "task-0"], capture_output=True, text=True, check=False)
            for _ in range(2)
        ]
        claimed = [json.loads(p.stdout or "{}") for p in procs]
        winners = [row for row in claimed if row.get("claimed")]
        return {
            "single_winner": len(winners) == 1,
            "winner_task_id": winners[0]["task_id"] if winners else None,
        }


def _run_allowlist_corrupt(repo_root, scenario):
    orchestrator = _load_module("orchestrator", repo_root / "bin" / "orchestrator.py")
    telegram = _load_module("telegram_bot", repo_root / "bin" / "telegram_bot.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        allowlist = root / "runtime" / "allowlist.json"
        allowlist.parent.mkdir(parents=True, exist_ok=True)
        allowlist.write_text("{broken", encoding="utf-8")
        config = root / "config" / "telegram.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"bot_token": "token"}), encoding="utf-8")
        events = []
        old = {
            "ALLOWLIST_PATH": orchestrator.ALLOWLIST_PATH,
            "append_event": orchestrator.append_event,
            "CONFIG_PATH": telegram.CONFIG_PATH,
        }
        orchestrator.ALLOWLIST_PATH = allowlist
        orchestrator.append_event = lambda *args, **kwargs: events.append({"args": args, "kwargs": kwargs})
        telegram.CONFIG_PATH = config
        try:
            cfg = telegram.load_bot_config()
        finally:
            orchestrator.ALLOWLIST_PATH = old["ALLOWLIST_PATH"]
            orchestrator.append_event = old["append_event"]
            telegram.CONFIG_PATH = old["CONFIG_PATH"]
        return {
            "bot_refused": cfg is None,
            "typed_alert_emitted": any(row["args"][1] == "allowlist_corrupt" for row in events),
        }


def _run_runner_fixture_restore(repo_root, scenario_dir, scenario):
    def _trial(workspace_dir, index):
        seeded = workspace_dir / "state" / "runtime" / "seed.txt"
        dirty = workspace_dir / "state" / "runtime" / "dirty.txt"
        initial_seed = seeded.read_text(encoding="utf-8").strip()
        dirty_exists_before = dirty.exists()
        seeded.write_text(f"mutated-{index}\n", encoding="utf-8")
        dirty.write_text(f"dirty-{index}\n", encoding="utf-8")
        return {
            "initial_seed": initial_seed,
            "dirty_exists_before": dirty_exists_before,
        }

    trials = _run_trials_with_fixture_snapshot(scenario_dir, scenario, _trial)
    return {
        "trial_count": len(trials),
        "all_seeded": all(t["initial_seed"] == "seed" for t in trials),
        "dirty_exists_before_any": any(t["dirty_exists_before"] for t in trials),
        "mutations_isolated": len({t["initial_seed"] for t in trials}) == 1,
    }


def _run_runner_trace_dirs(repo_root, scenario_dir, scenario):
    def _trial(workspace_dir, index):
        marker = workspace_dir / "state" / "runtime" / f"trace-{index}.txt"
        marker.write_text(f"trial-{index + 1}\n", encoding="utf-8")
        return {"marker": marker.name}

    trials = _run_trials_with_fixture_snapshot(scenario_dir, scenario, _trial)
    trace_root = pathlib.Path(os.environ["HARNESS_TRACE_ROOT"])
    trial_dirs = sorted([p for p in trace_root.iterdir() if p.is_dir() and p.name.startswith("trial-")])
    return {
        "trial_count": len(trials),
        "trial_dir_count": len(trial_dirs),
        "has_meta": all((p / "meta.json").exists() for p in trial_dirs),
        "has_before_snapshots": all((p / "state_snapshots" / "before").exists() for p in trial_dirs),
        "has_after_snapshots": all((p / "state_snapshots" / "after").exists() for p in trial_dirs),
        "marker_captured": all(
            any(path.name.startswith("trace-") for path in (p / "state_snapshots" / "after" / "state" / "runtime").glob("*"))
            for p in trial_dirs
        ),
    }


def _run_runner_version_budgets(repo_root, scenario_dir, scenario):
    delay = float(scenario.get("sleep_seconds") or 0.0)
    if delay > 0:
        time.sleep(delay)
    return {
        "token_usage": int(scenario.get("reported_token_usage") or 0),
        "marker": "ok",
    }


def _run_runner_summary(repo_root, scenario_dir, scenario):
    with tempfile.TemporaryDirectory() as tmp:
        runs_root = pathlib.Path(tmp)
        samples = [
            ("20260101T000000Z", "59-wal-growth-no-checkpoint", "wal_growth_stalls", True),
            ("20260101T000001Z", "62-council-verdict-json-malformed", "council_malformed", False),
            ("20260101T000002Z", "44-telegram-command-surface", "telegram_surface", True),
            ("20260101T000003Z", "runner-r3-version-and-budgets", "runner_version_budgets", True),
        ]
        for stamp, name, kind, passed in samples:
            root = runs_root / stamp / name
            root.mkdir(parents=True, exist_ok=True)
            _write_json(root / "scenario.json", {"kind": kind, "scenario_version": 1})
            _write_json(root / "expected.json", {})
            _write_json(root / "actual.json", {})
            _write_json(root / "result.json", {"passed": passed, "scenario_kind": kind})
        out = summarize_runs(repo_root, runs_dir=runs_root)
        return {
            "total_scenarios": out["total"]["scenarios"],
            "total_passed": out["total"]["passed"],
            "state_engine_pass_rate": out["clusters"]["state-engine"]["pass_rate"],
            "self_repair_failed": out["clusters"]["self-repair"]["failed"],
            "wave_c_passed": out["clusters"]["wave-c"]["passed"],
            "runner_passed": out["clusters"]["runner"]["passed"],
        }


def _run_self_repair_observation(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        feats = root / "features"
        feats.mkdir()
        queue_root = root / "queue"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old_env = {
            "STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"),
            "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH"),
        }
        old = {
            "FEATURES_DIR": orchestrator.FEATURES_DIR,
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "append_event": orchestrator.append_event,
            "append_transition": orchestrator.append_transition,
        }
        events = []
        transitions = []
        os.environ["STATE_ENGINE_MODE"] = "off"
        os.environ["STATE_ENGINE_PATH"] = str(root / "runtime" / "scenario.db")
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        orchestrator.FEATURES_DIR = feats
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.append_event = lambda *args, **kwargs: events.append({"args": args, "kwargs": kwargs})
        orchestrator.append_transition = lambda *args: transitions.append(args)
        try:
            orchestrator.write_json_atomic(
                feats / f"{scenario['feature_id']}.json",
                {
                    "feature_id": scenario["feature_id"],
                    "project": scenario["project"],
                    "status": "open",
                    "source": "self-repair:test",
                    "self_repair": {
                        "enabled": True,
                        "issues": [
                            {
                                "issue_key": scenario["issue_key"],
                                "status": "resolved",
                                "observation_target": dict(scenario["observation_target"]),
                                "observation_due_at": scenario["observation_due_at"],
                                "observation_status": "pending",
                            }
                        ],
                    },
                },
            )
            orchestrator.write_json_atomic(
                queue_root / scenario["task_state"] / f"{scenario['observation_target']['task_id']}.json",
                {
                    "task_id": scenario["observation_target"]["task_id"],
                    "blocker": {"code": scenario["observation_target"]["blocker_code"]},
                },
            )
            out = orchestrator.tick_self_repair_observation_window()
            saved = orchestrator.read_json(feats / f"{scenario['feature_id']}.json", {})
        finally:
            orchestrator.FEATURES_DIR = old["FEATURES_DIR"]
            orchestrator.QUEUE_ROOT = old["QUEUE_ROOT"]
            orchestrator.append_event = old["append_event"]
            orchestrator.append_transition = old["append_transition"]
            orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]
            if old_env["STATE_ENGINE_PATH"] is None:
                os.environ.pop("STATE_ENGINE_PATH", None)
            else:
                os.environ["STATE_ENGINE_PATH"] = old_env["STATE_ENGINE_PATH"]
        issue = saved["self_repair"]["issues"][0]
        return {
            "reopened": out["reopened"],
            "checked": out["checked"],
            "status": issue.get("status"),
            "observation_status": issue.get("observation_status"),
            "planner_task_id": issue.get("planner_task_id"),
            "transition_labels": [list(row[1:4]) for row in transitions],
            "event_names": [row["args"][1] for row in events],
        }


def _run_telegram_surface(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    old = {
        "_health_payload": orchestrator._health_payload,
        "features_brief": orchestrator.features_brief,
        "queue_brief": orchestrator.queue_brief,
        "task_text": orchestrator.task_text,
    }
    orchestrator._health_payload = lambda: {
        "environment_ok": False,
        "environment_error_count": 2,
        "workflow_check_issue_count": 3,
        "feature_open_count": 4,
        "feature_frontier_blocked_count": 1,
        "queue": {"queued": 5, "running": 2, "blocked": 1, "awaiting-review": 1, "awaiting-qa": 0},
        "generated_at": "2026-04-19T23:45:00",
    }
    orchestrator.features_brief = lambda project=None: f"FEATURES {project or 'all'}"
    orchestrator.queue_brief = lambda state=None: f"QUEUE {state or 'sample'}"
    orchestrator.task_text = lambda task_id: f"TASK {task_id}"
    try:
        outputs = {cmd: orchestrator.dispatch_telegram_command(cmd) for cmd in scenario["commands"]}
    finally:
        for key, value in old.items():
            setattr(orchestrator, key, value)
    return {
        "help": outputs["/help"],
        "health": outputs["/health"],
        "status": outputs["/status"],
        "tasks": outputs["/tasks"],
        "task": outputs["/task task-123"],
        "queue": outputs["/queue blocked"],
    }


def _run_task_cost_capture(repo_root, scenario):
    orchestrator, worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        old_env = {
            "STATE_ENGINE_MODE": os.environ.get("STATE_ENGINE_MODE"),
            "STATE_ENGINE_PATH": os.environ.get("STATE_ENGINE_PATH"),
        }
        old = {
            "STATE_ROOT": orchestrator.STATE_ROOT,
            "RUNTIME_DIR": orchestrator.RUNTIME_DIR,
            "LOGS_DIR": orchestrator.LOGS_DIR,
            "STATE_ENGINE_DB_PATH": orchestrator.STATE_ENGINE_DB_PATH,
        }
        os.environ["STATE_ENGINE_MODE"] = "primary"
        os.environ["STATE_ENGINE_PATH"] = str(root / "runtime" / "orchestrator.db")
        orchestrator.STATE_ROOT = root
        orchestrator.RUNTIME_DIR = root / "runtime"
        orchestrator.LOGS_DIR = root / "logs"
        orchestrator.STATE_ENGINE_DB_PATH = root / "runtime" / "orchestrator.db"
        orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
        try:
            engine = orchestrator.get_state_engine()
            status = engine.initialize()
            codex_rows = worker._record_task_costs_from_text(
                scenario["codex_task_id"],
                "codex",
                scenario["codex_model"],
                scenario["codex_payload"],
            )
            claude_rows = worker._record_task_costs_from_text(
                scenario["claude_task_id"],
                "claude",
                scenario["claude_model"],
                scenario["claude_payload"],
            )
            rows = engine.read_task_costs(limit=10)
            summary = engine.aggregate_task_costs(hours=24)
        finally:
            orchestrator._STATE_ENGINE_CACHE = {"key": None, "engine": None}
            orchestrator.STATE_ROOT = old["STATE_ROOT"]
            orchestrator.RUNTIME_DIR = old["RUNTIME_DIR"]
            orchestrator.LOGS_DIR = old["LOGS_DIR"]
            orchestrator.STATE_ENGINE_DB_PATH = old["STATE_ENGINE_DB_PATH"]
            if old_env["STATE_ENGINE_MODE"] is None:
                os.environ.pop("STATE_ENGINE_MODE", None)
            else:
                os.environ["STATE_ENGINE_MODE"] = old_env["STATE_ENGINE_MODE"]
            if old_env["STATE_ENGINE_PATH"] is None:
                os.environ.pop("STATE_ENGINE_PATH", None)
            else:
                os.environ["STATE_ENGINE_PATH"] = old_env["STATE_ENGINE_PATH"]
        rows_by_task = {row["task_id"]: row for row in rows}
        return {
            "integrity_check": status.get("integrity_check"),
            "codex_rows": codex_rows,
            "claude_rows": claude_rows,
            "summary_rows": summary["summary"]["rows_count"],
            "codex_cost_positive": float(rows_by_task[scenario["codex_task_id"]]["cost_usd"]) > 0,
            "codex_input_tokens": int(rows_by_task[scenario["codex_task_id"]]["input_tokens"]),
            "codex_cache_tokens": int(rows_by_task[scenario["codex_task_id"]]["cache_tokens"]),
            "claude_cost_usd": round(float(rows_by_task[scenario["claude_task_id"]]["cost_usd"]), 4),
            "claude_output_tokens": int(rows_by_task[scenario["claude_task_id"]]["output_tokens"]),
        }


def _run_supply_chain_gate(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        wt = pathlib.Path(tmp) / "repo"
        subprocess.run(["git", "init", "-b", "main", str(wt)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(wt), "config", "user.name", "Doctest"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(wt), "config", "user.email", "doctest@example.com"], check=True, capture_output=True, text=True)
        path = wt / scenario["manifest_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(scenario["baseline_body"])
        subprocess.run(["git", "-C", str(wt), "add", "."], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(wt), "commit", "-m", "baseline"], check=True, capture_output=True, text=True)
        path.write_text(scenario["candidate_body"])
        findings = worker._supply_chain_findings(wt, "main")
        requested = worker._requested_external_skills(
            scenario["project"],
            gate_name="supply-chain-audit-pass",
            changed_files_text=f"{scenario['manifest_path']}\n",
        )
        return {
            "finding_count": len(findings),
            "first_finding": findings[0] if findings else None,
            "requested_skills": requested,
        }


def _run_security_secret_gate(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        wt = pathlib.Path(tmp) / "repo"
        subprocess.run(["git", "init", "-b", "main", str(wt)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(wt), "config", "user.name", "Doctest"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(wt), "config", "user.email", "doctest@example.com"], check=True, capture_output=True, text=True)
        path = wt / scenario["file_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(scenario.get("baseline_body", ""))
        subprocess.run(["git", "-C", str(wt), "add", "."], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(wt), "commit", "-m", "baseline"], check=True, capture_output=True, text=True)
        path.write_text(scenario["candidate_body"])
        findings = worker._security_gate_findings(wt, "main")
        requested = worker._requested_external_skills(
            scenario["project"],
            gate_name="security-review-pass",
            changed_files_text=f"{scenario['file_path']}\n",
        )
        return {
            "finding_count": len(findings),
            "first_finding": findings[0] if findings else None,
            "requested_skills": requested,
        }


def _run_security_secret_gate_ignored(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        wt = pathlib.Path(tmp) / "repo"
        subprocess.run(["git", "init", "-b", "main", str(wt)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(wt), "config", "user.name", "Doctest"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(wt), "config", "user.email", "doctest@example.com"], check=True, capture_output=True, text=True)
        (wt / ".gitignore").write_text("config/telegram.json\n")
        path = wt / "config" / "telegram.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        (wt / "README.md").write_text("baseline\n")
        subprocess.run(["git", "-C", str(wt), "add", ".gitignore", "README.md"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(wt), "commit", "-m", "baseline"], check=True, capture_output=True, text=True)
        path.write_text('{"token":"123456:abcdef"}\n')
        findings = worker._security_gate_findings(wt, "main")
        return {
            "finding_count": len(findings),
            "first_finding": findings[0] if findings else None,
        }


def _run_review_feedback_challenge(repo_root, scenario):
    _, worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        wt = pathlib.Path(tmp) / "repo"
        subprocess.run(["git", "init", "-b", "main", str(wt)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(wt), "config", "user.name", "Doctest"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(wt), "config", "user.email", "doctest@example.com"], check=True, capture_output=True, text=True)
        (wt / ".gitignore").write_text("config/telegram.json\n")
        (wt / "README.md").write_text("baseline\n")
        subprocess.run(["git", "-C", str(wt), "add", ".gitignore", "README.md"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(wt), "commit", "-m", "baseline"], check=True, capture_output=True, text=True)
        path = wt / "config" / "telegram.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"token":"placeholder"}\n')
        target = {
            "task_id": "task-target",
            "worktree": str(wt),
            "base_branch": "main",
        }
        notes = worker._review_feedback_challenge_notes(
            target,
            "REQUEST_CHANGE: secret exposure in config/telegram.json local credential file",
        )
        return {
            "has_challenge": bool(notes),
            "mentions_ignored_file": "config/telegram.json" in notes,
        }


def _run_untrusted_skill_refusal(repo_root, scenario):
    orchestrator, worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        skill_dir = root / "code-reviewer"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# untrusted skill\n")
        old = {
            "_trusted_external_skills_root": worker._trusted_external_skills_root,
            "_trusted_external_skill_registry": worker._trusted_external_skill_registry,
            "append_event": orchestrator.append_event,
        }
        events = []
        worker._trusted_external_skills_root = lambda: root
        worker._trusted_external_skill_registry = lambda: {}
        orchestrator.append_event = lambda *args, **kwargs: events.append({"args": args, "kwargs": kwargs})
        try:
            context = worker._external_skill_context(
                scenario["project"],
                task_id=scenario["task_id"],
                changed_files_text=scenario["changed_files_text"],
            )
        finally:
            worker._trusted_external_skills_root = old["_trusted_external_skills_root"]
            worker._trusted_external_skill_registry = old["_trusted_external_skill_registry"]
            orchestrator.append_event = old["append_event"]
        return {
            "context": context,
            "event_count": len(events),
            "event_name": events[0]["args"][1] if events else None,
            "event_skill": ((events[0]["kwargs"].get("details") or {}).get("skill") if events else None),
        }


def _run_launchd_runtime_env_contract(repo_root, scenario):
    orchestrator, _worker = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        runtime_env = root / "runtime.env"
        runtime_env.write_text("DEV_ROOT=/Volumes/devssd\n")
        labels = list(scenario.get("labels") or [])
        plists = []
        for label in labels:
            plist = root / f"{label}.plist"
            command = (
                f"set -a; source {runtime_env}; set +a; "
                f"exec /opt/homebrew/bin/python3 /Volumes/devssd/orchestrator/bin/orchestrator.py status"
            )
            plist.write_bytes(
                plistlib.dumps(
                    {
                        "Label": label,
                        "ProgramArguments": ["/bin/bash", "-c", command],
                    }
                )
            )
            plists.append(plist)
        result = orchestrator.launchd_runtime_env_contract(plists, runtime_env)
        return {
            "checked": result["checked"],
            "ok": result["ok"],
            "missing": result["missing"],
            "labels": [row["label"] for row in result["rows"]],
        }


def _run_template_contract_yaml_validates(repo_root, scenario):
    _orchestrator, worker = _load_repo_modules(repo_root)
    schema_path = repo_root / "braid" / "contract_schema.json"
    schema = json.loads(schema_path.read_text())
    contract = worker._load_template_contract("lvc-implement-operator")
    invalid_rejected = False
    try:
        worker._validate_template_contract({"contractspec": 1}, path="fixture.contract.yaml")
    except ValueError:
        invalid_rejected = True

    task = {
        "engine_args": {
            "roadmap_entry": {
                "id": "abc-1-fixture",
                "title": "Contract prompt fixture",
                "body": "Plan one LVC implementation slice.",
            }
        }
    }
    system_prompt, _user_prompt, _panel = worker.planner_council_prompt(
        task,
        {"name": "lvc-standard", "path": str(repo_root)},
        "memory fixture",
        {},
    )
    return {
        "contract_loaded": bool(contract),
        "schema_declares_contractspec": "contractspec" in schema.get("required", []),
        "invalid_rejected": invalid_rejected,
        "prompt_contains_contract_block": "[CONTRACT]" in system_prompt and "no-secret-emitted" in system_prompt,
    }


def _run_blocker_tier_routing(repo_root, scenario):
    orchestrator, _worker = _load_repo_modules(repo_root)

    def decision(code, *, state="blocked"):
        issue = {
            "kind": "frontier_task_blocked",
            "task_state": state,
            "blocker": orchestrator.make_blocker(
                code,
                summary=code,
                detail=code,
                source="scenario",
                retryable=True,
            ),
            "diagnosis": code,
        }
        task = {"task_id": f"task-{code}", "project": "devmini-orchestrator", "blocker": issue["blocker"]}
        return orchestrator._workflow_policy_decision(
            issue,
            task,
            {"name": "devmini-orchestrator", "path": str(repo_root)},
        )

    tier3_action, _tier3_diag, tier3_policy = decision("review_feedback_loop")
    tier4_action, _tier4_diag, tier4_policy = decision("attempt_exhausted")
    env_action, _env_diag, env_policy = decision("environment_bypass_budget_exceeded")
    tier5_action, _tier5_diag, tier5_policy = decision("missing_child_unrecoverable")

    return {
        "all_codes_have_tier": all(code in orchestrator.BLOCKER_TIER for code in orchestrator.BLOCKER_CODES),
        "tier_3_stops_codex": tier3_action is None and tier3_policy == "tier_3_autonomy_reduction",
        "tier_4_opens_self_repair": tier4_action == "enqueue_self_repair_and_alert",
        "tier_4_alerts_telegram": env_action == "enqueue_self_repair_and_alert" and env_policy == "environment_bypass_budget_alert",
        "tier_5_cascades_to_children": tier5_action == "abandon_task" and tier5_policy == "tier_5_terminate",
    }


def main(argv):
    if len(argv) < 2 or len(argv) > 3:
        raise SystemExit("usage: harness/run_scenario.py <scenario-dir> | summary [runs-dir]")
    if argv[1] == "summary":
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        out = summarize_runs(repo_root, runs_dir=argv[2] if len(argv) == 3 else None)
        print(json.dumps(out, indent=2, sort_keys=True))
        return
    scenario_dir = pathlib.Path(argv[1]).resolve()
    scenario = _normalize_scenario_contract(json.loads((scenario_dir / "scenario.yaml").read_text()))
    expected = json.loads((scenario_dir / "expected.json").read_text())
    repo_root = scenario_dir.parents[2]
    trace_root = _new_trace_root(repo_root, scenario_dir)
    trace_root.mkdir(parents=True, exist_ok=True)
    os.environ["HARNESS_TRACE_ROOT"] = str(trace_root)
    kind = scenario["kind"]
    start_time = time.perf_counter()
    if kind == "attempt_cap":
        actual = _run_attempt_cap(repo_root, scenario_dir, scenario)
    elif kind == "fix2_reopen_after_manual_abandon":
        actual = _run_fix2_reopen(repo_root, scenario)
    elif kind == "r16_override":
        actual = _run_r16_override(repo_root, scenario)
    elif kind == "migration_forward_drift":
        actual = _run_migration_forward_drift(repo_root, scenario)
    elif kind == "migration_sha_mismatch":
        actual = _run_migration_sha_mismatch(repo_root, scenario)
    elif kind == "council_timeout":
        actual = _run_council_timeout(repo_root, scenario)
    elif kind == "self_repair_resolution":
        actual = _run_self_repair_resolution(repo_root, scenario)
    elif kind == "review_feedback_exhaustion":
        actual = _run_review_feedback_exhaustion(repo_root, scenario)
    elif kind == "issue_replan_cap":
        actual = _run_issue_replan_cap(repo_root, scenario)
    elif kind == "false_blocker_attach":
        actual = _run_false_blocker_attach(repo_root, scenario)
    elif kind == "qa_preflight":
        actual = _run_qa_preflight(repo_root, scenario)
    elif kind == "project_main_dirty_cap":
        actual = _run_project_main_dirty_cap(repo_root, scenario)
    elif kind == "regression_clear":
        actual = _run_regression_clear(repo_root, scenario)
    elif kind == "missing_child":
        actual = _run_missing_child(repo_root, scenario)
    elif kind == "canary_fallback":
        actual = _run_canary_fallback(repo_root, scenario)
    elif kind == "qa_contract_scoped":
        actual = _run_qa_contract_scoped(repo_root, scenario)
    elif kind == "qa_contract_full_tick":
        actual = _run_qa_contract_full_tick(repo_root, scenario)
    elif kind == "self_repair_issue_backfill":
        actual = _run_self_repair_issue_backfill(repo_root, scenario)
    elif kind == "state_engine_mirror":
        actual = _run_state_engine_mirror(repo_root, scenario)
    elif kind == "fs_to_engine_migration":
        actual = _run_fs_to_engine_migration(repo_root, scenario)
    elif kind == "atomic_claim_concurrency":
        actual = _run_atomic_claim_concurrency(repo_root, scenario)
    elif kind == "atomic_claim_concurrency_10":
        actual = _run_atomic_claim_concurrency_10(repo_root, scenario)
    elif kind == "kill9_integrity":
        actual = _run_kill9_integrity(repo_root, scenario)
    elif kind == "orphan_recovery_log":
        actual = _run_orphan_recovery_log(repo_root, scenario)
    elif kind == "environment_check_log":
        actual = _run_environment_check_log(repo_root, scenario)
    elif kind == "backup_roundtrip":
        actual = _run_backup_roundtrip(repo_root, scenario)
    elif kind == "wal_backup_restore":
        actual = _run_wal_backup_restore(repo_root, scenario)
    elif kind == "corrupt_db_fallback":
        actual = _run_corrupt_db_fallback(repo_root, scenario)
    elif kind == "disk_full_insert":
        actual = _run_disk_full_insert(repo_root, scenario)
    elif kind == "eio_read":
        actual = _run_eio_read(repo_root, scenario)
    elif kind == "db_deleted":
        actual = _run_db_deleted(repo_root, scenario)
    elif kind == "restore_active_rejected":
        actual = _run_restore_active_rejected(repo_root, scenario)
    elif kind == "wal_growth_stalls":
        actual = _run_wal_growth_stalls(repo_root, scenario)
    elif kind == "migration_partial":
        actual = _run_migration_partial(repo_root, scenario)
    elif kind == "migration_idempotence":
        actual = _run_migration_idempotence(repo_root, scenario)
    elif kind == "council_malformed":
        actual = _run_council_malformed(repo_root, scenario)
    elif kind == "fts5_recovery":
        actual = _run_fts5_recovery(repo_root, scenario)
    elif kind == "vector_rowid_divergence":
        actual = _run_vector_rowid_divergence(repo_root, scenario)
    elif kind == "observation_orphan":
        actual = _run_observation_orphan(repo_root, scenario)
    elif kind == "observation_idempotent":
        actual = _run_observation_idempotent(repo_root, scenario)
    elif kind == "clock_skew_backward":
        actual = _run_clock_skew_backward(repo_root, scenario)
    elif kind == "checkpoint_starved":
        actual = _run_checkpoint_starved(repo_root, scenario)
    elif kind == "metrics_rotation":
        actual = _run_metrics_rotation(repo_root, scenario)
    elif kind == "retention_purge_safe":
        actual = _run_retention_purge_safe(repo_root, scenario)
    elif kind == "fk_constraint_violation":
        actual = _run_fk_violation(repo_root, scenario)
    elif kind == "skill_prompt_injection":
        actual = _run_skill_prompt_injection(repo_root, scenario)
    elif kind == "council_deleted_task_ref":
        actual = _run_council_deleted_task_ref(repo_root, scenario)
    elif kind == "state_engine_reconnect_after_replace":
        actual = _run_state_engine_reconnect_after_replace(repo_root, scenario)
    elif kind == "clean_state_wipe_and_restart":
        actual = _run_clean_state_wipe_and_restart(repo_root, scenario)
    elif kind == "memory_hybrid_rrf":
        actual = _run_memory_hybrid(repo_root, scenario)
    elif kind == "memory_vec_missing_fallback":
        actual = _run_memory_vec_missing(repo_root, scenario)
    elif kind == "metrics_scale_1m":
        actual = _run_metrics_scale_1m(repo_root, scenario)
    elif kind == "transitions_scale_100k":
        actual = _run_transitions_scale_100k(repo_root, scenario)
    elif kind == "memory_obs_scale_10k":
        actual = _run_memory_obs_scale_10k(repo_root, scenario)
    elif kind == "feature_fanout_50":
        actual = _run_feature_fanout_50(repo_root, scenario)
    elif kind == "vec_dimension_mismatch":
        actual = _run_vec_dimension_mismatch(repo_root, scenario)
    elif kind == "vec_rebuild_complete":
        actual = _run_vec_rebuild_complete(repo_root, scenario)
    elif kind == "vec_version_drift":
        actual = _run_vec_version_drift(repo_root, scenario)
    elif kind == "fts5_shadow_corrupt":
        actual = _run_fts_shadow_corrupt(repo_root, scenario)
    elif kind == "fts_rebuild_during_search":
        actual = _run_fts_rebuild_during_search(repo_root, scenario)
    elif kind == "json_malformed_field":
        actual = _run_metadata_json_malformed(repo_root, scenario)
    elif kind == "timestamp_future_epoch":
        actual = _run_timestamp_future_epoch(repo_root, scenario)
    elif kind == "blocker_code_missing":
        actual = _run_blocker_code_missing(repo_root, scenario)
    elif kind == "dashboard_environment_reads_db":
        actual = _run_dashboard_environment_reads_db(repo_root, scenario)
    elif kind == "env_health_launchctl_fallback":
        actual = _run_env_health_launchctl_fallback(repo_root, scenario)
    elif kind == "planner_depends_on_ids":
        actual = _run_planner_depends_on_ids(repo_root, scenario)
    elif kind == "planner_depends_on_slice_alias":
        actual = _run_planner_depends_on_slice_alias(repo_root, scenario)
    elif kind == "patch_anchor_failures_trigger_topology":
        actual = _run_patch_anchor_failures_trigger_topology(repo_root, scenario)
    elif kind == "depends_on_context_prompts":
        actual = _run_depends_on_context_prompts(repo_root, scenario)
    elif kind == "braid_trailer_markdown_wrapped":
        actual = _run_braid_trailer_markdown_wrapped(repo_root, scenario)
    elif kind == "braid_refine_and_pr_wrapped":
        actual = _run_braid_refine_and_pr_wrapped(repo_root, scenario)
    elif kind == "council_payload_normalization":
        actual = _run_council_payload_normalization(repo_root, scenario)
    elif kind == "planner_output_normalization":
        actual = _run_planner_output_normalization(repo_root, scenario)
    elif kind == "end_to_end_handoff_contract":
        actual = _run_end_to_end_handoff_contract(repo_root, scenario)
    elif kind == "braid_result_json_envelope":
        actual = _run_braid_result_json_envelope(repo_root, scenario)
    elif kind == "braid_planner_refine_json_envelope":
        actual = _run_braid_planner_refine_json_envelope(repo_root, scenario)
    elif kind == "template_output_json_envelope":
        actual = _run_template_output_json_envelope(repo_root, scenario)
    elif kind == "claude_result_text_shared":
        actual = _run_claude_result_text_shared(repo_root, scenario)
    elif kind == "planner_refine_route":
        actual = _run_planner_refine_route(repo_root, scenario)
    elif kind == "template_refine_route_requires_template_context":
        actual = _run_template_refine_route_requires_template_context(repo_root, scenario)
    elif kind == "handoff_writer_contract_audit":
        actual = _run_handoff_writer_contract_audit(repo_root, scenario)
    elif kind == "planner_prompt_json_contract":
        actual = _run_planner_prompt_json_contract(repo_root, scenario)
    elif kind == "planner_fenced_json_normalization":
        actual = _run_planner_fenced_json_normalization(repo_root, scenario)
    elif kind == "feature_finalize_planner_live_no_children":
        actual = _run_feature_finalize_planner_live_no_children(repo_root, scenario)
    elif kind == "feature_finalize_untracked_live_no_children":
        actual = _run_feature_finalize_untracked_live_no_children(repo_root, scenario)
    elif kind == "feature_finalize_orphan_no_children":
        actual = _run_feature_finalize_orphan_no_children(repo_root, scenario)
    elif kind == "atomic_claim_skips_terminal_feature_children":
        actual = _run_atomic_claim_skips_terminal_feature_children(repo_root, scenario)
    elif kind == "review_feedback_reaper_stale_target":
        actual = _run_review_feedback_reaper_stale_target(repo_root, scenario)
    elif kind == "atomic_claim_feature_race_requeues_newer":
        actual = _run_atomic_claim_feature_race_requeues_newer(repo_root, scenario)
    elif kind == "health_payload_prefers_live_state":
        actual = _run_health_payload_prefers_live_state(repo_root, scenario)
    elif kind == "ull_lock_guard_findings":
        actual = _run_ull_lock_guard_findings(repo_root, scenario)
    elif kind == "circular_feature_lineage":
        actual = _run_circular_feature_lineage(repo_root, scenario)
    elif kind == "same_epoch_ordering":
        actual = _run_same_epoch_ordering(repo_root, scenario)
    elif kind == "epoch_2038":
        actual = _run_epoch_2038(repo_root, scenario)
    elif kind == "vacuum_bloat":
        actual = _run_vacuum_bloat(repo_root, scenario)
    elif kind == "events_rotation":
        actual = _run_events_rotation(repo_root, scenario)
    elif kind == "wal_nfs_contention":
        actual = _run_nfs_wal_contention_spec(repo_root, scenario)
    elif kind == "cross_process_claim":
        actual = _run_cross_process_claim(repo_root, scenario)
    elif kind == "allowlist_corrupt":
        actual = _run_allowlist_corrupt(repo_root, scenario)
    elif kind == "runner_fixture_restore":
        actual = _run_runner_fixture_restore(repo_root, scenario_dir, scenario)
    elif kind == "runner_trace_dirs":
        actual = _run_runner_trace_dirs(repo_root, scenario_dir, scenario)
    elif kind == "runner_version_budgets":
        actual = _run_runner_version_budgets(repo_root, scenario_dir, scenario)
    elif kind == "runner_summary":
        actual = _run_runner_summary(repo_root, scenario_dir, scenario)
    elif kind == "self_repair_observation":
        actual = _run_self_repair_observation(repo_root, scenario)
    elif kind == "telegram_surface":
        actual = _run_telegram_surface(repo_root, scenario)
    elif kind == "task_cost_capture":
        actual = _run_task_cost_capture(repo_root, scenario)
    elif kind == "supply_chain_gate":
        actual = _run_supply_chain_gate(repo_root, scenario)
    elif kind == "security_secret_gate":
        actual = _run_security_secret_gate(repo_root, scenario)
    elif kind == "security_secret_gate_ignored":
        actual = _run_security_secret_gate_ignored(repo_root, scenario)
    elif kind == "review_feedback_challenge":
        actual = _run_review_feedback_challenge(repo_root, scenario)
    elif kind == "untrusted_skill_refusal":
        actual = _run_untrusted_skill_refusal(repo_root, scenario)
    elif kind == "launchd_runtime_env_contract":
        actual = _run_launchd_runtime_env_contract(repo_root, scenario)
    elif kind == "template_contract_yaml_validates":
        actual = _run_template_contract_yaml_validates(repo_root, scenario)
    elif kind == "blocker_tier_routing":
        actual = _run_blocker_tier_routing(repo_root, scenario)
    elif kind == "self_repair_review_state_live":
        actual = _run_self_repair_review_state_live(repo_root, scenario)
    elif kind == "orchestrator_template_candidate_only":
        actual = _run_orchestrator_template_candidate_only(repo_root, scenario)
    elif kind == "telegram_health_dedupe":
        actual = _run_telegram_health_dedupe(repo_root, scenario)
    elif kind == "telegram_health_backoff":
        actual = _run_telegram_health_backoff(repo_root, scenario)
    elif kind == "template_owner_project":
        actual = _run_template_owner_project(repo_root, scenario)
    elif kind == "fetch_failure_cached_remote_ok":
        actual = _run_fetch_failure_cached_remote_ok(repo_root, scenario)
    elif kind == "planner_json_parse_retryable_block":
        actual = _run_planner_json_parse_retryable_block(repo_root, scenario)
    elif kind == "planner_slice_format_error_blocks":
        actual = _run_planner_depends_on_format_error_blocks(repo_root, scenario)
    elif kind == "classify_slice_misroute_enqueues_router_clarify":
        actual = _run_classify_slice_misroute_enqueues_router_clarify(repo_root, scenario)
    elif kind == "template_refine_missing_hash_blocks":
        actual = _run_template_refine_missing_hash_blocks(repo_root, scenario)
    elif kind == "review_feedback_loop_detection":
        actual = _run_review_feedback_loop_detection(repo_root, scenario)
    elif kind == "feature_abandon_cascades_children":
        actual = _run_feature_abandon_cascades_children(repo_root, scenario)
    elif kind == "review_feedback_blocks_on_inflight_target":
        actual = _run_review_feedback_blocks_on_inflight_target(repo_root, scenario)
    elif kind == "env_bypass_budget_triggers_self_repair":
        actual = _run_env_bypass_budget_triggers_self_repair(repo_root, scenario)
    elif kind == "planner_refine_missing_origin_context":
        actual = _run_planner_refine_missing_origin_context(repo_root, scenario)
    elif kind == "slice_alias_custom_id_not_hijacked":
        actual = _run_slice_alias_custom_id_not_hijacked(repo_root, scenario)
    elif kind == "slice_context_block_handles_null_plan":
        actual = _run_slice_context_block_handles_null_plan(repo_root, scenario)
    elif kind == "frontier_surfaces_blocked_dependency":
        actual = _run_frontier_surfaces_blocked_dependency(repo_root, scenario)
    elif kind == "workflow_e2e_story":
        actual = _run_workflow_e2e_story(repo_root, scenario)
    elif kind == "ull_lock_guard_retries_with_context":
        actual = _run_ull_lock_guard_retries_with_context(repo_root, scenario)
    else:
        raise SystemExit(f"unknown scenario kind: {kind}")

    elapsed_seconds = round(time.perf_counter() - start_time, 6)
    budget_report = _budget_report(actual, scenario, elapsed_seconds)
    trace_root = pathlib.Path(os.environ["HARNESS_TRACE_ROOT"]) if os.environ.get("HARNESS_TRACE_ROOT") else None
    if trace_root is not None:
        _write_json(trace_root / "scenario.json", scenario)
        _write_json(trace_root / "expected.json", expected)
        _write_json(trace_root / "actual.json", actual)
        _write_json(
            trace_root / "result.json",
            {
                "passed": actual == expected and not budget_report["wall_time_budget_exceeded"] and not budget_report["token_budget_exceeded"],
                "scenario_kind": kind,
                "budget_report": budget_report,
            },
        )
    budget_failed = budget_report["wall_time_budget_exceeded"] or budget_report["token_budget_exceeded"]
    if actual != expected or budget_failed:
        payload = {"expected": expected, "actual": actual, "budget_report": budget_report}
        print(json.dumps(payload, indent=2))
        raise SystemExit(1)
    print(json.dumps(actual, indent=2))


if __name__ == "__main__":
    main(sys.argv)

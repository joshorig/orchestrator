#!/usr/bin/env python3
import importlib.util
import json
import os
import pathlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
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
        captured = {}
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
    return {
        "ratio_findings": ratio_findings,
        "review_verdict": "approve" if not ratio_findings else "request_change",
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
        events = []
        transitions = []
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
            failed = orchestrator.read_json(orchestrator.task_path(task["task_id"], "failed"), {})
        finally:
            orchestrator.QUEUE_ROOT = old_orch["QUEUE_ROOT"]
            orchestrator.REPORT_DIR = old_orch["REPORT_DIR"]
            orchestrator._write_pr_alert = old_orch["_write_pr_alert"]
            worker.o = old_worker["o"]
        return {
            "state": failed.get("state"),
            "blocker_code": ((failed.get("blocker") or {}).get("code")),
            "retryable": ((failed.get("blocker") or {}).get("retryable")),
            "review_feedback_rounds": failed.get("review_feedback_rounds"),
            "failure": failed.get("failure"),
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
        alerts = []
        enqueued = []
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


def main(argv):
    if len(argv) != 2:
        raise SystemExit("usage: harness/run_scenario.py <scenario-dir>")
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
    elif kind == "memory_hybrid_rrf":
        actual = _run_memory_hybrid(repo_root, scenario)
    elif kind == "memory_vec_missing_fallback":
        actual = _run_memory_vec_missing(repo_root, scenario)
    elif kind == "runner_fixture_restore":
        actual = _run_runner_fixture_restore(repo_root, scenario_dir, scenario)
    elif kind == "runner_trace_dirs":
        actual = _run_runner_trace_dirs(repo_root, scenario_dir, scenario)
    elif kind == "runner_version_budgets":
        actual = _run_runner_version_budgets(repo_root, scenario_dir, scenario)
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
    elif kind == "untrusted_skill_refusal":
        actual = _run_untrusted_skill_refusal(repo_root, scenario)
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

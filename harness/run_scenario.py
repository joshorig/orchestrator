#!/usr/bin/env python3
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading


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
        shutil_copy(repo_root / "config" / "orchestrator.example.json", root / "config" / "orchestrator.example.json")
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
            "integrity_check": out["integrity_check"],
        }


def _run_atomic_claim_concurrency(repo_root, scenario):
    orchestrator, _ = _load_repo_modules(repo_root)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        queue_root = root / "queue"
        claims_dir = root / "state" / "runtime" / "claims"
        for state in orchestrator.STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        claims_dir.mkdir(parents=True, exist_ok=True)
        old = {
            "QUEUE_ROOT": orchestrator.QUEUE_ROOT,
            "RUNTIME_DIR": orchestrator.RUNTIME_DIR,
            "CLAIMS_DIR": orchestrator.CLAIMS_DIR,
            "now_iso": orchestrator.now_iso,
            "project_environment_ok": orchestrator.project_environment_ok,
            "project_hard_stopped": orchestrator.project_hard_stopped,
            "load_braid_index": orchestrator.load_braid_index,
            "crash_loop_guard_status": orchestrator.crash_loop_guard_status,
            "slot_paused": orchestrator.slot_paused,
        }
        orchestrator.QUEUE_ROOT = queue_root
        orchestrator.RUNTIME_DIR = root / "state" / "runtime"
        orchestrator.CLAIMS_DIR = claims_dir
        orchestrator.now_iso = lambda: "2026-04-19T20:10:00"
        orchestrator.project_environment_ok = lambda *args, **kwargs: True
        orchestrator.project_hard_stopped = lambda *args, **kwargs: False
        orchestrator.load_braid_index = lambda: {}
        orchestrator.crash_loop_guard_status = lambda *args, **kwargs: {"suppressed": False, "crashes": [], "window_seconds": 180, "max_crashes": 2}
        orchestrator.slot_paused = lambda *args, **kwargs: None
        try:
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


def main(argv):
    if len(argv) != 2:
        raise SystemExit("usage: harness/run_scenario.py <scenario-dir>")
    scenario_dir = pathlib.Path(argv[1]).resolve()
    scenario = json.loads((scenario_dir / "scenario.yaml").read_text())
    expected = json.loads((scenario_dir / "expected.json").read_text())
    repo_root = scenario_dir.parents[2]
    kind = scenario["kind"]
    if kind == "attempt_cap":
        actual = _run_attempt_cap(repo_root, scenario_dir, scenario)
    elif kind == "fix2_reopen_after_manual_abandon":
        actual = _run_fix2_reopen(repo_root, scenario)
    elif kind == "r16_override":
        actual = _run_r16_override(repo_root, scenario)
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
    else:
        raise SystemExit(f"unknown scenario kind: {kind}")

    if actual != expected:
        print(json.dumps({"expected": expected, "actual": actual}, indent=2))
        raise SystemExit(1)
    print(json.dumps(actual, indent=2))


if __name__ == "__main__":
    main(sys.argv)

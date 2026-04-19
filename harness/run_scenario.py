#!/usr/bin/env python3
import importlib.util
import json
import pathlib
import sys
import tempfile


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

            ratio_cfg = {
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
    else:
        raise SystemExit(f"unknown scenario kind: {kind}")

    if actual != expected:
        print(json.dumps({"expected": expected, "actual": actual}, indent=2))
        raise SystemExit(1)
    print(json.dumps(actual, indent=2))


if __name__ == "__main__":
    main(sys.argv)

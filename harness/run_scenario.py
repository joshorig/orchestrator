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


def main(argv):
    if len(argv) != 2:
        raise SystemExit("usage: harness/run_scenario.py <scenario-dir>")
    scenario_dir = pathlib.Path(argv[1]).resolve()
    scenario = json.loads((scenario_dir / "scenario.yaml").read_text())
    expected = json.loads((scenario_dir / "expected.json").read_text())
    mock = _load_module("mock_reviewer", scenario_dir / "mock_reviewer.py")

    repo_root = scenario_dir.parents[2]
    orchestrator = _load_module("orchestrator", repo_root / "bin" / "orchestrator.py")
    worker = _load_module("worker", repo_root / "bin" / "worker.py")

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
                source="scenario-19",
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

            ratio_cfg = {"review_policy": {"test_to_code_ratio": {"min_ratio": 0.5, "accept_doctest": False, "template_overrides": {}}}}
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
                source="scenario-19",
            )
        finally:
            orchestrator.QUEUE_ROOT = old["QUEUE_ROOT"]
            orchestrator.REPORT_DIR = old["REPORT_DIR"]
            orchestrator.should_push_alert = old["should_push_alert"]
            orchestrator.max_task_attempts = old["max_task_attempts"]

        alerts = list((root / "reports").glob("workflow-alert_*.md"))
        actual = {
            "ratio_findings": ratio_findings,
            "final_state": out["state"],
            "blocker_code": (out.get("blocker") or {}).get("code"),
            "alert_count": len(alerts),
            "review_verdict": out.get("review_verdict"),
            "policy_review_findings": out.get("policy_review_findings") or [],
        }

    if actual != expected:
        print(json.dumps({"expected": expected, "actual": actual}, indent=2))
        raise SystemExit(1)
    print(json.dumps(actual, indent=2))


if __name__ == "__main__":
    main(sys.argv)

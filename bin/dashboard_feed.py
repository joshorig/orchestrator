#!/usr/bin/env python3
"""Build a live JSON feed for the orchestrator dashboard."""

from __future__ import annotations

import json
import os
import pathlib
import statistics
import sys
import datetime as dt
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orchestrator as o  # noqa: E402


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _state_engine():
    cfg = o.load_config()
    if o.state_engine_config(cfg=cfg).get("mode") == "off":
        return None
    engine = o.get_state_engine(cfg=cfg)
    engine.initialize()
    return engine


def _conn():
    engine = _state_engine()
    if not engine:
        return None, None
    return engine, engine.connect()


def _read_task_log(task_id, *, tail_lines=80, max_chars=12000):
    log_path = o.LOGS_DIR / f"{task_id}.log"
    if not log_path.exists():
        return {"path": None, "tail": []}
    text = log_path.read_text(errors="replace")
    tail = text.splitlines()[-tail_lines:]
    if sum(len(line) for line in tail) > max_chars:
        joined = "\n".join(tail)
        tail = joined[-max_chars:].splitlines()
    return {"path": str(log_path), "tail": tail}


def _task_snapshot(task_id, state, task):
    task = dict(task or {})
    blocker = task.get("blocker") or o.task_blocker(task) or {}
    transitions = o.read_transitions(task_id=task_id, limit=6)
    log_info = _read_task_log(task_id)
    return {
        "task_id": task_id,
        "state": state,
        "role": task.get("role"),
        "engine": task.get("engine"),
        "project": task.get("project"),
        "feature_id": task.get("feature_id"),
        "parent_task_id": task.get("parent_task_id"),
        "summary": task.get("summary"),
        "source": task.get("source"),
        "attempt": _safe_int(task.get("attempt"), 1),
        "base_branch": task.get("base_branch"),
        "braid_template": task.get("braid_template"),
        "created_at": task.get("created_at"),
        "claimed_at": task.get("claimed_at"),
        "started_at": task.get("started_at"),
        "reviewed_at": task.get("reviewed_at"),
        "qa_passed_at": task.get("qa_passed_at"),
        "pushed_at": task.get("pushed_at"),
        "pr_created_at": task.get("pr_created_at"),
        "review_requested_at": task.get("review_requested_at"),
        "finished_at": task.get("finished_at"),
        "worktree": task.get("worktree"),
        "blocker": blocker,
        "failure": task.get("failure"),
        "qa_failure": task.get("qa_failure"),
        "push_failure": task.get("push_failure"),
        "deploy_failure": task.get("deploy_failure"),
        "review_verdict": task.get("review_verdict"),
        "reviewed_by": task.get("reviewed_by"),
        "policy_review_findings": list(task.get("policy_review_findings") or []),
        "review_feedback_rounds": _safe_int(task.get("review_feedback_rounds"), 0),
        "review_gates": list(task.get("review_gates") or []),
        "review_gate_panels": dict(task.get("review_gate_panels") or {}),
        "review_council_panel": list(task.get("review_council_panel") or []),
        "resolved_thread_count": _safe_int(task.get("resolved_thread_count"), 0),
        "resolve_thread_failures": _safe_int(task.get("resolve_thread_failures"), 0),
        "resolved_thread_evidence": list(task.get("resolved_thread_evidence") or []),
        "qa_gate_output": task.get("qa_gate_output"),
        "pr_number": task.get("pr_number"),
        "pr_url": task.get("pr_url"),
        "pr_body_path": task.get("pr_body_path"),
        "push_commit_sha": task.get("push_commit_sha"),
        "push_commit_count": _safe_int(task.get("push_commit_count"), 0),
        "push_branch": task.get("push_branch"),
        "push_base_branch": task.get("push_base_branch"),
        "pr_create_failure": task.get("pr_create_failure"),
        "review_request_error": task.get("review_request_error"),
        "pr_sweep": dict(task.get("pr_sweep") or {}),
        "local_deploy": dict(task.get("local_deploy") or {}),
        "log_path": log_info["path"],
        "log_tail": log_info["tail"],
        "artifacts_count": len(task.get("artifacts") or []),
        "transitions": transitions,
    }


def _feature_task_rows(feature_id, child_states, follow_up_states):
    rows = []
    seen = set()
    for mapping, kind in ((child_states, "child"), (follow_up_states, "follow_up")):
        for task_id, state in mapping.items():
            if not task_id or task_id in seen:
                continue
            seen.add(task_id)
            found = o.find_task(task_id)
            task = found[1] if found else None
            effective_state = found[0] if found else state
            row = _task_snapshot(task_id, effective_state, task)
            row["kind"] = kind
            row["is_frontier"] = False
            rows.append(row)
    return rows


def _feature_roadmap(feature):
    roadmap_entry_id = feature.get("roadmap_entry_id")
    source = feature.get("source") or ""
    summary = feature.get("summary") or ""
    title = summary
    if roadmap_entry_id and summary.startswith(f"[{roadmap_entry_id}] "):
        title = summary[len(roadmap_entry_id) + 3 :]
    return {
        "id": roadmap_entry_id,
        "title": title,
        "source": source,
    }


def _health():
    payload = o._health_payload()
    return {
        "environment_ok": payload["environment_ok"],
        "environment_error_count": payload["environment_error_count"],
        "workflow_check_issue_count": payload["workflow_check_issue_count"],
        "feature_open_count": payload["feature_open_count"],
        "feature_frontier_blocked_count": payload["feature_frontier_blocked_count"],
    }


def _agents():
    rows = []
    for row in o.effective_agent_statuses():
        rows.append(
            {
                "role": row.get("role"),
                "status": row.get("status"),
                "detail": row.get("detail"),
                "updated_at": row.get("updated_at"),
            }
        )
    return rows


def _queue():
    engine = _state_engine()
    counts = engine.queue_state_counts() if engine else o.queue_counts()
    return [{"state": state, "count": count} for state, count in counts.items()]


def _features():
    engine = _state_engine()
    rows = []
    for wf in o.open_feature_workflow_summaries():
        feature = o.read_feature(wf.get("feature_id")) or {}
        frontier = wf.get("frontier") or {}
        blocker = frontier.get("blocker") or {}
        self_repair = dict(wf.get("self_repair") or {})
        issues = list(self_repair.get("issues") or [])
        tasks = _feature_task_rows(
            wf.get("feature_id"),
            wf.get("child_states") or {},
            wf.get("follow_up_states") or {},
        )
        frontier_task_id = frontier.get("task_id")
        for row in tasks:
            if row["task_id"] == frontier_task_id:
                row["is_frontier"] = True
                break
        rows.append(
            {
                "feature_id": wf.get("feature_id"),
                "project": wf.get("project"),
                "status": wf.get("feature_status"),
                "summary": wf.get("summary"),
                "created_at": feature.get("created_at"),
                "branch": feature.get("branch"),
                "roadmap": _feature_roadmap(feature),
                "delivery": {
                    "final_pr_number": feature.get("final_pr_number"),
                    "final_pr_url": feature.get("final_pr_url"),
                    "finalized_at": feature.get("finalized_at"),
                    "merged_at": feature.get("merged_at"),
                    "finalize_error": feature.get("finalize_error"),
                    "final_pr_sweep": feature.get("final_pr_sweep") or {},
                },
                "planner": wf.get("planner") or {},
                "frontier": {
                    "task_id": frontier.get("task_id"),
                    "state": frontier.get("state"),
                    "entered_at": frontier.get("entered_at"),
                    "reason": frontier.get("reason"),
                    "attempt": frontier.get("attempt"),
                    "age_seconds": frontier.get("age_seconds"),
                    "age_text": frontier.get("age_text"),
                    "blocker": blocker,
                },
                "child_states": wf.get("child_states") or {},
                "follow_up_states": wf.get("follow_up_states") or {},
                "recent_events": wf.get("recent_events") or [],
                "repair_history": wf.get("repair_history") or [],
                "workflow_check": wf.get("workflow_check") or {},
                "canary": wf.get("canary") or {},
                "memory_observations": _project_memory_observations(engine, wf.get("project")),
                "tasks": tasks,
                "self_repair": {
                    "enabled": bool(self_repair.get("enabled")),
                    "status": (issues[0] if issues else {}).get("status") or self_repair.get("status"),
                    "issue_count": len(issues),
                    "issues": issues,
                    "deliberations": [
                        deliberation
                        for issue in issues[:2]
                        for deliberation in list(issue.get("deliberations") or [])[-3:]
                    ][-6:],
                },
            }
        )
    return rows


def _project_memory_observations(engine, project):
    if not engine or not project:
        return {"count": 0, "latest": None}
    count = engine.memory_count(project=project)
    if count <= 0:
        return {"count": 0, "latest": None}
    latest = None
    try:
        rows = engine.connect().execute(
            """
            SELECT title, created_at
              FROM memory_observations
             WHERE project = ?
             ORDER BY created_at_epoch DESC, id DESC
             LIMIT 1
            """,
            (project,),
        ).fetchall()
        if rows:
            latest = {
                "title": rows[0]["title"],
                "updated_at": rows[0]["created_at"],
            }
    except Exception:
        latest = None
    return {"count": count, "latest": latest}


def _blocker_codes():
    engine = _state_engine()
    counts = {}
    total = 0
    rows = engine.read_tasks(states=("blocked",)) if engine else None
    if rows is None:
        rows = []
        for path in o.queue_dir("blocked").glob("*.json"):
            rows.append(o.read_json(path, {}) or {})
    for task in rows:
        blocker = o.task_blocker(task or {}) or {}
        code = blocker.get("code") or "unknown"
        counts[code] = counts.get(code, 0) + 1
        total += 1
    rows = []
    for code, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        rows.append(
            {
                "code": code,
                "count": count,
                "pct_of_blocked": round((count / total) * 100.0, 1) if total else 0.0,
            }
        )
    return rows


def _recent_transitions():
    return [
        {
            "ts": row.get("ts"),
            "task_id": row.get("task_id"),
            "from_state": row.get("from_state"),
            "to_state": row.get("to_state"),
            "reason": row.get("reason"),
        }
        for row in o.read_transitions(limit=50)
    ]


def _claude_budget():
    cfg = o.load_config()
    budgets = cfg.get("budgets") or {}
    configured = {
        "ask": float(budgets.get("ask_usd", o.claude_budget_usd("ask", cfg=cfg))),
        "review": float(budgets.get("review_usd", o.claude_budget_usd("review", cfg=cfg))),
        "planner": float(budgets.get("planner_usd", o.claude_budget_usd("planner", cfg=cfg))),
        "template_gen": float(budgets.get("template_gen_usd", o.claude_budget_usd("template_gen", cfg=cfg))),
        "template_refine": float(budgets.get("template_refine_usd", o.claude_budget_usd("template_refine", cfg=cfg))),
        "memory_synthesis": float(budgets.get("memory_synthesis_usd", o.claude_budget_usd("memory_synthesis", cfg=cfg))),
        "self_repair": float(budgets.get("self_repair_usd", o.claude_budget_usd("planner", cfg=cfg, mode="self-repair-plan"))),
    }
    since = dt.datetime.now() - dt.timedelta(hours=24)
    counts = {}
    recent = []
    for row in o.read_metrics(name="claude.budget_exhausted", limit=500):
        try:
            ts = dt.datetime.fromisoformat(row.get("ts") or "")
        except ValueError:
            continue
        if ts.tzinfo is not None:
            ts = ts.astimezone().replace(tzinfo=None)
        if ts < since:
            continue
        tags = row.get("tags") or {}
        lane = tags.get("lane") or "unknown"
        counts[lane] = counts.get(lane, 0) + int(row.get("value", 0) or 0)
        recent.append(
            {
                "ts": row.get("ts"),
                "lane": lane,
                "project": tags.get("project") or "",
                "role": tags.get("role") or "",
            }
        )
    return {
        "configured": configured,
        "hits_24h": counts,
        "recent_hits": recent[-12:],
    }


def _dashboard_server():
    cfg = o.dashboard_server_config()
    engine = _state_engine()
    return {
        "host": cfg["host"],
        "port": cfg["port"],
        "allowed_cidrs": list(cfg["allowed_cidrs"]),
        "dashboard_url": f"http://{cfg['host']}:{cfg['port']}/",
        "state_engine": (engine.status() if engine else {"enabled": False, "mode": "off"}),
    }


def _task_costs():
    engine, conn = _conn()
    if not engine or not conn:
        return {
            "window_hours": 24,
            "summary": {},
            "by_engine": [],
            "by_project": [],
            "by_template": [],
            "recent": [],
            "template_success": [],
        }
    payload = engine.aggregate_task_costs(hours=24, conn=conn)
    since_epoch = int(dt.datetime.now(dt.timezone.utc).timestamp()) - 24 * 3600
    project_rows = conn.execute(
        """
        SELECT
            COALESCE(NULLIF(t.project, ''), 'unknown') AS project,
            COUNT(*) AS rows_count,
            COUNT(DISTINCT c.task_id) AS task_count,
            COALESCE(SUM(c.cost_usd), 0.0) AS cost_usd
          FROM task_costs AS c
          LEFT JOIN tasks AS t ON t.task_id = c.task_id
         WHERE c.ts_epoch >= ?
         GROUP BY 1
         ORDER BY cost_usd DESC, project ASC
         LIMIT 8
        """,
        (since_epoch,),
    ).fetchall()
    template_rows = conn.execute(
        """
        SELECT
            COALESCE(NULLIF(t.braid_template, ''), 'untemplated') AS braid_template,
            COUNT(*) AS rows_count,
            COUNT(DISTINCT c.task_id) AS task_count,
            COALESCE(SUM(c.cost_usd), 0.0) AS cost_usd
          FROM task_costs AS c
          LEFT JOIN tasks AS t ON t.task_id = c.task_id
         WHERE c.ts_epoch >= ?
         GROUP BY 1
         ORDER BY cost_usd DESC, braid_template ASC
         LIMIT 8
        """,
        (since_epoch,),
    ).fetchall()
    template_success_rows = conn.execute(
        """
        SELECT
            COALESCE(NULLIF(braid_template, ''), 'untemplated') AS braid_template,
            COUNT(*) AS finished_count,
            SUM(
                CASE
                    WHEN state = 'done'
                     AND attempt = 1
                     AND COALESCE(json_extract(metadata_json, '$.review_verdict'), '') = 'approve'
                    THEN 1 ELSE 0
                END
            ) AS one_shot_successes
          FROM tasks
         WHERE finished_at_epoch IS NOT NULL
           AND finished_at_epoch >= ?
         GROUP BY 1
         ORDER BY finished_count DESC, braid_template ASC
         LIMIT 8
        """,
        (since_epoch,),
    ).fetchall()
    payload["by_project"] = [
        {
            "project": row["project"],
            "rows_count": int(row["rows_count"] or 0),
            "task_count": int(row["task_count"] or 0),
            "cost_usd": float(row["cost_usd"] or 0.0),
        }
        for row in project_rows
    ]
    payload["by_template"] = [
        {
            "braid_template": row["braid_template"],
            "rows_count": int(row["rows_count"] or 0),
            "task_count": int(row["task_count"] or 0),
            "cost_usd": float(row["cost_usd"] or 0.0),
        }
        for row in template_rows
    ]
    payload["template_success"] = [
        {
            "braid_template": row["braid_template"],
            "finished_count": int(row["finished_count"] or 0),
            "one_shot_successes": int(row["one_shot_successes"] or 0),
            "one_shot_success_rate": round(
                (int(row["one_shot_successes"] or 0) / max(int(row["finished_count"] or 0), 1)) * 100.0,
                1,
            ),
        }
        for row in template_success_rows
    ]
    return payload


def _runtime_audit():
    engine = _state_engine()
    if not engine:
        return {
            "environment_checks": [],
            "orphan_recoveries": [],
            "task_bypasses": [],
        }
    return {
        "environment_checks": engine.read_environment_checks()[-12:],
        "orphan_recoveries": engine.read_orphan_recoveries()[-12:],
        "task_bypasses": engine.read_task_bypasses()[-12:],
    }


def _skills():
    cfg = o.load_config()
    trusted = list(o.skills_config(cfg=cfg)["trusted_skills"])
    engine, conn = _conn()
    scans = []
    if o.AGENT_SCAN_DIR.exists():
        scans = sorted(o.AGENT_SCAN_DIR.glob("skills-audit-*.json"))[-8:]
    latest_scan = None
    if scans:
        try:
            latest_scan = json.loads(scans[-1].read_text())
        except Exception:
            latest_scan = None
    scan_counts = (latest_scan or {}).get("counts") or {}
    recent_events = []
    usage_counter = Counter()
    token_savior_count = 0
    skill_tasks = defaultdict(set)
    last_used_at = {}
    since_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    for row in o.read_events(role="skills", limit=500):
        event = row.get("event")
        details = row.get("details") or {}
        try:
            row_dt = dt.datetime.fromisoformat(str(row.get("ts") or "").replace("Z", "+00:00"))
        except Exception:
            row_dt = None
        if row_dt and row_dt.tzinfo is None:
            row_dt = row_dt.replace(tzinfo=dt.timezone.utc)
        if row_dt and row_dt < since_dt:
            continue
        if event == "skill_context_used":
            for name in details.get("skills") or []:
                usage_counter[name] += 1
                if row.get("task_id"):
                    skill_tasks[name].add(row.get("task_id"))
                last_used_at[name] = max(str(row.get("ts") or ""), last_used_at.get(name, ""))
            recent_events.append(
                {
                    "ts": row.get("ts"),
                    "event": event,
                    "task_id": row.get("task_id"),
                    "project": details.get("project"),
                    "skills": list(details.get("skills") or []),
                    "gate_name": details.get("gate_name"),
                }
            )
        elif event == "token_savior_used":
            token_savior_count += 1
            recent_events.append(
                {
                    "ts": row.get("ts"),
                    "event": event,
                    "task_id": row.get("task_id"),
                    "project": details.get("project"),
                    "role": details.get("role"),
                    "sections": list(details.get("sections") or []),
                }
            )
    task_cost_by_task = {}
    if engine and conn:
        for row in engine.read_task_costs(limit=500, conn=conn):
            ts = str(row.get("ts") or "")
            try:
                row_dt = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                row_dt = None
            if row_dt and row_dt.tzinfo is None:
                row_dt = row_dt.replace(tzinfo=dt.timezone.utc)
            if row_dt and row_dt < since_dt:
                continue
            task_id = row.get("task_id")
            if not task_id:
                continue
            task_cost_by_task[task_id] = task_cost_by_task.get(task_id, 0.0) + float(row.get("cost_usd") or 0.0)
    rows = []
    for item in trusted:
        name = item.get("name")
        pass_count = 0
        fail_count = 0
        error_count = 0
        pending_count = 0
        costs = []
        projects = Counter()
        for task_id in sorted(skill_tasks.get(name) or []):
            task = o.find_task(task_id)
            if not task:
                error_count += 1
                continue
            state = str(task.get("state") or "")
            projects[str(task.get("project") or "unknown")] += 1
            if state == "done":
                pass_count += 1
            elif state == "failed":
                fail_count += 1
            elif state in {"abandoned", "blocked"}:
                error_count += 1
            else:
                pending_count += 1
            if task_id in task_cost_by_task:
                costs.append(float(task_cost_by_task[task_id]))
        median_cost = statistics.median(costs) if costs else 0.0
        total_outcomes = pass_count + fail_count + error_count
        rows.append(
            {
                "name": name,
                "source": item.get("source"),
                "sha": item.get("sha"),
                "upstream_path": item.get("upstream_path"),
                "scan_status": "accepted" if latest_scan and latest_scan.get("accepted") else ("unknown" if not latest_scan else "rejected"),
                "scan_counts": scan_counts,
                "usage_7d": usage_counter.get(name, 0),
                "pass_count": pass_count,
                "fail_count": fail_count,
                "error_count": error_count,
                "pending_count": pending_count,
                "median_cost_usd": round(median_cost, 4),
                "project_mix": [{"project": project, "count": count} for project, count in projects.most_common(3)],
                "last_used_at": last_used_at.get(name),
                "pass_rate": round((pass_count / total_outcomes) * 100.0, 1) if total_outcomes else 0.0,
            }
        )
    rows.sort(key=lambda row: (-int(row.get("usage_7d") or 0), row.get("name") or ""))
    return {
        "count": len(rows),
        "latest_scan_at": (latest_scan or {}).get("scanned_at"),
        "latest_scan_counts": scan_counts,
        "token_savior_usage_7d": token_savior_count,
        "recent_usage": recent_events[-12:],
        "skills": rows,
    }


def _harness():
    scenarios = sorted((o.STATE_ROOT / "harness" / "scenarios").glob("*/scenario.yaml"))
    summary = {"total": {"scenarios": len(scenarios), "passed": 0, "failed": 0, "pass_rate": 0.0}, "clusters": {}}
    runs_root = o.STATE_ROOT / "harness" / "runs"
    if runs_root.exists():
        try:
            sys.path.insert(0, str(o.STATE_ROOT / "harness"))
            import run_scenario  # type: ignore
            summary = run_scenario.summarize_runs(o.STATE_ROOT, runs_dir=runs_root)
        except Exception:
            pass
    recent = []
    if runs_root.exists():
        for result_path in sorted(runs_root.glob("*/**/result.json"))[-6:]:
            try:
                result = json.loads(result_path.read_text())
                recent.append(
                    {
                        "scenario": result_path.parent.name,
                        "passed": bool(result.get("passed")),
                        "wall_time_seconds": float((result.get("budget_report") or {}).get("wall_time_seconds") or 0.0),
                        "timestamp": result.get("completed_at") or result.get("started_at"),
                    }
                )
            except Exception:
                continue
    return {
        "total_scenarios": len(scenarios),
        "summary": summary.get("total") or {},
        "clusters": summary.get("clusters") or {},
        "recent": recent[-6:],
    }


def _telegram_log():
    rows = []
    reject_path = o.LOGS_DIR / "telegram-reject.log"
    if reject_path.exists():
        for line in reject_path.read_text(errors="replace").splitlines()[-8:]:
            parts = line.split("\t", 3)
            if len(parts) >= 4:
                rows.append({"ts": parts[0], "kind": "reject", "cmd": parts[3], "result": parts[2]})
    for path in sorted(o.REPORT_DIR.glob("workflow-check_*.md"))[-4:]:
        rows.append({"ts": dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"), "kind": "workflow", "cmd": path.name, "result": "workflow-check report"})
    for path in sorted(o.REPORT_DIR.glob("workflow-alert_*.md"))[-4:]:
        rows.append({"ts": dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"), "kind": "push", "cmd": path.name, "result": "workflow alert"})
    rows.sort(key=lambda row: row.get("ts") or "", reverse=True)
    return rows[:12]


def _environment_panel(runtime_audit, dashboard_server):
    status = (dashboard_server.get("state_engine") or {}) if dashboard_server else {}
    checks_by_project = defaultdict(list)
    for row in runtime_audit.get("environment_checks") or []:
        checks_by_project[row.get("project") or "global"].append(row)
    projects = []
    for project, entries in sorted(checks_by_project.items()):
        latest = entries[-1]
        projects.append(
            {
                "project": project,
                "result": latest.get("result"),
                "blocker_summary": latest.get("blocker_summary"),
                "checked_at": latest.get("ts"),
            }
        )
    return {
        "projects": projects,
        "migrations": list((status.get("applied_migrations") or [])),
        "integrity_check": status.get("integrity_check"),
        "mode": status.get("mode"),
        "db_path": status.get("db_path"),
    }


def _fs_fallback():
    engine, conn = _conn()
    if not engine or not conn:
        return {"event_count": 0, "hours_since_last": None, "last_fallback_at": None, "streak_anchor_at": None}
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS event_count,
            MAX(created_at_epoch) AS last_fallback_epoch,
            MIN(created_at_epoch) AS first_fallback_epoch
          FROM metrics
         WHERE name = 'state_engine.fs_fallback'
        """
    ).fetchone()
    any_metric = conn.execute("SELECT MIN(created_at_epoch) AS first_metric_epoch FROM metrics").fetchone()
    now_epoch = int(dt.datetime.now(dt.timezone.utc).timestamp())
    last_epoch = int(row["last_fallback_epoch"] or 0) if row else 0
    anchor_epoch = last_epoch or int(any_metric["first_metric_epoch"] or 0) if any_metric else 0
    hours_since_last = round((now_epoch - anchor_epoch) / 3600.0, 1) if anchor_epoch else None
    return {
        "event_count": int(row["event_count"] or 0) if row else 0,
        "last_fallback_at": dt.datetime.fromtimestamp(last_epoch, tz=dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") if last_epoch else None,
        "hours_since_last": hours_since_last,
        "streak_anchor_at": dt.datetime.fromtimestamp(anchor_epoch, tz=dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") if anchor_epoch else None,
    }


def _review_gate_status(features):
    tasks = [task for feature in features for task in feature.get("tasks") or []]
    awaiting_review = sum(1 for task in tasks if task.get("state") == "awaiting-review")
    request_change = sum(1 for task in tasks if task.get("review_verdict") == "request_change")
    approve = sum(1 for task in tasks if task.get("review_verdict") == "approve")
    thread_failures = sum(_safe_int(task.get("resolve_thread_failures"), 0) for task in tasks)
    policy_findings = sum(len(task.get("policy_review_findings") or []) for task in tasks)
    gate_counter = Counter()
    recent = []
    for task in tasks:
        for gate in task.get("review_gates") or []:
            gate_counter[gate] += 1
        if task.get("review_verdict") or task.get("state") == "awaiting-review":
            recent.append(task)
    recent.sort(key=lambda task: str(task.get("reviewed_at") or task.get("started_at") or task.get("created_at") or ""), reverse=True)
    return {
        "awaiting_review": awaiting_review,
        "request_change": request_change,
        "approve": approve,
        "thread_failures": thread_failures,
        "policy_findings": policy_findings,
        "top_gates": [{"gate": gate, "count": count} for gate, count in gate_counter.most_common(6)],
        "recent": recent[:6],
    }


def _observation_window(features):
    issues = []
    for feature in features:
        for issue in (feature.get("self_repair") or {}).get("issues") or []:
            if not issue.get("observation_due_at"):
                continue
            due = issue.get("observation_due_at")
            try:
                due_dt = dt.datetime.fromisoformat(due.replace("Z", "+00:00"))
                now_dt = dt.datetime.now(dt.timezone.utc)
                remaining = int((due_dt - now_dt).total_seconds())
            except Exception:
                remaining = None
            issues.append(
                {
                    "feature_id": feature.get("feature_id"),
                    "issue_key": issue.get("issue_key"),
                    "status": issue.get("status"),
                    "observation_status": issue.get("observation_status"),
                    "observation_due_at": due,
                    "remaining_seconds": remaining,
                    "task_id": ((issue.get("observation_target") or {}).get("task_id")),
                    "blocker_code": ((issue.get("observation_target") or {}).get("blocker_code")),
                }
            )
    issues.sort(key=lambda row: (row["remaining_seconds"] is None, row["remaining_seconds"] if row["remaining_seconds"] is not None else 10**12))
    return {"active_count": len(issues), "issues": issues[:8]}


def _memory_observations():
    engine, conn = _conn()
    if not engine or not conn:
        return {"total_count": 0, "by_project": [], "token_savior": {"usage_7d": 0, "by_project": [], "recent": []}}
    rows = conn.execute(
        """
        SELECT
            COALESCE(NULLIF(project, ''), 'unknown') AS project,
            COUNT(*) AS obs_count,
            MAX(created_at) AS latest_at
          FROM memory_observations
         GROUP BY 1
         ORDER BY obs_count DESC, project ASC
         LIMIT 8
        """
    ).fetchall()
    total = engine.memory_count()
    since_dt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)
    token_savior_by_project = Counter()
    token_savior_sections = Counter()
    token_savior_recent = []
    for row in o.read_events(role="skills", limit=500):
        if row.get("event") != "token_savior_used":
            continue
        try:
            row_dt = dt.datetime.fromisoformat(str(row.get("ts") or "").replace("Z", "+00:00"))
        except Exception:
            row_dt = None
        if row_dt and row_dt.tzinfo is None:
            row_dt = row_dt.replace(tzinfo=dt.timezone.utc)
        if row_dt and row_dt < since_dt:
            continue
        details = row.get("details") or {}
        project = str(details.get("project") or "unknown")
        token_savior_by_project[project] += 1
        for section in details.get("sections") or []:
            token_savior_sections[str(section)] += 1
        token_savior_recent.append(
            {
                "ts": row.get("ts"),
                "project": project,
                "task_id": row.get("task_id"),
                "role": details.get("role"),
                "sections": list(details.get("sections") or []),
            }
        )
    return {
        "total_count": total,
        "by_project": [
            {
                "project": row["project"],
                "count": int(row["obs_count"] or 0),
                "latest_at": row["latest_at"],
            }
            for row in rows
        ],
        "token_savior": {
            "usage_7d": sum(token_savior_by_project.values()),
            "by_project": [
                {"project": project, "count": count}
                for project, count in token_savior_by_project.most_common(6)
            ],
            "top_sections": [
                {"section": section, "count": count}
                for section, count in token_savior_sections.most_common(6)
            ],
            "recent": token_savior_recent[-8:],
        },
    }


def _heartbeat(health, runtime_audit, fs_fallback, observation_window, features):
    env_errors = _safe_int(health.get("environment_error_count"), 0)
    escalated = 0
    blocked = _safe_int(health.get("feature_frontier_blocked_count"), 0)
    for feature in features:
        for issue in (feature.get("self_repair") or {}).get("issues") or []:
            if issue.get("status") == "escalated":
                escalated += 1
    return {
        "fs_fallback_streak_hours": fs_fallback.get("hours_since_last"),
        "fs_fallback_events": fs_fallback.get("event_count"),
        "workflow_issues": _safe_int(health.get("workflow_check_issue_count"), 0),
        "blocked_frontiers": blocked,
        "escalated_issues": escalated,
        "environment_errors": env_errors,
        "orphan_recoveries_24h": len(runtime_audit.get("orphan_recoveries") or []),
        "task_bypasses_24h": len(runtime_audit.get("task_bypasses") or []),
        "observation_windows": observation_window.get("active_count", 0),
    }


def build_feed(*, emit_runtime_metrics=False):
    if emit_runtime_metrics:
        o.emit_runtime_metrics_snapshot(source="dashboard-feed")
    health = _health()
    features = _features()
    runtime_audit = _runtime_audit()
    fs_fallback = _fs_fallback()
    observation_window = _observation_window(features)
    dashboard_server = _dashboard_server()
    return {
        "timestamp": o.now_iso(),
        "health": health,
        "agents": _agents(),
        "queue": _queue(),
        "features": features,
        "blocker_codes": _blocker_codes(),
        "recent_transitions": _recent_transitions(),
        "claude_budget": _claude_budget(),
        "task_costs": _task_costs(),
        "runtime_audit": runtime_audit,
        "review_gates": _review_gate_status(features),
        "fs_fallback": fs_fallback,
        "memory_observations": _memory_observations(),
        "observation_window": observation_window,
        "heartbeat": _heartbeat(health, runtime_audit, fs_fallback, observation_window, features),
        "skills": _skills(),
        "harness": _harness(),
        "telegram_log": _telegram_log(),
        "environment": _environment_panel(runtime_audit, dashboard_server),
        "dashboard_server": dashboard_server,
    }


def main():
    o.write_json_atomic(o.DASHBOARD_FEED_PATH, build_feed(emit_runtime_metrics=True))
    print(o.DASHBOARD_FEED_PATH)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build a live JSON feed for the orchestrator dashboard."""

from __future__ import annotations

import json
import os
import pathlib
import sys
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orchestrator as o  # noqa: E402


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
    return [{"state": state, "count": count} for state, count in o.queue_counts().items()]


def _features():
    rows = []
    for wf in o.open_feature_workflow_summaries():
        frontier = wf.get("frontier") or {}
        blocker = frontier.get("blocker") or {}
        self_repair = dict(wf.get("self_repair") or {})
        issues = list(self_repair.get("issues") or [])
        rows.append(
            {
                "feature_id": wf.get("feature_id"),
                "project": wf.get("project"),
                "status": wf.get("feature_status"),
                "summary": wf.get("summary"),
                "created_at": (o.read_feature(wf.get("feature_id")) or {}).get("created_at"),
                "frontier": {
                    "task_id": frontier.get("task_id"),
                    "state": frontier.get("state"),
                    "age_text": frontier.get("age_text"),
                    "blocker": blocker,
                },
                "child_states": wf.get("child_states") or {},
                "self_repair": {
                    "enabled": bool(self_repair.get("enabled")),
                    "status": (issues[0] if issues else {}).get("status") or self_repair.get("status"),
                    "issue_count": len(issues),
                    "issues": issues,
                },
            }
        )
    return rows


def _blocker_codes():
    counts = {}
    total = 0
    for path in o.queue_dir("blocked").glob("*.json"):
        task = o.read_json(path, {}) or {}
        blocker = o.task_blocker(task) or {}
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
    rows = []
    for row in o.read_transitions(limit=50):
        rows.append(
            {
                "ts": row.get("ts"),
                "task_id": row.get("task_id"),
                "from_state": row.get("from_state"),
                "to_state": row.get("to_state"),
                "reason": row.get("reason"),
            }
        )
    return rows


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
    return {
        "host": cfg["host"],
        "port": cfg["port"],
        "allowed_cidrs": list(cfg["allowed_cidrs"]),
        "dashboard_url": f"http://{cfg['host']}:{cfg['port']}/",
    }


def build_feed():
    return {
        "timestamp": o.now_iso(),
        "health": _health(),
        "agents": _agents(),
        "queue": _queue(),
        "features": _features(),
        "blocker_codes": _blocker_codes(),
        "recent_transitions": _recent_transitions(),
        "claude_budget": _claude_budget(),
        "dashboard_server": _dashboard_server(),
    }


def main():
    o.emit_runtime_metrics_snapshot(source="dashboard-feed")
    o.write_json_atomic(o.DASHBOARD_FEED_PATH, build_feed())
    print(o.DASHBOARD_FEED_PATH)


if __name__ == "__main__":
    main()

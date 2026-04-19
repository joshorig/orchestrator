#!/usr/bin/env python3
"""devmini orchestrator — file-backed task state machine.

Storage layout:
  queue/<state>/<task_id>.json        where state ∈ STATES
  state/runtime/transitions.log       append-only, one transition per line
  state/runtime/claims/<task_id>.pid  pid + slot + worktree of claiming worker
  state/runtime/locks/<project>.lock  advisory lock for regression exclusivity
  state/agents/<slot>.json            last-known status per slot
  braid/templates/<task_type>.mmd     cached Mermaid reasoning graphs
  braid/generators/<task_type>.prompt.md
  braid/index.json                    live template registry (runtime state, untracked)

This file is both a CLI entry point and a module. worker.py imports helpers.
"""
import argparse
from collections import deque
from dataclasses import dataclass
import datetime as dt
import errno
import fcntl
import gzip
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

STATE_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEV_ROOT = pathlib.Path(os.environ.get("DEV_ROOT", str(STATE_ROOT.parent)))
CONFIG_LOCAL_PATH = STATE_ROOT / "config" / "orchestrator.local.json"
CONFIG_EXAMPLE_PATH = STATE_ROOT / "config" / "orchestrator.example.json"
CONTEXT_SOURCES_LOCAL_PATH = STATE_ROOT / "config" / "context-sources.json"
CONTEXT_SOURCES_EXAMPLE_PATH = STATE_ROOT / "config" / "context-sources.example.json"
GH_TOKEN_PATH = STATE_ROOT / "config" / "gh-token"
GITIGNORED_OPERATOR_CONFIGS = (
    "config/gh-token",
    "config/orchestrator.local.json",
    "config/context-sources.json",
    "config/telegram.json",
    "config/claude.env",
)
QUEUE_ROOT = STATE_ROOT / "queue"
AGENT_STATE_DIR = STATE_ROOT / "state" / "agents"
RUNTIME_DIR = STATE_ROOT / "state" / "runtime"
PLANNER_DISABLED_DIR = RUNTIME_DIR / "planner-disabled"
SLOT_PAUSE_DIR = RUNTIME_DIR / "slot-paused"
CLAIMS_DIR = RUNTIME_DIR / "claims"
LOCKS_DIR = RUNTIME_DIR / "locks"
EVENTS_LOG = RUNTIME_DIR / "events.jsonl"
METRICS_LOG = RUNTIME_DIR / "metrics.jsonl"
PR_SWEEP_METRICS_PATH = RUNTIME_DIR / "pr-sweep-metrics.json"
TELEGRAM_PUSH_STATE_PATH = RUNTIME_DIR / "telegram-pushes.json"
WORKER_CRASH_HISTORY_PATH = RUNTIME_DIR / "worker-crashes.json"
TASK_ACTION_NOTES_PATH = RUNTIME_DIR / "task-action-notes.json"
TASK_FULL_MESSAGE_DIR = RUNTIME_DIR / "telegram-full"
TASK_FULL_MESSAGE_LIMIT = 100
ALLOWLIST_PATH = RUNTIME_DIR / "allowlist.json"
FEATURES_DIR = STATE_ROOT / "state" / "features"
TRANSITIONS_LOG = RUNTIME_DIR / "transitions.log"
REPORT_DIR = STATE_ROOT / "reports"
LOGS_DIR = STATE_ROOT / "logs"
LOG_ARCHIVE_DIR = LOGS_DIR / "archive"
PROJECT_HARD_STOPS_PATH = RUNTIME_DIR / "project-hard-stops.json"
TELEGRAM_INBOX = STATE_ROOT / "telegram" / "inbox"
TELEGRAM_OUTBOX = STATE_ROOT / "telegram" / "outbox"
BRAID_DIR = STATE_ROOT / "braid"
BRAID_TEMPLATES = BRAID_DIR / "templates"
BRAID_GENERATORS = BRAID_DIR / "generators"
BRAID_INDEX = BRAID_DIR / "index.json"
DASHBOARD_FEED_PATH = RUNTIME_DIR / "dashboard-feed.json"
DASHBOARD_HTML_PATH = STATE_ROOT / "orchestrator-dashboard.html"
STATE_ENGINE_DB_PATH = RUNTIME_DIR / "orchestrator.db"
STATE_MIGRATIONS_DIR = STATE_ROOT / "state" / "migrations"
BRAID_NODE_DEF_RE = re.compile(r"(?P<node>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<shape>\[[^\]\n]*\]|\{[^\}\n]*\})")
BRAID_EDGE_START_RE = re.compile(r"^\s*(?P<node>[A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]\n]*\]|\{[^\}\n]*\})?")
BRAID_EDGE_END_RE = re.compile(r"(?P<node>[A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]\n]*\]|\{[^\}\n]*\})?\s*;?\s*$")
BRAID_BARE_EDGE_RE = re.compile(r"^\s*A\s*-->\s*B\s*;?\s*$")
BRAID_HARD_PREFIXES = ("Start", "End", "Check:", "Revise:", "Draft:", "Run:", "Read:")
CLAUDE_CANDIDATE_PATHS = (
    os.environ.get("CLAUDE_BIN", "").strip(),
    shutil.which("claude") or "",
    str(pathlib.Path.home() / ".local/bin/claude"),
    str(pathlib.Path.home() / "Library/Application Support/Claude/claude-code-vm/current/claude"),
    str(pathlib.Path.home() / "Library/Application Support/Claude/claude-code/claude.app/Contents/MacOS/claude"),
)
CODEX_CANDIDATE_PATHS = (
    os.environ.get("CODEX_BIN", "").strip(),
    shutil.which("codex") or "",
    "/opt/homebrew/bin/codex",
)

STATES = (
    "queued",
    "claimed",
    "running",
    "blocked",
    "awaiting-review",
    "awaiting-qa",
    "done",
    "failed",
    "abandoned",
)

VALID_ENGINES = ("claude", "codex", "qa")
VALID_ROLES = ("planner", "implementer", "reviewer", "qa", "historian")
BLOCKER_CODES = (
    "template_missing",
    "template_missing_edge",
    "template_refine_exhausted",
    "template_graph_error",
    "invalid_braid_refine",
    "false_blocker_claim",
    "project_main_dirty",
    "project_regression_failed",
    "runtime_policy_stale",
    "worker_crash",
    "worker_crash_lock_contention",
    "worker_crash_subprocess_timeout",
    "worker_crash_oom_killed",
    "worker_crash_git_failure",
    "worker_crash_unhandled",
    "worktree_create_failed",
    "planner_emitted_no_children",
    "feature_has_no_children",
    "missing_child",
    "missing_child_unrecoverable",
    "final_pr_blocked",
    "finalize_follow_up_dead",
    "canary_missing_recent_success",
    "canary_stale",
    "canary_unrecoverable",
    "runtime_env_dirty",
    "delivery_auth_expired",
    "runtime_unknown_project",
    "runtime_precondition_failed",
    "review_gate_protocol_error",
    "review_feedback_exhausted",
    "claude_budget_exhausted",
    "slot_paused",
    "llm_timeout",
    "llm_exit_error",
    "model_output_invalid",
    "validator_malfunction",
    "qa_contract_error",
    "qa_target_missing",
    "qa_smoke_failed",
    "qa_scope_inadequate",
    "auto_commit_failed",
    "delivery_push_failed",
    "attempt_exhausted",
)
WORKFLOW_REPAIR_POLICY = (
    {
        "name": "template_missing_wait",
        "kind": "frontier_task_blocked",
        "blocker_code": "template_missing",
        "task_state": "blocked",
        "action": "push_alert",
        "diagnosis": "template regeneration has not landed; page an operator instead of silently waiting",
        "when": "template_missing_blocked",
    },
    {
        "name": "template_missing_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "template_missing",
        "task_state": "blocked",
        "action": "retry_task",
        "diagnosis": "template now exists",
        "when": "template_missing_ready",
    },
    {
        "name": "template_refine_wait",
        "kind": "frontier_task_blocked",
        "blocker_code": "template_missing_edge",
        "action": "push_alert",
        "diagnosis": "template refinement is pending; page an operator instead of silently waiting",
        "when": "template_refine_waiting",
    },
    {
        "name": "template_refine_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "template_missing_edge",
        "action": "retry_task",
        "diagnosis": "template refinement landed",
        "when": "template_refine_ready",
    },
    {
        "name": "project_main_dirty_repair",
        "kind": "frontier_task_blocked",
        "blocker_code": "project_main_dirty",
        "action": "repair_main_then_retry",
        "diagnosis": "project main checkout dirty or ahead; attempt bounded checkout repair first",
    },
    {
        "name": "project_main_dirty_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "project_main_dirty",
        "action": "retry_task",
        "diagnosis": "project main checkout is clean again",
        "when": "project_main_clean",
    },
    {
        "name": "runtime_policy_reload",
        "kind": "frontier_task_blocked",
        "blocker_code": "runtime_policy_stale",
        "action": "restart_workers_then_retry",
        "diagnosis": "workflow runtime changed; reload workers then retry",
    },
    {
        "name": "worker_crash_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "worker_crash",
        "action": "retry_task",
        "diagnosis": "worker crashed; task is retryable",
    },
    {
        "name": "worker_crash_lock_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "worker_crash_lock_contention",
        "action": "retry_task",
        "diagnosis": "worker lost a shared-lock race; retry the task",
    },
    {
        "name": "worker_crash_timeout_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "worker_crash_subprocess_timeout",
        "action": "retry_task",
        "diagnosis": "worker subprocess timed out; retry within bounded attempt budget",
    },
    {
        "name": "worker_crash_git_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "worker_crash_git_failure",
        "action": "retry_task",
        "diagnosis": "worker hit a transient git failure; retry the task",
    },
    {
        "name": "worker_crash_unhandled_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "worker_crash_unhandled",
        "action": "retry_task",
        "diagnosis": "worker crashed unexpectedly; retry the task once through workflow-check",
    },
    {
        "name": "validator_malfunction_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "validator_malfunction",
        "action": "retry_task",
        "diagnosis": "a validator path malfunctioned; retry the task without treating it as a real product failure",
    },
    {
        "name": "worktree_create_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "worktree_create_failed",
        "action": "retry_task",
        "diagnosis": "worktree creation failed; retry after cleanup",
    },
    {
        "name": "llm_timeout_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "llm_timeout",
        "action": "retry_task",
        "diagnosis": "LLM slot timed out; retry within bounded attempt budget",
    },
    {
        "name": "llm_exit_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "llm_exit_error",
        "action": "retry_task",
        "diagnosis": "LLM process exited unexpectedly; retry within bounded attempt budget",
    },
    {
        "name": "model_output_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "model_output_invalid",
        "action": "retry_task",
        "diagnosis": "model output violated the contract; retry within bounded attempt budget",
    },
    {
        "name": "invalid_braid_refine_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "invalid_braid_refine",
        "action": "retry_task",
        "diagnosis": "BRAID_REFINE request was invalid; retry with the updated runtime parser",
    },
    {
        "name": "auto_commit_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "auto_commit_failed",
        "action": "retry_task",
        "diagnosis": "auto-commit failed; retry within bounded attempt budget",
    },
    {
        "name": "push_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "delivery_push_failed",
        "action": "retry_task",
        "diagnosis": "push failed after a successful local step; retry within bounded attempt budget",
    },
    {
        "name": "qa_smoke_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "qa_smoke_failed",
        "action": "retry_task",
        "diagnosis": "QA smoke failed; allow one bounded retry before escalation",
    },
    {
        "name": "qa_scope_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "qa_scope_inadequate",
        "action": "retry_task",
        "diagnosis": "semantic QA judged the executed validation scope inadequate; retry after fixes or broader validation",
    },
    {
        "name": "template_graph_wait",
        "kind": "frontier_task_blocked",
        "blocker_code": "template_graph_error",
        "action": "push_alert",
        "diagnosis": "template graph remains broken without a landed fix; page an operator",
        "when": "template_graph_waiting",
    },
    {
        "name": "template_graph_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "template_graph_error",
        "action": "retry_task",
        "diagnosis": "template regeneration landed",
        "when": "template_graph_ready",
    },
    {
        "name": "template_refine_exhausted_report",
        "kind": "frontier_task_blocked",
        "blocker_code": "template_refine_exhausted",
        "action": "retry_task",
        "diagnosis": "template changed after refinement exhaustion; retry the blocked task",
        "when": "template_graph_ready",
    },
    {
        "name": "template_refine_exhausted_self_repair",
        "kind": "frontier_task_blocked",
        "blocker_code": "template_refine_exhausted",
        "action": "enqueue_self_repair",
        "diagnosis": "template refinement attempts exhausted and no new template landed; escalate into self-repair",
        "when": "template_graph_waiting",
    },
    {
        "name": "review_feedback_stale_abandon",
        "kind": "frontier_task_blocked",
        "blocker_code": "qa_target_missing",
        "action": "abandon_task",
        "diagnosis": "review-feedback lane is stale or superseded; retire the obsolete task so the real frontier can progress",
        "when": "review_feedback_stale",
    },
    {
        "name": "regression_green_clear",
        "kind": "frontier_task_blocked",
        "blocker_code": "project_regression_failed",
        "action": "clear_regression_hard_stop",
        "diagnosis": "a fresh green regression run proves the hard-stop can be cleared",
        "when": "project_regression_green",
    },
    {
        "name": "regression_human_push_clear",
        "kind": "frontier_task_blocked",
        "blocker_code": "project_regression_failed",
        "action": "clear_regression_hard_stop",
        "diagnosis": "a newer human commit landed after the regression failure, so clear the stale hard-stop",
        "when": "project_regression_human_push",
    },
    {
        "name": "regression_self_repair",
        "kind": "frontier_task_blocked",
        "blocker_code": "project_regression_failed",
        "action": "enqueue_self_repair",
        "diagnosis": "hard-stopped by regression failure; attach a repair issue so the project does not deadlock silently",
    },
    {
        "name": "delivery_auth_wait",
        "kind": "environment_degraded",
        "blocker_code": "delivery_auth_expired",
        "action": "push_alert",
        "diagnosis": "delivery credentials are expired; page an operator to refresh auth",
    },
    {
        "name": "false_blocker_claim_self_repair",
        "kind": "frontier_task_blocked",
        "blocker_code": "false_blocker_claim",
        "action": "enqueue_self_repair",
        "diagnosis": "solver emitted a control-plane false blocker claim; open orchestrator self-repair",
        "when": "control_plane_feedback_bug",
    },
    {
        "name": "false_blocker_claim_report",
        "kind": "frontier_task_blocked",
        "blocker_code": "false_blocker_claim",
        "action": "push_alert",
        "diagnosis": "solver emitted a false blocker claim outside the known self-repair path; page an operator",
    },
    {
        "name": "qa_contract_repair",
        "kind": "frontier_task_blocked",
        "blocker_code": "qa_contract_error",
        "action": "enqueue_qa_contract_repair",
        "diagnosis": "QA contract or script configuration is missing; queue a dedicated repair task without blocking unrelated work",
    },
    {
        "name": "qa_target_missing_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "qa_target_missing",
        "action": "retry_task",
        "diagnosis": "target exists but moved to a different queue state; retry with the updated lifecycle logic",
        "when": "qa_target_relocated",
    },
    {
        "name": "qa_target_missing_self_repair",
        "kind": "frontier_task_blocked",
        "blocker_code": "qa_target_missing",
        "action": "enqueue_self_repair",
        "diagnosis": "workflow target lifecycle is inconsistent; open orchestrator self-repair",
        "when": "qa_target_still_missing",
    },
    {
        "name": "qa_target_missing_report",
        "kind": "frontier_task_blocked",
        "blocker_code": "qa_target_missing",
        "action": "push_alert",
        "diagnosis": "QA target state or worktree is missing; page an operator to inspect queue/worktree lifecycle",
    },
    {
        "name": "runtime_env_dirty_repair",
        "kind": "environment_degraded",
        "blocker_code": "runtime_env_dirty",
        "action": "repair_environment",
        "diagnosis": "runtime environment is degraded; attempt bounded autonomous repair first",
    },
    {
        "name": "delivery_auth_repair",
        "kind": "environment_degraded",
        "blocker_code": "delivery_auth_expired",
        "action": "repair_environment",
        "diagnosis": "delivery auth is degraded; attempt bounded autonomous repair first",
    },
    {
        "name": "runtime_precondition_report",
        "kind": "frontier_task_blocked",
        "blocker_code": "runtime_precondition_failed",
        "action": "push_alert",
        "diagnosis": "runtime preconditions are missing; page an operator to repair task inputs",
    },
    {
        "name": "review_gate_protocol_retry",
        "kind": "frontier_task_blocked",
        "blocker_code": "review_gate_protocol_error",
        "action": "enqueue_reviewer",
        "diagnosis": "review-gate protocol failed; retry the reviewer lane once with typed blocker context",
    },
    {
        "name": "review_feedback_exhausted_alert",
        "kind": "frontier_task_blocked",
        "blocker_code": "review_feedback_exhausted",
        "action": "push_alert",
        "diagnosis": "review feedback rounds were exhausted and the task now requires human triage",
    },
    {
        "name": "slot_paused_wait",
        "kind": "frontier_task_blocked",
        "blocker_code": "slot_paused",
        "action": "push_alert",
        "diagnosis": "slot is paused and requires explicit operator resume",
    },
    {
        "name": "claude_budget_wait",
        "kind": "frontier_task_blocked",
        "blocker_code": "claude_budget_exhausted",
        "action": "push_alert",
        "diagnosis": "Claude slot budget is exhausted and has been auto-paused; page an operator to resume the slot",
    },
    {
        "name": "unknown_project_report",
        "kind": "frontier_task_blocked",
        "blocker_code": "runtime_unknown_project",
        "action": "push_alert",
        "diagnosis": "task references an unknown project; page an operator to repair configuration",
    },
    {
        "name": "attempt_exhausted_alert",
        "kind": "frontier_task_blocked",
        "blocker_code": "attempt_exhausted",
        "action": "push_alert",
        "diagnosis": "task exceeded the cumulative retry limit and requires operator review before any further execution",
    },
    {
        "name": "missing_child_recover",
        "kind": "missing_child",
        "blocker_code": "missing_child",
        "action": "recover_missing_child",
        "diagnosis": "feature child task file is missing; reconstruct it from the transition log if possible",
    },
    {
        "name": "planner_empty_retry",
        "kind": "planner_emitted_no_children",
        "blocker_code": "planner_emitted_no_children",
        "action": "retry_task",
        "diagnosis": "planner completed without emitting any runnable slices",
    },
    {
        "name": "feature_no_children_report",
        "kind": "feature_has_no_children",
        "blocker_code": "feature_has_no_children",
        "action": "push_alert",
        "diagnosis": "feature is open but has no child tasks to make progress; page an operator",
    },
    {
        "name": "feature_finalize_ready",
        "kind": "ready_for_finalize",
        "action": "feature_finalize",
        "diagnosis": "all child task PRs are merged; feature should be finalized",
    },
    {
        "name": "final_pr_repair",
        "kind": "final_pr_blocked",
        "blocker_code": "final_pr_blocked",
        "action": "pr_sweep",
        "diagnosis": "final PR is blocked",
    },
    {
        "name": "finalize_follow_up_dead_ready",
        "kind": "finalize_follow_up_dead",
        "blocker_code": "finalize_follow_up_dead",
        "action": "mark_ready_for_merge",
        "diagnosis": "final PR is clean and the stale follow-up lane should be retired",
    },
    {
        "name": "canary_enqueue",
        "kind": "canary_missing_recent_success",
        "blocker_code": "canary_missing_recent_success",
        "action": "enqueue_canary",
        "diagnosis": "recent successful canary missing",
    },
    {
        "name": "canary_stale_self_repair",
        "kind": "canary_stale",
        "blocker_code": "canary_stale",
        "action": "enqueue_canary_fallback",
        "diagnosis": "synthetic canary has been in-flight too long; rotate to the configured fallback canary project before paging an operator",
    },
)
ATTEMPT_ARCHIVE_FIELDS = (
    "state",
    "claimed_at",
    "started_at",
    "finished_at",
    "log_path",
    "failure",
    "topology_error",
    "topology_error_message",
    "false_blocker_claim",
    "blocker",
    "refine_request",
)
RETRY_CLEAR_FIELDS = (
    "claimed_at",
    "started_at",
    "finished_at",
    "log_path",
    "failure",
    "topology_error",
    "topology_error_message",
    "false_blocker_claim",
    "refine_request",
)
CONFIG_DEFAULTS = {
    "drift_threshold": 5,
    "topology_error_regen_threshold": 0.10,
    "topology_error_regen_window": 20,
    "topology_error_regen_min_samples": 5,
    "workflow_check_max_attempts": 6,
    "self_repair_issue_max_attempts": 3,
    "max_task_attempts": 5,
    "state_engine": {
        "mode": "off",
        "path": str(STATE_ENGINE_DB_PATH),
        "checkpoint_interval_sec": 300,
    },
    "memory": {
        "index_limit": 4,
        "search_limit": 6,
        "get_limit": 3,
        "get_char_limit": 1800,
        "label_primed_query": {
            "enabled": True,
            "review_gates": ("security-review-pass",),
        },
    },
    "review_policy": {
        "test_to_code_ratio": {
            "min_ratio": 0.5,
            "accept_doctest": False,
            "template_overrides": {},
        },
    },
    "synthetic_canary": {
        "enabled": False,
        "project": "lvc-standard",
        "interval_hours": 6,
        "success_sla_hours": 24,
        "max_frontier_age_hours": 2,
        "summary": "Synthetic workflow canary",
        "roadmap_entry_id": "R-021-CANARY",
        "roadmap_title": "Synthetic workflow canary",
        "roadmap_body": (
            "Goal: validate the orchestrator end-to-end path with one tiny, safe slice.\n"
            "- Emit at most one bounded implementation slice that fits an existing project template.\n"
            "- Prefer a no-op or documentation-safe touch if nothing clearly safe fits.\n"
            "- Do not widen scope or pick a risky refactor.\n"
            "- The value of this run is proving planner -> implementer -> reviewer -> qa -> pr-sweep -> feature-finalize still works."
        ),
    },
    "environment_health": {
        "cache_sec": 60,
        "required_launchd_labels": (
            "planner",
            "reviewer",
            "qa-scheduler",
            "reaper",
            "pr-sweep",
            "feature-finalize",
            "workflow-check",
            "telegram-bot",
            "canary-workflows",
        ),
    },
    "telegram": {
        "card_limit": 600,
        "full_message_limit": 6000,
    },
    "dashboard": {
        "host": "127.0.0.1",
        "port": 8765,
        "allowed_cidrs": (
            "127.0.0.0/8",
            "::1/128",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "100.64.0.0/10",
        ),
    },
    "budgets": {
        "claude_default_usd": 0.50,
        "ask_usd": 0.50,
        "review_usd": 1.00,
        "planner_usd": 1.00,
        "template_gen_usd": 1.00,
        "template_refine_usd": 1.00,
        "memory_synthesis_usd": 1.00,
        "self_repair_usd": 2.00,
    },
    "reviews": {
        "self_review": True,
    },
    "worker_crash_guard": {
        "window_seconds": 180,
        "max_crashes": 2,
    },
    "template_regen": {
        "max_attempts": 3,
    },
    "self_repair": {
        "enabled": True,
        "project": "devmini-orchestrator",
        "deploy_mode": "local-main",
        "council_members": ("socrates", "feynman", "ada", "torvalds"),
        "verifier_panel": ("socrates", "kahneman", "ada"),
        "approval_panel": ("feynman", "meadows", "torvalds"),
        "restart_launch_agents": True,
        "observation_window_minutes": 10,
    },
    "council": {
        "enabled": True,
        "planner_panel": ("aristotle", "socrates", "meadows"),
        "review_panel": ("socrates", "kahneman", "torvalds"),
        "qa_panel": ("feynman", "kahneman", "ada"),
    },
}
CONTEXT_SOURCE_DEFAULTS = {
    "engineering_skills_root": "",
    "council_root": "",
    "token_savior_root": "",
    "token_savior_python": "",
}
SECRET_SPECS = {
    "gh-token": {
        "label": "GitHub token",
        "env_var": "GH_TOKEN",
        "path": GH_TOKEN_PATH,
    },
    "telegram-bot-token": {
        "label": "Telegram bot token",
        "env_var": "TELEGRAM_BOT_TOKEN",
        "path": STATE_ROOT / "config" / "telegram.token",
    },
    "claude-env": {
        "label": "Claude env blob",
        "env_var": None,
        "path": STATE_ROOT / "config" / "claude.env",
    },
}

BRAID_RECENT_OUTCOMES_MAX = 50
LOG_RETENTION_DAYS = 7
LOG_SIZE_CAP_BYTES = 1024 * 1024 * 1024
_ENV_HEALTH_CACHE = {"ts": 0.0, "data": None}
_STATE_ENGINE_CACHE = {"key": None, "engine": None}
_STATE_ENGINE_RECONCILE = {"ts": 0.0, "active": False, "last": None}
R7_ALLOWED_CAPS = {
    "API", "BRAID", "CI", "CLI", "CPU", "CSS", "FIXME", "GH", "HTML", "HTTP",
    "JSON", "JVM", "OK", "PNG", "PPD", "PR", "QA", "RSA", "SHA", "TODO",
    "TS", "TTL", "UI", "URL", "UUID", "XML", "XXX", "YAML",
}


@dataclass(frozen=True)
class LintError:
    rule: str
    severity: str
    message: str
    line: int | None = None

    def format(self):
        where = f"line {self.line}: " if self.line else ""
        return f"{self.severity} {self.rule}: {where}{self.message}"


def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def load_gh_token_env():
    """Load GH_TOKEN from config/gh-token if the process environment lacks it."""
    token = os.environ.get("GH_TOKEN", "").strip()
    if token:
        return True
    token = secret_value("gh-token")
    if not token:
        return False
    os.environ["GH_TOKEN"] = token
    return True


def load_telegram_bot_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        return token
    token = secret_value("telegram-bot-token")
    if token:
        os.environ["TELEGRAM_BOT_TOKEN"] = token
    return token


def load_claude_env_blob():
    return secret_value("claude-env")

def canonical_orchestrator_root():
    """Best-effort canonical checkout path for this repo, preferring branch main."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(STATE_ROOT), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if proc.returncode == 0:
            current_path = None
            current_branch = None
            for raw in (proc.stdout or "").splitlines() + [""]:
                line = raw.strip()
                if line.startswith("worktree "):
                    current_path = pathlib.Path(line.split(" ", 1)[1]).resolve()
                    current_branch = None
                elif line.startswith("branch "):
                    current_branch = line.split(" ", 1)[1]
                elif not line and current_path is not None:
                    if current_branch == "refs/heads/main":
                        return current_path
                    current_path = None
                    current_branch = None
    except Exception:
        pass
    fallback = DEV_ROOT / "orchestrator"
    return fallback.resolve() if fallback.exists() else STATE_ROOT

def gh_subprocess_env(extra_env=None):
    """Return a subprocess env with GitHub token vars populated when available."""
    env = os.environ.copy()
    token = env.get("GH_TOKEN", "").strip()
    if not token:
        load_gh_token_env()
        token = os.environ.get("GH_TOKEN", "").strip()
        if token:
            env["GH_TOKEN"] = token
    if token:
        env.setdefault("GH_TOKEN", token)
        env.setdefault("GITHUB_TOKEN", token)
        env.setdefault("GH_HOST", "github.com")
    if extra_env:
        env.update(extra_env)
    return env


def _read_json_if_exists(path):
    if not pathlib.Path(path).exists():
        return {}
    return json.loads(pathlib.Path(path).read_text())


def _secret_spec(name):
    spec = SECRET_SPECS.get(name)
    if not spec:
        raise KeyError(f"unknown secret: {name}")
    return spec


def secret_value(name):
    spec = _secret_spec(name)
    path = pathlib.Path(spec["path"])
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def secret_set(name, value):
    spec = _secret_spec(name)
    path = pathlib.Path(spec["path"])
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        path.chmod(0o600)
    except OSError as exc:
        return False, str(exc)
    return True, str(path)


def secret_delete(name):
    spec = _secret_spec(name)
    path = pathlib.Path(spec["path"])
    try:
        path.unlink()
    except FileNotFoundError:
        return True, str(path)
    except OSError as exc:
        return False, str(exc)
    return True, str(path)


def secret_status(name):
    spec = _secret_spec(name)
    value = secret_value(name)
    return {
        "name": name,
        "label": spec["label"],
        "present": bool(value),
        "source": "file" if value else "missing",
        "env_var": spec.get("env_var"),
        "path": str(spec["path"]),
    }


def _allowlist_default():
    return {"approved": [], "pending": [], "updated_at": None}


def load_operator_allowlist():
    data = read_json(ALLOWLIST_PATH, None)
    if not isinstance(data, dict):
        data = _allowlist_default()
    data.setdefault("approved", [])
    data.setdefault("pending", [])
    data.setdefault("updated_at", None)
    return data


def save_operator_allowlist(data):
    payload = dict(_allowlist_default())
    payload.update(data or {})
    payload["updated_at"] = now_iso()
    write_json_atomic(ALLOWLIST_PATH, payload)
    return payload


def telegram_allowed_chat_ids():
    data = load_operator_allowlist()
    out = []
    for row in data.get("approved", []):
        try:
            out.append(int(row.get("chat_id")))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def telegram_register_operator(chat_id, name=""):
    """Record or refresh a pending operator registration.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = telegram_register_operator.__globals__["ALLOWLIST_PATH"]
    ...     telegram_register_operator.__globals__["ALLOWLIST_PATH"] = pathlib.Path(tmp) / "allowlist.json"
    ...     out1 = telegram_register_operator(123, "alice")
    ...     out2 = telegram_register_operator(123, "alice")
    ...     telegram_register_operator.__globals__["ALLOWLIST_PATH"] = old
    >>> (out1["status"], out2["seen_count"] >= 2)
    ('pending', True)
    """
    data = load_operator_allowlist()
    now = now_iso()
    chat_id = int(chat_id)
    for row in data.get("approved", []):
        if int(row.get("chat_id")) == chat_id:
            return {"status": "already_approved", "chat_id": chat_id, "name": row.get("name") or name}
    for row in data.get("pending", []):
        if int(row.get("chat_id")) == chat_id:
            row["name"] = name or row.get("name") or f"operator-{chat_id}"
            row["last_requested_at"] = now
            row["seen_count"] = int(row.get("seen_count", 0) or 0) + 1
            save_operator_allowlist(data)
            return {"status": "pending", "chat_id": chat_id, "name": row["name"], "seen_count": row["seen_count"]}
    row = {
        "chat_id": chat_id,
        "name": name or f"operator-{chat_id}",
        "requested_at": now,
        "last_requested_at": now,
        "seen_count": 1,
    }
    data.setdefault("pending", []).append(row)
    save_operator_allowlist(data)
    return {"status": "pending", "chat_id": chat_id, "name": row["name"], "seen_count": 1}


def telegram_approve_operator(chat_id, approved_by, name=""):
    """Approve a pending operator registration into the durable allowlist.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = telegram_approve_operator.__globals__["ALLOWLIST_PATH"]
    ...     telegram_approve_operator.__globals__["ALLOWLIST_PATH"] = pathlib.Path(tmp) / "allowlist.json"
    ...     _ = telegram_register_operator(123, "alice")
    ...     out = telegram_approve_operator(123, approved_by=999, name="Alice")
    ...     telegram_approve_operator.__globals__["ALLOWLIST_PATH"] = old
    >>> (out["status"], out["chat_id"], out["name"])
    ('approved', 123, 'Alice')
    """
    data = load_operator_allowlist()
    now = now_iso()
    chat_id = int(chat_id)
    approved_by = int(approved_by)
    pending = None
    keep = []
    for row in data.get("pending", []):
        if int(row.get("chat_id")) == chat_id and pending is None:
            pending = row
        else:
            keep.append(row)
    data["pending"] = keep
    for row in data.get("approved", []):
        if int(row.get("chat_id")) == chat_id:
            fallback_name = (pending or {}).get("name") if pending else row.get("name")
            row["name"] = name or row.get("name") or fallback_name
            row["approved_by"] = approved_by
            row["approved_at"] = now
            save_operator_allowlist(data)
            return {"status": "approved", "chat_id": chat_id, "name": row.get("name")}
    approved = {
        "chat_id": chat_id,
        "name": name or ((pending or {}).get("name")) or f"operator-{chat_id}",
        "approved_at": now,
        "approved_by": approved_by,
    }
    data.setdefault("approved", []).append(approved)
    save_operator_allowlist(data)
    return {"status": "approved", "chat_id": chat_id, "name": approved["name"]}


def append_metric(name, value, *, metric_type="gauge", tags=None, source=None, ts=None):
    row = {
        "ts": ts or now_iso(),
        "name": name,
        "type": metric_type,
        "value": value,
        "tags": dict(tags or {}),
        "source": source or "runtime",
    }
    if not _state_engine_read_enabled():
        METRICS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with METRICS_LOG.open("a") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    _mirror_metric_row(row)
    return row


def read_metrics(*, name=None, limit=None):
    if _state_engine_read_enabled():
        try:
            rows = get_state_engine().read_metrics(name=name, limit=limit)
            if rows or not METRICS_LOG.exists():
                return rows
            _record_state_engine_fs_fallback("metrics", name or "*")
        except Exception:
            _record_state_engine_fs_fallback("metrics", name or "*")
    if not METRICS_LOG.exists():
        return []
    rows = []
    try:
        lines = METRICS_LOG.read_text(errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if name is not None and row.get("name") != name:
            continue
        rows.append(row)
    if limit is not None and limit >= 0:
        rows = rows[-limit:]
    return rows


def latest_metric_values(*, names=None):
    wanted = set(names or [])
    latest = {}
    for row in read_metrics():
        name = row.get("name")
        if wanted and name not in wanted:
            continue
        latest[name] = row
    return latest


def _latest_metric_values_by_tags(*, limit=200):
    latest = {}
    for row in read_metrics(limit=limit):
        tags = tuple(sorted((row.get("tags") or {}).items()))
        latest[(row.get("name"), tags)] = row
    return latest


def load_telegram_push_state():
    data = read_json(TELEGRAM_PUSH_STATE_PATH, {}) or {}
    data.setdefault("events", {})
    data.setdefault("full_messages", {})
    return data


def save_telegram_push_state(state):
    write_json_atomic(TELEGRAM_PUSH_STATE_PATH, state)


def should_push_alert(event_key, dedupe_seconds):
    state = load_telegram_push_state()
    events = dict(state.get("events") or {})
    now = time.time()
    row = events.get(event_key) or {}
    last = float(row.get("ts", 0.0) or 0.0)
    if last and (now - last) < max(0, int(dedupe_seconds or 0)):
        return False
    events[event_key] = {"ts": now, "at": now_iso(), "dedupe_seconds": int(dedupe_seconds or 0)}
    state["events"] = events
    save_telegram_push_state(state)
    return True


def remember_full_message(body):
    TASK_FULL_MESSAGE_DIR.mkdir(parents=True, exist_ok=True)
    state = load_telegram_push_state()
    full = dict(state.get("full_messages") or {})
    token = uuid.uuid4().hex[:12]
    path = TASK_FULL_MESSAGE_DIR / f"{token}.txt"
    path.write_text(body or "")
    full[token] = {"path": str(path), "created_at": now_iso()}
    while len(full) > TASK_FULL_MESSAGE_LIMIT:
        oldest_key = sorted(full, key=lambda key: (full[key].get("created_at") or "", key))[0]
        old_path = pathlib.Path(full[oldest_key].get("path") or "")
        if old_path.exists():
            old_path.unlink(missing_ok=True)
        full.pop(oldest_key, None)
    state["full_messages"] = full
    save_telegram_push_state(state)
    return token


def load_full_message(token):
    row = (load_telegram_push_state().get("full_messages") or {}).get(token)
    if not row:
        return None
    path = pathlib.Path(row.get("path") or "")
    if not path.exists():
        return None
    return path.read_text(errors="replace")


def _card_limit():
    cfg = load_config().get("telegram") or {}
    return int(cfg.get("card_limit", CONFIG_DEFAULTS["telegram"]["card_limit"]) or 600)


def _trim_card(text, *, limit=None):
    text = (text or "").strip()
    limit = int(limit or _card_limit())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _queue_count_map():
    return {row["state"]: row["count"] for row in ({"state": s, "count": c} for s, c in queue_counts().items())}


def _health_payload():
    latest = _latest_metric_values_by_tags()
    queue = _queue_count_map()
    def metric(name, default=0, tags=None):
        row = latest.get((name, tuple(sorted((tags or {}).items()))))
        if row is None:
            return default
        return row.get("value", default)
    return {
        "environment_ok": int(metric("environment.ok", 1)),
        "environment_error_count": int(metric("environment.error_count", 0)),
        "workflow_check_issue_count": int(metric("workflow_check.issue_count", 0)),
        "feature_open_count": int(metric("feature.open_count", len(open_feature_workflow_summaries()))),
        "feature_frontier_blocked_count": int(metric("feature.frontier_blocked_count", 0)),
        "queue": queue,
        "generated_at": now_iso(),
    }


def health_snapshot():
    payload = _health_payload()
    queue = payload["queue"]
    env_state = "ok" if payload["environment_ok"] and payload["environment_error_count"] == 0 else f"{payload['environment_error_count']} errors"
    blocked = payload["feature_frontier_blocked_count"]
    lines = [
        f"HEALTH {payload['generated_at'][:16].replace('T', ' ')}",
        (
            "queue:   "
            f"{queue.get('queued', 0)}q · {queue.get('claimed', 0)}c · {queue.get('running', 0)}r · "
            f"{queue.get('blocked', 0)}b · {queue.get('awaiting-review', 0)}ar · {queue.get('awaiting-qa', 0)}aq"
        ),
        f"env:     {env_state}",
        f"features:{payload['feature_open_count']} open · {blocked} frontier-blocked",
        f"issues:  {payload['workflow_check_issue_count']} workflow-check",
    ]
    return _trim_card("\n".join(lines))


def telegram_health_card():
    payload = _health_payload()
    queue = payload["queue"]
    env_ok = payload["environment_ok"] and payload["environment_error_count"] == 0
    blocked = payload["feature_frontier_blocked_count"]
    return _trim_card(
        "\n".join(
            [
                f"env {'OK' if env_ok else f'{payload['environment_error_count']} errors'}",
                f"workflow {payload['workflow_check_issue_count']} issues",
                f"features {payload['feature_open_count']} open / {blocked} blocked",
                (
                    "queue "
                    f"{queue.get('queued',0)}q {queue.get('running',0)}r "
                    f"{queue.get('blocked',0)}b {queue.get('awaiting-review',0)}ar {queue.get('awaiting-qa',0)}aq"
                ),
                f"updated {payload['generated_at'][:16].replace('T', ' ')}",
            ]
        ),
        limit=280,
    )


def features_brief(project=None):
    workflows = open_feature_workflow_summaries(project_name=project)
    if not workflows:
        return _trim_card(f"FEATURES\n{project or 'all projects'}: none open")
    lines = ["FEATURES"]
    for wf in workflows[:5]:
        frontier = wf.get("frontier") or {}
        blocker = frontier.get("blocker") or {}
        lines.append(
            f"{wf.get('feature_id')} · {wf.get('project')}\n"
            f"{frontier.get('state') or wf.get('feature_status') or '-'} {frontier.get('task_id') or '-'} · {frontier.get('age_text') or 'fresh'}"
            + (f"\nblocker: {blocker.get('code')}" if blocker.get("code") else "")
        )
    return _trim_card("\n".join(lines))


def queue_brief(state=None):
    counts = queue_counts()
    if state:
        sample = queue_sample(state, limit=4)
        body = [f"QUEUE {state}", f"count: {counts.get(state, 0)}"]
        body.extend(sample or ["(empty)"])
        return _trim_card("\n".join(body))
    return _trim_card(
        "\n".join(
            [
                "QUEUE",
                f"queued:           {counts.get('queued', 0)}",
                f"claimed:          {counts.get('claimed', 0)}",
                f"running:          {counts.get('running', 0)}",
                f"blocked:          {counts.get('blocked', 0)}",
                f"awaiting-review:  {counts.get('awaiting-review', 0)}",
                f"awaiting-qa:      {counts.get('awaiting-qa', 0)}",
            ]
        )
    )


def issue_card(issue):
    blocker = (issue or {}).get("blocker") or {}
    details = (issue or {}).get("details") or {}
    lines = [
        "ISSUE",
        f"project: {issue.get('project') or details.get('project') or 'global'}",
        f"code:    {blocker.get('code') or issue.get('code') or issue.get('event') or 'unknown'}",
    ]
    age = issue.get("age_text") or details.get("age_text")
    if age:
        lines.append(f"age:     {age}")
    detail = blocker.get("detail") or details.get("detail") or issue.get("summary") or ""
    if detail:
        lines.append(f"detail:  {detail[:180]}")
    return _trim_card("\n".join(lines))


def emit_runtime_metrics_snapshot(*, source="runtime"):
    counts = queue_counts()
    health = environment_health(refresh=True)
    workflows = open_feature_workflow_summaries()
    snapshot = []
    for state, count in counts.items():
        snapshot.append(append_metric("queue.count", count, tags={"state": state}, source=source))
    snapshot.append(append_metric("environment.ok", 1 if health.get("ok") else 0, source=source))
    error_count = len([i for i in health.get("issues", []) if i.get("severity") == "error"])
    snapshot.append(append_metric("environment.error_count", error_count, source=source))
    snapshot.append(append_metric("feature.open_count", len(workflows), source=source))
    blocked = sum(1 for wf in workflows if (wf.get("frontier") or {}).get("state") in ("blocked", "failed", "abandoned"))
    snapshot.append(append_metric("feature.frontier_blocked_count", blocked, source=source))
    return snapshot


def _deep_merge_dicts(base, override):
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config():
    base_path = CONFIG_EXAMPLE_PATH
    if not base_path.exists():
        raise FileNotFoundError(f"missing orchestrator config example: {CONFIG_EXAMPLE_PATH}")
    data = _read_json_if_exists(base_path)
    if CONFIG_LOCAL_PATH.exists():
        data = _deep_merge_dicts(data, _read_json_if_exists(CONFIG_LOCAL_PATH))
    for key, value in CONFIG_DEFAULTS.items():
        if isinstance(value, dict):
            data[key] = _deep_merge_dicts(value, data.get(key) or {})
        else:
            data.setdefault(key, value)
    return data


def state_engine_config(*, cfg=None):
    cfg = cfg or load_config()
    base = _deep_merge_dicts(CONFIG_DEFAULTS.get("state_engine") or {}, cfg.get("state_engine") or {})
    mode = str(os.environ.get("STATE_ENGINE_MODE", base.get("mode", "off")) or "off").strip().lower()
    if mode not in {"off", "mirror", "primary"}:
        mode = "off"
    path_value = pathlib.Path(
        os.environ.get("STATE_ENGINE_PATH", base.get("path", str(STATE_ENGINE_DB_PATH)))
        or str(STATE_ENGINE_DB_PATH)
    )
    if not path_value.is_absolute():
        path_value = (STATE_ROOT / path_value).resolve()
    return {
        "mode": mode,
        "path": str(path_value),
        "checkpoint_interval_sec": max(1, int(base.get("checkpoint_interval_sec") or 300)),
        "migrations_dir": str(STATE_MIGRATIONS_DIR),
    }


def get_state_engine(*, cfg=None):
    cfg = cfg or load_config()
    engine_cfg = state_engine_config(cfg=cfg)
    key = (
        engine_cfg["mode"],
        engine_cfg["path"],
        engine_cfg["checkpoint_interval_sec"],
        engine_cfg["migrations_dir"],
    )
    if _STATE_ENGINE_CACHE["key"] == key:
        return _STATE_ENGINE_CACHE["engine"]
    from state_engine import StateEngine, StateEngineConfig

    engine = StateEngine(
        StateEngineConfig(
            root=STATE_ROOT,
            db_path=pathlib.Path(engine_cfg["path"]),
            migrations_dir=pathlib.Path(engine_cfg["migrations_dir"]),
            mode=engine_cfg["mode"],
            checkpoint_interval_sec=engine_cfg["checkpoint_interval_sec"],
        )
    )
    _STATE_ENGINE_CACHE["key"] = key
    _STATE_ENGINE_CACHE["engine"] = engine
    return engine


def state_engine_status(*, cfg=None):
    cfg = cfg or load_config()
    engine = get_state_engine(cfg=cfg)
    status = engine.initialize()
    if not status.get("enabled"):
        return status
    status["seeded_blocker_codes"] = engine.seed_blocker_codes(BLOCKER_CODES)
    status.update(engine.status())
    return status


def _state_engine_mode(*, cfg=None):
    return state_engine_config(cfg=cfg or load_config())["mode"]


def _state_engine_write_enabled(*, cfg=None):
    return _state_engine_mode(cfg=cfg) in {"mirror", "primary"}


def _state_engine_read_enabled(*, cfg=None):
    return _state_engine_mode(cfg=cfg) == "primary"


def _record_state_engine_fs_fallback(scope, key):
    append_metric(
        "state_engine.fs_fallback",
        1,
        metric_type="counter",
        tags={"scope": scope, "key": key},
        source="state-engine",
    )


def iter_tasks(*, states=None, project=None, engine=None, role=None, limit=None, newest_first=False):
    states = tuple(states or STATES)
    if _state_engine_read_enabled():
        try:
            rows = get_state_engine().read_tasks(
                states=states,
                project=project,
                engine=engine,
                role=role,
                limit=limit,
                newest_first=newest_first,
            )
            if rows or not QUEUE_ROOT.exists():
                return rows
            key = project or engine or role or ",".join(states) or "*"
            _record_state_engine_fs_fallback("tasks", key)
        except Exception:
            key = project or engine or role or ",".join(states) or "*"
            _record_state_engine_fs_fallback("tasks", key)
    rows = []
    for state in states:
        for p in queue_dir(state).glob("*.json"):
            t = read_json(p, {})
            if project is not None and t.get("project") != project:
                continue
            if engine is not None and t.get("engine") != engine:
                continue
            if role is not None and t.get("role") != role:
                continue
            rows.append(t)
    rows.sort(key=lambda t: t.get("created_at", ""), reverse=newest_first)
    if limit is not None and limit >= 0:
        rows = rows[:limit]
    return rows


def _mirror_fs_write(path, obj, *, cfg=None):
    if not _state_engine_write_enabled(cfg=cfg):
        return False
    try:
        rel = pathlib.Path(path).resolve().relative_to(STATE_ROOT.resolve())
    except ValueError:
        return False
    parts = rel.parts
    if not parts:
        return False
    engine = get_state_engine(cfg=cfg)
    engine.initialize()
    if len(parts) >= 3 and parts[0] == "queue" and parts[1] in STATES and parts[-1].endswith(".json"):
        engine.upsert_task_from_fs(obj, state=parts[1])
        _state_engine_maybe_reconcile(cfg=cfg)
        return True
    if len(parts) >= 3 and parts[0] == "state" and parts[1] == "features" and parts[-1].endswith(".json"):
        engine.upsert_feature_from_fs(obj)
        _state_engine_maybe_reconcile(cfg=cfg)
        return True
    return False


def _mirror_transition_row(row, *, cfg=None):
    if not _state_engine_write_enabled(cfg=cfg):
        return False
    engine = get_state_engine(cfg=cfg)
    engine.initialize()
    engine.record_transition(row)
    return True


def _mirror_event_row(row, *, cfg=None):
    if not _state_engine_write_enabled(cfg=cfg):
        return False
    engine = get_state_engine(cfg=cfg)
    engine.initialize()
    engine.record_event(row)
    return True


def _mirror_metric_row(row, *, cfg=None):
    if not _state_engine_write_enabled(cfg=cfg):
        return False
    engine = get_state_engine(cfg=cfg)
    engine.initialize()
    engine.record_metric(row)
    return True


def _record_environment_check(project_name, blockers, *, cfg=None):
    if not _state_engine_write_enabled(cfg=cfg):
        return False
    engine = get_state_engine(cfg=cfg)
    engine.initialize()
    summary = "; ".join(
        f"{issue.get('code')}:{issue.get('summary')}"
        for issue in (blockers or [])
    )[:500] or None
    engine.record_environment_check(
        ts=now_iso(),
        project=project_name,
        result="blocked" if blockers else "ok",
        blocker_summary=summary,
    )
    return True


def _record_orphan_recovery(task_id, from_state, age_seconds, *, cfg=None):
    if not _state_engine_write_enabled(cfg=cfg):
        return False
    engine = get_state_engine(cfg=cfg)
    engine.initialize()
    engine.record_orphan_recovery(
        ts=now_iso(),
        task_id=task_id,
        from_state=from_state,
        age_seconds=int(age_seconds),
    )
    return True


def _record_task_bypass(task_id, gate, reason, *, cfg=None):
    if not _state_engine_write_enabled(cfg=cfg):
        return False
    engine = get_state_engine(cfg=cfg)
    engine.initialize()
    engine.record_task_bypass(
        ts=now_iso(),
        task_id=task_id,
        gate=gate,
        reason=reason,
    )
    return True


def record_task_cost(
    *,
    task_id,
    engine,
    model=None,
    input_tokens=0,
    output_tokens=0,
    cache_tokens=0,
    search_tokens=0,
    cost_usd=0.0,
    cfg=None,
):
    if not _state_engine_write_enabled(cfg=cfg):
        return False
    db = get_state_engine(cfg=cfg)
    db.initialize()
    db.record_task_cost(
        ts=now_iso(),
        task_id=task_id,
        engine=engine,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_tokens,
        search_tokens=search_tokens,
        cost_usd=cost_usd,
    )
    return True


def _fs_queue_state_counts():
    counts = {}
    for state in STATES:
        state_dir = queue_dir(state)
        counts[state] = sum(1 for _ in state_dir.glob("*.json")) if state_dir.exists() else 0
    return counts


def _fs_feature_count():
    return sum(1 for _ in FEATURES_DIR.glob("*.json")) if FEATURES_DIR.exists() else 0


def state_engine_reconcile(*, cfg=None, emit_metrics=True):
    cfg = cfg or load_config()
    engine = get_state_engine(cfg=cfg)
    status = engine.initialize()
    if not status.get("enabled"):
        return {
            "enabled": False,
            "mode": status.get("mode"),
            "queue": {},
            "features": {"db": 0, "fs": 0, "diff": 0},
        }
    primary = status.get("mode") == "primary"
    relation = "fs_subset_of_db" if primary else "db_equals_fs"
    db_counts = engine.queue_state_counts()
    fs_counts = _fs_queue_state_counts()
    queue = {}
    for state in STATES:
        db_n = int(db_counts.get(state, 0))
        fs_n = int(fs_counts.get(state, 0))
        diff = (fs_n - db_n) if primary else (db_n - fs_n)
        queue[state] = {"db": db_n, "fs": fs_n, "diff": diff}
        if emit_metrics:
            append_metric(
                "state_engine.reconciliation_diff",
                diff,
                metric_type="gauge",
                tags={"scope": "queue", "state": state, "relation": relation},
                source="state-engine",
            )
    features = {
        "db": engine.feature_count(),
        "fs": _fs_feature_count(),
    }
    features["diff"] = int(features["fs"]) - int(features["db"]) if primary else int(features["db"]) - int(features["fs"])
    if emit_metrics:
        append_metric(
            "state_engine.reconciliation_diff",
            features["diff"],
            metric_type="gauge",
            tags={"scope": "features", "state": "all", "relation": relation},
            source="state-engine",
        )
    return {
        "enabled": True,
        "mode": status.get("mode"),
        "relation": relation,
        "queue": queue,
        "features": features,
        "integrity_check": status.get("integrity_check"),
    }


def state_engine_backup(*, cfg=None, backup_path=None):
    cfg = cfg or load_config()
    engine = get_state_engine(cfg=cfg)
    status = engine.initialize()
    if not status.get("enabled"):
        return {"enabled": False, "mode": status.get("mode"), "backup_path": None}
    target = pathlib.Path(backup_path or (RUNTIME_DIR / "state.backup.db"))
    out = engine.backup_into(target)
    append_event("state-engine", "backup_written", details={"path": out})
    return {
        "enabled": True,
        "mode": status.get("mode"),
        "backup_path": out,
        "integrity_check": engine.integrity_check(),
    }


def _state_engine_maybe_reconcile(*, cfg=None):
    cfg = cfg or load_config()
    if not _state_engine_write_enabled(cfg=cfg):
        return None
    now = time.time()
    if _STATE_ENGINE_RECONCILE["active"]:
        return _STATE_ENGINE_RECONCILE["last"]
    if (now - float(_STATE_ENGINE_RECONCILE["ts"] or 0.0)) < 60.0:
        return _STATE_ENGINE_RECONCILE["last"]
    _STATE_ENGINE_RECONCILE["active"] = True
    try:
        result = state_engine_reconcile(cfg=cfg, emit_metrics=True)
        _STATE_ENGINE_RECONCILE["ts"] = now
        _STATE_ENGINE_RECONCILE["last"] = result
        return result
    finally:
        _STATE_ENGINE_RECONCILE["active"] = False


def claude_budget_usd(kind="claude_default", *, cfg=None, task=None, mode=None):
    cfg = cfg or load_config()
    budgets = cfg.get("budgets") or {}
    slots = cfg.get("slots") or {}
    claude_slot = slots.get("claude") or {}
    fallback = float(
        budgets.get("claude_default_usd", claude_slot.get("max_budget_usd", CONFIG_DEFAULTS["budgets"]["claude_default_usd"]))
        or CONFIG_DEFAULTS["budgets"]["claude_default_usd"]
    )
    if mode == "self-repair-plan":
        return float(budgets.get("self_repair_usd", fallback) or fallback)
    if task is not None:
        engine_args = task.get("engine_args") or {}
        if engine_args.get("max_budget_usd") is not None:
            return float(engine_args.get("max_budget_usd"))
    key = f"{kind}_usd"
    return float(budgets.get(key, fallback) or fallback)


def max_task_attempts(*, cfg=None):
    cfg = cfg or load_config()
    return max(1, int(cfg.get("max_task_attempts") or CONFIG_DEFAULTS["max_task_attempts"]))


def self_repair_issue_max_attempts(*, cfg=None):
    cfg = cfg or load_config()
    return max(1, int(cfg.get("self_repair_issue_max_attempts") or CONFIG_DEFAULTS["self_repair_issue_max_attempts"]))


def dashboard_server_config(*, cfg=None):
    cfg = cfg or load_config()
    dashboard = cfg.get("dashboard") or {}
    defaults = CONFIG_DEFAULTS["dashboard"]
    return {
        "host": str(dashboard.get("host", defaults["host"]) or defaults["host"]),
        "port": int(dashboard.get("port", defaults["port"]) or defaults["port"]),
        "allowed_cidrs": tuple(dashboard.get("allowed_cidrs", defaults["allowed_cidrs"]) or defaults["allowed_cidrs"]),
    }


def dashboard_client_allowed(client_ip, *, cfg=None):
    cfg = cfg or load_config()
    allowed_cidrs = dashboard_server_config(cfg=cfg)["allowed_cidrs"]
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for cidr in allowed_cidrs:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def load_context_sources():
    data = dict(CONTEXT_SOURCE_DEFAULTS)
    if CONTEXT_SOURCES_EXAMPLE_PATH.exists():
        data = _deep_merge_dicts(data, _read_json_if_exists(CONTEXT_SOURCES_EXAMPLE_PATH))
    if CONTEXT_SOURCES_LOCAL_PATH.exists():
        data = _deep_merge_dicts(data, _read_json_if_exists(CONTEXT_SOURCES_LOCAL_PATH))

    env_overrides = {
        "engineering_skills_root": os.environ.get("ORCH_ENGINEERING_SKILLS_ROOT", "").strip(),
        "council_root": os.environ.get("ORCH_COUNCIL_ROOT", "").strip(),
        "token_savior_root": os.environ.get("ORCH_TOKEN_SAVIOR_ROOT", "").strip(),
        "token_savior_python": os.environ.get("ORCH_TOKEN_SAVIOR_PYTHON", "").strip(),
    }
    for key, value in env_overrides.items():
        if value:
            data[key] = value

    fallbacks = {
        "engineering_skills_root": STATE_ROOT / "skills" / "engineering-memory" / "skills",
        "council_root": STATE_ROOT / "vendor" / "council-of-high-intelligence",
        "token_savior_root": STATE_ROOT / "vendor" / "token-savior",
    }
    for key, path in fallbacks.items():
        if not data.get(key) and path.exists():
            data[key] = str(path)
    return data


def get_project(config, name):
    for p in config["projects"]:
        if p["name"] == name:
            return p
    raise KeyError(f"unknown project: {name}")


def _write_local_config_patch(mutator):
    local = _read_json_if_exists(CONFIG_LOCAL_PATH)
    if not isinstance(local, dict):
        local = {}
    mutator(local)
    write_json_atomic(CONFIG_LOCAL_PATH, local)
    return local


def _validate_project_registration(name, path, smoke, regression):
    repo = pathlib.Path(path).expanduser().resolve()
    if not repo.exists():
        raise SystemExit(f"project path missing: {repo}")
    ok, detail = _git_ok(repo, "rev-parse", "--is-inside-work-tree")
    if not ok:
        raise SystemExit(f"project path is not a git repo: {detail[:200]}")
    if smoke and not (repo / smoke).exists():
        raise SystemExit(f"missing smoke script: {repo / smoke}")
    if regression and not (repo / regression).exists():
        raise SystemExit(f"missing regression script: {repo / regression}")
    cfg = load_config()
    names = {p["name"] for p in cfg.get("projects", [])}
    if name in names:
        raise SystemExit(f"project already registered: {name}")
    return repo


def register_project(
    *,
    name,
    path,
    project_type="application",
    playwright=False,
    auto_push=False,
    smoke="qa/smoke.sh",
    regression="qa/regression.sh",
    regression_days=None,
    regression_threshold_pct=3,
    reload_launchd=True,
):
    """Register a project in the local orchestrator config and refresh the env.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     repo = root / "repo"; repo.mkdir()
    ...     qa = repo / "qa"; qa.mkdir()
    ...     _ = (qa / "smoke.sh").write_text("#!/bin/sh\\n")
    ...     _ = (qa / "regression.sh").write_text("#!/bin/sh\\n")
    ...     _ = subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], capture_output=True, text=True, check=True)
    ...     old_cfg = register_project.__globals__["CONFIG_LOCAL_PATH"]
    ...     old_load = register_project.__globals__["load_config"]
    ...     old_restart = register_project.__globals__["restart_orchestrator_launch_agents"]
    ...     old_health = register_project.__globals__["environment_health"]
    ...     register_project.__globals__["CONFIG_LOCAL_PATH"] = root / "orchestrator.local.json"
    ...     register_project.__globals__["load_config"] = lambda: {"projects": []}
    ...     register_project.__globals__["restart_orchestrator_launch_agents"] = lambda **kwargs: (["ok"], [])
    ...     register_project.__globals__["environment_health"] = lambda refresh=False: {"ok": True, "issues": []}
    ...     out = register_project(name="demo", path=str(repo), reload_launchd=True)
    ...     body = json.loads((root / "orchestrator.local.json").read_text())
    ...     register_project.__globals__["CONFIG_LOCAL_PATH"] = old_cfg
    ...     register_project.__globals__["load_config"] = old_load
    ...     register_project.__globals__["restart_orchestrator_launch_agents"] = old_restart
    ...     register_project.__globals__["environment_health"] = old_health
    >>> (out["registered"], body["projects"][0]["name"], out["environment_ok"])
    (True, 'demo', True)
    """
    repo = _validate_project_registration(name, path, smoke, regression)
    regression_days = [d.lower()[:3] for d in (regression_days or ["sun"])]
    project_entry = {
        "name": name,
        "path": str(repo),
        "type": project_type,
        "playwright": bool(playwright),
        "auto_push": bool(auto_push),
        "qa": {
            "smoke": smoke,
            "regression": regression,
            "regression_days": regression_days,
            "regression_threshold_pct": int(regression_threshold_pct),
        },
    }
    payload = _write_local_config_patch(
        lambda local: local.setdefault("projects", []).append(project_entry)
    )
    restarted, failed = ([], [])
    if reload_launchd:
        restarted, failed = restart_orchestrator_launch_agents()
    health = environment_health(refresh=True)
    append_event(
        "config",
        "project_registered",
        details={"project": name, "path": str(repo), "reload_launchd": reload_launchd},
    )
    return {
        "registered": True,
        "project": name,
        "path": str(repo),
        "local_config_path": str(CONFIG_LOCAL_PATH),
        "projects_total": len(payload.get("projects", [])),
        "restarted_agents": restarted,
        "restart_failures": failed,
        "environment_ok": bool(health.get("ok")),
    }


class EnvironmentDegraded(RuntimeError):
    """Raised when host/project environment health blocks autonomous work."""


def _launchctl_loaded_labels():
    try:
        proc = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    labels = set()
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split()
        if parts:
            labels.add(parts[-1])
    return labels


def _launchctl_setenv(name, value):
    try:
        proc = subprocess.run(
            ["launchctl", "setenv", name, value],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (proc.stderr or proc.stdout or "").strip()
    return proc.returncode == 0, detail or "ok"


def _git_ok(repo_path, *args):
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (proc.stderr or proc.stdout or "").strip()
    return proc.returncode == 0, detail


def _project_main_preflight_issue(project):
    repo_path = pathlib.Path(project["path"])
    if not repo_path.exists():
        return {
            "code": "runtime_env_dirty",
            "scope": "project",
            "project": project["name"],
            "severity": "error",
            "summary": "project path missing",
            "detail": str(repo_path),
        }
    ok, detail = _git_ok(repo_path, "rev-parse", "--is-inside-work-tree")
    if not ok:
        return {
            "code": "runtime_env_dirty",
            "scope": "project",
            "project": project["name"],
            "severity": "error",
            "summary": "project path is not a git repo",
            "detail": detail[:200],
        }
    ok, detail = _git_ok(repo_path, "fetch", "origin", "main")
    if not ok:
        return {
            "code": "runtime_env_dirty",
            "scope": "project",
            "project": project["name"],
            "severity": "error",
            "summary": "fetch origin main failed",
            "detail": detail[:200],
        }
    ok, detail = _git_ok(repo_path, "status", "--porcelain")
    if ok and detail.strip():
        return {
            "code": "project_main_dirty",
            "scope": "project",
            "project": project["name"],
            "severity": "error",
            "summary": "project main checkout dirty",
            "detail": f"{project['path']} has uncommitted changes on main",
        }
    ok, detail = _git_ok(repo_path, "merge-base", "--is-ancestor", "main", "origin/main")
    if not ok:
        return {
            "code": "project_main_dirty",
            "scope": "project",
            "project": project["name"],
            "severity": "error",
            "summary": "project main ahead of origin/main",
            "detail": detail[:200] or f"{project['path']} local main has commits not in origin/main",
        }
    return None


def _repair_project_fetch_state(project):
    repo_path = pathlib.Path(project["path"])
    ok, git_dir_text = _git_ok(repo_path, "rev-parse", "--git-dir")
    if not ok:
        return {"fixed": False, "action": "git_dir_unavailable", "detail": git_dir_text[:200]}
    git_dir = pathlib.Path(git_dir_text)
    if not git_dir.is_absolute():
        git_dir = (repo_path / git_dir).resolve()
    fetch_head = git_dir / "FETCH_HEAD"
    repaired = []
    try:
        if fetch_head.exists():
            fetch_head.chmod(fetch_head.stat().st_mode | 0o600)
            repaired.append("chmod FETCH_HEAD")
    except OSError:
        pass
    try:
        if fetch_head.exists():
            fetch_head.unlink()
            repaired.append("remove FETCH_HEAD")
    except OSError:
        pass
    ok, detail = _git_ok(repo_path, "fetch", "origin", "main")
    return {
        "fixed": ok,
        "action": "git_fetch_retry",
        "detail": detail[:200],
        "changes": repaired,
    }


def repair_environment():
    """Attempt bounded autonomous fixes for known environment failures."""
    cfg = load_config()
    health = environment_health(refresh=True)
    attempts = []

    for issue in health.get("issues", []):
        if issue.get("severity") != "error":
            continue
        code = issue.get("code")
        summary = issue.get("summary") or ""
        project_name = issue.get("project")
        project = None
        if project_name:
            try:
                project = get_project(cfg, project_name)
            except KeyError:
                project = None

        result = {"fixed": False, "issue": issue}
        if code == "delivery_auth_expired" and "GH_TOKEN unavailable" in summary:
            token_loaded = load_gh_token_env()
            launchctl_ok = False
            launchctl_detail = ""
            if token_loaded and os.environ.get("GH_TOKEN", "").strip():
                launchctl_ok, launchctl_detail = _launchctl_setenv("GH_TOKEN", os.environ["GH_TOKEN"])
            result = {
                "fixed": token_loaded,
                "action": "load_gh_token",
                "detail": launchctl_detail or ("ok" if token_loaded else "GH_TOKEN still unavailable"),
                "changes": ["launchctl setenv GH_TOKEN"] if launchctl_ok else [],
                "issue": issue,
            }
        elif code == "runtime_env_dirty" and summary == "telegram bot token unavailable":
            token_loaded = bool(load_telegram_bot_token())
            result = {
                "fixed": token_loaded,
                "action": "load_telegram_bot_token",
                "detail": "ok" if token_loaded else "telegram token still unavailable",
                "issue": issue,
            }
        elif code == "runtime_env_dirty" and summary == "worktrees root missing":
            worktrees_root = DEV_ROOT / "worktrees"
            worktrees_root.mkdir(parents=True, exist_ok=True)
            result = {
                "fixed": worktrees_root.exists(),
                "action": "mkdir_worktrees_root",
                "detail": str(worktrees_root),
                "changes": [str(worktrees_root)],
                "issue": issue,
            }
        elif code == "runtime_env_dirty" and summary == "required launchd job not loaded":
            label = (issue.get("detail") or "").strip()
            plist = pathlib.Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            if plist.exists():
                proc = subprocess.run(
                    ["launchctl", "load", str(plist)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                result = {
                    "fixed": proc.returncode == 0,
                    "action": "load_launch_agent",
                    "detail": (proc.stderr or proc.stdout or "").strip()[:240] or label,
                    "changes": [str(plist)] if proc.returncode == 0 else [],
                    "issue": issue,
                }
            else:
                result = {
                    "fixed": False,
                    "action": "missing_launch_agent_plist",
                    "detail": str(plist),
                    "issue": issue,
                }
        elif code == "runtime_env_dirty" and summary == "fetch origin main failed" and project is not None:
            repair = _repair_project_fetch_state(project)
            result = {**repair, "issue": issue}

        attempts.append(result)

    refreshed = environment_health(refresh=True)
    return {
        "attempted": len(attempts),
        "attempts": attempts,
        "ok": refreshed.get("ok", False),
        "remaining_error_count": len([i for i in refreshed.get("issues", []) if i.get("severity") == "error"]),
    }


def environment_health(*, refresh=False):
    cfg = load_config()
    cache_sec = int((cfg.get("environment_health") or {}).get("cache_sec", 60) or 60)
    now = time.monotonic()
    if not refresh and _ENV_HEALTH_CACHE["data"] is not None and (now - _ENV_HEALTH_CACHE["ts"]) < cache_sec:
        return _ENV_HEALTH_CACHE["data"]

    issues = []

    required_bins = {
        "git": ("runtime_env_dirty", lambda: shutil.which("git")),
        "bash": ("runtime_env_dirty", lambda: shutil.which("bash")),
        "python3": ("runtime_env_dirty", lambda: shutil.which("python3")),
        "launchctl": ("runtime_env_dirty", lambda: shutil.which("launchctl")),
        "gh": ("delivery_auth_expired", lambda: shutil.which("gh")),
        "codex": ("runtime_env_dirty", lambda: next((p for p in CODEX_CANDIDATE_PATHS if p and pathlib.Path(p).exists()), None)),
        "claude": ("runtime_env_dirty", lambda: next((p for p in CLAUDE_CANDIDATE_PATHS if p and pathlib.Path(p).exists()), None)),
    }
    for binary, (code, resolver) in required_bins.items():
        if not resolver():
            issues.append(
                {
                    "code": code,
                    "scope": "global",
                    "project": None,
                    "severity": "error",
                    "summary": f"required binary missing: {binary}",
                    "detail": f"{binary} is not on PATH",
                }
            )

    if not load_gh_token_env():
        issues.append(
            {
                "code": "delivery_auth_expired",
                "scope": "global",
                "project": None,
                "severity": "error",
                "summary": "GH_TOKEN unavailable",
                "detail": "missing GitHub token in env and config/gh-token",
            }
        )
    if not load_telegram_bot_token():
        issues.append(
            {
                "code": "runtime_env_dirty",
                "scope": "global",
                "project": None,
                "severity": "error",
                "summary": "telegram bot token unavailable",
                "detail": "store a token in config/telegram.token or config/telegram.json",
            }
        )
    if not load_claude_env_blob() and not (STATE_ROOT / "config" / "claude.env").exists():
        issues.append(
            {
                "code": "runtime_env_dirty",
                "scope": "global",
                "project": None,
                "severity": "warning",
                "summary": "claude env blob unavailable",
                "detail": "store shared Claude env in config/claude.env",
            }
        )
    if not telegram_allowed_chat_ids():
        issues.append(
            {
                "code": "runtime_env_dirty",
                "scope": "global",
                "project": None,
                "severity": "warning",
                "summary": "telegram allowlist empty",
                "detail": "approve an operator via `orchestrator.py telegram-approve`",
            }
        )

    labels = _launchctl_loaded_labels()
    required_labels = (cfg.get("environment_health") or {}).get("required_launchd_labels") or ()
    if labels is None:
        issues.append(
            {
                "code": "runtime_env_dirty",
                "scope": "global",
                "project": None,
                "severity": "warning",
                "summary": "launchctl status unavailable",
                "detail": "could not read launchd job list",
            }
        )
    else:
        for name in required_labels:
            label = f"com.devmini.orchestrator.{name}"
            if label not in labels:
                issues.append(
                    {
                        "code": "runtime_env_dirty",
                        "scope": "global",
                        "project": None,
                        "severity": "warning",
                        "summary": "required launchd job not loaded",
                        "detail": label,
                    }
                )

    worktrees_root = DEV_ROOT / "worktrees"
    if not worktrees_root.exists():
        issues.append(
            {
                "code": "runtime_env_dirty",
                "scope": "global",
                "project": None,
                "severity": "warning",
                "summary": "worktrees root missing",
                "detail": str(worktrees_root),
            }
        )

    for project in cfg.get("projects", []):
        issue = _project_main_preflight_issue(project)
        if issue:
            issues.append(issue)
        qa_cfg = project.get("qa") or {}
        for kind in ("smoke", "regression"):
            script_rel = qa_cfg.get(kind)
            if script_rel and not (pathlib.Path(project["path"]) / script_rel).exists():
                issues.append(
                    {
                        "code": "runtime_env_dirty",
                        "scope": "project",
                        "project": project["name"],
                        "severity": "error",
                        "summary": f"qa.{kind} script missing",
                        "detail": str(pathlib.Path(project["path"]) / script_rel),
                    }
                )

    out = {
        "ok": not any(issue["severity"] == "error" for issue in issues),
        "generated_at": now_iso(),
        "issues": issues,
    }
    _ENV_HEALTH_CACHE["ts"] = now
    _ENV_HEALTH_CACHE["data"] = out
    return out


def project_environment_blockers(project_name, *, refresh=False):
    rows = []
    for issue in environment_health(refresh=refresh).get("issues", []):
        if issue.get("severity") != "error":
            continue
        if issue.get("project") is None and issue.get("summary") == "telegram bot token unavailable":
            continue
        if issue.get("project") in (None, project_name):
            rows.append(issue)
    _record_environment_check(project_name, rows)
    return rows


def project_environment_ok(project_name, *, refresh=False):
    return not project_environment_blockers(project_name, refresh=refresh)


def _self_repair_project_environment_ok(project_name):
    """Whether a project is healthy enough to branch a local self-repair feature.

    For the orchestrator repo itself, a clean local `main` that is merely ahead
    of `origin/main` is acceptable input for branching a bounded self-repair
    feature. Uncommitted changes, fetch failures, and all other project errors
    remain blocking.
    """
    blockers = project_environment_blockers(project_name, refresh=True)
    if not blockers:
        return True
    if project_name != "devmini-orchestrator":
        return False
    catastrophic = {
        "project path missing",
        "project path is not a git repo",
        "required binary missing: git",
        "required binary missing: python3",
        "required binary missing: bash",
    }
    return not any((issue.get("summary") or "") in catastrophic for issue in blockers)


def environment_health_text(*, refresh=False):
    health = environment_health(refresh=refresh)
    lines = [f"Environment health ({health.get('generated_at')})"]
    if health.get("ok"):
        lines.append("status: healthy")
        return "\n".join(lines)
    lines.append("status: degraded")
    for issue in health.get("issues", []):
        scope = issue.get("project") or issue.get("scope") or "global"
        lines.append(
            f"- [{issue.get('severity')}] {issue.get('code')} {scope}: "
            f"{issue.get('summary')} ({issue.get('detail')})"
        )
    return "\n".join(lines)


def project_historian_template(project_name):
    mapping = {
        "lvc-standard": "lvc-historian-update",
        "dag-framework": "dag-historian-update",
    }
    if project_name == "trade-research-platform":
        trp_prompt = BRAID_GENERATORS / "trp-historian-update.prompt.md"
        return "trp-historian-update" if trp_prompt.exists() else "lvc-historian-update"
    return mapping.get(project_name, "lvc-historian-update")


def project_reviewer_template(project_name):
    mapping = {
        "lvc-standard": "lvc-reviewer-pass",
        "dag-framework": "lvc-reviewer-pass",
        "trade-research-platform": "trp-reviewer-pass",
    }
    if project_name == "trade-research-platform":
        trp_prompt = BRAID_GENERATORS / "trp-reviewer-pass.prompt.md"
        return "trp-reviewer-pass" if trp_prompt.exists() else "lvc-reviewer-pass"
    return mapping.get(project_name, "lvc-reviewer-pass")


def write_json_atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    os.rename(tmp, path)
    _mirror_fs_write(path, obj)


def _write_json_mirror(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    os.rename(tmp, path)


def _delete_json_mirror(path):
    try:
        pathlib.Path(path).unlink()
    except FileNotFoundError:
        pass


def _write_task_record(task, state=None, *, cfg=None):
    state = state or task.get("state") or "queued"
    path = task_path(task["task_id"], state)
    if _state_engine_mode(cfg=cfg) == "primary":
        task = dict(task or {})
        task["state"] = state
        engine = get_state_engine(cfg=cfg)
        engine.initialize()
        engine.upsert_task(task, state=state)
        _write_json_mirror(path, task)
        return path
    write_json_atomic(path, task)
    return path


def _delete_task_record(task_id, state, *, cfg=None):
    path = task_path(task_id, state)
    _delete_json_mirror(path)


def _write_feature_record(feature, *, cfg=None):
    path = feature_path(feature["feature_id"])
    if _state_engine_mode(cfg=cfg) == "primary":
        engine = get_state_engine(cfg=cfg)
        engine.initialize()
        engine.upsert_feature(feature)
        _write_json_mirror(path, feature)
        return path
    write_json_atomic(path, feature)
    return path


def read_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def append_transition(task_id, from_state, to_state, reason=""):
    row = {
        "ts": now_iso(),
        "task_id": task_id,
        "from_state": from_state,
        "to_state": to_state,
        "reason": reason,
    }
    if not _state_engine_read_enabled():
        TRANSITIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with TRANSITIONS_LOG.open("a") as f:
            f.write(f"{row['ts']}\t{task_id}\t{from_state}\t->\t{to_state}\t{reason}\n")
            f.flush()
            os.fsync(f.fileno())
    _mirror_transition_row(row)


def make_blocker(code, *, summary=None, detail=None, source=None, confidence="high", retryable=None, metadata=None):
    if code not in BLOCKER_CODES:
        raise ValueError(f"unknown blocker code: {code}")
    return {
        "code": code,
        "summary": summary or code.replace("_", " "),
        "detail": detail or "",
        "source": source or "runtime",
        "confidence": confidence,
        "retryable": retryable,
        "updated_at": now_iso(),
        "metadata": dict(metadata or {}),
    }


def set_task_blocker(task, code, *, summary=None, detail=None, source=None, confidence="high", retryable=None, metadata=None):
    task["blocker"] = make_blocker(
        code,
        summary=summary,
        detail=detail,
        source=source,
        confidence=confidence,
        retryable=retryable,
        metadata=metadata,
    )
    return task["blocker"]


def clear_task_blocker(task):
    task["blocker"] = None


def _is_validator_malfunction(detail):
    lower = (detail or "").lower()
    if "braid_topology_error" not in lower and "false blocker claim" not in lower:
        return False
    if "orchestrator.json" not in lower:
        return False
    return any(token in lower for token in ("validatesmoke", "validatecanary", "smoke", "canary", "lint-templates"))


def task_blocker(task):
    """Return explicit blocker metadata or infer it from legacy task fields.

    >>> task_blocker({"blocker": {"code": "template_missing"}})["code"]
    'template_missing'
    >>> task_blocker({"topology_error": "template_missing"})["code"]
    'template_missing'
    >>> task_blocker({"topology_error": "main_dirty_or_ahead"})["code"]
    'project_main_dirty'
    >>> task_blocker({"false_blocker_claim": "BRAID_REFINE: Sketch: missing edge"})["code"]
    'false_blocker_claim'
    >>> task_blocker({"failure": "Command '['git', 'worktree', 'add']' returned non-zero exit status 255."})["code"]
    'worktree_create_failed'
    >>> task_blocker({"failure": "claude timeout 1800s"})["code"]
    'llm_timeout'
    >>> task_blocker({"failure": "smoke re-run timeout"})["code"]
    'qa_smoke_failed'
    """
    blocker = task.get("blocker")
    if isinstance(blocker, dict) and blocker.get("code"):
        detail = str(blocker.get("detail") or "")
        if blocker.get("code") == "false_blocker_claim" and _is_validator_malfunction(detail):
            return make_blocker(
                "validator_malfunction",
                summary="validator malfunctioned while assessing the task",
                detail=detail,
                source=blocker.get("source") or "legacy",
                retryable=True,
                confidence=blocker.get("confidence"),
                metadata=blocker.get("metadata"),
            )
        return blocker

    topo = str(task.get("topology_error") or "")
    failure = str(task.get("failure") or "")
    false_claim = str(task.get("false_blocker_claim") or "")
    detail = " ".join(part for part in (failure, topo, false_claim) if part).strip()
    if not detail and task.get("task_id") and task.get("state"):
        _, transition_reason = task_state_entered_at(task["task_id"], task["state"])
        detail = str(transition_reason or "").strip()
    detail_lower = detail.lower()

    if topo == "template_missing":
        return make_blocker("template_missing", summary="template missing", detail=detail, source="legacy", retryable=True)
    if topo == "main_dirty_or_ahead":
        return make_blocker("project_main_dirty", summary="project main checkout dirty or ahead", detail=detail, source="legacy", retryable=True)
    if topo == "regression-failure":
        return make_blocker("project_regression_failed", summary="project regression failed", detail=detail, source="legacy", retryable=False)
    if topo.startswith("BRAID_REFINE:"):
        return make_blocker("template_missing_edge", summary="template missing edge/condition", detail=detail, source="legacy", retryable=True)
    if "refine_rounds_exhausted" in topo:
        return make_blocker("template_refine_exhausted", summary="template refinement exhausted", detail=detail, source="legacy", retryable=False)
    if topo.startswith("BRAID_TOPOLOGY_ERROR:"):
        if _is_validator_malfunction(detail):
            return make_blocker("validator_malfunction", summary="validator malfunctioned while assessing the task", detail=detail, source="legacy", retryable=True)
        return make_blocker("template_graph_error", summary="template graph traversal error", detail=detail, source="legacy", retryable=True)
    if failure.startswith("invalid BRAID_REFINE") or "invalid braid_refine" in detail_lower:
        return make_blocker("invalid_braid_refine", summary="invalid BRAID_REFINE contract", detail=detail, source="legacy", retryable=False)
    if _is_validator_malfunction(detail):
        return make_blocker("validator_malfunction", summary="validator malfunctioned while assessing the task", detail=detail, source="legacy", retryable=True)
    if false_claim:
        return make_blocker("false_blocker_claim", summary="false blocker claim", detail=detail, source="legacy", retryable=False)
    if "budget" in detail_lower and any(token in detail_lower for token in ("exhaust", "limit", "max_budget", "max budget")):
        return make_blocker("claude_budget_exhausted", summary="Claude budget exhausted", detail=detail, source="legacy", retryable=False)
    if "repo-memory secret-scan hit" in detail_lower or "detect-secrets findings" in detail_lower:
        return make_blocker("runtime_policy_stale", summary="worker runtime policy stale", detail=detail, source="legacy", retryable=True)
    if "shared lock" in detail_lower and "acquire" in detail_lower:
        return make_blocker("worker_crash_lock_contention", summary="worker crashed on shared lock contention", detail=detail, source="legacy", retryable=True)
    if "worker crash" in detail_lower and "timeout" in detail_lower:
        return make_blocker("worker_crash_subprocess_timeout", summary="worker subprocess timed out", detail=detail, source="legacy", retryable=True)
    if "worker crash" in detail_lower and ("oom" in detail_lower or "out of memory" in detail_lower or "killed" in detail_lower):
        return make_blocker("worker_crash_oom_killed", summary="worker process was killed by the runtime", detail=detail, source="legacy", retryable=False)
    if "worker crash" in detail_lower and "git" in detail_lower:
        return make_blocker("worker_crash_git_failure", summary="worker crashed during a git operation", detail=detail, source="legacy", retryable=True)
    if "worker crash" in detail_lower:
        return make_blocker("worker_crash_unhandled", summary="worker crashed mid-task", detail=detail, source="legacy", retryable=True)
    if "git', 'worktree', 'add'" in failure and "255" in failure:
        return make_blocker("worktree_create_failed", summary="git worktree creation failed", detail=detail, source="legacy", retryable=True)
    if "unknown project" in detail_lower:
        return make_blocker("runtime_unknown_project", summary="unknown project", detail=detail, source="legacy", retryable=False)
    if "timeout" in detail_lower and any(name in detail_lower for name in ("claude", "codex", "llm")):
        return make_blocker("llm_timeout", summary="LLM slot timeout", detail=detail, source="legacy", retryable=True)
    if "claude exit" in detail_lower or "codex exit" in detail_lower:
        return make_blocker("llm_exit_error", summary="LLM process exit error", detail=detail, source="legacy", retryable=True)
    if any(needle in detail_lower for needle in ("no json array", "malformed json", "no mermaid block", "no braid trailer", "candidate markdown invalid", "no review verdict trailer")):
        return make_blocker("model_output_invalid", summary="model output invalid", detail=detail, source="legacy", retryable=False)
    if any(needle in detail_lower for needle in ("missing target_task_id", "repo-memory incomplete", "missing refine_request", "missing braid_template", "codex task has no braid_template")):
        return make_blocker("runtime_precondition_failed", summary="runtime precondition failed", detail=detail, source="legacy", retryable=False)
    if "no qa." in detail_lower or "script missing:" in detail_lower:
        return make_blocker("qa_contract_error", summary="QA contract missing", detail=detail, source="legacy", retryable=False)
    if "worktree missing" in detail_lower or "target worktree missing" in detail_lower:
        return make_blocker("qa_target_missing", summary="QA target missing", detail=detail, source="legacy", retryable=False)
    if "smoke red" in detail_lower or "smoke timeout" in detail_lower or "smoke re-run timeout" in detail_lower:
        return make_blocker("qa_smoke_failed", summary="QA smoke failed", detail=detail, source="legacy", retryable=True)
    if "auto-commit:" in detail_lower:
        return make_blocker("auto_commit_failed", summary="auto-commit failed", detail=detail, source="legacy", retryable=True)
    if "push failed" in detail_lower or "push_failure" in detail_lower:
        return make_blocker("delivery_push_failed", summary="delivery push failed", detail=detail, source="legacy", retryable=True)
    return None


def append_event(role, event, *, task_id=None, feature_id=None, details=None):
    payload = {
        "ts": now_iso(),
        "role": role,
        "event": event,
        "task_id": task_id,
        "feature_id": feature_id,
        "details": details or {},
    }
    if not _state_engine_read_enabled():
        EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_LOG.open("a") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")
    _mirror_event_row(payload)


def _read_project_hard_stops():
    data = read_json(PROJECT_HARD_STOPS_PATH, {}) or {}
    return data if isinstance(data, dict) else {}


def _write_project_hard_stops(data):
    write_json_atomic(PROJECT_HARD_STOPS_PATH, data)


def set_project_hard_stop(project_name, code, detail):
    stops = _read_project_hard_stops()
    entry = {"code": code, "detail": detail, "ts": now_iso()}
    stops[project_name] = entry
    _write_project_hard_stops(stops)
    append_event(
        "workflow-check",
        "project_hard_stopped",
        details={"project": project_name, "code": code, "reason": detail},
    )
    return entry


def clear_project_hard_stop(project_name, *, cleared_by=None, detail=None):
    stops = _read_project_hard_stops()
    removed = stops.pop(project_name, None)
    if removed is not None:
        _write_project_hard_stops(stops)
        append_event(
            "workflow-check",
            "project_hard_stop_cleared",
            details={
                "project": project_name,
                "code": removed.get("code"),
                "cleared_by": cleared_by or "unknown",
                "detail": detail or removed.get("detail"),
            },
        )
    return removed


def read_events(*, feature_id=None, task_id=None, role=None, limit=None):
    """Return structured rows from the append-only event log.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = read_events.__globals__["EVENTS_LOG"]
    ...     read_events.__globals__["EVENTS_LOG"] = pathlib.Path(tmp) / "events.jsonl"
    ...     rows = [
    ...         {"ts": "2026-04-15T10:00:00", "role": "workflow-check", "event": "report_written", "task_id": None, "feature_id": "f1", "details": {"x": 1}},
    ...         {"ts": "2026-04-15T10:01:00", "role": "planner", "event": "task_enqueued", "task_id": "task-1", "feature_id": "f1", "details": {}},
    ...     ]
    ...     _ = read_events.__globals__["EVENTS_LOG"].write_text("\\n".join(__import__('json').dumps(r, sort_keys=True) for r in rows))
    ...     out = read_events(feature_id="f1", limit=1)
    ...     read_events.__globals__["EVENTS_LOG"] = old
    >>> (len(out), out[0]["event"])
    (1, 'task_enqueued')
    """
    if _state_engine_read_enabled():
        key = feature_id or task_id or role or "*"
        try:
            rows = get_state_engine().read_events(feature_id=feature_id, task_id=task_id, role=role, limit=limit)
            if rows or not EVENTS_LOG.exists():
                return rows
            _record_state_engine_fs_fallback("events", key)
        except Exception:
            _record_state_engine_fs_fallback("events", key)
    if not EVENTS_LOG.exists():
        return []
    rows = []
    try:
        lines = EVENTS_LOG.read_text(errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if feature_id is not None and row.get("feature_id") != feature_id:
            continue
        if task_id is not None and row.get("task_id") != task_id:
            continue
        if role is not None and row.get("role") != role:
            continue
        rows.append(row)
    if limit is not None and limit >= 0:
        rows = rows[-limit:]
    return rows


def read_pr_sweep_metrics():
    data = read_json(PR_SWEEP_METRICS_PATH, {}) or {}
    data.setdefault("guard_skip_total", 0)
    data.setdefault("guard_skip_by_reason", {})
    data.setdefault("updated_at", None)
    return data


def read_transitions(*, task_id=None, limit=None):
    """Return structured transition rows from the append-only transition log.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = read_transitions.__globals__["TRANSITIONS_LOG"]
    ...     read_transitions.__globals__["TRANSITIONS_LOG"] = pathlib.Path(tmp) / "transitions.log"
    ...     _ = read_transitions.__globals__["TRANSITIONS_LOG"].write_text(
    ...         "2026-04-15T10:00:00\\ttask-1\\tqueued\\t->\\tclaimed\\tcodex\\n"
    ...         "2026-04-15T10:01:00\\ttask-1\\tclaimed\\t->\\trunning\\tstart\\n"
    ...     )
    ...     out = read_transitions(task_id="task-1")
    ...     read_transitions.__globals__["TRANSITIONS_LOG"] = old
    >>> (out[0]["task_id"], out[-1]["to_state"], len(out))
    ('task-1', 'running', 2)
    """
    if _state_engine_read_enabled():
        key = task_id or "*"
        try:
            rows = get_state_engine().read_transitions(task_id=task_id, limit=limit)
            if rows or not TRANSITIONS_LOG.exists():
                return rows
            _record_state_engine_fs_fallback("transitions", key)
        except Exception:
            _record_state_engine_fs_fallback("transitions", key)
    if not TRANSITIONS_LOG.exists():
        return []
    rows = []
    try:
        lines = TRANSITIONS_LOG.read_text(errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        parts = line.split("\t", 5)
        if len(parts) < 6:
            continue
        ts, row_task_id, from_state, arrow, to_state, reason = parts
        if arrow != "->":
            continue
        if task_id is not None and row_task_id != task_id:
            continue
        rows.append(
            {
                "ts": ts,
                "task_id": row_task_id,
                "from_state": from_state,
                "to_state": to_state,
                "reason": reason,
            }
        )
    if limit is not None and limit >= 0:
        rows = rows[-limit:]
    return rows


def task_state_entered_at(task_id, state):
    rows = read_transitions(task_id=task_id)
    for row in reversed(rows):
        if row.get("to_state") == state:
            return row.get("ts"), row.get("reason", "")
    return None, ""


def _seconds_since_iso(ts):
    if not ts:
        return None
    try:
        then = dt.datetime.fromisoformat(ts)
    except ValueError:
        return None
    if then.tzinfo is None:
        now = dt.datetime.now()
    else:
        now = dt.datetime.now(tz=then.tzinfo)
    return max(0, int((now - then).total_seconds()))


def _format_age(seconds):
    if seconds is None:
        return None
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def _canary_interval_due(cfg, project_name):
    canary_cfg = cfg.get("synthetic_canary") or {}
    interval_hours = float(canary_cfg.get("interval_hours", 6) or 6)
    last = _latest_canary_feature(project_name)
    if last is None:
        return True, None
    age_seconds = _seconds_since_iso(last.get("created_at"))
    if age_seconds is None:
        return True, last
    return age_seconds >= int(interval_hours * 3600), last


def _canary_success_overdue(cfg, project_name):
    canary_cfg = cfg.get("synthetic_canary") or {}
    sla_hours = float(canary_cfg.get("success_sla_hours", 24) or 24)
    last = _latest_successful_canary(project_name)
    if last is None:
        return True, None
    age_seconds = _seconds_since_iso(last.get("merged_at") or last.get("created_at"))
    if age_seconds is None:
        return True, last
    return age_seconds >= int(sla_hours * 3600), last


def record_pr_sweep_guard_skip(reason):
    """Increment the persistent pr-sweep running-guard telemetry counter.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = record_pr_sweep_guard_skip.__globals__["PR_SWEEP_METRICS_PATH"]
    ...     record_pr_sweep_guard_skip.__globals__["PR_SWEEP_METRICS_PATH"] = pathlib.Path(tmp) / "pr-sweep-metrics.json"
    ...     _ = record_pr_sweep_guard_skip("task_conflict")
    ...     out = record_pr_sweep_guard_skip("task_conflict")
    ...     record_pr_sweep_guard_skip.__globals__["PR_SWEEP_METRICS_PATH"] = old
    >>> (out["guard_skip_total"], out["guard_skip_by_reason"]["task_conflict"])
    (2, 2)
    """
    metrics = read_pr_sweep_metrics()
    metrics["guard_skip_total"] = int(metrics.get("guard_skip_total", 0)) + 1
    by_reason = dict(metrics.get("guard_skip_by_reason") or {})
    by_reason[reason] = int(by_reason.get(reason, 0)) + 1
    metrics["guard_skip_by_reason"] = by_reason
    metrics["updated_at"] = now_iso()
    write_json_atomic(PR_SWEEP_METRICS_PATH, metrics)
    return metrics


def _recent_template_stats(entry, window):
    outcomes = list((entry or {}).get("recent_outcomes") or [])[-max(0, int(window)):]
    samples = len(outcomes)
    errors = sum(1 for outcome in outcomes if outcome == "topology_error")
    rate = (errors / samples) if samples else 0.0
    return samples, errors, rate


def _template_owner_project(config, task_type):
    if task_type.startswith("dag-"):
        return "dag-framework"
    if task_type.startswith("trp-"):
        return "trade-research-platform"
    if task_type.startswith("lvc-") or task_type in ("pr-address-feedback", "review-address-feedback", "memory-synthesis"):
        return "lvc-standard"
    projects = config.get("projects", [])
    return projects[0]["name"] if projects else None


def _template_regen_in_flight(task_type):
    for task in iter_tasks(states=("queued", "claimed", "running"), engine="claude"):
        if task.get("braid_template") != task_type:
            continue
        if (task.get("engine_args") or {}).get("mode") == "template-gen":
            return True
    return False


def tick_template_audit(today=None):
    """Write a template-audit report under reports/ for all live templates.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     templates = root / "templates"
    ...     reports = root / "reports"
    ...     templates.mkdir()
    ...     _ = (templates / "good.mmd").write_text("flowchart TD;\\nStart[Start] -- \\"always\\" --> C1[Check: ok];\\nC1 -- \\"pass\\" --> End[End];\\n")
    ...     old = {k: tick_template_audit.__globals__[k] for k in ("BRAID_TEMPLATES", "REPORT_DIR", "now_iso")}
    ...     tick_template_audit.__globals__["BRAID_TEMPLATES"] = templates
    ...     tick_template_audit.__globals__["REPORT_DIR"] = reports
    ...     tick_template_audit.__globals__["now_iso"] = lambda: "2026-04-14T12:00:00"
    ...     out = tick_template_audit(today="2026-04-14")
    ...     body = pathlib.Path(out).read_text()
    ...     for key, value in old.items():
    ...         tick_template_audit.__globals__[key] = value
    >>> ("template-audit-2026-04-14.md" in str(out), "good: OK" in body)
    (True, True)
    """
    day = today or dt.date.today().isoformat()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"template-audit-{day}.md"
    names = sorted(p.stem for p in BRAID_TEMPLATES.glob("*.mmd"))
    lines = ["# Template Audit", "", f"Generated: {now_iso()}", ""]
    failures = 0
    warnings = 0
    if not names:
        lines.append("- (no templates found)")
    for name in names:
        body = braid_template_path(name).read_text()
        errors = lint_template(body)
        hard = [err for err in errors if err.severity == "error"]
        warns = [err for err in errors if err.severity == "warning"]
        failures += len(hard)
        warnings += len(warns)
        status = "FAIL" if hard else "OK"
        lines.append(f"- {name}: {status}")
        for err in hard + warns:
            lines.append(f"  - {err.format()}")
    lines += ["", f"- total hard failures: {failures}", f"- total warnings: {warnings}", ""]
    out.write_text("\n".join(lines))
    append_event("template-audit", "report_written", details={"path": str(out), "failures": failures, "warnings": warnings})
    return out


def tick_braid_auto_regen(config=None):
    """Enqueue template regeneration when recent topology errors stay high.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     idx_path = root / "index.json"
    ...     queue_root = root / "queue"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     _ = idx_path.write_text(json.dumps({
    ...         "demo-template": {
    ...             "hash": "sha256:old",
    ...             "recent_outcomes": ["topology_error"] * 5,
    ...             "uses": 0,
    ...             "topology_errors": 5,
    ...         }
    ...     }))
    ...     captured = []
    ...     old = {k: tick_braid_auto_regen.__globals__[k] for k in ("BRAID_INDEX", "QUEUE_ROOT", "new_task", "enqueue_task", "now_iso")}
    ...     tick_braid_auto_regen.__globals__["BRAID_INDEX"] = idx_path
    ...     tick_braid_auto_regen.__globals__["QUEUE_ROOT"] = queue_root
    ...     tick_braid_auto_regen.__globals__["new_task"] = lambda **kwargs: {"task_id": "task-regen", **kwargs}
    ...     tick_braid_auto_regen.__globals__["enqueue_task"] = lambda task: captured.append(task["braid_template"])
    ...     tick_braid_auto_regen.__globals__["now_iso"] = lambda: "2026-04-14T12:00:00"
    ...     cfg = {"projects": [{"name": "lvc-standard", "path": str(root)}], "topology_error_regen_threshold": 0.10, "topology_error_regen_window": 5, "topology_error_regen_min_samples": 5}
    ...     out = tick_braid_auto_regen(cfg)
    ...     saved = json.loads(idx_path.read_text())["demo-template"]
    ...     for key, value in old.items():
    ...         tick_braid_auto_regen.__globals__[key] = value
    >>> (out, captured, saved["dispatch_paused"], saved["auto_regen_task_id"])
    (['demo-template'], ['demo-template'], True, 'task-regen')
    """
    config = config or load_config()
    idx = load_braid_index()
    changed = False
    enqueued = []
    threshold = float(config.get("topology_error_regen_threshold", CONFIG_DEFAULTS["topology_error_regen_threshold"]))
    window = int(config.get("topology_error_regen_window", CONFIG_DEFAULTS["topology_error_regen_window"]))
    min_samples = int(config.get("topology_error_regen_min_samples", CONFIG_DEFAULTS["topology_error_regen_min_samples"]))
    for task_type, entry in sorted(idx.items()):
        samples, errors, rate = _recent_template_stats(entry, window)
        if samples < min_samples or rate <= threshold:
            continue
        if entry.get("dispatch_paused") or _template_regen_in_flight(task_type):
            continue
        project_name = _template_owner_project(config, task_type)
        if not project_name:
            continue
        task = new_task(
            role="planner",
            engine="claude",
            project=project_name,
            summary=f"Generate BRAID template for {task_type}",
            source=f"auto-regen:{task_type}",
            braid_template=task_type,
            engine_args={
                "mode": "template-gen",
                "auto_regen": True,
                "topology_error_rate": rate,
                "topology_error_samples": samples,
                "topology_error_count": errors,
            },
        )
        enqueue_task(task)
        entry["dispatch_paused"] = True
        entry["dispatch_paused_at"] = now_iso()
        entry["dispatch_pause_reason"] = f"recent topology error rate {errors}/{samples} ({rate:.0%})"
        entry["auto_regen_task_id"] = task["task_id"]
        entry["auto_regen_requested_at"] = now_iso()
        idx[task_type] = entry
        changed = True
        enqueued.append(task_type)
        append_event("template-auto-regen", "enqueued", details={
            "braid_template": task_type,
            "task_id": task["task_id"],
            "error_rate": rate,
            "samples": samples,
            "errors": errors,
        })
    if changed:
        save_braid_index(idx)
    return enqueued


def rotate_logs(now=None):
    """Rotate stale logs and evict oldest archives to keep logs/ bounded.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     logs = root / "logs"
    ...     logs.mkdir()
    ...     old_log = logs / "task-old.log"
    ...     _ = old_log.write_text("hello\\n")
    ...     ts = time.time() - (LOG_RETENTION_DAYS + 1) * 86400
    ...     os.utime(old_log, (ts, ts))
    ...     old = {k: rotate_logs.__globals__[k] for k in ("LOGS_DIR", "LOG_ARCHIVE_DIR", "LOG_SIZE_CAP_BYTES")}
    ...     rotate_logs.__globals__["LOGS_DIR"] = logs
    ...     rotate_logs.__globals__["LOG_ARCHIVE_DIR"] = logs / "archive"
    ...     rotate_logs.__globals__["LOG_SIZE_CAP_BYTES"] = 1024 * 1024
    ...     out = rotate_logs(now=dt.datetime.now())
    ...     archived = sorted((logs / "archive").glob("*.gz"))
    ...     for key, value in old.items():
    ...         rotate_logs.__globals__[key] = value
    >>> (out[0] >= 1, len(archived) == 1, not old_log.exists())
    (True, True, True)
    """
    now_dt = now if isinstance(now, dt.datetime) else dt.datetime.now()
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = now_dt - dt.timedelta(days=LOG_RETENTION_DAYS)
    compressed = 0
    evicted = 0

    for path in sorted(LOGS_DIR.glob("*.log")):
        try:
            mtime = dt.datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if mtime > cutoff:
            continue
        archive_path = LOG_ARCHIVE_DIR / f"{path.name}.gz"
        with path.open("rb") as src, gzip.open(archive_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.utime(archive_path, (path.stat().st_atime, path.stat().st_mtime))
        path.unlink(missing_ok=True)
        compressed += 1

    def total_bytes():
        total = 0
        for candidate in LOGS_DIR.rglob("*"):
            if candidate.is_file():
                total += candidate.stat().st_size
        return total

    total = total_bytes()
    archives = sorted(
        (p for p in LOG_ARCHIVE_DIR.glob("*.gz") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    while total > LOG_SIZE_CAP_BYTES and archives:
        victim = archives.pop(0)
        try:
            total -= victim.stat().st_size
        except OSError:
            pass
        victim.unlink(missing_ok=True)
        evicted += 1
    return compressed, evicted, max(total, 0)


def queue_dir(state):
    if state not in STATES:
        raise ValueError(f"unknown state: {state}")
    d = QUEUE_ROOT / state
    d.mkdir(parents=True, exist_ok=True)
    return d


def task_path(task_id, state):
    return queue_dir(state) / f"{task_id}.json"


def new_task(
    *,
    role,
    engine,
    project,
    summary,
    source,
    braid_template=None,
    braid_generate_if_missing=True,
    parent_task_id=None,
    feature_id=None,
    engine_args=None,
    depends_on=None,
    worktree=None,
    base_branch=None,
):
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES}: {role}")
    if engine not in VALID_ENGINES:
        raise ValueError(f"engine must be one of {VALID_ENGINES}: {engine}")
    task_id = f"task-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    return {
        "task_id": task_id,
        "role": role,
        "engine": engine,
        "project": project,
        "summary": summary,
        "source": source,
        "parent_task_id": parent_task_id,
        "feature_id": feature_id,
        "state": "queued",
        "braid_template": braid_template,
        "braid_template_path": None,
        "braid_template_hash": None,
        "braid_generate_if_missing": braid_generate_if_missing,
        "worktree": worktree,
        "base_branch": base_branch,
        "log_path": None,
        "artifacts": [],
        "engine_args": engine_args or {},
        "depends_on": list(depends_on or []),
        "topology_error": None,
        "blocker": None,
        "attempt": 1,
        "attempt_history": [],
        "created_at": now_iso(),
        "claimed_at": None,
        "started_at": None,
        "finished_at": None,
    }


def enqueue_task(task):
    path = _write_task_record(task, "queued")
    append_transition(task["task_id"], "new", "queued", task.get("source", ""))
    append_event(
        task.get("role", "unknown"),
        "task_enqueued",
        task_id=task.get("task_id"),
        feature_id=task.get("feature_id"),
        details={"project": task.get("project"), "source": task.get("source"), "braid_template": task.get("braid_template")},
    )
    append_metric(
        "task.enqueued",
        1,
        metric_type="counter",
        tags={"engine": task.get("engine"), "role": task.get("role"), "project": task.get("project")},
        source=task.get("source") or "runtime",
    )
    _nudge_engine_workers(task.get("engine"))
    return path


def _worker_launch_labels(engine):
    if engine == "claude":
        return ("com.devmini.orchestrator.worker.claude",)
    if engine == "qa":
        return ("com.devmini.orchestrator.worker.qa",)
    if engine == "codex":
        return (
            "com.devmini.orchestrator.worker.codex",
            "com.devmini.orchestrator.worker.codex-2",
            "com.devmini.orchestrator.worker.codex-3",
            "com.devmini.orchestrator.worker.codex-4",
            "com.devmini.orchestrator.worker.codex-5",
            "com.devmini.orchestrator.worker.codex-6",
        )
    return ()


def _nudge_engine_workers(engine):
    """Ask launchd to start the worker(s) for a queued engine task.

    This avoids relying on KeepAlive/throttle timing after a clean no-work
    exit. Failures are non-fatal: workers may still wake on their own.
    """
    labels = _worker_launch_labels(engine)
    if not labels:
        return []
    uid = str(os.getuid())
    attempted = []
    for label in labels:
        attempted.append(label)
        try:
            proc = subprocess.run(
                ["launchctl", "kickstart", f"gui/{uid}/{label}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
            if proc.returncode == 0:
                break
        except (OSError, subprocess.SubprocessError):
            continue
    return attempted


def _nudge_idle_queued_workers():
    """Kick idle engine workers when runnable queued work exists.

    This is a recovery path for cases where launchd keeps a worker label loaded
    but does not promptly respawn it after a clean no-work exit.
    """
    active = engine_active_counts()
    queued_by_engine = {"claude": 0, "codex": 0, "qa": 0}
    for task in iter_tasks(states=("queued",)):
        eng = task.get("engine")
        if eng in queued_by_engine:
            queued_by_engine[eng] += 1
    nudged = {}
    for eng, count in queued_by_engine.items():
        if count <= 0 or active.get(eng, 0) > 0:
            continue
        labels = _nudge_engine_workers(eng)
        if labels:
            nudged[eng] = labels
    return nudged


def move_task(task_id, from_state, to_state, reason="", mutator=None):
    """Atomically move a task file between state subdirs. Returns (new_path, task_dict).

    If `mutator` is given, it receives the loaded dict, mutates it, and the
    mutated form is written to the destination path (not the source).
    """
    found = find_task(task_id, states=(from_state,))
    if not found:
        raise FileNotFoundError(f"no task {task_id} in {from_state}")
    _, task = found
    if mutator is not None:
        mutator(task)
    task["state"] = to_state
    if to_state not in ("blocked", "failed", "abandoned"):
        clear_task_blocker(task)
    _write_task_record(task, to_state)
    _delete_task_record(task_id, from_state)
    append_transition(task_id, from_state, to_state, reason)
    append_event(
        task.get("role", "unknown"),
        "task_transition",
        task_id=task.get("task_id"),
        feature_id=task.get("feature_id"),
        details={
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
            "project": task.get("project"),
            "blocker_code": (task.get("blocker") or {}).get("code"),
        },
    )
    append_metric(
        "task.transition",
        1,
        metric_type="counter",
        tags={
            "from_state": from_state,
            "to_state": to_state,
            "engine": task.get("engine"),
            "role": task.get("role"),
            "project": task.get("project"),
            "blocker_code": (task.get("blocker") or {}).get("code") or "",
        },
        source=task.get("source") or "runtime",
    )
    return task_path(task_id, to_state), task


def _archive_attempt_entry(task, from_state, reason, *, source=None):
    snapshot = {}
    for field in ATTEMPT_ARCHIVE_FIELDS:
        value = task.get(field)
        if value is not None:
            snapshot[field] = value
    return {
        "attempt": int(task.get("attempt", 1) or 1),
        "from_state": from_state,
        "retry_reason": reason,
        "retry_source": source or "runtime",
        "archived_at": now_iso(),
        "snapshot": snapshot,
    }


def reset_task_for_retry(task_id, from_state, *, reason, source=None, mutator=None):
    """Archive the current attempt, clear transient failure fields, and requeue.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     old_root = reset_task_for_retry.__globals__["QUEUE_ROOT"]
    ...     old_subprocess = reset_task_for_retry.__globals__["subprocess"]
    ...     reset_task_for_retry.__globals__["QUEUE_ROOT"] = root / "queue"
    ...     reset_task_for_retry.__globals__["subprocess"] = subprocess
    ...     task = new_task(role="implementer", engine="codex", project="demo", summary="x", source="test")
    ...     task["task_id"] = "task-1"
    ...     task["state"] = "failed"
    ...     task["attempt"] = 1
    ...     task["failure"] = "boom"
    ...     task["finished_at"] = "2026-04-15T21:00:00"
    ...     write_json_atomic(task_path("task-1", "failed"), task)
    ...     _ = reset_task_for_retry("task-1", "failed", reason="retry", source="doctest")
    ...     out = read_json(task_path("task-1", "queued"))
    ...     reset_task_for_retry.__globals__["QUEUE_ROOT"] = old_root
    ...     reset_task_for_retry.__globals__["subprocess"] = old_subprocess
    >>> (out["attempt"], out["failure"] is None, out["attempt_history"][0]["snapshot"]["failure"], out["attempt_history"][0]["retry_source"])
    (2, True, 'boom', 'doctest')
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     old_root = reset_task_for_retry.__globals__["QUEUE_ROOT"]
    ...     old_subprocess = reset_task_for_retry.__globals__["subprocess"]
    ...     old_report = reset_task_for_retry.__globals__["REPORT_DIR"]
    ...     old_should = reset_task_for_retry.__globals__["should_push_alert"]
    ...     old_max = reset_task_for_retry.__globals__["max_task_attempts"]
    ...     reset_task_for_retry.__globals__["QUEUE_ROOT"] = root / "queue"
    ...     reset_task_for_retry.__globals__["subprocess"] = subprocess
    ...     reset_task_for_retry.__globals__["REPORT_DIR"] = root / "reports"
    ...     reset_task_for_retry.__globals__["should_push_alert"] = lambda key, seconds: True
    ...     reset_task_for_retry.__globals__["max_task_attempts"] = lambda cfg=None: 5
    ...     task = new_task(role="implementer", engine="codex", project="demo", summary="x", source="test")
    ...     task["task_id"] = "task-2"
    ...     task["state"] = "failed"
    ...     task["attempt"] = 5
    ...     task["failure"] = "still failing"
    ...     write_json_atomic(task_path("task-2", "failed"), task)
    ...     exhausted = reset_task_for_retry("task-2", "failed", reason="retry", source="doctest")
    ...     queued_exists = task_path("task-2", "queued").exists()
    ...     alert_exists = any((root / "reports").glob("workflow-alert_*.md"))
    ...     reset_task_for_retry.__globals__["QUEUE_ROOT"] = old_root
    ...     reset_task_for_retry.__globals__["subprocess"] = old_subprocess
    ...     reset_task_for_retry.__globals__["REPORT_DIR"] = old_report
    ...     reset_task_for_retry.__globals__["should_push_alert"] = old_should
    ...     reset_task_for_retry.__globals__["max_task_attempts"] = old_max
    >>> (exhausted["blocker"]["code"], exhausted["state"], queued_exists, alert_exists)
    ('attempt_exhausted', 'abandoned', False, True)
    """
    current_task = read_json(task_path(task_id, from_state), {})
    if not current_task:
        raise FileNotFoundError(f"no task {task_id} in {from_state}")
    current_attempt = int(current_task.get("attempt", 1) or 1)
    attempt_limit = max_task_attempts()
    if current_attempt >= attempt_limit:
        detail = f"{reason} (attempt {current_attempt} >= max {attempt_limit})"
        def mut_abandon(task):
            task["finished_at"] = now_iso()
            task["abandoned_reason"] = detail
            task["last_retry_at"] = now_iso()
            task["last_retry_reason"] = reason
            task["last_retry_source"] = source or "runtime"
            set_task_blocker(
                task,
                "attempt_exhausted",
                summary="task attempt limit exhausted",
                detail=detail,
                source=source or "runtime",
                retryable=False,
                metadata={"attempt": current_attempt, "max_attempts": attempt_limit},
            )
        _, out = move_task(task_id, from_state, "abandoned", reason=detail, mutator=mut_abandon)
        append_event(
            source or "runtime",
            "retry_exhausted",
            task_id=task_id,
            feature_id=current_task.get("feature_id"),
            details={"from_state": from_state, "reason": reason, "attempt": current_attempt, "max_attempts": attempt_limit},
        )
        _write_workflow_alert(
            {
                "feature_id": current_task.get("feature_id"),
                "project": current_task.get("project"),
                "summary": current_task.get("summary") or task_id,
                "issue_key": f"task:{task_id}:attempt_exhausted",
                "kind": "frontier_task_blocked",
                "task_id": task_id,
                "task_state": from_state,
                "blocker": out.get("blocker"),
                "task": out,
            },
            detail,
        )
        return out

    feature_id_holder = {"value": None}
    new_attempt_holder = {"value": None}

    def mut(task):
        feature_id_holder["value"] = task.get("feature_id")
        history = list(task.get("attempt_history") or [])
        history.append(_archive_attempt_entry(task, from_state, reason, source=source))
        task["attempt_history"] = history
        task["attempt"] = int(task.get("attempt", 1) or 1) + 1
        new_attempt_holder["value"] = task["attempt"]
        task["last_retry_at"] = now_iso()
        task["last_retry_reason"] = reason
        task["last_retry_source"] = source or "runtime"
        for field in RETRY_CLEAR_FIELDS:
            task[field] = None
        clear_task_blocker(task)
        if mutator is not None:
            mutator(task)
    out = move_task(task_id, from_state, "queued", reason=reason, mutator=mut)
    removed_worktree = None
    found = find_task(task_id, states=("queued",))
    if found:
        _, task = found
        removed_worktree = _remove_task_worktree_if_present(task)
    append_event(
        source or "runtime",
        "retry_reset",
        task_id=task_id,
        feature_id=feature_id_holder["value"],
        details={
            "from_state": from_state,
            "reason": reason,
            "attempt": new_attempt_holder["value"],
            "worktree_cleanup": removed_worktree,
        },
    )
    if found:
        _, task = found
        _nudge_engine_workers(task.get("engine"))
    return out


def _atomic_claim_doctest_case(dep_state):
    with tempfile.TemporaryDirectory() as tmp:
        old_root = atomic_claim.__globals__["QUEUE_ROOT"]
        old_now = atomic_claim.__globals__["now_iso"]
        old_env_ok = atomic_claim.__globals__["project_environment_ok"]
        atomic_claim.__globals__["QUEUE_ROOT"] = pathlib.Path(tmp)
        atomic_claim.__globals__["now_iso"] = lambda: "2026-04-14T12:00:00"
        atomic_claim.__globals__["project_environment_ok"] = lambda *args, **kwargs: True
        try:
            for state in STATES:
                _ = queue_dir(state)
            dep = new_task(role="implementer", engine="codex", project="demo", summary="dep", source="x")
            dep["task_id"] = "task-dep"
            write_json_atomic(task_path("task-dep", dep_state), dep)
            blocked = new_task(role="implementer", engine="codex", project="demo", summary="blocked", source="x", depends_on=["task-dep"])
            blocked["task_id"] = "task-blocked"
            write_json_atomic(task_path("task-blocked", "queued"), blocked)
            if dep_state == "done":
                return atomic_claim("codex")["task_id"]
            ready = new_task(role="implementer", engine="codex", project="demo", summary="ready", source="x")
            ready["task_id"] = "task-ready"
            write_json_atomic(task_path("task-ready", "queued"), ready)
            return atomic_claim("codex")["task_id"]
        finally:
            atomic_claim.__globals__["QUEUE_ROOT"] = old_root
            atomic_claim.__globals__["now_iso"] = old_now
            atomic_claim.__globals__["project_environment_ok"] = old_env_ok


def project_hard_stopped(project_name):
    """Return True if the project has any blocked task tagged regression-failure.
    Plan §5 hard stop: no implementer/planner work runs for a project until a
    human reviews and clears the blocked regression task."""
    if not project_name:
        return False
    if project_name in _read_project_hard_stops():
        return True
    for task in iter_tasks(states=("blocked",), project=project_name):
        if task.get("topology_error") == "regression-failure":
            return True
    return False


def atomic_claim(slot_engine):
    """Find oldest queued task matching engine and atomically move it to claimed/.

    Returns the claimed task dict or None if no work is available. Uses
    os.rename, which is atomic on APFS — the worker that wins the rename owns
    the task; the losers get FileNotFoundError and move on.

    Claude and codex slots additionally skip tasks whose project is
    hard-stopped by a regression-failure block (Plan §5). QA slot is exempt —
    smoke/regression are diagnostic and must still run.

    Codex slot also skips tasks whose feature_id is already held by another
    claimed/running task — sibling slices of the same feature serialize to
    avoid concurrent merges on the shared feature branch.

    >>> _atomic_claim_doctest_case("done")
    'task-blocked'
    >>> _atomic_claim_doctest_case("running")
    'task-ready'
    """
    if slot_paused(slot_engine):
        return None
    crash_guard = crash_loop_guard_status(slot_engine)
    if crash_guard.get("suppressed"):
        append_event(
            "worker",
            "claim_suppressed",
            details={
                "slot": slot_engine,
                "reason": "crash_loop",
                "crash_count": len(crash_guard.get("crashes") or []),
                "window_seconds": crash_guard.get("window_seconds"),
            },
        )
        return None

    if _state_engine_read_enabled():
        candidates = iter_tasks(states=("queued",), engine=slot_engine)
    else:
        queued = queue_dir("queued")
        candidates = sorted(queued.glob("*.json"), key=lambda p: p.stat().st_mtime)
    busy_features = in_flight_feature_ids() if slot_engine == "codex" else set()
    paused_templates = {
        name for name, entry in load_braid_index().items()
        if entry.get("dispatch_paused")
    } if slot_engine == "codex" else set()
    for candidate in candidates:
        if _state_engine_read_enabled():
            task = dict(candidate or {})
            src = task_path(task.get("task_id"), "queued")
        else:
            src = candidate
            try:
                task = read_json(src)
            except (OSError, json.JSONDecodeError):
                continue
            if task is None:
                continue
            if task.get("engine") != slot_engine:
                continue
        if slot_engine in ("claude", "codex") and project_hard_stopped(task.get("project")):
            continue
        project_name = task.get("project")
        if (
            project_name
            and not project_environment_ok(project_name)
        ):
            if not _task_allows_environment_bypass(task):
                continue
            _record_task_bypass(
                task["task_id"],
                "project_environment_ok",
                f"self-repair bypass for degraded project {project_name}",
            )
        if slot_engine == "codex":
            fid = task.get("feature_id")
            if fid and fid in busy_features:
                continue
            if task.get("braid_template") in paused_templates:
                continue
        deps = task.get("depends_on") or []
        if deps:
            blocked = False
            for dep_id in deps:
                if _task_exists_in_queue(dep_id, states=("done",)):
                    continue
                if _task_exists_in_queue(dep_id, states=("queued", "claimed", "running")):
                    blocked = True
                    break
                blocked = True
                break
            if blocked:
                continue
        dst = task_path(task["task_id"], "claimed")
        if _state_engine_read_enabled():
            claimed = get_state_engine().claim_task(
                task["task_id"],
                slot_engine=slot_engine,
                claimed_at=now_iso(),
            )
            if claimed is None:
                continue
            task = claimed
            _delete_task_record(task["task_id"], "queued")
            _write_task_record(task, "claimed")
        else:
            try:
                os.rename(src, dst)
            except FileNotFoundError:
                continue  # lost the race
            task["state"] = "claimed"
            task["claimed_at"] = now_iso()
            write_json_atomic(dst, task)
        append_transition(task["task_id"], "queued", "claimed", slot_engine)
        return task
    return None


def write_claim_pid(task_id, slot, worktree=None):
    CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    pidfile = CLAIMS_DIR / f"{task_id}.pid"
    pidfile.write_text(f"{os.getpid()}\n{slot}\n{worktree or ''}\n")


def clear_claim_pid(task_id):
    pidfile = CLAIMS_DIR / f"{task_id}.pid"
    try:
        pidfile.unlink()
    except FileNotFoundError:
        pass


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        try:
            proc = subprocess.run(
                ["ps", "-p", str(pid), "-o", "pid="],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0 and bool((proc.stdout or "").strip())
    return True


def _task_allows_environment_bypass(task):
    """Whether a queued task may run even when its project env is degraded.

    Self-repair lanes must be able to repair the very project/runtime defect
    that made normal product work ineligible to claim.
    """
    eargs = (task or {}).get("engine_args") or {}
    repair = eargs.get("self_repair") or {}
    return bool(repair.get("enabled"))


def _remove_task_worktree_if_present(task):
    worktree = (task or {}).get("worktree")
    if not worktree:
        return False, "no worktree"
    wt_path = pathlib.Path(worktree)
    if not wt_path.exists():
        return True, "worktree already absent"
    proc = subprocess.run(
        ["git", "-C", str(STATE_ROOT), "worktree", "remove", "--force", str(wt_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode == 0:
        return True, "git worktree remove --force"
    try:
        shutil.rmtree(wt_path)
    except OSError as exc:
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, f"{detail[:160] or 'remove failed'}; fallback rmtree failed: {exc}"
    return True, "fallback rmtree"


def reap():
    """Return stale claimed/running tasks to the queue when their worker died."""
    CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
    reaped = 0
    for pidfile in CLAIMS_DIR.glob("*.pid"):
        task_id = pidfile.stem
        try:
            lines = pidfile.read_text().splitlines()
            pid = int(lines[0])
        except (OSError, ValueError, IndexError):
            pidfile.unlink(missing_ok=True)
            continue
        if pid_alive(pid):
            continue
        # Find the task in claimed/ or running/
        for state in ("claimed", "running"):
            src = task_path(task_id, state)
            if src.exists():
                task = read_json(src, {}) or {}
                age_seconds = _seconds_since_iso(task.get("started_at") or task.get("claimed_at") or task.get("created_at")) or 0
                _record_orphan_recovery(task_id, state, age_seconds)
                reset_task_for_retry(
                    task_id,
                    state,
                    reason=f"reaper: pid {pid} dead",
                    source="reaper",
                )
                reaped += 1
                break
        pidfile.unlink(missing_ok=True)
    # Also: transition blocked tasks that have a regenerated template back to queued.
    for task in iter_tasks(states=("blocked",)):
        if task.get("topology_error") == "template_missing":
            tmpl = task.get("braid_template")
            if tmpl and (BRAID_TEMPLATES / f"{tmpl}.mmd").exists():
                reset_task_for_retry(
                    task["task_id"],
                    "blocked",
                    reason="reaper: template regenerated",
                    source="reaper",
                    mutator=lambda t: t.update(
                        braid_template_hash=None,
                    ),
                )
                reaped += 1
                continue
        refine = task.get("refine_request") or {}
        tmpl = task.get("braid_template")
        old_hash = refine.get("template_hash")
        if tmpl and old_hash:
            _, current_hash = braid_template_load(tmpl)
            if current_hash and current_hash != old_hash:
                reset_task_for_retry(
                    task["task_id"],
                    "blocked",
                    reason="reaper: template refined",
                    source="reaper",
                    mutator=lambda t: t.update(
                        braid_template_hash=None,
                    ),
                )
                reaped += 1
    tick_braid_auto_regen(load_config())
    nudged = _nudge_idle_queued_workers()
    if nudged:
        append_event("reaper", "idle_workers_nudged", details=nudged)
    return reaped


# --- PR sweep ---------------------------------------------------------------
#
# Feature-branch delivery model means task PRs target `feature/<id>`, not main.
# pr-sweep is the tick that drives them through to merge:
#
#   1. For each queue/done/*.json with pr_number set and no cleaned_at:
#      - gh pr view --json state,mergeable,mergeStateStatus,reviewDecision,
#        reviews,comments,headRefOid,author
#   2. If PR is MERGED or CLOSED: skip (cleanup-worktrees handles local state).
#   3. If PR is MERGEABLE, reviewDecision not CHANGES_REQUESTED, and no
#      actionable unhandled comments from auto-handle authors: auto-merge via
#      `gh pr merge <n> --squash --delete-branch`. Stamp auto_merged_at etc.
#   4. If PR has new actionable comments from auto-handle authors: enqueue a
#      codex pr-feedback task, record comment_ids in pr_sweep.handled_comment_ids
#      so we don't re-enqueue the same comments on the next tick.
#   5. If PR is CONFLICTING (mergeable=CONFLICTING or mergeStateStatus in
#      DIRTY/BEHIND): enqueue a pr-feedback task. The BRAID graph instructs
#      the agent to rebase onto base first before touching any comments. The
#      task's engine_args carries a `[CONFLICT PREVIEW]` block (conflict list
#      + diff stats + recent base log, 4000-char budget) so the solver sees
#      the rebase surface up front. The parent task's pr_sweep.conflict_task_id
#      pins the active guard slice; subsequent sweeps of the same parent skip
#      dispatch while the guard task is still in queued/claimed/running.
#   6. Drift probe: if mergeable=MERGEABLE but the worktree HEAD is
#      `drift_threshold` or more commits behind its base (configurable,
#      default 5), synthesise mergeStateStatus=BEHIND so Case 1 fires a
#      drift_sync pr-feedback task. Prevents slow-rot merges that technically
#      pass gh but have drifted far enough to surprise reviewers.
#   7. If pr_sweep.feedback_rounds >= PR_SWEEP_MAX_FEEDBACK_ROUNDS: stop
#      enqueuing more feedback tasks and write a Telegram alert instead so a
#      human can take over.
#
# Auto-merge only targets feature branches, never main. Feature->main PRs are
# human-merged; pr-sweep only serves them with cleanup + alerts.

AUTO_HANDLE_COMMENT_AUTHORS = {
    "chatgpt-codex-connector",
    "copilot",
    "github-advanced-security",
}

PR_SWEEP_MAX_FEEDBACK_ROUNDS = 8


def _pr_body_has_orchestrator_mention(body):
    if not body:
        return False
    b = body.lower()
    return "@devmini-orchestrator" in b or "@orchestrator" in b


def _comment_is_actionable(comment):
    """Return True if this comment should trigger a pr-feedback cycle.

    Actionable = authored by an auto-handle bot OR explicitly @-mentions the
    orchestrator. Everything else (casual human discussion, re-review requests,
    etc.) is ignored — pr-sweep only responds to unambiguous machine-or-marked
    requests.

    >>> _comment_is_actionable({"author": {"login": "chatgpt-codex-connector"}, "body": "Codex Review: Didn't find any major issues. Bravo."})
    False
    >>> _comment_is_actionable({"author": {"login": "chatgpt-codex-connector"}, "body": "![P1 Badge] Must fix before merge"})
    True
    """
    author = ((comment.get("author") or {}).get("login", "") or "").lower()
    body = comment.get("body", "") or ""
    lower = body.lower()
    if author in AUTO_HANDLE_COMMENT_AUTHORS:
        non_actionable_markers = (
            "didn't find any major issues",
            "did not find any major issues",
            "codex review:",
            "about codex in github",
            "jmh smoke summary",
        )
        if any(marker in lower for marker in non_actionable_markers):
            return False
        actionable_markers = (
            "![p0 badge]",
            "![p1 badge]",
            "![p2 badge]",
            "![p3 badge]",
            "must fix",
            "needs fix",
            "request changes",
            "security finding",
            "code scanning alert",
        )
        if any(marker in lower for marker in actionable_markers):
            return True
    return _pr_body_has_orchestrator_mention(body)


def _extract_failed_checks(pr_info):
    """Return failed or errored check-rollup entries worth a self-heal round.

    >>> payload = {"statusCheckRollup": [
    ...     {"name": "unresolved-review-threads", "conclusion": "FAILURE", "detailsUrl": "https://example/1"},
    ...     {"name": "lint", "conclusion": "SUCCESS"},
    ...     {"workflowName": "CI", "status": "COMPLETED", "conclusion": "TIMED_OUT", "url": "https://example/2"},
    ... ]}
    >>> [c["name"] for c in _extract_failed_checks(payload)]
    ['unresolved-review-threads', 'CI']
    """
    out = []
    for item in pr_info.get("statusCheckRollup", []) or []:
        conclusion = str(item.get("conclusion") or item.get("state") or item.get("status") or "").upper()
        if conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED", "PENDING", "IN_PROGRESS", "EXPECTED", ""):
            continue
        name = item.get("name") or item.get("workflowName") or item.get("context") or item.get("__typename") or "check"
        out.append({
            "name": str(name),
            "conclusion": conclusion,
            "details_url": item.get("detailsUrl") or item.get("url") or "",
        })
    return out


def _unresolved_bot_review_threads(repo_path, pr_number, pr_url=None):
    """Return unresolved PR review threads started by allowlisted bots.

    Shape matches `_extract_actionable_comments` so the result can be
    handed straight to `_enqueue_pr_feedback`:
        {id, author, body, created_at, thread_id, url, path, line}
    where `id` is the comment databaseId (used for dedup against
    handled_comment_ids) and `thread_id` is the GraphQL node id of the
    owning review thread (used by worker.py after the fix lands to call
    resolveReviewThread and close the self-heal loop).

    Same AUTO_HANDLE_COMMENT_AUTHORS allowlist as `_comment_is_actionable`,
    but this path uses GraphQL to reach `reviewThreads.isResolved`, which
    the REST `comments` field on `gh pr view` does NOT expose. Without
    this check, a P2 review-thread comment from chatgpt-codex-connector
    is invisible to pr-sweep — the PR shows reviewDecision="" (no formal
    "Changes Requested") and Case 4 merges through it. That is how PR
    #13 shipped a buggy AgronaHistogram.addToBucket past an unresolved
    bot review.

    Mirrors the GraphQL query in
    .github/workflows/unresolved-bot-review.yml so the client-side and
    GitHub-side gates agree on what counts as blocking.

    Fail-open on GH API errors: returns [] so a transient GraphQL flake
    does not stall every auto-merge. The GitHub Action remains the
    belt-and-suspenders backstop when it is wired up as a required status
    check on the target branch.
    """
    owner = name = None
    if pr_url:
        m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/pull/", pr_url)
        if m:
            owner, name = m.group(1), m.group(2)
    if not owner or not name:
        try:
            gp = subprocess.run(
                ["gh", "repo", "view", "--json", "owner,name"],
                cwd=repo_path,
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            return []
        if gp.returncode == 0:
            try:
                d = json.loads(gp.stdout or "{}")
                owner = (d.get("owner") or {}).get("login")
                name = d.get("name")
            except json.JSONDecodeError:
                return []
    if not owner or not name:
        return []

    query = (
        "query($owner:String!, $repo:String!, $number:Int!) {"
        "  repository(owner:$owner, name:$repo) {"
        "    pullRequest(number:$number) {"
        "      reviewThreads(first:100) {"
        "        pageInfo { hasNextPage }"
        "        nodes {"
        "          id"
        "          isResolved"
        "          path"
        "          line"
        "          comments(first:1) {"
        "            nodes {"
        "              databaseId"
        "              author { login }"
        "              url"
        "              body"
        "              createdAt"
        "            }"
        "          }"
        "        }"
        "      }"
        "    }"
        "  }"
        "}"
    )
    try:
        proc = subprocess.run(
            ["gh", "api", "graphql",
             "-F", f"owner={owner}",
             "-F", f"repo={name}",
             "-F", f"number={pr_number}",
             "-f", f"query={query}"],
            cwd=repo_path,
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return []
    if proc.returncode != 0:
        print(f"pr-sweep: graphql reviewThreads failed: {(proc.stderr or '').strip()[:200]}")
        return []
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return []

    threads = (
        (((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {})
        .get("reviewThreads", {})
        .get("nodes", [])
    ) or []
    out = []
    for t in threads:
        if t.get("isResolved"):
            continue
        first_nodes = (t.get("comments") or {}).get("nodes") or []
        if not first_nodes:
            continue
        first = first_nodes[0]
        login = ((first.get("author") or {}).get("login") or "").lower()
        normalized = login[:-5] if login.endswith("[bot]") else login
        if normalized not in AUTO_HANDLE_COMMENT_AUTHORS:
            continue
        comment_id = first.get("databaseId")
        thread_id = t.get("id")
        if comment_id is None or not thread_id:
            continue
        out.append({
            "id": str(comment_id),
            "author": (first.get("author") or {}).get("login", ""),
            "body": (first.get("body") or "")[:4000],
            "created_at": first.get("createdAt", ""),
            "thread_id": thread_id,
            "url": first.get("url"),
            "path": t.get("path"),
            "line": t.get("line"),
        })
    return out


def _resolve_review_threads(repo_path, thread_ids):
    """Mark the given review thread node IDs resolved via GraphQL mutation.

    Called after a pr-feedback task successfully pushes its fix — closes
    the self-heal loop so the next pr-sweep tick sees isResolved=true and
    auto-merges, rather than re-alerting on the same thread forever.
    chatgpt-codex-connector does NOT mark its own threads resolved after a
    fix, so if the orchestrator does not resolve them itself the PR sits
    blocked in pr-sweep until a human clicks "Resolve conversation".

    Returns (resolved_count, failed_count). Fails soft — any single
    mutation error is logged but does not raise, and the caller is
    expected to move the task to done regardless (the fix is already
    pushed; an unresolved thread is a re-check, not a regression).
    """
    if not thread_ids:
        return (0, 0)
    mutation = (
        "mutation($tid: ID!) {"
        "  resolveReviewThread(input: {threadId: $tid}) {"
        "    thread { id isResolved }"
        "  }"
        "}"
    )
    resolved = failed = 0
    seen = set()
    for tid in thread_ids:
        if not tid or tid in seen:
            continue
        seen.add(tid)
        try:
            proc = subprocess.run(
                ["gh", "api", "graphql",
                 "-F", f"tid={tid}",
                 "-f", f"query={mutation}"],
                cwd=repo_path,
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            failed += 1
            print(f"resolveReviewThread: timeout on {tid}")
            continue
        if proc.returncode != 0:
            failed += 1
            print(f"resolveReviewThread {tid}: {(proc.stderr or '').strip()[:200]}")
            continue
        resolved += 1
    return (resolved, failed)


def _extract_actionable_comments(pr_info, already_handled):
    """Return list of {id, author, body, created_at} for unhandled actionables."""
    out = []
    seen = set(already_handled or [])
    for c in pr_info.get("comments", []) or []:
        cid = str(c.get("id") or c.get("databaseId") or "")
        if not cid or cid in seen:
            continue
        if not _comment_is_actionable(c):
            continue
        out.append({
            "id": cid,
            "author": (c.get("author") or {}).get("login", ""),
            "body": (c.get("body") or "")[:4000],
            "created_at": c.get("createdAt", ""),
        })
    return out


def _write_pr_alert(project_name, target_id, pr_number, reason, pr_url):
    """Drop a markdown alert into REPORT_DIR so the telegram bot fans it out.

    Mirrors the regression-alert pattern already used for regression-failure
    hard stops, so the same telegram poller picks it up with no wiring changes.
    """
    ts = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    alert_path = REPORT_DIR / f"pr-sweep_{project_name}_pr{pr_number}_{ts}.md"
    body = [
        f"# PR ATTENTION — {project_name} #{pr_number}",
        "",
        f"- task: `{target_id}`",
        f"- pr: {pr_url or '(unknown)'}",
        f"- reason: {reason}",
        f"- time: {now_iso()}",
        "",
        "pr-sweep could not make progress autonomously. A human needs to "
        "review and either resolve the comment thread, push a manual fix, "
        "or close the PR.",
        "",
    ]
    alert_path.write_text("\n".join(body))
    return alert_path


def _write_workflow_alert(issue, reason):
    feature_id = issue.get("feature_id") or "env:global"
    issue_key = issue.get("issue_key") or f"{feature_id}:{issue.get('kind') or 'workflow'}"
    event_key = f"workflow-alert:{issue_key}"
    if not should_push_alert(event_key, 6 * 3600):
        return None

    ts = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", issue_key)[:120]
    alert_path = REPORT_DIR / f"workflow-alert_{slug}_{ts}.md"
    blocker = issue.get("blocker") or {}
    lines = [
        f"# WORKFLOW ATTENTION — {issue.get('project') or 'unknown-project'}",
        "",
        f"- feature: `{feature_id}`",
        f"- issue_key: `{issue_key}`",
        f"- kind: `{issue.get('kind') or '-'}`",
        f"- task: `{issue.get('task_id') or '-'}`",
        f"- blocker: `{blocker.get('code') or '-'}`",
        f"- reason: {reason}",
        f"- time: {now_iso()}",
        "",
    ]
    if blocker.get("detail"):
        lines.extend(["## Blocker Detail", "", blocker.get("detail"), ""])
    if blocker.get("code") in ("slot_paused", "claude_budget_exhausted"):
        lines.extend([
            "## Operator Action",
            "",
            "Resume the paused slot explicitly:",
            '`python3 bin/orchestrator.py slot-resume claude --reason "workflow-check recovery"`',
            "",
        ])
    else:
        lines.extend([
            "workflow-check could not recover this lane autonomously and has escalated it for human attention.",
            "",
        ])
    alert_path.write_text("\n".join(lines))
    return alert_path


def _gh_auth_failed(stderr_text):
    """Best-effort detector for gh auth/401 failures."""
    text = (stderr_text or "").lower()
    needles = (
        "http 401",
        "requires authentication",
        "authentication failed",
        "not logged into any github hosts",
        "gh auth login",
    )
    return any(n in text for n in needles)


def _parse_pr_create_output(stdout_text):
    """Extract (url, number) from `gh pr create` stdout."""
    pr_url = None
    for line in reversed((stdout_text or "").splitlines()):
        line = line.strip()
        if line.startswith("http"):
            pr_url = line
            break
    pr_number = None
    if pr_url:
        m = re.search(r"/pull/(\d+)", pr_url)
        if m:
            pr_number = int(m.group(1))
    return pr_url, pr_number


def sync_operator_branch_with_main(repo_path, *, branch=None):
    """Fetch origin/main and merge it into the operator branch before PR open.

    Uses `-X ours` so branch-local operator changes win on conflicting hunks
    while still absorbing the rest of upstream main automatically.
    """
    repo = pathlib.Path(repo_path).resolve()
    env = gh_subprocess_env()
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    current = (branch or proc.stdout or "").strip()
    if not current or current in ("HEAD", "main"):
        return {"ok": False, "reason": f"refuse to sync branch {current or '(unknown)'}"}

    fetch = subprocess.run(
        ["git", "-C", str(repo), "fetch", "origin", "main"],
        capture_output=True, text=True, check=False, timeout=120, env=env,
    )
    if fetch.returncode != 0:
        return {"ok": False, "reason": (fetch.stderr or fetch.stdout or "git fetch failed").strip()[:400]}

    behind = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", "origin/main", current],
        capture_output=True, text=True, check=False, timeout=30,
    )
    if behind.returncode == 0:
        return {"ok": True, "reason": "already_up_to_date", "branch": current}

    merge = subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-edit", "-X", "ours", "origin/main"],
        capture_output=True, text=True, check=False, timeout=120,
    )
    if merge.returncode != 0:
        subprocess.run(
            ["git", "-C", str(repo), "merge", "--abort"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        return {"ok": False, "reason": (merge.stderr or merge.stdout or "git merge failed").strip()[:400], "branch": current}

    push = subprocess.run(
        ["git", "-C", str(repo), "push", "origin", current],
        capture_output=True, text=True, check=False, timeout=120, env=env,
    )
    if push.returncode != 0:
        return {"ok": False, "reason": (push.stderr or push.stdout or "git push failed").strip()[:400], "branch": current}
    return {"ok": True, "reason": "merged_origin_main", "branch": current}


def open_operator_pr(*, repo_path=None, base_branch="main", head_branch=None, title=None, body_file=None, draft=False, sync_main=True):
    """Open a PR for the current operator branch using repo-managed GH token.

    Defaults to a ready-for-review PR. Use `draft=True` only when you want to
    pause autonomous delivery intentionally.
    """
    repo = pathlib.Path(repo_path or os.getcwd()).resolve()
    if shutil.which("gh") is None:
        return {"ok": False, "reason": "gh CLI not installed"}
    env = gh_subprocess_env()
    if not env.get("GH_TOKEN", "").strip():
        return {"ok": False, "reason": f"GH_TOKEN unavailable in {GH_TOKEN_PATH}"}
    if head_branch is None:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        head_branch = (proc.stdout or "").strip()
    if not head_branch or head_branch in ("HEAD", "main"):
        return {"ok": False, "reason": f"refuse to open PR from branch {head_branch or '(unknown)'}"}
    sync_result = {"ok": True, "reason": "sync_skipped", "branch": head_branch}
    if sync_main:
        sync_result = sync_operator_branch_with_main(repo, branch=head_branch)
        if not sync_result.get("ok"):
            return {"ok": False, "reason": sync_result.get("reason"), "sync": sync_result}
    if title is None:
        proc = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--pretty=%s"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        title = ((proc.stdout or "").strip() or head_branch)[:120]
    cmd = ["gh", "pr", "create", "--head", head_branch, "--base", base_branch, "--title", title]
    if draft:
        cmd.append("--draft")
    if body_file:
        cmd.extend(["--body-file", str(body_file)])
    else:
        cmd.append("--fill")
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "gh pr create timeout", "sync": sync_result}
    if proc.returncode != 0:
        return {"ok": False, "reason": (proc.stderr or proc.stdout or "gh pr create failed").strip()[:400], "sync": sync_result}
    pr_url, pr_number = _parse_pr_create_output(proc.stdout or "")
    return {"ok": True, "pr_url": pr_url, "pr_number": pr_number, "head_branch": head_branch, "base_branch": base_branch, "sync": sync_result}


def pr_sweep(dry_run=False):
    """Sweep open task PRs: auto-merge clean ones, dispatch pr-feedback, alert on stuck.

    Returns (checked, merged, feedback_enqueued, alerted, skipped).

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     features_dir = root / "features"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     features_dir.mkdir(parents=True, exist_ok=True)
    ...     task = {
    ...         "task_id": "task-behind",
    ...         "project": "demo",
    ...         "pr_number": 7,
    ...         "summary": "demo",
    ...         "feature_id": "f1",
    ...         "worktree": str(root / "wt"),
    ...         "pr_sweep": {"feedback_rounds": 0},
    ...     }
    ...     (root / "wt").mkdir()
    ...     task_path = queue_root / "done" / "task-behind.json"
    ...     _ = task_path.write_text(json.dumps(task))
    ...     calls = []
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "FEATURES_DIR", "load_config", "get_project", "_extract_actionable_comments", "_enqueue_pr_feedback", "_write_pr_alert", "_unresolved_bot_review_threads", "now_iso", "subprocess")}
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             if cmd[:3] == ["gh", "pr", "view"]:
    ...                 payload = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND", "reviewDecision": "", "baseRefName": "feature/demo", "url": "https://example/pr/7", "comments": []}
    ...                 return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...             if cmd[:4] == ["git", "-C", str(root / "wt"), "fetch"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    ...             if cmd[:5] == ["git", "-C", str(root / "wt"), "rev-list", "--count"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="0\\n", stderr="")
    ...             raise AssertionError(cmd)
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
    ...     pr_sweep.__globals__["FEATURES_DIR"] = features_dir
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: []
    ...     pr_sweep.__globals__["_enqueue_pr_feedback"] = lambda *args, **kwargs: calls.append(kwargs) or "task-new"
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: None
    ...     pr_sweep.__globals__["now_iso"] = lambda: "2026-04-14T12:00:00"
    ...     pr_sweep.__globals__["subprocess"] = FakeSubprocess()
    ...     _ = pr_sweep()
    ...     saved = json.loads(task_path.read_text())
    ...     observed = ([c["conflicts"] for c in calls], saved["pr_sweep"]["last_feedback_reason"])
    ...     for key, value in original.items():
    ...         pr_sweep.__globals__[key] = value
    >>> observed
    ([True], 'conflict')

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     features_dir = root / "features"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     features_dir.mkdir(parents=True, exist_ok=True)
    ...     wt = root / "wt"
    ...     wt.mkdir()
    ...     task = {
    ...         "task_id": "task-drift",
    ...         "project": "demo",
    ...         "pr_number": 8,
    ...         "summary": "demo",
    ...         "feature_id": "f1",
    ...         "worktree": str(wt),
    ...         "pr_sweep": {"feedback_rounds": 0},
    ...     }
    ...     task_path = queue_root / "done" / "task-drift.json"
    ...     _ = task_path.write_text(json.dumps(task))
    ...     calls = []
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "FEATURES_DIR", "load_config", "get_project", "_extract_actionable_comments", "_enqueue_pr_feedback", "_write_pr_alert", "_unresolved_bot_review_threads", "now_iso", "subprocess")}
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             if cmd[:3] == ["gh", "pr", "view"]:
    ...                 payload = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "reviewDecision": "", "baseRefName": "feature/demo", "url": "https://example/pr/8", "comments": []}
    ...                 return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...             if cmd[:4] == ["git", "-C", str(wt), "fetch"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    ...             if cmd[:5] == ["git", "-C", str(wt), "rev-list", "--count"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="7\\n", stderr="")
    ...             raise AssertionError(cmd)
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
    ...     pr_sweep.__globals__["FEATURES_DIR"] = features_dir
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: []
    ...     pr_sweep.__globals__["_enqueue_pr_feedback"] = lambda *args, **kwargs: calls.append(kwargs) or "task-drift-sync"
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: None
    ...     pr_sweep.__globals__["_unresolved_bot_review_threads"] = lambda *args, **kwargs: []
    ...     pr_sweep.__globals__["now_iso"] = lambda: "2026-04-14T12:00:00"
    ...     pr_sweep.__globals__["subprocess"] = FakeSubprocess()
    ...     _ = pr_sweep()
    ...     saved = json.loads(task_path.read_text())
    ...     observed = (saved["pr_sweep"]["last_feedback_reason"], saved["pr_sweep"]["last_merge_state"], saved["pr_sweep"]["conflict_task_id"])
    ...     for key, value in original.items():
    ...         pr_sweep.__globals__[key] = value
    >>> observed
    ('drift_sync', 'BEHIND', 'task-drift-sync')

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     features_dir = root / "features"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     features_dir.mkdir(parents=True, exist_ok=True)
    ...     wt = root / "wt"
    ...     wt.mkdir()
    ...     task = {
    ...         "task_id": "task-project-threshold",
    ...         "project": "demo",
    ...         "pr_number": 10,
    ...         "summary": "demo",
    ...         "feature_id": "f1",
    ...         "worktree": str(wt),
    ...         "pr_sweep": {"feedback_rounds": 0},
    ...     }
    ...     task_path = queue_root / "done" / "task-project-threshold.json"
    ...     _ = task_path.write_text(json.dumps(task))
    ...     calls = []
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "FEATURES_DIR", "load_config", "get_project", "_extract_actionable_comments", "_enqueue_pr_feedback", "_write_pr_alert", "_unresolved_bot_review_threads", "now_iso", "subprocess")}
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             if cmd[:3] == ["gh", "pr", "view"]:
    ...                 payload = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED", "baseRefName": "feature/demo", "url": "https://example/pr/10", "comments": []}
    ...                 return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...             if cmd[:4] == ["git", "-C", str(wt), "fetch"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    ...             if cmd[:5] == ["git", "-C", str(wt), "rev-list", "--count"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="7\\n", stderr="")
    ...             if cmd[:3] == ["gh", "pr", "merge"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="merged", stderr="")
    ...             raise AssertionError(cmd)
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
    ...     pr_sweep.__globals__["FEATURES_DIR"] = features_dir
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root), "drift_threshold": 10}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: []
    ...     pr_sweep.__globals__["_enqueue_pr_feedback"] = lambda *args, **kwargs: calls.append(kwargs) or "task-drift-sync"
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: None
    ...     pr_sweep.__globals__["_unresolved_bot_review_threads"] = lambda *args, **kwargs: []
    ...     pr_sweep.__globals__["_unresolved_bot_review_threads"] = lambda *args, **kwargs: []
    ...     pr_sweep.__globals__["now_iso"] = lambda: "2026-04-14T12:00:00"
    ...     pr_sweep.__globals__["subprocess"] = FakeSubprocess()
    ...     import contextlib, io
    ...     with contextlib.redirect_stdout(io.StringIO()):
    ...         result = pr_sweep()
    ...     saved = json.loads(task_path.read_text())
    ...     observed = (result, calls, saved["pr_sweep"].get("last_merge_state"), saved.get("auto_merged_at") is not None)
    ...     for key, value in original.items():
    ...         pr_sweep.__globals__[key] = value
    >>> observed
    ((1, 1, 0, 0, 0), [], None, True)

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     features_dir = root / "features"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     features_dir.mkdir(parents=True, exist_ok=True)
    ...     wt = root / "wt"
    ...     wt.mkdir()
    ...     task = {
    ...         "task_id": "task-clean-no-approval",
    ...         "project": "demo",
    ...         "pr_number": 11,
    ...         "summary": "demo",
    ...         "feature_id": "f1",
    ...         "worktree": str(wt),
    ...         "pr_sweep": {"feedback_rounds": 0},
    ...     }
    ...     task_path = queue_root / "done" / "task-clean-no-approval.json"
    ...     _ = task_path.write_text(json.dumps(task))
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "FEATURES_DIR", "load_config", "get_project", "_extract_actionable_comments", "_write_pr_alert", "_unresolved_bot_review_threads", "now_iso", "subprocess")}
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             if cmd[:3] == ["gh", "pr", "view"]:
    ...                 payload = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "reviewDecision": "", "baseRefName": "feature/demo", "url": "https://example/pr/11", "comments": []}
    ...                 return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...             if cmd[:4] == ["git", "-C", str(wt), "fetch"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    ...             if cmd[:5] == ["git", "-C", str(wt), "rev-list", "--count"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="0\\n", stderr="")
    ...             if cmd[:3] == ["gh", "pr", "merge"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="merged", stderr="")
    ...             raise AssertionError(cmd)
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
    ...     pr_sweep.__globals__["FEATURES_DIR"] = features_dir
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: []
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: None
    ...     pr_sweep.__globals__["_unresolved_bot_review_threads"] = lambda *args, **kwargs: []
    ...     pr_sweep.__globals__["now_iso"] = lambda: "2026-04-14T12:00:00"
    ...     pr_sweep.__globals__["subprocess"] = FakeSubprocess()
    ...     import contextlib, io
    ...     with contextlib.redirect_stdout(io.StringIO()):
    ...         result = pr_sweep()
    ...     saved = json.loads(task_path.read_text())
    ...     observed = (result, saved["pr_sweep"].get("auto_merged"), saved.get("auto_merged_at") is not None)
    ...     for key, value in original.items():
    ...         pr_sweep.__globals__[key] = value
    >>> observed
    ((1, 1, 0, 0, 0), True, True)

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     features_dir = root / "features"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     features_dir.mkdir(parents=True, exist_ok=True)
    ...     wt = root / "wt"
    ...     wt.mkdir()
    ...     task = {
    ...         "task_id": "task-guard",
    ...         "project": "demo",
    ...         "pr_number": 9,
    ...         "summary": "demo",
    ...         "feature_id": "f1",
    ...         "worktree": str(wt),
    ...         "pr_sweep": {"feedback_rounds": 0, "conflict_task_id": "task-existing"},
    ...     }
    ...     task_path = queue_root / "done" / "task-guard.json"
    ...     _ = task_path.write_text(json.dumps(task))
    ...     _ = (queue_root / "running" / "task-existing.json").write_text("{}")
    ...     calls = []
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "FEATURES_DIR", "load_config", "get_project", "_extract_actionable_comments", "_enqueue_pr_feedback", "_write_pr_alert", "now_iso", "subprocess")}
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             payload = {"state": "OPEN", "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY", "reviewDecision": "", "baseRefName": "feature/demo", "url": "https://example/pr/9", "comments": []}
    ...             return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
    ...     pr_sweep.__globals__["FEATURES_DIR"] = features_dir
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: []
    ...     pr_sweep.__globals__["_enqueue_pr_feedback"] = lambda *args, **kwargs: calls.append(kwargs) or "task-retry"
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: None
    ...     pr_sweep.__globals__["now_iso"] = lambda: "2026-04-14T12:00:00"
    ...     pr_sweep.__globals__["subprocess"] = FakeSubprocess()
    ...     first = pr_sweep()
    ...     (queue_root / "running" / "task-existing.json").unlink()
    ...     _ = (queue_root / "done" / "task-existing.json").write_text("{}")
    ...     second = pr_sweep()
    ...     saved = json.loads(task_path.read_text())
    ...     observed = (first[2], second[2], len(calls), saved["pr_sweep"]["conflict_task_id"])
    ...     for key, value in original.items():
    ...         pr_sweep.__globals__[key] = value
    >>> observed
    (0, 1, 1, 'task-retry')

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     features_dir = root / "features"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     features_dir.mkdir(parents=True, exist_ok=True)
    ...     feature_path = features_dir / "feature-1.json"
    ...     _ = feature_path.write_text(json.dumps({
    ...         "feature_id": "feature-1",
    ...         "project": "demo",
    ...         "status": "finalizing",
    ...         "final_pr_number": 15,
    ...         "final_pr_sweep": {"feedback_rounds": 0},
    ...     }))
    ...     calls = []
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "FEATURES_DIR", "load_config", "get_project", "_extract_actionable_comments", "_enqueue_feature_pr_feedback", "_write_pr_alert", "_unresolved_bot_review_threads", "now_iso", "subprocess")}
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             if cmd[:3] == ["gh", "pr", "view"]:
    ...                 payload = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "UNSTABLE", "reviewDecision": "", "baseRefName": "main", "url": "https://example/pr/15", "comments": []}
    ...                 return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...             raise AssertionError(cmd)
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
    ...     pr_sweep.__globals__["FEATURES_DIR"] = features_dir
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: []
    ...     pr_sweep.__globals__["_enqueue_feature_pr_feedback"] = lambda *args, **kwargs: calls.append(kwargs) or "task-feature-fb"
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: None
    ...     pr_sweep.__globals__["_unresolved_bot_review_threads"] = lambda *args, **kwargs: [{"id": "91", "author": "chatgpt-codex-connector", "body": "fix it", "created_at": "2026-04-14T21:00:00Z", "thread_id": "thread-1"}]
    ...     pr_sweep.__globals__["now_iso"] = lambda: "2026-04-14T21:20:00"
    ...     pr_sweep.__globals__["subprocess"] = FakeSubprocess()
    ...     result = pr_sweep()
    ...     saved = json.loads(feature_path.read_text())
    ...     observed = (result, len(calls), saved["final_pr_sweep"]["last_feedback_reason"], saved["final_pr_sweep"]["feedback_task_id"])
    ...     for key, value in original.items():
    ...         pr_sweep.__globals__[key] = value
    >>> observed
    ((1, 0, 1, 0, 0), 1, 'unresolved_bot_review_threads', 'task-feature-fb')

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     features_dir = root / "features"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     features_dir.mkdir(parents=True, exist_ok=True)
    ...     feature_path = features_dir / "feature-2.json"
    ...     _ = feature_path.write_text(json.dumps({
    ...         "feature_id": "feature-2",
    ...         "project": "demo",
    ...         "status": "finalizing",
    ...         "final_pr_number": 16,
    ...         "final_pr_sweep": {"feedback_rounds": 0},
    ...     }))
    ...     calls = []
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "FEATURES_DIR", "load_config", "get_project", "_extract_actionable_comments", "_enqueue_feature_pr_feedback", "_write_pr_alert", "_unresolved_bot_review_threads", "now_iso", "subprocess")}
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             if cmd[:3] == ["gh", "pr", "view"]:
    ...                 payload = {"state": "OPEN", "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY", "reviewDecision": "", "baseRefName": "main", "url": "https://example/pr/16", "comments": []}
    ...                 return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...             raise AssertionError(cmd)
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
    ...     pr_sweep.__globals__["FEATURES_DIR"] = features_dir
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: []
    ...     pr_sweep.__globals__["_enqueue_feature_pr_feedback"] = lambda *args, **kwargs: calls.append(kwargs) or "task-feature-conflict"
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: None
    ...     pr_sweep.__globals__["_unresolved_bot_review_threads"] = lambda *args, **kwargs: []
    ...     pr_sweep.__globals__["now_iso"] = lambda: "2026-04-14T21:30:00"
    ...     pr_sweep.__globals__["subprocess"] = FakeSubprocess()
    ...     result = pr_sweep()
    ...     saved = json.loads(feature_path.read_text())
    ...     observed = (result, len(calls), saved["final_pr_sweep"]["last_feedback_reason"], saved["final_pr_sweep"]["feedback_task_id"])
    ...     for key, value in original.items():
    ...         pr_sweep.__globals__[key] = value
    >>> observed
    ((1, 0, 1, 0, 0), 1, 'conflict', 'task-feature-conflict')

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     features_dir = root / "features"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     features_dir.mkdir(parents=True, exist_ok=True)
    ...     wt = root / "wt"
    ...     wt.mkdir()
    ...     task = {
    ...         "task_id": "task-checks",
    ...         "project": "demo",
    ...         "pr_number": 17,
    ...         "summary": "demo",
    ...         "feature_id": "f1",
    ...         "worktree": str(wt),
    ...         "pr_sweep": {"feedback_rounds": 0},
    ...     }
    ...     task_path = queue_root / "done" / "task-checks.json"
    ...     _ = task_path.write_text(json.dumps(task))
    ...     calls = []
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "FEATURES_DIR", "load_config", "get_project", "_extract_actionable_comments", "_extract_failed_checks", "_enqueue_pr_feedback", "_write_pr_alert", "_unresolved_bot_review_threads", "now_iso", "subprocess")}
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             if cmd[:3] == ["gh", "pr", "view"]:
    ...                 payload = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "UNSTABLE", "reviewDecision": "", "baseRefName": "feature/demo", "url": "https://example/pr/17", "comments": [], "statusCheckRollup": [{"name": "unresolved-review-threads", "conclusion": "FAILURE"}]}
    ...                 return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...             if cmd[:4] == ["git", "-C", str(wt), "fetch"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    ...             if cmd[:5] == ["git", "-C", str(wt), "rev-list", "--count"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="0\\n", stderr="")
    ...             raise AssertionError(cmd)
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
    ...     pr_sweep.__globals__["FEATURES_DIR"] = features_dir
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: []
    ...     pr_sweep.__globals__["_extract_failed_checks"] = lambda info: [{"name": "unresolved-review-threads", "conclusion": "FAILURE", "details_url": "https://example/check"}]
    ...     pr_sweep.__globals__["_enqueue_pr_feedback"] = lambda *args, **kwargs: calls.append(kwargs) or "task-check-fix"
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: None
    ...     pr_sweep.__globals__["_unresolved_bot_review_threads"] = lambda *args, **kwargs: []
    ...     pr_sweep.__globals__["now_iso"] = lambda: "2026-04-14T22:00:00"
    ...     pr_sweep.__globals__["subprocess"] = FakeSubprocess()
    ...     result = pr_sweep()
    ...     saved = json.loads(task_path.read_text())
    ...     observed = (result, len(calls), saved["pr_sweep"]["last_feedback_reason"], saved["pr_sweep"]["feedback_task_id"], saved["pr_sweep"]["failing_checks"])
    ...     for key, value in original.items():
    ...         pr_sweep.__globals__[key] = value
    >>> observed
    ((1, 0, 1, 0, 0), 1, 'checks', 'task-check-fix', ['unresolved-review-threads'])

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     features_dir = root / "features"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     features_dir.mkdir(parents=True, exist_ok=True)
    ...     wt = root / "wt"
    ...     wt.mkdir()
    ...     task = {
    ...         "task_id": "task-checks-escalated",
    ...         "project": "demo",
    ...         "pr_number": 17,
    ...         "summary": "demo",
    ...         "feature_id": "f1",
    ...         "worktree": str(wt),
    ...         "pr_sweep": {"feedback_rounds": 3, "escalated_checks": True},
    ...     }
    ...     task_path = queue_root / "done" / "task-checks-escalated.json"
    ...     _ = task_path.write_text(json.dumps(task))
    ...     alerts = []
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "FEATURES_DIR", "load_config", "get_project", "_extract_actionable_comments", "_extract_failed_checks", "_enqueue_pr_feedback", "_write_pr_alert", "_unresolved_bot_review_threads", "now_iso", "subprocess")}
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             if cmd[:3] == ["gh", "pr", "view"]:
    ...                 payload = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "UNSTABLE", "reviewDecision": "", "baseRefName": "feature/demo", "url": "https://example/pr/17", "comments": [], "statusCheckRollup": [{"name": "unresolved-review-threads", "conclusion": "FAILURE"}]}
    ...                 return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...             if cmd[:4] == ["git", "-C", str(wt), "fetch"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    ...             if cmd[:5] == ["git", "-C", str(wt), "rev-list", "--count"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="0\\n", stderr="")
    ...             raise AssertionError(cmd)
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
    ...     pr_sweep.__globals__["FEATURES_DIR"] = features_dir
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: []
    ...     pr_sweep.__globals__["_extract_failed_checks"] = lambda info: [{"name": "unresolved-review-threads", "conclusion": "FAILURE", "details_url": "https://example/check"}]
    ...     calls = []
    ...     pr_sweep.__globals__["_enqueue_pr_feedback"] = lambda *args, **kwargs: calls.append(kwargs) or "task-check-fix-escalated"
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: alerts.append(args) or None
    ...     pr_sweep.__globals__["_unresolved_bot_review_threads"] = lambda *args, **kwargs: []
    ...     pr_sweep.__globals__["now_iso"] = lambda: "2026-04-14T22:46:35"
    ...     pr_sweep.__globals__["subprocess"] = FakeSubprocess()
    ...     result = pr_sweep()
    ...     saved = json.loads(task_path.read_text())
    ...     observed = (result, len(calls), saved["pr_sweep"]["last_feedback_reason"], saved["pr_sweep"]["feedback_task_id"], saved["pr_sweep"]["failing_checks"])
    ...     for key, value in original.items():
    ...         pr_sweep.__globals__[key] = value
    >>> observed
    ((1, 0, 1, 0, 0), 1, 'checks', 'task-check-fix-escalated', ['unresolved-review-threads'])

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     features_dir = root / "features"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     features_dir.mkdir(parents=True, exist_ok=True)
    ...     wt = root / "wt"
    ...     wt.mkdir()
    ...     _ = (queue_root / "running" / "task-fb.json").write_text(json.dumps({"task_id": "task-fb"}))
    ...     task = {
    ...         "task_id": "task-comments-guard",
    ...         "project": "demo",
    ...         "pr_number": 19,
    ...         "summary": "demo",
    ...         "feature_id": "f1",
    ...         "worktree": str(wt),
    ...         "pr_sweep": {"feedback_rounds": 1, "feedback_task_id": "task-fb"},
    ...     }
    ...     task_path = queue_root / "done" / "task-comments-guard.json"
    ...     _ = task_path.write_text(json.dumps(task))
    ...     calls = []
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "FEATURES_DIR", "load_config", "get_project", "_extract_actionable_comments", "_extract_failed_checks", "_enqueue_pr_feedback", "_write_pr_alert", "_unresolved_bot_review_threads", "record_pr_sweep_guard_skip", "now_iso", "subprocess")}
    ...     guard_reasons = []
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             if cmd[:3] == ["gh", "pr", "view"]:
    ...                 payload = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "reviewDecision": "", "baseRefName": "feature/demo", "url": "https://example/pr/19", "comments": []}
    ...                 return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...             if cmd[:4] == ["git", "-C", str(wt), "fetch"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    ...             if cmd[:5] == ["git", "-C", str(wt), "rev-list", "--count"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="0\\n", stderr="")
    ...             raise AssertionError(cmd)
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
    ...     pr_sweep.__globals__["FEATURES_DIR"] = features_dir
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: [{"id": "c1", "author": "chatgpt-codex-connector", "body": "![P1 Badge] must fix"}]
    ...     pr_sweep.__globals__["_extract_failed_checks"] = lambda info: []
    ...     pr_sweep.__globals__["_enqueue_pr_feedback"] = lambda *args, **kwargs: calls.append(kwargs) or "task-dup"
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: None
    ...     pr_sweep.__globals__["_unresolved_bot_review_threads"] = lambda *args, **kwargs: []
    ...     pr_sweep.__globals__["record_pr_sweep_guard_skip"] = lambda reason: guard_reasons.append(reason) or {}
    ...     pr_sweep.__globals__["now_iso"] = lambda: "2026-04-14T22:30:00"
    ...     pr_sweep.__globals__["subprocess"] = FakeSubprocess()
    ...     result = pr_sweep()
    ...     saved = json.loads(task_path.read_text())
    ...     observed = (result, calls, guard_reasons, saved["pr_sweep"]["feedback_task_id"], saved["pr_sweep"]["last_merge_state"])
    ...     for key, value in original.items():
    ...         pr_sweep.__globals__[key] = value
    >>> observed
    ((1, 0, 0, 0, 0), [], ['task_feedback'], 'task-fb', 'CLEAN')

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     features_dir = root / "features"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     features_dir.mkdir(parents=True, exist_ok=True)
    ...     wt = root / "wt"
    ...     wt.mkdir()
    ...     _ = (queue_root / "claimed" / "task-fb.json").write_text(json.dumps({"task_id": "task-fb"}))
    ...     task = {
    ...         "task_id": "task-threads-guard",
    ...         "project": "demo",
    ...         "pr_number": 20,
    ...         "summary": "demo",
    ...         "feature_id": "f1",
    ...         "worktree": str(wt),
    ...         "pr_sweep": {"feedback_rounds": 1, "feedback_task_id": "task-fb", "handled_comment_ids": []},
    ...     }
    ...     task_path = queue_root / "done" / "task-threads-guard.json"
    ...     _ = task_path.write_text(json.dumps(task))
    ...     calls = []
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "FEATURES_DIR", "load_config", "get_project", "_extract_actionable_comments", "_extract_failed_checks", "_enqueue_pr_feedback", "_write_pr_alert", "_unresolved_bot_review_threads", "record_pr_sweep_guard_skip", "now_iso", "subprocess")}
    ...     guard_reasons = []
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             if cmd[:3] == ["gh", "pr", "view"]:
    ...                 payload = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "UNSTABLE", "reviewDecision": "", "baseRefName": "feature/demo", "url": "https://example/pr/20", "comments": []}
    ...                 return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...             if cmd[:4] == ["git", "-C", str(wt), "fetch"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    ...             if cmd[:5] == ["git", "-C", str(wt), "rev-list", "--count"]:
    ...                 return types.SimpleNamespace(returncode=0, stdout="0\\n", stderr="")
    ...             raise AssertionError(cmd)
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
    ...     pr_sweep.__globals__["FEATURES_DIR"] = features_dir
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: []
    ...     pr_sweep.__globals__["_extract_failed_checks"] = lambda info: []
    ...     pr_sweep.__globals__["_enqueue_pr_feedback"] = lambda *args, **kwargs: calls.append(kwargs) or "task-dup"
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: None
    ...     pr_sweep.__globals__["_unresolved_bot_review_threads"] = lambda *args, **kwargs: [{"id": "c2", "author": "chatgpt-codex-connector", "body": "![P1 Badge] must fix", "thread_id": "t1"}]
    ...     pr_sweep.__globals__["record_pr_sweep_guard_skip"] = lambda reason: guard_reasons.append(reason) or {}
    ...     pr_sweep.__globals__["now_iso"] = lambda: "2026-04-14T22:31:00"
    ...     pr_sweep.__globals__["subprocess"] = FakeSubprocess()
    ...     result = pr_sweep()
    ...     saved = json.loads(task_path.read_text())
    ...     observed = (result, calls, guard_reasons, saved["pr_sweep"]["feedback_task_id"], saved["pr_sweep"]["unresolved_bot_threads"])
    ...     for key, value in original.items():
    ...         pr_sweep.__globals__[key] = value
    >>> observed
    ((1, 0, 0, 0, 0), [], ['task_unresolved_bot_review_threads'], 'task-fb', 1)

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     features_dir = root / "features"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     features_dir.mkdir(parents=True, exist_ok=True)
    ...     feature_path = features_dir / "feature-3.json"
    ...     _ = feature_path.write_text(json.dumps({
    ...         "feature_id": "feature-3",
    ...         "project": "demo",
    ...         "status": "finalizing",
    ...         "final_pr_number": 18,
    ...         "final_pr_sweep": {"feedback_rounds": 0},
    ...     }))
    ...     calls = []
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "FEATURES_DIR", "load_config", "get_project", "_extract_actionable_comments", "_extract_failed_checks", "_enqueue_feature_pr_feedback", "_write_pr_alert", "_unresolved_bot_review_threads", "now_iso", "subprocess")}
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             if cmd[:3] == ["gh", "pr", "view"]:
    ...                 payload = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "UNSTABLE", "reviewDecision": "", "baseRefName": "main", "url": "https://example/pr/18", "comments": [], "statusCheckRollup": [{"workflowName": "unresolved-bot-review", "conclusion": "FAILURE"}]}
    ...                 return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...             raise AssertionError(cmd)
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
    ...     pr_sweep.__globals__["FEATURES_DIR"] = features_dir
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: []
    ...     pr_sweep.__globals__["_extract_failed_checks"] = lambda info: [{"name": "unresolved-bot-review", "conclusion": "FAILURE", "details_url": "https://example/check"}]
    ...     pr_sweep.__globals__["_enqueue_feature_pr_feedback"] = lambda *args, **kwargs: calls.append(kwargs) or "task-feature-check-fix"
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: None
    ...     pr_sweep.__globals__["_unresolved_bot_review_threads"] = lambda *args, **kwargs: []
    ...     pr_sweep.__globals__["now_iso"] = lambda: "2026-04-14T22:05:00"
    ...     pr_sweep.__globals__["subprocess"] = FakeSubprocess()
    ...     result = pr_sweep()
    ...     saved = json.loads(feature_path.read_text())
    ...     observed = (result, len(calls), saved["final_pr_sweep"]["last_feedback_reason"], saved["final_pr_sweep"]["feedback_task_id"], saved["final_pr_sweep"]["failing_checks"])
    ...     for key, value in original.items():
    ...         pr_sweep.__globals__[key] = value
    >>> observed
    ((1, 0, 1, 0, 0), 1, 'checks', 'task-feature-check-fix', ['unresolved-bot-review'])

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     features_dir = root / "features"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     features_dir.mkdir(parents=True, exist_ok=True)
    ...     feature_path = features_dir / "feature-escalated.json"
    ...     _ = feature_path.write_text(json.dumps({
    ...         "feature_id": "feature-escalated",
    ...         "project": "demo",
    ...         "status": "finalizing",
    ...         "final_pr_number": 18,
    ...         "final_pr_sweep": {"feedback_rounds": 3, "escalated_checks": True},
    ...     }))
    ...     alerts = []
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "FEATURES_DIR", "load_config", "get_project", "_extract_actionable_comments", "_extract_failed_checks", "_enqueue_feature_pr_feedback", "_write_pr_alert", "_unresolved_bot_review_threads", "now_iso", "subprocess")}
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             if cmd[:3] == ["gh", "pr", "view"]:
    ...                 payload = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "UNSTABLE", "reviewDecision": "", "baseRefName": "main", "url": "https://example/pr/18", "comments": [], "statusCheckRollup": [{"workflowName": "unresolved-bot-review", "conclusion": "FAILURE"}]}
    ...                 return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...             raise AssertionError(cmd)
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
    ...     pr_sweep.__globals__["FEATURES_DIR"] = features_dir
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: []
    ...     pr_sweep.__globals__["_extract_failed_checks"] = lambda info: [{"name": "unresolved-bot-review", "conclusion": "FAILURE", "details_url": "https://example/check"}]
    ...     calls = []
    ...     pr_sweep.__globals__["_enqueue_feature_pr_feedback"] = lambda *args, **kwargs: calls.append(kwargs) or "task-feature-check-fix-escalated"
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: alerts.append(args) or None
    ...     pr_sweep.__globals__["_unresolved_bot_review_threads"] = lambda *args, **kwargs: []
    ...     pr_sweep.__globals__["now_iso"] = lambda: "2026-04-14T22:46:35"
    ...     pr_sweep.__globals__["subprocess"] = FakeSubprocess()
    ...     result = pr_sweep()
    ...     saved = json.loads(feature_path.read_text())
    ...     observed = (result, len(calls), saved["final_pr_sweep"]["last_feedback_reason"], saved["final_pr_sweep"]["feedback_task_id"], saved["final_pr_sweep"]["failing_checks"])
    ...     for key, value in original.items():
    ...         pr_sweep.__globals__[key] = value
    >>> observed
    ((1, 0, 1, 0, 0), 1, 'checks', 'task-feature-check-fix-escalated', ['unresolved-bot-review'])

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     features_dir = root / "features"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     features_dir.mkdir(parents=True, exist_ok=True)
    ...     _ = (queue_root / "running" / "task-child-repair.json").write_text(json.dumps({
    ...         "task_id": "task-child-repair",
    ...         "feature_id": "feature-4",
    ...         "project": "demo",
    ...     }))
    ...     feature_path = features_dir / "feature-4.json"
    ...     _ = feature_path.write_text(json.dumps({
    ...         "feature_id": "feature-4",
    ...         "project": "demo",
    ...         "status": "finalizing",
    ...         "final_pr_number": 21,
    ...         "final_pr_sweep": {"feedback_rounds": 0},
    ...     }))
    ...     calls = []
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "FEATURES_DIR", "load_config", "get_project", "_extract_actionable_comments", "_extract_failed_checks", "_enqueue_feature_pr_feedback", "_write_pr_alert", "_unresolved_bot_review_threads", "record_pr_sweep_guard_skip", "now_iso", "subprocess")}
    ...     guard_reasons = []
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             if cmd[:3] == ["gh", "pr", "view"]:
    ...                 payload = {"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "UNSTABLE", "reviewDecision": "", "baseRefName": "main", "url": "https://example/pr/21", "comments": [], "statusCheckRollup": [{"workflowName": "unresolved-bot-review", "conclusion": "FAILURE"}]}
    ...                 return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...             raise AssertionError(cmd)
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
    ...     pr_sweep.__globals__["FEATURES_DIR"] = features_dir
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: []
    ...     pr_sweep.__globals__["_extract_failed_checks"] = lambda info: [{"name": "unresolved-bot-review", "conclusion": "FAILURE", "details_url": "https://example/check"}]
    ...     pr_sweep.__globals__["_enqueue_feature_pr_feedback"] = lambda *args, **kwargs: calls.append(kwargs) or "task-feature-check-fix"
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: None
    ...     pr_sweep.__globals__["_unresolved_bot_review_threads"] = lambda *args, **kwargs: []
    ...     pr_sweep.__globals__["record_pr_sweep_guard_skip"] = lambda reason: guard_reasons.append(reason) or {}
    ...     pr_sweep.__globals__["now_iso"] = lambda: "2026-04-14T22:40:00"
    ...     pr_sweep.__globals__["subprocess"] = FakeSubprocess()
    ...     result = pr_sweep()
    ...     saved = json.loads(feature_path.read_text())
    ...     observed = (result, calls, guard_reasons, saved["final_pr_sweep"]["failing_checks"], saved["final_pr_sweep"]["last_merge_state"])
    ...     for key, value in original.items():
    ...         pr_sweep.__globals__[key] = value
    >>> observed
    ((1, 0, 0, 0, 0), [], ['feature_branch_repair'], ['unresolved-bot-review'], 'UNSTABLE')
    """
    if shutil.which("gh") is None:
        print("pr-sweep: gh CLI not installed, nothing to do", file=sys.stderr)
        return (0, 0, 0, 0, 0)

    config = load_config()
    done_dir = queue_dir("done")

    checked = merged = fb_enqueued = alerted = skipped = 0

    for task_file in sorted(done_dir.glob("*.json")):
        task = read_json(task_file, {})
        pr_number = task.get("pr_number")
        if not pr_number or task.get("cleaned_at"):
            continue
        # Skip tasks that were already merged on a prior tick.
        if task.get("pr_final_state") in ("MERGED", "CLOSED"):
            continue

        checked += 1
        project_name = task.get("project")
        try:
            project = get_project(config, project_name)
        except KeyError:
            skipped += 1
            continue

        repo_path = project["path"]
        proc = subprocess.run(
            ["gh", "pr", "view", str(pr_number),
             "--json", "state,mergeable,mergeStateStatus,reviewDecision,"
             "headRefOid,baseRefName,url,author,comments,statusCheckRollup"],
            cwd=repo_path,
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:200]
            print(f"pr-sweep {task.get('task_id')}: gh pr view failed: {err}")
            if any(token in err.lower() for token in ("401", "unauthorized", "authentication", "forbidden")):
                environment_health(refresh=True)
                write_agent_status("pr-sweep", "failed", f"gh auth failure on {project_name}; sweep halted")
                append_event(
                    "pr-sweep",
                    "auth_failure",
                    task_id=task.get("task_id"),
                    details={"project": project_name, "pr_number": pr_number, "error": err},
                )
                skipped += 1
                return (checked, merged, fb_enqueued, alerted, skipped)
            skipped += 1
            continue
        try:
            info = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            skipped += 1
            continue

        state = info.get("state", "")
        if state in ("MERGED", "CLOSED"):
            continue  # cleanup_worktrees will finalize

        sweep = task.get("pr_sweep") or {}
        handled_ids = sweep.get("handled_comment_ids", [])
        rounds = sweep.get("feedback_rounds", 0)
        base_ref = info.get("baseRefName") or task.get("push_base_branch") or "main"
        pr_url = info.get("url")

        actionable = _extract_actionable_comments(info, handled_ids)
        failed_checks = _extract_failed_checks(info)
        mergeable = info.get("mergeable", "UNKNOWN")
        review_decision = info.get("reviewDecision", "")
        merge_state = info.get("mergeStateStatus", "")
        dispatch_reason = "conflict"

        drift_threshold = int(
            project.get(
                "drift_threshold",
                config.get("drift_threshold", CONFIG_DEFAULTS["drift_threshold"]),
            )
        )
        if mergeable == "MERGEABLE":
            drift_count = _git_drift_ahead_count(
                task.get("worktree"), base_ref,
                warn_prefix=f"pr-sweep {task.get('task_id')}",
            )
            if drift_count is not None and drift_count >= drift_threshold:
                merge_state = "BEHIND"
                dispatch_reason = "drift_sync"

        def stamp_sweep(updates):
            def mut(t):
                s = dict(t.get("pr_sweep") or {})
                s.update(updates)
                s["last_checked_at"] = now_iso()
                t["pr_sweep"] = s
            return mut

        # Case 1: conflicts — rebase needed. Only feature-branch-targeted PRs
        # get an auto-rebase attempt (safe because we own the feature branch).
        # Conflicts on feature->main PRs alert the human directly.
        if mergeable == "CONFLICTING" or merge_state in ("DIRTY", "BEHIND"):
            if not base_ref.startswith("feature/"):
                alert = _write_pr_alert(
                    project_name, task.get("task_id"), pr_number,
                    f"feature->main PR conflicts with {base_ref} — needs manual rebase",
                    pr_url,
                )
                print(f"pr-sweep: alerted on {task.get('task_id')} pr=#{pr_number} conflict")
                task = update_task_in_place(task_file,
                    stamp_sweep({"last_mergeable": mergeable, "escalated_conflict": True}))
                alerted += 1
                continue
            if rounds >= PR_SWEEP_MAX_FEEDBACK_ROUNDS:
                if sweep.get("escalated_conflict"):
                    task = update_task_in_place(task_file, stamp_sweep({
                        "last_mergeable": mergeable,
                        "last_merge_state": merge_state,
                    }))
                    skipped += 1
                    continue
                alert = _write_pr_alert(
                    project_name, task.get("task_id"), pr_number,
                    f"exhausted {rounds} pr-feedback rounds, still CONFLICTING",
                    pr_url,
                )
                task = update_task_in_place(task_file,
                    stamp_sweep({"last_mergeable": mergeable, "escalated_conflict": True}))
                alerted += 1
                continue
            guard_task_id = sweep.get("conflict_task_id")
            if guard_task_id and _task_exists_in_queue(guard_task_id):
                record_pr_sweep_guard_skip("task_conflict")
                task = update_task_in_place(task_file, stamp_sweep({
                    "last_mergeable": mergeable,
                    "last_merge_state": merge_state,
                }))
                continue
            if dry_run:
                print(f"DRY-RUN pr-sweep {task.get('task_id')}: would enqueue rebase feedback")
                continue
            conflict_task_id = _enqueue_pr_feedback(task, project_name, pr_number, base_ref,
                                                    conflicts=True, comments=actionable, check_failures=[])
            task = update_task_in_place(task_file, stamp_sweep({
                "last_mergeable": mergeable,
                "last_merge_state": merge_state,
                "feedback_rounds": rounds + 1,
                "last_feedback_reason": dispatch_reason,
                "handled_comment_ids": handled_ids + [c["id"] for c in actionable],
                "conflict_task_id": conflict_task_id,
                "conflict_task_dispatched_at": now_iso(),
            }))
            fb_enqueued += 1
            continue

        # Case 2: actionable unhandled comments — dispatch pr-feedback.
        if actionable:
            if rounds >= PR_SWEEP_MAX_FEEDBACK_ROUNDS:
                if sweep.get("escalated_comments"):
                    task = update_task_in_place(task_file, stamp_sweep({
                        "last_mergeable": mergeable,
                        "last_merge_state": merge_state,
                    }))
                    skipped += 1
                    continue
                alert = _write_pr_alert(
                    project_name, task.get("task_id"), pr_number,
                    f"exhausted {rounds} pr-feedback rounds, {len(actionable)} comments still unresolved",
                    pr_url,
                )
                task = update_task_in_place(task_file,
                    stamp_sweep({"last_mergeable": mergeable, "escalated_comments": True}))
                alerted += 1
                continue
            guard_task_id = sweep.get("feedback_task_id")
            if guard_task_id and _task_exists_in_queue(guard_task_id):
                record_pr_sweep_guard_skip("task_feedback")
                task = update_task_in_place(task_file, stamp_sweep({
                    "last_mergeable": mergeable,
                    "last_merge_state": merge_state,
                }))
                continue
            if dry_run:
                print(f"DRY-RUN pr-sweep {task.get('task_id')}: would enqueue "
                      f"feedback for {len(actionable)} comment(s)")
                continue
            feedback_task_id = _enqueue_pr_feedback(task, project_name, pr_number, base_ref,
                                                    conflicts=False, comments=actionable, check_failures=[])
            task = update_task_in_place(task_file, stamp_sweep({
                "last_mergeable": mergeable,
                "feedback_rounds": rounds + 1,
                "last_feedback_reason": "comments",
                "feedback_task_id": feedback_task_id,
                "handled_comment_ids": handled_ids + [c["id"] for c in actionable],
            }))
            fb_enqueued += 1
            continue

        # Case 2.5: unresolved review threads from allowlisted bots block
        # merge AND dispatch a pr-feedback round so the solver can self-
        # heal. reviewDecision stays "" when a bot leaves a plain review-
        # thread comment instead of a formal Changes Requested review, so
        # Case 2's REST-comment scan can't see these and Case 4 would
        # merge straight through. GraphQL is the only path to
        # reviewThreads.isResolved — .github/workflows/unresolved-bot-
        # review.yml mirrors this query as the GH-side backstop. After
        # pr-feedback's fix lands, worker.run_pr_feedback_task calls
        # _resolve_review_threads on the stored thread_ids so the next
        # tick sees isResolved=true and merges — closing the self-heal
        # loop without human intervention. chatgpt-codex-connector does
        # NOT mark its own threads resolved, so the orchestrator has to.
        #
        # MUST run before Case 3's BLOCKED/UNSTABLE silent-stamp: the
        # GH-side unresolved-bot-review workflow is itself a required
        # check, so the very condition this gate is meant to heal puts
        # the PR into merge_state=UNSTABLE. If Case 3 catches it first,
        # pr-sweep just stamps and skips forever — the self-heal loop
        # never closes. Same reasoning applies to CHANGES_REQUESTED:
        # a bot that files a formal review would otherwise silently win.
        unresolved_bot_threads = _unresolved_bot_review_threads(
            repo_path, pr_number, pr_url,
        )
        if unresolved_bot_threads:
            count = len(unresolved_bot_threads)
            new_thread_comments = [
                c for c in unresolved_bot_threads
                if c.get("id") and c["id"] not in set(handled_ids)
            ]
            if new_thread_comments:
                if rounds >= PR_SWEEP_MAX_FEEDBACK_ROUNDS:
                    if sweep.get("escalated_comments"):
                        task = update_task_in_place(task_file, stamp_sweep({
                            "last_mergeable": mergeable,
                            "last_merge_state": merge_state,
                            "unresolved_bot_threads": count,
                        }))
                        skipped += 1
                        continue
                    _write_pr_alert(
                        project_name, task.get("task_id"), pr_number,
                        f"exhausted {rounds} pr-feedback rounds, "
                        f"{count} unresolved bot review thread(s) "
                        f"({len(new_thread_comments)} not yet attempted)",
                        pr_url,
                    )
                    task = update_task_in_place(task_file, stamp_sweep({
                        "last_mergeable": mergeable,
                        "escalated_comments": True,
                        "unresolved_bot_threads": count,
                    }))
                    alerted += 1
                    continue
                guard_task_id = sweep.get("feedback_task_id")
                if guard_task_id and _task_exists_in_queue(guard_task_id):
                    record_pr_sweep_guard_skip("task_unresolved_bot_review_threads")
                    task = update_task_in_place(task_file, stamp_sweep({
                        "last_mergeable": mergeable,
                        "last_merge_state": merge_state,
                        "unresolved_bot_threads": count,
                    }))
                    continue
                if dry_run:
                    print(f"DRY-RUN pr-sweep {task.get('task_id')}: would enqueue "
                          f"feedback for {len(new_thread_comments)} bot review thread(s)")
                    continue
                feedback_task_id = _enqueue_pr_feedback(
                    task, project_name, pr_number, base_ref,
                    conflicts=False, comments=new_thread_comments, check_failures=[],
                )
                task = update_task_in_place(task_file, stamp_sweep({
                    "last_mergeable": mergeable,
                    "feedback_rounds": rounds + 1,
                    "last_feedback_reason": "unresolved_bot_review_threads",
                    "feedback_task_id": feedback_task_id,
                    "handled_comment_ids": _merge_comment_ids(handled_ids, new_thread_comments),
                    "unresolved_bot_threads": count,
                }))
                fb_enqueued += 1
                continue
            guard_task_id = sweep.get("feedback_task_id")
            if guard_task_id and _task_exists_in_queue(guard_task_id):
                record_pr_sweep_guard_skip("task_unresolved_bot_review_threads")
                task = update_task_in_place(task_file, stamp_sweep({
                    "last_mergeable": mergeable,
                    "last_merge_state": merge_state,
                    "unresolved_bot_threads": count,
                }))
                continue
            if rounds >= PR_SWEEP_MAX_FEEDBACK_ROUNDS:
                if sweep.get("escalated_comments"):
                    task = update_task_in_place(task_file, stamp_sweep({
                        "last_mergeable": mergeable,
                        "last_merge_state": merge_state,
                        "unresolved_bot_threads": count,
                    }))
                    skipped += 1
                    continue
                _write_pr_alert(
                    project_name, task.get("task_id"), pr_number,
                    f"exhausted {rounds} pr-feedback rounds, "
                    f"{count} unresolved bot review thread(s) remain unresolved",
                    pr_url,
                )
                task = update_task_in_place(task_file, stamp_sweep({
                    "last_mergeable": mergeable,
                    "last_merge_state": merge_state,
                    "escalated_comments": True,
                    "unresolved_bot_threads": count,
                }))
                alerted += 1
                continue
            if dry_run:
                print(f"DRY-RUN pr-sweep {task.get('task_id')}: would retry "
                      f"feedback for {count} unresolved bot review thread(s)")
                continue
            feedback_task_id = _enqueue_pr_feedback(
                task, project_name, pr_number, base_ref,
                conflicts=False, comments=unresolved_bot_threads, check_failures=[],
            )
            task = update_task_in_place(task_file, stamp_sweep({
                "last_mergeable": mergeable,
                "last_merge_state": merge_state,
                "feedback_rounds": rounds + 1,
                "last_feedback_reason": "unresolved_bot_review_threads_retry",
                "feedback_task_id": feedback_task_id,
                "handled_comment_ids": _merge_comment_ids(handled_ids, unresolved_bot_threads),
                "unresolved_bot_threads": count,
            }))
            fb_enqueued += 1
            continue

        # Case 3: waiting on review / checks. Don't merge, don't alert — just stamp.
        if review_decision == "CHANGES_REQUESTED":
            task = update_task_in_place(task_file, stamp_sweep({
                "last_mergeable": mergeable,
                "last_merge_state": merge_state,
                "last_review_decision": review_decision,
            }))
            continue
        if merge_state in ("BLOCKED", "UNSTABLE") and failed_checks:
            if rounds >= PR_SWEEP_MAX_FEEDBACK_ROUNDS:
                if sweep.get("escalated_checks"):
                    task = update_task_in_place(task_file, stamp_sweep({
                        "last_mergeable": mergeable,
                        "last_merge_state": merge_state,
                        "failing_checks": [c["name"] for c in failed_checks],
                    }))
                    skipped += 1
                    continue
                _write_pr_alert(
                    project_name, task.get("task_id"), pr_number,
                    f"exhausted {rounds} pr-feedback rounds, failed checks still blocking",
                    pr_url,
                )
                task = update_task_in_place(task_file, stamp_sweep({
                    "last_mergeable": mergeable,
                    "last_merge_state": merge_state,
                    "escalated_checks": True,
                    "failing_checks": [c["name"] for c in failed_checks],
                }))
                alerted += 1
                continue
            guard_task_id = sweep.get("feedback_task_id")
            if guard_task_id and _task_exists_in_queue(guard_task_id):
                record_pr_sweep_guard_skip("task_check_failure")
                task = update_task_in_place(task_file, stamp_sweep({
                    "last_mergeable": mergeable,
                    "last_merge_state": merge_state,
                    "failing_checks": [c["name"] for c in failed_checks],
                }))
                continue
            if dry_run:
                print(f"DRY-RUN pr-sweep {task.get('task_id')}: would enqueue feedback for failed checks")
                continue
            check_comments = unresolved_bot_threads if _checks_require_thread_feedback(failed_checks) else []
            feedback_task_id = _enqueue_pr_feedback(
                task, project_name, pr_number, base_ref,
                conflicts=False, comments=check_comments, check_failures=failed_checks,
            )
            task = update_task_in_place(task_file, stamp_sweep({
                "last_mergeable": mergeable,
                "last_merge_state": merge_state,
                "feedback_rounds": rounds + 1,
                "last_feedback_reason": "checks",
                "feedback_task_id": feedback_task_id,
                "handled_comment_ids": _merge_comment_ids(handled_ids, check_comments),
                "failing_checks": [c["name"] for c in failed_checks],
            }))
            fb_enqueued += 1
            continue
        if merge_state in ("BLOCKED", "UNSTABLE"):
            task = update_task_in_place(task_file, stamp_sweep({
                "last_mergeable": mergeable,
                "last_merge_state": merge_state,
            }))
            continue

        # Case 4: fully green — auto-merge (feature-targeted only).
        if mergeable == "MERGEABLE" and base_ref.startswith("feature/"):
            if dry_run:
                print(f"DRY-RUN pr-sweep {task.get('task_id')}: would auto-merge "
                      f"pr=#{pr_number} into {base_ref}")
                continue
            mp = subprocess.run(
                ["gh", "pr", "merge", str(pr_number),
                 "--squash", "--delete-branch"],
                cwd=repo_path,
                capture_output=True, text=True, timeout=60,
            )
            if mp.returncode != 0:
                reason = (mp.stderr or "").strip()[:300]
                print(f"pr-sweep {task.get('task_id')}: gh pr merge failed: {reason}")
                task = update_task_in_place(task_file, stamp_sweep({
                    "last_mergeable": mergeable,
                    "last_merge_error": reason,
                }))
                skipped += 1
                continue
            def mut_merged(t):
                t["auto_merged_at"] = now_iso()
                t["pr_final_state"] = "MERGED"
                s = dict(t.get("pr_sweep") or {})
                s["last_checked_at"] = now_iso()
                s["auto_merged"] = True
                t["pr_sweep"] = s
            update_task_in_place(task_file, mut_merged)
            feature_id = task.get("feature_id")
            if feature_id:
                feature = read_feature(feature_id)
                if feature and feature.get("status") == "finalizing" and feature.get("final_pr_number"):
                    review_ok, review_reason = _request_codex_review(
                        repo_path,
                        feature["final_pr_number"],
                        reason=f"feature branch advanced via task PR #{pr_number} auto-merge",
                    )
                    if feature:
                        def mut_feature_review(f):
                            if review_ok:
                                f["final_pr_review_requested_at"] = now_iso()
                                f["final_pr_review_request_error"] = None
                            else:
                                f["final_pr_review_request_error"] = review_reason
                        update_feature(feature_id, mut_feature_review)
            merged += 1
            print(f"pr-sweep merged {task.get('task_id')} pr=#{pr_number} into {base_ref}")
            append_transition(task.get("task_id", "?"), "done", "done",
                              reason=f"pr-sweep auto-merged into {base_ref}")
            continue

        # Case 5: nothing to do (OPEN, MERGEABLE but base is main, etc.).
        task = update_task_in_place(task_file, stamp_sweep({
            "last_mergeable": mergeable,
            "last_merge_state": merge_state,
        }))

    for feature in list_features(status="finalizing"):
        pr_number = feature.get("final_pr_number")
        if not pr_number:
            continue

        checked += 1
        project_name = feature.get("project")
        feature_id = feature.get("feature_id")
        try:
            project = get_project(config, project_name)
        except KeyError:
            skipped += 1
            continue

        repo_path = project["path"]
        proc = subprocess.run(
            ["gh", "pr", "view", str(pr_number),
             "--json", "state,mergeable,mergeStateStatus,reviewDecision,"
             "baseRefName,url,author,comments,statusCheckRollup"],
            cwd=repo_path,
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:200]
            print(f"pr-sweep feature {feature_id}: gh pr view failed: {err}")
            skipped += 1
            continue
        try:
            info = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            skipped += 1
            continue

        state = info.get("state", "")
        if state in ("MERGED", "CLOSED"):
            continue

        sweep = dict(feature.get("final_pr_sweep") or {})
        handled_ids = sweep.get("handled_comment_ids", [])
        rounds = sweep.get("feedback_rounds", 0)
        pr_url = info.get("url")
        actionable = _extract_actionable_comments(info, handled_ids)
        failed_checks = _extract_failed_checks(info)
        unresolved_bot_threads = _unresolved_bot_review_threads(
            repo_path, pr_number, pr_url,
        )

        def stamp_feature_sweep(updates):
            def mut(f):
                s = dict(f.get("final_pr_sweep") or {})
                s.update(updates)
                s["last_checked_at"] = now_iso()
                f["final_pr_sweep"] = s
            return mut

        new_thread_comments = [
            c for c in unresolved_bot_threads
            if c.get("id") and c["id"] not in set(handled_ids)
        ]
        mergeable = info.get("mergeable", "UNKNOWN")
        merge_state = info.get("mergeStateStatus", "")
        branch_repair_in_flight = _feature_has_in_flight_branch_repair(
            feature_id,
            exclude_task_ids={sweep.get("feedback_task_id")},
        )
        conflict_feedback = mergeable == "CONFLICTING" or merge_state in ("DIRTY", "BEHIND")
        if conflict_feedback:
            if rounds >= PR_SWEEP_MAX_FEEDBACK_ROUNDS:
                if sweep.get("escalated_conflict"):
                    update_feature(feature_id, stamp_feature_sweep({
                        "last_mergeable": mergeable,
                        "last_merge_state": merge_state,
                        "unresolved_bot_threads": len(unresolved_bot_threads),
                    }))
                    skipped += 1
                    continue
                _write_pr_alert(
                    project_name, feature_id, pr_number,
                    f"exhausted {rounds} feature-pr feedback rounds, still {merge_state or mergeable}",
                    pr_url,
                )
                update_feature(feature_id, stamp_feature_sweep({
                    "escalated_conflict": True,
                    "last_mergeable": mergeable,
                    "last_merge_state": merge_state,
                }))
                alerted += 1
                continue
            guard_task_id = sweep.get("feedback_task_id")
            if guard_task_id and _task_exists_in_queue(guard_task_id):
                record_pr_sweep_guard_skip("feature_conflict")
                update_feature(feature_id, stamp_feature_sweep({
                    "last_mergeable": mergeable,
                    "last_merge_state": merge_state,
                }))
                continue
            if branch_repair_in_flight:
                record_pr_sweep_guard_skip("feature_branch_repair")
                update_feature(feature_id, stamp_feature_sweep({
                    "last_mergeable": mergeable,
                    "last_merge_state": merge_state,
                    "unresolved_bot_threads": len(unresolved_bot_threads),
                }))
                continue
            if dry_run:
                print(f"DRY-RUN pr-sweep feature {feature_id}: would enqueue conflict feedback")
                continue
            feedback_task_id = _enqueue_feature_pr_feedback(
                feature, project_name, pr_number,
                comments=actionable + new_thread_comments,
                check_failures=[],
                conflicts=True,
            )
            update_feature(feature_id, stamp_feature_sweep({
                "feedback_rounds": rounds + 1,
                "last_feedback_reason": "conflict",
                "feedback_task_id": feedback_task_id,
                "feedback_task_dispatched_at": now_iso(),
                "handled_comment_ids": _merge_comment_ids(handled_ids, actionable + new_thread_comments),
                "unresolved_bot_threads": len(unresolved_bot_threads),
                "last_mergeable": mergeable,
                "last_merge_state": merge_state,
            }))
            fb_enqueued += 1
            continue
        feedback_comments = actionable + new_thread_comments
        if feedback_comments:
            if rounds >= PR_SWEEP_MAX_FEEDBACK_ROUNDS:
                if sweep.get("escalated_comments"):
                    update_feature(feature_id, stamp_feature_sweep({
                        "last_mergeable": info.get("mergeable", "UNKNOWN"),
                        "last_merge_state": info.get("mergeStateStatus", ""),
                        "unresolved_bot_threads": len(unresolved_bot_threads),
                    }))
                    skipped += 1
                    continue
                _write_pr_alert(
                    project_name, feature_id, pr_number,
                    f"exhausted {rounds} feature-pr feedback rounds, "
                    f"{len(feedback_comments)} comment(s) still unresolved",
                    pr_url,
                )
                update_feature(feature_id, stamp_feature_sweep({
                    "escalated_comments": True,
                    "last_mergeable": info.get("mergeable", "UNKNOWN"),
                    "last_merge_state": info.get("mergeStateStatus", ""),
                }))
                alerted += 1
                continue
            guard_task_id = sweep.get("feedback_task_id")
            if guard_task_id and _task_exists_in_queue(guard_task_id):
                record_pr_sweep_guard_skip("feature_feedback")
                update_feature(feature_id, stamp_feature_sweep({
                    "last_mergeable": info.get("mergeable", "UNKNOWN"),
                    "last_merge_state": info.get("mergeStateStatus", ""),
                }))
                continue
            if branch_repair_in_flight:
                record_pr_sweep_guard_skip("feature_branch_repair")
                update_feature(feature_id, stamp_feature_sweep({
                    "last_mergeable": info.get("mergeable", "UNKNOWN"),
                    "last_merge_state": info.get("mergeStateStatus", ""),
                    "unresolved_bot_threads": len(unresolved_bot_threads),
                }))
                continue
            if dry_run:
                print(f"DRY-RUN pr-sweep feature {feature_id}: would enqueue feedback for {len(feedback_comments)} comment(s)")
                continue
            feedback_task_id = _enqueue_feature_pr_feedback(
                feature, project_name, pr_number, comments=feedback_comments, check_failures=[], conflicts=False,
            )
            last_reason = "unresolved_bot_review_threads" if new_thread_comments else "comments"
            update_feature(feature_id, stamp_feature_sweep({
                "feedback_rounds": rounds + 1,
                "last_feedback_reason": last_reason,
                "feedback_task_id": feedback_task_id,
                "feedback_task_dispatched_at": now_iso(),
                "handled_comment_ids": _merge_comment_ids(handled_ids, feedback_comments),
                "unresolved_bot_threads": len(unresolved_bot_threads),
                "last_mergeable": info.get("mergeable", "UNKNOWN"),
                "last_merge_state": info.get("mergeStateStatus", ""),
            }))
            fb_enqueued += 1
            continue
        if unresolved_bot_threads:
            if rounds >= PR_SWEEP_MAX_FEEDBACK_ROUNDS:
                if sweep.get("escalated_comments"):
                    update_feature(feature_id, stamp_feature_sweep({
                        "last_mergeable": info.get("mergeable", "UNKNOWN"),
                        "last_merge_state": info.get("mergeStateStatus", ""),
                        "unresolved_bot_threads": len(unresolved_bot_threads),
                    }))
                    skipped += 1
                    continue
                _write_pr_alert(
                    project_name, feature_id, pr_number,
                    f"exhausted {rounds} feature-pr feedback rounds, "
                    f"{len(unresolved_bot_threads)} unresolved bot review thread(s) remain unresolved",
                    pr_url,
                )
                update_feature(feature_id, stamp_feature_sweep({
                    "escalated_comments": True,
                    "last_mergeable": info.get("mergeable", "UNKNOWN"),
                    "last_merge_state": info.get("mergeStateStatus", ""),
                    "unresolved_bot_threads": len(unresolved_bot_threads),
                }))
                alerted += 1
                continue
            guard_task_id = sweep.get("feedback_task_id")
            if guard_task_id and _task_exists_in_queue(guard_task_id):
                record_pr_sweep_guard_skip("feature_feedback")
                update_feature(feature_id, stamp_feature_sweep({
                    "last_mergeable": info.get("mergeable", "UNKNOWN"),
                    "last_merge_state": info.get("mergeStateStatus", ""),
                    "unresolved_bot_threads": len(unresolved_bot_threads),
                }))
                continue
            if branch_repair_in_flight:
                record_pr_sweep_guard_skip("feature_branch_repair")
                update_feature(feature_id, stamp_feature_sweep({
                    "last_mergeable": info.get("mergeable", "UNKNOWN"),
                    "last_merge_state": info.get("mergeStateStatus", ""),
                    "unresolved_bot_threads": len(unresolved_bot_threads),
                }))
                continue
            if dry_run:
                print(f"DRY-RUN pr-sweep feature {feature_id}: would retry feedback for "
                      f"{len(unresolved_bot_threads)} unresolved bot review thread(s)")
                continue
            feedback_task_id = _enqueue_feature_pr_feedback(
                feature, project_name, pr_number,
                comments=unresolved_bot_threads, check_failures=[], conflicts=False,
            )
            update_feature(feature_id, stamp_feature_sweep({
                "feedback_rounds": rounds + 1,
                "last_feedback_reason": "unresolved_bot_review_threads_retry",
                "feedback_task_id": feedback_task_id,
                "feedback_task_dispatched_at": now_iso(),
                "handled_comment_ids": _merge_comment_ids(handled_ids, unresolved_bot_threads),
                "unresolved_bot_threads": len(unresolved_bot_threads),
                "last_mergeable": info.get("mergeable", "UNKNOWN"),
                "last_merge_state": info.get("mergeStateStatus", ""),
            }))
            fb_enqueued += 1
            continue
        if merge_state in ("BLOCKED", "UNSTABLE") and failed_checks:
            if rounds >= PR_SWEEP_MAX_FEEDBACK_ROUNDS:
                if sweep.get("escalated_checks"):
                    update_feature(feature_id, stamp_feature_sweep({
                        "last_mergeable": info.get("mergeable", "UNKNOWN"),
                        "last_merge_state": info.get("mergeStateStatus", ""),
                        "failing_checks": [c["name"] for c in failed_checks],
                    }))
                    skipped += 1
                    continue
                _write_pr_alert(
                    project_name, feature_id, pr_number,
                    f"exhausted {rounds} feature-pr feedback rounds, failed checks still blocking",
                    pr_url,
                )
                update_feature(feature_id, stamp_feature_sweep({
                    "escalated_checks": True,
                    "last_mergeable": info.get("mergeable", "UNKNOWN"),
                    "last_merge_state": info.get("mergeStateStatus", ""),
                    "failing_checks": [c["name"] for c in failed_checks],
                }))
                alerted += 1
                continue
            guard_task_id = sweep.get("feedback_task_id")
            if guard_task_id and _task_exists_in_queue(guard_task_id):
                record_pr_sweep_guard_skip("feature_check_failure")
                update_feature(feature_id, stamp_feature_sweep({
                    "last_mergeable": info.get("mergeable", "UNKNOWN"),
                    "last_merge_state": info.get("mergeStateStatus", ""),
                    "failing_checks": [c["name"] for c in failed_checks],
                }))
                continue
            if branch_repair_in_flight:
                record_pr_sweep_guard_skip("feature_branch_repair")
                update_feature(feature_id, stamp_feature_sweep({
                    "last_mergeable": info.get("mergeable", "UNKNOWN"),
                    "last_merge_state": info.get("mergeStateStatus", ""),
                    "failing_checks": [c["name"] for c in failed_checks],
                }))
                continue
            if dry_run:
                print(f"DRY-RUN pr-sweep feature {feature_id}: would enqueue feedback for failed checks")
                continue
            check_comments = unresolved_bot_threads if _checks_require_thread_feedback(failed_checks) else []
            feedback_task_id = _enqueue_feature_pr_feedback(
                feature, project_name, pr_number, comments=check_comments, check_failures=failed_checks, conflicts=False,
            )
            update_feature(feature_id, stamp_feature_sweep({
                "feedback_rounds": rounds + 1,
                "last_feedback_reason": "checks",
                "feedback_task_id": feedback_task_id,
                "feedback_task_dispatched_at": now_iso(),
                "handled_comment_ids": _merge_comment_ids(handled_ids, check_comments),
                "failing_checks": [c["name"] for c in failed_checks],
                "last_mergeable": info.get("mergeable", "UNKNOWN"),
                "last_merge_state": info.get("mergeStateStatus", ""),
            }))
            fb_enqueued += 1
            continue

        update_feature(feature_id, stamp_feature_sweep({
            "last_mergeable": info.get("mergeable", "UNKNOWN"),
            "last_merge_state": info.get("mergeStateStatus", ""),
            "last_review_decision": info.get("reviewDecision", ""),
            "unresolved_bot_threads": len(unresolved_bot_threads),
            "failing_checks": [c["name"] for c in failed_checks],
        }))

    return (checked, merged, fb_enqueued, alerted, skipped)


def update_task_in_place(task_file, mutator):
    """Read, mutate, and write a task JSON at its current path. No state transition."""
    task_file = pathlib.Path(task_file)
    state = task_file.parent.name
    task_id = task_file.stem
    found = find_task(task_id, states=(state,))
    task = dict((found or (None, {}))[1] or {})
    mutator(task)
    _write_task_record(task, state)
    return task


def _task_exists_in_queue(task_id, states=("queued", "claimed", "running")):
    return find_task(task_id, states=states) is not None


def _feature_has_in_flight_branch_repair(feature_id, *, exclude_task_ids=()):
    """Whether another queued/claimed/running task is already advancing a feature branch.

    >>> import json, pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     old = _feature_has_in_flight_branch_repair.__globals__["QUEUE_ROOT"]
    ...     _feature_has_in_flight_branch_repair.__globals__["QUEUE_ROOT"] = queue_root
    ...     _ = (queue_root / "running" / "task-1.json").write_text(json.dumps({"task_id": "task-1", "feature_id": "f1"}))
    ...     _ = (queue_root / "done" / "task-2.json").write_text(json.dumps({"task_id": "task-2", "feature_id": "f1"}))
    ...     out = (
    ...         _feature_has_in_flight_branch_repair("f1"),
    ...         _feature_has_in_flight_branch_repair("f1", exclude_task_ids={"task-1"}),
    ...         _feature_has_in_flight_branch_repair("missing"),
    ...     )
    ...     _feature_has_in_flight_branch_repair.__globals__["QUEUE_ROOT"] = old
    >>> out
    (True, False, False)
    """
    exclude = set(exclude_task_ids or ())
    for task in iter_tasks(states=("queued", "claimed", "running")):
        if task.get("feature_id") != feature_id:
            continue
        if task.get("task_id") in exclude:
            continue
        return True
    return False


def _run_git_capture(worktree, args, *, timeout=15, warn_prefix="git"):
    wt = pathlib.Path(worktree) if worktree else None
    if wt is None or not wt.exists():
        print(f"{warn_prefix}: git command skipped, worktree missing")
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(wt), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"{warn_prefix}: git {' '.join(args)} failed: {exc}")
        return None
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:200]
        print(f"{warn_prefix}: git {' '.join(args)} failed: {err or 'non-zero exit'}")
        return None
    return proc.stdout


def _git_drift_ahead_count(worktree, base_ref, *, timeout=15, warn_prefix="git-drift"):
    if _run_git_capture(worktree, ["fetch", "origin", base_ref], timeout=timeout, warn_prefix=warn_prefix) is None:
        return None
    count_text = _run_git_capture(
        worktree,
        ["rev-list", "--count", f"HEAD..origin/{base_ref}"],
        timeout=timeout,
        warn_prefix=warn_prefix,
    )
    if count_text is None:
        return None
    try:
        return int((count_text or "0").strip())
    except ValueError:
        print(f"{warn_prefix}: git rev-list returned non-integer drift count: {(count_text or '').strip()[:40]}")
        return None


def _trim_conflict_preview(preview, budget=4000):
    while len(json.dumps(preview, sort_keys=True)) > budget and (preview.get("our_commits") or preview.get("their_commits")):
        for key in ("our_commits", "their_commits"):
            text = preview.get(key) or ""
            if not text:
                continue
            if len(text) <= 64:
                preview[key] = ""
            else:
                cut = max(32, len(text) - max(128, len(json.dumps(preview, sort_keys=True)) - budget))
                preview[key] = text[:cut].rstrip() + "\n...[truncated]"
            if len(json.dumps(preview, sort_keys=True)) <= budget:
                break
    while len(json.dumps(preview, sort_keys=True)) > budget and preview.get("likely_conflict_files"):
        preview["likely_conflict_files"] = preview["likely_conflict_files"][:-1]
    return preview


def _build_conflict_preview(worktree, base_branch):
    ref = f"origin/{base_branch}"
    our_commits = _run_git_capture(
        worktree, ["log", "-n", "50", "--oneline", "--stat", f"{ref}..HEAD"], warn_prefix="pr-feedback preview",
    )
    their_commits = _run_git_capture(
        worktree, ["log", "-n", "50", "--oneline", "--stat", f"HEAD..{ref}"], warn_prefix="pr-feedback preview",
    )
    ours_files = _run_git_capture(
        worktree, ["diff", "--name-only", f"{ref}...HEAD"], warn_prefix="pr-feedback preview",
    )
    theirs_files = _run_git_capture(
        worktree, ["diff", "--name-only", f"HEAD...{ref}"], warn_prefix="pr-feedback preview",
    )
    preview = {
        "our_commits": (our_commits or "").strip(),
        "their_commits": (their_commits or "").strip(),
        "likely_conflict_files": sorted(set(filter(None, (ours_files or "").splitlines())) & set(filter(None, (theirs_files or "").splitlines()))),
    }
    if not any((preview["our_commits"], preview["their_commits"], preview["likely_conflict_files"])):
        return None
    return _trim_conflict_preview(preview)


def _enqueue_pr_feedback(target, project_name, pr_number, base_branch, *, conflicts, comments, check_failures):
    """Create a codex pr-feedback task bound to target's feature_id.

    >>> captured = {}
    >>> original = {k: _enqueue_pr_feedback.__globals__[k] for k in ("new_task", "enqueue_task", "_build_conflict_preview")}
    >>> _enqueue_pr_feedback.__globals__["new_task"] = lambda **kwargs: captured.setdefault("task", {"task_id": "task-preview", **kwargs}) or captured["task"]
    >>> _enqueue_pr_feedback.__globals__["enqueue_task"] = lambda task: captured.setdefault("enqueued", task["task_id"])
    >>> _enqueue_pr_feedback.__globals__["_build_conflict_preview"] = lambda worktree, base: {"our_commits": "a", "their_commits": "b", "likely_conflict_files": ["conflict.py"]}
    >>> target = {"task_id": "task-target", "feature_id": "f1", "worktree": "/tmp/demo"}
    >>> _enqueue_pr_feedback(target, "demo", 42, "feature/demo", conflicts=True, comments=[], check_failures=[])
    'task-preview'
    >>> captured["task"]["engine_args"]["conflict_preview"]["likely_conflict_files"]
    ['conflict.py']
    >>> for key, value in original.items():
    ...     _enqueue_pr_feedback.__globals__[key] = value
    """
    target_id = target.get("task_id")
    summary_prefix = "Rebase and address feedback" if conflicts else "Address feedback"
    conflict_preview = _build_conflict_preview(target.get("worktree"), base_branch) if conflicts else None
    summary = (
        f"{summary_prefix} on pr #{pr_number} for {target_id}"
    )[:240]
    task = new_task(
        role="implementer",
        engine="codex",
        project=project_name,
        summary=summary,
        source=f"pr-sweep:{target_id}",
        braid_template="pr-address-feedback",
        parent_task_id=target_id,
        feature_id=target.get("feature_id"),
        engine_args={
            "mode": "pr-feedback",
            "target_task_id": target_id,
            "pr_number": pr_number,
            "base_branch": base_branch,
            "conflicts": conflicts,
            "conflict_preview": conflict_preview,
            "comments": comments,
            "check_failures": check_failures,
        },
    )
    enqueue_task(task)
    return task["task_id"]


def _enqueue_feature_pr_feedback(feature, project_name, pr_number, *, comments, check_failures, conflicts):
    """Create a codex feature-feedback task bound to an existing feature_id.

    The child task branches from `feature/<id>`, runs the `pr-address-feedback`
    graph with the feature PR's review context, then goes through the normal
    reviewer/qa/task-pr flow back into the same feature branch.
    """
    feature_id = feature.get("feature_id")
    summary = (
        f"Address feature PR #{pr_number} feedback for {feature_id}"
    )[:240]
    thread_ids = sorted({c.get("thread_id") for c in (comments or []) if c.get("thread_id")})
    task = new_task(
        role="implementer",
        engine="codex",
        project=project_name,
        summary=summary,
        source=f"feature-pr-sweep:{feature_id}",
        braid_template="pr-address-feedback",
        feature_id=feature_id,
        engine_args={
            "mode": "feature-pr-feedback",
            "feature_pr_number": pr_number,
            "comments": comments,
            "check_failures": check_failures,
            "conflicts": conflicts,
            "feature_review_thread_ids": thread_ids,
        },
    )
    enqueue_task(task)
    return task["task_id"]


def _merge_comment_ids(existing_ids, comments):
    merged = list(existing_ids or [])
    seen = {str(cid) for cid in merged if cid is not None}
    for comment in comments or []:
        cid = comment.get("id")
        if cid is None:
            continue
        cid = str(cid)
        if cid in seen:
            continue
        seen.add(cid)
        merged.append(cid)
    return merged


def _checks_require_thread_feedback(failed_checks):
    """Whether the failing check set means unresolved review threads still need fixing.

    >>> _checks_require_thread_feedback([{"name": "unresolved-review-threads"}])
    True
    >>> _checks_require_thread_feedback([{"name": "CI"}])
    False
    """
    for check in failed_checks or []:
        name = str(check.get("name") or "").lower()
        if "unresolved" in name and "review" in name:
            return True
    return False


def _load_feature_children(feature):
    """Load child task JSONs for a feature across all queue states."""
    children = []
    for child_id in feature.get("child_task_ids", []):
        found = find_task(child_id)
        if not found:
            print(f"feature-finalize {feature.get('feature_id')}: orphan child id {child_id}")
            return None
        state, child = found
        child = dict(child or {})
        child["state"] = state
        children.append(child)
    return children


def _build_final_pr_body(feature, children):
    """Aggregate child PR evidence into a feature->main PR body."""
    lines = [
        f"# {feature.get('summary') or feature.get('feature_id')}",
        "",
        "<!-- devmini-orchestrator: aggregate final PR body -->",
        "",
        "@codex please review this change.",
        "",
        "## Feature",
        "",
        f"- **Feature id:** `{feature.get('feature_id')}`",
        f"- **Project:** `{feature.get('project')}`",
        f"- **Branch:** `{feature.get('branch')}`",
        f"- **Created:** `{feature.get('created_at')}`",
        "",
        "## Included task PRs",
        "",
    ]
    for child in children:
        task_id = child.get("task_id", "(unknown)")
        pr_number = child.get("pr_number")
        pr_url = child.get("pr_url")
        pr_ref = pr_url or (f"#{pr_number}" if pr_number else "(unknown)")
        lines.append(f"### {task_id} — {child.get('summary', '(no summary)')}")
        lines.append("")
        lines.append(f"- PR: {pr_ref}")
        lines.append(f"- Branch: `{child.get('push_branch') or f'agent/{task_id}'}`")
        lines.append(f"- Merged at: `{child.get('pr_merged_at') or child.get('auto_merged_at') or '(unknown)'}`")
        lines.append(f"- Reviewer verdict: {child.get('review_verdict') or '(not recorded)'}")
        pr_body_path = child.get("pr_body_path")
        if pr_body_path:
            lines.append(f"- PR body artifact: `{pr_body_path}`")
        lines.append("")
    lines.extend([
        "## Notes",
        "",
        "- Child task PRs landed on the feature branch and were auto-merged there after smoke/review.",
        "- This feature PR targets `main` and requires human review plus a human merge.",
        "",
    ])
    return "\n".join(lines)


def _feature_branch_on_origin(project_path, branch):
    """Return True if origin has `branch`, False if absent."""
    proc = subprocess.run(
        ["git", "-C", project_path, "ls-remote", "--heads", "origin", branch],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:300]
        raise RuntimeError(err or "git ls-remote failed")
    return bool((proc.stdout or "").strip())


def _feature_finalize_blocking_issue(project_name):
    for issue in environment_health(refresh=True).get("issues", []):
        if issue.get("severity") != "error":
            continue
        code = issue.get("code")
        scope = issue.get("project")
        if code == "delivery_auth_expired":
            return issue
        if scope and scope != project_name:
            continue
        if code in ("project_main_dirty", "runtime_env_dirty"):
            return issue
    return None


def _feature_all_children_failed_without_retry(feature):
    """Return True when every child landed failed/abandoned and no retry is active.

    >>> _feature_all_children_failed_doctest()
    True
    """
    child_ids = feature.get("child_task_ids") or []
    if not child_ids:
        return False
    all_failed = True
    for child_id in child_ids:
        found = find_task(child_id, states=("failed", "abandoned"))
        if found is None:
            all_failed = False
            break
    if not all_failed:
        return False
    fid = feature.get("feature_id")
    for task in iter_tasks(states=("queued", "claimed", "running")):
        if task.get("feature_id") == fid:
            return False
    return True


def _feature_all_children_failed_doctest():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        queue_root = root / "queue"
        for state in STATES:
            (queue_root / state).mkdir(parents=True, exist_ok=True)
        old_root = _feature_all_children_failed_doctest.__globals__["QUEUE_ROOT"]
        _feature_all_children_failed_doctest.__globals__["QUEUE_ROOT"] = queue_root
        try:
            write_json_atomic(queue_root / "failed" / "task-a.json", {"task_id": "task-a", "feature_id": "feature-1"})
            write_json_atomic(queue_root / "abandoned" / "task-b.json", {"task_id": "task-b", "feature_id": "feature-1"})
            return _feature_all_children_failed_without_retry({"feature_id": "feature-1", "child_task_ids": ["task-a", "task-b"]})
        finally:
            _feature_all_children_failed_doctest.__globals__["QUEUE_ROOT"] = old_root


def feature_finalize(dry_run=False):
    """Open feature->main PRs for fully merged features.

    Returns (checked, opened, abandoned, skipped).
    """
    if shutil.which("gh") is None:
        print("feature-finalize: gh CLI not installed, nothing to do", file=sys.stderr)
        return (0, 0, 0, 0)

    config = load_config()
    checked = opened = abandoned = skipped = 0

    for feature in list_features(status="open"):
        checked += 1
        feature_id = feature.get("feature_id")
        children = _load_feature_children(feature)
        if children is None:
            skipped += 1
            continue

        ready = all(
            child.get("state") == "done"
            and child.get("cleaned_at") is not None
            and child.get("pr_final_state") == "MERGED"
            for child in children
        )
        if not ready:
            if feature.get("status") == "open" and _feature_all_children_failed_without_retry(feature):
                try:
                    project = get_project(config, feature["project"])
                except KeyError:
                    print(f"feature-finalize {feature_id}: unknown project {feature.get('project')}, skip abandon")
                    skipped += 1
                    continue
                branch = feature.get("branch") or f"feature/{feature_id}"
                if dry_run:
                    print(f"DRY-RUN feature-finalize {feature_id}: would mark abandoned (all children failed)")
                else:
                    def mut_feature(f):
                        f["status"] = "abandoned"
                        f["abandoned_at"] = now_iso()
                        f["abandoned_reason"] = "all children failed with no retry"
                    update_feature(feature_id, mut_feature)
                    append_transition(feature_id, "open", "abandoned", "all children failed")
                    append_event(
                        "feature-finalize",
                        "feature_abandoned",
                        feature_id=feature_id,
                        details={"project": project["name"], "reason": "all children failed with no retry"},
                    )
                    _write_pr_alert(
                        project["name"], feature_id, "feature", "all children failed with no retry", None,
                    )
                    branch_drop = subprocess.run(
                        ["git", "branch", "-d", branch],
                        cwd=project["path"],
                        capture_output=True, text=True, check=False,
                    )
                    if branch_drop.returncode != 0:
                        print(f"feature-finalize {feature_id}: local branch retained ({branch_drop.stderr.strip()[:200]})")
                abandoned += 1
            continue

        if not feature.get("child_task_ids"):
            if dry_run:
                print(f"DRY-RUN feature-finalize {feature_id}: would mark abandoned (no children)")
            else:
                update_feature(feature_id, lambda f: f.update({"status": "abandoned", "abandoned_at": now_iso(), "abandoned_reason": "feature has no child tasks"}))
                append_transition(feature_id, "open", "abandoned", "feature has no child tasks")
                append_event(
                    "feature-finalize",
                    "feature_abandoned",
                    feature_id=feature_id,
                    details={"project": feature.get("project"), "reason": "feature has no child tasks"},
                )
            abandoned += 1
            continue

        try:
            project = get_project(config, feature["project"])
        except KeyError:
            print(f"feature-finalize {feature_id}: unknown project {feature.get('project')}, skip")
            skipped += 1
            continue

        env_issue = _feature_finalize_blocking_issue(project["name"])
        if env_issue is not None:
            reason = f"{env_issue.get('code')}: {env_issue.get('summary')}"
            if not dry_run:
                update_feature(
                    feature_id,
                    lambda f: f.update(
                        {
                            "finalize_error": reason,
                            "finalize_blocker": env_issue,
                            "finalize_blocked_at": now_iso(),
                        }
                    ),
                )
                append_event(
                    "feature-finalize",
                    "blocked_by_environment",
                    feature_id=feature_id,
                    details={"project": project["name"], "issue": env_issue},
                )
            print(f"feature-finalize {feature_id}: blocked by environment health: {reason}")
            skipped += 1
            continue

        body = _build_final_pr_body(feature, children)
        body_path = STATE_ROOT / "artifacts" / feature_id / "final-pr-body.md"
        if dry_run:
            print(f"DRY-RUN feature-finalize {feature_id}: would write {body_path}")
        else:
            body_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_text(body)

        branch = feature.get("branch") or f"feature/{feature_id}"
        try:
            on_origin = _feature_branch_on_origin(project["path"], branch)
        except RuntimeError as exc:
            err = str(exc)
            print(f"feature-finalize {feature_id}: branch probe failed: {err}")
            skipped += 1
            continue
        if not on_origin:
            if dry_run:
                print(f"DRY-RUN feature-finalize {feature_id}: would stamp finalize_error=branch_missing")
            else:
                update_feature(feature_id, lambda f: f.update({"finalize_error": "branch_missing"}))
            skipped += 1
            continue

        if dry_run:
            print(f"DRY-RUN feature-finalize {feature_id}: would open PR {branch} -> main")
            continue

        title = (feature.get("summary") or feature_id).splitlines()[0]
        proc = subprocess.run(
            ["gh", "pr", "create",
             "--base", "main",
             "--head", branch,
             "--title", title,
             "--body-file", str(body_path)],
            cwd=project["path"],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:400]
            print(f"feature-finalize {feature_id}: gh pr create failed: {err}")
            if _gh_auth_failed(err):
                skipped += 1
                continue
            update_feature(feature_id, lambda f: f.update({"finalize_error": err or "gh pr create failed"}))
            skipped += 1
            continue

        pr_url, pr_number = _parse_pr_create_output(proc.stdout or "")

        def mut_feature(f):
            f["status"] = "finalizing"
            f["final_pr_number"] = pr_number
            f["final_pr_url"] = pr_url
            f["finalized_at"] = now_iso()
            f["finalize_error"] = None
            f["final_pr_review_requested_at"] = None
            f["final_pr_review_request_error"] = None

        update_feature(feature_id, mut_feature)
        review_ok, review_reason = _request_codex_review(
            project["path"],
            pr_number,
            reason="initial final feature PR",
        )
        if not review_ok:
            update_feature(
                feature_id,
                lambda f: f.update({"final_pr_review_request_error": review_reason}),
            )
        else:
            update_feature(
                feature_id,
                lambda f: f.update(
                    {
                        "final_pr_review_requested_at": now_iso(),
                        "final_pr_review_request_error": None,
                    }
                ),
            )
        opened += 1
        print(f"feature-finalize opened {feature_id} pr={pr_url or pr_number or '(unknown)'}")

    return (checked, opened, abandoned, skipped)


def cleanup_worktrees(dry_run=False):
    """Sweep closed/merged PRs and remove their local worktrees + branches.

    Scans queue/done/ for tasks that have a pr_number and no cleaned_at yet,
    queries `gh pr view <n> --json state,mergedAt,closedAt` in the project's
    canonical repo, and on MERGED or CLOSED removes the agent worktree via
    `git worktree remove --force` and the local branch via `git branch -D`.

    Remote branches are NOT touched — GitHub's per-repo "Delete branch on
    merge" setting (or the human's one-click on close) is the source of
    truth. This function only cleans local state.

    Task JSON is updated in place: stamps cleaned_at, pr_final_state,
    pr_merged_at, pr_closed_at. Does not move the task file out of done/.

    Also scans state/features/ for `finalizing` features with a `final_pr_number`.
    When the feature PR is MERGED, cleanup removes the local `feature/<id>`
    branch (if present) and stamps the feature `merged`. When CLOSED without a
    merge, cleanup marks the feature `abandoned`. Remote refs are never touched.

    Returns (checked, cleaned, skipped).
    """
    rotate_logs()
    if shutil.which("gh") is None:
        print("cleanup: gh CLI not installed, nothing to do", file=sys.stderr)
        return (0, 0, 0)

    config = load_config()
    done_dir = queue_dir("done")

    checked = 0
    cleaned = 0
    skipped = 0

    for task_file in sorted(done_dir.glob("*.json")):
        task = read_json(task_file, {})
        pr_number = task.get("pr_number")
        if not pr_number or task.get("cleaned_at"):
            continue

        checked += 1
        project_name = task.get("project")
        try:
            project = get_project(config, project_name)
        except KeyError:
            print(f"cleanup {task.get('task_id')}: unknown project {project_name}, skip")
            skipped += 1
            continue

        repo_path = project["path"]
        proc = subprocess.run(
            ["gh", "pr", "view", str(pr_number),
             "--json", "state,mergedAt,closedAt,url"],
            cwd=repo_path,
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            print(f"cleanup {task.get('task_id')}: gh pr view failed: {proc.stderr.strip()[:200]}")
            skipped += 1
            continue
        try:
            info = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            print(f"cleanup {task.get('task_id')}: bad gh pr view json")
            skipped += 1
            continue

        state = info.get("state", "")
        if state not in ("CLOSED", "MERGED"):
            continue  # still OPEN — leave it alone

        wt_path = task.get("worktree")
        branch = task.get("push_branch") or f"agent/{task.get('task_id')}"

        if dry_run:
            print(f"DRY-RUN cleanup {task.get('task_id')}: {state} would remove worktree={wt_path} branch={branch}")
            continue

        if wt_path and pathlib.Path(wt_path).exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", wt_path],
                cwd=repo_path,
                capture_output=True, text=True, check=False,
            )
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=repo_path,
            capture_output=True, text=True, check=False,
        )

        task["cleaned_at"] = now_iso()
        task["pr_final_state"] = state
        task["pr_merged_at"] = info.get("mergedAt")
        task["pr_closed_at"] = info.get("closedAt")
        if state == "MERGED":
            eargs = task.get("engine_args") or {}
            if eargs.get("mode") == "feature-pr-feedback":
                thread_ids = eargs.get("feature_review_thread_ids") or []
                if thread_ids:
                    resolved_count, failed_count = _resolve_review_threads(
                        repo_path, thread_ids,
                    )
                    task["resolved_thread_count"] = resolved_count
                    task["resolve_thread_failures"] = failed_count
        _write_task_record(task, task.get("state") or "done")
        append_transition(task.get("task_id", "?"), "done", "done",
                          reason=f"cleanup: {state}")
        cleaned += 1
        print(f"cleaned {task.get('task_id')}: {state}")

    for feature in list_features(status="finalizing"):
        pr_number = feature.get("final_pr_number")
        if not pr_number:
            continue

        checked += 1
        project_name = feature.get("project")
        try:
            project = get_project(config, project_name)
        except KeyError:
            print(f"cleanup feature {feature.get('feature_id')}: unknown project {project_name}, skip")
            skipped += 1
            continue

        repo_path = project["path"]
        proc = subprocess.run(
            ["gh", "pr", "view", str(pr_number),
             "--json", "state,mergedAt,closedAt,url"],
            cwd=repo_path,
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:200]
            print(f"cleanup feature {feature.get('feature_id')}: gh pr view failed: {err}")
            skipped += 1
            continue
        try:
            info = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            print(f"cleanup feature {feature.get('feature_id')}: bad gh pr view json")
            skipped += 1
            continue

        state = info.get("state", "")
        if state not in ("CLOSED", "MERGED"):
            continue

        branch = feature.get("branch") or f"feature/{feature.get('feature_id')}"
        if dry_run:
            print(f"DRY-RUN cleanup feature {feature.get('feature_id')}: {state} would update feature state and delete local branch={branch}")
            continue

        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=repo_path,
            capture_output=True, text=True, check=False,
        )

        def mut_feature(f):
            if state == "MERGED":
                f["status"] = "merged"
                f["merged_at"] = info.get("mergedAt")
            else:
                f["status"] = "abandoned"

        feature = update_feature(feature["feature_id"], mut_feature)
        if state == "MERGED" and feature.get("roadmap_entry_id"):
            historian = new_task(
                role="historian",
                engine="codex",
                project=project_name,
                summary=(
                    f"Flip ROADMAP.md [{feature['roadmap_entry_id']}] status TODO→DONE "
                    f"(feature {feature['feature_id']} merged)"
                ),
                source=f"cleanup-feature-merge:{feature['feature_id']}",
                braid_template=project_historian_template(project_name),
                feature_id=feature["feature_id"],
                engine_args={
                    "roadmap_entry_id": feature["roadmap_entry_id"],
                    "merged_feature_id": feature["feature_id"],
                },
            )
            enqueue_task(historian)
        cleaned += 1
        print(f"cleaned feature {feature.get('feature_id')}: {state}")

    return (checked, cleaned, skipped)


# --- BRAID template registry -------------------------------------------------

def load_braid_index():
    if not BRAID_INDEX.exists():
        return {}
    return json.loads(BRAID_INDEX.read_text())


def save_braid_index(idx):
    write_json_atomic(BRAID_INDEX, idx)


def braid_template_path(task_type):
    return BRAID_TEMPLATES / f"{task_type}.mmd"


def _braid_brackets_balanced(body):
    counts = {"[": 0, "]": 0, "{": 0, "}": 0}
    in_quote = False
    escaped = False
    for ch in body:
        if ch == "\\" and not escaped:
            escaped = True
            continue
        if ch == '"' and not escaped:
            in_quote = not in_quote
        elif not in_quote and ch in counts:
            counts[ch] += 1
            if ch == "]" and counts["]"] > counts["["]:
                return False
            if ch == "}" and counts["}"] > counts["{"]:
                return False
        escaped = False
    return counts["["] == counts["]"] and counts["{"] == counts["}"]


def _collect_braid_nodes_and_edges(body):
    nodes = {}
    adjacency = {}
    edge_lines = []
    errors = []
    subgraph_depth = 0
    for lineno, raw in enumerate(body.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("subgraph "):
            subgraph_depth += 1
            continue
        if line == "end":
            subgraph_depth -= 1
            if subgraph_depth < 0:
                errors.append(LintError("R6", "error", "unexpected `end`", lineno))
                subgraph_depth = 0
            continue

        for match in BRAID_NODE_DEF_RE.finditer(raw):
            label = match.group("shape")[1:-1].strip()
            node_id = match.group("node")
            current = nodes.get(node_id)
            if current is None or current["label"] == node_id:
                nodes[node_id] = {"label": label, "line": lineno}

        if "-->" in raw:
            start = BRAID_EDGE_START_RE.match(raw)
            end = BRAID_EDGE_END_RE.search(raw)
        else:
            start = None
            end = None
        if start and end:
            src = start.group("node")
            dst = end.group("node")
            adjacency.setdefault(src, set()).add(dst)
            adjacency.setdefault(dst, set())
            edge_lines.append((lineno, src, dst))
            nodes.setdefault(src, {"label": src, "line": lineno})
            nodes.setdefault(dst, {"label": dst, "line": lineno})

    if subgraph_depth:
        errors.append(LintError("R6", "error", "unclosed subgraph", None))
    return nodes, adjacency, edge_lines, errors


def _reachable_nodes(adjacency, start):
    seen = set()
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        for nxt in adjacency.get(node, ()):
            if nxt not in seen:
                queue.append(nxt)
    return seen


def _end_reachable_without_check(adjacency, nodes):
    queue = deque([("Start", False)])
    seen = set()
    while queue:
        node, saw_check = queue.popleft()
        state = (node, saw_check)
        if state in seen:
            continue
        seen.add(state)
        label = nodes.get(node, {}).get("label", "")
        now_checked = saw_check or label.startswith("Check:")
        if node == "End" and not now_checked:
            return True
        for nxt in adjacency.get(node, ()):
            queue.append((nxt, now_checked))
    return False


def _dirty_braid_template_names():
    try:
        proc = subprocess.run(
            ["git", "-C", str(STATE_ROOT), "status", "--porcelain", "braid/templates"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return set()
    if proc.returncode != 0:
        return set()
    names = set()
    for line in proc.stdout.splitlines():
        path = line[3:].strip()
        if path.startswith("braid/templates/") and path.endswith(".mmd"):
            names.add(pathlib.Path(path).stem)
    return names


def lint_template(body: str) -> list[LintError]:
    """Lint a BRAID Mermaid template.

    >>> any(e.rule == "R1" for e in lint_template("flowchart TD;\\nA[Read: one two three four five six seven eight nine ten eleven twelve thirteen fourteen]\\n"))
    True
    >>> any(e.rule == "R2" for e in lint_template("flowchart TD;\\nA --> B\\n"))
    True
    >>> any(e.rule == "R3" for e in lint_template("flowchart TD;\\nStart[Start] -- \\"always\\" --> End[End];\\n"))
    True
    >>> any(e.rule == "R4" for e in lint_template("flowchart TD;\\nStart[Start] -- \\"always\\" --> C1;\\nC1[Check: gate 1] -- \\"fail\\" --> Revise[Revise: generic loop];\\nC1 -- \\"pass\\" --> C2;\\nC2[Check: gate 2] -- \\"fail\\" --> Revise;\\nC2 -- \\"pass\\" --> C3;\\nC3[Check: gate 3] -- \\"fail\\" --> Revise;\\nC3 -- \\"pass\\" --> C4;\\nC4[Check: gate 4] -- \\"fail\\" --> Revise;\\nC4 -- \\"pass\\" --> C5;\\nC5[Check: gate 5] -- \\"fail\\" --> Revise;\\nC5 -- \\"pass\\" --> C6;\\nC6[Check: gate 6] -- \\"fail\\" --> Revise;\\nC6 -- \\"pass\\" --> C7;\\nC7[Check: gate 7] -- \\"fail\\" --> Revise;\\nC7 -- \\"pass\\" --> C8;\\nC8[Check: gate 8] -- \\"fail\\" --> Revise;\\nC8 -- \\"pass\\" --> C9;\\nC9[Check: gate 9] -- \\"fail\\" --> Revise;\\nC9 -- \\"pass\\" --> End[End];\\nRevise -- \\"retry\\" --> C1;\\n"))
    True
    >>> any(e.rule == "R5" for e in lint_template("flowchart TD;\\nStart[Start] -- \\"always\\" --> End[End];\\nOrphan[Draft: stray node]\\n"))
    True
    >>> any(e.rule == "R6" for e in lint_template("Start[Start] -- \\"always\\" --> End[End];\\n"))
    True
    >>> any(e.rule == "R7" for e in lint_template("flowchart TD;\\nStart[Start] -- \\"always\\" --> A[Read: Apache Kafka Streams];\\nA -- \\"done\\" --> End[End];\\n"))
    True
    >>> reviewer_body = braid_template_path("lvc-reviewer-pass").read_text()
    >>> any(e.rule == "R7" for e in lint_template(reviewer_body))
    False
    """
    errors = []
    stripped = [line.strip() for line in body.splitlines() if line.strip()]
    if not stripped or stripped[0] != "flowchart TD;":
        errors.append(LintError("R6", "error", "missing `flowchart TD;` prefix", 1))
    if not _braid_brackets_balanced(body):
        errors.append(LintError("R6", "error", "unbalanced brackets or braces", None))

    nodes, adjacency, _, parse_errors = _collect_braid_nodes_and_edges(body)
    errors.extend(parse_errors)

    for lineno, raw in enumerate(body.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("subgraph ") or line == "end":
            continue
        if BRAID_BARE_EDGE_RE.match(raw):
            errors.append(LintError("R2", "error", "edge is missing a quoted label", lineno))

    for node_id, meta in nodes.items():
        label = meta["label"]
        tokens = label.split()
        if len(tokens) >= 15:
            severity = "error" if label.startswith(BRAID_HARD_PREFIXES) else "warning"
            errors.append(
                LintError("R1", severity, f"{node_id} label has {len(tokens)} tokens", meta["line"])
            )
        words = [
            word for word in re.findall(r"\b[A-Z][A-Za-z0-9_/.-]*\b", label)
            if not word.isupper() and word not in R7_ALLOWED_CAPS
        ]
        if "<" not in label and len(words) >= 3:
            errors.append(
                LintError("R7", "warning", f"{node_id} label may leak repo literals", meta["line"])
            )

    if "Start" not in nodes:
        errors.append(LintError("R5", "error", "missing Start node", None))
    if "End" not in nodes:
        errors.append(LintError("R3", "error", "missing End node", None))

    reachable = _reachable_nodes(adjacency, "Start") if "Start" in nodes else set()
    for node_id, meta in nodes.items():
        if node_id != "Start" and node_id not in reachable:
            errors.append(LintError("R5", "error", f"{node_id} is unreachable from Start", meta["line"]))

    check_nodes = {node_id for node_id, meta in nodes.items() if meta["label"].startswith("Check:")}
    check_to_end = any("End" in _reachable_nodes(adjacency, node_id) for node_id in check_nodes)
    if "End" in nodes and not check_to_end:
        errors.append(LintError("R3", "error", "no `Check:` node has a path to End", None))
    if "End" in nodes and _end_reachable_without_check(adjacency, nodes):
        errors.append(LintError("R3", "error", "End is reachable without traversing a `Check:` node", None))

    revise_nodes = {
        node_id: meta
        for node_id, meta in nodes.items()
        if meta["label"].startswith("Revise")
    }
    if len(check_nodes) >= 9 and revise_nodes:
        distinct_revise = len(revise_nodes)
        has_gate_placeholder = any("<gate>" in meta["label"] for meta in revise_nodes.values())
        if distinct_revise < len(check_nodes) - 1 and not has_gate_placeholder:
            errors.append(
                LintError(
                    "R4",
                    "error",
                    "distinct Revise nodes are underspecified for the number of Check gates",
                    None,
                )
            )

    return errors


def braid_template_load(task_type):
    p = braid_template_path(task_type)
    if not p.exists():
        return None, None
    body = p.read_text()
    digest = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
    return body, digest


def braid_template_write(task_type, body, generator_model):
    errors = lint_template(body)
    hard_failures = [err for err in errors if err.severity == "error"]
    if hard_failures:
        joined = "; ".join(err.format() for err in hard_failures)
        raise ValueError(f"BRAID lint failed for {task_type}: {joined}")
    p = braid_template_path(task_type)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".mmd.tmp")
    tmp.write_text(body)
    os.rename(tmp, p)
    idx = load_braid_index()
    entry = idx.get(task_type, {})
    old_hash = entry.get("hash")
    new_hash = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
    entry.update(
        path=str(p.relative_to(STATE_ROOT)),
        hash=new_hash,
        generator_model=generator_model,
        created_at=now_iso(),
        uses=entry.get("uses", 0),
        topology_errors=entry.get("topology_errors", 0),
        recent_outcomes=list(entry.get("recent_outcomes") or []),
    )
    if entry.get("dispatch_paused") and old_hash != new_hash:
        entry.pop("dispatch_paused", None)
        entry.pop("dispatch_paused_at", None)
        entry.pop("dispatch_pause_reason", None)
        entry["auto_regen_resolved_at"] = now_iso()
    if old_hash != new_hash:
        entry["regen_attempts"] = 0
        entry.pop("regen_exhausted_at", None)
        entry.pop("regen_exhausted_reason", None)
    idx[task_type] = entry
    save_braid_index(idx)


def braid_template_record_use(task_type, topology_error=False):
    idx = load_braid_index()
    entry = idx.get(task_type, {})
    if topology_error:
        entry["topology_errors"] = entry.get("topology_errors", 0) + 1
    else:
        entry["uses"] = entry.get("uses", 0) + 1
    recent = list(entry.get("recent_outcomes") or [])
    recent.append("topology_error" if topology_error else "ok")
    entry["recent_outcomes"] = recent[-BRAID_RECENT_OUTCOMES_MAX:]
    idx[task_type] = entry
    save_braid_index(idx)


def request_template_regen(task_type, *, project_name, source_task_id=None, reason=""):
    cfg = load_config()
    idx = load_braid_index()
    entry = idx.get(task_type, {})
    max_attempts = int((cfg.get("template_regen") or {}).get("max_attempts", CONFIG_DEFAULTS["template_regen"]["max_attempts"]) or 3)
    attempts = int(entry.get("regen_attempts", 0) or 0)
    if entry.get("auto_regen_task_id") and _template_regen_in_flight(task_type):
        return {"enqueued": False, "reason": "already_in_flight", "attempts": attempts, "max_attempts": max_attempts}
    if attempts >= max_attempts:
        entry["regen_exhausted_at"] = now_iso()
        entry["regen_exhausted_reason"] = reason[:240]
        idx[task_type] = entry
        save_braid_index(idx)
        append_event(
            "template-auto-regen",
            "exhausted",
            task_id=source_task_id,
            details={"braid_template": task_type, "attempts": attempts, "max_attempts": max_attempts, "reason": reason[:240]},
        )
        return {"enqueued": False, "reason": "attempts_exhausted", "attempts": attempts, "max_attempts": max_attempts}
    task = new_task(
        role="planner",
        engine="claude",
        project=project_name,
        summary=f"Generate BRAID template for {task_type}",
        source=f"regen-for:{source_task_id or task_type}",
        braid_template=task_type,
        engine_args={"mode": "template-gen", "regen_reason": reason[:240], "origin_task_id": source_task_id},
    )
    enqueue_task(task)
    entry["regen_attempts"] = attempts + 1
    entry["auto_regen_task_id"] = task["task_id"]
    entry["auto_regen_requested_at"] = now_iso()
    entry["last_regen_reason"] = reason[:240]
    idx[task_type] = entry
    save_braid_index(idx)
    append_event(
        "template-auto-regen",
        "requested",
        task_id=source_task_id,
        details={"braid_template": task_type, "regen_task_id": task["task_id"], "attempts": attempts + 1, "max_attempts": max_attempts},
    )
    return {"enqueued": True, "task_id": task["task_id"], "attempts": attempts + 1, "max_attempts": max_attempts}


def lint_templates_command(*, template=None, lint_all=False):
    failures = 0
    warnings = 0

    if lint_all:
        names = sorted(p.stem for p in BRAID_TEMPLATES.glob("*.mmd"))
        dirty = _dirty_braid_template_names()
        names = [name for name in names if name not in dirty]
        if dirty:
            print("skipped dirty templates:", ", ".join(sorted(dirty)))
    else:
        names = [template]

    for name in names:
        body, _ = braid_template_load(name)
        if body is None:
            print(f"{name}: missing template")
            failures += 1
            continue
        errors = lint_template(body)
        hard = [err for err in errors if err.severity == "error"]
        warns = [err for err in errors if err.severity == "warning"]
        warnings += len(warns)
        if hard:
            failures += 1
            print(f"{name}: FAIL")
            for err in hard + warns:
                print(f"  {err.format()}")
        else:
            print(f"{name}: OK")
            for err in warns:
                print(f"  {err.format()}")

    if failures:
        return 1
    print(f"lint summary: {len(names)} checked, {warnings} warnings")
    return 0


# --- Feature entities --------------------------------------------------------
#
# A feature is a branch-scoped container of codex slices that share a common
# delivery target. Each feature has its own long-lived git branch
# `feature/<feature_id>` in the project repo. Individual slice task PRs target
# that branch (auto-merged once smoke+reviewer+pr-sweep are green) and a single
# feature->main PR opens for human review once all children have merged.
#
# Per-feature rule: at most ONE codex task in flight per feature at a time.
# Enforced in atomic_claim by reading claimed/ + running/ task files and
# excluding queued tasks whose feature_id is already busy. Sibling slices on
# the same feature serialize to avoid merge conflicts on the shared branch;
# parallelism happens ACROSS features, bounded by codex slot count.

FEATURE_STATES = ("open", "finalizing", "merged", "abandoned")
TERMINAL_FEATURE_STATES = {"merged", "abandoned"}


def new_feature_id():
    return f"feature-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def feature_path(feature_id):
    return FEATURES_DIR / f"{feature_id}.json"


def read_feature(feature_id):
    if _state_engine_read_enabled():
        try:
            row = get_state_engine().read_feature(feature_id)
            if row is not None or not feature_path(feature_id).exists():
                return row
            _record_state_engine_fs_fallback("feature", feature_id)
        except Exception:
            _record_state_engine_fs_fallback("feature", feature_id)
    return read_json(feature_path(feature_id), None)


def list_features(status=None):
    if _state_engine_read_enabled():
        try:
            rows = get_state_engine().read_features(status=status)
            if rows or not FEATURES_DIR.exists():
                return rows
            _record_state_engine_fs_fallback("features", status or "*")
        except Exception:
            _record_state_engine_fs_fallback("features", status or "*")
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(FEATURES_DIR.glob("*.json")):
        f = read_json(p, {})
        if status and f.get("status") != status:
            continue
        out.append(f)
    return out


def _project_has_open_feature(project_name):
    """Return True when a project has any non-terminal feature record.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = _project_has_open_feature.__globals__["FEATURES_DIR"]
    ...     _project_has_open_feature.__globals__["FEATURES_DIR"] = pathlib.Path(tmp)
    ...     _project_has_open_feature("demo")
    ...     _project_has_open_feature.__globals__["FEATURES_DIR"] = old
    False

    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = _project_has_open_feature.__globals__["FEATURES_DIR"]
    ...     _project_has_open_feature.__globals__["FEATURES_DIR"] = pathlib.Path(tmp)
    ...     write_json_atomic(pathlib.Path(tmp) / "open.json", {"project": "demo", "status": "open"})
    ...     _project_has_open_feature("demo")
    ...     _project_has_open_feature.__globals__["FEATURES_DIR"] = old
    True

    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = _project_has_open_feature.__globals__["FEATURES_DIR"]
    ...     _project_has_open_feature.__globals__["FEATURES_DIR"] = pathlib.Path(tmp)
    ...     write_json_atomic(pathlib.Path(tmp) / "merged.json", {"project": "demo", "status": "merged"})
    ...     _project_has_open_feature("demo")
    ...     _project_has_open_feature.__globals__["FEATURES_DIR"] = old
    False

    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = _project_has_open_feature.__globals__["FEATURES_DIR"]
    ...     _project_has_open_feature.__globals__["FEATURES_DIR"] = pathlib.Path(tmp)
    ...     write_json_atomic(pathlib.Path(tmp) / "other.json", {"project": "other", "status": "open"})
    ...     _project_has_open_feature("demo")
    ...     _project_has_open_feature.__globals__["FEATURES_DIR"] = old
    False
    """
    for f in list_features():
        if f.get("project") == project_name and f.get("status") in ("open", "finalizing"):
            return True
    return False


def create_feature(*, project, summary, source, roadmap_entry=None, roadmap_entry_id=None, canary=None, self_repair=None):
    """Create a new feature record in state/features/<id>.json.

    The git branch itself is created lazily by the first codex worker that
    picks up a slice of this feature (worker.py make_worktree branches from
    `feature/<id>`, creating the ref on origin if it doesn't exist yet).
    That keeps orchestrator.py out of git-push/auth territory — its only job
    is to track intent.
    """
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    feature_id = new_feature_id()
    branch = f"feature/{feature_id}"
    feature = {
        "feature_id": feature_id,
        "project": project,
        "branch": branch,
        "summary": (
            f"[{roadmap_entry['id']}] {roadmap_entry['title']}"
            if roadmap_entry else summary
        ),
        "roadmap_entry_id": roadmap_entry_id or (roadmap_entry or {}).get("id"),
        "source": source,
        "status": "open",
        "child_task_ids": [],
        "created_at": now_iso(),
        "final_pr_number": None,
        "final_pr_url": None,
        "finalized_at": None,
        "merged_at": None,
        "finalize_error": None,
        "canary": dict(canary or {}),
        "self_repair": dict(self_repair or {}),
    }
    _write_feature_record(feature)
    return feature


def is_canary_feature(feature):
    return bool((feature or {}).get("canary", {}).get("enabled"))


def is_self_repair_feature(feature):
    return bool((feature or {}).get("self_repair", {}).get("enabled"))


def list_canary_features(*, project_name=None, statuses=None):
    feats = []
    allowed = set(statuses or ())
    for feature in list_features():
        if not is_canary_feature(feature):
            continue
        if project_name is not None and feature.get("project") != project_name:
            continue
        if allowed and feature.get("status") not in allowed:
            continue
        feats.append(feature)
    feats.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return feats


def _latest_canary_feature(project_name=None):
    rows = list_canary_features(project_name=project_name)
    return rows[0] if rows else None


def _latest_successful_canary(project_name=None):
    rows = list_canary_features(project_name=project_name, statuses=("merged",))
    return rows[0] if rows else None


def list_self_repair_features(*, project_name=None, statuses=None):
    feats = []
    allowed = set(statuses or ())
    for feature in list_features():
        if not is_self_repair_feature(feature):
            continue
        if project_name is not None and feature.get("project") != project_name:
            continue
        if allowed and feature.get("status") not in allowed:
            continue
        feats.append(feature)
    feats.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return feats


def _active_self_repair_feature(project_name="devmini-orchestrator"):
    rows = list_self_repair_features(project_name=project_name, statuses=("open", "finalizing"))
    return rows[0] if rows else None


def _normalize_self_repair_issue(issue, *, cfg=None):
    issue = issue or {}
    changed = False
    normalized_attempts = int(issue.get("attempts") or 0)
    if issue.get("attempts") != normalized_attempts:
        issue["attempts"] = normalized_attempts
        changed = True
    normalized_max_attempts = int(issue.get("max_attempts") or self_repair_issue_max_attempts(cfg=cfg))
    if issue.get("max_attempts") != normalized_max_attempts:
        issue["max_attempts"] = normalized_max_attempts
        changed = True
    if "execution_task_ids" not in issue or issue.get("execution_task_ids") is None:
        issue["execution_task_ids"] = []
        changed = True
    if "superseded_task_ids" not in issue or issue.get("superseded_task_ids") is None:
        issue["superseded_task_ids"] = []
        changed = True
    return changed


def _normalize_self_repair_feature_issues(feature_id, *, cfg=None):
    changed = False

    def mut(feature):
        nonlocal changed
        issues = ((feature.setdefault("self_repair", {})).setdefault("issues", []))
        for issue in issues:
            changed = _normalize_self_repair_issue(issue, cfg=cfg) or changed

    updated = update_feature(feature_id, mut)
    return updated, changed


def _self_repair_issue_key(*, issue_key=None, issue_kind="runtime_bug", summary="", evidence="", source="manual"):
    if issue_key:
        return str(issue_key)
    raw = json.dumps(
        {
            "issue_kind": issue_kind,
            "summary": (summary or "").strip(),
            "evidence": (evidence or "").strip(),
            "source": source,
        },
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _self_repair_issue_live(issue):
    issue = issue or {}
    task_ids = []
    planner_task_id = issue.get("planner_task_id")
    if planner_task_id:
        task_ids.append(planner_task_id)
    task_ids.extend(issue.get("execution_task_ids") or ())
    for task_id in task_ids:
        found = find_task(task_id)
        if found and found[0] in ("queued", "claimed", "running"):
            return True
    return False


def _self_repair_pending_issues(feature):
    issues = list(((feature or {}).get("self_repair") or {}).get("issues") or [])
    return [issue for issue in issues if issue.get("status") in (None, "pending", "scheduled", "stalled")]


def _self_repair_has_active_work(feature):
    for issue in (((feature or {}).get("self_repair") or {}).get("issues") or []):
        if issue.get("status") not in (None, "pending", "scheduled", "stalled", "resolved", "rejected"):
            return True
        if _self_repair_issue_live(issue):
            return True
    return False


def _self_repair_mark_issue(feature_id, issue_key, **updates):
    def mut(feature):
        sr = feature.setdefault("self_repair", {})
        issues = sr.setdefault("issues", [])
        for issue in issues:
            if issue.get("issue_key") == issue_key:
                issue.update(updates)
                break
    return update_feature(feature_id, mut)


def _self_repair_issue_observation_target(issue):
    issue_key = str((issue or {}).get("issue_key") or "")
    if not issue_key.startswith("task:"):
        return None
    parts = issue_key.split(":", 3)
    if len(parts) < 4:
        return None
    _, task_id, state, blocker_code = parts
    if not task_id or not blocker_code:
        return None
    return {
        "task_id": task_id,
        "state": state,
        "blocker_code": blocker_code,
    }


def _self_repair_execution_terminal(task_id):
    """Return the resolution state for one execution slice.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     old = _self_repair_execution_terminal.__globals__["QUEUE_ROOT"]
    ...     _self_repair_execution_terminal.__globals__["QUEUE_ROOT"] = root / "queue"
    ...     for state in STATES:
    ...         (root / "queue" / state).mkdir(parents=True, exist_ok=True)
    ...     write_json_atomic(root / "queue" / "done" / "task-a.json", {"task_id": "task-a", "review_verdict": "approve"})
    ...     write_json_atomic(root / "queue" / "done" / "task-b.json", {"task_id": "task-b", "review_verdict": "request_change"})
    ...     write_json_atomic(root / "queue" / "failed" / "task-c.json", {"task_id": "task-c"})
    ...     write_json_atomic(root / "queue" / "running" / "task-d.json", {"task_id": "task-d"})
    ...     out = (
    ...         _self_repair_execution_terminal("task-a"),
    ...         _self_repair_execution_terminal("task-b"),
    ...         _self_repair_execution_terminal("task-c"),
    ...         _self_repair_execution_terminal("task-d"),
    ...         _self_repair_execution_terminal("missing"),
    ...     )
    ...     _self_repair_execution_terminal.__globals__["QUEUE_ROOT"] = old
    >>> out
    ('done_approved', 'done_other', 'failed', 'in_flight', 'failed')
    """
    found = find_task(task_id)
    if not found:
        return "failed"
    state, task = found
    if state in ("failed", "abandoned"):
        return "failed"
    if state == "done":
        return "done_approved" if (task or {}).get("review_verdict") == "approve" else "done_other"
    return "in_flight"


def tick_self_repair_resolution():
    """Advance executing self-repair issues once their slices terminate.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     feats = root / "features"; feats.mkdir()
    ...     queue_root = root / "queue"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     old = {k: tick_self_repair_resolution.__globals__[k] for k in ("FEATURES_DIR", "QUEUE_ROOT", "append_event", "append_transition")}
    ...     events = []
    ...     transitions = []
    ...     tick_self_repair_resolution.__globals__["FEATURES_DIR"] = feats
    ...     tick_self_repair_resolution.__globals__["QUEUE_ROOT"] = queue_root
    ...     tick_self_repair_resolution.__globals__["append_event"] = lambda *args, **kwargs: events.append((args, kwargs))
    ...     tick_self_repair_resolution.__globals__["append_transition"] = lambda *args: transitions.append(args)
    ...     write_json_atomic(feats / "feature-sr.json", {"feature_id": "feature-sr", "status": "open", "self_repair": {"enabled": True, "issues": [{"issue_key": "i1", "status": "executing", "execution_task_ids": ["task-a"]}]}})
    ...     write_json_atomic(queue_root / "done" / "task-a.json", {"task_id": "task-a", "review_verdict": "approve"})
    ...     out = tick_self_repair_resolution()
    ...     saved = read_json(feats / "feature-sr.json", {})
    ...     for key, value in old.items():
    ...         tick_self_repair_resolution.__globals__[key] = value
    >>> issue = saved["self_repair"]["issues"][0]
    >>> out["resolved"], issue["status"], issue["execution_task_ids"], issue["completed_execution_task_ids"]
    (1, 'resolved', [], ['task-a'])
    >>> transitions[0][1:4]
    ('issue:executing', 'issue:resolved', 'i1')
    """
    resolved = 0
    stalled = 0
    changed_features = []
    cfg = load_config()
    for feature in list_features():
        if feature.get("status") not in ("open", "finalizing"):
            continue
        if is_self_repair_feature(feature):
            feature, normalized = _normalize_self_repair_feature_issues(feature["feature_id"], cfg=cfg)
            if normalized and feature["feature_id"] not in changed_features:
                changed_features.append(feature["feature_id"])
        issues = ((feature.get("self_repair") or {}).get("issues") or [])
        mutated = False
        for issue in issues:
            if issue.get("status") != "executing":
                continue
            exec_ids = list(issue.get("execution_task_ids") or [])
            if not exec_ids:
                continue
            states = [_self_repair_execution_terminal(task_id) for task_id in exec_ids]
            if any(state in ("failed", "done_other") for state in states):
                issue["status"] = "stalled"
                issue["stalled_at"] = now_iso()
                issue["stalled_reason"] = "execution slice failed or completed without approval"
                issue["last_seen_at"] = now_iso()
                issue["attempts"] = int(issue.get("attempts") or 0) + 1
                issue["max_attempts"] = int(issue.get("max_attempts") or self_repair_issue_max_attempts())
                stalled += 1
                mutated = True
                append_transition(feature["feature_id"], "issue:executing", "issue:stalled", issue.get("issue_key") or "")
                append_event(
                    "self-repair-resolution",
                    "issue_stalled",
                    feature_id=feature["feature_id"],
                    details={"issue_key": issue.get("issue_key"), "execution_task_ids": exec_ids, "states": states},
                )
            elif all(state == "done_approved" for state in states):
                observation_target = _self_repair_issue_observation_target(issue)
                observation_minutes = int((cfg.get("self_repair") or {}).get("observation_window_minutes", 10) or 10)
                observation_due_at = (
                    (dt.datetime.now() + dt.timedelta(minutes=max(observation_minutes, 1))).isoformat(timespec="seconds")
                    if observation_target else None
                )
                issue["status"] = "resolved"
                issue["resolved_at"] = now_iso()
                issue["resolution"] = "execution_approved"
                issue["completed_execution_task_ids"] = exec_ids
                issue["execution_task_ids"] = []
                issue["last_seen_at"] = now_iso()
                issue["observation_target"] = observation_target
                issue["observation_due_at"] = observation_due_at
                issue["observation_checked_at"] = None
                issue["observation_status"] = "pending" if observation_target else "not_applicable"
                resolved += 1
                mutated = True
                append_transition(feature["feature_id"], "issue:executing", "issue:resolved", issue.get("issue_key") or "")
                append_event(
                    "self-repair-resolution",
                    "issue_resolved",
                    feature_id=feature["feature_id"],
                    details={"issue_key": issue.get("issue_key"), "execution_task_ids": exec_ids, "observation_due_at": observation_due_at},
                )
        if mutated:
            _write_feature_record(feature)
            changed_features.append(feature["feature_id"])
    return {"resolved": resolved, "stalled": stalled, "features": changed_features}


def tick_self_repair_observation_window():
    """Reopen resolved issues when the original blocker reappears after the watch window.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     feats = root / "features"; feats.mkdir()
    ...     queue_root = root / "queue"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     old = {k: tick_self_repair_observation_window.__globals__[k] for k in ("FEATURES_DIR", "QUEUE_ROOT", "append_event", "append_transition")}
    ...     events = []
    ...     transitions = []
    ...     tick_self_repair_observation_window.__globals__["FEATURES_DIR"] = feats
    ...     tick_self_repair_observation_window.__globals__["QUEUE_ROOT"] = queue_root
    ...     tick_self_repair_observation_window.__globals__["append_event"] = lambda *args, **kwargs: events.append((args, kwargs))
    ...     tick_self_repair_observation_window.__globals__["append_transition"] = lambda *args: transitions.append(args)
    ...     write_json_atomic(feats / "feature-sr.json", {"feature_id": "feature-sr", "status": "open", "self_repair": {"enabled": True, "issues": [{"issue_key": "task:task-x:blocked:template_refine_exhausted", "status": "resolved", "observation_target": {"task_id": "task-x", "blocker_code": "template_refine_exhausted"}, "observation_due_at": "2000-01-01T00:00:00"}]}})
    ...     write_json_atomic(queue_root / "blocked" / "task-x.json", {"task_id": "task-x", "blocker": {"code": "template_refine_exhausted"}})
    ...     out = tick_self_repair_observation_window()
    ...     saved = read_json(feats / "feature-sr.json", {})
    ...     for key, value in old.items():
    ...         tick_self_repair_observation_window.__globals__[key] = value
    >>> out["reopened"], saved["self_repair"]["issues"][0]["status"], transitions[0][1:4]
    (1, 'pending', ('issue:resolved', 'issue:pending', 'task:task-x:blocked:template_refine_exhausted'))
    """
    reopened = 0
    checked = 0
    changed_features = []
    now = dt.datetime.now()
    for feature in list_features():
        if feature.get("status") not in ("open", "finalizing"):
            continue
        if not is_self_repair_feature(feature):
            continue
        issues = ((feature.get("self_repair") or {}).get("issues") or [])
        mutated = False
        for issue in issues:
            if issue.get("status") != "resolved":
                continue
            target = issue.get("observation_target") or {}
            due_at_raw = issue.get("observation_due_at")
            if not target or not due_at_raw or issue.get("observation_checked_at"):
                continue
            try:
                due_at = dt.datetime.fromisoformat(str(due_at_raw))
            except ValueError:
                due_at = now
            if due_at.tzinfo is not None:
                due_at = due_at.astimezone().replace(tzinfo=None)
            if due_at > now:
                continue
            checked += 1
            issue["observation_checked_at"] = now_iso()
            found = find_task(target.get("task_id"))
            blocker = task_blocker(found[1]) if found else {}
            same_blocker = found and (blocker or {}).get("code") == target.get("blocker_code")
            if same_blocker:
                issue["status"] = "pending"
                issue["reopened_at"] = now_iso()
                issue["last_seen_at"] = now_iso()
                issue["observation_status"] = "did_not_fix"
                issue["last_blocker_code"] = "resolution_did_not_fix"
                issue["last_reopen_reason"] = (
                    f"resolved self-repair did not fix {target.get('task_id')}:{target.get('blocker_code')}"
                )
                issue["planner_task_id"] = None
                issue["resolution"] = "reopened_after_observation"
                append_transition(feature["feature_id"], "issue:resolved", "issue:pending", issue.get("issue_key") or "")
                append_event(
                    "self-repair-observation",
                    "issue_reopened",
                    feature_id=feature["feature_id"],
                    task_id=target.get("task_id"),
                    details={"issue_key": issue.get("issue_key"), "blocker_code": target.get("blocker_code")},
                )
                reopened += 1
                mutated = True
            else:
                issue["observation_status"] = "passed"
                append_event(
                    "self-repair-observation",
                    "issue_verified",
                    feature_id=feature["feature_id"],
                    task_id=target.get("task_id"),
                    details={"issue_key": issue.get("issue_key"), "blocker_code": target.get("blocker_code")},
                )
                mutated = True
        if mutated:
            _write_feature_record(feature)
            changed_features.append(feature["feature_id"])
    return {"checked": checked, "reopened": reopened, "features": changed_features}


def _self_repair_append_deliberation(feature_id, issue_key, deliberation):
    def mut(feature):
        sr = feature.setdefault("self_repair", {})
        issues = sr.setdefault("issues", [])
        for issue in issues:
            if issue.get("issue_key") != issue_key:
                continue
            rows = issue.setdefault("deliberations", [])
            rows.append(dict(deliberation or {}))
            issue["last_deliberation_at"] = deliberation.get("ts") or now_iso()
            issue["last_deliberation_stage"] = deliberation.get("stage")
            break
    return update_feature(feature_id, mut)


def self_repair_record_deliberation(
    feature_id,
    issue_key,
    *,
    stage,
    verdict,
    panel=(),
    chosen_strategy="",
    rejected_strategies=(),
    dissent_reasons=(),
    confidence=None,
    retry_conditions=(),
    escalation_threshold="",
    reason="",
    task_id=None,
):
    """Persist machine-usable council state for a self-repair issue.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     feats = root / "features"; feats.mkdir()
    ...     old = self_repair_record_deliberation.__globals__["FEATURES_DIR"]
    ...     self_repair_record_deliberation.__globals__["FEATURES_DIR"] = feats
    ...     write_json_atomic(feats / "feature-sr.json", {"feature_id": "feature-sr", "self_repair": {"enabled": True, "issues": [{"issue_key": "i1"}]}})
    ...     out = self_repair_record_deliberation("feature-sr", "i1", stage="verifier", verdict="replan", panel=("a",), chosen_strategy="retry", confidence=0.7)
    ...     self_repair_record_deliberation.__globals__["FEATURES_DIR"] = old
    >>> issue = out["self_repair"]["issues"][0]
    >>> issue["last_deliberation_stage"], issue["deliberations"][0]["chosen_strategy"], issue["deliberations"][0]["confidence"]
    ('verifier', 'retry', 0.7)
    """
    ts = now_iso()
    payload = {
        "ts": ts,
        "stage": stage,
        "verdict": verdict,
        "panel": list(panel or ()),
        "chosen_strategy": chosen_strategy or "",
        "rejected_strategies": list(rejected_strategies or ()),
        "dissent_reasons": list(dissent_reasons or ()),
        "confidence": confidence,
        "retry_conditions": list(retry_conditions or ()),
        "escalation_threshold": escalation_threshold or "",
        "reason": reason or "",
        "task_id": task_id,
    }
    return _self_repair_append_deliberation(feature_id, issue_key, payload)


def self_repair_reopen_issue(
    feature_id,
    issue_key,
    *,
    summary="",
    evidence="",
    blocker_code="",
    reason="",
    deliberation=None,
):
    """Reopen a self-repair issue for another council-guided planning round.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     feats = root / "features"; feats.mkdir()
    ...     old = {
    ...         "FEATURES_DIR": self_repair_reopen_issue.__globals__["FEATURES_DIR"],
    ...         "append_event": self_repair_reopen_issue.__globals__["append_event"],
    ...     }
    ...     events = []
    ...     self_repair_reopen_issue.__globals__["FEATURES_DIR"] = feats
    ...     self_repair_reopen_issue.__globals__["append_event"] = lambda *args, **kwargs: events.append((args, kwargs))
    ...     write_json_atomic(feats / "feature-sr.json", {"feature_id": "feature-sr", "self_repair": {"enabled": True, "issues": [{"issue_key": "i1", "status": "scheduled", "planner_task_id": "task-old"}]}})
    ...     out = self_repair_reopen_issue("feature-sr", "i1", blocker_code="false_blocker_claim", reason="needs replan")
    ...     for key, value in old.items():
    ...         self_repair_reopen_issue.__globals__[key] = value
    >>> issue = out["self_repair"]["issues"][0]
    >>> issue["status"], issue["planner_task_id"] is None, issue["reopen_count"]
    ('pending', True, 1)
    >>> issue["execution_task_ids"], issue["superseded_task_ids"]
    ([], ['task-old'])
    """
    reopened_at = now_iso()

    def mut(feature):
        sr = feature.setdefault("self_repair", {})
        sr["replan_requested_at"] = reopened_at
        sr["last_reopened_issue_key"] = issue_key
        issues = sr.setdefault("issues", [])
        for issue in issues:
            if issue.get("issue_key") != issue_key:
                continue
            prior_task_id = issue.get("planner_task_id")
            issue["status"] = "pending"
            issue["planner_task_id"] = None
            issue["reopened_at"] = reopened_at
            issue["last_seen_at"] = reopened_at
            issue["reopen_count"] = int(issue.get("reopen_count", 0) or 0) + 1
            superseded = list(issue.get("superseded_task_ids") or [])
            if prior_task_id and prior_task_id not in superseded:
                superseded.append(prior_task_id)
            for task_id in (issue.get("execution_task_ids") or []):
                if task_id and task_id not in superseded:
                    superseded.append(task_id)
            issue["superseded_task_ids"] = superseded
            issue["execution_task_ids"] = []
            if summary:
                issue["summary"] = summary[:240]
            if evidence:
                issue["evidence"] = evidence
            issue["last_blocker_code"] = blocker_code or issue.get("last_blocker_code")
            issue["last_reopen_reason"] = reason or issue.get("last_reopen_reason")
            break

    updated = update_feature(feature_id, mut)
    if deliberation:
        updated = self_repair_record_deliberation(
            feature_id,
            issue_key,
            stage=deliberation.get("stage") or "reopen",
            verdict=deliberation.get("verdict") or "replan",
            panel=deliberation.get("panel") or (),
            chosen_strategy=deliberation.get("chosen_strategy") or "",
            rejected_strategies=deliberation.get("rejected_strategies") or (),
            dissent_reasons=deliberation.get("dissent_reasons") or (),
            confidence=deliberation.get("confidence"),
            retry_conditions=deliberation.get("retry_conditions") or (),
            escalation_threshold=deliberation.get("escalation_threshold") or "",
            reason=deliberation.get("reason") or reason,
            task_id=deliberation.get("task_id"),
        )
    append_event(
        "self-repair",
        "issue_reopened",
        feature_id=feature_id,
        details={"issue_key": issue_key, "blocker_code": blocker_code, "reason": reason},
    )
    return updated


def _enqueue_self_repair_issue_task(feature, issue):
    feature_id = feature["feature_id"]
    repair_cfg = (feature.get("self_repair") or {}).copy()
    prior_task_id = issue.get("planner_task_id")
    superseded_task_ids = list(issue.get("superseded_task_ids") or [])
    if prior_task_id and prior_task_id not in superseded_task_ids:
        superseded_task_ids.append(prior_task_id)
    task = new_task(
        role="planner",
        engine="claude",
        project=feature["project"],
        summary=f"Council-plan self-repair: {(issue.get('summary') or feature.get('summary') or '')[:210]}",
        source=f"self-repair:{issue.get('source') or 'workflow-check'}",
        braid_template="orchestrator-self-repair",
        feature_id=feature_id,
        engine_args={
            "mode": "self-repair-plan",
            "self_repair": {
                "enabled": True,
                "issue_kind": issue.get("issue_kind") or repair_cfg.get("issue_kind") or "runtime_bug",
                "source": issue.get("source") or repair_cfg.get("source") or "manual",
                "deploy_mode": repair_cfg.get("deploy_mode", "local-main"),
                "council_members": list(repair_cfg.get("council_members") or ()),
                "restart_launch_agents": bool(repair_cfg.get("restart_launch_agents", True)),
            },
            "evidence": issue.get("evidence") or "",
            "issue_key": issue.get("issue_key"),
            "issue_summary": issue.get("summary") or "",
        },
    )
    enqueue_task(task)
    append_feature_child(feature_id, task["task_id"])
    _self_repair_mark_issue(
        feature_id,
        issue["issue_key"],
        planner_task_id=task["task_id"],
        status="scheduled",
        scheduled_at=now_iso(),
        execution_task_ids=[],
        superseded_task_ids=superseded_task_ids,
    )
    update_feature(
        feature_id,
        lambda f: f.setdefault("self_repair", {}).update({"material_update_pending": False}),
    )
    append_event(
        "self-repair",
        "issue_task_enqueued",
        task_id=task["task_id"],
        feature_id=feature_id,
        details={"issue_key": issue.get("issue_key"), "summary": issue.get("summary")},
    )
    return task


def _attach_issue_to_self_repair_feature(feature, *, summary, evidence, issue_kind, source, issue_key=None):
    attached_at = now_iso()
    resolved_issue_key = _self_repair_issue_key(
        issue_key=issue_key,
        issue_kind=issue_kind,
        summary=summary,
        evidence=evidence,
        source=source,
    )
    record = {
        "issue_key": resolved_issue_key,
        "issue_kind": issue_kind,
        "summary": (summary or "").strip()[:240],
        "evidence": (evidence or "").strip(),
        "source": source,
        "status": "pending",
        "attached_at": attached_at,
        "last_seen_at": attached_at,
        "seen_count": 1,
        "attempts": 0,
        "max_attempts": self_repair_issue_max_attempts(),
        "planner_task_id": None,
    }

    def mut(f):
        sr = f.setdefault("self_repair", {})
        issues = sr.setdefault("issues", [])
        attached_new = True
        for issue in issues:
            if issue.get("issue_key") == resolved_issue_key:
                issue["last_seen_at"] = attached_at
                issue["seen_count"] = int(issue.get("seen_count", 0) or 0) + 1
                if record["evidence"] and record["evidence"] != issue.get("evidence"):
                    issue["evidence"] = record["evidence"]
                if record["summary"] and record["summary"] != issue.get("summary"):
                    issue["summary"] = record["summary"]
                issue["source"] = source
                issue["issue_kind"] = issue_kind
                issue["attempts"] = int(issue.get("attempts") or 0)
                issue["max_attempts"] = int(issue.get("max_attempts") or self_repair_issue_max_attempts())
                attached_new = False
                break
        else:
            issues.append(record.copy())
        sr["last_attached_issue_key"] = resolved_issue_key
        sr["last_attached_at"] = attached_at
        if attached_new:
            sr["replan_requested_at"] = attached_at
            sr["material_update_pending"] = True

    updated = update_feature(feature["feature_id"], mut)
    attached_issue = next(
        issue for issue in ((updated.get("self_repair") or {}).get("issues") or [])
        if issue.get("issue_key") == resolved_issue_key
    )
    append_event(
        "self-repair",
        "issue_attached",
        feature_id=feature["feature_id"],
        task_id=attached_issue.get("planner_task_id"),
        details={"issue_key": resolved_issue_key, "summary": attached_issue.get("summary")},
    )
    return updated, attached_issue


def tick_self_repair_queue():
    """Schedule the oldest pending issue onto the active self-repair feature.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     feats = root / "features"; feats.mkdir()
    ...     queue_root = root / "queue"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     feature = {
    ...         "feature_id": "feature-sr",
    ...         "project": "devmini-orchestrator",
    ...         "status": "open",
    ...         "summary": "repair",
    ...         "source": "self-repair:test",
    ...         "child_task_ids": [],
    ...         "self_repair": {"enabled": True, "issues": [{"issue_key": "i1", "summary": "fix queue", "evidence": "e", "source": "workflow-check", "issue_kind": "runtime_bug", "status": "pending", "planner_task_id": "task-old"}]},
    ...     }
    ...     old = {k: tick_self_repair_queue.__globals__[k] for k in ("FEATURES_DIR", "QUEUE_ROOT", "new_task", "enqueue_task", "append_feature_child")}
    ...     captured = {}
    ...     tick_self_repair_queue.__globals__["FEATURES_DIR"] = feats
    ...     tick_self_repair_queue.__globals__["QUEUE_ROOT"] = queue_root
    ...     tick_self_repair_queue.__globals__["new_task"] = lambda **kwargs: {"task_id": "task-sr", **kwargs}
    ...     tick_self_repair_queue.__globals__["enqueue_task"] = lambda task: captured.setdefault("task", task)
    ...     tick_self_repair_queue.__globals__["append_feature_child"] = lambda fid, tid: update_feature(fid, lambda f: f.setdefault("child_task_ids", []).append(tid))
    ...     write_json_atomic(feats / "feature-sr.json", feature)
    ...     out = tick_self_repair_queue()
    ...     saved = read_json(feats / "feature-sr.json", {})
    ...     for key, value in old.items():
    ...         tick_self_repair_queue.__globals__[key] = value
    >>> out["scheduled"], captured["task"]["task_id"], saved["self_repair"]["issues"][0]["planner_task_id"]
    (1, 'task-sr', 'task-sr')
    >>> saved["self_repair"]["issues"][0]["superseded_task_ids"]
    ['task-old']
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     feats = root / "features"; feats.mkdir()
    ...     queue_root = root / "queue"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     feature = {
    ...         "feature_id": "feature-sr",
    ...         "project": "devmini-orchestrator",
    ...         "status": "open",
    ...         "summary": "repair",
    ...         "source": "self-repair:test",
    ...         "child_task_ids": [],
    ...         "self_repair": {"enabled": True, "issues": [{"issue_key": "i1", "summary": "fix queue", "evidence": "e", "source": "workflow-check", "issue_kind": "runtime_bug", "status": "stalled", "attempts": 3, "max_attempts": 3, "planner_task_id": None}]},
    ...     }
    ...     old = {k: tick_self_repair_queue.__globals__[k] for k in ("FEATURES_DIR", "QUEUE_ROOT", "new_task", "enqueue_task", "append_feature_child", "_write_workflow_alert")}
    ...     alerts = []
    ...     tick_self_repair_queue.__globals__["FEATURES_DIR"] = feats
    ...     tick_self_repair_queue.__globals__["QUEUE_ROOT"] = queue_root
    ...     tick_self_repair_queue.__globals__["new_task"] = lambda **kwargs: {"task_id": "task-sr", **kwargs}
    ...     tick_self_repair_queue.__globals__["enqueue_task"] = lambda task: alerts.append({"unexpected_task": task["task_id"]})
    ...     tick_self_repair_queue.__globals__["append_feature_child"] = lambda fid, tid: None
    ...     tick_self_repair_queue.__globals__["_write_workflow_alert"] = lambda issue, reason: alerts.append({"issue_key": issue["issue_key"], "reason": reason}) or "alert.md"
    ...     write_json_atomic(feats / "feature-sr.json", feature)
    ...     out = tick_self_repair_queue()
    ...     saved = read_json(feats / "feature-sr.json", {})
    ...     for key, value in old.items():
    ...         tick_self_repair_queue.__globals__[key] = value
    >>> out["scheduled"], out["reason"], saved["self_repair"]["issues"][0]["status"], alerts[0]["issue_key"]
    (0, 'issue_escalated', 'escalated', 'i1')
    """
    scheduled = []
    lock_fh = acquire_lock("self-repair.lock", mode="exclusive", timeout_sec=10)
    try:
        cfg = load_config()
        active = _active_self_repair_feature()
        if not active or active.get("status") != "open":
            return {"scheduled": 0, "feature_id": (active or {}).get("feature_id")}
        active, _ = _normalize_self_repair_feature_issues(active["feature_id"], cfg=cfg)
        if _self_repair_has_active_work(active):
            return {"scheduled": 0, "feature_id": active["feature_id"], "reason": "feature_busy"}
        pending = [issue for issue in _self_repair_pending_issues(active) if not _self_repair_issue_live(issue)]
        if not pending:
            return {"scheduled": 0, "feature_id": active["feature_id"], "reason": "no_pending_issues"}
        pending.sort(key=lambda issue: (issue.get("attached_at") or "", issue.get("issue_key") or ""))
        escalated = 0
        for issue in pending:
            attempts = int(issue.get("attempts") or 0)
            max_attempts = int(issue.get("max_attempts") or self_repair_issue_max_attempts(cfg=cfg))
            if attempts >= max_attempts:
                _self_repair_mark_issue(
                    active["feature_id"],
                    issue["issue_key"],
                    status="escalated",
                    escalated_at=now_iso(),
                    escalated_reason="self-repair replan budget exhausted",
                    last_seen_at=now_iso(),
                    max_attempts=max_attempts,
                )
                _write_workflow_alert(
                    {
                        "feature_id": active["feature_id"],
                        "project": active.get("project"),
                        "summary": issue.get("summary") or active.get("summary") or issue.get("issue_key"),
                        "issue_key": issue.get("issue_key"),
                        "kind": "frontier_task_blocked",
                        "task_id": issue.get("planner_task_id"),
                        "task_state": issue.get("status"),
                        "blocker": make_blocker(
                            "attempt_exhausted",
                            summary="self-repair issue replan budget exhausted",
                            detail=f"issue attempts {attempts} >= max_attempts {max_attempts}",
                            source="workflow-check",
                            retryable=False,
                        ),
                    },
                    f"self-repair issue replan budget exhausted ({attempts}/{max_attempts})",
                )
                escalated += 1
                continue
            task = _enqueue_self_repair_issue_task(active, issue)
            scheduled.append(task["task_id"])
            return {"scheduled": len(scheduled), "feature_id": active["feature_id"], "task_ids": scheduled, "escalated": escalated}
        if escalated:
            return {"scheduled": 0, "feature_id": active["feature_id"], "reason": "issue_escalated", "escalated": escalated}
        return {"scheduled": 0, "feature_id": active["feature_id"], "reason": "no_schedulable_issues"}
    finally:
        lock_fh.close()


def _sync_operator_worktree_local_config(worktree_path):
    wt_root = pathlib.Path(worktree_path)
    for rel in GITIGNORED_OPERATOR_CONFIGS:
        src = STATE_ROOT / rel
        if not src.exists():
            canonical = canonical_orchestrator_root() / rel
            if canonical.exists():
                src = canonical
        dst = wt_root / rel
        if not src.exists() or dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            dst.symlink_to(src)
        except OSError:
            shutil.copy2(src, dst)
def reserve_orchestrator_operator_worktree(name, *, allow_with_self_repair=False):
    """Create a dedicated manual worktree for operator changes on this repo.

    Refuses when a self-repair feature is already open unless explicitly
    overridden. This keeps prompted/manual edits out of a control-plane surface
    already owned by autonomous repair.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    if not slug:
        raise ValueError("worktree name must contain at least one alphanumeric character")

    repo = STATE_ROOT
    if repo_status(repo).get("dirty"):
        raise RuntimeError("canonical orchestrator checkout is dirty; commit or stash before reserving an operator worktree")

    active_self_repairs = list_self_repair_features(
        project_name="devmini-orchestrator",
        statuses=("open", "finalizing"),
    )
    if active_self_repairs and not allow_with_self_repair:
        owner = active_self_repairs[0]
        raise RuntimeError(
            "active self-repair already owns the orchestrator control plane: "
            f"{owner.get('feature_id')} {owner.get('summary') or ''}".strip()
        )

    branch = f"operator/{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug}"
    wt_root = DEV_ROOT / "worktrees" / "devmini-orchestrator-ops"
    wt_root.mkdir(parents=True, exist_ok=True)
    wt_path = wt_root / branch.replace("/", "-")
    proc = subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(wt_path), "main"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "git worktree add failed").strip()[:300])
    _sync_operator_worktree_local_config(wt_path)
    return {"branch": branch, "worktree": str(wt_path)}


def update_feature(feature_id, mutator):
    """Load feature, apply mutator, write atomically. Returns the updated dict."""
    feature = read_feature(feature_id)
    if feature is None:
        raise FileNotFoundError(f"no feature {feature_id}")
    mutator(feature)
    _write_feature_record(feature)
    return feature


def append_feature_child(feature_id, child_task_id):
    def mut(f):
        kids = f.setdefault("child_task_ids", [])
        if child_task_id not in kids:
            kids.append(child_task_id)
    return update_feature(feature_id, mut)


def in_flight_feature_ids():
    """Return the set of feature_ids currently held by claimed or running tasks.

    Used by atomic_claim to serialize sibling slices on the same feature
    branch. A feature is "in flight" if any of its children is past the
    queued state and before done/failed/awaiting-*, i.e. actively being
    executed by a codex worker.
    """
    fids = set()
    for t in iter_tasks(states=("claimed", "running")):
        fid = t.get("feature_id") if t else None
        if fid:
            fids.add(fid)
    return fids


def acquire_lock(lock_name, mode, timeout_sec=0):
    """Acquire an advisory lock from state/runtime/locks.

    Returns an open file handle whose lifetime holds the lock.
    """
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCKS_DIR / lock_name
    fh = open(lock_path, "a+")
    flag = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
    deadline = time.monotonic() + max(timeout_sec, 0)
    while True:
        try:
            fcntl.flock(fh.fileno(), flag | fcntl.LOCK_NB)
            return fh
        except BlockingIOError:
            if time.monotonic() >= deadline:
                fh.close()
                raise TimeoutError(f"could not acquire {mode} lock {lock_name}")
            time.sleep(1.0)


def parse_roadmap_next_todo(project_path, skip_ids=None):
    """Return the first TODO roadmap entry for a project, or None.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     (root / "repo-memory").mkdir()
    ...     _ = (root / "repo-memory" / "ROADMAP.md").write_text(
    ...         "## Active\\n\\n"
    ...         "### [R-001] First item\\n"
    ...         "- **Status:** TODO\\n"
    ...         "- **Goal:** Ship it.\\n\\n"
    ...         "### [R-002] Second item\\n"
    ...         "- **Status:** DONE\\n"
    ...     )
    ...     entry = parse_roadmap_next_todo(root)
    ...     (entry["id"], entry["title"])
    ('R-001', 'First item')

    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     (root / "repo-memory").mkdir()
    ...     _ = (root / "repo-memory" / "ROADMAP.md").write_text(
    ...         "## Active\\n\\n"
    ...         "### [R-001] Busy item\\n"
    ...         "- **Status:** IN_PROGRESS\\n\\n"
    ...         "### [R-002] Ready item\\n"
    ...         "- **Status:** TODO\\n"
    ...         "- **Acceptance:** Works.\\n"
    ...     )
    ...     parse_roadmap_next_todo(root)["id"]
    'R-002'

    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     parse_roadmap_next_todo(pathlib.Path(tmp)) is None
    True

    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     (root / "repo-memory").mkdir()
    ...     _ = (root / "repo-memory" / "ROADMAP.md").write_text(
    ...         "### [R-001] Done item\\n"
    ...         "- **Status:** DONE\\n\\n"
    ...         "### [R-002] Blocked item\\n"
    ...         "- **Status:** BLOCKED\\n"
    ...     )
    ...     parse_roadmap_next_todo(root) is None
    True
    """
    roadmap_path = pathlib.Path(project_path) / "repo-memory" / "ROADMAP.md"
    if not roadmap_path.exists():
        return None

    text = roadmap_path.read_text()
    entry_re = re.compile(r"^### \[(?P<id>R-\d+)\] (?P<title>.+?)\s*$", re.MULTILINE)
    boundary_re = re.compile(r"^## |^### \[R-\d+\] ", re.MULTILINE)
    status_re = re.compile(r"^- \*\*Status:\*\* (?P<status>[A-Z_]+)\s*$", re.MULTILINE)

    skipped = set(skip_ids or ())
    for match in entry_re.finditer(text):
        boundary = boundary_re.search(text, match.end())
        end = boundary.start() if boundary else len(text)
        body = text[match.start():end].strip()
        status = status_re.search(body)
        if not status or status.group("status") != "TODO":
            continue
        if match.group("id") in skipped:
            continue
        return {
            "id": match.group("id"),
            "title": match.group("title").strip(),
            "body": body,
        }
    return None


def assigned_roadmap_entries(project):
    """Return roadmap entry ids assigned to non-terminal features for a project.

    Features created before roadmap wiring may not carry `roadmap_entry_id`.
    Those legacy records are ignored rather than masking a real roadmap entry.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = assigned_roadmap_entries.__globals__["FEATURES_DIR"]
    ...     assigned_roadmap_entries.__globals__["FEATURES_DIR"] = pathlib.Path(tmp)
    ...     assigned_roadmap_entries("demo")
    ...     assigned_roadmap_entries.__globals__["FEATURES_DIR"] = old
    set()

    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = assigned_roadmap_entries.__globals__["FEATURES_DIR"]
    ...     assigned_roadmap_entries.__globals__["FEATURES_DIR"] = pathlib.Path(tmp)
    ...     write_json_atomic(assigned_roadmap_entries.__globals__["FEATURES_DIR"] / "legacy.json", {
    ...         "feature_id": "feature-1",
    ...         "project": "demo",
    ...         "status": "open",
    ...     })
    ...     assigned_roadmap_entries("demo")
    ...     assigned_roadmap_entries.__globals__["FEATURES_DIR"] = old
    set()

    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = assigned_roadmap_entries.__globals__["FEATURES_DIR"]
    ...     assigned_roadmap_entries.__globals__["FEATURES_DIR"] = pathlib.Path(tmp)
    ...     write_json_atomic(assigned_roadmap_entries.__globals__["FEATURES_DIR"] / "merged.json", {
    ...         "feature_id": "feature-1",
    ...         "project": "demo",
    ...         "status": "merged",
    ...         "roadmap_entry_id": "R-001",
    ...     })
    ...     assigned_roadmap_entries("demo")
    ...     assigned_roadmap_entries.__globals__["FEATURES_DIR"] = old
    set()

    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = assigned_roadmap_entries.__globals__["FEATURES_DIR"]
    ...     assigned_roadmap_entries.__globals__["FEATURES_DIR"] = pathlib.Path(tmp)
    ...     write_json_atomic(assigned_roadmap_entries.__globals__["FEATURES_DIR"] / "abandoned.json", {
    ...         "feature_id": "feature-1",
    ...         "project": "demo",
    ...         "status": "abandoned",
    ...         "roadmap_entry_id": "R-001",
    ...     })
    ...     assigned_roadmap_entries("demo")
    ...     assigned_roadmap_entries.__globals__["FEATURES_DIR"] = old
    set()

    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = assigned_roadmap_entries.__globals__["FEATURES_DIR"]
    ...     assigned_roadmap_entries.__globals__["FEATURES_DIR"] = pathlib.Path(tmp)
    ...     write_json_atomic(assigned_roadmap_entries.__globals__["FEATURES_DIR"] / "one.json", {
    ...         "feature_id": "feature-1",
    ...         "project": "demo",
    ...         "status": "open",
    ...         "roadmap_entry_id": "R-001",
    ...     })
    ...     write_json_atomic(assigned_roadmap_entries.__globals__["FEATURES_DIR"] / "two.json", {
    ...         "feature_id": "feature-2",
    ...         "project": "demo",
    ...         "status": "finalizing",
    ...         "roadmap_entry_id": "R-002",
    ...     })
    ...     print(sorted(assigned_roadmap_entries("demo")))
    ...     assigned_roadmap_entries.__globals__["FEATURES_DIR"] = old
    ['R-001', 'R-002']
    """
    assigned = set()
    for feature in list_features():
        if feature.get("project") != project:
            continue
        if feature.get("status") in TERMINAL_FEATURE_STATES:
            continue
        roadmap_entry_id = feature.get("roadmap_entry_id")
        if roadmap_entry_id:
            assigned.add(roadmap_entry_id)
    return assigned


# --- Agent status ------------------------------------------------------------

def write_agent_status(role, status, detail=""):
    write_json_atomic(
        AGENT_STATE_DIR / f"{role}.json",
        {"role": role, "status": status, "detail": detail, "updated_at": now_iso()},
    )
    append_event(role, "agent_status", details={"status": status, "detail": detail})
    append_metric(
        "agent.status",
        1,
        tags={"role": role, "status": status},
        source="agent-status",
    )


def agent_statuses():
    AGENT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return [read_json(p, {}) for p in sorted(AGENT_STATE_DIR.glob("*.json"))]


def _matching_tasks(*, states, engine=None, role=None, source=None):
    tasks = []
    for task in iter_tasks(states=states):
        if not task:
            continue
        if engine and task.get("engine") != engine:
            continue
        if role and task.get("role") != role:
            continue
        if source and task.get("source") != source:
            continue
        tasks.append((task.get("state"), task))
    return tasks


def _format_status_task_detail(tasks):
    if not tasks:
        return ""
    task_ids = [task.get("task_id") or "-" for _, task in tasks]
    if len(task_ids) == 1:
        return task_ids[0]
    return f"{task_ids[0]} (+{len(task_ids) - 1} more)"


def effective_agent_statuses():
    """Return operator-facing agent statuses reconciled with live queue state."""
    rows = {row.get("role"): row for row in agent_statuses() if row.get("role")}

    for slot in ("claude", "codex", "qa"):
        role = f"worker-{slot}"
        active = _matching_tasks(states=("claimed", "running"), engine=slot)
        row = dict(rows.get(role) or {"role": role, "updated_at": now_iso()})
        if active:
            row["status"] = "running"
            row["detail"] = _format_status_task_detail(active)
        else:
            row["status"] = "idle"
            row["detail"] = ""
        rows[role] = row

    reviewer_active = _matching_tasks(states=("claimed", "running"), engine="claude", role="reviewer", source="tick-reviewer")
    reviewer_queued = _matching_tasks(states=("queued",), engine="claude", role="reviewer", source="tick-reviewer")
    reviewer_pending = len(iter_tasks(states=("awaiting-review",)))
    row = dict(rows.get("reviewer") or {"role": "reviewer", "updated_at": now_iso()})
    if reviewer_active:
        row["status"] = "running"
        row["detail"] = f"Reviewing {reviewer_pending} pending via {_format_status_task_detail(reviewer_active)}"
    elif reviewer_queued:
        row["status"] = "queued"
        row["detail"] = f"Reviewer queued for {reviewer_pending} pending via {_format_status_task_detail(reviewer_queued)}"
    elif reviewer_pending:
        row["status"] = "idle"
        row["detail"] = f"{reviewer_pending} awaiting-review without queued reviewer"
    else:
        row["status"] = "idle"
        row["detail"] = "No awaiting-review work."
    rows["reviewer"] = row

    qa_active = _matching_tasks(states=("claimed", "running"), engine="qa", role="qa", source="tick-qa")
    qa_queued = _matching_tasks(states=("queued",), engine="qa", role="qa", source="tick-qa")
    qa_pending = len(iter_tasks(states=("awaiting-qa",)))
    row = dict(rows.get("qa") or {"role": "qa", "updated_at": now_iso()})
    if qa_active:
        row["status"] = "running"
        row["detail"] = f"QA on {qa_pending} pending via {_format_status_task_detail(qa_active)}"
    elif qa_queued:
        row["status"] = "queued"
        row["detail"] = f"QA driver queued for {qa_pending} pending via {_format_status_task_detail(qa_queued)}"
    elif qa_pending:
        row["status"] = "idle"
        row["detail"] = f"{qa_pending} awaiting-qa without queued QA driver"
    else:
        row["status"] = "idle"
        row["detail"] = "No awaiting-qa work."
    rows["qa"] = row

    return [rows[key] for key in sorted(rows)]


def planner_disabled(project_name):
    """Return True when planner is runtime-disabled for a project.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old = planner_disabled.__globals__["PLANNER_DISABLED_DIR"]
    ...     planner_disabled.__globals__["PLANNER_DISABLED_DIR"] = pathlib.Path(tmp)
    ...     planner_disabled("demo")
    ...     planner_disabled.__globals__["PLANNER_DISABLED_DIR"] = old
    False
    """
    return (PLANNER_DISABLED_DIR / f"{project_name}.flag").exists()


def set_planner_disabled(project_name, disabled, *, reason=""):
    """Set or clear the runtime planner-disabled flag for a project.

    Return True iff the flag state actually changed.

    >>> import json, pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     old_dir = set_planner_disabled.__globals__["PLANNER_DISABLED_DIR"]
    ...     old_now = set_planner_disabled.__globals__["now_iso"]
    ...     set_planner_disabled.__globals__["PLANNER_DISABLED_DIR"] = pathlib.Path(tmp)
    ...     set_planner_disabled.__globals__["now_iso"] = lambda: "2026-04-14T12:00:00"
    ...     changed1 = set_planner_disabled("demo", True, reason="x")
    ...     body = json.loads((pathlib.Path(tmp) / "demo.flag").read_text())
    ...     changed2 = set_planner_disabled("demo", True)
    ...     changed3 = set_planner_disabled("demo", False)
    ...     changed4 = set_planner_disabled("demo", False)
    ...     out = (changed1, body["reason"], "disabled_at" in body, changed2, changed3, (pathlib.Path(tmp) / "demo.flag").exists(), changed4)
    ...     set_planner_disabled.__globals__["PLANNER_DISABLED_DIR"] = old_dir
    ...     set_planner_disabled.__globals__["now_iso"] = old_now
    >>> out
    (True, 'x', True, False, True, False, False)
    """
    PLANNER_DISABLED_DIR.mkdir(parents=True, exist_ok=True)
    path = PLANNER_DISABLED_DIR / f"{project_name}.flag"
    if disabled:
        if path.exists():
            return False
        write_json_atomic(path, {"disabled_at": now_iso(), "reason": reason})
        return True
    if not path.exists():
        return False
    path.unlink()
    return True


def slot_paused(slot):
    path = SLOT_PAUSE_DIR / f"{slot}.json"
    if not path.exists():
        return None
    return read_json(path, {})


def set_slot_paused(slot, paused, *, reason="", source="runtime"):
    SLOT_PAUSE_DIR.mkdir(parents=True, exist_ok=True)
    path = SLOT_PAUSE_DIR / f"{slot}.json"
    if paused:
        payload = {
            "slot": slot,
            "paused_at": now_iso(),
            "reason": reason,
            "source": source,
        }
        write_json_atomic(path, payload)
        append_event("slot-control", "slot_paused", details=payload)
        return payload
    prior = read_json(path, {}) if path.exists() else None
    path.unlink(missing_ok=True)
    append_event(
        "slot-control",
        "slot_resumed",
        details={"slot": slot, "reason": reason, "source": source, "previous": prior or {}},
    )
    return None


def slot_pause_status():
    rows = {}
    for slot in VALID_ENGINES:
        paused = slot_paused(slot)
        rows[slot] = paused
    return rows


def slot_pause_status_text():
    lines = []
    for slot, paused in slot_pause_status().items():
        if paused:
            lines.append(f"{slot}: paused at {paused.get('paused_at')} ({paused.get('reason') or 'no reason recorded'})")
        else:
            lines.append(f"{slot}: active")
    return "\n".join(lines)


def record_worker_crash(slot, *, task_id=None, detail=""):
    history = read_json(WORKER_CRASH_HISTORY_PATH, {}) or {}
    rows = list(history.get(slot) or [])
    rows.append({"ts": now_iso(), "task_id": task_id, "detail": detail[:240]})
    history[slot] = rows[-20:]
    write_json_atomic(WORKER_CRASH_HISTORY_PATH, history)
    append_event(
        "worker",
        "crash_recorded",
        task_id=task_id,
        details={"slot": slot, "detail": detail[:240]},
    )
    return history[slot]


def crash_loop_guard_status(slot):
    cfg = load_config().get("worker_crash_guard") or {}
    window_seconds = int(cfg.get("window_seconds", CONFIG_DEFAULTS["worker_crash_guard"]["window_seconds"]) or 900)
    max_crashes = int(cfg.get("max_crashes", CONFIG_DEFAULTS["worker_crash_guard"]["max_crashes"]) or 3)
    rows = list((read_json(WORKER_CRASH_HISTORY_PATH, {}) or {}).get(slot) or [])
    now = dt.datetime.now()
    fresh = []
    for row in rows:
        ts = row.get("ts")
        try:
            then = dt.datetime.fromisoformat(ts) if ts else None
        except ValueError:
            then = None
        if then is None:
            continue
        if then.tzinfo is not None:
            then = then.astimezone().replace(tzinfo=None)
        if (now - then).total_seconds() <= window_seconds:
            fresh.append(row)
    return {
        "slot": slot,
        "window_seconds": window_seconds,
        "max_crashes": max_crashes,
        "crashes": fresh,
        "suppressed": len(fresh) >= max_crashes,
    }


# --- Queue inspection --------------------------------------------------------

def queue_counts():
    if _state_engine_read_enabled():
        try:
            db_counts = get_state_engine().queue_state_counts()
            counts = {state: int(db_counts.get(state, 0)) for state in STATES}
            if any(counts.values()) or not QUEUE_ROOT.exists():
                return counts
            _record_state_engine_fs_fallback("queue_counts", "*")
        except Exception:
            _record_state_engine_fs_fallback("queue_counts", "*")
    counts = {}
    for state in STATES:
        counts[state] = len(list(queue_dir(state).glob("*.json")))
    return counts


def engine_outstanding():
    """Count per-engine backlog across queued/claimed/running.

    Used by producer ticks such as planner/memory-synthesis to apply
    backpressure when the pipeline is already carrying work. awaiting-review
    / awaiting-qa are excluded — those are pipeline holding states picked up
    by their own tickers.
    """
    counts = {"claude": 0, "codex": 0, "qa": 0}
    for t in iter_tasks(states=("queued", "claimed", "running")):
        eng = t.get("engine")
        if eng in counts:
            counts[eng] += 1
    return counts


def engine_active_counts():
    """Count active slot occupancy per engine across claimed/running only.

    Used by consumer ticks that should gate on real slot use, not their own
    queued driver tasks.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     old = engine_active_counts.__globals__["QUEUE_ROOT"]
    ...     engine_active_counts.__globals__["QUEUE_ROOT"] = root / "queue"
    ...     write_json_atomic(engine_active_counts.__globals__["QUEUE_ROOT"] / "queued" / "q.json", {"engine": "qa"})
    ...     write_json_atomic(engine_active_counts.__globals__["QUEUE_ROOT"] / "claimed" / "c.json", {"engine": "qa"})
    ...     write_json_atomic(engine_active_counts.__globals__["QUEUE_ROOT"] / "running" / "r.json", {"engine": "codex"})
    ...     out = engine_active_counts()
    ...     engine_active_counts.__globals__["QUEUE_ROOT"] = old
    >>> out == {"claude": 0, "codex": 1, "qa": 1}
    True
    """
    counts = {"claude": 0, "codex": 0, "qa": 0}
    for t in iter_tasks(states=("claimed", "running")):
        eng = t.get("engine")
        if eng in counts:
            counts[eng] += 1
    return counts


def queue_sample(state, limit=10):
    items = []
    for t in iter_tasks(states=(state,), limit=limit):
        attempt = int(t.get("attempt", 1) or 1)
        retry_note = f" a{attempt}" if attempt > 1 else ""
        items.append(
            f"  {t.get('task_id')} [{t.get('engine')}/{t.get('role')}{retry_note}] "
            f"{t.get('project')}: {t.get('summary','')[:60]}"
        )
    return items


def find_task(task_id, states=STATES):
    """Return (state, task) for a task id, else None.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     old = find_task.__globals__["QUEUE_ROOT"]
    ...     find_task.__globals__["QUEUE_ROOT"] = root / "queue"
    ...     write_json_atomic(find_task.__globals__["QUEUE_ROOT"] / "running" / "task-1.json", {"task_id": "task-1", "summary": "demo"})
    ...     out1 = find_task("task-1")
    ...     out2 = find_task("missing")
    ...     find_task.__globals__["QUEUE_ROOT"] = old
    >>> out1[0], out1[1]["summary"], out2 is None
    ('running', 'demo', True)
    """
    if _state_engine_read_enabled():
        try:
            row = get_state_engine().find_task(task_id, states=states)
            if row is not None:
                return row
            if not any(task_path(task_id, state).exists() for state in states):
                return None
            _record_state_engine_fs_fallback("task", task_id)
        except Exception:
            _record_state_engine_fs_fallback("task", task_id)
    for state in states:
        path = task_path(task_id, state)
        if path.exists():
            return state, read_json(path, {})
    return None


def running_tasks_text():
    """Return a concise list of running tasks.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     old = running_tasks_text.__globals__["QUEUE_ROOT"]
    ...     running_tasks_text.__globals__["QUEUE_ROOT"] = root / "queue"
    ...     out1 = running_tasks_text()
    ...     write_json_atomic(running_tasks_text.__globals__["QUEUE_ROOT"] / "running" / "task-1.json", {"task_id": "task-1", "engine": "codex", "role": "implementer", "project": "demo", "summary": "Fix CI"})
    ...     out2 = running_tasks_text()
    ...     running_tasks_text.__globals__["QUEUE_ROOT"] = old
    >>> out1, "task-1" in out2 and "Fix CI" in out2
    ('no running tasks', True)
    """
    items = queue_sample("running", limit=50)
    if not items:
        return "no running tasks"
    return "\n".join(["Running tasks:"] + items)


def task_text(task_id, log_tail_lines=20):
    """Return a task detail block for telegram/CLI inspection.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     logs_dir = root / "logs"
    ...     old = {k: task_text.__globals__[k] for k in ("QUEUE_ROOT", "LOGS_DIR")}
    ...     task_text.__globals__["QUEUE_ROOT"] = queue_root
    ...     task_text.__globals__["LOGS_DIR"] = logs_dir
    ...     write_json_atomic(queue_root / "running" / "task-1.json", {"task_id": "task-1", "engine": "codex", "role": "implementer", "project": "demo", "summary": "Fix CI", "source": "telegram", "feature_id": "feature-1"})
    ...     logs_dir.mkdir(parents=True, exist_ok=True)
    ...     _ = (logs_dir / "task-1.log").write_text("line1\\nline2\\n")
    ...     out1 = task_text("task-1", log_tail_lines=1)
    ...     out2 = task_text("missing")
    ...     for key, value in old.items():
    ...         task_text.__globals__[key] = value
    >>> ("task-1" in out1 and "state: running" in out1 and "line2" in out1, out2)
    (True, 'task not found: missing')
    """
    found = find_task(task_id)
    if not found:
        return f"task not found: {task_id}"
    state, task = found
    lines = [
        task.get("task_id", task_id),
        f"state: {state}",
        f"attempt: {int(task.get('attempt', 1) or 1)}",
        f"engine: {task.get('engine', '-')}",
        f"role: {task.get('role', '-')}",
        f"project: {task.get('project', '-')}",
        f"source: {task.get('source', '-')}",
        f"summary: {task.get('summary', '')}",
    ]
    optional_fields = (
        "feature_id",
        "parent_task_id",
        "pr_number",
        "base_branch",
        "worktree",
        "feedback_task_id",
        "conflict_task_id",
    )
    for field in optional_fields:
        value = task.get(field)
        if value:
            lines.append(f"{field}: {value}")
    blocker = task_blocker(task)
    if blocker:
        lines.append(f"blocker: {blocker.get('code')} ({blocker.get('source','-')})")
        if blocker.get("detail"):
            lines.append(f"blocker_detail: {blocker['detail']}")
    if task.get("last_retry_at"):
        lines.append(f"last_retry_at: {task.get('last_retry_at')}")
        lines.append(f"last_retry_source: {task.get('last_retry_source', '-')}")
        lines.append(f"last_retry_reason: {task.get('last_retry_reason', '-')}")
    history = list(task.get("attempt_history") or [])
    if history:
        lines.append("attempt_history:")
        for item in history[-3:]:
            lines.append(
                "  "
                f"a{item.get('attempt','?')} from={item.get('from_state','-')} "
                f"source={item.get('retry_source','-')} "
                f"reason={item.get('retry_reason','-')}"
            )
    engine_args = task.get("engine_args")
    if engine_args:
        lines.append("engine_args:")
        for key in sorted(engine_args):
            value = engine_args[key]
            rendered = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
            lines.append(f"  {key}: {rendered}")
    log_path = LOGS_DIR / f"{task_id}.log"
    lines.append(f"log: {log_path}")
    if log_path.exists():
        tail = log_path.read_text(errors="replace").splitlines()[-log_tail_lines:]
        if tail:
            lines.append("")
            lines.append("log tail:")
            lines.extend(tail)
    return "\n".join(lines)


# --- Periodic tick roles -----------------------------------------------------

def tick_planner():
    """Emit one claude planning task per project, each scoped to a new feature.

    Every planner tick that passes the backpressure check creates a feature
    (state/features/<id>.json) for each eligible project and enqueues a single
    claude planner task bound to that feature_id. When the claude worker runs
    the task, its emitted codex slices inherit the parent task's feature_id,
    so every slice of that planner run shares one feature branch.

    Gated: skips if any slot already has >1 outstanding task in
    queued/claimed/running. Prevents the 3-min tick cadence from unbounded
    queue growth when workers can't keep up.

    >>> import pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     repo = root / "repo"
    ...     memdir = repo / "repo-memory"
    ...     memdir.mkdir(parents=True)
    ...     _ = (memdir / "CURRENT_STATE.md").write_text("ok\\n")
    ...     old = {k: tick_planner.__globals__[k] for k in ("load_config", "engine_outstanding", "write_agent_status", "project_hard_stopped", "project_environment_ok", "acquire_lock", "assigned_roadmap_entries", "parse_roadmap_next_todo", "create_feature", "new_task", "enqueue_task", "FEATURES_DIR", "PLANNER_DISABLED_DIR")}
    ...     tick_planner.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(repo)}]}
    ...     tick_planner.__globals__["engine_outstanding"] = lambda: {"claude": 0, "codex": 0, "qa": 0}
    ...     tick_planner.__globals__["write_agent_status"] = lambda *args, **kwargs: None
    ...     tick_planner.__globals__["project_hard_stopped"] = lambda name: False
    ...     tick_planner.__globals__["project_environment_ok"] = lambda name: True
    ...     tick_planner.__globals__["acquire_lock"] = lambda *args, **kwargs: types.SimpleNamespace(close=lambda: None)
    ...     tick_planner.__globals__["assigned_roadmap_entries"] = lambda name: set()
    ...     tick_planner.__globals__["parse_roadmap_next_todo"] = lambda path, skip_ids=None: {"id": "R-001", "title": "First", "body": "TODO"}
    ...     tick_planner.__globals__["create_feature"] = lambda **kwargs: {"feature_id": "feature-1", "summary": "[R-001] First"}
    ...     tick_planner.__globals__["new_task"] = lambda **kwargs: kwargs
    ...     calls = []
    ...     tick_planner.__globals__["enqueue_task"] = lambda task: calls.append(task)
    ...     tick_planner.__globals__["FEATURES_DIR"] = root / "features"
    ...     tick_planner.__globals__["PLANNER_DISABLED_DIR"] = root / "planner-disabled"
    ...     tick_planner.__globals__["FEATURES_DIR"].mkdir(parents=True, exist_ok=True)
    ...     write_json_atomic(tick_planner.__globals__["FEATURES_DIR"] / "open.json", {"project": "demo", "status": "open"})
    ...     tick_planner()
    ...     out = len(calls)
    ...     for key, value in old.items():
    ...         tick_planner.__globals__[key] = value
    >>> out
    0

    >>> import pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     repo = root / "repo"
    ...     memdir = repo / "repo-memory"
    ...     memdir.mkdir(parents=True)
    ...     _ = (memdir / "CURRENT_STATE.md").write_text("ok\\n")
    ...     old = {k: tick_planner.__globals__[k] for k in ("load_config", "engine_outstanding", "write_agent_status", "project_hard_stopped", "project_environment_ok", "acquire_lock", "assigned_roadmap_entries", "parse_roadmap_next_todo", "create_feature", "new_task", "enqueue_task", "FEATURES_DIR", "PLANNER_DISABLED_DIR")}
    ...     tick_planner.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(repo)}]}
    ...     tick_planner.__globals__["engine_outstanding"] = lambda: {"claude": 0, "codex": 0, "qa": 0}
    ...     tick_planner.__globals__["write_agent_status"] = lambda *args, **kwargs: None
    ...     tick_planner.__globals__["project_hard_stopped"] = lambda name: False
    ...     tick_planner.__globals__["project_environment_ok"] = lambda name: True
    ...     tick_planner.__globals__["acquire_lock"] = lambda *args, **kwargs: types.SimpleNamespace(close=lambda: None)
    ...     tick_planner.__globals__["assigned_roadmap_entries"] = lambda name: set()
    ...     tick_planner.__globals__["parse_roadmap_next_todo"] = lambda path, skip_ids=None: {"id": "R-001", "title": "First", "body": "TODO"}
    ...     tick_planner.__globals__["create_feature"] = lambda **kwargs: {"feature_id": "feature-1", "summary": "[R-001] First"}
    ...     tick_planner.__globals__["new_task"] = lambda **kwargs: kwargs
    ...     calls = []
    ...     tick_planner.__globals__["enqueue_task"] = lambda task: calls.append(task)
    ...     tick_planner.__globals__["FEATURES_DIR"] = root / "features"
    ...     tick_planner.__globals__["PLANNER_DISABLED_DIR"] = root / "planner-disabled"
    ...     tick_planner.__globals__["FEATURES_DIR"].mkdir(parents=True, exist_ok=True)
    ...     write_json_atomic(tick_planner.__globals__["FEATURES_DIR"] / "merged.json", {"project": "demo", "status": "merged"})
    ...     tick_planner()
    ...     out = len(calls)
    ...     for key, value in old.items():
    ...         tick_planner.__globals__[key] = value
    >>> out
    1
    """
    outstanding = engine_outstanding()
    backed_up = {e: n for e, n in outstanding.items() if n > 1}
    if backed_up:
        msg = "Gated — backlog busy: " + ", ".join(f"{e}={n}" for e, n in sorted(backed_up.items()))
        write_agent_status("planner", "gated", msg)
        return
    write_agent_status("planner", "running", "Refreshing queue from repo-memory.")
    cfg = load_config()
    skipped_hard_stop = []
    for project in cfg["projects"]:
        if not project.get("planner_managed", True):
            continue
        # Only plan for projects whose repo-memory exists — avoid spamming stubs.
        memdir = pathlib.Path(project["path"]) / "repo-memory"
        if not (memdir / "CURRENT_STATE.md").exists():
            continue
        if not project_environment_ok(project["name"]):
            continue
        if project_hard_stopped(project["name"]):
            skipped_hard_stop.append(project["name"])
            continue
        if _project_has_open_feature(project["name"]):
            continue
        if planner_disabled(project["name"]):
            continue
        lock_fh = acquire_lock(f"{project['name']}.lock", mode="exclusive", timeout_sec=60)
        try:
            assigned = assigned_roadmap_entries(project["name"])
            roadmap_entry = parse_roadmap_next_todo(project["path"], skip_ids=assigned)
            if roadmap_entry is None:
                roadmap_path = memdir / "ROADMAP.md"
                if not roadmap_path.exists():
                    print(
                        f"WARN tick-planner: {project['name']} missing {roadmap_path}",
                        file=sys.stderr,
                    )
                continue
            feature = create_feature(
                project=project["name"],
                summary=f"Planner-emitted feature for {project['name']}",
                source=f"tick-planner:{roadmap_entry['id']}",
                roadmap_entry=roadmap_entry,
                roadmap_entry_id=roadmap_entry["id"],
            )
            task = new_task(
                role="planner",
                engine="claude",
                project=project["name"],
                summary=(
                    f"Decompose roadmap entry {feature['summary']} for "
                    f"{project['name']} (feature {feature['feature_id']})."
                ),
                source="tick-planner",
                braid_template=project_historian_template(project["name"]),
                feature_id=feature["feature_id"],
                engine_args={"roadmap_entry": roadmap_entry},
            )
            enqueue_task(task)
        finally:
            lock_fh.close()
    if skipped_hard_stop:
        write_agent_status(
            "planner", "idle",
            f"Queue refreshed. Hard-stopped: {','.join(skipped_hard_stop)}",
        )
    else:
        write_agent_status("planner", "idle", "Queue refreshed.")


def tick_reviewer():
    """Enqueue one claude reviewer-pass task per project with awaiting-review work.

    Gated on:
      1. claude slot outstanding > 0 (only one reviewer in flight across the whole
         claude slot, since claude is single-worker),
      2. no awaiting-review tasks for the project (nothing to review = skip).
    Each reviewer task claims the oldest awaiting-review target at run time and
    transitions it to awaiting-qa or failed based on the review verdict.
    """
    active = engine_active_counts()
    if active.get("claude", 0) > 0:
        write_agent_status(
            "reviewer", "gated", f"Gated — claude slot busy: claude={active['claude']}"
        )
        return
    # Count awaiting-review per project.
    ar_by_project = {}
    for t in iter_tasks(states=("awaiting-review",)):
        proj = t.get("project")
        if proj:
            ar_by_project[proj] = ar_by_project.get(proj, 0) + 1
    if not ar_by_project:
        write_agent_status("reviewer", "idle", "No awaiting-review work.")
        return
    write_agent_status("reviewer", "running", f"Queueing reviewer for {len(ar_by_project)} project(s).")
    cfg = load_config()
    enqueued = 0
    covered = []
    for project in cfg["projects"]:
        name = project["name"]
        if name not in ar_by_project:
            continue
        if has_project_task(project=name, engine="claude", role="reviewer", source="tick-reviewer"):
            covered.append(name)
            continue
        memdir = pathlib.Path(project["path"]) / "repo-memory"
        if not (memdir / "CURRENT_STATE.md").exists():
            continue
        task = new_task(
            role="reviewer",
            engine="claude",
            project=name,
            summary=f"Review oldest awaiting-review task for {name} ({ar_by_project[name]} pending).",
            source="tick-reviewer",
            braid_template=None,  # reviewer loads lvc-reviewer-pass internally
        )
        enqueue_task(task)
        enqueued += 1
        # Only one reviewer in flight at a time across the claude slot.
        break
    if enqueued:
        detail = f"Reviewer enqueued ({enqueued})."
    elif covered:
        detail = f"Reviewer already queued/running for: {','.join(covered)}"
    else:
        detail = "No reviewer enqueued."
    write_agent_status("reviewer", "idle", detail)


def tick_qa():
    """Enqueue one smoke-driver task per project with awaiting-qa work.

    Gated on:
      1. qa slot outstanding > 0 (only one smoke driver in flight at a time —
         qa is single-worker and smoke locks the project),
      2. no awaiting-qa tasks for the project (nothing to qa = skip).
    Each driver task claims the oldest awaiting-qa target at run time and
    runs smoke.sh in that target's worktree.
    """
    active = engine_active_counts()
    if active.get("qa", 0) > 0:
        write_agent_status(
            "qa", "gated", f"Gated — qa slot busy: qa={active['qa']}"
        )
        return
    aq_by_project = {}
    for t in iter_tasks(states=("awaiting-qa",)):
        proj = t.get("project")
        if proj:
            aq_by_project[proj] = aq_by_project.get(proj, 0) + 1
    if not aq_by_project:
        write_agent_status("qa", "idle", "No awaiting-qa work.")
        return
    write_agent_status("qa", "running", f"Queueing qa smoke for {len(aq_by_project)} project(s).")
    cfg = load_config()
    enqueued = 0
    covered = []
    for project in cfg["projects"]:
        name = project["name"]
        if name not in aq_by_project:
            continue
        if has_project_task(project=name, engine="qa", role="qa", source="tick-qa"):
            covered.append(name)
            continue
        memdir = pathlib.Path(project["path"]) / "repo-memory"
        if not (memdir / "CURRENT_STATE.md").exists():
            continue
        qa_cfg = project.get("qa", {})
        if not qa_cfg.get("smoke"):
            continue
        task = new_task(
            role="qa",
            engine="qa",
            project=name,
            summary=f"Smoke qa for {name} ({aq_by_project[name]} pending).",
            source="tick-qa",
            engine_args={"contract": "smoke"},
        )
        enqueue_task(task)
        enqueued += 1
        # Single qa worker; one driver in flight at a time.
        break
    if enqueued:
        detail = f"QA driver enqueued ({enqueued})."
    elif covered:
        detail = f"QA driver already queued/running for: {','.join(covered)}"
    else:
        detail = "No QA driver enqueued."
    write_agent_status("qa", "idle", detail)


def tick_regression(project_name):
    """Enqueue a full regression sweep for a single project."""
    write_agent_status("regression", "running", f"Queueing regression for {project_name}.")
    cfg = load_config()
    project = get_project(cfg, project_name)
    qa_cfg = project.get("qa", {})
    if not qa_cfg.get("regression"):
        raise SystemExit(f"project {project_name} has no qa.regression contract")
    task = new_task(
        role="qa",
        engine="qa",
        project=project_name,
        summary=f"Run full JMH regression sweep for {project_name}.",
        source="tick-regression",
        engine_args={
            "contract": "regression",
            "lock": f"{project_name}.lock",
            "threshold_pct": qa_cfg.get("regression_threshold_pct", 3),
        },
    )
    enqueue_task(task)
    write_agent_status("regression", "idle", f"Regression queued for {project_name}.")


def tick_regression_scheduled(today=None):
    """Daily tick: enqueue regression only for projects whose regression_days
    includes today's weekday. Lets multiple projects share one scheduler plist
    while keeping their sweeps on staggered days so they never collide on the
    machine."""
    if today is None:
        today = dt.datetime.now().strftime("%a").lower()[:3]
    cfg = load_config()
    enqueued = []
    skipped = []
    for project in cfg.get("projects", []):
        qa_cfg = project.get("qa") or {}
        if not qa_cfg.get("regression"):
            continue
        days = [d.lower()[:3] for d in qa_cfg.get("regression_days", [])]
        if not days:
            skipped.append(f"{project['name']}(no-days)")
            continue
        if today not in days:
            skipped.append(f"{project['name']}({','.join(days)})")
            continue
        tick_regression(project["name"])
        enqueued.append(project["name"])
    detail = f"today={today} enqueued={enqueued or '-'} skipped={skipped or '-'}"
    write_agent_status("regression", "idle", detail)
    return enqueued


# --- Reports -----------------------------------------------------------------

def repo_status(repo_path):
    p = pathlib.Path(repo_path)
    if not p.exists():
        return {"exists": False, "path": str(repo_path)}
    def run(cmd):
        return subprocess.run(cmd, cwd=repo_path, text=True, capture_output=True).stdout.strip()
    return {
        "exists": True,
        "branch": run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "dirty": bool(run(["git", "status", "--short"])),
        "recent_commits": run(["git", "log", "--oneline", "-5"]).splitlines(),
    }


def today_ymd():
    return dt.datetime.now().strftime("%Y%m%d")


def ppd_report_path(day=None):
    return REPORT_DIR / f"ppd-{day or today_ymd()}.md"


def report(kind):
    emit_runtime_metrics_snapshot(source=f"report:{kind}")
    ts = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{kind}_{ts}.md"
    lines = [f"# {kind.title()} Status Report", "", f"Generated: {now_iso()}", ""]
    lines.append("## Agent status")
    for s in effective_agent_statuses():
        lines.append(f"- **{s.get('role')}**: {s.get('status')} — {s.get('detail','')}")
    lines += ["", "## Queue"]
    counts = queue_counts()
    for st in STATES:
        lines.append(f"- {st}: {counts[st]}")
    health = environment_health()
    lines += ["", "## Environment"]
    lines.append(f"- ok: `{health.get('ok')}`")
    for issue in health.get("issues", [])[:20]:
        scope = issue.get("project") or issue.get("scope") or "global"
        lines.append(
            f"- [{issue.get('severity')}] `{issue.get('code')}` {scope}: "
            f"{issue.get('summary')} — {issue.get('detail')}"
        )
    retried = []
    for task in iter_tasks(states=STATES):
        attempt = int(task.get("attempt", 1) or 1)
        if attempt > 1:
            retried.append((task.get("state"), task))
    lines += ["", "## Retries"]
    lines.append(f"- tasks with retries: {len(retried)}")
    for state, task in sorted(retried, key=lambda item: (item[1].get("last_retry_at") or "", item[1].get("task_id") or ""), reverse=True)[:10]:
        lines.append(
            f"- `{task.get('task_id')}` state={state} attempt={task.get('attempt')} "
            f"project={task.get('project','-')} last_retry={task.get('last_retry_at') or '-'} "
            f"source={task.get('last_retry_source') or '-'}"
        )
    metrics = read_pr_sweep_metrics()
    lines += ["", "## pr-sweep guard telemetry"]
    lines.append(f"- guard skips total: {metrics.get('guard_skip_total', 0)}")
    for reason, count in sorted((metrics.get("guard_skip_by_reason") or {}).items()):
        lines.append(f"- {reason}: {count}")
    latest_metrics = latest_metric_values(
        names=("environment.ok", "environment.error_count", "feature.open_count", "feature.frontier_blocked_count")
    )
    if latest_metrics:
        lines += ["", "## Runtime metrics"]
        for name in ("environment.ok", "environment.error_count", "feature.open_count", "feature.frontier_blocked_count"):
            row = latest_metrics.get(name)
            if row:
                lines.append(f"- {name}: {row.get('value')}")
    lines += ["", "## BRAID templates"]
    idx = load_braid_index()
    if not idx:
        lines.append("- (none registered)")
    for tt, e in sorted(idx.items()):
        lines.append(
            f"- `{tt}`: uses={e.get('uses',0)}, topology_errors={e.get('topology_errors',0)}"
            f"{', paused' if e.get('dispatch_paused') else ''}"
        )
    lines += ["", "## Projects"]
    for project in load_config()["projects"]:
        st = repo_status(project["path"])
        lines.append(f"### {project['name']}")
        if not st.get("exists"):
            lines.append(f"- missing path: `{project['path']}`")
            lines.append("")
            continue
        lines.append(f"- branch: `{st['branch']}`")
        lines.append(f"- dirty: `{st['dirty']}`")
        for c in st["recent_commits"]:
            lines.append(f"  - {c}")
        lines.append("")
    workflows = open_feature_workflow_summaries()
    lines += ["", "## Open Feature Workflows"]
    if not workflows:
        lines.append("- none")
    for wf in workflows[:10]:
        frontier = wf.get("frontier") or {}
        blocker = frontier.get("blocker") or {}
        canary = wf.get("canary") or {}
        self_repair = wf.get("self_repair") or {}
        recent_events = wf.get("recent_events") or []
        lines.append(
            f"- `{wf.get('feature_id')}` {wf.get('project')}: "
            f"{wf.get('summary') or '-'}"
            f"{' [canary]' if canary.get('enabled') else ''}"
            f"{' [self-repair]' if self_repair.get('enabled') else ''}"
        )
        lines.append(
            f"  frontier={frontier.get('task_id') or '-'} state={frontier.get('state') or wf.get('feature_status') or '-'} "
            f"attempt={frontier.get('attempt') or 1}"
        )
        if frontier.get("entered_at"):
            lines.append(f"  entered_at={frontier.get('entered_at')}")
        if blocker.get("code"):
            lines.append(f"  blocker={blocker.get('code')}")
        if recent_events:
            evt = recent_events[-1]
            lines.append(f"  last_event={evt.get('ts') or '-'} {evt.get('role') or '-'}:{evt.get('event') or '-'}")
    canaries = list_canary_features()
    lines += ["", "## Synthetic Canaries"]
    if not canaries:
        lines.append("- none")
    cfg = load_config()
    canary_cfg = cfg.get("synthetic_canary") or {}
    if canary_cfg.get("enabled"):
        last_success = _latest_successful_canary(canary_cfg.get("project"))
        age = _format_age(_seconds_since_iso((last_success or {}).get("merged_at") or (last_success or {}).get("created_at")))
        lines.append(
            f"- configured lane: `{canary_cfg.get('project')}` interval={canary_cfg.get('interval_hours', 6)}h "
            f"success_sla={canary_cfg.get('success_sla_hours', 24)}h last_success={age or 'never'}"
        )
    for feature in canaries[:10]:
        lines.append(
            f"- `{feature.get('feature_id')}` {feature.get('project')}: "
            f"status={feature.get('status') or '-'} created_at={feature.get('created_at') or '-'} "
            f"summary={feature.get('summary') or '-'}"
        )
    if kind == "morning":
        ppd_path = ppd_report_path()
        if ppd_path.exists():
            lines += ["", "## PPD", "", ppd_path.read_text().strip()]
    out.write_text("\n".join(lines))
    append_event("report", "report_written", details={"kind": kind, "path": str(out)})
    return out


def has_in_flight_task(*, project, braid_template):
    for task in iter_tasks(states=("queued", "claimed", "running"), project=project):
        if task.get("braid_template") == braid_template:
            return True
    return False


def has_project_task(*, project, engine=None, source=None, role=None, states=("queued", "claimed", "running")):
    """Return True when a matching task already exists for the project."""
    for task in iter_tasks(states=states, project=project, engine=engine, role=role):
        if source and task.get("source") != source:
            continue
        return True
    return False


def tick_memory_synthesis(force=False):
    """Queue weekly memory-synthesis tasks for stale repo-memory state."""
    outstanding = engine_outstanding()
    if outstanding.get("claude", 0) > 1:
        write_agent_status(
            "memory-synthesis", "gated",
            f"Gated — claude slot busy: claude={outstanding['claude']}",
        )
        return []
    write_agent_status("memory-synthesis", "running", "Scanning repo-memory freshness.")
    cfg = load_config()
    now = dt.datetime.now()
    enqueued = []
    for project in cfg.get("projects", []):
        current_state = pathlib.Path(project["path"]) / "repo-memory" / "CURRENT_STATE.md"
        if not current_state.exists():
            continue
        age_days = (now - dt.datetime.fromtimestamp(current_state.stat().st_mtime)).total_seconds() / 86400.0
        if age_days <= 7 and not force:
            continue
        if has_in_flight_task(project=project["name"], braid_template="memory-synthesis"):
            continue
        task = new_task(
            role="historian",
            engine="claude",
            project=project["name"],
            summary=f"Weekly memory synthesis for {project['name']}.",
            source="tick-memory-synthesis",
            braid_template="memory-synthesis",
            engine_args={"mode": "memory-synthesis", "force": force},
        )
        enqueue_task(task)
        enqueued.append(project["name"])
    write_agent_status("memory-synthesis", "idle", f"enqueued={enqueued or '-'} force={force}")
    return enqueued


def tick_canary_workflows(force=False, *, project_override=None, fallback_from=None):
    """Queue a synthetic canary feature through the normal planner path.

    >>> import pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     repo = root / "repo"
    ...     memdir = repo / "repo-memory"
    ...     memdir.mkdir(parents=True)
    ...     _ = (memdir / "CURRENT_STATE.md").write_text("ok\\n")
    ...     old = {k: tick_canary_workflows.__globals__[k] for k in ("FEATURES_DIR", "load_config", "engine_outstanding", "write_agent_status", "create_feature", "new_task", "enqueue_task", "append_event", "project_environment_ok", "_project_has_open_feature", "planner_disabled", "project_hard_stopped")}
    ...     tick_canary_workflows.__globals__["FEATURES_DIR"] = root / "features"
    ...     tick_canary_workflows.__globals__["FEATURES_DIR"].mkdir(parents=True, exist_ok=True)
    ...     tick_canary_workflows.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(repo)}], "synthetic_canary": {"enabled": True, "project": "demo", "summary": "Canary", "roadmap_entry_id": "C-1", "roadmap_title": "Canary", "roadmap_body": "body", "interval_hours": 6}}
    ...     tick_canary_workflows.__globals__["engine_outstanding"] = lambda: {"claude": 0, "codex": 0, "qa": 0}
    ...     tick_canary_workflows.__globals__["write_agent_status"] = lambda *args, **kwargs: None
    ...     tick_canary_workflows.__globals__["project_environment_ok"] = lambda name, refresh=True: True
    ...     tick_canary_workflows.__globals__["_project_has_open_feature"] = lambda name: False
    ...     tick_canary_workflows.__globals__["planner_disabled"] = lambda name: False
    ...     tick_canary_workflows.__globals__["project_hard_stopped"] = lambda name: False
    ...     tick_canary_workflows.__globals__["create_feature"] = lambda **kwargs: {"feature_id": "feature-1", "summary": kwargs["summary"], "canary": kwargs["canary"], "project": kwargs["project"]}
    ...     tick_canary_workflows.__globals__["new_task"] = lambda **kwargs: kwargs
    ...     calls = []
    ...     tick_canary_workflows.__globals__["enqueue_task"] = lambda task: calls.append(task)
    ...     tick_canary_workflows.__globals__["append_event"] = lambda *args, **kwargs: None
    ...     out = tick_canary_workflows()
    ...     for key, value in old.items():
    ...         tick_canary_workflows.__globals__[key] = value
    >>> (out["enqueued"], calls[0]["feature_id"], calls[0]["engine_args"]["canary"]["enabled"])
    (1, 'feature-1', True)
    """
    cfg = load_config()
    canary_cfg = dict(cfg.get("synthetic_canary") or {})
    if not canary_cfg.get("enabled"):
        write_agent_status("canary", "idle", "disabled")
        return {"enqueued": 0, "reason": "disabled"}

    outstanding = engine_outstanding()
    backed_up = {engine: n for engine, n in outstanding.items() if n > 1}
    if backed_up:
        msg = "Gated — backlog busy: " + ", ".join(f"{e}={n}" for e, n in sorted(backed_up.items()))
        write_agent_status("canary", "gated", msg)
        return {"enqueued": 0, "reason": "gated"}

    project_name = project_override or canary_cfg.get("project")
    try:
        project = get_project(cfg, project_name)
    except KeyError:
        write_agent_status("canary", "failed", f"unknown project {project_name}")
        return {"enqueued": 0, "reason": "unknown_project"}
    if not project_environment_ok(project_name, refresh=True):
        write_agent_status("canary", "idle", f"project {project_name} environment degraded")
        return {"enqueued": 0, "reason": "environment_degraded"}

    if project_hard_stopped(project_name):
        write_agent_status("canary", "idle", f"project {project_name} hard-stopped")
        return {"enqueued": 0, "reason": "hard_stopped"}
    if planner_disabled(project_name):
        write_agent_status("canary", "idle", f"planner disabled for {project_name}")
        return {"enqueued": 0, "reason": "planner_disabled"}
    if _project_has_open_feature(project_name):
        if not list_canary_features(project_name=project_name, statuses=("open", "finalizing")):
            write_agent_status("canary", "idle", f"project {project_name} already has an open feature")
            return {"enqueued": 0, "reason": "project_busy"}
    if list_canary_features(project_name=project_name, statuses=("open", "finalizing")):
        write_agent_status("canary", "idle", f"in-flight canary exists for {project_name}")
        return {"enqueued": 0, "reason": "already_in_flight"}

    memdir = pathlib.Path(project["path"]) / "repo-memory"
    if not (memdir / "CURRENT_STATE.md").exists():
        write_agent_status("canary", "failed", f"missing repo-memory for {project_name}")
        return {"enqueued": 0, "reason": "missing_repo_memory"}

    due, last = _canary_interval_due(cfg, project_name)
    if not force and not due:
        age = _format_age(_seconds_since_iso((last or {}).get("created_at")))
        write_agent_status("canary", "idle", f"next canary not due yet for {project_name} (last={age or '-'})")
        return {"enqueued": 0, "reason": "not_due"}

    roadmap_entry = {
        "id": canary_cfg.get("roadmap_entry_id") or "R-021-CANARY",
        "title": canary_cfg.get("roadmap_title") or canary_cfg.get("summary") or "Synthetic workflow canary",
        "body": canary_cfg.get("roadmap_body") or "",
    }
    canary_meta = {
        "enabled": True,
        "kind": "workflow",
        "scheduled_by": "tick-canary",
        "interval_hours": canary_cfg.get("interval_hours", 6),
        "config_project": project_name,
    }
    if fallback_from:
        canary_meta["fallback_from"] = fallback_from
    feature = create_feature(
        project=project_name,
        summary=canary_cfg.get("summary") or "Synthetic workflow canary",
        source=f"tick-canary:{roadmap_entry['id']}",
        roadmap_entry=roadmap_entry,
        roadmap_entry_id=roadmap_entry["id"],
        canary=canary_meta,
    )
    task = new_task(
        role="planner",
        engine="claude",
        project=project_name,
        summary=(
            f"Decompose synthetic canary {feature['summary']} for "
            f"{project_name} (feature {feature['feature_id']})."
        ),
        source="tick-canary",
        braid_template=project_historian_template(project_name),
        feature_id=feature["feature_id"],
        engine_args={"roadmap_entry": roadmap_entry, "canary": canary_meta},
    )
    enqueue_task(task)
    append_event(
        "canary",
        "feature_enqueued",
        task_id=task.get("task_id"),
        feature_id=feature["feature_id"],
        details={"project": project_name, "summary": feature.get("summary"), "force": force},
    )
    if fallback_from:
        append_event(
            "canary",
            "canary_fallback_engaged",
            task_id=task.get("task_id"),
            feature_id=feature["feature_id"],
            details={"primary": fallback_from, "fallback": project_name},
        )
    write_agent_status("canary", "idle", f"enqueued {feature['feature_id']} for {project_name}")
    return {"enqueued": 1, "reason": "queued", "feature_id": feature["feature_id"], "task_id": task.get("task_id")}


def enqueue_self_repair(*, summary, evidence, issue_kind="runtime_bug", source="manual", issue_key=None):
    cfg = load_config()
    repair_cfg = cfg.get("self_repair") or {}
    if not repair_cfg.get("enabled", True):
        return {"enqueued": 0, "reason": "disabled"}
    project_name = repair_cfg.get("project", "devmini-orchestrator")
    resolved_issue_key = _self_repair_issue_key(
        issue_key=issue_key,
        issue_kind=issue_kind,
        summary=summary,
        evidence=evidence,
        source=source,
    )
    lock_fh = acquire_lock("self-repair.lock", mode="exclusive", timeout_sec=30)
    try:
        active = _active_self_repair_feature(project_name)
        if active:
            updated, issue = _attach_issue_to_self_repair_feature(
                active,
                summary=summary,
                evidence=evidence,
                issue_kind=issue_kind,
                source=source,
                issue_key=resolved_issue_key,
            )
            if _self_repair_issue_live(issue):
                return {
                    "enqueued": 0,
                    "reason": "already_attached",
                    "feature_id": updated["feature_id"],
                    "task_id": issue.get("planner_task_id"),
                    "issue_key": resolved_issue_key,
                }
            if updated.get("status") == "open" and not _self_repair_has_active_work(updated):
                task = _enqueue_self_repair_issue_task(updated, issue)
                return {
                    "enqueued": 1,
                    "reason": "attached_and_scheduled",
                    "feature_id": updated["feature_id"],
                    "task_id": task["task_id"],
                    "issue_key": resolved_issue_key,
                }
            return {
                "enqueued": 0,
                "reason": "attached_to_active",
                "feature_id": updated["feature_id"],
                "task_id": issue.get("planner_task_id"),
                "issue_key": resolved_issue_key,
            }

        if not _self_repair_project_environment_ok(project_name):
            return {"enqueued": 0, "reason": "environment_degraded", "project": project_name}
        feature = create_feature(
            project=project_name,
            summary=summary[:240],
            source=f"self-repair:{source}",
            roadmap_entry={"id": "R-023-SELF-REPAIR", "title": summary[:120], "body": evidence},
            roadmap_entry_id="R-023-SELF-REPAIR",
            self_repair={
                "enabled": True,
                "issue_kind": issue_kind,
                "source": source,
                "deploy_mode": repair_cfg.get("deploy_mode", "local-main"),
                "council_members": list(repair_cfg.get("council_members") or ()),
                "restart_launch_agents": bool(repair_cfg.get("restart_launch_agents", True)),
                "issues": [{
                    "issue_key": resolved_issue_key,
                    "issue_kind": issue_kind,
                    "summary": summary[:240],
                    "evidence": evidence,
                    "source": source,
                    "status": "pending",
                    "attached_at": now_iso(),
                    "last_seen_at": now_iso(),
                    "seen_count": 1,
                    "attempts": 0,
                    "max_attempts": self_repair_issue_max_attempts(cfg=cfg),
                    "planner_task_id": None,
                }],
            },
        )
        issue = ((feature.get("self_repair") or {}).get("issues") or [])[0]
        task = _enqueue_self_repair_issue_task(feature, issue)
        append_event(
            "self-repair",
            "feature_enqueued",
            task_id=task["task_id"],
            feature_id=feature["feature_id"],
            details={"project": project_name, "issue_kind": issue_kind, "source": source, "issue_key": resolved_issue_key},
        )
        return {"enqueued": 1, "reason": "queued", "feature_id": feature["feature_id"], "task_id": task["task_id"], "issue_key": resolved_issue_key}
    finally:
        lock_fh.close()


def status_text():
    lines = ["devmini orchestrator status", f"({now_iso()})", ""]
    lines.append("Agents:")
    for s in effective_agent_statuses():
        lines.append(f"  {s.get('role')}: {s.get('status')} — {s.get('detail','')}")
    lines.append("")
    lines.append("Queue:")
    counts = queue_counts()
    for st in STATES:
        if counts[st]:
            lines.append(f"  {st}: {counts[st]}")
    retried = sum(1 for t in iter_tasks(states=STATES) if int(t.get("attempt", 1) or 1) > 1)
    if retried:
        lines.append(f"  retried_tasks: {retried}")
    health = environment_health()
    lines.append(f"  environment_ok: {health.get('ok')}")
    if health.get("issues"):
        for issue in health.get("issues")[:5]:
            scope = issue.get("project") or issue.get("scope") or "global"
            lines.append(
                f"  env[{issue.get('severity')}]: {issue.get('code')} {scope} {issue.get('summary')}"
            )
    hard_stopped = [
        p["name"] for p in load_config().get("projects", [])
        if project_hard_stopped(p["name"])
    ]
    if hard_stopped:
        lines.append("")
        lines.append(f"HARD-STOP (regression-failure): {', '.join(hard_stopped)}")
    workflows = open_feature_workflow_summaries()
    if workflows:
        lines.append("")
        lines.append(f"Features (open): {len(workflows)}")
        per_proj = {}
        for wf in workflows:
            proj = wf.get("project") or "unknown"
            per_proj[proj] = per_proj.get(proj, 0) + 1
        for proj, n in sorted(per_proj.items()):
            lines.append(f"  {proj}: {n}")
        for wf in workflows[:5]:
            frontier = wf.get("frontier") or {}
            blocker = frontier.get("blocker") or {}
            canary = wf.get("canary") or {}
            self_repair = wf.get("self_repair") or {}
            recent_events = wf.get("recent_events") or []
            line = (
                f"  - {wf.get('feature_id')}: "
                f"{frontier.get('task_id') or '-'} {frontier.get('state') or wf.get('feature_status') or '-'}"
            )
            if canary.get("enabled"):
                line += " [canary]"
            if self_repair.get("enabled"):
                line += " [self-repair]"
            if frontier.get("attempt") and frontier.get("attempt", 1) > 1:
                line += f" a{frontier['attempt']}"
            if blocker.get("code"):
                line += f" blocker={blocker['code']}"
            lines.append(line)
            if recent_events:
                evt = recent_events[-1]
                lines.append(f"    last_event={evt.get('ts') or '-'} {evt.get('role') or '-'}:{evt.get('event') or '-'}")
    canaries = list_canary_features()
    if canaries:
        lines.append("")
        lines.append("Canaries:")
        for feature in canaries[:5]:
            lines.append(
                f"  - {feature.get('feature_id')}: {feature.get('project')} {feature.get('status') or '-'}"
            )
    canary_cfg = load_config().get("synthetic_canary") or {}
    if canary_cfg.get("enabled"):
        last_success = _latest_successful_canary(canary_cfg.get("project"))
        age = _format_age(_seconds_since_iso((last_success or {}).get("merged_at") or (last_success or {}).get("created_at")))
        lines.append("")
        lines.append(
            f"Canary freshness: project={canary_cfg.get('project')} interval={canary_cfg.get('interval_hours', 6)}h "
            f"sla={canary_cfg.get('success_sla_hours', 24)}h last_success={age or 'never'}"
        )
    idx = load_braid_index()
    if idx:
        lines.append("")
        lines.append("BRAID:")
        for tt, e in sorted(idx.items()):
            lines.append(
                f"  {tt}: uses={e.get('uses',0)} errs={e.get('topology_errors',0)}"
                f"{' paused' if e.get('dispatch_paused') else ''}"
            )
    metrics = read_pr_sweep_metrics()
    if metrics.get("guard_skip_total", 0):
        lines.append("")
        lines.append(
            "pr-sweep guard: "
            f"{metrics['guard_skip_total']} skip(s) "
            f"({', '.join(f'{k}={v}' for k, v in sorted((metrics.get('guard_skip_by_reason') or {}).items()))})"
        )
    latest = latest_metric_values(
        names=("environment.error_count", "feature.open_count", "feature.frontier_blocked_count", "workflow_check.issue_count")
    )
    if latest:
        lines.append("")
        lines.append("Metrics:")
        for key in ("environment.error_count", "feature.open_count", "feature.frontier_blocked_count", "workflow_check.issue_count"):
            row = latest.get(key)
            if row:
                lines.append(f"  {key}: {row.get('value')}")
    return "\n".join(lines)


def planner_status_text(project_filter=None):
    """Return a fixed-width planner status table in config order.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     features = root / "features"
    ...     disabled = root / "planner-disabled"
    ...     queue_root = root / "queue"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
    ...     old = {k: planner_status_text.__globals__[k] for k in ("FEATURES_DIR", "PLANNER_DISABLED_DIR", "QUEUE_ROOT", "load_config", "project_hard_stopped")}
    ...     planner_status_text.__globals__["FEATURES_DIR"] = features
    ...     planner_status_text.__globals__["PLANNER_DISABLED_DIR"] = disabled
    ...     planner_status_text.__globals__["QUEUE_ROOT"] = queue_root
    ...     planner_status_text.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}]}
    ...     planner_status_text.__globals__["project_hard_stopped"] = lambda name: False
    ...     body1 = planner_status_text()
    ...     _ = set_planner_disabled("demo", True, reason="pause")
    ...     write_json_atomic(features / "open.json", {"project": "demo", "status": "open"})
    ...     write_json_atomic(queue_root / "claimed" / "task-1.json", {"project": "demo"})
    ...     body2 = planner_status_text()
    ...     body3 = planner_status_text(project_filter="missing")
    ...     for key, value in old.items():
    ...         planner_status_text.__globals__[key] = value
    >>> ("demo" in body1 and "enabled" in body1 and "0" in body1, "disabled" in body2 and "1" in body2, body3)
    (True, True, 'error: unknown project missing')
    """
    cfg = load_config()
    projects = cfg.get("projects", [])
    names = [p["name"] for p in projects]
    if project_filter is not None and project_filter not in names:
        return f"error: unknown project {project_filter}"
    rows = []
    workflows = open_feature_workflow_summaries()
    in_flight_counts = {}
    for task in iter_tasks(states=("claimed", "running")):
        project_name = task.get("project")
        if not project_name:
            continue
        in_flight_counts[project_name] = in_flight_counts.get(project_name, 0) + 1
    for project in projects:
        name = project["name"]
        if project_filter is not None and name != project_filter:
            continue
        open_features = sum(1 for wf in workflows if wf.get("project") == name)
        in_flight = in_flight_counts.get(name, 0)
        rows.append({
            "project": name,
            "planner": (
                "manual" if not project.get("planner_managed", True)
                else ("enabled" if not planner_disabled(name) else "disabled")
            ),
            "hard_stop": "yes" if project_hard_stopped(name) else "no",
            "open_features": str(open_features),
            "in_flight": str(in_flight),
        })
    widths = {
        "project": max(len("project"), *(len(r["project"]) for r in rows)) if rows else len("project"),
        "planner": max(len("planner"), *(len(r["planner"]) for r in rows)) if rows else len("planner"),
        "hard_stop": max(len("hard-stop"), *(len(r["hard_stop"]) for r in rows)) if rows else len("hard-stop"),
        "open_features": max(len("open-features"), *(len(r["open_features"]) for r in rows)) if rows else len("open-features"),
        "in_flight": max(len("in-flight"), *(len(r["in_flight"]) for r in rows)) if rows else len("in-flight"),
    }

    def fmt(project, planner, hard_stop, open_features, in_flight):
        return (
            project.ljust(widths["project"]) + " | " +
            planner.ljust(widths["planner"]) + " | " +
            hard_stop.ljust(widths["hard_stop"]) + " | " +
            open_features.ljust(widths["open_features"]) + " | " +
            in_flight.ljust(widths["in_flight"])
        )

    lines = [
        fmt("project", "planner", "hard-stop", "open-features", "in-flight"),
        fmt("-" * widths["project"], "-" * widths["planner"], "-" * widths["hard_stop"], "-" * widths["open_features"], "-" * widths["in_flight"]),
    ]
    for row in rows:
        lines.append(fmt(row["project"], row["planner"], row["hard_stop"], row["open_features"], row["in_flight"]))
    return "\n".join(lines)


def _telegram_guess_project(question):
    lower = question.lower()
    aliases = {
        "lvc": "lvc-standard",
        "lvc-standard": "lvc-standard",
        "dag": "dag-framework",
        "dag-framework": "dag-framework",
        "trp": "trade-research-platform",
        "trade-research-platform": "trade-research-platform",
    }
    for needle, project_name in aliases.items():
        if needle in lower:
            for project in load_config().get("projects", []):
                if project["name"] == project_name:
                    return project
    return None


def _telegram_find_pr_number(question):
    m = re.search(r"\bPR\s*#?(\d+)\b", question, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(?<![A-Za-z0-9])#(\d+)\b", question)
    if m:
        return int(m.group(1))
    return None


def _telegram_find_task_id(question):
    m = re.search(r"\btask-\d{8}-\d{6}-[a-f0-9]+\b", question)
    return m.group(0) if m else None


def _telegram_find_feature_id(question):
    m = re.search(r"\bfeature-\d{8}-\d{6}-[a-f0-9]+\b", question)
    return m.group(0) if m else None


def _recent_text_lines(path, limit=40):
    p = pathlib.Path(path)
    if not p.exists():
        return "(missing)"
    try:
        lines = p.read_text(errors="replace").splitlines()
    except OSError as exc:
        return f"(unreadable: {exc})"
    return "\n".join(lines[-limit:]) if lines else "(empty)"


def _feature_context_text(project_name=None, limit=5):
    feats = list_features()
    if project_name:
        feats = [f for f in feats if f.get("project") == project_name]
    feats.sort(key=lambda f: f.get("created_at", ""), reverse=True)
    rows = []
    for feature in feats[:limit]:
        rows.append(
            {
                "feature_id": feature.get("feature_id"),
                "project": feature.get("project"),
                "status": feature.get("status"),
                "roadmap_entry_id": feature.get("roadmap_entry_id"),
                "summary": feature.get("summary"),
                "created_at": feature.get("created_at"),
                "merged_at": feature.get("merged_at"),
                "final_pr_number": feature.get("final_pr_number"),
                "abandoned_reason": feature.get("abandoned_reason"),
            }
        )
    return json.dumps(rows, indent=2, sort_keys=True) if rows else "[]"


def _feature_workflow_context(project_name=None, limit=5):
    feats = list_features()
    if project_name:
        feats = [f for f in feats if f.get("project") == project_name]
    feats.sort(key=lambda f: f.get("created_at", ""), reverse=True)
    rows = [feature_workflow_summary(feature) for feature in feats[:limit]]
    return json.dumps(rows, indent=2, sort_keys=True) if rows else "[]"


def _recent_project_task_context(project_name, limit=8):
    tasks = iter_tasks(project=project_name, newest_first=True)
    rows = []
    for task in tasks[:limit]:
        row = {
            "task_id": task.get("task_id"),
            "state": task.get("state"),
            "role": task.get("role"),
            "summary": task.get("summary"),
            "feature_id": task.get("feature_id"),
            "created_at": task.get("created_at"),
            "finished_at": task.get("finished_at"),
        }
        log_path = task.get("log_path")
        if log_path:
            log_text = _recent_text_lines(log_path, limit=30)
            if "# dropped " in log_text or "decomposed into 0" in log_text or "worker crashed" in log_text:
                row["log_excerpt"] = log_text[-1200:]
        rows.append(row)
    return json.dumps(rows, indent=2, sort_keys=True) if rows else "[]"


def _recent_feature_diagnosis(project_name, limit=5):
    items = []
    for wf in open_feature_workflow_summaries(project_name)[:limit]:
        item = {
            "feature_id": wf.get("feature_id"),
            "status": wf.get("feature_status"),
            "summary": wf.get("summary"),
            "planner": wf.get("planner"),
            "frontier": wf.get("frontier"),
            "recent_events": wf.get("recent_events") or [],
            "child_states": wf.get("child_states") or {},
        }
        items.append(item)
    return json.dumps(items, indent=2, sort_keys=True) if items else "[]"


def _workflow_snapshot():
    counts = queue_counts()
    open_features = {}
    for wf in open_feature_workflow_summaries():
        project = wf.get("project") or "unknown"
        open_features[project] = open_features.get(project, 0) + 1
    hard_stopped = [
        p["name"] for p in load_config().get("projects", [])
        if project_hard_stopped(p["name"])
    ]
    return json.dumps(
        {
            "captured_at": now_iso(),
            "queue_counts": {state: counts[state] for state in STATES if counts[state]},
            "open_features_by_project": open_features,
            "hard_stopped_projects": hard_stopped,
            "pr_sweep_metrics": read_pr_sweep_metrics(),
        },
        indent=2,
        sort_keys=True,
    )


def _agent_status_snapshot():
    today = dt.datetime.now().date().isoformat()
    rows = []
    for status in effective_agent_statuses():
        updated_at = status.get("updated_at") or ""
        rows.append(
            {
                "role": status.get("role"),
                "status": status.get("status"),
                "detail": status.get("detail", ""),
                "updated_at": updated_at,
                "same_day": updated_at.startswith(today),
            }
        )
    return json.dumps(rows, indent=2, sort_keys=True)


def _regression_schedule_snapshot(project_name=None):
    today = dt.datetime.now()
    today_code = today.strftime("%a").lower()[:3]
    rows = []
    for project in load_config().get("projects", []):
        name = project["name"]
        if project_name is not None and name != project_name:
            continue
        qa_cfg = project.get("qa") or {}
        days = [d.lower()[:3] for d in qa_cfg.get("regression_days", [])]
        rows.append(
            {
                "project": name,
                "today_date": today.date().isoformat(),
                "today_weekday": today.strftime("%A"),
                "today_code": today_code,
                "configured_days": days,
                "scheduled_today": today_code in days,
                "agent_status": read_json(AGENT_STATE_DIR / "regression.json", {}),
            }
        )
    return json.dumps(rows, indent=2, sort_keys=True)


def _parse_investigation_request(question):
    raw = (question or "").strip()
    if not raw:
        return "claude", ""
    lower = raw.lower()
    for target in ("claude", "codex", "both"):
        prefix = f"{target}:"
        if lower.startswith(prefix):
            return target, raw[len(prefix):].strip()
    m = re.match(r"^(claude|codex|both)\s+(.+)$", raw, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).lower(), m.group(2).strip()
    return "claude", raw


def _gh_pr_snapshot(repo_path, pr_number):
    if shutil.which("gh") is None:
        return {"error": "gh CLI not installed"}
    proc = subprocess.run(
        [
            "gh", "pr", "view", str(pr_number),
            "--json",
            "number,title,state,isDraft,mergeable,mergeStateStatus,reviewDecision,baseRefName,headRefName,statusCheckRollup,url",
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout or "gh pr view failed").strip()[:240]}
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {"error": f"bad gh pr json: {exc}"}


def _request_codex_review(repo_path, pr_number, *, reason=None, commit_sha=None):
    if shutil.which("gh") is None:
        return False, "gh CLI not installed"
    if not pr_number:
        return False, "missing pr number"
    details = []
    if reason:
        details.append(reason)
    if commit_sha:
        details.append(f"head {commit_sha[:12]}")
    suffix = f" ({'; '.join(details)})" if details else ""
    body = f"@codex please review the latest push{suffix}."
    try:
        proc = subprocess.run(
            ["gh", "pr", "comment", str(pr_number), "--body", body],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "gh pr comment timeout"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "gh pr comment failed").strip()[:240]
    return True, "ok"


def _latest_feature_planner_task(feature_id):
    latest = None
    latest_state = None
    for task in iter_tasks(states=STATES, role="planner"):
        if task.get("feature_id") != feature_id:
            continue
        if latest is None or task.get("created_at", "") > latest.get("created_at", ""):
            latest = task
            latest_state = task.get("state")
    if latest is None:
        return None
    return latest_state, latest


def _feature_ready_for_finalize(feature):
    children = _load_feature_children(feature)
    if children is None:
        return False
    if not children:
        return False
    return all(
        child.get("state") == "done"
        and child.get("cleaned_at") is not None
        and child.get("pr_final_state") == "MERGED"
        for child in children
    )


def _feature_related_tasks(feature_id):
    tasks = []
    if not feature_id:
        return tasks
    for task in iter_tasks(states=STATES):
        if task.get("feature_id") != feature_id:
            continue
        tasks.append({"task_id": task.get("task_id"), "state": task.get("state"), "task": task})
    return tasks


def _follow_up_frontier_key(item):
    task = item.get("task") or {}
    entered_at, _ = task_state_entered_at(item.get("task_id"), item.get("state"))
    state_rank = {
        "failed": 0,
        "blocked": 1,
        "abandoned": 2,
        "running": 3,
        "claimed": 4,
        "queued": 5,
        "awaiting-review": 6,
        "awaiting-qa": 7,
    }
    return (
        state_rank.get(item.get("state"), 99),
        entered_at or "",
        task.get("created_at") or "",
        item.get("task_id") or "",
    )


def _follow_up_repair_key(item):
    """Group repeated repair attempts so older superseded lanes don't own the frontier.

    >>> _follow_up_repair_key({"task_id": "t1", "task": {"feature_id": "f1", "engine_args": {"mode": "feature-pr-feedback", "feature_pr_number": 21}}})
    ('feature-pr-feedback', 'f1', 21)
    >>> _follow_up_repair_key({"task_id": "t2", "task": {"feature_id": "f1", "engine_args": {"mode": "pr-feedback", "pr_number": 7}, "parent_task_id": "task-parent"}})
    ('pr-feedback', 'task-parent', 7)
    >>> _follow_up_repair_key({"task_id": "t3", "task": {"feature_id": "f1", "source": "review-feedback:task-r", "engine_args": {"target_task_id": "task-target"}}})
    ('review-feedback', 'f1', 'task-target')
    """
    task = item.get("task") or {}
    eargs = task.get("engine_args") or {}
    mode = eargs.get("mode")
    if mode == "feature-pr-feedback":
        return ("feature-pr-feedback", task.get("feature_id"), eargs.get("feature_pr_number"))
    if mode == "pr-feedback":
        return ("pr-feedback", task.get("parent_task_id"), eargs.get("pr_number"))
    if (task.get("source") or "").startswith("review-feedback:"):
        return ("review-feedback", task.get("feature_id"), eargs.get("target_task_id") or task.get("parent_task_id"))
    return None


def _suppress_superseded_follow_ups(items):
    """Drop older retry attempts once a newer task exists for the same repair lane.

    >>> items = [
    ...     {"task_id": "task-old", "state": "failed", "task": {"created_at": "2026-04-16T12:00:00", "feature_id": "f1", "engine_args": {"mode": "feature-pr-feedback", "feature_pr_number": 21}}},
    ...     {"task_id": "task-new", "state": "failed", "task": {"created_at": "2026-04-16T12:05:00", "feature_id": "f1", "engine_args": {"mode": "feature-pr-feedback", "feature_pr_number": 21}}},
    ... ]
    >>> [item["task_id"] for item in _suppress_superseded_follow_ups(items)]
    ['task-new']
    """
    latest = {}
    for item in items:
        key = _follow_up_repair_key(item)
        if key is None:
            continue
        marker = (
            (item.get("task") or {}).get("created_at") or "",
            item.get("task_id") or "",
        )
        if marker > latest.get(key, ("", "")):
            latest[key] = marker

    kept = []
    for item in items:
        key = _follow_up_repair_key(item)
        if key is None:
            kept.append(item)
            continue
        marker = (
            (item.get("task") or {}).get("created_at") or "",
            item.get("task_id") or "",
        )
        if marker == latest.get(key):
            kept.append(item)
    return kept


def feature_workflow_summary(feature):
    """Build a compact workflow summary with event-backed timing for the frontier task."""
    feature_id = feature.get("feature_id")
    state_rank = {
        "running": 0,
        "claimed": 1,
        "queued": 2,
        "awaiting-review": 3,
        "awaiting-qa": 4,
        "blocked": 5,
        "failed": 6,
        "abandoned": 7,
        "missing": 8,
        "done": 9,
        None: 10,
    }

    def frontier_sort_key(item):
        task = item.get("task") or {}
        created_at = task.get("created_at") or ""
        try:
            created_rank = -int(dt.datetime.fromisoformat(created_at).timestamp()) if created_at else 0
        except ValueError:
            created_rank = 0
        return (
            state_rank.get(item.get("state"), 99),
            -int(bool(task)),
            -(1 if item.get("state") in ("running", "claimed", "queued", "awaiting-review", "awaiting-qa") else 0),
            created_rank,
            item.get("task_id") or "",
        )

    child_ids = set(feature.get("child_task_ids", []))
    children = []
    for child_id in feature.get("child_task_ids", []):
        found = find_task(child_id)
        if not found:
            children.append({"task_id": child_id, "state": "missing", "task": None})
        else:
            state, task = found
            children.append({"task_id": child_id, "state": state, "task": task})

    frontier = None
    child_frontier = [child for child in children if child["state"] != "done"]
    if child_frontier:
        child_frontier.sort(key=frontier_sort_key)
        frontier = child_frontier[0]

    follow_ups = []
    if frontier is None:
        for item in _feature_related_tasks(feature_id):
            task_id = item.get("task_id")
            if task_id in child_ids:
                continue
            task = item.get("task") or {}
            if task.get("role") == "planner":
                continue
            if item.get("state") == "done":
                continue
            follow_ups.append(item)
        follow_ups = _suppress_superseded_follow_ups(follow_ups)
        follow_ups.sort(key=lambda item: (frontier_sort_key(item), _follow_up_frontier_key(item)))
        if follow_ups:
            frontier = follow_ups[0]

    planner = _latest_feature_planner_task(feature_id)
    planner_state = planner[0] if planner else None
    planner_task = planner[1] if planner else None
    planner_entered_at = planner_reason = None
    if planner_task and planner_state:
        planner_entered_at, planner_reason = task_state_entered_at(planner_task["task_id"], planner_state)

    frontier_state = frontier_entered_at = frontier_reason = None
    frontier_attempt = None
    frontier_blocker = None
    if frontier:
        frontier_state = frontier["state"]
        if frontier["task"] is not None and frontier_state:
            frontier_entered_at, frontier_reason = task_state_entered_at(frontier["task_id"], frontier_state)
            frontier_attempt = int(frontier["task"].get("attempt", 1) or 1)
            frontier_blocker = task_blocker(frontier["task"])
    frontier_age_seconds = _seconds_since_iso(frontier_entered_at)
    recent_events = []
    for row in read_events(feature_id=feature_id, limit=6):
        recent_events.append(
            {
                "ts": row.get("ts"),
                "role": row.get("role"),
                "event": row.get("event"),
                "task_id": row.get("task_id"),
                "details": row.get("details") or {},
            }
        )
    repair_events = [
        row for row in recent_events
        if row.get("event") in ("repair_attempt", "retry_reset")
    ]
    workflow_check = dict(feature.get("workflow_check") or {})

    return {
        "feature_id": feature_id,
        "feature_status": feature.get("status"),
        "project": feature.get("project"),
        "summary": feature.get("summary"),
        "canary": dict(feature.get("canary") or {}),
        "self_repair": dict(feature.get("self_repair") or {}),
        "child_states": {item["task_id"]: item["state"] for item in children},
        "follow_up_states": {item["task_id"]: item["state"] for item in follow_ups},
        "planner": {
            "task_id": planner_task.get("task_id") if planner_task else None,
            "state": planner_state,
            "entered_at": planner_entered_at,
            "reason": planner_reason,
        },
        "frontier": {
            "task_id": frontier["task_id"] if frontier else None,
            "state": frontier_state,
            "entered_at": frontier_entered_at,
            "reason": frontier_reason,
            "age_seconds": frontier_age_seconds,
            "age_text": _format_age(frontier_age_seconds),
            "attempt": frontier_attempt,
            "blocker": frontier_blocker,
        },
        "workflow_check": workflow_check,
        "recent_events": recent_events,
        "repair_history": repair_events,
    }


def open_feature_workflow_summaries(project_name=None):
    feats = list_features()
    feats = [f for f in feats if f.get("status") in ("open", "finalizing")]
    if project_name is not None:
        feats = [f for f in feats if f.get("project") == project_name]
    feats.sort(key=lambda f: f.get("created_at", ""), reverse=True)
    return [feature_workflow_summary(feature) for feature in feats]


def _workflow_check_attempts(feature, issue_key):
    wc = feature.get("workflow_check") or {}
    attempts = wc.get("attempts") or {}
    try:
        return int(attempts.get(issue_key, 0))
    except (TypeError, ValueError):
        return 0


def _workflow_check_record_attempt(feature_id, issue_key, action, note):
    def mut(feature):
        wc = feature.setdefault("workflow_check", {})
        attempts = wc.setdefault("attempts", {})
        attempts[issue_key] = int(attempts.get(issue_key, 0)) + 1
        wc["last_issue_key"] = issue_key
        wc["last_action"] = action
        wc["last_note"] = note
        wc["updated_at"] = now_iso()
    update_feature(feature_id, mut)
    append_event(
        "workflow-check",
        "repair_attempt",
        feature_id=feature_id,
        details={"issue_key": issue_key, "action": action, "note": note},
    )


def _workflow_issue_self_repair_summary(issue):
    blocker = issue.get("blocker") or {}
    project = issue.get("project") or "unknown-project"
    kind = issue.get("kind") or "workflow-issue"
    summary = issue.get("summary") or issue.get("feature_id") or "workflow issue"
    blocker_code = blocker.get("code") or "runtime_bug"
    task_id = issue.get("task_id") or "-"
    return f"Self-repair {project}: {blocker_code} in {kind} for {task_id} — {summary}"[:240]


def _workflow_issue_self_repair_evidence(issue):
    blocker = issue.get("blocker") or {}
    workflow = issue.get("workflow") or {}
    frontier = workflow.get("frontier") or {}
    lines = [
        f"feature_id: {issue.get('feature_id')}",
        f"project: {issue.get('project')}",
        f"kind: {issue.get('kind')}",
        f"summary: {issue.get('summary')}",
        f"issue_key: {issue.get('issue_key')}",
        f"task_id: {issue.get('task_id')}",
        f"task_state: {issue.get('task_state')}",
        f"blocker_code: {blocker.get('code')}",
        f"blocker_summary: {blocker.get('summary')}",
        f"blocker_detail: {blocker.get('detail')}",
        f"diagnosis: {issue.get('diagnosis')}",
        f"frontier_reason: {frontier.get('reason')}",
    ]
    repairs = workflow.get("repair_history") or []
    if repairs:
        evt = repairs[-1]
        lines.append(
            f"last_repair_activity: {evt.get('ts')} {evt.get('role')}:{evt.get('event')}"
        )
    return "\n".join(line for line in lines if line.split(": ", 1)[1] not in ("None", ""))


def _workflow_check_worker_plists():
    home = pathlib.Path.home() / "Library" / "LaunchAgents"
    names = (
        "worker.claude",
        "worker.codex",
        "worker.codex-2",
        "worker.codex-3",
        "worker.codex-4",
        "worker.codex-5",
        "worker.codex-6",
        "worker.qa",
    )
    return [home / f"com.devmini.orchestrator.{name}.plist" for name in names]


def orchestrator_launch_agent_plists():
    home = pathlib.Path.home() / "Library" / "LaunchAgents"
    return sorted(home.glob("com.devmini.orchestrator*.plist"))


def restart_orchestrator_launch_agents(*, exclude_labels=None):
    """Reload local orchestrator launch agents from plist files.

    Returns `(restarted, failed)`.

    >>> import tempfile
    >>> from types import SimpleNamespace
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     home = pathlib.Path(tmp)
    ...     agents = home / "Library" / "LaunchAgents"
    ...     agents.mkdir(parents=True)
    ...     _ = (agents / "com.devmini.orchestrator.worker.codex.plist").write_text("<plist/>")
    ...     _ = (agents / "com.devmini.orchestrator.worker.qa.plist").write_text("<plist/>")
    ...     old_home = restart_orchestrator_launch_agents.__globals__["pathlib"].Path.home
    ...     old_run = restart_orchestrator_launch_agents.__globals__["subprocess"].run
    ...     calls = []
    ...     restart_orchestrator_launch_agents.__globals__["pathlib"].Path.home = lambda: home
    ...     restart_orchestrator_launch_agents.__globals__["subprocess"].run = lambda cmd, **kwargs: (calls.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    ...     out = restart_orchestrator_launch_agents(exclude_labels={"com.devmini.orchestrator.worker.qa"})
    ...     restart_orchestrator_launch_agents.__globals__["pathlib"].Path.home = old_home
    ...     restart_orchestrator_launch_agents.__globals__["subprocess"].run = old_run
    ...     out[0], len(calls)
    (['com.devmini.orchestrator.worker.codex'], 2)
    """
    restarted = []
    failed = []
    exclude = set(exclude_labels or ())
    for plist in orchestrator_launch_agent_plists():
        label = plist.stem
        if label in exclude:
            continue
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, text=True, check=False)
        proc = subprocess.run(["launchctl", "load", str(plist)], capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            restarted.append(label)
        else:
            failed.append(f"{label}: {(proc.stderr or proc.stdout or 'load failed').strip()[:160]}")
    return restarted, failed


def _workflow_check_restart_workers():
    restarted = []
    failed = []
    for plist in _workflow_check_worker_plists():
        if not plist.exists():
            continue
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, text=True, check=False)
        proc = subprocess.run(["launchctl", "load", str(plist)], capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            restarted.append(plist.name)
        else:
            failed.append(f"{plist.name}: {(proc.stderr or proc.stdout or 'load failed').strip()[:160]}")
    return restarted, failed


def _workflow_check_retry_task(task, state, reason):
    reset_task_for_retry(
        task["task_id"],
        state,
        reason=reason,
        source="workflow-check",
    )
    found = find_task(task["task_id"])
    return bool(found and found[0] in ("queued", "claimed", "running"))


def _workflow_check_abandon_task(task, state, reason):
    move_task(
        task["task_id"],
        state,
        "abandoned",
        reason=reason,
        mutator=lambda t: t.update({"finished_at": now_iso(), "abandoned_reason": reason}),
    )
    return True


def _workflow_check_mark_ready_for_merge(feature, issue, reason):
    task = issue.get("task")
    state = issue.get("task_state")
    if task and state in ("failed", "blocked"):
        _workflow_check_abandon_task(
            task,
            state,
            reason=f"workflow-check: {reason[:120]}",
        )

    feature_id = feature["feature_id"]
    def mut(f):
        sweep = dict(f.get("final_pr_sweep") or {})
        sweep["merge_ready_at"] = now_iso()
        sweep["merge_ready_reason"] = reason
        sweep["feedback_task_id"] = None
        sweep["last_feedback_task_id"] = None
        f["final_pr_sweep"] = sweep
    update_feature(feature_id, mut)
    append_event(
        "workflow-check",
        "final_pr_merge_ready",
        feature_id=feature_id,
        task_id=issue.get("task_id"),
        details={"reason": reason, "final_pr_number": feature.get("final_pr_number")},
    )
    return True


def _repair_project_main_checkout(project):
    repo_path = pathlib.Path(project["path"])
    if not repo_path.exists():
        return {"fixed": False, "detail": f"missing repo path: {repo_path}"}
    branch = repo_status(repo_path).get("branch")
    if branch and branch != "main":
        return {"fixed": False, "detail": f"refusing to auto-repair non-main branch: {branch}"}

    changes = []
    dirty = repo_status(repo_path).get("dirty")
    if dirty:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "stash", "push", "-u", "-m", f"orchestrator-auto-main-repair-{stamp}"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            return {"fixed": False, "detail": (proc.stderr or proc.stdout or "git stash failed").strip()[:240]}
        changes.append("git stash push -u")

    for cmd in (
        ["git", "-C", str(repo_path), "fetch", "origin", "main"],
        ["git", "-C", str(repo_path), "checkout", "main"],
        ["git", "-C", str(repo_path), "reset", "--hard", "origin/main"],
    ):
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            return {"fixed": False, "detail": (proc.stderr or proc.stdout or "git repair failed").strip()[:240], "changes": changes}
    fixed = _project_main_preflight_issue(project) is None
    return {"fixed": fixed, "detail": "project main checkout repaired" if fixed else "project main checkout still dirty", "changes": changes}


def _repair_qa_contract(task):
    cfg = load_config()
    try:
        project = get_project(cfg, task["project"])
    except KeyError:
        return {"fixed": False, "detail": f"unknown project: {task.get('project')}"}
    contract_kind = ((task.get("engine_args") or {}).get("contract") or "smoke").strip()
    script_rel = ((task.get("engine_args") or {}).get("script") or ((project.get("qa") or {}).get(contract_kind) or "")).strip()
    if not script_rel:
        return {"fixed": False, "detail": f"qa.{contract_kind} missing from project config"}
    restored, detail = _restore_project_file_from_head(project["path"], script_rel)
    script_abs = pathlib.Path(project["path"]) / script_rel
    if script_abs.exists():
        try:
            mode = script_abs.stat().st_mode
            script_abs.chmod(mode | 0o111)
        except OSError as exc:
            return {"fixed": False, "detail": str(exc), "restored": restored}
    return {"fixed": bool(script_abs.exists()) and os.access(script_abs, os.X_OK), "detail": detail or str(script_abs), "restored": restored}


def _enqueue_qa_contract_repair(task):
    if not task:
        return {"enqueued": False, "reason": "missing_task"}
    target_task_id = task.get("task_id")
    source = f"fix-qa-contract:{target_task_id}"
    for existing in iter_tasks(states=("queued", "claimed", "running", "awaiting-review", "awaiting-qa")):
        if existing.get("source") == source:
            return {"enqueued": False, "reason": "already_exists", "task_id": existing.get("task_id")}
    repair_task = new_task(
        role="implementer",
        engine="codex",
        project=task.get("project"),
        summary=f"Repair QA contract for {task.get('project')}",
        source=source,
        braid_template="orchestrator-self-repair" if task.get("project") == "devmini-orchestrator" else None,
        engine_args={
            "self_repair": {"enabled": task.get("project") == "devmini-orchestrator"},
            "qa_contract_repair": {
                "target_task_id": target_task_id,
                "contract": ((task.get("engine_args") or {}).get("contract") or "smoke"),
                "script": ((task.get("engine_args") or {}).get("script")),
            },
        },
    )
    enqueue_task(repair_task)
    append_event(
        "workflow-check",
        "qa_contract_repair_enqueued",
        task_id=repair_task.get("task_id"),
        details={"target_task_id": target_task_id, "project": task.get("project")},
    )
    return {"enqueued": True, "task_id": repair_task.get("task_id")}


def _synthesize_task_from_transitions(task_id):
    rows = read_transitions(task_id=task_id)
    if not rows:
        return None
    final = rows[-1]
    state = final.get("to_state")
    if state not in STATES:
        return None
    task = {
        "task_id": task_id,
        "state": state,
        "reconstructed_from": "transition_log",
        "reconstructed_at": now_iso(),
    }
    if state in ("done", "failed", "abandoned", "blocked"):
        task["finished_at"] = final.get("ts")
    return task


def _recover_missing_child_task(task_id):
    task = _synthesize_task_from_transitions(task_id)
    if not task:
        return {"recovered": False, "reason": "transition_log_missing"}
    _write_task_record(task, task["state"])
    append_event(
        "workflow-check",
        "missing_child_recovered",
        task_id=task_id,
        details={"state": task["state"]},
    )
    return {"recovered": True, "state": task["state"]}


def _abandon_feature_missing_child(feature_id, task_id):
    blocker = make_blocker(
        "missing_child_unrecoverable",
        summary="feature child task could not be reconstructed",
        detail=f"child task {task_id} is missing from queue state and transition log",
        source="workflow-check",
        retryable=False,
    )
    def mut(feature):
        feature["status"] = "abandoned"
        feature["finished_at"] = now_iso()
        feature["blocker"] = blocker
    update_feature(feature_id, mut)
    append_event(
        "workflow-check",
        "missing_child_unrecoverable",
        feature_id=feature_id,
        details={"task_id": task_id},
    )
    return {"abandoned": True, "blocker_code": blocker["code"]}


def _project_green_regression_after(project_name, failed_at):
    for task in iter_tasks(states=("done",), project=project_name, role="qa"):
        if task.get("project") != project_name or task.get("role") != "qa":
            continue
        if (task.get("finished_at") or "") > (failed_at or ""):
            return True
    return False


def _project_human_push_after(project, failed_at):
    repo_path = pathlib.Path(project["path"])
    if not repo_path.exists():
        return False
    ok, detail = _git_ok(repo_path, "log", "-1", "--format=%cI", "main")
    return bool(ok and (detail or "") > (failed_at or ""))


def _clear_regression_hard_stop(task, state, project, *, cleared_by):
    reason = f"workflow-check: regression cleared via {cleared_by}"
    if state in ("blocked", "failed"):
        move_task(
            task["task_id"],
            state,
            "abandoned",
            reason=reason,
            mutator=lambda t: t.update(
                {
                    "finished_at": now_iso(),
                    "abandoned_reason": reason,
                    "regression_cleared_by": cleared_by,
                }
            ),
        )
    clear_project_hard_stop(project["name"], cleared_by=cleared_by, detail=reason)
    append_event(
        "workflow-check",
        "project_regression_cleared",
        task_id=task.get("task_id"),
        details={"project": project["name"], "cleared_by": cleared_by},
    )
    return True


def _workflow_policy_matches(policy, issue, task, project):
    if policy.get("kind") and issue.get("kind") != policy["kind"]:
        return False
    if policy.get("task_state") and issue.get("task_state") != policy["task_state"]:
        return False
    blocker = issue.get("blocker") or {}
    if policy.get("blocker_code") and blocker.get("code") != policy["blocker_code"]:
        return False
    when = policy.get("when")
    if when == "template_missing_blocked":
        tmpl = task.get("braid_template") if task else None
        return not (tmpl and (BRAID_TEMPLATES / f"{tmpl}.mmd").exists())
    if when == "template_missing_ready":
        tmpl = task.get("braid_template") if task else None
        return bool(tmpl and (BRAID_TEMPLATES / f"{tmpl}.mmd").exists())
    if when == "template_refine_waiting":
        req = task.get("refine_request") or {}
        refine_task_id = req.get("refine_task_id")
        if not refine_task_id:
            return True
        refine = find_task(refine_task_id)
        return not (refine and refine[0] == "done")
    if when == "template_refine_ready":
        req = task.get("refine_request") or {}
        refine_task_id = req.get("refine_task_id")
        if not refine_task_id:
            return False
        refine = find_task(refine_task_id)
        return bool(refine and refine[0] == "done")
    if when == "template_graph_waiting":
        tmpl = task.get("braid_template") if task else None
        if not tmpl:
            return True
        _, current_hash = braid_template_load(tmpl)
        previous_hash = task.get("braid_template_hash")
        return not (current_hash and current_hash != previous_hash)
    if when == "template_graph_ready":
        tmpl = task.get("braid_template") if task else None
        if not tmpl:
            return False
        _, current_hash = braid_template_load(tmpl)
        previous_hash = task.get("braid_template_hash")
        return bool(current_hash and current_hash != previous_hash)
    if when == "review_feedback_stale":
        source = str((task or {}).get("source") or "")
        if not source.startswith("review-feedback:"):
            return False
        feature_id = (task or {}).get("feature_id")
        if feature_id:
            feature = read_feature(feature_id)
            if feature and feature.get("status") == "finalizing":
                return True
        target_id = ((task or {}).get("engine_args") or {}).get("target_task_id") or task.get("parent_task_id")
        if not target_id:
            return False
        current_id = task.get("task_id")
        current_created = task.get("created_at") or ""
        for sibling in iter_tasks(states=STATES):
            if sibling.get("task_id") == current_id:
                continue
            if sibling.get("feature_id") != feature_id:
                continue
            sibling_source = str(sibling.get("source") or "")
            sibling_target = ((sibling.get("engine_args") or {}).get("target_task_id") or sibling.get("parent_task_id"))
            if sibling_source.startswith("review-feedback:") and sibling_target == target_id:
                if (sibling.get("created_at") or "") > current_created:
                    return True
        return False
    if when == "project_main_clean":
        repo = repo_status(project["path"])
        return bool(repo.get("exists") and not repo.get("dirty"))
    if when == "project_main_still_dirty":
        repo = repo_status(project["path"])
        return not (repo.get("exists") and not repo.get("dirty"))
    if when == "project_regression_green":
        failed_at = (task or {}).get("finished_at") or (task or {}).get("updated_at") or ""
        return _project_green_regression_after(project["name"], failed_at)
    if when == "project_regression_human_push":
        failed_at = (task or {}).get("finished_at") or (task or {}).get("updated_at") or ""
        return _project_human_push_after(project, failed_at)
    if when == "control_plane_feedback_bug":
        source = str((task or {}).get("source") or "")
        detail = str((issue.get("blocker") or {}).get("detail") or "")
        if not (
            source.startswith("review-feedback:")
            or source.startswith("pr-feedback:")
            or "unresolved_review_threads" in detail
            or "github_thread_resolution" in detail
        ):
            return False
        return True
    if when == "qa_target_relocated":
        detail = str((issue.get("blocker") or {}).get("detail") or "")
        m = re.search(r"target ([A-Za-z0-9_-]+) ", detail)
        if not m:
            return False
        found = find_task(m.group(1), states=("queued", "awaiting-review", "awaiting-qa", "done", "failed"))
        if not found:
            return False
        state, _ = found
        return state != "queued"
    if when == "qa_target_still_missing":
        detail = str((issue.get("blocker") or {}).get("detail") or "")
        if "review-feedback: target " not in detail and "pr-feedback: target " not in detail:
            return False
        m = re.search(r"target ([A-Za-z0-9_-]+) ", detail)
        if not m:
            return False
        found = find_task(m.group(1), states=("queued", "awaiting-review", "awaiting-qa", "done", "failed"))
        if not found:
            self_repair = get_project(load_config(), "devmini-orchestrator")
            return not _project_has_open_feature(self_repair["name"])
        state, _ = found
        if state == "queued":
            return False
        self_repair = get_project(load_config(), "devmini-orchestrator")
        return not _project_has_open_feature(self_repair["name"])
    return True


def _workflow_policy_decision(issue, task, project):
    blocker = issue.get("blocker") or {}
    detail = (blocker.get("detail") or "").strip()
    summary = (blocker.get("summary") or "").strip()
    for policy in WORKFLOW_REPAIR_POLICY:
        if _workflow_policy_matches(policy, issue, task, project):
            diagnosis = policy.get("diagnosis") or detail or summary or issue.get("diagnosis") or "workflow issue detected"
            return policy.get("action"), diagnosis, policy.get("name")
    if issue.get("task_state") in ("failed", "blocked"):
        return None, detail or summary or "task is blocked without a known automatic fix", None
    return None, issue.get("diagnosis") or "workflow issue detected", None


def _issue_with_policy(issue, task, project):
    action, diagnosis, policy = _workflow_policy_decision(issue, task, project)
    issue["action"] = action
    issue["diagnosis"] = diagnosis
    issue["policy"] = policy
    return issue


def _workflow_check_known_task_action(task, state, project):
    blocker = task_blocker(task)
    issue = {"kind": "frontier_task_blocked", "task_state": state, "blocker": blocker}
    action, diagnosis, _ = _workflow_policy_decision(issue, task, project)
    return action, diagnosis


def _latest_dead_finalizing_follow_up(feature_id, feedback_task_id=None):
    follow_up = None
    if feedback_task_id:
        found = find_task(feedback_task_id)
        if found and found[0] in ("failed", "blocked"):
            return {"task_id": feedback_task_id, "state": found[0], "task": found[1]}

    items = []
    for item in _feature_related_tasks(feature_id):
        task = item.get("task") or {}
        if task.get("role") == "planner":
            continue
        if item.get("state") not in ("failed", "blocked"):
            continue
        if _follow_up_repair_key(item) is None:
            continue
        items.append(item)
    items = _suppress_superseded_follow_ups(items)
    items.sort(
        key=lambda item: (
            (item.get("task") or {}).get("created_at") or "",
            item.get("task_id") or "",
        ),
        reverse=True,
    )
    return items[0] if items else None


def _workflow_pr_snapshot(project, feature):
    snap = _gh_pr_snapshot(project["path"], feature["final_pr_number"])
    if not snap.get("error"):
        return snap

    sweep = dict(feature.get("final_pr_sweep") or {})
    mergeable = sweep.get("last_mergeable") or ""
    merge_state = sweep.get("last_merge_state") or ""
    review_decision = sweep.get("last_review_decision") or ""
    if not any((mergeable, merge_state, review_decision, sweep.get("last_checked_at"))):
        return snap
    return {
        "state": "OPEN",
        "mergeable": mergeable,
        "mergeStateStatus": merge_state,
        "reviewDecision": review_decision,
        "statusCheckRollup": [],
        "unresolved_bot_threads": sweep.get("unresolved_bot_threads"),
        "cached": True,
        "error": snap.get("error"),
    }


def _workflow_issue_from_summary(feature, workflow, config):
    project = get_project(config, feature["project"])
    feature_id = feature.get("feature_id")
    feature_status = feature.get("status")
    frontier = workflow.get("frontier") or {}
    planner = workflow.get("planner") or {}
    canary = workflow.get("canary") or {}
    canary_cfg = config.get("synthetic_canary") or {}
    canary_stale_sec = int(float(canary_cfg.get("max_frontier_age_hours", 2) or 2) * 3600)

    if feature_status == "open":
        if not feature.get("child_task_ids"):
            if planner.get("task_id") and planner.get("state"):
                found = find_task(planner["task_id"])
                if found:
                    state, task = found
                else:
                    state, task = planner.get("state"), None
                blocker = task_blocker(task)
                if task is not None and state in ("failed", "blocked"):
                    action, diagnosis = _workflow_check_known_task_action(task, state, project)
                    return {
                        "feature_id": feature_id,
                        "project": project["name"],
                        "summary": feature.get("summary") or feature_id,
                        "issue_key": f"planner:{task['task_id']}:{state}:{(blocker or {}).get('code') or task.get('failure') or task.get('topology_error') or ''}",
                        "kind": "planner_blocked",
                        "task_id": task["task_id"],
                        "task_state": state,
                        "blocker": blocker,
                        "diagnosis": diagnosis,
                        "workflow": workflow,
                        "action": action,
                        "task": task,
                    }
                if task is not None and state == "done":
                    log_excerpt = _recent_text_lines(task.get("log_path"), limit=40) if task.get("log_path") else ""
                    if "decomposed into 0" in log_excerpt or "# dropped " in log_excerpt:
                        issue = {
                            "feature_id": feature_id,
                            "project": project["name"],
                            "summary": feature.get("summary") or feature_id,
                            "issue_key": f"planner-empty:{task['task_id']}",
                            "kind": "planner_emitted_no_children",
                            "task_id": task["task_id"],
                            "task_state": state,
                            "blocker": make_blocker("planner_emitted_no_children", summary="planner emitted no runnable slices", detail="planner completed without emitting any runnable slices", source="workflow-check", retryable=True),
                            "workflow": workflow,
                            "task": task,
                        }
                        issue["action"], issue["diagnosis"], issue["policy"] = _workflow_policy_decision(issue, task, project)
                        return {
                            **issue,
                        }
            issue = {
                "feature_id": feature_id,
                "project": project["name"],
                "summary": feature.get("summary") or feature_id,
                "issue_key": f"feature-no-children:{feature_id}",
                "kind": "feature_has_no_children",
                "task_id": None,
                "task_state": "open",
                "blocker": make_blocker("feature_has_no_children", summary="feature has no child tasks", detail="feature is open but has no child tasks to make progress", source="workflow-check", retryable=False),
                "diagnosis": "feature is open but has no child tasks to make progress",
                "workflow": workflow,
                "task": None,
            }
            return _issue_with_policy(issue, None, project)

        if _feature_ready_for_finalize(feature):
            issue = {
                "feature_id": feature_id,
                "project": project["name"],
                "summary": feature.get("summary") or feature_id,
                "issue_key": f"ready-for-finalize:{feature_id}",
                "kind": "ready_for_finalize",
                "task_id": None,
                "task_state": "done",
                "blocker": None,
                "workflow": workflow,
                "task": None,
            }
            issue["action"], issue["diagnosis"], issue["policy"] = _workflow_policy_decision(issue, None, project)
            return {
                **issue,
            }

    if feature_status == "finalizing" and feature.get("final_pr_number"):
        snap = _workflow_pr_snapshot(project, feature)
        merge_state = snap.get("mergeStateStatus", "")
        mergeable = snap.get("mergeable", "")
        review_decision = snap.get("reviewDecision", "")
        sweep = dict(feature.get("final_pr_sweep") or {})
        if (
            sweep.get("merge_ready_at")
            and not sweep.get("feedback_task_id")
            and not sweep.get("last_feedback_task_id")
            and mergeable == "MERGEABLE"
            and merge_state == "CLEAN"
            and review_decision != "CHANGES_REQUESTED"
        ):
            return None
        if snap.get("state") == "OPEN" and (
            merge_state in ("BLOCKED", "UNSTABLE", "DIRTY", "BEHIND")
            or mergeable == "CONFLICTING"
            or review_decision == "CHANGES_REQUESTED"
        ):
            issue = {
                "feature_id": feature_id,
                "project": project["name"],
                "summary": feature.get("summary") or feature_id,
                "issue_key": f"final-pr:{feature.get('final_pr_number')}:{merge_state}:{review_decision}",
                "kind": "final_pr_blocked",
                "task_id": None,
                "task_state": "finalizing",
                "blocker": make_blocker(
                    "final_pr_blocked",
                    summary="final PR is blocked",
                    detail=(
                        f"mergeStateStatus={merge_state or '-'}, "
                        f"mergeable={mergeable or '-'}, reviewDecision={review_decision or '-'}"
                    ),
                    source="workflow-check",
                    retryable=True,
                ),
                "workflow": workflow,
                "task": None,
            }
            issue["action"], issue["diagnosis"], issue["policy"] = _workflow_policy_decision(issue, None, project)
            return {
                **issue,
            }
        if (
            snap.get("state") == "OPEN"
            and mergeable == "MERGEABLE"
            and merge_state == "CLEAN"
            and review_decision != "CHANGES_REQUESTED"
        ):
            follow_up = _latest_dead_finalizing_follow_up(
                feature_id,
                sweep.get("feedback_task_id") or sweep.get("last_feedback_task_id"),
            )
            if follow_up:
                task = follow_up.get("task")
                blocker = task_blocker(task or {})
                issue = {
                    "feature_id": feature_id,
                    "project": project["name"],
                    "summary": feature.get("summary") or feature_id,
                    "issue_key": f"finalize-follow-up-dead:{feature_id}:{follow_up.get('task_id')}:{follow_up.get('state')}",
                    "kind": "finalize_follow_up_dead",
                    "task_id": follow_up.get("task_id"),
                    "task_state": follow_up.get("state"),
                    "blocker": make_blocker(
                        "finalize_follow_up_dead",
                        summary="clean final PR is blocked by a dead follow-up lane",
                        detail=(
                            f"final PR #{feature.get('final_pr_number')} is CLEAN/MERGEABLE but "
                            f"follow-up task {follow_up.get('task_id')} is {follow_up.get('state')}"
                            + (f" ({(blocker or {}).get('code')})" if blocker else "")
                        ),
                        source="workflow-check",
                        retryable=True,
                        metadata={"follow_up_task_id": follow_up.get("task_id")},
                    ),
                    "diagnosis": "final PR is mergeable but the last repair lane died; surface merge readiness and retire the dead follow-up",
                    "workflow": workflow,
                    "task": task,
                }
                return _issue_with_policy(issue, task, project)

    if frontier.get("state") == "missing" and frontier.get("task_id"):
        child_id = frontier["task_id"]
        if not find_task(child_id):
            issue = {
                "feature_id": feature_id,
                "project": project["name"],
                "summary": feature.get("summary") or feature_id,
                "issue_key": f"missing-child:{child_id}",
                "kind": "missing_child",
                "task_id": child_id,
                "task_state": "missing",
                "blocker": make_blocker("missing_child", summary="feature child task file missing", detail="feature child task file is missing from the queue state tree", source="workflow-check", retryable=False),
                "diagnosis": "feature child task file is missing from the queue state tree",
                "workflow": workflow,
                "task": None,
            }
            return _issue_with_policy(issue, None, project)
    if frontier.get("task_id") and frontier.get("state") in ("failed", "blocked", "abandoned"):
        found = find_task(frontier["task_id"])
        if found:
            state, task = found
            blocker = task_blocker(task)
            action, diagnosis = _workflow_check_known_task_action(task, state, project)
            return {
                "feature_id": feature_id,
                "project": project["name"],
                "summary": feature.get("summary") or feature_id,
                "issue_key": f"task:{task['task_id']}:{state}:{(blocker or {}).get('code') or task.get('failure') or task.get('topology_error') or ''}",
                "kind": "frontier_task_blocked",
                "task_id": task["task_id"],
                "task_state": state,
                "blocker": blocker,
                "diagnosis": diagnosis,
                "workflow": workflow,
                "action": action,
                "task": task,
            }
    if feature_status == "open":
        if frontier.get("task_id") and frontier.get("state") not in (None, "done"):
            if frontier.get("state") == "awaiting-review" and (frontier.get("blocker") or {}).get("code"):
                found = find_task(frontier.get("task_id"))
                task = found[1] if found else None
                issue = {
                    "feature_id": feature_id,
                    "project": project["name"],
                    "summary": feature.get("summary") or feature_id,
                    "issue_key": f"task:{frontier.get('task_id')}:{frontier.get('state')}:{(frontier.get('blocker') or {}).get('code')}",
                    "kind": "frontier_task_blocked",
                    "task_id": frontier.get("task_id"),
                    "task_state": frontier.get("state"),
                    "blocker": frontier.get("blocker"),
                    "workflow": workflow,
                    "task": task,
                }
                issue["action"], issue["diagnosis"], issue["policy"] = _workflow_policy_decision(issue, task, project)
                if issue.get("action") or (frontier.get("blocker") or {}).get("code") == "review_gate_protocol_error":
                    return issue
            if frontier.get("state") == "awaiting-review" and (frontier.get("age_seconds") or 0) >= 6 * 3600:
                return {
                    "feature_id": feature_id,
                    "project": project["name"],
                    "summary": feature.get("summary") or feature_id,
                    "issue_key": f"awaiting-review-stale:{feature_id}:{frontier.get('task_id')}",
                    "kind": "awaiting_review_stale",
                    "task_id": frontier.get("task_id"),
                    "task_state": frontier.get("state"),
                    "blocker": make_blocker(
                        "runtime_precondition_failed",
                        summary="awaiting-review task is stale",
                        detail=(
                            f"frontier {frontier.get('task_id') or '-'} has stayed awaiting-review for "
                            f"{frontier.get('age_text') or '-'}"
                        ),
                        source="workflow-check",
                        retryable=True,
                    ),
                    "diagnosis": "awaiting-review frontier exceeded 6h SLA; requeue reviewer sweep",
                    "workflow": workflow,
                    "action": "enqueue_reviewer",
                    "task": None,
                    "policy": "awaiting_review_sla",
                }
            if frontier.get("state") == "awaiting-qa" and (frontier.get("age_seconds") or 0) >= 6 * 3600:
                return {
                    "feature_id": feature_id,
                    "project": project["name"],
                    "summary": feature.get("summary") or feature_id,
                    "issue_key": f"awaiting-qa-stale:{feature_id}:{frontier.get('task_id')}",
                    "kind": "awaiting_qa_stale",
                    "task_id": frontier.get("task_id"),
                    "task_state": frontier.get("state"),
                    "blocker": make_blocker(
                        "runtime_precondition_failed",
                        summary="awaiting-qa task is stale",
                        detail=(
                            f"frontier {frontier.get('task_id') or '-'} has stayed awaiting-qa for "
                            f"{frontier.get('age_text') or '-'}"
                        ),
                        source="workflow-check",
                        retryable=True,
                    ),
                    "diagnosis": "awaiting-qa frontier exceeded 6h SLA; requeue QA sweep",
                    "workflow": workflow,
                    "action": "enqueue_qa",
                    "task": None,
                    "policy": "awaiting_qa_sla",
                }
            if (
                canary.get("enabled")
                and frontier.get("age_seconds") is not None
                and frontier.get("age_seconds", 0) >= canary_stale_sec
            ):
                issue = {
                    "feature_id": feature_id,
                    "project": project["name"],
                    "summary": feature.get("summary") or feature_id,
                    "issue_key": f"canary-stale:{feature_id}:{frontier.get('task_id')}:{frontier.get('state')}",
                    "kind": "canary_stale",
                    "task_id": frontier.get("task_id"),
                    "task_state": frontier.get("state"),
                    "blocker": make_blocker(
                        "canary_stale",
                        summary="synthetic canary is stale",
                        detail=(
                            f"frontier {frontier.get('task_id') or '-'} has stayed "
                            f"{frontier.get('state') or '-'} for {frontier.get('age_text') or '-'}"
                        ),
                        source="workflow-check",
                        retryable=False,
                    ),
                    "workflow": workflow,
                    "task": None,
                }
                issue["action"], issue["diagnosis"], issue["policy"] = _workflow_policy_decision(issue, None, project)
                return issue
            return None

    return None


def _write_workflow_check_report(issues, *, reaped=0):
    ts = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"workflow-check_{ts}.md"
    fingerprint_rows = []
    for issue in issues:
        blocker = issue.get("blocker") or {}
        frontier = (issue.get("workflow") or {}).get("frontier") or {}
        fingerprint_rows.append({
            "feature_id": issue.get("feature_id"),
            "project": issue.get("project"),
            "summary": issue.get("summary"),
            "kind": issue.get("kind"),
            "issue_key": issue.get("issue_key"),
            "task_id": issue.get("task_id"),
            "task_state": issue.get("task_state"),
            "blocker_code": blocker.get("code"),
            "diagnosis": issue.get("diagnosis"),
            "policy": issue.get("policy"),
            "action": issue.get("action"),
            "frontier_reason": frontier.get("reason"),
        })
    fingerprint = hashlib.sha256(
        json.dumps(sorted(fingerprint_rows, key=lambda row: row.get("issue_key") or ""), sort_keys=True).encode("utf-8")
    ).hexdigest()
    lines = [
        "# Workflow Check Report",
        "",
        f"Generated: {now_iso()}",
        f"Issues found: {len(issues)}",
        f"Reaper recoveries run first: {reaped}",
        f"Fingerprint: `{fingerprint}`",
        "",
    ]
    for issue in issues:
        lines += [
            f"## {issue['feature_id']} — {issue['project']}",
            "",
            f"- summary: `{issue['summary']}`",
            f"- kind: `{issue['kind']}`",
            f"- diagnosis: {issue['diagnosis']}",
        ]
        if (issue.get("workflow") or {}).get("canary", {}).get("enabled"):
            lines.append("- canary: `true`")
        blocker = issue.get("blocker") or {}
        if blocker.get("code"):
            lines.append(f"- blocker: `{blocker['code']}` ({blocker.get('confidence') or '-'})")
            if blocker.get("source"):
                lines.append(f"- blocker source: `{blocker['source']}`")
        if issue.get("task_id"):
            lines.append(f"- task: `{issue['task_id']}` ({issue.get('task_state')})")
        wf = issue.get("workflow") or {}
        frontier = wf.get("frontier") or {}
        if frontier.get("entered_at"):
            lines.append(f"- frontier entered current state: `{frontier['entered_at']}`")
        if frontier.get("age_text"):
            lines.append(f"- frontier age: `{frontier['age_text']}`")
        if frontier.get("reason"):
            lines.append(f"- frontier transition reason: {frontier['reason']}")
        if frontier.get("attempt") and frontier.get("attempt", 1) > 1:
            lines.append(f"- frontier attempt: `{frontier['attempt']}`")
        repairs = wf.get("repair_history") or []
        if repairs:
            evt = repairs[-1]
            lines.append(
                f"- last repair activity: `{evt.get('ts') or '-'}` {evt.get('role') or '-'}:{evt.get('event') or '-'}"
            )
        if issue.get("policy"):
            lines.append(f"- repair policy: `{issue['policy']}`")
        if issue.get("action"):
            lines.append(f"- action: `{issue['action']}`")
        lines.append(f"- attempts: {issue.get('attempts', 0)}/{issue.get('max_attempts', 0)}")
        lines.append(f"- outcome: {issue.get('outcome', 'no action recorded')}")
        lines.append("")
    out.write_text("\n".join(lines))
    append_event("workflow-check", "report_written", details={"path": str(out), "issues": len(issues)})
    return out


def _canary_freshness_issue(cfg):
    canary_cfg = cfg.get("synthetic_canary") or {}
    if not canary_cfg.get("enabled"):
        return None
    project_name = canary_cfg.get("project")
    try:
        project = get_project(cfg, project_name)
    except KeyError:
        return None
    if _project_has_open_feature(project_name) and not list_canary_features(project_name=project_name, statuses=("open", "finalizing")):
        return None
    if list_canary_features(project_name=project_name, statuses=("open", "finalizing")):
        return None
    overdue, last = _canary_success_overdue(cfg, project_name)
    if not overdue:
        return None
    anchor = _latest_canary_feature(project_name) or last or {}
    issue = {
        "feature_id": anchor.get("feature_id") or f"canary:{project_name}",
        "project": project_name,
        "summary": canary_cfg.get("summary") or "Synthetic workflow canary",
        "issue_key": f"canary-freshness:{project_name}",
        "kind": "canary_missing_recent_success",
        "task_id": None,
        "task_state": "idle",
        "blocker": make_blocker(
            "canary_missing_recent_success",
            summary="recent successful canary missing",
            detail=(
                f"no merged canary within {canary_cfg.get('success_sla_hours', 24)}h"
                if last is not None else
                "no successful canary has been recorded yet"
            ),
            source="workflow-check",
            retryable=True,
        ),
        "workflow": {
            "canary": {"enabled": True, "project": project_name},
            "recent_events": [],
            "repair_history": [],
        },
        "task": None,
    }
    issue["action"], issue["diagnosis"], issue["policy"] = _workflow_policy_decision(issue, None, project)
    return issue


def _enqueue_canary_fallback(issue, cfg):
    canary_cfg = cfg.get("synthetic_canary") or {}
    primary = issue.get("project")
    fallback = (canary_cfg.get("fallback_project") or "").strip()
    if not fallback:
        return {"enqueued": 0, "reason": "no_fallback_project"}
    overdue, _ = _canary_success_overdue(cfg, fallback)
    if overdue:
        alert_path = _write_workflow_alert(
            {
                **issue,
                "blocker": make_blocker(
                    "canary_unrecoverable",
                    summary="primary and fallback canary are both stale",
                    detail=f"primary={primary} fallback={fallback}",
                    source="workflow-check",
                    retryable=False,
                ),
            },
            f"synthetic canary fallback also stale: primary={primary} fallback={fallback}",
        )
        return {"enqueued": 0, "reason": "fallback_stale", "blocker_code": "canary_unrecoverable", "alert": bool(alert_path)}
    return tick_canary_workflows(force=True, project_override=fallback, fallback_from=primary)


def _environment_health_issues():
    issues = []
    for issue in environment_health(refresh=True).get("issues", []):
        if issue.get("severity") != "error":
            continue
        envelope = {
            "feature_id": f"env:{issue.get('project') or 'global'}",
            "project": issue.get("project") or "global",
            "summary": issue.get("summary") or "environment degraded",
            "issue_key": f"env:{issue.get('project') or 'global'}:{issue.get('code')}:{issue.get('summary')}",
            "kind": "environment_degraded",
            "task_id": None,
            "task_state": "idle",
            "blocker": make_blocker(
                issue.get("code") or "runtime_env_dirty",
                summary=issue.get("summary"),
                detail=issue.get("detail"),
                source="environment-health",
                retryable=False,
            ),
            "diagnosis": issue.get("detail") or issue.get("summary") or "environment degraded",
            "workflow": {},
            "task": None,
            "attempts": 0,
            "max_attempts": 0,
            "outcome": "diagnosed only",
        }
        pseudo_project = {"name": issue.get("project") or "global", "path": get_project(load_config(), issue["project"])["path"] if issue.get("project") and any(p["name"] == issue["project"] for p in load_config().get("projects", [])) else str(STATE_ROOT)}
        envelope["action"], envelope["diagnosis"], envelope["policy"] = _workflow_policy_decision(envelope, None, pseudo_project)
        issues.append(envelope)
    return issues


def tick_workflow_check():
    cfg = load_config()
    max_attempts = int(cfg.get("workflow_check_max_attempts", 3))
    write_agent_status("workflow-check", "running", "Scanning feature workflows for blockers.")
    emit_runtime_metrics_snapshot(source="workflow-check")

    reaped = reap()
    issues = []
    action_cache = {}

    def cached_action(name, fn):
        if name not in action_cache:
            action_cache[name] = fn()
        return action_cache[name]

    for env_issue in _environment_health_issues():
        outcome = "diagnosed only"
        if env_issue.get("action") == "repair_environment":
            repair = cached_action("repair_environment", repair_environment)
            still_present = any(
                row.get("issue_key") == env_issue.get("issue_key")
                for row in _environment_health_issues()
            )
            if still_present:
                out = cached_action(
                    "self_repair:environment",
                    lambda: enqueue_self_repair(
                        summary=_workflow_issue_self_repair_summary(env_issue),
                        evidence=_workflow_issue_self_repair_evidence(env_issue),
                        issue_kind=env_issue.get("kind") or "environment_degraded",
                        source="workflow-check",
                        issue_key=env_issue.get("issue_key"),
                    ),
                )
                outcome = (
                    f"environment repair attempted={repair.get('attempted', 0)} "
                    f"remaining={repair.get('remaining_error_count', 0)}; "
                    f"self-repair result: {json.dumps(out, sort_keys=True)}"
                )
            else:
                outcome = (
                    f"environment repair attempted={repair.get('attempted', 0)} "
                    f"remaining={repair.get('remaining_error_count', 0)}"
                )
        env_issue["outcome"] = outcome
        issues.append(env_issue)

    canary_issue = _canary_freshness_issue(cfg)
    if canary_issue:
        attempts = 0
        if canary_issue["feature_id"].startswith("feature-"):
            feature = read_feature(canary_issue["feature_id"])
            if feature:
                attempts = _workflow_check_attempts(feature, canary_issue["issue_key"])
        canary_issue["attempts"] = attempts
        canary_issue["max_attempts"] = max_attempts
        outcome = "diagnosed only"
        if canary_issue.get("action") and attempts < max_attempts:
            if canary_issue["action"] == "enqueue_canary":
                out = cached_action("enqueue_canary", lambda: tick_canary_workflows(force=True))
                outcome = f"canary scheduler result: {json.dumps(out, sort_keys=True)}"
            else:
                outcome = f"unknown action {canary_issue['action']}"
            if canary_issue["feature_id"].startswith("feature-"):
                _workflow_check_record_attempt(canary_issue["feature_id"], canary_issue["issue_key"], canary_issue["action"], outcome)
                canary_issue["attempts"] = attempts + 1
        elif canary_issue.get("action"):
            outcome = "automatic attempts exhausted"
        canary_issue["outcome"] = outcome
        issues.append(canary_issue)

    for feature in list_features():
        if feature.get("status") not in ("open", "finalizing"):
            continue
        workflow = feature_workflow_summary(feature)
        issue = _workflow_issue_from_summary(feature, workflow, cfg)
        if not issue:
            continue
        attempts = _workflow_check_attempts(feature, issue["issue_key"])
        issue["attempts"] = attempts
        issue["max_attempts"] = max_attempts
        outcome = "diagnosed only"
        project = get_project(cfg, issue["project"])

        if issue.get("action") and attempts < max_attempts:
            action = issue["action"]
            if action == "retry_task":
                ok = _workflow_check_retry_task(
                    issue["task"],
                    issue["task_state"],
                    reason=f"workflow-check: {issue['diagnosis'][:120]}",
                )
                outcome = "task requeued" if ok else "retry failed"
            elif action == "abandon_task":
                ok = _workflow_check_abandon_task(
                    issue["task"],
                    issue["task_state"],
                    reason=f"workflow-check: {issue['diagnosis'][:120]}",
                )
                outcome = "task abandoned" if ok else "abandon failed"
            elif action == "restart_workers_then_retry":
                restarted, failed = cached_action(
                    "restart_workers",
                    _workflow_check_restart_workers,
                )
                ok = _workflow_check_retry_task(
                    issue["task"],
                    issue["task_state"],
                    reason="workflow-check: worker runtime reloaded, retrying task",
                )
                outcome = (
                    f"workers restarted={len(restarted)} failed={len(failed)}; "
                    f"{'task requeued' if ok else 'retry failed'}"
                )
            elif action == "feature_finalize":
                checked, opened, abandoned, skipped = cached_action(
                    "feature_finalize",
                    lambda: feature_finalize(dry_run=False),
                )
                refreshed = read_feature(feature["feature_id"]) or feature
                if refreshed.get("status") != "open":
                    outcome = (
                        f"feature-finalize ran: checked={checked} opened={opened} "
                        f"abandoned={abandoned} skipped={skipped}; feature now {refreshed.get('status')}"
                    )
                else:
                    outcome = (
                        f"feature-finalize ran: checked={checked} opened={opened} "
                        f"abandoned={abandoned} skipped={skipped}; feature still open"
                    )
            elif action == "pr_sweep":
                checked, merged, fb, alerted, skipped = cached_action(
                    "pr_sweep",
                    lambda: pr_sweep(dry_run=False),
                )
                outcome = (
                    f"pr-sweep ran: checked={checked} merged={merged} "
                    f"feedback={fb} alerted={alerted} skipped={skipped}"
                )
            elif action == "enqueue_canary":
                out = cached_action(
                    "enqueue_canary",
                    lambda: tick_canary_workflows(force=True),
                )
                outcome = f"canary scheduler result: {json.dumps(out, sort_keys=True)}"
            elif action == "enqueue_canary_fallback":
                out = cached_action(
                    f"canary_fallback:{issue.get('project')}",
                    lambda: _enqueue_canary_fallback(issue, cfg),
                )
                outcome = f"canary fallback result: {json.dumps(out, sort_keys=True)}"
            elif action == "enqueue_reviewer":
                cached_action("tick_reviewer", tick_reviewer)
                outcome = "reviewer sweep enqueued"
            elif action == "enqueue_qa":
                cached_action("tick_qa", tick_qa)
                outcome = "qa sweep enqueued"
            elif action == "repair_main_then_retry":
                repair = cached_action(
                    f"repair_main:{issue.get('project')}",
                    lambda: _repair_project_main_checkout(project),
                )
                ok = False
                if repair.get("fixed") and issue.get("task"):
                    ok = _workflow_check_retry_task(
                        issue["task"],
                        issue["task_state"],
                        reason="workflow-check: project main checkout repaired, retrying task",
                    )
                outcome = (
                    f"project-main repair fixed={repair.get('fixed')} detail={repair.get('detail')}; "
                    f"{'task requeued' if ok else 'retry deferred'}"
                )
            elif action == "enqueue_qa_contract_repair":
                out = cached_action(
                    f"repair_qa:{issue.get('task_id')}",
                    lambda: _enqueue_qa_contract_repair(issue["task"]),
                )
                outcome = f"qa-contract repair task: {json.dumps(out, sort_keys=True)}"
            elif action == "mark_ready_for_merge":
                ok = _workflow_check_mark_ready_for_merge(
                    feature,
                    issue,
                    reason=issue["diagnosis"],
                )
                outcome = "dead follow-up retired; feature marked merge-ready" if ok else "mark-merge-ready failed"
            elif action == "clear_regression_hard_stop":
                cleared_by = "green_run" if _project_green_regression_after(project["name"], (issue.get("task") or {}).get("finished_at")) else "human_push"
                ok = _clear_regression_hard_stop(issue["task"], issue["task_state"], project, cleared_by=cleared_by)
                outcome = f"regression hard-stop cleared via {cleared_by}" if ok else "regression hard-stop clear failed"
            elif action == "recover_missing_child":
                recover = _recover_missing_child_task(issue["task_id"])
                if recover.get("recovered"):
                    outcome = f"missing child reconstructed into {recover.get('state')}"
                else:
                    abandoned = _abandon_feature_missing_child(feature["feature_id"], issue["task_id"])
                    _write_workflow_alert(issue, "missing child could not be reconstructed; feature abandoned")
                    outcome = f"missing child unrecoverable; feature abandoned with {abandoned.get('blocker_code')}"
            elif action == "enqueue_self_repair":
                out = cached_action(
                    f"self_repair:{issue['issue_key']}",
                    lambda: enqueue_self_repair(
                        summary=_workflow_issue_self_repair_summary(issue),
                        evidence=_workflow_issue_self_repair_evidence(issue),
                        issue_kind=issue.get("kind") or "runtime_bug",
                        source="workflow-check",
                        issue_key=issue.get("issue_key"),
                    ),
                )
                outcome = f"self-repair result: {json.dumps(out, sort_keys=True)}"
            elif action == "push_alert":
                alert_path = _write_workflow_alert(issue, issue["diagnosis"])
                outcome = f"operator alert written: {alert_path}" if alert_path else "operator alert already recently sent"
            else:
                outcome = f"unknown action {action}"

            _workflow_check_record_attempt(feature["feature_id"], issue["issue_key"], action, outcome)
            issue["attempts"] = attempts + 1

        elif issue.get("action"):
            outcome = "automatic attempts exhausted"
            blocker_code = ((issue.get("blocker") or {}).get("code") or "")
            if blocker_code == "project_main_dirty":
                set_project_hard_stop(project["name"], "project_main_dirty", "project_main_dirty:repair_exhausted")
                _write_workflow_alert(issue, "project main checkout repair attempts exhausted")
                outcome = "project hard-stopped: project_main_dirty:repair_exhausted"

        issue["outcome"] = outcome
        issues.append(issue)

    self_repair_resolution = tick_self_repair_resolution()
    if self_repair_resolution.get("resolved") or self_repair_resolution.get("stalled"):
        append_event(
            "self-repair-resolution",
            "tick_complete",
            details=self_repair_resolution,
        )

    self_repair_observation = tick_self_repair_observation_window()
    if self_repair_observation.get("checked") or self_repair_observation.get("reopened"):
        append_event(
            "self-repair-observation",
            "tick_complete",
            details=self_repair_observation,
        )

    self_repair_drain = tick_self_repair_queue()
    if self_repair_drain.get("scheduled"):
        append_event(
            "self-repair",
            "queue_drained",
            feature_id=self_repair_drain.get("feature_id"),
            details=self_repair_drain,
        )

    report_path = _write_workflow_check_report(issues, reaped=reaped) if issues else None
    append_metric("workflow_check.issue_count", len(issues), source="workflow-check")
    detail = f"issues={len(issues)} reaped={reaped} report={report_path.name if report_path else '-'}"
    write_agent_status("workflow-check", "idle", detail)
    return {"issues": len(issues), "reaped": reaped, "report": str(report_path) if report_path else None}


def _build_investigation_context(question):
    project = _telegram_guess_project(question)
    pr_number = _telegram_find_pr_number(question)
    task_id = _telegram_find_task_id(question)
    feature_id = _telegram_find_feature_id(question)

    sections = [
        ("QUESTION", question.strip()),
        ("CURRENT_DATE", dt.datetime.now().strftime("%Y-%m-%d %A %H:%M:%S %Z")),
        ("WORKFLOW_SNAPSHOT", _workflow_snapshot()),
        ("ENVIRONMENT_HEALTH", environment_health_text(refresh=True)),
        ("AGENT_STATUSES", _agent_status_snapshot()),
        ("REGRESSION_SCHEDULE", _regression_schedule_snapshot(project["name"] if project else None)),
    ]

    if project:
        sections.append(("PROJECT", project["name"]))
        sections.append(("PLANNER_STATUS", planner_status_text(project_filter=project["name"])))
        next_entry = parse_roadmap_next_todo(
            project["path"],
            skip_ids=assigned_roadmap_entries(project["name"]),
        )
        sections.append(
            ("NEXT_ROADMAP_ENTRY", json.dumps(next_entry, indent=2, sort_keys=True) if next_entry else "null")
        )
        sections.append(("FEATURES", _feature_context_text(project["name"], limit=8)))
        sections.append(("FEATURE_WORKFLOWS", _feature_workflow_context(project["name"], limit=8)))
        sections.append(("FEATURE_DIAGNOSIS", _recent_feature_diagnosis(project["name"], limit=6)))
        sections.append(("RECENT_PROJECT_TASKS", _recent_project_task_context(project["name"], limit=8)))
        sections.append(
            (
                "PROJECT_ROADMAP_HEAD",
                _recent_text_lines(pathlib.Path(project["path"]) / "repo-memory" / "ROADMAP.md", limit=80),
            )
        )

    if task_id:
        sections.append((f"TASK_{task_id}", task_text(task_id)))

    if feature_id:
        feature = read_feature(feature_id)
        sections.append(
            (
                f"FEATURE_{feature_id}",
                json.dumps(feature, indent=2, sort_keys=True) if feature else "(missing feature file)",
            )
        )

    if pr_number is not None:
        repo_path = project["path"] if project else None
        if repo_path is None:
            for cand in load_config().get("projects", []):
                snap = _gh_pr_snapshot(cand["path"], pr_number)
                if "error" not in snap:
                    repo_path = cand["path"]
                    project = cand
                    sections.append(("PR_PROJECT", cand["name"]))
                    sections.append((f"PR_{pr_number}", json.dumps(snap, indent=2, sort_keys=True)))
                    break
            if repo_path is None:
                sections.append((f"PR_{pr_number}", json.dumps({"error": "unable to resolve project for PR"}, indent=2)))
        else:
            sections.append((f"PR_{pr_number}", json.dumps(_gh_pr_snapshot(repo_path, pr_number), indent=2, sort_keys=True)))

    sections.append(
        (
            "CLEANUP_LOG_TAIL",
            _recent_text_lines(pathlib.Path.home() / "Library/Logs/devmini/cleanup-worktrees.stdout.log", limit=40),
        )
    )
    sections.append(
        (
            "TELEGRAM_BOT_LOG_TAIL",
            _recent_text_lines(pathlib.Path.home() / "Library/Logs/devmini/telegram-bot.stderr.log", limit=40),
        )
    )
    return "\n\n".join(f"[{title}]\n{body}" for title, body in sections)


def _investigation_system_prompt():
    return (
        "You are a read-only workflow investigator for the devmini orchestrator.\n"
        "Answer ONLY from the supplied evidence. Do not invent facts. Do not suggest commands unless clearly marked as a next step.\n"
        "Be concise and structured with exactly three sections titled: Answer, Evidence, Next step.\n"
        "Cite exact task ids, feature ids, PR numbers, or file paths when relevant.\n"
        "This answer will be sent over Telegram. Return no more than 2500 characters total, including headings.\n"
        "Keep Evidence to at most 4 short lines. Prefer the highest-signal facts only.\n"
        "Use the CURRENT_DATE and REGRESSION_SCHEDULE sections for any statement about 'today' or weekday. "
        "Do not treat stale agent-status text containing 'today=' as current truth unless its updated_at matches CURRENT_DATE.\n"
        "If the evidence already includes task ids, feature ids, log excerpts, or dropped-slice reasons explaining the issue, state that diagnosis directly.\n"
        "Do not use Next step to tell the user to inspect logs, tasks, or PR state that is already present in the evidence; only recommend follow-on action that remains after your diagnosis."
    )


def _fit_investigation_answer(answer):
    answer = (answer or "").strip()
    if not answer:
        return None
    if len(answer) > 2500:
        answer = answer[:2450].rstrip() + "\n\nNext step\nReply was truncated to fit Telegram."
    return answer


def _claude_investigation_answer(question, context_text):
    claude_bin = next((p for p in CLAUDE_CANDIDATE_PATHS if p and pathlib.Path(p).exists()), None)
    if not claude_bin:
        return None
    system_prompt = _investigation_system_prompt()
    user_prompt = (
        f"Question:\n{question.strip()}\n\n"
        "Evidence follows. Answer strictly from it.\n\n"
        f"{context_text}"
    )
    try:
        proc = subprocess.run(
            [
                claude_bin,
                "-p", user_prompt,
                "--dangerously-skip-permissions",
                "--system-prompt", system_prompt,
                "--output-format", "text",
                "--model", "sonnet",
                "--max-budget-usd", f"{claude_budget_usd('ask'):.2f}",
                "--disallowedTools", "Bash,Read,Write,Edit,Grep,Glob,Agent,WebFetch,WebSearch",
                "--no-session-persistence",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd="/tmp",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _fit_investigation_answer(proc.stdout or "")


def _codex_investigation_answer(question, context_text):
    codex_bin = next((p for p in CODEX_CANDIDATE_PATHS if p and pathlib.Path(p).exists()), None)
    if not codex_bin:
        return None
    prompt = (
        f"{_investigation_system_prompt()}\n\n"
        f"Question:\n{question.strip()}\n\n"
        "Evidence follows. Answer strictly from it.\n\n"
        f"{context_text}"
    )
    with tempfile.TemporaryDirectory(prefix="codex-investigate-") as tmp:
        out_path = pathlib.Path(tmp) / "answer.txt"
        try:
            proc = subprocess.run(
                [
                    codex_bin,
                    "exec",
                    "-m", "gpt-5.4-mini",
                    "--skip-git-repo-check",
                    "--cd", "/tmp",
                    "--sandbox", "read-only",
                    "--output-last-message", str(out_path),
                    prompt,
                ],
                capture_output=True,
                text=True,
                timeout=90,
                cwd="/tmp",
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not out_path.exists():
            return None
        return _fit_investigation_answer(out_path.read_text(errors="replace"))


def _synthesize_investigation_answers(question, context_text, claude_answer, codex_answer):
    synthesizer = _claude_investigation_answer if next((p for p in CLAUDE_CANDIDATE_PATHS if p and pathlib.Path(p).exists()), None) else _codex_investigation_answer
    synthesis_question = (
        f"{question.strip()}\n\n"
        "You are synthesizing two investigator answers over the same evidence. "
        "Produce one final answer, using the evidence and preferring the more precise claims when they differ."
    )
    synth_context = (
        f"{context_text}\n\n"
        "[CLAUDE_DRAFT]\n"
        f"{claude_answer or '(missing)'}\n\n"
        "[CODEX_DRAFT]\n"
        f"{codex_answer or '(missing)'}"
    )
    return synthesizer(synthesis_question, synth_context)


def investigate_question(question, engine=None):
    parsed_engine, clean_question = _parse_investigation_request(question)
    engine = (engine or parsed_engine or "claude").lower()
    question = (clean_question or "").strip()
    if not question:
        return "usage: /ask <question>"
    context_text = _build_investigation_context(question)
    if engine == "claude":
        answer = _claude_investigation_answer(question, context_text)
        if answer:
            return answer
    elif engine == "codex":
        answer = _codex_investigation_answer(question, context_text)
        if answer:
            return answer
    elif engine == "both":
        claude_answer = _claude_investigation_answer(question, context_text)
        codex_answer = _codex_investigation_answer(question, context_text)
        if claude_answer and codex_answer:
            synthesized = _synthesize_investigation_answers(question, context_text, claude_answer, codex_answer)
            if synthesized:
                return synthesized
        if claude_answer:
            return claude_answer
        if codex_answer:
            return codex_answer
    return (
        "Answer\n"
        f"LLM investigator unavailable for engine={engine}; returning gathered evidence only.\n\n"
        "Evidence\n"
        f"{context_text[:2200]}\n\n"
        "Next step\n"
        "Retry once the requested local LLM CLI is available in the bot/runtime environment."
    )


# --- Telegram file-stub (legacy bridge, kept for CLI compat) -----------------

def process_telegram():
    TELEGRAM_INBOX.mkdir(parents=True, exist_ok=True)
    TELEGRAM_OUTBOX.mkdir(parents=True, exist_ok=True)
    for p in sorted(TELEGRAM_INBOX.glob("*.json")):
        try:
            msg = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            p.unlink(missing_ok=True)
            continue
        text = (msg.get("text") or "").strip()
        reply = dispatch_telegram_command(text)
        (TELEGRAM_OUTBOX / (p.stem + ".txt")).write_text(reply)
        p.unlink(missing_ok=True)


def dispatch_telegram_command(text):
    """Shared dispatcher used by both the file stub and the real bot."""
    if text in ("/start", "start", "/help", "help"):
        return (
            "commands\n"
            "/health\n"
            "/features [project]\n"
            "/queue [state]\n"
            "/task <task_id>\n"
            "/planner <project> [on|off|status|run]\n"
            "/action <id> <retry|abandon|unblock|approve>\n"
            "/ask <question>\n"
            "/report [morning|evening]"
        )
    if text in ("/health", "health"):
        return telegram_health_card()
    if text.startswith("/features") or text.startswith("features"):
        parts = text.split(maxsplit=1)
        return features_brief(parts[1].strip() if len(parts) > 1 else None)
    if text.startswith("/queue ") or text.startswith("queue "):
        parts = text.split(maxsplit=1)
        return queue_brief(parts[1].strip())
    if text in ("/status", "status"):
        return telegram_health_card()
    if text in ("/tasks", "tasks"):
        return queue_brief("running")
    if text in ("/task", "task"):
        return "usage: /task <task_id>"
    if text.startswith("/task ") or text.startswith("task "):
        task_id = text.split(" ", 1)[1].strip()
        if not task_id:
            return "usage: /task <task_id>"
        return task_text(task_id)
    if text in ("/queue", "queue"):
        lines = ["Queue sample:"]
        for st in ("queued", "running", "blocked", "awaiting-review", "awaiting-qa"):
            items = queue_sample(st, limit=5)
            if items:
                lines.append(f"[{st}]")
                lines.extend(items)
        return "\n".join(lines) if len(lines) > 1 else "queue empty"
    if text.startswith("/planner ") or text.startswith("planner "):
        parts = text.split()
        project = parts[1] if len(parts) > 1 else None
        action = parts[2].lower() if len(parts) > 2 else "status"
        if action == "status":
            return planner_status_text(project_filter=project)
        if action == "on":
            set_planner_disabled(project, False)
            return f"planner enabled for {project}"
        if action == "off":
            set_planner_disabled(project, True, reason="telegram operator request")
            return f"planner disabled for {project}"
        if action == "run":
            tick_planner()
            return f"planner tick complete for {project}"
        return "usage: /planner <project> [on|off|status|run]"
    if text in ("/planner", "planner"):
        return planner_status_text()
    if text.startswith("/tick ") or text.startswith("tick "):
        target = text.split(" ", 1)[1].strip().lower()
        if target == "worker":
            return json.dumps(_nudge_idle_queued_workers(), sort_keys=True)
        if target == "reviewer":
            tick_reviewer()
            return "reviewer tick complete"
        if target == "qa":
            tick_qa()
            return "qa tick complete"
        if target == "canary":
            return json.dumps(tick_canary_workflows(force=True), sort_keys=True)
        return "usage: /tick [worker|reviewer|qa|canary]"
    if text in ("/reviewer", "reviewer"):
        tick_reviewer()
        return "reviewer tick complete"
    if text in ("/qa", "qa"):
        tick_qa()
        return "qa tick complete"
    if text in ("/cleanup", "cleanup"):
        checked, cleaned, skipped = cleanup_worktrees()
        return f"cleanup: checked={checked} cleaned={cleaned} skipped={skipped}"
    if text in ("/env", "env", "/env-health", "env-health"):
        return environment_health_text(refresh=True)
    if text in ("/env-repair", "env-repair"):
        return json.dumps(repair_environment(), sort_keys=True)
    if text == "/ask" or text == "ask":
        return "usage: /ask <question>"
    if text.startswith("/ask ") or text.startswith("ask "):
        question = text.split(" ", 1)[1].strip()
        return investigate_question(question)
    if text.startswith("/regression "):
        project = text.split(" ", 1)[1].strip()
        tick_regression(project)
        return f"regression sweep queued for {project}"
    if text.startswith("/report "):
        kind = text.split(" ", 1)[1].strip()
        return f"report written: {report(kind)}"
    if text.startswith("/action ") or text.startswith("action "):
        parts = text.split()
        if len(parts) < 3:
            return "usage: /action <id> <retry|abandon|unblock|approve>"
        target_id = parts[1]
        verb = parts[2].lower()
        found = find_task(target_id)
        if verb == "approve" and target_id.startswith("feature-"):
            tick_self_repair_queue()
            return f"{target_id}: self-repair queue ticked"
        if not found:
            return f"target not found: {target_id}"
        state, task = found
        if verb in ("retry", "unblock"):
            reset_task_for_retry(target_id, state, reason=f"telegram {verb}", source="telegram")
            return f"{target_id}: {state} -> queued"
        if verb == "abandon":
            move_task(target_id, state, "abandoned", reason="telegram abandon", mutator=lambda t: t.update({"finished_at": now_iso(), "abandoned_reason": "telegram abandon"}))
            return f"{target_id}: {state} -> abandoned"
        return f"unsupported action: {verb}"
    if text.startswith("/enqueue "):
        summary = text.split(" ", 1)[1].strip()
        task = new_task(
            role="implementer",
            engine="codex",
            project="manual",
            summary=summary,
            source="telegram",
        )
        enqueue_task(task)
        return f"enqueued: {task['task_id']}"
    if text.startswith("/telegram-register "):
        raw = text.split(" ", 1)[1].strip()
        chat_id, _, name = raw.partition(" ")
        return json.dumps(telegram_register_operator(int(chat_id), name.strip()), sort_keys=True)
    if text.startswith("/telegram-approve "):
        raw = text.split(" ", 1)[1].strip().split(maxsplit=2)
        if len(raw) < 2:
            return "usage: /telegram-approve <chat_id> <approved_by> [name]"
        chat_id = int(raw[0]); approved_by = int(raw[1]); name = raw[2].strip() if len(raw) > 2 else ""
        return json.dumps(telegram_approve_operator(chat_id, approved_by, name), sort_keys=True)
    if text in ("/operators", "operators"):
        return json.dumps(load_operator_allowlist(), sort_keys=True)
    if text.startswith("/self_repair ") or text.startswith("self_repair "):
        raw = text.split(" ", 1)[1].strip()
        if not raw:
            return "usage: /self_repair <summary> | <evidence>"
        if "|" in raw:
            summary, evidence = [part.strip() for part in raw.split("|", 1)]
        else:
            summary, evidence = raw, raw
        out = enqueue_self_repair(summary=summary, evidence=evidence, source="telegram")
        return (
            f"self-repair feature={out['feature_id']} task={out['task_id']}"
            if out.get("feature_id") else json.dumps(out, sort_keys=True)
        )
    return (
        "unknown command. start with /health\n\n"
        "/health\n"
        "/features [project]\n"
        "/queue [state]\n"
        "/task <task_id>\n"
        "/planner <project> [on|off|status|run]\n"
        "/action <id> <retry|abandon|unblock|approve>\n"
        "/ask <question>\n"
        "/report [morning|evening]"
    )


# --- CLI ---------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")

    p_enq = sub.add_parser("enqueue")
    p_enq.add_argument("--engine", required=True, choices=VALID_ENGINES)
    p_enq.add_argument("--role", required=True, choices=VALID_ROLES)
    p_enq.add_argument("--project", required=True)
    p_enq.add_argument("--summary", required=True)
    p_enq.add_argument("--source", default="cli")
    p_enq.add_argument("--braid-template", default=None)
    p_enq.add_argument("--no-braid-generate", action="store_true")

    p_slice = sub.add_parser("enqueue-slice")
    p_slice.add_argument("--parent", required=True)
    p_slice.add_argument("--engine", default="codex", choices=VALID_ENGINES)
    p_slice.add_argument("--role", default="implementer", choices=VALID_ROLES)
    p_slice.add_argument("--project", required=True)
    p_slice.add_argument("--summary", required=True)
    p_slice.add_argument("--braid-template", default=None)

    sub.add_parser("planner")
    sub.add_parser("reviewer")
    sub.add_parser("qa")

    p_reg = sub.add_parser("regression")
    p_reg.add_argument("project")

    p_regt = sub.add_parser("regression-tick")
    p_regt.add_argument("--today", default=None,
                        help="Override weekday (mon|tue|...) for dry-run tests.")

    p_mem = sub.add_parser("tick-memory-synthesis")
    p_mem.add_argument("--force", action="store_true")

    p_canary = sub.add_parser("tick-canary-workflows")
    p_canary.add_argument("--force", action="store_true")
    sub.add_parser("state-engine-status")
    sub.add_parser("state-engine-reconcile")
    p_state_backup = sub.add_parser("state-engine-backup")
    p_state_backup.add_argument("--path", default=None)

    p_env = sub.add_parser("env-health")
    p_env.add_argument("--refresh", action="store_true")
    sub.add_parser("repair-environment")
    p_creds = sub.add_parser("creds")
    p_creds.add_argument("action", choices=("status", "get", "set", "delete"))
    p_creds.add_argument("secret", choices=tuple(sorted(SECRET_SPECS)))
    p_creds.add_argument("--value", default=None)
    p_tg_reg = sub.add_parser("telegram-register")
    p_tg_reg.add_argument("--chat-id", required=True, type=int)
    p_tg_reg.add_argument("--name", default="")
    p_tg_appr = sub.add_parser("telegram-approve")
    p_tg_appr.add_argument("--chat-id", required=True, type=int)
    p_tg_appr.add_argument("--approved-by", required=True, type=int)
    p_tg_appr.add_argument("--name", default="")
    sub.add_parser("telegram-operators")
    p_reg_proj = sub.add_parser("register-project")
    p_reg_proj.add_argument("--name", required=True)
    p_reg_proj.add_argument("--path", required=True)
    p_reg_proj.add_argument("--type", default="application")
    p_reg_proj.add_argument("--playwright", action="store_true")
    p_reg_proj.add_argument("--auto-push", action="store_true")
    p_reg_proj.add_argument("--smoke", default="qa/smoke.sh")
    p_reg_proj.add_argument("--regression", default="qa/regression.sh")
    p_reg_proj.add_argument("--regression-day", action="append", dest="regression_days")
    p_reg_proj.add_argument("--regression-threshold-pct", default=3, type=int)
    p_reg_proj.add_argument("--no-reload-launchd", action="store_true")

    p_audit = sub.add_parser("tick-template-audit")
    p_audit.add_argument("--today", default=None)

    sub.add_parser("workflow-check")

    sub.add_parser("reap")
    sub.add_parser("rotate-logs")

    p_clean = sub.add_parser("cleanup-worktrees")
    p_clean.add_argument("--dry-run", action="store_true")

    p_slots = sub.add_parser("slots")
    p_slots.add_argument("action", choices=("status", "pause", "resume"))
    p_slots.add_argument("--slot", choices=VALID_ENGINES)
    p_slots.add_argument("--reason", default="")

    p_sweep = sub.add_parser("pr-sweep")
    p_sweep.add_argument("--dry-run", action="store_true")

    p_lint = sub.add_parser("lint-templates")
    lint_scope = p_lint.add_mutually_exclusive_group(required=True)
    lint_scope.add_argument("--all", action="store_true")
    lint_scope.add_argument("--template", default=None)

    p_ff = sub.add_parser("feature-finalize")
    p_ff.add_argument("--dry-run", action="store_true")

    p_feat = sub.add_parser("features")
    p_feat.add_argument("--status", default=None, choices=FEATURE_STATES)

    p_rep = sub.add_parser("report")
    p_rep.add_argument("kind", default="morning", nargs="?")

    sub.add_parser("process-telegram")

    p_inv = sub.add_parser("investigate")
    p_inv.add_argument("--question", required=True)
    p_inv.add_argument("--engine", choices=("claude", "codex", "both"), default=None)

    p_trans = sub.add_parser("transition")
    p_trans.add_argument("--task", required=True)
    p_trans.add_argument("--from", dest="from_state", required=True, choices=STATES)
    p_trans.add_argument("--to", dest="to_state", required=True, choices=STATES)
    p_trans.add_argument("--reason", default="")

    p_retry = sub.add_parser("retry-task")
    p_retry.add_argument("--task", required=True)
    p_retry.add_argument("--from", dest="from_state", default=None, choices=STATES)
    p_retry.add_argument("--reason", required=True)
    p_retry.add_argument("--source", default="cli")

    p_self = sub.add_parser("enqueue-self-repair")
    p_self.add_argument("--summary", required=True)
    p_self.add_argument("--evidence", required=True)
    p_self.add_argument("--issue-kind", default="runtime_bug")
    p_self.add_argument("--source", default="cli")
    p_wt = sub.add_parser("reserve-operator-worktree")
    p_wt.add_argument("--name", required=True)
    p_wt.add_argument("--allow-with-self-repair", action="store_true")
    p_opr = sub.add_parser("open-operator-pr")
    p_opr.add_argument("--repo", default=".")
    p_opr.add_argument("--base", default="main")
    p_opr.add_argument("--head", default=None)
    p_opr.add_argument("--title", default=None)
    p_opr.add_argument("--body-file", default=None)
    p_opr.add_argument("--draft", action="store_true")
    p_opr.add_argument("--no-sync-main", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "status":
        print(status_text())
    elif args.cmd == "enqueue":
        task = new_task(
            role=args.role,
            engine=args.engine,
            project=args.project,
            summary=args.summary,
            source=args.source,
            braid_template=args.braid_template,
            braid_generate_if_missing=not args.no_braid_generate,
        )
        path = enqueue_task(task)
        print(task["task_id"])
        print(path)
    elif args.cmd == "enqueue-slice":
        task = new_task(
            role=args.role,
            engine=args.engine,
            project=args.project,
            summary=args.summary,
            source=f"slice-of:{args.parent}",
            braid_template=args.braid_template,
            parent_task_id=args.parent,
        )
        enqueue_task(task)
        print(task["task_id"])
    elif args.cmd == "planner":
        tick_planner()
    elif args.cmd == "reviewer":
        tick_reviewer()
    elif args.cmd == "qa":
        tick_qa()
    elif args.cmd == "regression":
        tick_regression(args.project)
    elif args.cmd == "regression-tick":
        out = tick_regression_scheduled(today=args.today)
        print("enqueued:" + (",".join(out) if out else "(none)"))
    elif args.cmd == "tick-memory-synthesis":
        out = tick_memory_synthesis(force=args.force)
        print("enqueued:" + (",".join(out) if out else "(none)"))
    elif args.cmd == "tick-canary-workflows":
        out = tick_canary_workflows(force=args.force)
        print(json.dumps(out, sort_keys=True))
    elif args.cmd == "state-engine-status":
        print(json.dumps(state_engine_status(), sort_keys=True))
    elif args.cmd == "state-engine-reconcile":
        print(json.dumps(state_engine_reconcile(), sort_keys=True))
    elif args.cmd == "state-engine-backup":
        print(json.dumps(state_engine_backup(backup_path=args.path), sort_keys=True))
    elif args.cmd == "env-health":
        print(environment_health_text(refresh=args.refresh))
    elif args.cmd == "repair-environment":
        print(json.dumps(repair_environment(), sort_keys=True))
    elif args.cmd == "creds":
        if args.action == "status":
            print(json.dumps(secret_status(args.secret), sort_keys=True))
        elif args.action == "get":
            print(secret_value(args.secret))
        elif args.action == "set":
            value = args.value if args.value is not None else sys.stdin.read()
            ok, detail = secret_set(args.secret, value.rstrip("\n"))
            print(json.dumps({"ok": ok, "detail": detail, "secret": args.secret}, sort_keys=True))
        elif args.action == "delete":
            ok, detail = secret_delete(args.secret)
            print(json.dumps({"ok": ok, "detail": detail, "secret": args.secret}, sort_keys=True))
    elif args.cmd == "telegram-register":
        print(json.dumps(telegram_register_operator(args.chat_id, args.name), sort_keys=True))
    elif args.cmd == "telegram-approve":
        print(json.dumps(telegram_approve_operator(args.chat_id, args.approved_by, args.name), sort_keys=True))
    elif args.cmd == "telegram-operators":
        print(json.dumps(load_operator_allowlist(), sort_keys=True))
    elif args.cmd == "register-project":
        print(json.dumps(register_project(
            name=args.name,
            path=args.path,
            project_type=args.type,
            playwright=args.playwright,
            auto_push=args.auto_push,
            smoke=args.smoke,
            regression=args.regression,
            regression_days=args.regression_days,
            regression_threshold_pct=args.regression_threshold_pct,
            reload_launchd=not args.no_reload_launchd,
        ), sort_keys=True))
    elif args.cmd == "tick-template-audit":
        print(tick_template_audit(today=args.today))
    elif args.cmd == "workflow-check":
        out = tick_workflow_check()
        print(json.dumps(out, sort_keys=True))
    elif args.cmd == "reap":
        n = reap()
        print(f"reaped {n}")
    elif args.cmd == "rotate-logs":
        compressed, evicted, total = rotate_logs()
        print(f"rotate-logs: {compressed} compressed, {evicted} evicted, {total} bytes retained")
    elif args.cmd == "cleanup-worktrees":
        checked, cleaned, skipped = cleanup_worktrees(dry_run=args.dry_run)
        print(f"cleanup: {checked} checked, {cleaned} cleaned, {skipped} skipped")
    elif args.cmd == "slots":
        if args.action == "status":
            print(slot_pause_status_text())
        elif args.action == "pause":
            if not args.slot:
                raise SystemExit("--slot is required for slots pause")
            print(json.dumps(set_slot_paused(args.slot, True, reason=args.reason or "manual pause", source="cli"), sort_keys=True))
        elif args.action == "resume":
            if not args.slot:
                raise SystemExit("--slot is required for slots resume")
            set_slot_paused(args.slot, False, reason=args.reason or "manual resume", source="cli")
            print(f"{args.slot}: resumed")
    elif args.cmd == "pr-sweep":
        checked, merged, fb, alerted, skipped = pr_sweep(dry_run=args.dry_run)
        print(
            f"pr-sweep: {checked} checked, {merged} merged, {fb} feedback enqueued, "
            f"{alerted} alerted, {skipped} skipped"
        )
    elif args.cmd == "lint-templates":
        raise SystemExit(lint_templates_command(template=args.template, lint_all=args.all))
    elif args.cmd == "feature-finalize":
        checked, opened, abandoned, skipped = feature_finalize(dry_run=args.dry_run)
        print(
            f"feature-finalize: {checked} checked, {opened} opened, "
            f"{abandoned} abandoned, {skipped} skipped"
        )
    elif args.cmd == "features":
        feats = list_features(status=args.status)
        if not feats:
            print("(no features)")
        for f in feats:
            kids = len(f.get("child_task_ids", []))
            line = (
                f"{f['feature_id']} [{f['status']}] {f['project']} "
                f"children={kids} branch={f.get('branch','?')}"
            )
            if is_canary_feature(f):
                line += " canary"
            if is_self_repair_feature(f):
                line += " self-repair"
            pr_number = f.get("final_pr_number") or f.get("pr_number")
            if pr_number:
                line += f" pr=#{pr_number}"
            print(line)
    elif args.cmd == "report":
        print(report(args.kind))
    elif args.cmd == "process-telegram":
        process_telegram()
    elif args.cmd == "investigate":
        print(investigate_question(args.question, engine=args.engine))
    elif args.cmd == "transition":
        move_task(args.task, args.from_state, args.to_state, reason=args.reason)
        print(f"{args.task}: {args.from_state} -> {args.to_state}")
    elif args.cmd == "retry-task":
        if args.from_state:
            from_state = args.from_state
        else:
            found = find_task(args.task)
            if not found:
                raise SystemExit(f"task not found: {args.task}")
            from_state = found[0]
        reset_task_for_retry(
            args.task,
            from_state,
            reason=args.reason,
            source=args.source,
        )
        print(f"{args.task}: {from_state} -> queued (attempt reset)")
    elif args.cmd == "enqueue-self-repair":
        out = enqueue_self_repair(
            summary=args.summary,
            evidence=args.evidence,
            issue_kind=args.issue_kind,
            source=args.source,
        )
        print(json.dumps(out, sort_keys=True))
    elif args.cmd == "reserve-operator-worktree":
        out = reserve_orchestrator_operator_worktree(
            args.name,
            allow_with_self_repair=args.allow_with_self_repair,
        )
        print(json.dumps(out, sort_keys=True))
    elif args.cmd == "open-operator-pr":
        out = open_operator_pr(
            repo_path=args.repo,
            base_branch=args.base,
            head_branch=args.head,
            title=args.title,
            body_file=args.body_file,
            draft=args.draft,
            sync_main=not args.no_sync_main,
        )
        print(json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    main()

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
  braid/index.json                    template registry

This file is both a CLI entry point and a module. worker.py imports helpers.
"""
import argparse
from collections import deque
from dataclasses import dataclass
import datetime as dt
import errno
import fcntl
import hashlib
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

DEV_ROOT = pathlib.Path(os.environ.get("DEV_ROOT", "/Volumes/devssd"))
STATE_ROOT = DEV_ROOT / "orchestrator"
CONFIG_PATH = STATE_ROOT / "config" / "orchestrator.json"
QUEUE_ROOT = STATE_ROOT / "queue"
AGENT_STATE_DIR = STATE_ROOT / "state" / "agents"
RUNTIME_DIR = STATE_ROOT / "state" / "runtime"
PLANNER_DISABLED_DIR = RUNTIME_DIR / "planner-disabled"
CLAIMS_DIR = RUNTIME_DIR / "claims"
LOCKS_DIR = RUNTIME_DIR / "locks"
FEATURES_DIR = STATE_ROOT / "state" / "features"
TRANSITIONS_LOG = RUNTIME_DIR / "transitions.log"
REPORT_DIR = STATE_ROOT / "reports"
LOGS_DIR = STATE_ROOT / "logs"
TELEGRAM_INBOX = STATE_ROOT / "telegram" / "inbox"
TELEGRAM_OUTBOX = STATE_ROOT / "telegram" / "outbox"
BRAID_DIR = STATE_ROOT / "braid"
BRAID_TEMPLATES = BRAID_DIR / "templates"
BRAID_GENERATORS = BRAID_DIR / "generators"
BRAID_INDEX = BRAID_DIR / "index.json"
BRAID_NODE_DEF_RE = re.compile(r"(?P<node>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<shape>\[[^\]\n]*\]|\{[^\}\n]*\})")
BRAID_EDGE_START_RE = re.compile(r"^\s*(?P<node>[A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]\n]*\]|\{[^\}\n]*\})?")
BRAID_EDGE_END_RE = re.compile(r"(?P<node>[A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]\n]*\]|\{[^\}\n]*\})?\s*;?\s*$")
BRAID_BARE_EDGE_RE = re.compile(r"^\s*A\s*-->\s*B\s*;?\s*$")
BRAID_HARD_PREFIXES = ("Start", "End", "Check:", "Revise:", "Draft:", "Run:", "Read:")

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
CONFIG_DEFAULTS = {
    "drift_threshold": 5,
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


def load_config():
    data = json.loads(CONFIG_PATH.read_text())
    for key, value in CONFIG_DEFAULTS.items():
        data.setdefault(key, value)
    return data


def get_project(config, name):
    for p in config["projects"]:
        if p["name"] == name:
            return p
    raise KeyError(f"unknown project: {name}")


def project_historian_template(project_name):
    mapping = {
        "lvc-standard": "lvc-historian-update",
        "dag-framework": "dag-historian-update",
    }
    if project_name == "trade-research-platform":
        trp_prompt = BRAID_GENERATORS / "trp-historian-update.prompt.md"
        return "trp-historian-update" if trp_prompt.exists() else "lvc-historian-update"
    return mapping.get(project_name, "lvc-historian-update")


def write_json_atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True))
    os.rename(tmp, path)


def read_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def append_transition(task_id, from_state, to_state, reason=""):
    TRANSITIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TRANSITIONS_LOG.open("a") as f:
        f.write(f"{now_iso()}\t{task_id}\t{from_state}\t->\t{to_state}\t{reason}\n")


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
        "created_at": now_iso(),
        "claimed_at": None,
        "started_at": None,
        "finished_at": None,
    }


def enqueue_task(task):
    path = task_path(task["task_id"], "queued")
    write_json_atomic(path, task)
    append_transition(task["task_id"], "new", "queued", task.get("source", ""))
    return path


def move_task(task_id, from_state, to_state, reason="", mutator=None):
    """Atomically move a task file between state subdirs. Returns (new_path, task_dict).

    If `mutator` is given, it receives the loaded dict, mutates it, and the
    mutated form is written to the destination path (not the source).
    """
    src = task_path(task_id, from_state)
    dst = task_path(task_id, to_state)
    task = read_json(src)
    if task is None:
        raise FileNotFoundError(f"no task {task_id} in {from_state}")
    if mutator is not None:
        mutator(task)
    task["state"] = to_state
    # Write destination first, then unlink source — if anything crashes mid-way
    # the transition log lets us tell the task didn't land.
    write_json_atomic(dst, task)
    try:
        src.unlink()
    except FileNotFoundError:
        pass
    append_transition(task_id, from_state, to_state, reason)
    return dst, task


def _atomic_claim_doctest_case(dep_state):
    with tempfile.TemporaryDirectory() as tmp:
        old_root = atomic_claim.__globals__["QUEUE_ROOT"]
        old_now = atomic_claim.__globals__["now_iso"]
        atomic_claim.__globals__["QUEUE_ROOT"] = pathlib.Path(tmp)
        atomic_claim.__globals__["now_iso"] = lambda: "2026-04-14T12:00:00"
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


def project_hard_stopped(project_name):
    """Return True if the project has any blocked task tagged regression-failure.
    Plan §5 hard stop: no implementer/planner work runs for a project until a
    human reviews and clears the blocked regression task."""
    if not project_name:
        return False
    for p in queue_dir("blocked").glob("*.json"):
        t = read_json(p, {})
        if t.get("project") == project_name and t.get("topology_error") == "regression-failure":
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
    queued = queue_dir("queued")
    candidates = sorted(queued.glob("*.json"), key=lambda p: p.stat().st_mtime)
    busy_features = in_flight_feature_ids() if slot_engine == "codex" else set()
    for src in candidates:
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
        if slot_engine == "codex":
            fid = task.get("feature_id")
            if fid and fid in busy_features:
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
        return True
    return True


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
                move_task(task_id, state, "queued", reason=f"reaper: pid {pid} dead")
                reaped += 1
                break
        pidfile.unlink(missing_ok=True)
    # Also: transition blocked tasks that have a regenerated template back to queued.
    for src in queue_dir("blocked").glob("*.json"):
        task = read_json(src, {})
        if task.get("topology_error") == "template_missing":
            tmpl = task.get("braid_template")
            if tmpl and (BRAID_TEMPLATES / f"{tmpl}.mmd").exists():
                move_task(
                    task["task_id"],
                    "blocked",
                    "queued",
                    reason="reaper: template regenerated",
                    mutator=lambda t: t.update(
                        braid_template_hash=None,
                        topology_error=None,
                    ),
                )
                reaped += 1
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

PR_SWEEP_MAX_FEEDBACK_ROUNDS = 3


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
    """
    author = (comment.get("author") or {}).get("login", "").lower()
    if author in AUTO_HANDLE_COMMENT_AUTHORS:
        return True
    return _pr_body_has_orchestrator_mention(comment.get("body", ""))


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


def pr_sweep(dry_run=False):
    """Sweep open task PRs: auto-merge clean ones, dispatch pr-feedback, alert on stuck.

    Returns (checked, merged, feedback_enqueued, alerted, skipped).

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     queue_root = root / "queue"
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
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
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "load_config", "get_project", "_extract_actionable_comments", "_enqueue_pr_feedback", "_write_pr_alert", "now_iso", "subprocess")}
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
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
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
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "load_config", "get_project", "_extract_actionable_comments", "_enqueue_pr_feedback", "_write_pr_alert", "now_iso", "subprocess")}
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
    ...     pr_sweep.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(root)}], "drift_threshold": 5}
    ...     pr_sweep.__globals__["get_project"] = lambda config, name: config["projects"][0]
    ...     pr_sweep.__globals__["_extract_actionable_comments"] = lambda info, handled: []
    ...     pr_sweep.__globals__["_enqueue_pr_feedback"] = lambda *args, **kwargs: calls.append(kwargs) or "task-drift-sync"
    ...     pr_sweep.__globals__["_write_pr_alert"] = lambda *args, **kwargs: None
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
    ...     for state in STATES:
    ...         (queue_root / state).mkdir(parents=True, exist_ok=True)
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
    ...     original = {k: pr_sweep.__globals__[k] for k in ("QUEUE_ROOT", "load_config", "get_project", "_extract_actionable_comments", "_enqueue_pr_feedback", "_write_pr_alert", "now_iso", "subprocess")}
    ...     class FakeSubprocess:
    ...         TimeoutExpired = subprocess.TimeoutExpired
    ...         def run(self, cmd, **kwargs):
    ...             payload = {"state": "OPEN", "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY", "reviewDecision": "", "baseRefName": "feature/demo", "url": "https://example/pr/9", "comments": []}
    ...             return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    ...     pr_sweep.__globals__["QUEUE_ROOT"] = queue_root
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
             "headRefOid,baseRefName,url,author,comments"],
            cwd=repo_path,
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:200]
            print(f"pr-sweep {task.get('task_id')}: gh pr view failed: {err}")
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
        mergeable = info.get("mergeable", "UNKNOWN")
        review_decision = info.get("reviewDecision", "")
        merge_state = info.get("mergeStateStatus", "")
        dispatch_reason = "conflict"

        drift_threshold = int(config.get("drift_threshold", CONFIG_DEFAULTS["drift_threshold"]))
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
                task = update_task_in_place(task_file, stamp_sweep({
                    "last_mergeable": mergeable,
                    "last_merge_state": merge_state,
                }))
                continue
            if dry_run:
                print(f"DRY-RUN pr-sweep {task.get('task_id')}: would enqueue rebase feedback")
                continue
            conflict_task_id = _enqueue_pr_feedback(task, project_name, pr_number, base_ref,
                                                    conflicts=True, comments=actionable)
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
                alert = _write_pr_alert(
                    project_name, task.get("task_id"), pr_number,
                    f"exhausted {rounds} pr-feedback rounds, {len(actionable)} comments still unresolved",
                    pr_url,
                )
                task = update_task_in_place(task_file,
                    stamp_sweep({"last_mergeable": mergeable, "escalated_comments": True}))
                alerted += 1
                continue
            if dry_run:
                print(f"DRY-RUN pr-sweep {task.get('task_id')}: would enqueue "
                      f"feedback for {len(actionable)} comment(s)")
                continue
            _enqueue_pr_feedback(task, project_name, pr_number, base_ref,
                                 conflicts=False, comments=actionable)
            task = update_task_in_place(task_file, stamp_sweep({
                "last_mergeable": mergeable,
                "feedback_rounds": rounds + 1,
                "last_feedback_reason": "comments",
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
                if dry_run:
                    print(f"DRY-RUN pr-sweep {task.get('task_id')}: would enqueue "
                          f"feedback for {len(new_thread_comments)} bot review thread(s)")
                    continue
                _enqueue_pr_feedback(
                    task, project_name, pr_number, base_ref,
                    conflicts=False, comments=new_thread_comments,
                )
                task = update_task_in_place(task_file, stamp_sweep({
                    "last_mergeable": mergeable,
                    "feedback_rounds": rounds + 1,
                    "last_feedback_reason": "unresolved_bot_review_threads",
                    "handled_comment_ids": handled_ids + [c["id"] for c in new_thread_comments],
                    "unresolved_bot_threads": count,
                }))
                fb_enqueued += 1
                continue
            # All unresolved threads are already in handled_comment_ids —
            # a previous round is in flight or its fix failed to satisfy
            # the bot and no new comment has landed. Do NOT fall through
            # to Case 4 (would merge an un-green PR); stamp and skip.
            task = update_task_in_place(task_file, stamp_sweep({
                "last_mergeable": mergeable,
                "unresolved_bot_threads": count,
            }))
            skipped += 1
            continue

        # Case 3: waiting on review. Don't merge, don't alert — just stamp.
        if review_decision == "CHANGES_REQUESTED":
            task = update_task_in_place(task_file, stamp_sweep({
                "last_mergeable": mergeable,
                "last_review_decision": review_decision,
            }))
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

    return (checked, merged, fb_enqueued, alerted, skipped)


def update_task_in_place(task_file, mutator):
    """Read, mutate, and write a task JSON at its current path. No state transition."""
    task = read_json(task_file, {})
    mutator(task)
    write_json_atomic(task_file, task)
    return task


def _task_exists_in_queue(task_id, states=("queued", "claimed", "running")):
    return any(task_path(task_id, state).exists() for state in states)


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


def _enqueue_pr_feedback(target, project_name, pr_number, base_branch, *, conflicts, comments):
    """Create a codex pr-feedback task bound to target's feature_id.

    >>> captured = {}
    >>> original = {k: _enqueue_pr_feedback.__globals__[k] for k in ("new_task", "enqueue_task", "_build_conflict_preview")}
    >>> _enqueue_pr_feedback.__globals__["new_task"] = lambda **kwargs: captured.setdefault("task", {"task_id": "task-preview", **kwargs}) or captured["task"]
    >>> _enqueue_pr_feedback.__globals__["enqueue_task"] = lambda task: captured.setdefault("enqueued", task["task_id"])
    >>> _enqueue_pr_feedback.__globals__["_build_conflict_preview"] = lambda worktree, base: {"our_commits": "a", "their_commits": "b", "likely_conflict_files": ["conflict.py"]}
    >>> target = {"task_id": "task-target", "feature_id": "f1", "worktree": "/tmp/demo"}
    >>> _enqueue_pr_feedback(target, "demo", 42, "feature/demo", conflicts=True, comments=[])
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
        },
    )
    enqueue_task(task)
    return task["task_id"]


def _load_feature_children(feature):
    """Load done/ child task JSONs for a feature, or return None if any missing."""
    children = []
    for child_id in feature.get("child_task_ids", []):
        child = read_json(task_path(child_id, "done"), None)
        if child is None:
            print(f"feature-finalize {feature.get('feature_id')}: orphan child id {child_id}")
            return None
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
        if not (
            task_path(child_id, "failed").exists()
            or task_path(child_id, "abandoned").exists()
        ):
            all_failed = False
            break
    if not all_failed:
        return False
    fid = feature.get("feature_id")
    for state in ("queued", "claimed", "running"):
        for p in queue_dir(state).glob("*.json"):
            task = read_json(p, {})
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
                update_feature(feature_id, lambda f: f.update({"status": "abandoned"}))
            abandoned += 1
            continue

        try:
            project = get_project(config, feature["project"])
        except KeyError:
            print(f"feature-finalize {feature_id}: unknown project {feature.get('project')}, skip")
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

        update_feature(feature_id, mut_feature)
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
        write_json_atomic(task_file, task)
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
        words = re.findall(r"\b[A-Z][A-Za-z0-9_/.-]*\b", label)
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
    entry.update(
        path=str(p.relative_to(STATE_ROOT)),
        hash="sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        generator_model=generator_model,
        created_at=now_iso(),
        uses=entry.get("uses", 0),
        topology_errors=entry.get("topology_errors", 0),
    )
    idx[task_type] = entry
    save_braid_index(idx)


def braid_template_record_use(task_type, topology_error=False):
    idx = load_braid_index()
    entry = idx.get(task_type, {})
    if topology_error:
        entry["topology_errors"] = entry.get("topology_errors", 0) + 1
    else:
        entry["uses"] = entry.get("uses", 0) + 1
    idx[task_type] = entry
    save_braid_index(idx)


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
    return read_json(feature_path(feature_id), None)


def list_features(status=None):
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


def create_feature(*, project, summary, source, roadmap_entry=None, roadmap_entry_id=None):
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
    }
    write_json_atomic(feature_path(feature_id), feature)
    return feature


def update_feature(feature_id, mutator):
    """Load feature, apply mutator, write atomically. Returns the updated dict."""
    path = feature_path(feature_id)
    feature = read_json(path, None)
    if feature is None:
        raise FileNotFoundError(f"no feature {feature_id}")
    mutator(feature)
    write_json_atomic(path, feature)
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
    for state in ("claimed", "running"):
        for p in queue_dir(state).glob("*.json"):
            t = read_json(p, {})
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


def agent_statuses():
    AGENT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return [read_json(p, {}) for p in sorted(AGENT_STATE_DIR.glob("*.json"))]


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


# --- Queue inspection --------------------------------------------------------

def queue_counts():
    counts = {}
    for state in STATES:
        counts[state] = len(list(queue_dir(state).glob("*.json")))
    return counts


def engine_outstanding():
    """Count active tasks per engine across queued/claimed/running only.

    Used by tick_planner to gate further decomposition when the pipeline is
    already carrying work. awaiting-review/awaiting-qa are excluded — those are
    pipeline holding states picked up by their own tickers.
    """
    counts = {"claude": 0, "codex": 0, "qa": 0}
    for state in ("queued", "claimed", "running"):
        for p in queue_dir(state).glob("*.json"):
            t = read_json(p, {})
            eng = t.get("engine")
            if eng in counts:
                counts[eng] += 1
    return counts


def queue_sample(state, limit=10):
    items = []
    for p in sorted(queue_dir(state).glob("*.json"))[:limit]:
        t = read_json(p, {})
        items.append(
            f"  {t.get('task_id')} [{t.get('engine')}/{t.get('role')}] "
            f"{t.get('project')}: {t.get('summary','')[:60]}"
        )
    return items


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
    ...     old = {k: tick_planner.__globals__[k] for k in ("load_config", "engine_outstanding", "write_agent_status", "project_hard_stopped", "acquire_lock", "assigned_roadmap_entries", "parse_roadmap_next_todo", "create_feature", "new_task", "enqueue_task", "FEATURES_DIR", "PLANNER_DISABLED_DIR")}
    ...     tick_planner.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(repo)}]}
    ...     tick_planner.__globals__["engine_outstanding"] = lambda: {"claude": 0, "codex": 0, "qa": 0}
    ...     tick_planner.__globals__["write_agent_status"] = lambda *args, **kwargs: None
    ...     tick_planner.__globals__["project_hard_stopped"] = lambda name: False
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
    ...     old = {k: tick_planner.__globals__[k] for k in ("load_config", "engine_outstanding", "write_agent_status", "project_hard_stopped", "acquire_lock", "assigned_roadmap_entries", "parse_roadmap_next_todo", "create_feature", "new_task", "enqueue_task", "FEATURES_DIR", "PLANNER_DISABLED_DIR")}
    ...     tick_planner.__globals__["load_config"] = lambda: {"projects": [{"name": "demo", "path": str(repo)}]}
    ...     tick_planner.__globals__["engine_outstanding"] = lambda: {"claude": 0, "codex": 0, "qa": 0}
    ...     tick_planner.__globals__["write_agent_status"] = lambda *args, **kwargs: None
    ...     tick_planner.__globals__["project_hard_stopped"] = lambda name: False
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
        msg = "Gated — slots busy: " + ", ".join(f"{e}={n}" for e, n in sorted(backed_up.items()))
        write_agent_status("planner", "gated", msg)
        return
    write_agent_status("planner", "running", "Refreshing queue from repo-memory.")
    cfg = load_config()
    skipped_hard_stop = []
    for project in cfg["projects"]:
        # Only plan for projects whose repo-memory exists — avoid spamming stubs.
        memdir = pathlib.Path(project["path"]) / "repo-memory"
        if not (memdir / "CURRENT_STATE.md").exists():
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
    outstanding = engine_outstanding()
    if outstanding.get("claude", 0) > 0:
        write_agent_status(
            "reviewer", "gated", f"Gated — claude slot busy: claude={outstanding['claude']}"
        )
        return
    # Count awaiting-review per project.
    ar_by_project = {}
    for p in queue_dir("awaiting-review").glob("*.json"):
        t = read_json(p, {})
        proj = t.get("project")
        if proj:
            ar_by_project[proj] = ar_by_project.get(proj, 0) + 1
    if not ar_by_project:
        write_agent_status("reviewer", "idle", "No awaiting-review work.")
        return
    write_agent_status("reviewer", "running", f"Queueing reviewer for {len(ar_by_project)} project(s).")
    cfg = load_config()
    enqueued = 0
    for project in cfg["projects"]:
        name = project["name"]
        if name not in ar_by_project:
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
    write_agent_status("reviewer", "idle", f"Reviewer enqueued ({enqueued}).")


def tick_qa():
    """Enqueue one smoke-driver task per project with awaiting-qa work.

    Gated on:
      1. qa slot outstanding > 0 (only one smoke driver in flight at a time —
         qa is single-worker and smoke locks the project),
      2. no awaiting-qa tasks for the project (nothing to qa = skip).
    Each driver task claims the oldest awaiting-qa target at run time and
    runs smoke.sh in that target's worktree.
    """
    outstanding = engine_outstanding()
    if outstanding.get("qa", 0) > 0:
        write_agent_status(
            "qa", "gated", f"Gated — qa slot busy: qa={outstanding['qa']}"
        )
        return
    aq_by_project = {}
    for p in queue_dir("awaiting-qa").glob("*.json"):
        t = read_json(p, {})
        proj = t.get("project")
        if proj:
            aq_by_project[proj] = aq_by_project.get(proj, 0) + 1
    if not aq_by_project:
        write_agent_status("qa", "idle", "No awaiting-qa work.")
        return
    write_agent_status("qa", "running", f"Queueing qa smoke for {len(aq_by_project)} project(s).")
    cfg = load_config()
    enqueued = 0
    for project in cfg["projects"]:
        name = project["name"]
        if name not in aq_by_project:
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
    write_agent_status("qa", "idle", f"QA driver enqueued ({enqueued}).")


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
    ts = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{kind}_{ts}.md"
    lines = [f"# {kind.title()} Status Report", "", f"Generated: {now_iso()}", ""]
    lines.append("## Agent status")
    for s in agent_statuses():
        lines.append(f"- **{s.get('role')}**: {s.get('status')} — {s.get('detail','')}")
    lines += ["", "## Queue"]
    counts = queue_counts()
    for st in STATES:
        lines.append(f"- {st}: {counts[st]}")
    lines += ["", "## BRAID templates"]
    idx = load_braid_index()
    if not idx:
        lines.append("- (none registered)")
    for tt, e in sorted(idx.items()):
        lines.append(
            f"- `{tt}`: uses={e.get('uses',0)}, topology_errors={e.get('topology_errors',0)}"
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
    if kind == "morning":
        ppd_path = ppd_report_path()
        if ppd_path.exists():
            lines += ["", "## PPD", "", ppd_path.read_text().strip()]
    out.write_text("\n".join(lines))
    return out


def has_in_flight_task(*, project, braid_template):
    for state in ("queued", "claimed", "running"):
        for p in queue_dir(state).glob("*.json"):
            t = read_json(p, {})
            if not t:
                continue
            if t.get("project") == project and t.get("braid_template") == braid_template:
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


def status_text():
    lines = ["devmini orchestrator status", f"({now_iso()})", ""]
    lines.append("Agents:")
    for s in agent_statuses():
        lines.append(f"  {s.get('role')}: {s.get('status')} — {s.get('detail','')}")
    lines.append("")
    lines.append("Queue:")
    counts = queue_counts()
    for st in STATES:
        if counts[st]:
            lines.append(f"  {st}: {counts[st]}")
    hard_stopped = [
        p["name"] for p in load_config().get("projects", [])
        if project_hard_stopped(p["name"])
    ]
    if hard_stopped:
        lines.append("")
        lines.append(f"HARD-STOP (regression-failure): {', '.join(hard_stopped)}")
    open_features = list_features(status="open")
    if open_features:
        lines.append("")
        lines.append(f"Features (open): {len(open_features)}")
        per_proj = {}
        for f in open_features:
            per_proj[f["project"]] = per_proj.get(f["project"], 0) + 1
        for proj, n in sorted(per_proj.items()):
            lines.append(f"  {proj}: {n}")
    idx = load_braid_index()
    if idx:
        lines.append("")
        lines.append("BRAID:")
        for tt, e in sorted(idx.items()):
            lines.append(
                f"  {tt}: uses={e.get('uses',0)} errs={e.get('topology_errors',0)}"
            )
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
    for project in projects:
        name = project["name"]
        if project_filter is not None and name != project_filter:
            continue
        open_features = 0
        for feature in list_features():
            if feature.get("project") == name and feature.get("status") in ("open", "finalizing"):
                open_features += 1
        in_flight = 0
        for state in ("claimed", "running"):
            for p in queue_dir(state).glob("*.json"):
                task = read_json(p, {})
                if task.get("project") == name:
                    in_flight += 1
        rows.append({
            "project": name,
            "planner": "enabled" if not planner_disabled(name) else "disabled",
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
    if text in ("/status", "status"):
        return status_text()
    if text in ("/queue", "queue"):
        lines = ["Queue sample:"]
        for st in ("queued", "running", "blocked", "awaiting-review", "awaiting-qa"):
            items = queue_sample(st, limit=5)
            if items:
                lines.append(f"[{st}]")
                lines.extend(items)
        return "\n".join(lines) if len(lines) > 1 else "queue empty"
    if text in ("/planner", "planner"):
        tick_planner()
        return "planner tick complete"
    if text in ("/reviewer", "reviewer"):
        tick_reviewer()
        return "reviewer tick complete"
    if text in ("/qa", "qa"):
        tick_qa()
        return "qa tick complete"
    if text.startswith("/regression "):
        project = text.split(" ", 1)[1].strip()
        tick_regression(project)
        return f"regression sweep queued for {project}"
    if text.startswith("/report "):
        kind = text.split(" ", 1)[1].strip()
        return f"report written: {report(kind)}"
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
    return (
        "unknown command. allowed: /status /queue /planner /reviewer /qa "
        "/regression <project> /report morning|evening /enqueue <summary>"
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

    sub.add_parser("reap")

    p_clean = sub.add_parser("cleanup-worktrees")
    p_clean.add_argument("--dry-run", action="store_true")

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

    p_trans = sub.add_parser("transition")
    p_trans.add_argument("--task", required=True)
    p_trans.add_argument("--from", dest="from_state", required=True, choices=STATES)
    p_trans.add_argument("--to", dest="to_state", required=True, choices=STATES)
    p_trans.add_argument("--reason", default="")

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
    elif args.cmd == "reap":
        n = reap()
        print(f"reaped {n}")
    elif args.cmd == "cleanup-worktrees":
        checked, cleaned, skipped = cleanup_worktrees(dry_run=args.dry_run)
        print(f"cleanup: {checked} checked, {cleaned} cleaned, {skipped} skipped")
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
            pr_number = f.get("final_pr_number") or f.get("pr_number")
            if pr_number:
                line += f" pr=#{pr_number}"
            print(line)
    elif args.cmd == "report":
        print(report(args.kind))
    elif args.cmd == "process-telegram":
        process_telegram()
    elif args.cmd == "transition":
        move_task(args.task, args.from_state, args.to_state, reason=args.reason)
        print(f"{args.task}: {args.from_state} -> {args.to_state}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""devmini worker — one task, then exit. Launchd respawns us.

Invoke as: worker.py <slot>   slot ∈ {claude, codex, qa}

Design choices:
- Bounded runs: claim ONE task, execute it, exit. No in-process loop.
- Atomic claim via os.rename (orchestrator.atomic_claim).
- Worktree isolation: worker.py manages git worktrees directly rather than
  calling engineering-memory/tooling/create_agent_worktree.sh, because that
  script hardcodes $HOME/dev paths and doesn't match /Volumes/devssd/repos.
  Fixing the shared script is a pass-2 task.
- BRAID template resolution: codex slot only. Missing template → block task,
  enqueue claude regeneration, exit. Present template → load as system context.
- Trailer parsing: codex is invoked with `-o <last_msg_file>` which writes the
  final assistant message to disk, avoiding fragile log scraping.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orchestrator as o  # noqa: E402

WORKTREES_ROOT = o.DEV_ROOT / "worktrees"
QA_ARTIFACTS_ROOT = o.DEV_ROOT / "qa-artifacts"

# Default per-slot timeouts in seconds. Overridable via
# config["slots"][<slot>]["timeout_sec"] or task["engine_args"]["timeout_sec"].
DEFAULT_TIMEOUTS = {"claude": 1800, "codex": 1800, "qa": 900}


def log(msg):
    sys.stderr.write(f"[worker {dt.datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    sys.stderr.flush()


# --- git worktree management ------------------------------------------------

def make_worktree(project_path, task_id):
    WORKTREES_ROOT.mkdir(parents=True, exist_ok=True)
    repo_name = pathlib.Path(project_path).name
    wt_root = WORKTREES_ROOT / repo_name
    wt_root.mkdir(parents=True, exist_ok=True)
    wt_path = wt_root / task_id
    branch = f"agent/{task_id}"
    subprocess.run(
        ["git", "worktree", "add", str(wt_path), "-b", branch],
        cwd=project_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return wt_path, branch


def remove_worktree(project_path, wt_path, branch):
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=project_path,
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=project_path,
            check=False,
            capture_output=True,
        )
    except Exception as exc:  # best effort — worktree cleanup is advisory
        log(f"worktree cleanup warn: {exc}")


# --- memory context ---------------------------------------------------------

def read_memory_context(project_path):
    memdir = pathlib.Path(project_path) / "repo-memory"
    parts = []
    for name in ("CURRENT_STATE.md", "RECENT_WORK.md", "DECISIONS.md"):
        f = memdir / name
        if f.exists():
            body = f.read_text().strip()
            if name == "RECENT_WORK.md":
                # Keep only the last ~20 lines — tasks don't need ancient history.
                lines = body.splitlines()
                body = "\n".join(lines[-20:])
            parts.append(f"### {name}\n{body}")
    return "\n\n".join(parts) if parts else "(no repo-memory available)"


# --- advisory lock ----------------------------------------------------------

def acquire_lock(lock_name, mode, timeout_sec=0):
    """Acquire fcntl lock on locks/<name>. mode: 'shared' | 'exclusive'.
    Returns an open file handle whose lifetime holds the lock. Close to release.
    Raises TimeoutError on failure.
    """
    o.LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = o.LOCKS_DIR / lock_name
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


# --- mermaid extraction -----------------------------------------------------

_MERMAID_FENCE = re.compile(r"```(?:mermaid)?\s*(flowchart\s+\w+;?.*?)```", re.DOTALL)


def extract_mermaid(text):
    """Extract a Mermaid flowchart block from mixed claude output."""
    m = _MERMAID_FENCE.search(text)
    if m:
        return m.group(1).strip()
    # Fallback: raw `flowchart TD;` block with no fence. Grab from that keyword
    # to the end, then trim trailing markdown.
    idx = text.find("flowchart TD")
    if idx < 0:
        idx = text.find("flowchart LR")
    if idx < 0:
        return None
    body = text[idx:].strip()
    # Strip trailing markdown artifacts (``` at end, etc.)
    body = re.sub(r"```\s*$", "", body).strip()
    return body


# --- claude slot ------------------------------------------------------------

def run_claude_slot(task, cfg):
    """Handle claude-engine tasks: template generation or planner decomposition."""
    task_id = task["task_id"]
    summary = task.get("summary", "")
    bt = task.get("braid_template")
    mode = task.get("engine_args", {}).get("mode")

    # Heuristic: template generation if summary begins with the canonical phrase
    # OR engine_args.mode is set explicitly.
    is_template_gen = mode == "template-gen" or (
        bt and summary.startswith(f"Generate BRAID template for {bt}")
    )

    timeout = task.get("engine_args", {}).get(
        "timeout_sec", cfg.get("slots", {}).get("claude", {}).get("timeout_sec", DEFAULT_TIMEOUTS["claude"])
    )
    log_path = o.LOGS_DIR / f"{task_id}.log"
    o.LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if is_template_gen:
        return run_claude_template_gen(task, cfg, bt, timeout, log_path)
    if task.get("role") == "reviewer":
        return run_claude_reviewer(task, cfg, timeout, log_path)
    return run_claude_planner(task, cfg, timeout, log_path)


def run_claude_template_gen(task, cfg, task_type, timeout, log_path):
    task_id = task["task_id"]
    gen_prompt_path = o.BRAID_GENERATORS / f"{task_type}.prompt.md"
    if not gen_prompt_path.exists():
        o.move_task(
            task_id, "claimed", "failed",
            reason=f"no generator prompt for {task_type}",
        )
        return

    project_name = task.get("project")
    memory_ctx = ""
    if project_name and project_name != "manual":
        try:
            project = o.get_project(cfg, project_name)
            memory_ctx = read_memory_context(project["path"])
        except KeyError:
            memory_ctx = "(unknown project)"

    user_prompt = (
        f"TASK TYPE: {task_type}\n\n"
        f"PROJECT MEMORY:\n{memory_ctx}\n\n"
        f"Emit ONLY the Mermaid flowchart block and nothing else."
    )

    gen_system = gen_prompt_path.read_text()

    # NOTE: intentionally NOT using --bare. --bare forces ANTHROPIC_API_KEY/apiKeyHelper
    # auth and refuses to read the user's claude.ai OAuth credentials (Max subscription).
    # We rely on --system-prompt to fully replace the default system prompt, and on
    # cwd=/tmp to prevent stray CLAUDE.md auto-discovery in the worker invocation.
    cmd = [
        "claude",
        "-p", user_prompt,
        "--dangerously-skip-permissions",
        "--system-prompt", gen_system,
        "--output-format", "text",
        "--model", "opus",
        "--max-budget-usd", "1.00",
        "--no-session-persistence",
    ]

    def mut_running(t):
        t["started_at"] = o.now_iso()
        t["log_path"] = str(log_path)
    o.move_task(task_id, "claimed", "running", reason="claude template-gen", mutator=mut_running)

    with log_path.open("w") as logf:
        logf.write(f"# claude template-gen task={task_id} task_type={task_type}\n")
        logf.write(f"# model: opus\n# max_budget_usd: 1.00\n\n")
        logf.flush()
        try:
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=logf, text=True, timeout=timeout,
                cwd="/tmp",  # avoid CLAUDE.md auto-discovery at worker invocation
            )
        except subprocess.TimeoutExpired:
            o.move_task(task_id, "running", "failed", reason=f"claude timeout {timeout}s")
            return
        logf.write(f"\n# exit: {proc.returncode}\n")
        logf.write("# stdout:\n")
        logf.write(proc.stdout or "")

    if proc.returncode != 0:
        o.move_task(task_id, "running", "failed", reason=f"claude exit {proc.returncode}")
        return

    graph = extract_mermaid(proc.stdout or "")
    if not graph:
        o.move_task(task_id, "running", "failed", reason="no mermaid block in output")
        return

    o.braid_template_write(task_type, graph, generator_model="claude-opus")
    def mut(t):
        t["artifacts"] = t.get("artifacts", []) + [f"braid/templates/{task_type}.mmd"]
        t["finished_at"] = o.now_iso()
    o.move_task(task_id, "running", "done", reason="template written", mutator=mut)


# Post-validation on planner-emitted slices. The prompt already tells claude
# to skip CI/docs/release work, but in pass-1 several "Rewrite CI version-bump
# workflow" and "Audit docs and release automation" slices still landed with
# braid_template=lvc-implement-operator — the prompt guidance wasn't enough.
# This classifier is defense in depth: it rejects operator slices whose summary
# matches any anti-pattern or lacks every hot-path positive keyword. Both lists
# are substring-matched case-insensitively.
LVC_OPERATOR_ANTI_PATTERNS = (
    "ci workflow", "ci version", "version bump", "github action",
    "release automation", "release script", "release note",
    "stale package", "package ref", "readme",
    "dependabot", "lockfile", "gradle wrapper",
)
LVC_OPERATOR_POSITIVE_PATTERNS = (
    "hot path", "hot-path", "zero alloc", "zero-alloc", "jmh",
    "operator", "publisher", "poller", "consumer",
    "ringbuffer", "ring buffer", "mmap",
    "aeron", "guaranteed", "ipc", "signal",
    "position store", "replay", "cursor",
)


def classify_slice(template, summary):
    """Return (ok, reason). ok=False drops the slice with reason logged."""
    s = (summary or "").lower()
    if template == "lvc-implement-operator":
        for anti in LVC_OPERATOR_ANTI_PATTERNS:
            if anti in s:
                return False, f"anti-pattern {anti!r}"
        if not any(p in s for p in LVC_OPERATOR_POSITIVE_PATTERNS):
            return False, "no hot-path keyword"
    return True, None


# Legitimate reason codes that may appear after `BRAID_TOPOLOGY_ERROR:`.
# The lesson from the 2026-04-13 InterprocessIpcPolicy misdiagnosis: a codex
# solver is NOT allowed to declare an "unrelated" or "pre-existing" failure
# as a topology error — it must route through the CheckBaseline node and
# emit `baseline_red` instead. Any trailer whose reason does not contain one
# of these codes is a false_blocker_claim; we move the task to failed/
# (not blocked/), do NOT increment topology_errors, and do NOT enqueue a
# regeneration task (a new template won't fix a false claim).
VALID_TOPOLOGY_REASONS = (
    "template_missing",
    "baseline_red",
    "graph_unreachable",
    "graph_malformed",
)


def topology_reason_is_valid(trailer):
    _, _, rest = trailer.partition(":")
    rest = rest.strip().lower()
    if not rest:
        return False
    return any(code in rest for code in VALID_TOPOLOGY_REASONS)


def run_claude_planner(task, cfg, timeout, log_path):
    """Freeform planner: claude reads memory and emits slice proposals."""
    task_id = task["task_id"]
    project_name = task.get("project")
    try:
        project = o.get_project(cfg, project_name)
    except KeyError:
        o.move_task(task_id, "claimed", "failed", reason=f"unknown project {project_name}")
        return

    memory_ctx = read_memory_context(project["path"])
    system_prompt = (
        "You are the BRAID generator for devmini. Your job: read the project memory "
        "and propose at most 3 bounded CODEX execution slices for the next actionable work.\n\n"
        "Only two braid_template values are valid for codex slices:\n"
        "  - \"lvc-implement-operator\": ONLY for adding/modifying a Java operator in "
        "src/ under the hot path, with zero-allocation invariants verifiable via JMH + "
        "conformance tests. Requires Java source changes in the library's hot path.\n"
        "  - \"lvc-historian-update\": ONLY for appending observations to "
        "repo-memory/RECENT_WORK.md, DECISIONS.md, or FAILURES.md. No source code changes.\n\n"
        "Do NOT emit reviewer or QA slices — those are scheduled by separate tickers. "
        "Do NOT invent other template names. Do NOT use null. "
        "If a candidate piece of work fits NEITHER template (e.g. CI workflow changes, "
        "build config edits, documentation outside repo-memory, cross-module refactors), "
        "SKIP it — do not include it in your output. If nothing fits, emit an empty array [].\n\n"
        "Never assign \"lvc-implement-operator\" to a task that doesn't touch Java source "
        "in the hot path. When in doubt, pick \"lvc-historian-update\" to record the "
        "observation for a future pass, or skip.\n\n"
        "Emit ONLY a JSON array of objects, each with keys:\n"
        "  summary (≤120 chars), braid_template (one of the two values above, verbatim).\n"
        "No prose, no markdown fences."
    )
    user_prompt = (
        f"PROJECT: {project['name']}\n\n"
        f"PARENT TASK: {task['summary']}\n\n"
        f"MEMORY:\n{memory_ctx}\n"
    )

    cmd = [
        "claude",
        "-p", user_prompt,
        "--dangerously-skip-permissions",
        "--system-prompt", system_prompt,
        "--output-format", "text",
        "--model", "sonnet",
        "--max-budget-usd", "1.00",
        "--disallowedTools", "Bash,Read,Write,Edit,Grep,Glob,Agent,WebFetch,WebSearch",
        "--no-session-persistence",
    ]

    def mut_running(t):
        t["started_at"] = o.now_iso()
        t["log_path"] = str(log_path)
    o.move_task(task_id, "claimed", "running", reason="claude planner", mutator=mut_running)

    with log_path.open("w") as logf:
        logf.write(f"# claude planner task={task_id} project={project['name']}\n\n")
        try:
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=logf, text=True, timeout=timeout,
                cwd="/tmp",  # avoid CLAUDE.md auto-discovery
            )
        except subprocess.TimeoutExpired:
            o.move_task(task_id, "running", "failed", reason=f"claude timeout {timeout}s")
            return
        logf.write(f"\n# exit: {proc.returncode}\n# stdout:\n{proc.stdout or ''}\n")

    if proc.returncode != 0:
        o.move_task(task_id, "running", "failed", reason=f"claude exit {proc.returncode}")
        return

    # Extract JSON array from output — claude sometimes wraps in markdown.
    raw = proc.stdout or ""
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        o.move_task(task_id, "running", "failed", reason="no json array in output")
        return
    try:
        slices = json.loads(m.group(0))
    except json.JSONDecodeError:
        o.move_task(task_id, "running", "failed", reason="malformed json array")
        return

    # Template → role for codex slices. Slices with a template outside this set
    # are dropped — the planner prompt instructs claude to skip ill-fitting work
    # rather than invent new templates.
    TEMPLATE_ROLE = {
        "lvc-implement-operator": "implementer",
        "lvc-historian-update": "historian",
    }

    enqueued = []
    dropped = []
    for s in slices[:3]:
        raw_tpl = s.get("braid_template")
        summ = s.get("summary", "")
        if raw_tpl not in TEMPLATE_ROLE:
            dropped.append((raw_tpl, summ[:80], "unknown template"))
            continue
        ok, reason = classify_slice(raw_tpl, summ)
        if not ok:
            dropped.append((raw_tpl, summ[:80], reason))
            continue
        child = o.new_task(
            role=TEMPLATE_ROLE[raw_tpl],
            engine="codex",
            project=project["name"],
            summary=summ[:240],
            source=f"slice-of:{task_id}",
            braid_template=raw_tpl,
            parent_task_id=task_id,
        )
        o.enqueue_task(child)
        enqueued.append(child["task_id"])

    if dropped:
        with log_path.open("a") as logf:
            logf.write(f"\n# dropped {len(dropped)} slice(s):\n")
            for tpl, s80, reason in dropped:
                logf.write(f"#   template={tpl!r} reason={reason} :: {s80}\n")

    def mut(t):
        t["artifacts"] = t.get("artifacts", []) + enqueued
        t["finished_at"] = o.now_iso()
        t["log_path"] = str(log_path)
    o.move_task(task_id, "running", "done", reason=f"decomposed into {len(enqueued)}", mutator=mut)


def run_claude_reviewer(task, cfg, timeout, log_path):
    """Claude reviewer: claim one awaiting-review target and transition it.

    Loads the lvc-reviewer-pass BRAID template as system context plus the
    target's git diff vs main as user context, runs claude, parses verdict,
    promotes target → awaiting-qa on APPROVE or → failed on REQUEST_CHANGE.
    The reviewer task itself is marked done with the verdict in its log.
    """
    task_id = task["task_id"]
    project_name = task.get("project")
    try:
        project = o.get_project(cfg, project_name)
    except KeyError:
        o.move_task(task_id, "claimed", "failed", reason=f"unknown project {project_name}")
        return

    # Pick oldest awaiting-review task for this project (FIFO by finished_at).
    ar_dir = o.queue_dir("awaiting-review")
    candidates = []
    for p in ar_dir.glob("*.json"):
        t = o.read_json(p, {})
        if t.get("project") == project_name:
            candidates.append((t.get("finished_at") or p.name, t))
    if not candidates:
        def mut_noop(t):
            t["finished_at"] = o.now_iso()
            t["log_path"] = str(log_path)
        o.move_task(task_id, "claimed", "done", reason="nothing to review", mutator=mut_noop)
        return
    candidates.sort(key=lambda x: x[0])
    target = candidates[0][1]
    target_id = target["task_id"]

    graph_body, graph_hash = o.braid_template_load("lvc-reviewer-pass")
    if graph_body is None:
        o.move_task(task_id, "claimed", "failed", reason="reviewer template missing")
        return

    # Pull diff from the target's worktree (capped to keep prompt bounded).
    target_wt = target.get("worktree")
    diff_text = "(no worktree available)"
    if target_wt and pathlib.Path(target_wt).exists():
        try:
            stat = subprocess.run(
                ["git", "-C", target_wt, "diff", "main", "--stat"],
                capture_output=True, text=True, timeout=30,
            ).stdout
            body = subprocess.run(
                ["git", "-C", target_wt, "diff", "main"],
                capture_output=True, text=True, timeout=30,
            ).stdout
            if len(body) > 30000:
                body = body[:30000] + "\n\n[...diff truncated at 30k chars...]\n"
            diff_text = (stat + "\n\n" + body).strip() or "(empty diff vs main)"
        except subprocess.TimeoutExpired:
            diff_text = "(git diff timed out)"

    memory_ctx = read_memory_context(project["path"])

    system_prompt = (
        "You are the BRAID reviewer for devmini. Traverse the review graph below "
        "deterministically against the provided worktree diff. Do not freelance.\n\n"
        "[BRAID REASONING GRAPH — reviewer-pass]\n"
        f"{graph_body}\n"
        "[END GRAPH]\n\n"
        "Emit EXACTLY one of these as the final line of your response:\n"
        "  BRAID_OK: APPROVE — <one-line justification>\n"
        "  BRAID_OK: REQUEST_CHANGE — <one-line reason>\n"
        "  BRAID_TOPOLOGY_ERROR: <reason graph could not be traversed>"
    )
    user_prompt = (
        f"REVIEW TARGET: {target_id}\n"
        f"TARGET SUMMARY: {target.get('summary','')}\n"
        f"TARGET WORKTREE: {target_wt}\n\n"
        f"[DIFF vs main]\n{diff_text}\n\n"
        f"[PROJECT MEMORY]\n{memory_ctx}\n"
    )

    cmd = [
        "claude",
        "-p", user_prompt,
        "--dangerously-skip-permissions",
        "--system-prompt", system_prompt,
        "--output-format", "text",
        "--model", "sonnet",
        "--max-budget-usd", "1.00",
        "--disallowedTools", "Bash,Read,Write,Edit,Grep,Glob,Agent,WebFetch,WebSearch",
        "--no-session-persistence",
    ]

    def mut_running(t):
        t["started_at"] = o.now_iso()
        t["log_path"] = str(log_path)
        ea = dict(t.get("engine_args", {}))
        ea["target_task_id"] = target_id
        t["engine_args"] = ea
    o.move_task(task_id, "claimed", "running", reason=f"review of {target_id}", mutator=mut_running)

    with log_path.open("w") as logf:
        logf.write(f"# claude reviewer task={task_id} target={target_id}\n")
        logf.write(f"# template=lvc-reviewer-pass hash={graph_hash}\n\n")
        try:
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=logf, text=True, timeout=timeout,
                cwd="/tmp",
            )
        except subprocess.TimeoutExpired:
            o.braid_template_record_use("lvc-reviewer-pass", topology_error=True)
            o.move_task(task_id, "running", "failed", reason=f"claude timeout {timeout}s")
            return
        logf.write(f"\n# exit: {proc.returncode}\n# stdout:\n{proc.stdout or ''}\n")

    if proc.returncode != 0:
        o.move_task(task_id, "running", "failed", reason=f"claude exit {proc.returncode}")
        return

    raw = proc.stdout or ""
    verdict = None
    for line in reversed([l.strip() for l in raw.splitlines() if l.strip()][-15:]):
        if line.startswith("BRAID_OK: APPROVE"):
            verdict = "approve"; break
        if line.startswith("BRAID_OK: REQUEST_CHANGE"):
            verdict = "request_change"; break
        if line.startswith("BRAID_TOPOLOGY_ERROR"):
            verdict = "topology_error"; break

    if verdict is None:
        o.move_task(task_id, "running", "failed", reason="no review verdict trailer")
        return

    if verdict == "topology_error":
        o.braid_template_record_use("lvc-reviewer-pass", topology_error=True)
        o.move_task(task_id, "running", "failed", reason="reviewer topology error")
        return

    o.braid_template_record_use("lvc-reviewer-pass", topology_error=False)

    def mut_target(t):
        t["review_verdict"] = verdict
        t["reviewed_at"] = o.now_iso()
        t["reviewed_by"] = task_id

    if verdict == "approve":
        o.move_task(target_id, "awaiting-review", "awaiting-qa",
                    reason=f"approved by {task_id}", mutator=mut_target)
    else:
        o.move_task(target_id, "awaiting-review", "failed",
                    reason=f"review requested changes ({task_id})", mutator=mut_target)

    def mut_self(t):
        t["finished_at"] = o.now_iso()
    o.move_task(task_id, "running", "done",
                reason=f"reviewed {target_id} -> {verdict}", mutator=mut_self)


# --- codex slot -------------------------------------------------------------

def run_codex_slot(task, cfg):
    task_id = task["task_id"]
    bt = task.get("braid_template")

    if not bt:
        o.move_task(task_id, "claimed", "failed", reason="codex task has no braid_template")
        return

    graph_body, graph_hash = o.braid_template_load(bt)
    if graph_body is None:
        # Template missing. Block the task, enqueue claude regeneration, exit.
        if task.get("braid_generate_if_missing", True):
            regen = o.new_task(
                role="planner",
                engine="claude",
                project=task.get("project", "manual"),
                summary=f"Generate BRAID template for {bt}",
                source=f"regen-for:{task_id}",
                braid_template=bt,
                engine_args={"mode": "template-gen"},
            )
            o.enqueue_task(regen)
            def mut(t):
                t["topology_error"] = "template_missing"
            o.move_task(task_id, "claimed", "blocked", reason="template missing", mutator=mut)
            return
        o.move_task(task_id, "claimed", "failed", reason="template missing, regen disabled")
        return

    # Load project + create worktree.
    try:
        project = o.get_project(cfg, task["project"])
    except KeyError:
        o.move_task(task_id, "claimed", "failed", reason="unknown project")
        return

    lock_fh = None
    wt_path = None
    branch = None
    try:
        # Shared lock to coexist with other codex workers; blocks against regression exclusive.
        lock_fh = acquire_lock(f"{project['name']}.lock", mode="shared", timeout_sec=60)

        wt_path, branch = make_worktree(project["path"], task_id)

        memory_ctx = read_memory_context(project["path"])
        prompt = build_codex_prompt(task, graph_body, memory_ctx)

        timeout = task.get("engine_args", {}).get(
            "timeout_sec",
            cfg.get("slots", {}).get("codex", {}).get("timeout_sec", DEFAULT_TIMEOUTS["codex"]),
        )

        log_path = o.LOGS_DIR / f"{task_id}.log"
        last_msg_path = o.LOGS_DIR / f"{task_id}.last.txt"

        def mut_running(t):
            t["braid_template_path"] = f"braid/templates/{bt}.mmd"
            t["braid_template_hash"] = graph_hash
            t["worktree"] = str(wt_path)
            t["started_at"] = o.now_iso()
            t["log_path"] = str(log_path)
        o.move_task(task_id, "claimed", "running", reason="codex exec", mutator=mut_running)

        cmd = [
            "codex", "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C", str(wt_path),
            "--ephemeral",
            "-o", str(last_msg_path),
            prompt,
        ]

        with log_path.open("w") as logf:
            logf.write(f"# codex exec task={task_id} template={bt} hash={graph_hash}\n")
            logf.write(f"# worktree: {wt_path}\n")
            logf.write(f"# graph:\n{graph_body}\n\n---\n")
            logf.flush()
            try:
                proc = subprocess.run(
                    cmd, stdout=logf, stderr=subprocess.STDOUT, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                # Codex hit the slot wall — that's slot exhaustion, not a graph
                # traversal failure. Don't pollute the BRAID topology_errors
                # metric (regenerating the template won't fix a small budget).
                def mut_fail(t):
                    t["finished_at"] = o.now_iso()
                    t["failure"] = f"codex timeout {timeout}s"
                o.move_task(task_id, "running", "failed", reason=f"codex timeout {timeout}s", mutator=mut_fail)
                return

        trailer = ""
        if last_msg_path.exists():
            lines = [l.strip() for l in last_msg_path.read_text().splitlines() if l.strip()]
            # Paper's guidance: scan the last few non-empty lines.
            for line in reversed(lines[-20:]):
                if line.startswith("BRAID_OK") or line.startswith("BRAID_TOPOLOGY_ERROR"):
                    trailer = line
                    break

        if proc.returncode != 0 and not trailer:
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
            o.move_task(task_id, "running", "failed", reason=f"codex exit {proc.returncode}", mutator=mut_fail)
            return

        if trailer.startswith("BRAID_TOPOLOGY_ERROR"):
            if not topology_reason_is_valid(trailer):
                # False blocker claim — solver tried to exit with an
                # unrecognized reason (e.g. "pre-existing unrelated
                # failure"). Do NOT pollute topology_errors, do NOT
                # enqueue a useless regen, do NOT block downstream work
                # on this project.
                def mut_fail_false(t):
                    t["finished_at"] = o.now_iso()
                    t["topology_error"] = None
                    t["false_blocker_claim"] = trailer
                o.move_task(
                    task_id, "running", "failed",
                    reason=f"false blocker claim: {trailer[:80]}",
                    mutator=mut_fail_false,
                )
                return

            o.braid_template_record_use(bt, topology_error=True)
            regen = o.new_task(
                role="planner",
                engine="claude",
                project=project["name"],
                summary=f"Generate BRAID template for {bt}",
                source=f"regen-for:{task_id}",
                braid_template=bt,
                engine_args={"mode": "template-gen"},
            )
            o.enqueue_task(regen)
            def mut_block(t):
                t["finished_at"] = o.now_iso()
                t["topology_error"] = trailer
            o.move_task(task_id, "running", "blocked", reason=trailer[:80], mutator=mut_block)
            return

        if trailer.startswith("BRAID_OK"):
            o.braid_template_record_use(bt, topology_error=False)
            def mut_ok(t):
                t["finished_at"] = o.now_iso()
                t["artifacts"] = t.get("artifacts", []) + [branch]
            o.move_task(task_id, "running", "awaiting-review", reason=trailer[:80], mutator=mut_ok)
            return

        # Exit 0 but no recognizable trailer → treat as failed (CLI issue, not topology).
        def mut_fail(t):
            t["finished_at"] = o.now_iso()
        o.move_task(task_id, "running", "failed", reason="no BRAID trailer", mutator=mut_fail)

    finally:
        if lock_fh is not None:
            lock_fh.close()
        # Do NOT auto-remove the worktree — reviewer/QA still needs it.
        # pass-2 cleanup pass will sweep done worktrees on a schedule.


def build_codex_prompt(task, graph_body, memory_ctx):
    return (
        "[BRAID REASONING GRAPH — traverse deterministically. If you cannot, "
        "emit exactly one line `BRAID_TOPOLOGY_ERROR: <reason>` as the final line and stop.]\n\n"
        f"{graph_body}\n\n"
        "[END BRAID GRAPH]\n\n"
        "[TASK]\n"
        f"{task.get('summary','')}\n\n"
        "[REPO LAYOUT]\n"
        "All repo-memory files live under `repo-memory/` at the worktree root:\n"
        "  repo-memory/CURRENT_STATE.md  repo-memory/RECENT_WORK.md\n"
        "  repo-memory/DECISIONS.md      repo-memory/FAILURES.md\n"
        "  repo-memory/BENCHMARKS.md     repo-memory/RESEARCH.md\n"
        "Edit existing files in place — do NOT create new files at the worktree root.\n\n"
        "[PROJECT MEMORY]\n"
        f"{memory_ctx}\n\n"
        "[OUTPUT CONTRACT]\n"
        "Emit exactly one of these as the final line of your response:\n"
        "  BRAID_OK: <one-line summary of what you did>\n"
        "  BRAID_TOPOLOGY_ERROR: <reason the graph could not be traversed>\n"
    )


# --- qa slot ----------------------------------------------------------------

def write_regression_alert(project_name, task_id, reason, log_path):
    """Drop a markdown alert into REPORT_DIR so the telegram bot's push job
    picks it up on its next tick and fans it out to allowed chats.

    Body includes: project, task id, reason, and either the diff.md from the
    latest regression artifact dir (preferred) or the tail of the log.
    """
    ts = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    o.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    alert_path = o.REPORT_DIR / f"regression-failure_{project_name}_{ts}.md"

    diff_snippet = None
    artifacts_dir = QA_ARTIFACTS_ROOT / project_name / "regression"
    if artifacts_dir.is_dir():
        latest = sorted(
            (p for p in artifacts_dir.iterdir() if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
        for cand in latest[:1]:
            diff_file = cand / "diff.md"
            if diff_file.exists():
                try:
                    diff_snippet = diff_file.read_text()[:6000]
                except OSError:
                    diff_snippet = None
                break

    log_tail = None
    if log_path and pathlib.Path(log_path).exists():
        try:
            with open(log_path, "r", errors="replace") as f:
                lines = f.readlines()
            log_tail = "".join(lines[-40:])
        except OSError:
            log_tail = None

    body = [
        f"# REGRESSION FAILURE — {project_name}",
        "",
        f"- task: `{task_id}`",
        f"- reason: {reason}",
        f"- time: {o.now_iso()}",
        "",
        "**Hard stop**: no further implementer tasks for this project until"
        " a human reviews the blocked task.",
        "",
    ]
    if diff_snippet:
        body += ["## jmh_diff.py output", "", "```", diff_snippet.rstrip(), "```", ""]
    if log_tail:
        body += ["## last 40 log lines", "", "```", log_tail.rstrip(), "```", ""]
    alert_path.write_text("\n".join(body))
    log(f"wrote regression alert: {alert_path}")
    return alert_path


def run_qa_slot(task, cfg):
    """QA worker dispatch.

    smoke contract: claims oldest awaiting-qa target for the project, runs
    smoke.sh inside that target's worktree, transitions the TARGET task to
    done/failed based on exit code. The driver task itself just becomes done.

    regression contract: runs against the project's canonical repo path under
    an exclusive lock (no awaiting-qa coupling — regression is a periodic
    sweep, not a per-change drain).
    """
    task_id = task["task_id"]
    eargs = task.get("engine_args", {})
    contract_kind = eargs.get("contract", "smoke")

    try:
        project = o.get_project(cfg, task["project"])
    except KeyError:
        o.move_task(task_id, "claimed", "failed", reason="unknown project")
        return

    qa_cfg = project.get("qa", {})
    # engine_args.script lets a test harness or one-off task swap the contract
    # script without editing config (e.g., synthetic regression simulators).
    script_rel = eargs.get("script") or qa_cfg.get(contract_kind)
    if not script_rel:
        o.move_task(task_id, "claimed", "failed", reason=f"no qa.{contract_kind} in config")
        return

    # Resolve target + script path + cwd based on contract.
    target = None
    target_id = None
    if contract_kind == "smoke":
        ar_dir = o.queue_dir("awaiting-qa")
        candidates = []
        for p in ar_dir.glob("*.json"):
            t = o.read_json(p, {})
            if t.get("project") == project["name"]:
                candidates.append((t.get("reviewed_at") or t.get("finished_at") or p.name, t))
        if not candidates:
            def mut_noop(t):
                t["finished_at"] = o.now_iso()
            o.move_task(task_id, "claimed", "done",
                        reason="nothing awaiting-qa", mutator=mut_noop)
            return
        candidates.sort(key=lambda x: x[0])
        target = candidates[0][1]
        target_id = target["task_id"]
        target_wt = target.get("worktree")
        if not target_wt or not pathlib.Path(target_wt).exists():
            def mut_target_fail(t):
                t["finished_at"] = o.now_iso()
                t["qa_failure"] = "worktree missing"
            o.move_task(target_id, "awaiting-qa", "failed",
                        reason="qa target worktree missing", mutator=mut_target_fail)
            def mut_self(t):
                t["finished_at"] = o.now_iso()
            o.move_task(task_id, "claimed", "done",
                        reason=f"target {target_id} worktree gone", mutator=mut_self)
            return
        # Always run the canonical smoke.sh (latest version) but point REPO_ROOT
        # at the worktree so it tests the worktree's code. This avoids stale
        # smoke.sh copies in worktrees created before script edits.
        script_abs = pathlib.Path(project["path"]) / script_rel
        run_cwd = target_wt
        smoke_env_repo_root = target_wt
    else:  # regression
        script_abs = pathlib.Path(project["path"]) / script_rel
        run_cwd = project["path"]
        smoke_env_repo_root = None

    if not script_abs.exists():
        o.move_task(task_id, "claimed", "failed", reason=f"script missing: {script_abs}")
        return

    timeout = eargs.get(
        "timeout_sec",
        cfg.get("slots", {}).get("qa", {}).get("timeout_sec", DEFAULT_TIMEOUTS["qa"]),
    )
    if contract_kind == "regression":
        timeout = max(timeout, 8 * 3600)  # regression may take hours; cap at 8h

    log_path = o.LOGS_DIR / f"{task_id}.log"

    lock_fh = None
    try:
        lock_name = f"{project['name']}.lock"
        if contract_kind == "regression":
            lock_fh = acquire_lock(lock_name, mode="exclusive", timeout_sec=300)
        else:
            lock_fh = acquire_lock(lock_name, mode="shared", timeout_sec=30)

        def mut_running(t):
            t["started_at"] = o.now_iso()
            t["log_path"] = str(log_path)
            if target_id:
                ea = dict(t.get("engine_args", {}))
                ea["target_task_id"] = target_id
                t["engine_args"] = ea
        o.move_task(task_id, "claimed", "running", reason=f"qa {contract_kind}", mutator=mut_running)

        with log_path.open("w") as logf:
            logf.write(f"# qa {contract_kind} task={task_id} project={project['name']}\n")
            if target_id:
                logf.write(f"# target: {target_id}\n")
            logf.write(f"# script: {script_abs}\n# cwd: {run_cwd}\n")
            qa_env = os.environ.copy()
            if smoke_env_repo_root:
                qa_env["REPO_ROOT"] = str(smoke_env_repo_root)
                logf.write(f"# REPO_ROOT: {smoke_env_repo_root}\n")
            logf.write("\n")
            logf.flush()
            try:
                proc = subprocess.run(
                    ["bash", str(script_abs)],
                    cwd=run_cwd,
                    env=qa_env,
                    stdout=logf, stderr=subprocess.STDOUT, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                if contract_kind == "smoke" and target_id:
                    def mut_t_to(t):
                        t["finished_at"] = o.now_iso()
                        t["qa_failure"] = f"smoke timeout {timeout}s"
                    o.move_task(target_id, "awaiting-qa", "failed",
                                reason="smoke timeout", mutator=mut_t_to)
                    def mut_d_to(t):
                        t["finished_at"] = o.now_iso()
                    o.move_task(task_id, "running", "done",
                                reason=f"target timeout, driver done", mutator=mut_d_to)
                else:
                    o.move_task(task_id, "running", "failed", reason=f"qa timeout {timeout}s")
                return

        if proc.returncode == 0:
            if contract_kind == "smoke" and target_id:
                def mut_target_ok(t):
                    t["finished_at"] = o.now_iso()
                    t["qa_passed_at"] = o.now_iso()
                o.move_task(target_id, "awaiting-qa", "done",
                            reason=f"smoke pass via {task_id}", mutator=mut_target_ok)
                def mut_driver_ok(t):
                    t["finished_at"] = o.now_iso()
                o.move_task(task_id, "running", "done",
                            reason=f"smoke pass for {target_id}", mutator=mut_driver_ok)
            else:
                def mut_ok(t):
                    t["finished_at"] = o.now_iso()
                o.move_task(task_id, "running", "done",
                            reason=f"{contract_kind} pass", mutator=mut_ok)
            return

        # Non-zero exit.
        if contract_kind == "regression":
            def mut_block(t):
                t["finished_at"] = o.now_iso()
                t["topology_error"] = "regression-failure"
            o.move_task(task_id, "running", "blocked",
                        reason="regression failed", mutator=mut_block)
            try:
                write_regression_alert(
                    project["name"], task_id,
                    f"regression.sh exit {proc.returncode}", log_path,
                )
            except Exception as exc:
                log(f"regression alert write failed: {exc}")
        elif contract_kind == "smoke" and target_id:
            def mut_target_fail(t):
                t["finished_at"] = o.now_iso()
                t["qa_failure"] = f"smoke exit {proc.returncode}"
            o.move_task(target_id, "awaiting-qa", "failed",
                        reason=f"smoke fail via {task_id}", mutator=mut_target_fail)
            def mut_driver_fail(t):
                t["finished_at"] = o.now_iso()
            o.move_task(task_id, "running", "done",
                        reason=f"smoke fail for {target_id}", mutator=mut_driver_fail)
        else:
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
            o.move_task(task_id, "running", "failed",
                        reason=f"{contract_kind} exit {proc.returncode}", mutator=mut_fail)
    except TimeoutError as exc:
        o.move_task(task_id, "claimed", "failed", reason=str(exc))
    finally:
        if lock_fh is not None:
            lock_fh.close()


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(prog="worker")
    ap.add_argument("slot", choices=("claude", "codex", "qa"))
    args = ap.parse_args()

    slot = args.slot
    cfg = o.load_config()

    task = o.atomic_claim(slot)
    if task is None:
        # No work. Exit cleanly so launchd throttles respawn.
        return 0

    o.write_claim_pid(task["task_id"], slot)
    o.write_agent_status(f"worker-{slot}", "running", task["task_id"])

    try:
        if slot == "claude":
            run_claude_slot(task, cfg)
        elif slot == "codex":
            run_codex_slot(task, cfg)
        elif slot == "qa":
            run_qa_slot(task, cfg)
    except Exception as exc:
        import traceback
        log_path = o.LOGS_DIR / f"{task['task_id']}.log"
        with log_path.open("a") as f:
            f.write(f"\n# worker crashed:\n{traceback.format_exc()}\n")
        # Best-effort: try to move the task to failed wherever it is now.
        for st in ("running", "claimed"):
            if o.task_path(task["task_id"], st).exists():
                o.move_task(task["task_id"], st, "failed", reason=f"worker crash: {exc}")
                break
    finally:
        o.clear_claim_pid(task["task_id"])
        o.write_agent_status(f"worker-{slot}", "idle", "")

    return 0


if __name__ == "__main__":
    sys.exit(main())

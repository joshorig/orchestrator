#!/usr/bin/env python3
"""devmini worker — one task, then exit. Launchd respawns us.

Invoke as: worker.py <slot>   slot ∈ {claude, codex, qa}

Design choices:
- Bounded runs: claim ONE task, execute it, exit. No in-process loop.
- Atomic claim via os.rename (orchestrator.atomic_claim).
- Worktree isolation: worker.py manages git worktrees directly rather than
  calling an external helper script. That keeps worktree setup aligned with
  the repo's local config and avoids hardcoded host-specific paths.
- BRAID template resolution: codex slot only. Missing template → block task,
  enqueue claude regeneration, exit. Present template → load as system context.
- Trailer parsing: codex is invoked with `-o <last_msg_file>` which writes the
  final assistant message to disk, avoiding fragile log scraping.
"""
import argparse
import datetime as dt
import difflib
import fcntl
import hashlib
import json
import math
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orchestrator as o  # noqa: E402

WORKTREES_ROOT = o.DEV_ROOT / "worktrees"
QA_ARTIFACTS_ROOT = o.DEV_ROOT / "qa-artifacts"
SELF_REPAIR_COUNCIL_DEFAULT = ("socrates", "feynman", "ada", "torvalds")

# Default per-slot timeouts in seconds. Overridable via
# config["slots"][<slot>]["timeout_sec"] or task["engine_args"]["timeout_sec"].
DEFAULT_TIMEOUTS = {"claude": 1800, "codex": 3600, "qa": 900}
USAGE_JSON_RE = re.compile(r'^\s*\{.*"(input_tokens|output_tokens|cost_usd)".*\}\s*$')
CODEX_TURN_USAGE_RE = re.compile(r'"type"\s*:\s*"turn\.completed"')
CODEX_TOTAL_TOKENS_RE = re.compile(r"tokens used\s*\n([0-9,]+)", re.IGNORECASE | re.MULTILINE)
OPENAI_MODEL_PRICING = {
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.02, "output": 1.25},
}


def log(msg):
    sys.stderr.write(f"[worker {dt.datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    sys.stderr.flush()


def _run_bounded(cmd, *, timeout, cwd=None, env=None, stdout=None, stderr=None, text=True):
    """Run a subprocess in its own process group with TERM->KILL timeout handling."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=stdout,
        stderr=stderr,
        text=text,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout, output=out, stderr=err) from exc


def _record_task_costs_from_text(task_id, engine, model, text):
    if not task_id or not text:
        return 0
    def normalize_model(value):
        raw = str(value or model or "").strip().lower()
        for candidate in OPENAI_MODEL_PRICING:
            if candidate in raw:
                return candidate
        return raw or None

    def estimate_openai_cost(payload_model, input_tokens, output_tokens, cache_tokens):
        key = normalize_model(payload_model)
        pricing = OPENAI_MODEL_PRICING.get(key or "")
        if not pricing:
            return 0.0
        billable_input = max(int(input_tokens or 0) - int(cache_tokens or 0), 0)
        return (
            (billable_input * pricing["input"])
            + (int(cache_tokens or 0) * pricing["cached_input"])
            + (int(output_tokens or 0) * pricing["output"])
        ) / 1_000_000.0

    def extract_payload(payload):
        if not isinstance(payload, dict):
            return None
        if any(key in payload for key in ("input_tokens", "output_tokens", "cost_usd")):
            return {
                "engine": str(payload.get("slot") or engine or ""),
                "model": payload.get("model") or model,
                "input_tokens": int(payload.get("input_tokens") or 0),
                "output_tokens": int(payload.get("output_tokens") or 0),
                "cache_tokens": int(payload.get("cache_tokens") or payload.get("cached_tokens") or 0),
                "search_tokens": int(payload.get("search_tokens") or 0),
                "cost_usd": float(payload.get("cost_usd") or 0.0),
            }
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        if payload.get("type") == "turn.completed" or payload.get("type") == "result":
            cache_tokens = int(
                usage.get("cached_input_tokens")
                or usage.get("cache_read_input_tokens")
                or 0
            )
            parsed = {
                "engine": str(engine or ""),
                "model": payload.get("model") or model,
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cache_tokens": cache_tokens,
                "search_tokens": 0,
                "cost_usd": float(payload.get("total_cost_usd") or 0.0),
            }
            if not parsed["cost_usd"] and str(engine or "").lower() == "codex":
                parsed["cost_usd"] = estimate_openai_cost(
                    parsed["model"],
                    parsed["input_tokens"],
                    parsed["output_tokens"],
                    parsed["cache_tokens"],
                )
            return parsed
        return None

    recorded = 0
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line or not (USAGE_JSON_RE.match(line) or CODEX_TURN_USAGE_RE.search(line) or '"total_cost_usd"' in line):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed = extract_payload(payload)
        if not parsed:
            continue
        try:
            o.record_task_cost(
                task_id=task_id,
                engine=parsed["engine"],
                model=parsed["model"],
                input_tokens=parsed["input_tokens"],
                output_tokens=parsed["output_tokens"],
                cache_tokens=parsed["cache_tokens"],
                search_tokens=parsed["search_tokens"],
                cost_usd=parsed["cost_usd"],
            )
            recorded += 1
        except Exception:
            continue
    if recorded == 0 and str(engine or "").lower() == "codex":
        total_match = CODEX_TOTAL_TOKENS_RE.search(str(text))
        if total_match:
            try:
                o.record_task_cost(
                    task_id=task_id,
                    engine="codex",
                    model=model,
                    input_tokens=int(total_match.group(1).replace(",", "")),
                    output_tokens=0,
                    cache_tokens=0,
                    search_tokens=0,
                    cost_usd=0.0,
                )
                recorded = 1
            except Exception:
                pass
    return recorded


def _claude_subprocess_env(extra_env=None):
    env = os.environ.copy()
    blob = o.load_claude_env_blob() or ""
    for raw in blob.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in ("'", '"'):
            value = value[1:-1]
        if key not in env or not env[key]:
            env[key] = value
    if extra_env:
        env.update(extra_env)
    return env


def fail_task(task_id, from_state, reason, *, blocker_code=None, summary=None, detail=None, retryable=None, mutator=None):
    def mut(task):
        task["finished_at"] = o.now_iso()
        task["failure"] = reason
        if blocker_code:
            o.set_task_blocker(
                task,
                blocker_code,
                summary=summary or reason,
                detail=detail or reason,
                source="worker",
                retryable=retryable,
            )
        if mutator is not None:
            mutator(task)
    o.move_task(task_id, from_state, "failed", reason=reason[:200], mutator=mut)


def _claude_budget_exhausted(text):
    lower = (text or "").lower()
    return "budget" in lower and any(token in lower for token in ("exhaust", "limit", "max_budget", "max budget"))


def _pause_claude_slot_if_needed(reason, *, task=None):
    if not _claude_budget_exhausted(reason):
        return False
    lane = _claude_budget_lane(task)
    detail = f"Claude budget exhausted while handling {task.get('task_id') if task else '-'}: {reason[:180]}"
    o.set_slot_paused("claude", True, reason=detail, source="worker")
    o.append_metric(
        "claude.budget_exhausted",
        1,
        metric_type="counter",
        tags={
            "lane": lane,
            "project": (task or {}).get("project") or "",
            "role": (task or {}).get("role") or "",
            "mode": ((task or {}).get("engine_args") or {}).get("mode") or "",
        },
        source="worker",
    )
    if task:
        o.append_event(
            "worker",
            "slot_paused_budget_exhausted",
            task_id=task.get("task_id"),
            feature_id=task.get("feature_id"),
            details={"slot": "claude", "lane": lane, "reason": detail},
        )
    return True


def _classify_worker_crash(exc, tb_text=""):
    """Map worker crashes onto typed blocker codes so workflow-check can repair them.

    >>> _classify_worker_crash(TimeoutError("could not acquire shared lock lvc-standard.lock"))
    ('worker_crash_lock_contention', True)
    >>> import subprocess
    >>> _classify_worker_crash(subprocess.TimeoutExpired(["git"], 30))
    ('worker_crash_subprocess_timeout', True)
    >>> _classify_worker_crash(RuntimeError("git fetch failed"))
    ('worker_crash_git_failure', True)
    """
    import subprocess

    detail = " ".join(part for part in (str(exc or ""), tb_text or "") if part).lower()
    if ("shared lock" in detail or ".lock" in detail) and "acquire" in detail:
        return "worker_crash_lock_contention", True
    if isinstance(exc, subprocess.TimeoutExpired) or "timed out" in detail or "timeout" in detail:
        return "worker_crash_subprocess_timeout", True
    if "oom" in detail or "out of memory" in detail or "killed" in detail:
        return "worker_crash_oom_killed", False
    if "git " in detail or "git'" in detail or "git:" in detail or "git-" in detail:
        return "worker_crash_git_failure", True
    return "worker_crash_unhandled", True


def _handle_worker_crash(task, slot, exc, tb_text):
    """Persist worker crash state through the typed failure path.

    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     task_id = "task-lock"
    ...     calls = []
    ...     old = {
    ...         "LOGS_DIR": o.LOGS_DIR,
    ...         "record_worker_crash": o.record_worker_crash,
    ...         "task_path": o.task_path,
    ...         "fail_task": _handle_worker_crash.__globals__["fail_task"],
    ...     }
    ...     o.LOGS_DIR = pathlib.Path(tmp)
    ...     o.record_worker_crash = lambda slot_name, **kwargs: calls.append(("record", slot_name, kwargs["task_id"], kwargs["detail"]))
    ...     o.task_path = lambda tid, state: pathlib.Path(tmp) / state / f"{tid}.json"
    ...     (pathlib.Path(tmp) / "claimed").mkdir()
    ...     _ = (pathlib.Path(tmp) / "claimed" / f"{task_id}.json").write_text("{}")
    ...     _handle_worker_crash.__globals__["fail_task"] = lambda tid, state, reason, **kwargs: calls.append(("fail", tid, state, reason, kwargs["blocker_code"], kwargs["retryable"]))
    ...     _handle_worker_crash({"task_id": task_id}, "codex", TimeoutError("could not acquire shared lock lvc-standard.lock"), "traceback text")
    ...     o.LOGS_DIR = old["LOGS_DIR"]
    ...     o.record_worker_crash = old["record_worker_crash"]
    ...     o.task_path = old["task_path"]
    ...     _handle_worker_crash.__globals__["fail_task"] = old["fail_task"]
    ...     calls
    [('record', 'codex', 'task-lock', 'could not acquire shared lock lvc-standard.lock'), ('fail', 'task-lock', 'claimed', 'worker crash: could not acquire shared lock lvc-standard.lock', 'worker_crash_lock_contention', True)]
    """
    log_path = o.LOGS_DIR / f"{task['task_id']}.log"
    with log_path.open("a") as f:
        f.write(f"\n# worker crashed:\n{tb_text}\n")
    o.record_worker_crash(slot, task_id=task["task_id"], detail=str(exc))
    blocker_code, retryable = _classify_worker_crash(exc, tb_text)
    # Best-effort: move through the normal failure path so blocker metadata is persisted.
    for st in ("running", "claimed"):
        if o.find_task(task["task_id"], states=(st,)) is not None:
            fail_task(
                task["task_id"],
                st,
                f"worker crash: {exc}",
                blocker_code=blocker_code,
                summary="worker crashed mid-task",
                detail=f"{exc}\n\n{tb_text[-4000:]}".strip(),
                retryable=retryable,
            )
            break


def _claude_budget_flag(kind, *, cfg, task=None, mode=None):
    return f"{o.claude_budget_usd(kind, cfg=cfg, task=task, mode=mode):.2f}"


def _claude_budget_lane(task):
    if not task:
        return "unknown"
    mode = ((task.get("engine_args") or {}).get("mode") or "").strip()
    role = (task.get("role") or "").strip()
    if mode == "self-repair-plan":
        return "self_repair"
    if mode == "memory-synthesis":
        return "memory_synthesis"
    if mode == "template-refine":
        return "template_refine"
    if mode == "template-gen":
        return "template_gen"
    if role == "reviewer":
        return "review"
    if role == "planner":
        return "planner"
    return "claude_default"


def block_task(task_id, from_state, reason, *, blocker_code, summary=None, detail=None, retryable=True, mutator=None):
    def mut(task):
        task["finished_at"] = o.now_iso()
        o.set_task_blocker(
            task,
            blocker_code,
            summary=summary or reason,
            detail=detail or reason,
            source="worker",
            retryable=retryable,
        )
        if mutator is not None:
            mutator(task)
    o.move_task(task_id, from_state, "blocked", reason=reason[:200], mutator=mut)


def _mark_review_target_for_retry(target_id, reason, *, blocker_code="runtime_precondition_failed", metadata=None):
    found = o.find_task(target_id, states=("awaiting-review",))
    if not found:
        return False
    state, target = found
    target_path = o.task_path(target_id, state)
    def mut(task):
        task["review_failure"] = reason
        task["review_failure_at"] = o.now_iso()
        o.set_task_blocker(
            task,
            blocker_code,
            summary="reviewer task failed",
            detail=reason,
            source="worker",
            retryable=True,
            metadata=metadata,
        )
    o.update_task_in_place(target_path, mut)
    return True


def _newer_review_feedback_exists(feature_id, target_id, current_task_id, current_created_at):
    for sibling in o.iter_tasks(states=o.STATES):
        if sibling.get("task_id") == current_task_id:
            continue
        if sibling.get("feature_id") != feature_id:
            continue
        if not str(sibling.get("source") or "").startswith("review-feedback:"):
            continue
        sibling_target = ((sibling.get("engine_args") or {}).get("target_task_id") or sibling.get("parent_task_id"))
        if sibling_target != target_id:
            continue
        if (sibling.get("created_at") or "") > (current_created_at or ""):
            return True
    return False


# --- git worktree management ------------------------------------------------

def base_branch_for_task(task):
    """Return the branch name agent work should be based on.

    Tasks with a feature_id base their agent branch on `feature/<id>` so that
    sibling slices of the same feature compose on a shared integration branch.
    Tasks without a feature_id (historian, one-off manual runs) base on main.
    """
    fid = task.get("feature_id") if task else None
    return f"feature/{fid}" if fid else "main"


class MainDirtyOrAhead(RuntimeError):
    """Raised when local main cannot safely host a new feature branch cut."""


def _preflight_doctest_setup():
    """Create a disposable repo + file:// remote for preflight_main doctests."""
    tmp = tempfile.TemporaryDirectory()
    root = pathlib.Path(tmp.name)
    remote = root / "remote.git"
    seed = root / "seed"
    canonical = root / "canonical"
    remote_uri = remote.resolve().as_uri()

    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True, text=True)
    for repo in (seed,):
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Doctest"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "doctest@example.com"], check=True, capture_output=True, text=True)
    (seed / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "initial"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", remote_uri], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(seed), "push", "-u", "origin", "main"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", "--branch", "main", remote_uri, str(canonical)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(canonical), "config", "user.name", "Doctest"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(canonical), "config", "user.email", "doctest@example.com"], check=True, capture_output=True, text=True)
    return tmp, {"root": root, "remote": remote, "remote_uri": remote_uri, "seed": seed, "canonical": canonical}


def preflight_main(project_path):
    """Verify local main is clean and not ahead of origin/main.

    Performs an authoritative `git fetch origin main`, then refuses if the
    canonical checkout has uncommitted changes or if local `main` contains
    commits that `origin/main` lacks. Returns `None` on success and raises
    `MainDirtyOrAhead` on refusal.

    >>> tmp, env = _preflight_doctest_setup()
    >>> preflight_main(str(env["canonical"])) is None
    True
    >>> _ = subprocess.run(
    ...     ["git", "-C", str(env["seed"]), "commit", "--allow-empty", "-m", "remote-ahead"],
    ...     check=True, capture_output=True, text=True,
    ... )
    >>> _ = subprocess.run(
    ...     ["git", "-C", str(env["seed"]), "push", "origin", "main"],
    ...     check=True, capture_output=True, text=True,
    ... )
    >>> preflight_main(str(env["canonical"])) is None
    True
    >>> tmp.cleanup()

    >>> tmp, env = _preflight_doctest_setup()
    >>> _ = (env["canonical"] / "dirty.txt").write_text("dirty\\n")
    >>> try:
    ...     preflight_main(str(env["canonical"]))
    ... except MainDirtyOrAhead as exc:
    ...     "uncommitted" in str(exc)
    True
    >>> tmp.cleanup()

    >>> tmp, env = _preflight_doctest_setup()
    >>> _ = (env["canonical"] / "staged.txt").write_text("staged\\n")
    >>> _ = subprocess.run(
    ...     ["git", "-C", str(env["canonical"]), "add", "staged.txt"],
    ...     check=True, capture_output=True, text=True,
    ... )
    >>> try:
    ...     preflight_main(str(env["canonical"]))
    ... except MainDirtyOrAhead as exc:
    ...     "uncommitted" in str(exc)
    True
    >>> tmp.cleanup()

    >>> tmp, env = _preflight_doctest_setup()
    >>> _ = subprocess.run(
    ...     ["git", "-C", str(env["canonical"]), "commit", "--allow-empty", "-m", "local-ahead"],
    ...     check=True, capture_output=True, text=True,
    ... )
    >>> try:
    ...     preflight_main(str(env["canonical"]))
    ... except MainDirtyOrAhead as exc:
    ...     "not in origin/main" in str(exc)
    True
    >>> tmp.cleanup()

    >>> tmp, env = _preflight_doctest_setup()
    >>> missing_remote = env["root"] / "missing.git"
    >>> _ = subprocess.run(
    ...     ["git", "-C", str(env["canonical"]), "remote", "set-url", "origin", missing_remote.resolve().as_uri()],
    ...     check=True, capture_output=True, text=True,
    ... )
    >>> try:
    ...     preflight_main(str(env["canonical"]))
    ... except MainDirtyOrAhead as exc:
    ...     "fetch origin main failed" in str(exc)
    True
    >>> tmp.cleanup()
    """
    fetch = subprocess.run(
        ["git", "-C", project_path, "fetch", "origin", "main"],
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        detail = (fetch.stderr or fetch.stdout or "").strip()
        raise MainDirtyOrAhead(
            f"fetch origin main failed: {detail[:200] or 'unknown git fetch failure'}"
        )

    status = subprocess.run(
        ["git", "-C", project_path, "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    if status.stdout.strip():
        raise MainDirtyOrAhead(
            f"{project_path} has uncommitted changes on main working tree"
        )

    ancestor = subprocess.run(
        ["git", "-C", project_path, "merge-base", "--is-ancestor", "main", "origin/main"],
        capture_output=True,
        text=True,
    )
    if ancestor.returncode != 0:
        raise MainDirtyOrAhead(
            f"{project_path} local main has commits not in origin/main"
        )


def ensure_feature_branch(project_path, feature_branch, *, allow_dirty_main=False):
    """Create `feature_branch` locally + on origin if it doesn't already exist.

    Idempotent. Safe to call repeatedly — the first caller creates the branch,
    subsequent callers observe it already present and return. Uses origin/main
    as the starting point so every feature branch begins from the current
    trunk HEAD at the moment of first use.

    Returns True if the branch was newly created, False if it already existed.
    """
    if not allow_dirty_main:
        preflight_main(project_path)

    # Fast path: branch already present locally.
    probe = subprocess.run(
        ["git", "-C", project_path, "rev-parse", "--verify", "--quiet", feature_branch],
        capture_output=True, text=True,
    )
    if probe.returncode == 0:
        return False

    # Fetch origin so we can check + start from a fresh main when possible.
    subprocess.run(
        ["git", "-C", project_path, "fetch", "origin", "main",
         f"+refs/heads/{feature_branch}:refs/remotes/origin/{feature_branch}"],
        capture_output=True, text=True,
    )
    # If origin already has the branch, track it locally and return.
    remote_probe = subprocess.run(
        ["git", "-C", project_path, "rev-parse", "--verify", "--quiet",
         f"refs/remotes/origin/{feature_branch}"],
        capture_output=True, text=True,
    )
    if remote_probe.returncode == 0:
        subprocess.run(
            ["git", "-C", project_path, "branch", feature_branch,
             f"origin/{feature_branch}"],
            capture_output=True, text=True, check=False,
        )
        return False

    # Create the branch locally off origin/main when the canonical checkout is
    # clean. Self-repair may intentionally relax that preflight and branch from
    # the current local main so the repair can proceed from a clean worktree
    # even while the canonical checkout is noisy.
    start_ref = "main" if allow_dirty_main else "origin/main"
    create = subprocess.run(
        ["git", "-C", project_path, "branch", feature_branch, start_ref],
        capture_output=True, text=True,
    )
    if create.returncode != 0:
        raise RuntimeError(
            f"failed to create feature branch {feature_branch}: {create.stderr.strip()}"
        )
    push = subprocess.run(
        ["git", "-C", project_path, *AGENT_GIT_IDENTITY,
         "push", "-u", "origin", feature_branch],
        capture_output=True, text=True, timeout=120,
    )
    if push.returncode != 0:
        # Non-fatal: the branch exists locally and codex can still work on it.
        # A later slice or the finalize step will retry the push.
        log(f"ensure_feature_branch: push {feature_branch} failed: {push.stderr.strip()[:200]}")
    return True


def make_worktree(project_path, task_id, base_branch="main"):
    """Create an isolated worktree + agent branch rooted at base_branch.

    Returns (wt_path, agent_branch, base_branch). The base_branch is returned
    so downstream helpers (push, pr-body, create_pr) can target the same ref
    the agent branched from — critical when the base is a feature branch that
    may advance between slices.
    """
    WORKTREES_ROOT.mkdir(parents=True, exist_ok=True)
    repo_name = pathlib.Path(project_path).name
    wt_root = WORKTREES_ROOT / repo_name
    wt_root.mkdir(parents=True, exist_ok=True)
    wt_path = wt_root / task_id
    branch = f"agent/{task_id}"
    # A previous `git worktree add` can fail after creating the branch/worktree
    # registration, leaving a clean but unusable skeleton behind. Clean that up
    # before retrying so the task can resume without manual branch surgery.
    existing = subprocess.run(
        ["git", "-C", project_path, "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    if f"worktree {wt_path}\n" in existing.stdout:
        subprocess.run(
            ["git", "-C", project_path, "worktree", "remove", "--force", str(wt_path)],
            capture_output=True,
            text=True,
            check=False,
        )
    branch_probe = subprocess.run(
        ["git", "-C", project_path, "rev-parse", "--verify", "--quiet", branch],
        capture_output=True,
        text=True,
        check=False,
    )
    if branch_probe.returncode == 0:
        subprocess.run(
            ["git", "-C", project_path, "branch", "-D", branch],
            capture_output=True,
            text=True,
            check=False,
        )
    subprocess.run(
        ["git", "worktree", "add", str(wt_path), "-b", branch, base_branch],
        cwd=project_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return wt_path, branch, base_branch


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


# --- push + pr-body (pattern B delivery) ------------------------------------
#
# When a target task passes smoke.sh in its worktree, we always write a
# pr-body.md artifact capturing the task context, BRAID provenance, diff,
# smoke-log tail, and reviewer verdict. A human (or a future automated step)
# can feed this to `gh pr create --body-file` verbatim.
#
# If the project has `auto_push: true`, we additionally commit-and-push the
# worktree branch to origin under a distinct agent identity so a PR can be
# opened against it. Human-opened PRs still benefit from pr-body.md regardless.
#
# Secret scanning runs before any commit or push. Any hit aborts the push
# (target task → failed with push_failure) while leaving the worktree intact
# for human inspection.

AGENT_GIT_IDENTITY = [
    "-c", "user.name=devmini-orchestrator",
    "-c", "user.email=devmini-orchestrator@joshorig.com",
]

_SECRET_PATTERNS = [
    (re.compile(r"\btelegram\.json\b"), "telegram.json"),
    (re.compile(r"(^|/)\.env(\.|$)"), ".env file"),
    (re.compile(r"BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY"), "private key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "github personal token"),
    (re.compile(r"\bghs_[A-Za-z0-9]{20,}"), "github server token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "slack token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "openai-style key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws access key"),
    (re.compile(r"\bcredentials\.json\b"), "credentials.json"),
]

_SECRET_FALSE_POSITIVE_RE = re.compile(
    r"^(?:sha(?:1|256|512):)?[0-9a-f]{32,64}$|^[0-9a-f]{7,40}$|^[A-F0-9]{32,64}$"
)


def scan_for_secrets(text):
    """Return a list of (label, snippet) for any secret-like patterns found."""
    if not text:
        return []
    hits = []
    for pat, label in _SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 20)
            hits.append((label, text[start:end].replace("\n", " ")))
    for token in re.findall(r"\b[A-Za-z0-9+/=_-]{24,}\b", text):
        if _SECRET_FALSE_POSITIVE_RE.match(token):
            continue
        if token.isalpha():
            continue
        alphabet = set(token)
        if len(alphabet) < 6:
            continue
        counts = {}
        for ch in token:
            counts[ch] = counts.get(ch, 0) + 1
        entropy = 0.0
        for count in counts.values():
            p = count / len(token)
            entropy -= p * math.log2(p)
        if entropy >= 4.1:
            hits.append(("high-entropy token", token[:12] + "..." + token[-8:]))
    return hits


def _changed_files_against_base(wt: pathlib.Path, base_branch: str):
    files = set()
    for args in (
        ("diff", "--name-only", "-z", base_branch),
        ("diff", "--cached", "--name-only", "-z"),
        ("diff", "--name-only", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        proc = _git(wt, *args)
        if proc.returncode != 0:
            continue
        for part in (proc.stdout or "").split("\x00"):
            if part:
                files.add(part)
    return sorted(files)


def _detect_secrets_hook_findings(wt: pathlib.Path, filenames, baseline_path: pathlib.Path | None = None):
    """Run `detect-secrets-hook` against changed files when configured.

    Returns a list of finding dicts, `[]` for no findings, or `None` when the
    hook should not run (missing binary, missing baseline, or no files).

    >>> import json, pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     baseline = root / ".secrets.baseline"
    ...     _ = baseline.write_text("{}")
    ...     old = {k: _detect_secrets_hook_findings.__globals__[k] for k in ("shutil", "subprocess")}
    ...     class FakeShutil:
    ...         @staticmethod
    ...         def which(name):
    ...             return "/usr/bin/detect-secrets-hook" if name == "detect-secrets-hook" else None
    ...     class FakeSubprocess:
    ...         @staticmethod
    ...         def run(cmd, **kwargs):
    ...             payload = [{"filename": "foo.py", "type": "Secret Keyword"}]
    ...             return types.SimpleNamespace(returncode=1, stdout=json.dumps(payload), stderr="")
    ...     _detect_secrets_hook_findings.__globals__["shutil"] = FakeShutil()
    ...     _detect_secrets_hook_findings.__globals__["subprocess"] = FakeSubprocess()
    ...     out1 = _detect_secrets_hook_findings(root, ["foo.py"], baseline)
    ...     out2 = _detect_secrets_hook_findings(root, [], baseline)
    ...     out3 = _detect_secrets_hook_findings(root, ["foo.py"], root / "missing.baseline")
    ...     for key, value in old.items():
    ...         _detect_secrets_hook_findings.__globals__[key] = value
    >>> out1[0]["filename"], out2 is None, out3 is None
    ('foo.py', True, True)
    """
    filenames = [name for name in filenames if name]
    if not filenames:
        return None
    hook = shutil.which("detect-secrets-hook")
    if not hook:
        return None
    baseline = baseline_path or (wt / ".secrets.baseline")
    if not baseline.exists():
        return None
    proc = subprocess.run(
        [hook, "--json", "--baseline", str(baseline), *filenames],
        cwd=str(wt),
        text=True,
        capture_output=True,
    )
    body = (proc.stdout or "").strip()
    if not body:
        return []
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return [{"raw": body, "error": proc.stderr.strip()}]
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        findings = parsed.get("results")
        if isinstance(findings, list):
            return findings
        if findings is None and parsed:
            return [parsed]
        return findings or []
    return [{"raw": body}]


def _repo_memory_secret_hits(wt: pathlib.Path):
    """Inspect changed repo-memory markdown files for secret-like content.

    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     wt = pathlib.Path(tmp) / "repo"
    ...     _ = subprocess.run(["git", "init", "-b", "main", str(wt)], check=True, capture_output=True, text=True)
    ...     _ = subprocess.run(["git", "-C", str(wt), "config", "user.name", "Doctest"], check=True, capture_output=True, text=True)
    ...     _ = subprocess.run(["git", "-C", str(wt), "config", "user.email", "doctest@example.com"], check=True, capture_output=True, text=True)
    ...     (wt / "repo-memory").mkdir()
    ...     _ = (wt / "repo-memory" / "RECENT_WORK.md").write_text("AKIA1234567890ABCDEF\\n")
    ...     hits = _repo_memory_secret_hits(wt)
    >>> hits[0][0]
    'aws access key'
    """
    status = _git(wt, "status", "--porcelain", "--untracked-files=all").stdout or ""
    paths = set()
    for raw in status.splitlines():
        if not raw:
            continue
        path = raw[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path.startswith("repo-memory/") and path.endswith(".md"):
            paths.add(path)
    hits = []
    for rel in sorted(paths):
        full = wt / rel
        if not full.exists():
            continue
        for label, snippet in scan_for_secrets(full.read_text(errors="replace")):
            hits.append((label, f"{rel}: {snippet}"))
    return hits


def write_repo_memory_secret_alert(project_name, task_id, reason, hits, log_path=None):
    ts = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    o.REPORT_DIR.mkdir(parents=True, exist_ok=True)
    alert_path = o.REPORT_DIR / f"repo-memory-secret_{project_name}_{ts}.md"
    body = [
        f"# REPO-MEMORY SECRET BLOCK — {project_name}",
        "",
        f"- task: `{task_id}`",
        f"- reason: {reason}",
        f"- time: {o.now_iso()}",
        "",
        "Historian output was blocked because repo-memory markdown matched the secret detector.",
        "",
        "## Findings",
    ]
    for label, snippet in hits[:10]:
        body.append(f"- {label}: `{snippet[:200]}`")
    if log_path:
        body += ["", f"Log: `{log_path}`"]
    alert_path.write_text("\n".join(body) + "\n")
    log(f"wrote repo-memory secret alert: {alert_path}")
    return alert_path


def _git(worktree, *args, check=False, timeout=30):
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True, text=True, check=check, timeout=timeout,
    )


def _git_agent(worktree, *args, check=False, timeout=60):
    return subprocess.run(
        ["git", "-C", str(worktree), *AGENT_GIT_IDENTITY, *args],
        capture_output=True, text=True, check=check, timeout=timeout,
    )


def _autocommit_doctest_case(dirty):
    with tempfile.TemporaryDirectory() as tmp:
        wt = pathlib.Path(tmp) / "repo"
        log_path = pathlib.Path(tmp) / "auto.log"
        _ = subprocess.run(["git", "init", "-b", "main", str(wt)], check=True, capture_output=True, text=True)
        _ = subprocess.run(["git", "-C", str(wt), "config", "user.name", "Doctest"], check=True, capture_output=True, text=True)
        _ = subprocess.run(["git", "-C", str(wt), "config", "user.email", "doctest@example.com"], check=True, capture_output=True, text=True)
        _ = (wt / "tracked.txt").write_text("base\n")
        _ = subprocess.run(["git", "-C", str(wt), "add", "tracked.txt"], check=True, capture_output=True, text=True)
        _ = subprocess.run(["git", "-C", str(wt), "commit", "-m", "base"], check=True, capture_output=True, text=True)
        if dirty:
            _ = (wt / "new.txt").write_text("untracked\n")
        result = _autocommit_worktree(wt, {"task_id": "task-1", "summary": "Demo"}, str(log_path))
        if not dirty:
            return result
        head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        clean = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"], check=True, capture_output=True, text=True).stdout.strip()
        body = log_path.read_text()
        return (result[0], result[1] == head, "# AUTO-COMMIT " in body, clean == "")


def _autocommit_worktree(wt: pathlib.Path, target: dict, log_path: str):
    """Commit any dirty tracked/untracked worktree changes under agent identity.

    Returns (ok: bool, sha_or_err: str). A clean worktree is a successful no-op.

    >>> _autocommit_doctest_case(True)
    (True, True, True, True)

    >>> _autocommit_doctest_case(False)
    (True, 'clean')
    """
    status = _git(wt, "status", "--porcelain").stdout
    if not status.strip():
        return (True, "clean")
    repo_memory_hits = _repo_memory_secret_hits(wt)
    if repo_memory_hits:
        labels = ", ".join(sorted({label for label, _ in repo_memory_hits}))
        try:
            with open(log_path, "a") as f:
                f.write(f"\n# AUTO-COMMIT WARNING: repo-memory secret-scan hit ({labels})\n")
                for label, snippet in repo_memory_hits[:8]:
                    f.write(f"#   {label}: {snippet[:200]}\n")
        except Exception:
            pass
    add = _git_agent(wt, "add", "-A")
    if add.returncode:
        return (False, f"add: {add.stderr.strip()}")
    msg = f"agent: {target.get('summary', target['task_id'])}\n\ntask_id: {target['task_id']}\n"
    commit = _git_agent(wt, "commit", "-m", msg)
    if commit.returncode:
        return (False, f"commit: {commit.stderr.strip()}")
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
    with open(log_path, "a") as f:
        f.write(f"\n# AUTO-COMMIT {sha}\n")
    return (True, sha)


def write_pr_body(target, project, qa_log_path, driver_task_id, base_branch="main"):
    """Write artifacts/<target_id>/pr-body.md with PR-ready evidence.

    Always called on smoke pass, independent of auto_push. Returns the written
    path or None on failure (non-fatal).

    base_branch: the git ref this work was based on. Typically "main" for
    one-off tasks, or `feature/<id>` for slices of a feature. All diff/log
    computations target this ref so the evidence reflects exactly what the
    PR will show.
    """
    target_id = target.get("task_id", "unknown")
    wt = target.get("worktree")
    if not wt or not pathlib.Path(wt).exists():
        return None

    art_dir = o.STATE_ROOT / "artifacts" / target_id
    art_dir.mkdir(parents=True, exist_ok=True)
    out_path = art_dir / "pr-body.md"

    branch = f"agent/{target_id}"
    diff_stat = _git(wt, "diff", base_branch, "--stat").stdout.strip()
    commit_log = _git(wt, "log", f"{base_branch}..HEAD", "--pretty=format:- %h %s").stdout.strip()
    if not commit_log:
        commit_log = "(no commits yet — pending auto-commit on push)"
    changed_files = _git(wt, "diff", base_branch, "--name-only").stdout.strip() or f"(no changes vs {base_branch})"

    log_tail = ""
    try:
        if qa_log_path and pathlib.Path(qa_log_path).exists():
            lines = pathlib.Path(qa_log_path).read_text(errors="replace").splitlines()
            log_tail = "\n".join(lines[-120:])
    except Exception as exc:
        log_tail = f"(could not read qa log: {exc})"

    bt = target.get("braid_template") or "(none)"
    bt_hash = target.get("braid_template_hash") or "(none)"
    parent = target.get("parent_task_id") or "(none)"
    review_verdict = target.get("review_verdict") or "(not reviewed)"
    reviewed_by = target.get("reviewed_by") or ""

    body = f"""# {target.get('summary', target_id)}

<!--
  devmini-orchestrator: PR body artifact for task {target_id}.
  Smoke gate passed via driver task {driver_task_id} at {o.now_iso()}.
-->

@codex please review this change.

## Task

- **Task id:** `{target_id}`
- **Project:** `{project['name']}`
- **Parent:** `{parent}`
- **Branch:** `{branch}`
- **Base:** `{base_branch}`
- **Summary:** {target.get('summary', '(no summary)')}

## BRAID provenance

- **Template:** `{bt}`
- **Template hash:** `{bt_hash}`
- **Reviewer verdict:** {review_verdict}{' (by ' + reviewed_by + ')' if reviewed_by else ''}

## Changes

### Files touched

```
{changed_files}
```

### Commits on branch

{commit_log}

### Diff stat vs {base_branch}

```
{diff_stat or '(empty)'}
```

## QA evidence

Smoke script: `{project.get('qa', {}).get('smoke', '(none)')}`
Smoke log (last 120 lines):

```
{log_tail}
```

Full log: `{qa_log_path}`

## Manual PR open

```
gh -R <owner>/{pathlib.Path(project['path']).name} pr create \\
  --head {branch} --base {base_branch} \\
  --title {shlex.quote(target.get('summary', target_id))} \\
  --body-file {out_path}
```
"""
    out_path.write_text(body)
    return out_path


def push_worktree_branch(target, project, worktree, branch, log_path, base_branch="main"):
    """Commit any pending worktree changes and push the branch to origin.

    Returns (ok: bool, reason: str, commit_sha: str|None,
             commit_count: int, pushed_at: str|None).

    Safety gates:
      - never pushes `main`
      - uses a distinct `devmini-orchestrator` commit identity
      - leaves the worktree intact on failure for human inspection

    base_branch: the ref the agent branch was created from. Used for the
    secret-scan diff and the ahead-count check. Typically "main" or
    "feature/<id>".

    >>> import pathlib, tempfile, types
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     wt = root / "wt"
    ...     wt.mkdir()
    ...     log_path = root / "push.log"
    ...     original = {k: push_worktree_branch.__globals__[k] for k in ("scan_for_secrets", "_detect_secrets_hook_findings", "_changed_files_against_base", "_git", "_git_agent", "o")}
    ...     class FakeO:
    ...         @staticmethod
    ...         def now_iso():
    ...             return "2026-04-14T23:30:00"
    ...     def fake_git(path, *args, **kwargs):
    ...         cmd = tuple(args)
    ...         if cmd[:3] == ("diff", "--name-only", "-z") or cmd[:3] == ("diff", "--cached", "--name-only") or cmd[:2] == ("diff", "--name-only") or cmd[:2] == ("ls-files", "--others"):
    ...             return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    ...         if cmd[:3] == ("diff", "--cached") or cmd[:1] == ("diff",):
    ...             return types.SimpleNamespace(returncode=0, stdout="diff --git a/.github/workflows/unresolved-bot-review.yml b/.github/workflows/unresolved-bot-review.yml", stderr="")
    ...         if cmd[:2] == ("status", "--porcelain"):
    ...             return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    ...         if cmd[:2] == ("rev-list", "--count"):
    ...             return types.SimpleNamespace(returncode=0, stdout="1\\n", stderr="")
    ...         if cmd[:2] == ("rev-parse", "HEAD"):
    ...             return types.SimpleNamespace(returncode=0, stdout="abc123\\n", stderr="")
    ...         raise AssertionError(cmd)
    ...     def fake_git_agent(path, *args, **kwargs):
    ...         if args[:3] == ("push", "-u", "origin"):
    ...             return types.SimpleNamespace(returncode=0, stdout="ok\\n", stderr="")
    ...         raise AssertionError(args)
    ...     push_worktree_branch.__globals__["scan_for_secrets"] = lambda text: [("high-entropy token", "github/workf...t-review")]
    ...     push_worktree_branch.__globals__["_detect_secrets_hook_findings"] = lambda wt, filenames: None
    ...     push_worktree_branch.__globals__["_changed_files_against_base"] = lambda wt, base: []
    ...     push_worktree_branch.__globals__["_git"] = fake_git
    ...     push_worktree_branch.__globals__["_git_agent"] = fake_git_agent
    ...     push_worktree_branch.__globals__["o"] = FakeO()
    ...     out = push_worktree_branch({"task_id": "task-1", "summary": "Demo"}, {"name": "demo"}, str(wt), "agent/task-1", str(log_path), base_branch="main")
    ...     log_body = log_path.read_text()
    ...     for key, value in original.items():
    ...         push_worktree_branch.__globals__[key] = value
    >>> out[0], out[1], "PUSH WARNING" in log_body, "PUSH ABORTED" in log_body
    (True, 'ok', True, False)
    """
    if branch == "main" or branch.endswith("/main"):
        return (False, "refuse to push main", None, 0, None)

    wt = pathlib.Path(worktree)
    if not wt.exists():
        return (False, "worktree missing", None, 0, None)

    # Advisory secret scan: log suspicious diff content, but don't block pushes.
    full_diff = _git(wt, "diff", base_branch).stdout or ""
    staged = _git(wt, "diff", "--cached").stdout or ""
    unstaged = _git(wt, "diff").stdout or ""
    combined = full_diff + "\n" + staged + "\n" + unstaged
    advisory_hits = scan_for_secrets(combined)
    if advisory_hits:
        labels = ", ".join(sorted({h[0] for h in advisory_hits}))
        try:
            with open(log_path, "a") as f:
                f.write(f"\n# PUSH WARNING: advisory secret-scan hit ({labels})\n")
                for label, snippet in advisory_hits[:8]:
                    f.write(f"#   {label}: ...{snippet}...\n")
        except Exception:
            pass
    changed_files = _changed_files_against_base(wt, base_branch)
    hook_findings = _detect_secrets_hook_findings(wt, changed_files)
    if hook_findings:
        labels = sorted({
            finding.get("type")
            or finding.get("secret_type")
            or finding.get("filename")
            or "detect-secrets finding"
            for finding in hook_findings
        })
        reason = ", ".join(labels)
        try:
            with open(log_path, "a") as f:
                f.write(f"\n# PUSH WARNING: detect-secrets findings ({reason})\n")
                for finding in hook_findings[:8]:
                    rendered = json.dumps(finding, sort_keys=True)
                    f.write(f"#   {rendered[:300]}\n")
        except Exception:
            pass

    # Auto-commit anything the agent left uncommitted. The agent process is
    # expected to commit its own work, but we fall back to a single squash
    # commit so pattern-B still delivers a pushable branch.
    status = _git(wt, "status", "--porcelain").stdout
    if status.strip():
        add = _git_agent(wt, "add", "-A")
        if add.returncode != 0:
            return (False, f"git add failed: {add.stderr.strip()}", None, 0, None)
        msg = f"agent: {target.get('summary', target.get('task_id', 'auto-commit'))}\n\ntask_id: {target.get('task_id')}\n"
        commit = _git_agent(wt, "commit", "-m", msg)
        if commit.returncode != 0:
            return (False, f"git commit failed: {commit.stderr.strip()}", None, 0, None)

    # Any commits on branch ahead of base?
    count_proc = _git(wt, "rev-list", "--count", f"{base_branch}..HEAD")
    try:
        commit_count = int((count_proc.stdout or "0").strip() or "0")
    except ValueError:
        commit_count = 0
    if commit_count == 0:
        return (False, f"no commits ahead of {base_branch}", None, 0, None)

    sha = _git(wt, "rev-parse", "HEAD").stdout.strip() or None

    push = _git_agent(wt, "push", "-u", "origin", branch, timeout=120)
    if push.returncode != 0:
        return (False, f"git push failed: {push.stderr.strip()[:400]}", sha, commit_count, None)

    try:
        with open(log_path, "a") as f:
            f.write(f"\n# PUSHED {branch} @ {sha} ({commit_count} commit{'s' if commit_count != 1 else ''}) base={base_branch}\n")
            if push.stdout:
                f.write(push.stdout)
            if push.stderr:
                f.write(push.stderr)
    except Exception:
        pass

    return (True, "ok", sha, commit_count, o.now_iso())


def create_pr(target, project, branch, pr_body_path, log_path, base_branch="main"):
    """Open a GitHub PR for the pushed agent branch via `gh pr create`.

    Returns (ok, reason, pr_url, pr_number). Non-fatal on failure — the
    caller records pr_create_failure on the target and still marks it done
    because smoke + push both succeeded; a human can open the PR manually
    from the written pr-body.md.

    base_branch: the PR target branch. "main" for one-off tasks, or
    "feature/<id>" for slices of a feature. Human review happens at the
    feature->main PR; task->feature PRs are auto-merged by pr-sweep once
    smoke + reviewer + comments are green (phase 3).
    """
    if shutil.which("gh") is None:
        return (False, "gh CLI not installed", None, None)
    if not pr_body_path or not pathlib.Path(pr_body_path).exists():
        return (False, "pr-body missing", None, None)
    if branch == "main" or branch.endswith("/main"):
        return (False, "refuse to open PR against main branch", None, None)
    if branch == base_branch:
        return (False, f"head == base ({branch})", None, None)

    title = (target.get("summary") or target.get("task_id") or "agent change").splitlines()[0]
    if len(title) > 120:
        title = title[:117] + "..."

    cmd = [
        "gh", "pr", "create",
        "--head", branch,
        "--base", base_branch,
        "--title", title,
        "--body-file", str(pr_body_path),
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=project["path"],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return (False, "gh pr create timeout", None, None)

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()[:400]
        return (False, f"gh pr create failed: {stderr}", None, None)

    stdout = (proc.stdout or "").strip()
    pr_url = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("http"):
            pr_url = line
            break
    pr_number = None
    if pr_url:
        m = re.search(r"/pull/(\d+)", pr_url)
        if m:
            pr_number = int(m.group(1))

    try:
        with open(log_path, "a") as f:
            f.write(f"\n# PR OPENED: {pr_url or stdout}\n")
    except Exception:
        pass

    return (True, "ok", pr_url, pr_number)


def deploy_self_repair(target, project, worktree, log_path, *, restart_launch_agents=True):
    """Promote a validated self-repair branch into canonical main locally."""
    repo = pathlib.Path(project["path"])
    wt = pathlib.Path(worktree)
    if not repo.exists():
        return (False, "canonical repo missing", {})
    if not wt.exists():
        return (False, "self-repair worktree missing", {})

    branch = (_git(repo, "branch", "--show-current").stdout or "").strip()
    if branch != "main":
        return (False, f"canonical checkout not on main ({branch or '?'})", {})
    commits = [line.strip() for line in (_git(wt, "rev-list", "--reverse", "main..HEAD").stdout or "").splitlines() if line.strip()]
    if not commits:
        return (False, "no self-repair commits ahead of main", {})

    status = (_git(repo, "status", "--porcelain").stdout or "").strip()
    info = {
        "applied_commits": commits,
        "restart_launch_agents": bool(restart_launch_agents),
        "canonical_status_before_deploy": status,
    }
    with open(log_path, "a") as logf:
        logf.write(f"\n# self-repair deploy commits: {', '.join(commits)}\n")
        for sha in commits:
            proc = _git_agent(repo, "cherry-pick", sha, timeout=120)
            if proc.returncode != 0:
                logf.write(f"# cherry-pick failed for {sha}: {(proc.stderr or proc.stdout or '').strip()[:400]}\n")
                _git_agent(repo, "cherry-pick", "--abort", timeout=30)
                return (False, f"cherry-pick failed for {sha}", info)

        smoke_rel = (project.get("qa") or {}).get("smoke")
        smoke_abs = repo / smoke_rel if smoke_rel else None
        if not smoke_abs or not smoke_abs.exists():
            return (False, "canonical smoke script missing", info)

        logf.write(f"# self-repair canonical smoke: {smoke_abs}\n")
        try:
            smoke_proc = subprocess.run(
                ["bash", str(smoke_abs)],
                cwd=project["path"],
                env=os.environ.copy(),
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=DEFAULT_TIMEOUTS["qa"],
            )
        except subprocess.TimeoutExpired:
            return (False, "canonical smoke timeout", info)
        if smoke_proc.returncode != 0:
            return (False, f"canonical smoke failed: exit {smoke_proc.returncode}", info)

        head_sha = (_git(repo, "rev-parse", "HEAD").stdout or "").strip()
        info["deployed_sha"] = head_sha

        push = _git_agent(repo, "push", "origin", "main", timeout=120)
        if push.returncode != 0:
            logf.write(f"# self-repair push failed: {(push.stderr or push.stdout or '').strip()[:400]}\n")
            return (False, "push origin main failed", info)
        info["pushed_at"] = o.now_iso()

        deploy_check = subprocess.run(
            ["python3", "bin/orchestrator.py", "workflow-check"],
            cwd=project["path"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        logf.write("\n# self-repair post-deploy workflow-check\n")
        logf.write((deploy_check.stdout or "")[:4000])
        logf.write((deploy_check.stderr or "")[:2000])
        if deploy_check.returncode != 0:
            return (False, "post-deploy workflow-check failed", info)

        if restart_launch_agents:
            restarted, failed = o.restart_orchestrator_launch_agents(
                exclude_labels={"com.devmini.orchestrator.worker.qa"},
            )
            info["restarted_launch_agents"] = restarted
            info["restart_failures"] = failed
            logf.write(f"\n# self-repair restart: restarted={restarted} failed={failed}\n")
            if failed:
                return (False, "launch agent restart failed", info)

    return (True, "ok", info)


def _self_repair_deploy_mode(task, project_name):
    """Resolve self-repair deployment mode with canonical-main protection.

    >>> _self_repair_deploy_mode({"engine_args": {"self_repair": {"deploy_mode": "local-main"}}}, "devmini-orchestrator")
    'branch-pr'
    >>> _self_repair_deploy_mode({"engine_args": {"self_repair": {"deploy_mode": "local-main"}}}, "demo-project")
    'local-main'
    >>> _self_repair_deploy_mode({"engine_args": {"self_repair": {}}}, "devmini-orchestrator")
    'branch-pr'
    """
    repair = ((task.get("engine_args") or {}).get("self_repair") or {})
    mode = str(repair.get("deploy_mode") or "branch-pr").strip() or "branch-pr"
    if project_name == "devmini-orchestrator" and mode == "local-main":
        return "branch-pr"
    return mode


# --- memory context ---------------------------------------------------------

CONTEXT_RULE_FILES = ("AGENTS.md", "CLAUDE.md", "CODEX.md", "WARP.md")
ULL_PROJECTS = {"lvc-standard", "dag-framework", "trade-research-platform"}


def _context_sources():
    return o.load_context_sources()


def _engineering_skills_root():
    root = (_context_sources().get("engineering_skills_root") or "").strip()
    return pathlib.Path(root) if root else None


def _council_agent_dir():
    root = (_context_sources().get("council_root") or "").strip()
    if not root:
        return None
    return pathlib.Path(root) / "agents"


def _load_token_savior_memory():
    root = (_context_sources().get("token_savior_root") or "").strip()
    if not root:
        return None
    src = pathlib.Path(root) / "src"
    if not src.exists():
        return None
    src_str = str(src)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)
    try:
        from token_savior import memory_db  # type: ignore
    except Exception:
        return None
    return memory_db


def _read_text_if_exists(path, *, tail_lines=None, max_chars=None):
    path = pathlib.Path(path)
    if not path.exists():
        return ""
    try:
        body = path.read_text().strip()
    except OSError:
        return ""
    if tail_lines:
        lines = body.splitlines()
        body = "\n".join(lines[-tail_lines:])
    if max_chars and len(body) > max_chars:
        body = body[:max_chars] + "\n...[truncated]..."
    return body


def _trusted_external_skills_root():
    cfg = o.load_config()
    return pathlib.Path(o.skills_config(cfg=cfg)["trusted_root"])


def _trusted_external_skill_registry():
    return o.trusted_skill_registry(cfg=o.load_config())


def _file_list_has_prefix(files, prefixes):
    for rel in files:
        norm = rel.replace("\\", "/")
        if any(norm.startswith(prefix) for prefix in prefixes):
            return True
    return False


def _requested_external_skills(project_name, *, gate_name=None, changed_files_text=None):
    files = _changed_file_list(changed_files_text or "")
    requested = []
    if not files:
        if project_name in ULL_PROJECTS:
            requested.append("performance-profiler")
        if project_name in {"devmini-orchestrator", "trade-research-platform"}:
            requested.append("code-reviewer")
    if any(rel.endswith(".py") for rel in files):
        requested.append("code-reviewer")
    if any(rel.endswith(".java") for rel in files):
        requested.append("performance-profiler")
    if any(".env" in pathlib.Path(rel).name.lower() or rel.startswith("config/") for rel in files):
        requested.append("env-secrets-manager")
    if gate_name == "security-review-pass":
        requested.append("implementing-secret-scanning-with-gitleaks")
        if _file_list_has_prefix(files, ("bin/", "config/", ".codex/", ".claude/", "mcp/", "connectors/")):
            requested.append("detecting-ai-model-prompt-injection-attacks")
        if project_name == "trade-research-platform" and _file_list_has_prefix(files, ("ui/", "app/", "api/", "src/")):
            requested.append("testing-api-security-with-owasp-top-10")
    if gate_name == "supply-chain-audit-pass":
        requested.extend([
            "performing-sca-dependency-scanning-with-snyk",
            "analyzing-sbom-for-supply-chain-vulnerabilities",
        ])
    if project_name == "trade-research-platform" and _file_list_has_prefix(files, ("ui/", "app/", "api/", "src/")):
        requested.append("testing-api-security-with-owasp-top-10")
    deduped = []
    for name in requested:
        if name not in deduped:
            deduped.append(name)
    return deduped


def _external_skill_context(project_name, *, task_id=None, gate_name=None, changed_files_text=None):
    registry = _trusted_external_skill_registry()
    root = _trusted_external_skills_root()
    if not root.exists():
        return ""
    parts = []
    used = []
    for skill_name in _requested_external_skills(
        project_name,
        gate_name=gate_name,
        changed_files_text=changed_files_text,
    ):
        skill_dir = root / skill_name
        entry = registry.get(skill_name)
        if entry is None:
            if skill_dir.exists():
                o.append_event(
                    "skills",
                    "untrusted_skill_rejected",
                    task_id=task_id,
                    details={"skill": skill_name, "gate_name": gate_name, "project": project_name},
                )
            continue
        body = _read_text_if_exists(skill_dir / "SKILL.md", tail_lines=220, max_chars=7000)
        if body:
            parts.append(f"### trusted-skill/{skill_name}\n{body}")
            used.append(skill_name)
    if used and task_id:
        o.append_event(
            "skills",
            "skill_context_used",
            task_id=task_id,
            details={"project": project_name, "gate_name": gate_name, "skills": used},
        )
        for skill_name in used:
            o.append_metric(
                "skills.context_used",
                1,
                metric_type="counter",
                tags={"skill": skill_name, "project": project_name or "", "gate": gate_name or ""},
                source="worker",
            )
    return "\n\n".join(parts)


def _project_policy_context(project_name, project_path, *, task_id=None, gate_name=None, changed_files_text=None):
    project_root = pathlib.Path(project_path)
    parts = []
    for name in CONTEXT_RULE_FILES:
        body = _read_text_if_exists(project_root / name, tail_lines=220, max_chars=12000)
        if body:
            parts.append(f"### {name}\n{body}")

    skill_names = ["claude-code-cowork", "codex-delegation"]
    if project_name in ULL_PROJECTS:
        skill_names.extend(["ull-java-core", "ull-performance"])
    if project_name == "trade-research-platform":
        skill_names.append("ui-development-design")

    for skill_name in skill_names:
        skills_root = _engineering_skills_root()
        if skills_root is None:
            continue
        body = _read_text_if_exists(
            skills_root / skill_name / "SKILL.md",
            tail_lines=220,
            max_chars=8000,
        )
        if body:
            parts.append(f"### engineering-memory/{skill_name}\n{body}")
    external = _external_skill_context(
        project_name,
        task_id=task_id,
        gate_name=gate_name,
        changed_files_text=changed_files_text,
    )
    if external:
        parts.append(external)
    return "\n\n".join(parts)


def _format_token_savior_rows(rows, *, limit=5):
    out = []
    for row in (rows or [])[:limit]:
        title = row.get("title") or row.get("goal") or "(untitled)"
        rtype = row.get("type") or "memory"
        excerpt = row.get("excerpt") or row.get("conclusion") or row.get("excerpt_text") or ""
        excerpt = str(excerpt).strip().replace("\n", " ")
        if len(excerpt) > 220:
            excerpt = excerpt[:220] + "..."
        out.append(f"- [{rtype}] {title} — {excerpt}".rstrip(" — "))
    return "\n".join(out)


def _token_savior_context(project_name, project_path, *, role=None, query=None, task_id=None):
    memory_db = _load_token_savior_memory()
    if memory_db is None:
        return ""
    project_root = str(pathlib.Path(project_path).resolve())
    query = (query or "").strip()
    parts = []
    sections = []
    try:
        recent = memory_db.get_recent_index(project_root, limit=6)
        if recent:
            parts.append("### token-savior recent\n" + _format_token_savior_rows(recent, limit=6))
            sections.append("recent")
    except Exception:
        pass
    if query:
        try:
            found = memory_db.observation_search(project_root, query, limit=6)
            if found:
                parts.append("### token-savior search\n" + _format_token_savior_rows(found, limit=6))
                sections.append("search")
        except Exception:
            pass
        try:
            reasoning = memory_db.reasoning_search(project_root, query, limit=3)
            if reasoning:
                parts.append("### token-savior reasoning\n" + _format_token_savior_rows(reasoning, limit=3))
                sections.append("reasoning")
        except Exception:
            pass
        try:
            sessions = memory_db.session_summary_search(project_root, query, limit=3)
            if sessions:
                parts.append("### token-savior session summaries\n" + _format_token_savior_rows(sessions, limit=3))
                sections.append("sessions")
        except Exception:
            pass
    if parts and task_id:
        o.append_event(
            "skills",
            "token_savior_used",
            task_id=task_id,
            details={"project": project_name, "role": role, "query": query, "sections": sections},
        )
        o.append_metric(
            "token_savior.context_used",
            1,
            metric_type="counter",
            tags={"project": project_name or "", "role": role or "", "sections": ",".join(sections)},
            source="worker",
        )
    return "\n\n".join(parts)


def _memory_cfg():
    cfg = o.load_config()
    return (cfg.get("memory") or {}), cfg


def _memory_layer_query(query, *, gate_name=None):
    memory_cfg, _ = _memory_cfg()
    labels_cfg = (memory_cfg.get("label_primed_query") or {})
    if not labels_cfg.get("enabled", True):
        return (query or "").strip()
    labels = []
    review_gates = set(labels_cfg.get("review_gates") or [])
    if gate_name and gate_name in review_gates:
        labels.append(f"review_gate:{gate_name}")
    query = (query or "").strip()
    return " ".join(part for part in [*labels, query] if part).strip()


def _format_memory_rows(rows, *, include_content=False, char_limit=1800):
    out = []
    for row in rows:
        title = row.get("title") or row.get("section_key") or "untitled"
        tag_str = ",".join(row.get("tags") or [])
        prefix = f"- [{row.get('type')}] {title}"
        if tag_str:
            prefix += f" ({tag_str})"
        if include_content:
            content = (row.get("content") or "").strip()
            if len(content) > char_limit:
                content = content[:char_limit] + "..."
            out.append(f"{prefix}\n{content}".rstrip())
        else:
            excerpt = (row.get("excerpt") or "").strip()
            out.append(f"{prefix} — {excerpt}".rstrip(" — "))
    return "\n".join(out)


def _db_memory_context(project_name, *, query=None, gate_name=None):
    memory_cfg, cfg = _memory_cfg()
    if o.state_engine_config(cfg=cfg).get("mode") == "off":
        return ""
    try:
        engine = o.get_state_engine(cfg=cfg)
        engine.initialize()
        index_rows = engine.memory_index(
            project=project_name,
            limit=int(memory_cfg.get("index_limit") or 4),
        )
        search_rows = engine.memory_search(
            _memory_layer_query(query, gate_name=gate_name),
            project=project_name,
            limit=int(memory_cfg.get("search_limit") or 6),
        )
        selected_ids = []
        for row in search_rows or index_rows:
            obs_id = row.get("id")
            if obs_id not in selected_ids:
                selected_ids.append(obs_id)
            if len(selected_ids) >= int(memory_cfg.get("get_limit") or 3):
                break
        full_rows = []
        for obs_id in selected_ids:
            row = engine.memory_get(int(obs_id))
            if row:
                full_rows.append(row)
        parts = []
        if index_rows:
            parts.append("### memory_index\n" + _format_memory_rows(index_rows))
        if search_rows:
            parts.append("### memory_search\n" + _format_memory_rows(search_rows))
        if full_rows:
            parts.append(
                "### memory_get\n"
                + _format_memory_rows(
                    full_rows,
                    include_content=True,
                    char_limit=int(memory_cfg.get("get_char_limit") or 1800),
                )
            )
        return "\n\n".join(parts)
    except Exception as exc:
        return f"### memory_surface\n(memory observations unavailable: {exc})"


def _legacy_markdown_memory_context(project_path):
    memdir = pathlib.Path(project_path) / "repo-memory"
    parts = []
    for name in ("CURRENT_STATE.md", "RECENT_WORK.md", "DECISIONS.md", "FAILURES.md", "RESEARCH.md"):
        f = memdir / name
        if not f.exists():
            continue
        tail_lines = 20 if name == "RECENT_WORK.md" else None
        max_chars = 8000 if name == "CURRENT_STATE.md" else 6000
        body = _read_text_if_exists(f, tail_lines=tail_lines, max_chars=max_chars)
        if body:
            parts.append(f"### {name}\n{body}")
    return "\n\n".join(parts)


def read_memory_context(project_name, project_path, *, role=None, query=None, gate_name=None, task_id=None, changed_files_text=None):
    memory_ctx = _db_memory_context(project_name, query=query, gate_name=gate_name)
    parts = []
    if memory_ctx:
        parts.append(memory_ctx)
    elif o.state_engine_config(cfg=o.load_config()).get("mode") == "off":
        legacy = _legacy_markdown_memory_context(project_path)
        if legacy:
            parts.append(legacy)
    policy = _project_policy_context(
        project_name,
        project_path,
        task_id=task_id,
        gate_name=gate_name,
        changed_files_text=changed_files_text,
    )
    if policy:
        parts.append(f"### POLICY\n{policy}")
    token_ctx = _token_savior_context(project_name, project_path, role=role, query=query, task_id=task_id)
    if token_ctx:
        parts.append(token_ctx)
    return "\n\n".join(parts) if parts else "(no memory observations or policy context available)"


def recent_work_entries(text, limit=50):
    lines = text.splitlines()
    if not lines:
        return ""
    prefix = []
    idx = 0
    while idx < len(lines) and not lines[idx].startswith("## "):
        prefix.append(lines[idx])
        idx += 1
    entries = []
    current = []
    for line in lines[idx:]:
        if line.startswith("## ") and current:
            entries.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        entries.append("\n".join(current))
    return "\n".join(prefix + entries[:limit]).strip()


def markdown_sane(text):
    stripped = text.lstrip()
    return bool(stripped) and stripped.startswith("# ")


def extract_markdown_document(text):
    candidate = (text or "").strip()
    if not candidate:
        return ""
    fence = re.search(r"```(?:markdown|md)?\n(.*?)\n```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
    elif candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", candidate)
        candidate = re.sub(r"\n?```$", "", candidate).strip()
    if markdown_sane(candidate):
        return candidate
    header_idx = candidate.find("# ")
    if header_idx >= 0:
        tail = candidate[header_idx:].strip()
        if markdown_sane(tail):
            return tail
    return candidate


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
    if mode == "template-refine":
        return run_claude_template_refine(task, cfg, bt, timeout, log_path)
    if mode == "memory-synthesis":
        return run_claude_memory_synthesis(task, cfg, timeout, log_path)
    if task.get("role") == "reviewer":
        return run_claude_reviewer(task, cfg, timeout, log_path)
    return run_claude_planner(task, cfg, timeout, log_path)


def run_claude_memory_synthesis(task, cfg, timeout, log_path):
    task_id = task["task_id"]
    project_name = task.get("project")
    try:
        project = o.get_project(cfg, project_name)
    except KeyError:
        fail_task(task_id, "claimed", f"unknown project {project_name}",
                  blocker_code="runtime_unknown_project", summary="unknown project", retryable=False)
        return

    bt = task.get("braid_template") or "memory-synthesis"
    graph_body, graph_hash = o.braid_template_load(bt)
    if graph_body is None:
        if task.get("braid_generate_if_missing", True):
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
            def mut_missing(t):
                t["topology_error"] = "template_missing"
            o.move_task(task_id, "claimed", "blocked", reason="template missing", mutator=mut_missing)
            return
        fail_task(task_id, "claimed", "template missing, regen disabled",
                  blocker_code="template_missing", summary="template missing", retryable=False)
        return

    memdir = pathlib.Path(project["path"]) / "repo-memory"
    current_path = memdir / "CURRENT_STATE.md"
    recent_path = memdir / "RECENT_WORK.md"
    decisions_path = memdir / "DECISIONS.md"
    if not current_path.exists() or not recent_path.exists() or not decisions_path.exists():
        fail_task(task_id, "claimed", "repo-memory incomplete",
                  blocker_code="runtime_precondition_failed", summary="repo-memory incomplete", retryable=False)
        return

    current_text = current_path.read_text()
    recent_text = recent_path.read_text()
    decisions_text = decisions_path.read_text()
    recent_subset = recent_work_entries(recent_text, limit=50)

    system_prompt = (
        "You are the devmini historian for repo-memory synthesis.\n\n"
        "Use this BRAID graph as a hard workflow constraint:\n"
        f"{graph_body}\n\n"
        "Rewrite ONLY CURRENT_STATE.md. Do not modify RECENT_WORK.md. "
        "Do not invent new section headers unless an active subsystem was added. "
        "Keep the resulting diff under 200 lines and exclude secrets."
    )
    user_prompt = (
        f"PROJECT: {project['name']}\n\n"
        "Return ONLY the full replacement contents for CURRENT_STATE.md.\n\n"
        f"CURRENT_STATE.md:\n{current_text}\n\n"
        f"RECENT_WORK.md (latest entries):\n{recent_subset}\n\n"
        f"DECISIONS.md:\n{decisions_text}\n"
    )

    cmd = [
        "claude",
        "-p", user_prompt,
        "--dangerously-skip-permissions",
        "--system-prompt", system_prompt,
        "--output-format", "json",
        "--model", "sonnet",
        "--max-budget-usd", _claude_budget_flag("memory_synthesis", cfg=cfg, task=task),
        "--no-session-persistence",
    ]

    def mut_running(t):
        t["braid_template_path"] = f"braid/templates/{bt}.mmd"
        t["braid_template_hash"] = graph_hash
        t["started_at"] = o.now_iso()
        t["log_path"] = str(log_path)
    o.move_task(task_id, "claimed", "running", reason="claude memory synthesis", mutator=mut_running)

    with log_path.open("w") as logf:
        logf.write(f"# claude memory-synthesis task={task_id} project={project['name']}\n\n")
        try:
            proc = _run_bounded(
                cmd, stdout=subprocess.PIPE, stderr=logf, text=True, timeout=timeout,
                env=_claude_subprocess_env(),
                cwd="/tmp",
            )
        except subprocess.TimeoutExpired:
            fail_task(task_id, "running", f"claude timeout {timeout}s",
                      blocker_code="llm_timeout", summary="claude timeout", retryable=True)
            return
        logf.write(f"\n# exit: {proc.returncode}\n# stdout:\n{proc.stdout or ''}\n")
    _record_task_costs_from_text(task_id, "claude", "sonnet", proc.stdout or "")
    try:
        payload = json.loads(proc.stdout or "")
        raw_text = str(payload.get("result") or "")
    except json.JSONDecodeError:
        raw_text = proc.stdout or ""

    if proc.returncode != 0:
        _pause_claude_slot_if_needed(raw_text or proc.stdout or f"claude exit {proc.returncode}", task=task)
        fail_task(task_id, "running", f"claude exit {proc.returncode}",
                  blocker_code="llm_exit_error", summary="claude exit error", retryable=True)
        return

    candidate = extract_markdown_document(raw_text)
    if not markdown_sane(candidate):
        fail_task(task_id, "running", "candidate markdown invalid",
                  blocker_code="model_output_invalid", summary="candidate markdown invalid", retryable=False)
        return
    candidate_hits = scan_for_secrets(candidate)
    if candidate_hits:
        write_repo_memory_secret_alert(
            project["name"],
            task_id,
            "candidate contains secret-like content",
            candidate_hits,
            log_path=str(log_path),
        )
        fail_task(task_id, "running", "candidate contains secret-like content",
                  blocker_code="model_output_invalid", summary="candidate contains secret-like content", retryable=False)
        return

    diff_lines = list(
        difflib.unified_diff(
            current_text.splitlines(),
            candidate.splitlines(),
            fromfile="CURRENT_STATE.md",
            tofile="CURRENT_STATE.md",
            lineterm="",
        )
    )
    if len(diff_lines) >= 200:
        fail_task(task_id, "running", "delta exceeds 200 lines",
                  blocker_code="runtime_precondition_failed", summary="memory delta exceeds limit", retryable=False)
        return
    if recent_path.read_text() != recent_text:
        fail_task(task_id, "running", "RECENT_WORK changed during synthesis",
                  blocker_code="runtime_precondition_failed", summary="repo-memory changed during synthesis", retryable=True)
        return

    tmp = current_path.with_suffix(".md.tmp")
    tmp.write_text(candidate.rstrip() + "\n")
    os.rename(tmp, current_path)

    artifact_dir = o.STATE_ROOT / "artifacts" / task_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    diff_path = artifact_dir / "CURRENT_STATE.diff"
    diff_path.write_text("\n".join(diff_lines) + ("\n" if diff_lines else ""))

    def mut_done(t):
        t["artifacts"] = t.get("artifacts", []) + [str(diff_path.relative_to(o.STATE_ROOT))]
        t["finished_at"] = o.now_iso()
        t["log_path"] = str(log_path)
    o.move_task(task_id, "running", "done", reason="memory synthesis applied", mutator=mut_done)


def _git_diff_summary(worktree, base_ref, *, max_diff_chars=30000):
    changed_files = f"(no files changed vs {base_ref})"
    diff_text = f"(empty diff vs {base_ref})"
    try:
        changed_files = (
            subprocess.run(
                ["git", "-C", worktree, "diff", base_ref, "--name-only"],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip() or changed_files
        )
        stat = subprocess.run(
            ["git", "-C", worktree, "diff", base_ref, "--stat"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        body = subprocess.run(
            ["git", "-C", worktree, "diff", base_ref],
            capture_output=True, text=True, timeout=30,
        ).stdout
        if len(body) > max_diff_chars:
            body = body[:max_diff_chars] + "\n\n[...diff truncated...]\n"
        diff_text = (stat + "\n\n" + body).strip() or diff_text
    except subprocess.TimeoutExpired:
        diff_text = "(git diff timed out)"
    return changed_files, diff_text


def _changed_java_files(worktree, base_ref):
    try:
        proc = subprocess.run(
            ["git", "-C", worktree, "diff", base_ref, "--name-only", "--", "*.java"],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _policy_findings_for_diff(project_name, worktree, base_ref):
    findings = []
    if project_name not in ULL_PROJECTS:
        return findings
    for rel in _changed_java_files(worktree, base_ref):
        path = pathlib.Path(worktree) / rel
        try:
            body = path.read_text(errors="replace")
        except OSError:
            continue
        if re.search(r"\bsynchronized\b", body):
            findings.append(
                f"{rel}: contains `synchronized`; ULL/hot-path code must avoid monitor-based locking."
            )
    return findings


_HIGH_CONFIDENCE_SECRET_TYPES = {
    "Secret Keyword",
    "AWS Access Key",
    "AWS Secret Access Key",
    "Private Key",
    "Base64 High Entropy String",
}


def _security_gate_findings(worktree, base_ref):
    findings = []
    changed = _changed_files_against_base(pathlib.Path(worktree), base_ref)
    diff_text = _git(pathlib.Path(worktree), "diff", base_ref).stdout or ""
    for label, snippet in scan_for_secrets(diff_text):
        findings.append(f"high-confidence secret pattern in diff: {label} ({snippet[:80]})")
    hook_findings = _detect_secrets_hook_findings(pathlib.Path(worktree), changed)
    for finding in hook_findings or []:
        label = str(finding.get("type") or finding.get("secret_type") or "secret finding")
        if label in _HIGH_CONFIDENCE_SECRET_TYPES:
            findings.append(
                f"high-confidence secret finding: {label} in {finding.get('filename') or '(unknown file)'}"
            )
    return findings


_SUPPLY_CHAIN_VERSION_RULES = (
    (re.compile(r"\blog4j[-.]core\b.*\b2\.14\.1\b", re.I), "high-severity Java dependency: log4j-core 2.14.1"),
    (re.compile(r"\brequests\s*(?:==|>=?)\s*2\.19(?:\.1)?\b", re.I), "high-severity Python dependency: requests 2.19.x"),
    (re.compile(r"\burllib3\s*(?:==|>=?)\s*1\.25(?:\.\d+)?\b", re.I), "high-severity Python dependency: urllib3 1.25.x"),
)


def _supply_chain_findings(worktree, base_ref):
    changed = _changed_files_against_base(pathlib.Path(worktree), base_ref)
    if not any(
        rel.endswith((".java", ".py", "pom.xml", "build.gradle", "requirements.txt", "requirements-dev.txt", "pyproject.toml"))
        for rel in changed
    ):
        return []
    findings = []
    for rel in changed:
        path = pathlib.Path(worktree) / rel
        if not path.exists() or not path.is_file():
            continue
        try:
            body = path.read_text(errors="replace")
        except OSError:
            continue
        for pattern, reason in _SUPPLY_CHAIN_VERSION_RULES:
            if pattern.search(body):
                findings.append(f"{rel}: {reason}")
    return findings


def _restore_project_file_from_head(project_path, rel_path):
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_path), "checkout", "HEAD", "--", rel_path],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "git checkout timed out"
    detail = (proc.stderr or proc.stdout or "").strip()
    return proc.returncode == 0, detail or "ok"


def _changed_file_list(changed_files_text):
    return [line.strip() for line in (changed_files_text or "").splitlines() if line.strip() and not line.startswith("(")]


def _compile_gate_findings(project, worktree, changed_files_text):
    files = _changed_file_list(changed_files_text)
    findings = []
    py_files = [f for f in files if f.endswith(".py")]
    if py_files:
        cmd = ["python3", "-m", "py_compile", *py_files]
        try:
            proc = subprocess.run(
                cmd,
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            findings.append("compile gate timed out for Python changes")
        else:
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                findings.append(f"compile gate failed for Python changes: {(detail[0] if detail else 'py_compile failed')[:220]}")

    if any(f.endswith(".java") for f in files):
        gradlew = pathlib.Path(worktree) / "gradlew"
        if gradlew.exists():
            try:
                proc = subprocess.run(
                    [str(gradlew), "compileJava", "compileTestJava", "-q"],
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                findings.append("compile gate timed out for Java changes")
            else:
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                    findings.append(f"compile gate failed for Java changes: {(detail[0] if detail else 'gradle compile failed')[:220]}")
    return findings


def _test_ratio_policy(task=None, *, cfg=None):
    cfg = cfg or o.load_config()
    policy = dict((((cfg.get("review_policy") or {}).get("test_to_code_ratio")) or {}))
    override = {}
    template = (task or {}).get("braid_template")
    if template:
        override = dict((policy.get("template_overrides") or {}).get(template) or {})
    merged = {
        "min_ratio": float(policy.get("min_ratio", 0.5) or 0.0),
        "accept_doctest": bool(policy.get("accept_doctest", False)),
    }
    if override:
        if "min_ratio" in override:
            merged["min_ratio"] = float(override.get("min_ratio") or 0.0)
        if "accept_doctest" in override:
            merged["accept_doctest"] = bool(override.get("accept_doctest"))
    return merged


def _doctest_backed_files(worktree, files):
    out = []
    if not worktree:
        return out
    root = pathlib.Path(worktree)
    for rel in files:
        if not rel.endswith(".py"):
            continue
        path = root / rel
        if not path.exists():
            continue
        try:
            text = path.read_text()
        except Exception:
            continue
        if ">>>" in text:
            out.append(rel)
    return out


def _test_ratio_findings(changed_files_text, *, task=None, worktree=None, cfg=None):
    """Review gate for code/test delta balance, with template-level overrides.

    >>> _test_ratio_findings("bin/a.py\\nbin/b.py\\n")
    ['test-to-code delta ratio too low: code_files=2 test_files=0']
    >>> cfg = {"review_policy": {"test_to_code_ratio": {"min_ratio": 0.5, "accept_doctest": False, "template_overrides": {"orchestrator-self-repair": {"min_ratio": 0.0, "accept_doctest": True}}}}}
    >>> _test_ratio_findings("bin/a.py\\nbin/b.py\\n", task={"braid_template": "orchestrator-self-repair"}, cfg=cfg)
    []
    >>> import pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     (root / "bin").mkdir()
    ...     _ = (root / "bin" / "a.py").write_text('def demo():\\n    \"\"\"\\n    >>> demo()\\n    1\\n    \"\"\"\\n    return 1\\n')
    ...     cfg = {"review_policy": {"test_to_code_ratio": {"min_ratio": 0.5, "accept_doctest": True, "template_overrides": {}}}}
    ...     _test_ratio_findings("bin/a.py\\nbin/b.py\\n", worktree=root, cfg=cfg)
    []
    """
    files = _changed_file_list(changed_files_text)
    policy = _test_ratio_policy(task, cfg=cfg)
    code_files = [
        f for f in files
        if f.endswith((".py", ".java", ".ts", ".tsx", ".js", ".jsx"))
        and "/test" not in f.lower()
        and "/tests" not in f.lower()
        and not pathlib.Path(f).name.startswith("test_")
    ]
    test_files = [
        f for f in files
        if "/test" in f.lower()
        or "/tests" in f.lower()
        or pathlib.Path(f).name.startswith("test_")
        or f.endswith((".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx", ".test.ts", ".test.tsx", ".test.js", ".test.jsx"))
    ]
    if policy["accept_doctest"]:
        doctest_files = _doctest_backed_files(worktree, code_files)
        test_files = list(dict.fromkeys(test_files + doctest_files))
    if not code_files:
        return []
    if policy["min_ratio"] <= 0:
        return []
    if test_files:
        ratio = len(test_files) / max(len(code_files), 1)
        if ratio >= policy["min_ratio"]:
            return []
    return [f"test-to-code delta ratio too low: code_files={len(code_files)} test_files={len(test_files)}"]


def _specialized_review_prompt(
    *,
    gate_name,
    graph_body,
    target,
    target_wt,
    target_base,
    changed_files,
    diff_text,
    roadmap_ctx,
    memory_ctx,
    council_ctx,
    extra_ctx="",
):
    gate_brief = {
        "security-review-pass": (
            "You are the dedicated security review gate before autonomous push.\n"
            "- Hunt for auth/secret exposure, unsafe subprocess/file/network use, trust-boundary mistakes, missing validation, and review-bot bypasses.\n"
            "- Treat config drift, shell invocation, and credential handling as high-risk.\n"
            "- Request change if the diff weakens security posture or leaks local/operator information.\n"
        ),
        "supply-chain-audit-pass": (
            "You are the dedicated supply-chain audit gate before autonomous push.\n"
            "- Focus on dependency risk, manifest drift, vulnerable package versions, and supply-chain exposure.\n"
            "- Treat pinned high-severity vulnerable versions as blocking unless the diff explicitly remediates them.\n"
            "- Request change if dependency provenance or manifest safety is unclear.\n"
        ),
        "architectural-fit-review-pass": (
            "You are the dedicated architecture-fit review gate before autonomous push.\n"
            "- Check consistency with repo-memory decisions, workflow invariants, queue/state-machine semantics, and project-specific contracts.\n"
            "- Request change if the diff violates established architecture, broadens scope silently, or introduces policy drift.\n"
        ),
    }[gate_name]
    return (
        "[BRAID REVIEW GRAPH — traverse deterministically.]\n"
        f"{graph_body}\n"
        "[END GRAPH]\n\n"
        f"{gate_brief}\n"
        "- Run a compact internal council across the provided panel before deciding.\n"
        "- Preserve dissent in your reasoning body; do not flatten concerns away.\n"
        "- Do not modify files. Review only.\n\n"
        "[COUNCIL PERSONAS]\n"
        f"{council_ctx}\n"
        "[END COUNCIL PERSONAS]\n\n"
        f"REVIEW TARGET: {target.get('task_id')}\n"
        f"TARGET SUMMARY: {target.get('summary','')}\n"
        f"TARGET WORKTREE: {target_wt}\n"
        f"FILES CHANGED vs {target_base}:\n{changed_files}\n\n"
        f"[DIFF vs {target_base}]\n{diff_text}\n\n"
        f"{roadmap_ctx}"
        f"{extra_ctx}"
        f"[PROJECT CONTEXT]\n{memory_ctx}\n\n"
        "Emit EXACTLY one final line:\n"
        "  BRAID_OK: APPROVE — <one-line justification>\n"
        "  BRAID_OK: REQUEST_CHANGE — <one-line reason>\n"
        "  BRAID_TOPOLOGY_ERROR: <reason graph could not be traversed>\n"
    )


def _run_specialized_review_gate(
    gate_name,
    *,
    task_id,
    target,
    target_wt,
    target_base,
    changed_files,
    diff_text,
    roadmap_ctx,
    memory_ctx,
    panel,
    timeout,
    logf,
):
    graph_body, graph_hash = o.braid_template_load(gate_name)
    if graph_body is None:
        return {"error": f"{gate_name} template missing", "blocker_code": "runtime_precondition_failed"}
    prompt = _specialized_review_prompt(
        gate_name=gate_name,
        graph_body=graph_body,
        target=target,
        target_wt=target_wt,
        target_base=target_base,
        changed_files=changed_files,
        diff_text=diff_text,
        roadmap_ctx=roadmap_ctx,
        memory_ctx=memory_ctx,
        council_ctx="\n\n".join(_council_member_context(member) for member in panel),
    )
    last_msg_path = o.LOGS_DIR / f"{task_id}.{gate_name}.last.txt"
    logf.write(f"\n# {gate_name}\n")
    result = _run_review_agent(
        "claude",
        prompt=prompt,
        worktree=target_wt,
        timeout=timeout,
        logf=logf,
        last_msg_path=last_msg_path,
    )
    result["gate_name"] = gate_name
    result["graph_hash"] = graph_hash
    result["panel"] = list(panel)
    return result


def _review_gate_protocol_failure(task_id, target_id, gate_name, error, *, summary):
    _mark_review_target_for_retry(
        target_id,
        error,
        blocker_code="review_gate_protocol_error",
        metadata={"gate_name": gate_name},
    )
    o.append_event(
        "review-gate",
        "protocol_error",
        task_id=task_id,
        details={"target_task_id": target_id, "gate_name": gate_name, "error": str(error)[:240]},
    )
    fail_task(
        task_id,
        "running",
        str(error),
        blocker_code="review_gate_protocol_error",
        summary=summary,
        retryable=True,
    )


def _run_review_agent(kind, *, prompt, worktree, timeout, logf, last_msg_path):
    if kind == "codex":
        cmd = [
            "codex", "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C", str(worktree),
            "--ephemeral",
            "--json",
            "-o", str(last_msg_path),
            prompt,
        ]
        cwd = None
        stdout_target = subprocess.PIPE
        stderr_target = subprocess.PIPE
    elif kind == "claude":
        cmd = [
            "claude",
            "-p", prompt,
            "--dangerously-skip-permissions",
            "--output-format", "json",
            "--model", "sonnet",
            "--max-budget-usd", _claude_budget_flag("review", cfg=o.load_config()),
            "--no-session-persistence",
        ]
        cwd = str(worktree)
        stdout_target = subprocess.PIPE
        stderr_target = subprocess.STDOUT
    else:
        raise ValueError(f"unknown review agent {kind}")

    try:
        proc = _run_bounded(
            cmd,
            cwd=cwd,
            env=_claude_subprocess_env() if kind == "claude" else None,
            stdout=stdout_target,
            stderr=stderr_target,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"{kind} timeout {timeout}s"}
    if kind == "claude":
        structured = proc.stdout or ""
        logf.write(structured)
        _record_task_costs_from_text(last_msg_path.stem.split(".")[0], "claude", "sonnet", structured)
        try:
            payload = json.loads(structured)
            raw = str(payload.get("result") or "")
        except json.JSONDecodeError:
            raw = structured
        last_msg_path.write_text(raw)
    else:
        if proc.stderr:
            logf.write(proc.stderr)
        if proc.stdout:
            logf.write(proc.stdout)
        raw = last_msg_path.read_text(errors="replace") if last_msg_path.exists() else ""
        _record_task_costs_from_text(last_msg_path.stem.split(".")[0], "codex", "gpt-5.4", proc.stdout or "")
    if proc.returncode != 0:
        if kind == "claude":
            _pause_claude_slot_if_needed((proc.stderr or raw or f"claude exit {proc.returncode}"), task={"task_id": last_msg_path.stem.split(".")[0]})
        return {"error": f"{kind} exit {proc.returncode}", "raw": raw}

    verdict = None
    for line in reversed([l.strip() for l in raw.splitlines() if l.strip()][-20:]):
        if line.startswith("BRAID_OK: APPROVE") or line.startswith("BRAID_OK: QA_SUFFICIENT"):
            verdict = "approve"
            break
        if line.startswith("BRAID_OK: REQUEST_CHANGE") or line.startswith("BRAID_OK: QA_INSUFFICIENT"):
            verdict = "request_change"
            break
        if line.startswith("BRAID_TOPOLOGY_ERROR"):
            verdict = "topology_error"
            break
    if verdict is None:
        return {"error": f"{kind} verdict missing", "raw": raw}
    return {"verdict": verdict, "raw": raw}


def _comment_marker_still_present(worktree, comment):
    rel = comment.get("path")
    if not rel:
        return True
    path = pathlib.Path(worktree) / rel
    if not path.exists():
        return True
    try:
        body = path.read_text(errors="replace").lower()
    except OSError:
        return True
    comment_body = (comment.get("body") or "").lower()
    if "synchronized" in comment_body:
        return "synchronized" in body
    if "todo" in comment_body or "fixme" in comment_body or "xxx" in comment_body:
        return any(tok in body for tok in ("todo", "fixme", "xxx"))
    backticked = [m.group(1).strip().lower() for m in re.finditer(r"`([^`]{2,80})`", comment.get("body") or "")]
    supported = [tok for tok in backticked if re.fullmatch(r"[a-z0-9_./:-]+", tok)]
    if not supported:
        return True
    return any(tok in body for tok in supported)


def _comment_resolution_evidence(worktree, comment):
    rel = comment.get("path")
    if not rel:
        return None
    path = pathlib.Path(worktree) / rel
    if not path.exists():
        return None
    try:
        body = path.read_text(errors="replace")
    except OSError:
        return None
    body_lower = body.lower()
    comment_body = (comment.get("body") or "").lower()
    line_no = int(comment.get("line") or 0) if str(comment.get("line") or "").isdigit() else 0
    line_text = ""
    if line_no > 0:
        lines = body.splitlines()
        if line_no <= len(lines):
            line_text = lines[line_no - 1].lower()

    # Require that the current branch head actually touched the commented file.
    touched = False
    try:
        touched = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--name-only", "HEAD~1", "HEAD", "--", rel],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout.strip() != ""
    except subprocess.TimeoutExpired:
        touched = False
    if not touched:
        return None

    if "synchronized" in comment_body:
        if "synchronized" in body_lower or "synchronized" in line_text:
            return None
        return {"kind": "keyword_removed", "keyword": "synchronized", "path": rel, "line": line_no}

    backticked = [m.group(1).strip().lower() for m in re.finditer(r"`([^`]{2,120})`", comment.get("body") or "")]
    supported = [tok for tok in backticked if re.fullmatch(r"[a-z0-9_./:-]+", tok)]
    if supported:
        remaining = [tok for tok in supported if tok in body_lower]
        if remaining:
            return None
        return {"kind": "marker_removed", "markers": supported, "path": rel, "line": line_no}

    if any(tok in comment_body for tok in ("todo", "fixme", "xxx")):
        if any(tok in body_lower for tok in ("todo", "fixme", "xxx")):
            return None
        return {"kind": "todo_removed", "path": rel, "line": line_no}
    return None


def _eligible_thread_comments_for_auto_resolution(worktree, comments):
    eligible = []
    for comment in comments or []:
        thread_id = comment.get("thread_id")
        if not thread_id:
            continue
        evidence = _comment_resolution_evidence(worktree, comment)
        if evidence is None:
            continue
        eligible.append({**comment, "resolution_evidence": evidence})
    return eligible


def _qa_scope_expectations(project_name, changed_files, diff_text):
    expectations = []
    if isinstance(changed_files, str):
        files = [line.strip().lower() for line in changed_files.splitlines() if line.strip() and not line.startswith("(")]
    else:
        files = [str(path).lower() for path in (changed_files or [])]
    diff_lower = (diff_text or "").lower()

    if any(path.endswith(".java") for path in files):
        expectations.append("unit-or-gradle-tests")
    if project_name in ULL_PROJECTS and any(path.endswith(".java") for path in files):
        expectations.append("benchmark-or-jmh")
        expectations.append("concurrency-policy-review")
    if any(path.endswith((".ts", ".tsx", ".js", ".jsx")) for path in files):
        expectations.append("typecheck")
        expectations.append("lint")
    if any(path.endswith((".tsx", ".jsx")) or "/app/" in path or "/components/" in path for path in files):
        expectations.append("playwright-or-ui-smoke")
        expectations.append("a11y-or-ui-assertions")
    if "snapshot" in diff_lower or "contract" in diff_lower or any("snapshot" in path for path in files):
        expectations.append("api-contract")
    if any(path.endswith(".md") for path in files):
        expectations.append("docs-sync")
    return sorted(set(expectations))


def _qa_log_scope_signals(log_tail):
    text = (log_tail or "").lower()
    checks = {
        "benchmark-or-jmh": any(tok in text for tok in ("jmh", "benchmark")),
        "unit-or-gradle-tests": any(tok in text for tok in ("gradle test", "pytest", "unit", "tests passed", "> test", "mvn test")),
        "typecheck": any(tok in text for tok in ("typecheck", "tsc", "typescript")),
        "lint": "lint" in text,
        "playwright-or-ui-smoke": any(tok in text for tok in ("playwright", "ui smoke", "e2e", "browser")),
        "a11y-or-ui-assertions": any(tok in text for tok in ("a11y", "accessibility", "axe")),
        "api-contract": any(tok in text for tok in ("contract", "snapshot", "openapi")),
        "docs-sync": any(tok in text for tok in ("docs", "readme", "repo-memory")),
        "concurrency-policy-review": any(tok in text for tok in ("review", "qa gate", "semantic qa", "ull")),
    }
    return checks


def _run_semantic_qa_gate(project_name, project_path, worktree, base_ref, target, qa_log_path, timeout):
    changed_files, diff_text = _git_diff_summary(worktree, base_ref, max_diff_chars=20000)
    log_tail = _read_text_if_exists(qa_log_path, tail_lines=120, max_chars=12000)
    context = read_memory_context(project_name, project_path, role="qa", query=target.get("summary") or "")
    scope_expectations = _qa_scope_expectations(project_name, changed_files, diff_text)
    scope_signals = _qa_log_scope_signals(log_tail)
    panel = _config_council_panel(o.load_config(), "qa_panel", ("feynman", "kahneman", "ada"))
    council_ctx = "\n\n".join(_council_member_context(member) for member in panel)
    prompt = (
        "You are the final semantic QA gate before autonomous push.\n"
        "Decide whether the executed QA scope is sufficient for the changed code.\n"
        "Run a compact internal council across the provided QA panel before deciding.\n"
        "- Review the changed files, diff summary, repo policy, and the QA log tail.\n"
        "- Fail if QA missed an obvious required scope such as benchmarks, contract tests, replay checks, or UI smoke/a11y for touched areas.\n"
        "- Fail if the log shows only partial validation relative to the changed surfaces.\n"
        "- Approve only if the executed QA appears adequate for this slice.\n\n"
        "[QA COUNCIL PERSONAS]\n"
        f"{council_ctx}\n"
        "[END QA COUNCIL PERSONAS]\n\n"
        f"TARGET: {target.get('task_id')}\n"
        f"SUMMARY: {target.get('summary', '')}\n"
        f"WORKTREE: {worktree}\n"
        f"FILES CHANGED vs {base_ref}:\n{changed_files}\n\n"
        f"[DIFF]\n{diff_text}\n\n"
        f"[EXPECTED QA SCOPE]\n{scope_expectations or ['smoke']}\n\n"
        f"[OBSERVED QA SIGNALS]\n{json.dumps(scope_signals, sort_keys=True)}\n\n"
        f"[QA LOG TAIL]\n{log_tail or '(log unavailable)'}\n\n"
        f"[PROJECT CONTEXT]\n{context}\n\n"
        "Emit EXACTLY one final line:\n"
        "  BRAID_OK: QA_SUFFICIENT — <one-line reason>\n"
        "  BRAID_OK: QA_INSUFFICIENT — <one-line reason>\n"
        "  BRAID_TOPOLOGY_ERROR: <reason>\n"
    )
    last_msg_path = o.LOGS_DIR / f"{target.get('task_id')}.qa-gate.last.txt"
    qa_timeout = max(min(timeout, 3600), 1200)
    with open(qa_log_path, "a") as logf:
        logf.write("\n# semantic qa gate\n")
        result = _run_review_agent(
            "codex",
            prompt=prompt,
            worktree=worktree,
            timeout=qa_timeout,
            logf=logf,
            last_msg_path=last_msg_path,
        )
        if result.get("raw"):
            logf.write(f"\n# semantic qa verdict\n{result['raw']}\n")
    return result


def run_claude_template_gen(task, cfg, task_type, timeout, log_path):
    task_id = task["task_id"]
    gen_prompt_path = o.BRAID_GENERATORS / f"{task_type}.prompt.md"
    if not gen_prompt_path.exists():
        fail_task(task_id, "claimed", f"no generator prompt for {task_type}",
                  blocker_code="runtime_precondition_failed", summary="generator prompt missing", retryable=False)
        return

    project_name = task.get("project")
    memory_ctx = ""
    if project_name and project_name != "manual":
        try:
            project = o.get_project(cfg, project_name)
            memory_ctx = read_memory_context(project["name"], project["path"])
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
        "--output-format", "json",
        "--model", "opus",
        "--max-budget-usd", _claude_budget_flag("template_gen", cfg=cfg, task=task, mode=planner_mode),
        "--no-session-persistence",
    ]

    def mut_running(t):
        t["started_at"] = o.now_iso()
        t["log_path"] = str(log_path)
    o.move_task(task_id, "claimed", "running", reason="claude template-gen", mutator=mut_running)

    with log_path.open("w") as logf:
        logf.write(f"# claude template-gen task={task_id} task_type={task_type}\n")
        logf.write(f"# model: opus\n# max_budget_usd: {_claude_budget_flag('template_gen', cfg=cfg, task=task, mode=planner_mode)}\n\n")
        logf.flush()
        try:
            proc = _run_bounded(
                cmd, stdout=subprocess.PIPE, stderr=logf, text=True, timeout=timeout,
                env=_claude_subprocess_env(),
                cwd="/tmp",  # avoid CLAUDE.md auto-discovery at worker invocation
            )
        except subprocess.TimeoutExpired:
            fail_task(task_id, "running", f"claude timeout {timeout}s",
                      blocker_code="llm_timeout", summary="claude timeout", retryable=True)
            return
        logf.write(f"\n# exit: {proc.returncode}\n")
        logf.write("# stdout:\n")
        logf.write(proc.stdout or "")
    _record_task_costs_from_text(task_id, "claude", "opus", proc.stdout or "")
    try:
        payload = json.loads(proc.stdout or "")
        raw_text = str(payload.get("result") or "")
    except json.JSONDecodeError:
        raw_text = proc.stdout or ""

    if proc.returncode != 0:
        _pause_claude_slot_if_needed(raw_text or proc.stdout or f"claude exit {proc.returncode}", task=task)
        fail_task(task_id, "running", f"claude exit {proc.returncode}",
                  blocker_code="llm_exit_error", summary="claude exit error", retryable=True)
        return

    graph = extract_mermaid(raw_text)
    if not graph:
        fail_task(task_id, "running", "no mermaid block in output",
                  blocker_code="model_output_invalid", summary="template-gen output invalid", retryable=False)
        return

    o.braid_template_write(task_type, graph, generator_model="claude-opus")
    def mut(t):
        t["artifacts"] = t.get("artifacts", []) + [f"braid/templates/{task_type}.mmd"]
        t["finished_at"] = o.now_iso()
    o.move_task(task_id, "running", "done", reason="template written", mutator=mut)


def run_claude_template_refine(task, cfg, task_type, timeout, log_path):
    task_id = task["task_id"]
    if not task_type:
        fail_task(task_id, "claimed", "template-refine missing braid_template",
                  blocker_code="runtime_precondition_failed", summary="template-refine missing braid_template", retryable=False)
        return

    refine = (task.get("engine_args") or {}).get("refine_request") or {}
    node_id = (refine.get("node_id") or "").strip()
    condition = (refine.get("condition") or "").strip()
    base_hash = (refine.get("template_hash") or "").strip()
    if not node_id or not condition or not base_hash:
        fail_task(task_id, "claimed", "template-refine missing refine_request",
                  blocker_code="runtime_precondition_failed", summary="template-refine missing refine_request", retryable=False)
        return

    graph_body, graph_hash = o.braid_template_load(task_type)
    if graph_body is None:
        fail_task(task_id, "claimed", f"template-refine missing {task_type}",
                  blocker_code="runtime_precondition_failed", summary="template-refine target missing", retryable=False)
        return
    if graph_hash != base_hash:
        def mut_stale(t):
            t["finished_at"] = o.now_iso()
            t["artifacts"] = t.get("artifacts", []) + [f"braid/templates/{task_type}.mmd"]
        o.move_task(task_id, "claimed", "done", reason="template already changed", mutator=mut_stale)
        return

    gen_prompt_path = o.BRAID_GENERATORS / f"{task_type}.prompt.md"
    if not gen_prompt_path.exists():
        fail_task(task_id, "claimed", f"no generator prompt for {task_type}",
                  blocker_code="runtime_precondition_failed", summary="generator prompt missing", retryable=False)
        return

    project_name = task.get("project")
    memory_ctx = ""
    if project_name and project_name != "manual":
        try:
            project = o.get_project(cfg, project_name)
            memory_ctx = read_memory_context(project["name"], project["path"])
        except KeyError:
            memory_ctx = "(unknown project)"

    user_prompt = (
        f"TASK TYPE: {task_type}\n\n"
        f"PROJECT MEMORY:\n{memory_ctx}\n\n"
        "Current Mermaid template:\n"
        f"```mermaid\n{graph_body}\n```\n\n"
        "Refinement request:\n"
        f"- node_id: {node_id}\n"
        f"- missing_edge_condition: {condition}\n\n"
        "Return ONLY the full replacement Mermaid flowchart. "
        "Make the smallest valid change that adds the missing traversal or gate around the named node. "
        "Preserve unrelated nodes, edges, and names unless the template is invalid without a small rename."
    )

    gen_system = gen_prompt_path.read_text()
    cmd = [
        "claude",
        "-p", user_prompt,
        "--dangerously-skip-permissions",
        "--system-prompt", gen_system,
        "--output-format", "json",
        "--model", "opus",
        "--max-budget-usd", _claude_budget_flag("template_refine", cfg=cfg, task=task),
        "--no-session-persistence",
    ]

    def mut_running(t):
        t["started_at"] = o.now_iso()
        t["log_path"] = str(log_path)
        t["braid_template_hash"] = graph_hash
    o.move_task(task_id, "claimed", "running", reason="claude template-refine", mutator=mut_running)

    with log_path.open("w") as logf:
        logf.write(f"# claude template-refine task={task_id} task_type={task_type}\n")
        logf.write(f"# base_hash: {graph_hash}\n")
        logf.write(f"# refine node: {node_id}\n")
        logf.write(f"# refine condition: {condition}\n\n")
        try:
            proc = _run_bounded(
                cmd, stdout=subprocess.PIPE, stderr=logf, text=True, timeout=timeout,
                env=_claude_subprocess_env(),
                cwd="/tmp",
            )
        except subprocess.TimeoutExpired:
            fail_task(task_id, "running", f"claude timeout {timeout}s",
                      blocker_code="llm_timeout", summary="claude timeout", retryable=True)
            return
        logf.write(f"\n# exit: {proc.returncode}\n# stdout:\n{proc.stdout or ''}\n")
    _record_task_costs_from_text(task_id, "claude", "opus", proc.stdout or "")
    try:
        payload = json.loads(proc.stdout or "")
        raw_text = str(payload.get("result") or "")
    except json.JSONDecodeError:
        raw_text = proc.stdout or ""

    if proc.returncode != 0:
        _pause_claude_slot_if_needed(raw_text or proc.stdout or f"claude exit {proc.returncode}", task=task)
        fail_task(task_id, "running", f"claude exit {proc.returncode}",
                  blocker_code="llm_exit_error", summary="claude exit error", retryable=True)
        return

    candidate = extract_mermaid(raw_text)
    if not candidate:
        fail_task(task_id, "running", "no mermaid block in refine output",
                  blocker_code="model_output_invalid", summary="template-refine output invalid", retryable=False)
        return

    _, latest_hash = o.braid_template_load(task_type)
    if latest_hash != base_hash:
        def mut_stale(t):
            t["finished_at"] = o.now_iso()
            t["artifacts"] = t.get("artifacts", []) + [f"braid/templates/{task_type}.mmd"]
        o.move_task(task_id, "running", "done", reason="template already changed", mutator=mut_stale)
        return

    try:
        o.braid_template_write(task_type, candidate, generator_model="claude-opus-refine")
    except ValueError as exc:
        fail_task(task_id, "running", str(exc)[:200],
                  blocker_code="model_output_invalid", summary="template refinement lint failed", retryable=False)
        return

    def mut_done(t):
        t["artifacts"] = t.get("artifacts", []) + [f"braid/templates/{task_type}.mmd"]
        t["finished_at"] = o.now_iso()
    o.move_task(task_id, "running", "done", reason="template refined", mutator=mut_done)


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

DAG_IMPLEMENT_NODE_POSITIVE_PATTERNS = (
    "node", "operator", "processing-element", "processing element", "dag", "pipeline-node",
)
DAG_HISTORIAN_POSITIVE_PATTERNS = (
    "history", "memory", "recent_work", "recent work", "historian", "current_state", "repo memory",
)
TRP_PIPELINE_STAGE_POSITIVE_PATTERNS = (
    "stage", "pipeline", "platform-runtime", "platform runtime", "ingest", "jmh",
    "controller", "publish", "projection", "payload", "explainability", "signal",
)
TRP_PIPELINE_CONFLICT_PATTERNS = (
    "stage", "pipeline", "platform-runtime", "platform runtime", "ingest", "jmh",
)
TRP_UI_COMPONENT_POSITIVE_PATTERNS = (
    "component", "page", ".tsx", "playwright", "a11y", "apps/",
    "ui", "types", "type update", "contract snapshot", "provenance matrix",
)

PROJECT_CROSS_MARKERS = {
    "lvc": ("dag-framework", "trade-research-platform", "apps/", ".tsx", "playwright", "a11y"),
    "dag": ("lvc-standard", "trade-research-platform", "apps/", ".tsx", "playwright", "a11y"),
    "trp": ("lvc-standard", "dag-framework", "src/main/java", "operator"),
}


def planner_historian_template(project_name):
    return o.project_historian_template(project_name)


def planner_implementer_templates(project_name):
    if project_name == "lvc-standard":
        return ("lvc-implement-operator",)
    if project_name == "dag-framework":
        return ("dag-implement-node",)
    if project_name == "trade-research-platform":
        return ("trp-implement-pipeline-stage", "trp-ui-component")
    return ("lvc-implement-operator",)


def planner_template_roles(project_name):
    roles = {
        planner_historian_template(project_name): "historian",
    }
    for template in planner_implementer_templates(project_name):
        roles[template] = "implementer"
    return roles


def _council_member_context(member):
    agent_dir = _council_agent_dir()
    if agent_dir is None:
        return f"## {member}\nPersona root unavailable.\n"
    path = agent_dir / f"council-{member}.md"
    if not path.exists():
        return f"## {member}\nPersona file missing at {path}\n"
    lines = path.read_text().splitlines()
    kept = []
    for line in lines[:120]:
        kept.append(line)
        if line.strip() == "## Output Format (Council Round 2)":
            break
    return f"## {member}\n" + "\n".join(kept).strip() + "\n"


def _config_council_panel(cfg, key, fallback):
    council_cfg = (cfg.get("council") or {}) if cfg else {}
    panel = tuple(council_cfg.get(key) or ())
    return panel or tuple(fallback)


def planner_council_prompt(task, project, memory_ctx, cfg):
    roadmap_entry = (task.get("engine_args") or {}).get("roadmap_entry") or {}
    panel = _config_council_panel(cfg, "planner_panel", ("aristotle", "socrates", "meadows"))
    council_ctx = "\n\n".join(_council_member_context(member) for member in panel)
    allowed_templates = ", ".join(f'"{name}"' for name in planner_template_roles(project["name"]).keys())
    system_prompt = (
        "You are the planner council for the devmini orchestrator.\n"
        "Run a compact internal council before emitting implementation slices.\n"
        "Output ONLY one JSON object with keys:\n"
        "  panel: list[str]\n"
        "  key_agreements: list[str]\n"
        "  dissent: list[str]\n"
        "  execution_path: string\n"
        "  slices: list[object]\n"
        "Each slice object must include summary and braid_template, and may include depends_on.\n"
        "Use only valid braid_template values for this project.\n\n"
        "[COUNCIL PERSONAS]\n"
        f"{council_ctx}\n"
        "[END COUNCIL PERSONAS]"
    )
    user_prompt = (
        f"PROJECT: {project['name']}\n\n"
        f"ROADMAP ENTRY ID: {roadmap_entry.get('id') or '-'}\n"
        f"ROADMAP TITLE: {roadmap_entry.get('title') or '-'}\n\n"
        "[ROADMAP BODY]\n"
        f"{roadmap_entry.get('body') or '(none)'}\n\n"
        "[PROJECT MEMORY]\n"
        f"{memory_ctx}\n\n"
        "Decide the smallest safe next execution plan.\n"
        "Keep at most 3 slices.\n"
        f"Allowed braid_template values: {allowed_templates}\n"
        "Preserve dissent when a risky slice is rejected or narrowed.\n"
    )
    return system_prompt, user_prompt, panel


def self_repair_council_prompt(task, project, memory_ctx):
    engine_args = task.get("engine_args") or {}
    repair = engine_args.get("self_repair") or {}
    evidence = (engine_args.get("evidence") or "").strip()
    issue_key = engine_args.get("issue_key") or "-"
    feature = o.read_feature(task.get("feature_id")) if task.get("feature_id") else None
    attached_issues = []
    if feature:
        for issue in ((feature.get("self_repair") or {}).get("issues") or []):
            status = issue.get("status") or "pending"
            attached_issues.append(
                f"- [{status}] {issue.get('issue_key')}: {issue.get('summary') or '-'}"
            )
    members = tuple(repair.get("council_members") or SELF_REPAIR_COUNCIL_DEFAULT)
    council_ctx = "\n\n".join(_council_member_context(member) for member in members)
    system_prompt = (
        "You are the self-repair council planner for the devmini orchestrator.\n"
        "Use the provided council member personas to deliberate before deciding the repair path.\n"
        "The task is not to write code. The task is to choose the narrowest repair plan that restores autonomous runtime health.\n"
        "Prefer one bounded implementer slice. Add a second slice only if the first cannot safely land without it.\n"
        "Assume execution and deployment will be handled autonomously after QA on a clean working tree.\n"
        "Output ONLY one JSON object with keys:\n"
        "  panel: list[str]\n"
        "  key_agreements: list[str]\n"
        "  dissent: list[str]\n"
        "  execution_path: string\n"
        "  chosen_strategy: string\n"
        "  rejected_strategies: list[str]\n"
        "  dissent_reasons: list[str]\n"
        "  confidence: number\n"
        "  retry_conditions: list[str]\n"
        "  escalation_threshold: string\n"
        "  slices: list[object]\n"
        "Each slice object must include summary and braid_template.\n"
        "Valid braid_template values here: only \"orchestrator-self-repair\".\n"
        "Do not emit prose outside the JSON object.\n\n"
        "[COUNCIL PERSONAS]\n"
        f"{council_ctx}\n"
        "[END COUNCIL PERSONAS]"
    )
    user_prompt = (
        f"PROJECT: {project['name']}\n"
        f"FEATURE: {task.get('feature_id') or '-'}\n"
        f"TASK: {task.get('summary') or '-'}\n\n"
        f"CURRENT ISSUE KEY: {issue_key}\n\n"
        "[ISSUE EVIDENCE]\n"
        f"{evidence or '(none)'}\n\n"
        "[ATTACHED SELF-REPAIR ISSUES]\n"
        f"{chr(10).join(attached_issues) if attached_issues else '(none)'}\n\n"
        "[PROJECT MEMORY]\n"
        f"{memory_ctx}\n\n"
        "Run a compact internal council across the listed members.\n"
        "Focus on:\n"
        "1. observable failure facts\n"
        "2. the cleanest repair path\n"
        "3. whether local deployment/restart is required\n"
        "4. the single best Codex execution slice to fix the runtime\n"
    )
    return system_prompt, user_prompt, members


SELF_REPAIR_REASONING_BLOCKERS = {
    "false_blocker_claim",
    "validator_malfunction",
    "template_graph_error",
    "qa_target_missing",
    "qa_smoke_failed",
}


def _self_repair_enabled(task):
    return bool(((task.get("engine_args") or {}).get("self_repair") or {}).get("enabled"))


def _self_repair_issue_ref(task):
    eargs = task.get("engine_args") or {}
    return {
        "feature_id": task.get("feature_id"),
        "issue_key": eargs.get("issue_key"),
        "issue_summary": eargs.get("issue_summary") or task.get("summary") or "",
        "issue_evidence": eargs.get("evidence") or "",
    }


def _is_validator_malfunction(detail):
    lower = (detail or "").lower()
    if "orchestrator.json" not in lower:
        return False
    return any(token in lower for token in ("validatesmoke", "validatecanary", "smoke", "canary", "lint-templates"))


def _parse_council_json(raw):
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        raise ValueError("no json object in council output")
    parsed = json.loads(m.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("council output must decode to a JSON object")
    return parsed


def _run_self_repair_council(
    *,
    task,
    stage,
    panel,
    prompt_body,
    worktree,
    timeout,
    last_msg_path,
    model="sonnet",
):
    claude_bin = next((p for p in o.CLAUDE_CANDIDATE_PATHS if p and pathlib.Path(p).exists()), None)
    if not claude_bin:
        return {"error": "claude binary unavailable for self-repair council"}
    system_prompt = (
        "You are a council checkpoint inside the orchestrator self-repair runtime.\n"
        "Deliberate across the supplied panel before deciding.\n"
        "Output ONLY one JSON object with keys:\n"
        "  panel: list[str]\n"
        "  stage: string\n"
        "  verdict: string\n"
        "  chosen_strategy: string\n"
        "  rejected_strategies: list[str]\n"
        "  dissent_reasons: list[str]\n"
        "  confidence: number\n"
        "  retry_conditions: list[str]\n"
        "  escalation_threshold: string\n"
        "  reason: string\n"
        "Valid verdicts depend on the stage-specific instructions.\n"
    )
    prompt = (
        f"STAGE: {stage}\n\n"
        "[COUNCIL PERSONAS]\n"
        + "\n\n".join(_council_member_context(member) for member in panel)
        + "\n[END COUNCIL PERSONAS]\n\n"
        + prompt_body
    )
    cmd = [
        claude_bin,
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--system-prompt", system_prompt,
        "--output-format", "json",
        "--model", model,
        "--max-budget-usd", _claude_budget_flag("review", cfg=o.load_config(), task=task),
        "--no-session-persistence",
    ]
    try:
        proc = _run_bounded(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=_claude_subprocess_env(),
            cwd=str(worktree),
        )
    except subprocess.TimeoutExpired:
        return {"error": f"council timeout {timeout}s", "blocker_code": "council_timeout"}
    _record_task_costs_from_text(task.get("task_id"), "claude", model, proc.stdout or "")
    try:
        payload = json.loads(proc.stdout or "")
        raw = str(payload.get("result") or "")
    except json.JSONDecodeError:
        raw = proc.stdout or ""
    if last_msg_path:
        last_msg_path.write_text(raw)
    if proc.returncode != 0:
        if _claude_budget_exhausted(proc.stderr or raw or ""):
            _pause_claude_slot_if_needed((proc.stderr or raw or f"claude exit {proc.returncode}"))
            return {"error": "claude budget exhausted", "blocker_code": "claude_budget_exhausted"}
        return {"error": (proc.stderr or raw or f"claude exit {proc.returncode}").strip()[:400]}
    try:
        data = _parse_council_json(raw)
    except Exception as exc:
        return {"error": f"invalid council output: {exc}", "blocker_code": "review_gate_protocol_error"}
    referenced_task_id = (
        data.get("referenced_task_id")
        or data.get("target_task_id")
        or (data.get("task_ref") if isinstance(data.get("task_ref"), str) else None)
    )
    if referenced_task_id and o.find_task(str(referenced_task_id)) is None:
        return {
            "error": f"council referenced missing task: {referenced_task_id}",
            "blocker_code": "review_gate_protocol_error",
        }
    data.setdefault("panel", list(panel))
    data.setdefault("stage", stage)
    data["raw"] = raw
    return data


def _self_repair_record_council(task, stage, result):
    if not _self_repair_enabled(task):
        return
    ref = _self_repair_issue_ref(task)
    if not ref["feature_id"] or not ref["issue_key"]:
        return
    o.self_repair_record_deliberation(
        ref["feature_id"],
        ref["issue_key"],
        stage=stage,
        verdict=result.get("verdict") or "unknown",
        panel=result.get("panel") or (),
        chosen_strategy=result.get("chosen_strategy") or "",
        rejected_strategies=result.get("rejected_strategies") or (),
        dissent_reasons=result.get("dissent_reasons") or (),
        confidence=result.get("confidence"),
        retry_conditions=result.get("retry_conditions") or (),
        escalation_threshold=result.get("escalation_threshold") or "",
        reason=result.get("reason") or "",
        task_id=task.get("task_id"),
    )


def _self_repair_reopen_current_issue(task, *, blocker_code, reason, deliberation=None, summary_suffix=""):
    ref = _self_repair_issue_ref(task)
    if not ref["feature_id"] or not ref["issue_key"]:
        return None
    summary = ref["issue_summary"]
    if summary_suffix:
        summary = f"{summary} [{summary_suffix}]"
    evidence = ref["issue_evidence"]
    if reason:
        evidence = (evidence + "\n\n[REOPEN REASON]\n" + reason).strip()
    if deliberation and task.get("task_id"):
        deliberation = dict(deliberation)
        prior = list(deliberation.get("superseded_task_ids") or [])
        if task["task_id"] not in prior:
            prior.append(task["task_id"])
        deliberation["superseded_task_ids"] = prior
    return o.self_repair_reopen_issue(
        ref["feature_id"],
        ref["issue_key"],
        summary=summary,
        evidence=evidence,
        blocker_code=blocker_code,
        reason=reason,
        deliberation=deliberation or {},
    )


def _run_self_repair_pre_execute_council(task, project, worktree, timeout, memory_ctx, graph_body):
    repair = (task.get("engine_args") or {}).get("self_repair") or {}
    panel = tuple(repair.get("council_members") or SELF_REPAIR_COUNCIL_DEFAULT)
    council = (task.get("engine_args") or {}).get("council") or {}
    prompt_body = (
        "Decide whether execution should proceed on the current self-repair coding slice.\n"
        "Valid verdicts: approve, replan.\n\n"
        f"TASK SUMMARY: {task.get('summary') or '-'}\n"
        f"ISSUE KEY: {(task.get('engine_args') or {}).get('issue_key') or '-'}\n"
        f"COUNCIL CHOSEN PATH: {council.get('chosen_strategy') or council.get('execution_path') or '-'}\n"
        f"KEY AGREEMENTS: {council.get('key_agreements') or []}\n"
        f"DISSENT: {council.get('dissent') or []}\n\n"
        f"[TASK GRAPH]\n{graph_body}\n\n"
        f"[PROJECT MEMORY]\n{memory_ctx}\n"
    )
    result = _run_self_repair_council(
        task=task,
        stage="pre_execute",
        panel=panel,
        prompt_body=prompt_body,
        worktree=worktree,
        timeout=max(600, min(timeout, 1800)),
        last_msg_path=o.LOGS_DIR / f"{task['task_id']}.self-repair-pre-execute.last.txt",
    )
    if not result.get("error"):
        _self_repair_record_council(task, "pre_execute", result)
    return result


def _qa_contract_preflight(project, contract_kind, script_rel):
    """Validate QA contract configuration before invoking any script.

    >>> import json, pathlib, tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     (root / "qa").mkdir()
    ...     script = root / "qa" / "smoke.sh"
    ...     _ = script.write_text("#!/usr/bin/env bash\\nexit 0\\n")
    ...     _ = script.chmod(0o644)
    ...     _qa_contract_preflight({"name": "devmini-orchestrator", "path": str(root)}, "smoke", "qa/smoke.sh")["summary"]
    'QA script not executable'
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     root = pathlib.Path(tmp)
    ...     (root / "qa").mkdir()
    ...     (root / "config").mkdir()
    ...     script = root / "qa" / "smoke.sh"
    ...     _ = script.write_text("#!/usr/bin/env bash\\nexit 0\\n")
    ...     _ = script.chmod(0o755)
    ...     _ = (root / "config" / "orchestrator.local.json").write_text("{bad json")
    ...     _qa_contract_preflight({"name": "devmini-orchestrator", "path": str(root)}, "smoke", "qa/smoke.sh")["summary"]
    'QA config invalid'
    """
    if not script_rel:
        return {"ok": False, "error": f"no qa.{contract_kind} in config", "summary": "QA contract missing"}
    if project.get("name") == "devmini-orchestrator":
        for cfg_rel in ("config/orchestrator.local.json", "config/orchestrator.example.json"):
            cfg_abs = pathlib.Path(project["path"]) / cfg_rel
            if not cfg_abs.exists():
                continue
            try:
                json.loads(cfg_abs.read_text())
            except Exception as exc:
                return {
                    "ok": False,
                    "error": f"invalid orchestrator config: {cfg_abs}",
                    "summary": "QA config invalid",
                    "detail": f"{cfg_abs}: {exc}",
                }
    script_abs = pathlib.Path(project["path"]) / script_rel
    if script_abs.exists() and script_abs.is_file():
        if not os.access(script_abs, os.X_OK):
            return {
                "ok": False,
                "error": f"script not executable: {script_abs}",
                "summary": "QA script not executable",
                "detail": str(script_abs),
            }
        return {"ok": True, "script_abs": script_abs}
    restored, detail = _restore_project_file_from_head(project["path"], script_rel)
    if script_abs.exists() and script_abs.is_file():
        if not os.access(script_abs, os.X_OK):
            return {
                "ok": False,
                "error": f"script not executable: {script_abs}",
                "summary": "QA script not executable",
                "detail": str(script_abs),
                "restored": restored,
            }
        return {"ok": True, "script_abs": script_abs, "restored": restored, "detail": detail}
    return {
        "ok": False,
        "error": f"script missing: {script_abs}",
        "summary": "QA script missing",
        "detail": detail or str(script_abs),
    }


def _run_self_repair_blocker_verifier(task, project, worktree, timeout, blocker_code, detail, memory_ctx):
    repair_cfg = o.load_config().get("self_repair") or {}
    panel = tuple(repair_cfg.get("verifier_panel") or repair_cfg.get("council_members") or SELF_REPAIR_COUNCIL_DEFAULT)
    prompt_body = (
        "Assess whether this self-repair blocker should be accepted as-is.\n"
        "Valid verdicts: accept_blocker, replan, retry_same_path, change_strategy.\n"
        "Prefer automation over human escalation. Only choose accept_blocker if no safer autonomous next step exists.\n\n"
        f"TASK SUMMARY: {task.get('summary') or '-'}\n"
        f"BLOCKER CODE: {blocker_code}\n"
        f"BLOCKER DETAIL: {detail or '-'}\n\n"
        f"[PROJECT MEMORY]\n{memory_ctx}\n"
    )
    result = _run_self_repair_council(
        task=task,
        stage="blocker_verifier",
        panel=panel,
        prompt_body=prompt_body,
        worktree=worktree,
        timeout=max(600, min(timeout, 1800)),
        last_msg_path=o.LOGS_DIR / f"{task['task_id']}.self-repair-blocker.last.txt",
    )
    if not result.get("error"):
        _self_repair_record_council(task, "blocker_verifier", result)
    return result


def _run_self_repair_final_adjudication(task, target, target_wt, target_base, changed_files, diff_text, combined_findings, memory_ctx, timeout):
    repair_cfg = o.load_config().get("self_repair") or {}
    panel = tuple(repair_cfg.get("approval_panel") or repair_cfg.get("council_members") or SELF_REPAIR_COUNCIL_DEFAULT)
    prompt_body = (
        "Adjudicate whether this self-repair branch is safe to advance after functional, security, and architecture review.\n"
        "Valid verdicts: approve, request_change, replan.\n"
        "If there are unresolved concerns, prefer request_change or replan over accepting latent control-plane risk.\n\n"
        f"TARGET TASK: {target.get('task_id')}\n"
        f"TARGET SUMMARY: {target.get('summary') or '-'}\n"
        f"FILES CHANGED vs {target_base}:\n{changed_files}\n\n"
        f"[DIFF vs {target_base}]\n{diff_text}\n\n"
        f"[COMBINED REVIEW FINDINGS]\n{combined_findings or '(none)'}\n\n"
        f"[PROJECT MEMORY]\n{memory_ctx}\n"
    )
    result = _run_self_repair_council(
        task=task,
        stage="final_adjudication",
        panel=panel,
        prompt_body=prompt_body,
        worktree=target_wt,
        timeout=max(600, min(timeout, 1800)),
        last_msg_path=o.LOGS_DIR / f"{task['task_id']}.self-repair-final.last.txt",
    )
    if not result.get("error"):
        _self_repair_record_council(task, "final_adjudication", result)
    return result


def planner_system_prompt(project_name, roadmap_entry_body=""):
    historian_template = planner_historian_template(project_name)
    implementer_templates = planner_implementer_templates(project_name)
    if project_name == "lvc-standard":
        implementer_desc = (
            '  - "lvc-implement-operator": ONLY for adding/modifying a Java operator in '
            "src/ under the hot path, with zero-allocation invariants verifiable via JMH + "
            "conformance tests. Requires Java source changes in the library's hot path.\n"
        )
    elif project_name == "dag-framework":
        implementer_desc = (
            '  - "dag-implement-node": ONLY for adding/modifying one DAG runtime node or '
            "operator in the hot path. Requires Java source changes in dag-framework runtime.\n"
        )
    else:
        implementer_desc = (
            '  - "trp-implement-pipeline-stage": ONLY for ingest/ranking/runtime stage work in '
            "the Java pipeline hot path. Requires non-UI source changes.\n"
            '  - "trp-ui-component": ONLY for typed React/UI/page/component work in apps/, '
            ".tsx, Playwright, or a11y surfaces. Requires UI-facing changes.\n"
        )
    historian_desc = (
        f'  - "{historian_template}": ONLY for appending observations to '
        "repo-memory/RECENT_WORK.md, DECISIONS.md, or FAILURES.md. No source code changes.\n\n"
    )
    allowed = ", ".join(f'"{name}"' for name in (historian_template, *implementer_templates))
    roadmap_block = (
        "Decompose only the roadmap entry below. Treat it as the entire target work "
        "for this planner run.\n\n"
        f"{roadmap_entry_body.strip()}\n\n"
        if roadmap_entry_body else
        "No roadmap entry was provided. Emit an empty array [].\n\n"
    )
    return (
        "You are the BRAID generator for devmini. Your job: read the project memory "
        "and propose at most 3 bounded CODEX execution slices for the next actionable work.\n"
        "Before emitting slices, do a brief internal council pass across architecture, "
        "performance/risk, and delivery perspectives so unsafe work is filtered out early.\n\n"
        f"{roadmap_block}"
        "Only the following braid_template values are valid for codex slices in this project:\n"
        f"{implementer_desc}{historian_desc}"
        "Do NOT emit reviewer or QA slices — those are scheduled by separate tickers. "
        "Do NOT invent other template names. Do NOT use null. "
        "If a candidate piece of work fits NEITHER the allowed templates above "
        "(e.g. CI workflow changes, release automation, cross-project refactors), "
        "SKIP it — do not include it in your output. If nothing fits, emit an empty array [].\n\n"
        f'Use only these template strings verbatim: {allowed}.\n'
        "Every object MUST include summary and braid_template. "
        "Slices may also include optional depends_on as a list of zero-based sibling slice indices "
        "that must complete before that slice can run. Use depends_on only for same-feature "
        "ordering constraints within this emitted array; otherwise omit it.\n"
        "Emit ONLY a JSON array of objects, each with keys:\n"
        "  summary (≤120 chars), braid_template (one of the valid values above, verbatim), "
        "optional depends_on (list[int]).\n"
        "No prose, no markdown fences."
    )


def _contains_any(text, patterns):
    return any(p in text for p in patterns)


def _cross_project_reason(text, project_key):
    for marker in PROJECT_CROSS_MARKERS[project_key]:
        if marker in text:
            return f"cross-project marker {marker!r}"
    return None


def classify_slice(template, summary):
    """Return (ok, reason). ok=False drops the slice with reason logged.

    >>> classify_slice("lvc-implement-operator", "Optimize hot path poller zero alloc jmh gate")[0]
    True
    >>> classify_slice("lvc-implement-operator", "Repair IPC ordering in guaranteed operator")[0]
    True
    >>> classify_slice("lvc-implement-operator", "Tune replay cursor on aeron hot-path")[0]
    True
    >>> classify_slice("lvc-implement-operator", "Rewrite CI workflow for release automation")
    (False, "anti-pattern 'ci workflow'")
    >>> classify_slice("lvc-implement-operator", "Add docs for README and release note")[0]
    False
    >>> classify_slice("lvc-implement-operator", "Build trade-research-platform .tsx page")[0]
    False
    >>> classify_slice("dag-implement-node", "Implement pipeline-node scheduling in dag runtime")[0]
    True
    >>> classify_slice("dag-implement-node", "Tighten processing-element operator traversal")[0]
    True
    >>> classify_slice("dag-implement-node", "Reduce allocs in dag node hot path")[0]
    True
    >>> classify_slice("dag-implement-node", "Add apps/dashboard/page.tsx for dag status")[0]
    False
    >>> classify_slice("dag-implement-node", "Update historian RECENT_WORK entry")[0]
    False
    >>> classify_slice("dag-implement-node", "Fix lvc-standard poller order drift")[0]
    False
    >>> classify_slice("dag-historian-update", "Append historian memory entry to RECENT_WORK")[0]
    True
    >>> classify_slice("dag-historian-update", "Update CURRENT_STATE memory after historian pass")[0]
    True
    >>> classify_slice("dag-historian-update", "Capture dag history note in repo memory")[0]
    True
    >>> classify_slice("dag-historian-update", "Implement dag node traversal optimization")[0]
    False
    >>> classify_slice("dag-historian-update", "Add React component in apps/ui/page.tsx")[0]
    False
    >>> classify_slice("dag-historian-update", "Fix lvc-standard historian note")[0]
    False
    >>> classify_slice("trp-implement-pipeline-stage", "Optimize ingest stage in platform-runtime with jmh")[0]
    True
    >>> classify_slice("trp-implement-pipeline-stage", "Adjust ranking pipeline stage contract handling")[0]
    True
    >>> classify_slice("trp-implement-pipeline-stage", "Tune stage batching in ingest pipeline")[0]
    True
    >>> classify_slice("trp-implement-pipeline-stage", "Widen CryptoController + publishCryptoCycleExplainability; project 7 crypto signals into Phase 1 explainability shape")[0]
    True
    >>> classify_slice("trp-implement-pipeline-stage", "Build apps/research/page.tsx with a11y fixes")[0]
    False
    >>> classify_slice("trp-implement-pipeline-stage", "Append RECENT_WORK historian memory note")[0]
    False
    >>> classify_slice("trp-implement-pipeline-stage", "Patch dag-framework node traversal")[0]
    False
    >>> classify_slice("trp-ui-component", "Add .tsx component with Playwright a11y coverage")[0]
    True
    >>> classify_slice("trp-ui-component", "Refine apps/portfolio page component props")[0]
    True
    >>> classify_slice("trp-ui-component", "Update page component and accessibility flow")[0]
    True
    >>> classify_slice("trp-ui-component", "Update UI types + contract snapshot for crypto Phase 1 payload; flip provenance matrix crypto column to implemented")[0]
    True
    >>> classify_slice("trp-ui-component", "Optimize ingest pipeline stage jmh smoke")[0]
    False
    >>> classify_slice("trp-ui-component", "Append RECENT_WORK history entry")[0]
    False
    >>> classify_slice("trp-ui-component", "Fix lvc-standard operator semantics")[0]
    False
    >>> classify_slice("trp-historian-update", "Append R-001 completion observation to repo-memory/RECENT_WORK.md")[0]
    True
    """
    s = (summary or "").lower()
    if not template:
        return False, "missing braid_template"
    if not s.strip():
        return False, "empty summary"
    if template == "lvc-implement-operator":
        for anti in LVC_OPERATOR_ANTI_PATTERNS:
            if anti in s:
                return False, f"anti-pattern {anti!r}"
        reason = _cross_project_reason(s, "lvc")
        if reason:
            return False, reason
        if not any(p in s for p in LVC_OPERATOR_POSITIVE_PATTERNS):
            return False, "no hot-path keyword"
        return True, None
    if template == "dag-implement-node":
        reason = _cross_project_reason(s, "dag")
        if reason:
            return False, reason
        if _contains_any(s, DAG_HISTORIAN_POSITIVE_PATTERNS):
            return False, "historian slice misrouted to dag implementer"
        if not _contains_any(s, DAG_IMPLEMENT_NODE_POSITIVE_PATTERNS):
            return False, "no dag node keyword"
        return True, None
    if template == "dag-historian-update":
        reason = _cross_project_reason(s, "dag")
        if reason:
            return False, reason
        if _contains_any(s, TRP_UI_COMPONENT_POSITIVE_PATTERNS):
            return False, "ui slice misrouted to historian"
        if _contains_any(s, DAG_IMPLEMENT_NODE_POSITIVE_PATTERNS) and not _contains_any(s, DAG_HISTORIAN_POSITIVE_PATTERNS):
            return False, "implementation slice misrouted to historian"
        if not _contains_any(s, DAG_HISTORIAN_POSITIVE_PATTERNS):
            return False, "no historian keyword"
        return True, None
    if template == "trp-historian-update":
        reason = _cross_project_reason(s, "trp")
        if reason:
            return False, reason
        if _contains_any(s, TRP_UI_COMPONENT_POSITIVE_PATTERNS):
            return False, "ui slice misrouted to historian"
        if _contains_any(s, TRP_PIPELINE_STAGE_POSITIVE_PATTERNS):
            return False, "pipeline-stage slice misrouted to historian"
        if not _contains_any(s, DAG_HISTORIAN_POSITIVE_PATTERNS):
            return False, "no historian keyword"
        return True, None
    if template == "trp-implement-pipeline-stage":
        reason = _cross_project_reason(s, "trp")
        if reason:
            return False, reason
        if _contains_any(s, TRP_UI_COMPONENT_POSITIVE_PATTERNS):
            return False, "ui slice misrouted to pipeline-stage"
        if _contains_any(s, DAG_HISTORIAN_POSITIVE_PATTERNS):
            return False, "historian slice misrouted to pipeline-stage"
        if not _contains_any(s, TRP_PIPELINE_STAGE_POSITIVE_PATTERNS):
            return False, "no pipeline-stage keyword"
        return True, None
    if template == "trp-ui-component":
        reason = _cross_project_reason(s, "trp")
        if reason:
            return False, reason
        if _contains_any(s, TRP_PIPELINE_CONFLICT_PATTERNS):
            return False, "pipeline-stage slice misrouted to ui-component"
        if _contains_any(s, DAG_HISTORIAN_POSITIVE_PATTERNS):
            return False, "historian slice misrouted to ui-component"
        if not _contains_any(s, TRP_UI_COMPONENT_POSITIVE_PATTERNS):
            return False, "no ui-component keyword"
        return True, None
    if template == "lvc-historian-update":
        if _contains_any(s, TRP_UI_COMPONENT_POSITIVE_PATTERNS) or _contains_any(s, TRP_PIPELINE_STAGE_POSITIVE_PATTERNS):
            return False, "non-historian trp slice misrouted to lvc historian"
        if _contains_any(s, DAG_IMPLEMENT_NODE_POSITIVE_PATTERNS) and not _contains_any(s, DAG_HISTORIAN_POSITIVE_PATTERNS):
            return False, "implementation slice misrouted to lvc historian"
        if not _contains_any(s, DAG_HISTORIAN_POSITIVE_PATTERNS):
            return False, "no historian keyword"
        return True, None
    return False, f"unknown template {template!r}"


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
    "main_dirty_or_ahead",
)


def topology_reason_is_valid(trailer):
    _, _, rest = trailer.partition(":")
    rest = rest.strip().lower()
    if not rest:
        return False
    return any(code in rest for code in VALID_TOPOLOGY_REASONS)


def topology_reason_code(trailer):
    _, _, rest = trailer.partition(":")
    rest = rest.strip().lower()
    if not rest:
        return None
    for code in VALID_TOPOLOGY_REASONS:
        if code in rest:
            return code
    return None


_BRAID_REFINE_NODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def parse_braid_refine(trailer):
    """Parse `BRAID_REFINE: <node-id>: <missing-edge-condition>`.

    >>> parse_braid_refine("BRAID_REFINE: CheckBaseline: add baseline_red edge to End")
    {'node_id': 'CheckBaseline', 'condition': 'add baseline_red edge to End'}
    >>> parse_braid_refine("BRAID_REFINE: Sketch: define restore handoff semantics for journal cursor reconciliation and delayed visibility release until catch-up")
    {'node_id': 'Sketch', 'condition': 'define restore handoff semantics for journal cursor reconciliation and delayed visibility release until catch-up'}
    >>> parse_braid_refine("BRAID_REFINE: bad-node: something") is None
    True
    >>> parse_braid_refine("BRAID_REFINE: CheckBaseline") is None
    True
    """
    prefix = "BRAID_REFINE:"
    if not trailer.startswith(prefix):
        return None
    rest = trailer[len(prefix):].strip()
    node_id, sep, condition = rest.partition(":")
    node_id = node_id.strip()
    condition = condition.strip()
    if not sep or not _BRAID_REFINE_NODE_RE.fullmatch(node_id):
        return None
    # Refinement requests can legitimately carry substantial missing-context
    # detail for template repair; keep them bounded, but well above terse edge labels.
    if not condition or len(condition) > 600:
        return None
    if any(ch in condition for ch in "\r\n"):
        return None
    return {"node_id": node_id, "condition": condition}


def _find_braid_trailer(lines):
    for line in reversed(lines[-20:]):
        if line.startswith("BRAID_OK") or line.startswith("BRAID_TOPOLOGY_ERROR") or line.startswith("BRAID_REFINE"):
            return line
    return ""


def enqueue_braid_refine(task, project_name, trailer, *, from_state):
    refine = parse_braid_refine(trailer)
    if not refine:
        def mut_fail(t):
            t["finished_at"] = o.now_iso()
            t["false_blocker_claim"] = trailer
            o.set_task_blocker(
                t,
                "invalid_braid_refine",
                summary="invalid BRAID_REFINE contract",
                detail=trailer,
                source="worker",
                retryable=False,
            )
        o.move_task(
            task["task_id"],
            from_state,
            "failed",
            reason=f"invalid BRAID_REFINE: {trailer[:80]}",
            mutator=mut_fail,
        )
        return

    bt = task.get("braid_template")
    template_hash = task.get("braid_template_hash")
    if not bt or not template_hash:
        def mut_fail(t):
            t["finished_at"] = o.now_iso()
            t["false_blocker_claim"] = trailer
            o.set_task_blocker(
                t,
                "invalid_braid_refine",
                summary="BRAID_REFINE missing active template context",
                detail=trailer,
                source="worker",
                retryable=False,
            )
        o.move_task(
            task["task_id"],
            from_state,
            "failed",
            reason="BRAID_REFINE without active template context",
            mutator=mut_fail,
        )
        return

    rounds = int(task.get("refine_round", 0))
    if rounds >= 3:
        o.braid_template_record_use(bt, topology_error=True)
        regen = o.request_template_regen(
            bt,
            project_name=project_name,
            source_task_id=task["task_id"],
            reason=trailer,
        )

        def mut_block(t):
            t["finished_at"] = o.now_iso()
            t["topology_error"] = f"BRAID_TOPOLOGY_ERROR: refine_rounds_exhausted after {trailer}"
            detail = trailer
            if not regen.get("enqueued"):
                detail = (
                    f"{trailer}\nregen_attempts={regen.get('attempts')}/{regen.get('max_attempts')} "
                    f"({regen.get('reason')})"
                )
            o.set_task_blocker(
                t,
                "template_refine_exhausted",
                summary="template refinement exhausted",
                detail=detail,
                source="worker",
                retryable=False,
            )
        o.move_task(
            task["task_id"],
            from_state,
            "blocked",
            reason="refine exhausted -> regen" if regen.get("enqueued") else "refine exhausted -> regen capped",
            mutator=mut_block,
        )
        return

    refine_task = o.new_task(
        role="planner",
        engine="claude",
        project=project_name,
        summary=f"Refine BRAID template for {bt} around {refine['node_id']}",
        source=f"refine-for:{task['task_id']}",
        braid_template=bt,
        engine_args={
            "mode": "template-refine",
            "refine_request": {
                **refine,
                "template_hash": template_hash,
                "origin_task_id": task["task_id"],
            },
        },
    )
    o.enqueue_task(refine_task)

    def mut_block(t):
        t["finished_at"] = o.now_iso()
        t["topology_error"] = trailer
        t["refine_round"] = rounds + 1
        t["refine_request"] = {
            **refine,
            "template_hash": template_hash,
            "refine_task_id": refine_task["task_id"],
        }
        o.set_task_blocker(
            t,
            "template_missing_edge",
            summary=f"template refinement requested around {refine['node_id']}",
            detail=refine["condition"],
            source="worker",
            retryable=True,
            metadata={"node_id": refine["node_id"], "refine_task_id": refine_task["task_id"]},
        )
    o.move_task(task["task_id"], from_state, "blocked", reason=trailer[:80], mutator=mut_block)


def run_claude_planner(task, cfg, timeout, log_path):
    """Freeform planner: claude reads memory and emits slice proposals."""
    task_id = task["task_id"]
    project_name = task.get("project")
    planner_mode = ((task.get("engine_args") or {}).get("mode") or "").strip()
    try:
        project = o.get_project(cfg, project_name)
    except KeyError:
        fail_task(task_id, "claimed", f"unknown project {project_name}",
                  blocker_code="runtime_unknown_project", summary="unknown project", retryable=False)
        return

    roadmap_entry = (task.get("engine_args") or {}).get("roadmap_entry") or {}
    planner_query = " ".join(filter(None, [roadmap_entry.get("title"), roadmap_entry.get("body"), task.get("summary")]))
    memory_ctx = read_memory_context(project["name"], project["path"], role="planner", query=planner_query)
    if planner_mode == "self-repair-plan":
        system_prompt, user_prompt, council_members = self_repair_council_prompt(task, project, memory_ctx)
    else:
        system_prompt, user_prompt, council_members = planner_council_prompt(task, project, memory_ctx, cfg)

    cmd = [
        "claude",
        "-p", user_prompt,
        "--dangerously-skip-permissions",
        "--system-prompt", system_prompt,
        "--output-format", "json",
        "--model", "opus" if planner_mode == "self-repair-plan" else "sonnet",
        "--max-budget-usd", _claude_budget_flag("planner", cfg=cfg, task=task, mode=planner_mode),
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
            proc = _run_bounded(
                cmd, stdout=subprocess.PIPE, stderr=logf, text=True, timeout=timeout,
                env=_claude_subprocess_env(),
                cwd="/tmp",  # avoid CLAUDE.md auto-discovery
            )
        except subprocess.TimeoutExpired:
            fail_task(task_id, "running", f"claude timeout {timeout}s",
                      blocker_code="llm_timeout", summary="claude timeout", retryable=True)
            return
        logf.write(f"\n# exit: {proc.returncode}\n# stdout:\n{proc.stdout or ''}\n")
    model_name = "opus" if planner_mode == "self-repair-plan" else "sonnet"
    _record_task_costs_from_text(task_id, "claude", model_name, proc.stdout or "")
    try:
        payload = json.loads(proc.stdout or "")
        raw = str(payload.get("result") or "")
    except json.JSONDecodeError:
        raw = proc.stdout or ""

    if proc.returncode != 0:
        _pause_claude_slot_if_needed(raw or proc.stdout or f"claude exit {proc.returncode}", task=task)
        fail_task(task_id, "running", f"claude exit {proc.returncode}",
                  blocker_code="llm_exit_error", summary="claude exit error", retryable=True)
        return

    if planner_mode == "self-repair-plan":
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            fail_task(task_id, "running", "no json object in self-repair planner output",
                      blocker_code="model_output_invalid", summary="planner output invalid", retryable=False)
            return
        try:
            plan = json.loads(m.group(0))
        except json.JSONDecodeError:
            fail_task(task_id, "running", "malformed self-repair planner json",
                      blocker_code="model_output_invalid", summary="planner output invalid", retryable=False)
            return
        slices = plan.get("slices")
        if not isinstance(slices, list):
            fail_task(task_id, "running", "self-repair planner missing slices",
                      blocker_code="model_output_invalid", summary="planner output invalid", retryable=False)
            return
    else:
        plan = None
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                candidate = json.loads(m.group(0))
            except json.JSONDecodeError:
                candidate = None
            if isinstance(candidate, dict) and isinstance(candidate.get("slices"), list):
                plan = candidate
                slices = candidate.get("slices") or []
        if plan is None:
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                fail_task(task_id, "running", "no json array/object in output",
                          blocker_code="model_output_invalid", summary="planner output invalid", retryable=False)
                return
            try:
                slices = json.loads(m.group(0))
            except json.JSONDecodeError:
                fail_task(task_id, "running", "malformed json array",
                          blocker_code="model_output_invalid", summary="planner output invalid", retryable=False)
                return
            plan = {
                "panel": list(council_members),
                "key_agreements": [],
                "dissent": [],
                "execution_path": "planner-council",
                "slices": slices,
            }

    # Template → role for codex slices. Slices with a template outside this set
    # are dropped — the planner prompt instructs claude to skip ill-fitting work
    # rather than invent new templates.
    TEMPLATE_ROLE = (
        {"orchestrator-self-repair": "implementer"}
        if planner_mode == "self-repair-plan"
        else planner_template_roles(project["name"])
    )

    feature_id = task.get("feature_id")

    enqueued = []
    dropped = []
    sibling_task_ids = []
    council_meta = {
        "panel": list(plan.get("panel") or council_members),
        "key_agreements": list(plan.get("key_agreements") or []),
        "dissent": list(plan.get("dissent") or []),
        "execution_path": plan.get("execution_path") or "",
        "chosen_strategy": plan.get("chosen_strategy") or plan.get("execution_path") or "",
        "rejected_strategies": list(plan.get("rejected_strategies") or []),
        "dissent_reasons": list(plan.get("dissent_reasons") or []),
        "confidence": plan.get("confidence"),
        "retry_conditions": list(plan.get("retry_conditions") or []),
        "escalation_threshold": plan.get("escalation_threshold") or "",
    }
    shared_engine_args = {}
    if planner_mode == "self-repair-plan":
        shared_engine_args = {
            "self_repair": dict((task.get("engine_args") or {}).get("self_repair") or {}),
            "evidence": (task.get("engine_args") or {}).get("evidence") or "",
            "issue_key": (task.get("engine_args") or {}).get("issue_key"),
            "issue_summary": (task.get("engine_args") or {}).get("issue_summary") or task.get("summary") or "",
            "council": council_meta,
        }
    elif council_meta["panel"]:
        shared_engine_args = {"council": council_meta}
    for idx, s in enumerate(slices[:3]):
        raw_tpl = s.get("braid_template")
        summ = s.get("summary", "")
        if raw_tpl not in TEMPLATE_ROLE:
            dropped.append((raw_tpl, summ[:80], "unknown template"))
            sibling_task_ids.append(None)
            continue
        if planner_mode == "self-repair-plan":
            ok, reason = (bool(summ.strip()), None if summ.strip() else "empty summary")
        else:
            ok, reason = classify_slice(raw_tpl, summ)
        if not ok:
            dropped.append((raw_tpl, summ[:80], reason))
            sibling_task_ids.append(None)
            continue
        child = o.new_task(
            role=TEMPLATE_ROLE[raw_tpl],
            engine="codex",
            project=project["name"],
            summary=summ[:240],
            source=f"slice-of:{task_id}",
            braid_template=raw_tpl,
            parent_task_id=task_id,
            feature_id=feature_id,
            depends_on=[],
            engine_args=dict(shared_engine_args),
        )
        raw_depends = s.get("depends_on")
        resolved = []
        if isinstance(raw_depends, list):
            for dep in raw_depends:
                if not isinstance(dep, int):
                    dropped.append((raw_tpl, summ[:80], f"depends_on index not int: {dep!r}"))
                    resolved = None
                    break
                if dep < 0 or dep >= idx:
                    dropped.append((raw_tpl, summ[:80], f"depends_on index out of range: {dep}"))
                    resolved = None
                    break
                dep_task_id = sibling_task_ids[dep]
                if dep_task_id is None:
                    dropped.append((raw_tpl, summ[:80], f"depends_on refers to dropped slice: {dep}"))
                    resolved = None
                    break
                resolved.append(dep_task_id)
        if resolved is None:
            sibling_task_ids.append(None)
            continue
        child["depends_on"] = resolved
        o.enqueue_task(child)
        enqueued.append(child["task_id"])
        sibling_task_ids.append(child["task_id"])
        if feature_id:
            try:
                o.append_feature_child(feature_id, child["task_id"])
            except FileNotFoundError:
                pass  # feature record lost; child still lives as orphan

    if dropped:
        with log_path.open("a") as logf:
            logf.write(f"\n# dropped {len(dropped)} slice(s):\n")
            for tpl, s80, reason in dropped:
                logf.write(f"#   template={tpl!r} reason={reason} :: {s80}\n")

    def mut(t):
        t["artifacts"] = t.get("artifacts", []) + enqueued
        if council_meta["panel"]:
            t["council"] = council_meta
        t["finished_at"] = o.now_iso()
        t["log_path"] = str(log_path)
    o.move_task(task_id, "running", "done", reason=f"decomposed into {len(enqueued)}", mutator=mut)
    if planner_mode == "self-repair-plan" and feature_id and (task.get("engine_args") or {}).get("issue_key"):
        issue_status = "planned"
        issue_updates = {
            "planner_task_id": task_id,
            "chosen_strategy": council_meta.get("chosen_strategy") or "",
            "rejected_strategies": list(council_meta.get("rejected_strategies") or ()),
            "dissent_reasons": list(council_meta.get("dissent_reasons") or ()),
            "confidence": council_meta.get("confidence"),
            "retry_conditions": list(council_meta.get("retry_conditions") or ()),
            "escalation_threshold": council_meta.get("escalation_threshold") or "",
            "planned_task_ids": list(enqueued),
        }
        if enqueued:
            issue_status = "executing"
            issue_updates["execution_task_ids"] = list(enqueued)
        o.self_repair_record_deliberation(
            feature_id,
            (task.get("engine_args") or {}).get("issue_key"),
            stage="planning",
            verdict="planned",
            panel=council_meta.get("panel") or (),
            chosen_strategy=council_meta.get("chosen_strategy") or "",
            rejected_strategies=council_meta.get("rejected_strategies") or (),
            dissent_reasons=council_meta.get("dissent_reasons") or (),
            confidence=council_meta.get("confidence"),
            retry_conditions=council_meta.get("retry_conditions") or (),
            escalation_threshold=council_meta.get("escalation_threshold") or "",
            reason=council_meta.get("execution_path") or "",
            task_id=task_id,
        )
        o._self_repair_mark_issue(
            feature_id,
            (task.get("engine_args") or {}).get("issue_key"),
            status=issue_status,
            **issue_updates,
        )


def run_claude_reviewer(task, cfg, timeout, log_path):
    """Dual internal review gate before QA/push.

    Pass 1 is a Codex review with direct worktree access. Pass 2 is a Claude
    adversarial review over the same worktree. The target only advances to QA
    if both approve and no hard policy checks fail.
    """
    task_id = task["task_id"]
    project_name = task.get("project")
    try:
        project = o.get_project(cfg, project_name)
    except KeyError:
        fail_task(task_id, "claimed", f"unknown project {project_name}",
                  blocker_code="runtime_unknown_project", summary="unknown project", retryable=False)
        return
    if not (cfg.get("reviews") or {}).get("self_review", True):
        fail_task(
            task_id,
            "claimed",
            "review runtime invariant violated: reviews.self_review must remain enabled",
            blocker_code="runtime_precondition_failed",
            summary="self-review invariant disabled",
            retryable=False,
        )
        return

    # Pick oldest awaiting-review task for this project (FIFO by finished_at).
    candidates = []
    for t in o.iter_tasks(states=("awaiting-review",), project=project_name):
        if t.get("project") == project_name:
            candidates.append((t.get("finished_at") or t.get("task_id") or "", t))
    if not candidates:
        def mut_noop(t):
            t["finished_at"] = o.now_iso()
            t["log_path"] = str(log_path)
        o.move_task(task_id, "claimed", "done", reason="nothing to review", mutator=mut_noop)
        return
    candidates.sort(key=lambda x: x[0])
    target = candidates[0][1]
    target_id = target["task_id"]

    reviewer_template = o.project_reviewer_template(project_name)
    graph_body, graph_hash = o.braid_template_load(reviewer_template)
    if graph_body is None:
        fail_task(task_id, "claimed", "reviewer template missing",
                  blocker_code="runtime_precondition_failed", summary="reviewer template missing", retryable=False)
        return

    # Pull diff from the target's worktree (capped to keep prompt bounded).
    target_wt = target.get("worktree")
    target_base = target.get("base_branch") or base_branch_for_task(target)
    if not target_wt or not pathlib.Path(target_wt).exists():
        if _self_repair_enabled(target):
            _self_repair_reopen_current_issue(
                target,
                blocker_code="qa_target_missing",
                reason=f"review target worktree missing: {target_wt}",
                deliberation={
                    "stage": "blocker_verifier",
                    "verdict": "replan",
                    "chosen_strategy": "reopen_issue",
                    "reason": f"review target worktree missing: {target_wt}",
                    "task_id": task_id,
                },
                summary_suffix="review-worktree-missing",
            )
            o.move_task(
                target_id,
                "awaiting-review",
                "abandoned",
                reason=f"review target worktree missing: {target_wt}",
                mutator=lambda t: t.update({"finished_at": o.now_iso()}),
            )
            def mut_self(t):
                t["finished_at"] = o.now_iso()
            o.move_task(task_id, "claimed", "done", reason=f"reopened self-repair target {target_id}", mutator=mut_self)
            o.tick_self_repair_queue()
            return
        fail_task(task_id, "claimed", f"review target worktree missing: {target_wt}",
                  blocker_code="qa_target_missing", summary="review target worktree missing", retryable=False)
        return
    changed_files, diff_text = _git_diff_summary(target_wt, target_base)
    review_query = " ".join(filter(None, [target.get("summary"), changed_files]))
    memory_ctx = read_memory_context(
        project["name"],
        project["path"],
        role="reviewer",
        query=review_query,
        task_id=task_id,
        changed_files_text=changed_files,
    )
    policy_findings = _policy_findings_for_diff(project_name, target_wt, target_base)
    secret_findings = _security_gate_findings(target_wt, target_base)
    supply_chain_findings = _supply_chain_findings(target_wt, target_base)
    compile_findings = _compile_gate_findings(project, target_wt, changed_files)
    ratio_findings = _test_ratio_findings(changed_files, task=target, worktree=target_wt, cfg=cfg)
    feature = o.read_feature(target.get("feature_id")) if target.get("feature_id") else None
    roadmap_ctx = ""
    if feature:
        roadmap_ctx = (
            f"ROADMAP ENTRY ID: {feature.get('roadmap_entry_id') or '(none)'}\n"
            f"ROADMAP TITLE: {(feature.get('roadmap_entry') or {}).get('title') or '(none)'}\n"
            f"ROADMAP BODY:\n{(feature.get('roadmap_entry') or {}).get('body') or '(none)'}\n\n"
        )
    review_panel = _config_council_panel(cfg, "review_panel", ("socrates", "kahneman", "torvalds"))
    council_ctx = "\n\n".join(_council_member_context(member) for member in review_panel)
    gate_names = ["security-review-pass"]
    if supply_chain_findings:
        gate_names.append("supply-chain-audit-pass")
    gate_names.append("architectural-fit-review-pass")
    gate_panels = {
        "security-review-pass": _config_council_panel(cfg, "security_panel", review_panel or ("socrates", "kahneman", "torvalds")),
        "supply-chain-audit-pass": _config_council_panel(cfg, "security_panel", review_panel or ("socrates", "kahneman", "torvalds")),
        "architectural-fit-review-pass": _config_council_panel(cfg, "architecture_panel", review_panel or ("socrates", "kahneman", "torvalds")),
    }
    is_self_repair_target = _self_repair_enabled(target)

    codex_prompt = (
        "[BRAID REVIEW GRAPH — traverse deterministically.]\n"
        f"{graph_body}\n"
        "[END GRAPH]\n\n"
        "You are the primary internal reviewer before any autonomous push.\n"
        "- Read the changed files, surrounding implementation, related call sites, and relevant tests/benchmarks.\n"
        "- Treat repo policy and engineering-memory skills as binding constraints.\n"
        "- Focus on regressions, missing invariants, concurrency/performance hazards, incomplete tests, and contract mismatches.\n"
        "- Do not modify files. Review only.\n"
        "- If the diff is insufficient, inspect the repository directly from the worktree.\n\n"
        f"REVIEW TARGET: {target_id}\n"
        f"TARGET SUMMARY: {target.get('summary','')}\n"
        f"TARGET WORKTREE: {target_wt}\n"
        f"FILES CHANGED vs {target_base}:\n{changed_files}\n\n"
        f"[DIFF vs {target_base}]\n{diff_text}\n\n"
        f"{roadmap_ctx}"
        + (
            "[PRE-CHECK POLICY FINDINGS]\n"
            + "\n".join(f"- {item}" for item in (policy_findings + secret_findings + supply_chain_findings + compile_findings + ratio_findings))
            + "\n\n"
            if (policy_findings or secret_findings or supply_chain_findings or compile_findings or ratio_findings) else ""
        )
        + f"[PROJECT CONTEXT]\n{memory_ctx}\n\n"
        "Emit EXACTLY one final line:\n"
        "  BRAID_OK: APPROVE — <one-line justification>\n"
        "  BRAID_OK: REQUEST_CHANGE — <one-line reason>\n"
        "  BRAID_TOPOLOGY_ERROR: <reason graph could not be traversed>\n"
    )
    claude_prompt = (
        "[BRAID REVIEW GRAPH — traverse deterministically.]\n"
        f"{graph_body}\n"
        "[END GRAPH]\n\n"
        "You are the council-backed second reviewer before autonomous push.\n"
        "- Run an internal two-round council across the provided panel before deciding.\n"
        "- Preserve dissent in your body; do not flatten disagreement away.\n"
        "- Assume the first reviewer may have missed something important.\n"
        "- Hunt for reasons this branch should NOT be pushed yet.\n"
        "- Pay extra attention to ULL rules, locking, hidden allocations, weak benchmarks, API drift, and missing QA coverage.\n"
        "- Do not modify files. Review only.\n\n"
        "[COUNCIL PERSONAS]\n"
        f"{council_ctx}\n"
        "[END COUNCIL PERSONAS]\n\n"
        f"REVIEW TARGET: {target_id}\n"
        f"TARGET SUMMARY: {target.get('summary','')}\n"
        f"TARGET WORKTREE: {target_wt}\n"
        f"FILES CHANGED vs {target_base}:\n{changed_files}\n\n"
        f"[DIFF vs {target_base}]\n{diff_text}\n\n"
        f"{roadmap_ctx}"
        + (
            "[PRE-CHECK POLICY FINDINGS]\n"
            + "\n".join(f"- {item}" for item in (policy_findings + secret_findings + supply_chain_findings + compile_findings + ratio_findings))
            + "\n\n"
            if (policy_findings or secret_findings or supply_chain_findings or compile_findings or ratio_findings) else ""
        )
        + f"[PROJECT CONTEXT]\n{memory_ctx}\n\n"
        "Emit EXACTLY one final line:\n"
        "  BRAID_OK: APPROVE — <one-line justification>\n"
        "  BRAID_OK: REQUEST_CHANGE — <one-line reason>\n"
        "  BRAID_TOPOLOGY_ERROR: <reason graph could not be traversed>\n"
    )

    def mut_running(t):
        t["started_at"] = o.now_iso()
        t["log_path"] = str(log_path)
        ea = dict(t.get("engine_args", {}))
        ea["target_task_id"] = target_id
        t["engine_args"] = ea
    o.move_task(task_id, "claimed", "running", reason=f"review of {target_id}", mutator=mut_running)

    with log_path.open("w") as logf:
        logf.write(f"# codex reviewer task={task_id} target={target_id}\n")
        logf.write(f"# template={reviewer_template} hash={graph_hash}\n\n")
        review_timeout = max(timeout, 2400)
        codex_last_msg_path = o.LOGS_DIR / f"{task_id}.codex.last.txt"
        codex_result = _run_review_agent(
            "codex",
            prompt=codex_prompt,
            worktree=target_wt,
            timeout=review_timeout,
            logf=logf,
            last_msg_path=codex_last_msg_path,
        )
        if codex_result.get("error"):
            o.braid_template_record_use(reviewer_template, topology_error=True)
            blocker = codex_result.get("blocker_code") or ("llm_timeout" if "timeout" in codex_result["error"] else "llm_exit_error")
            _mark_review_target_for_retry(target_id, codex_result["error"])
            fail_task(task_id, "running", codex_result["error"],
                      blocker_code=blocker, summary="codex reviewer failed", retryable=True)
            return
        if codex_result.get("verdict") == "topology_error":
            if is_self_repair_target:
                verifier = _run_self_repair_blocker_verifier(
                    target,
                    project,
                    target_wt,
                    review_timeout,
                    "template_graph_error",
                    "reviewer topology error",
                    memory_ctx,
                )
                _self_repair_reopen_current_issue(
                    target,
                    blocker_code="template_graph_error",
                    reason=verifier.get("reason") or "reviewer topology error",
                    deliberation={**verifier, "stage": "blocker_verifier", "task_id": task_id},
                    summary_suffix="review-topology",
                )
                o.move_task(target_id, "awaiting-review", "abandoned",
                            reason=verifier.get("reason") or "reviewer topology error",
                            mutator=lambda t: t.update({"finished_at": o.now_iso(), "self_repair_verifier": verifier}))
                def mut_self(t):
                    t["finished_at"] = o.now_iso()
                o.move_task(task_id, "running", "done",
                            reason=f"reopened self-repair target {target_id}", mutator=mut_self)
                o.tick_self_repair_queue()
                return
            o.braid_template_record_use(reviewer_template, topology_error=True)
            _mark_review_target_for_retry(target_id, "reviewer topology error")
            fail_task(task_id, "running", "reviewer topology error",
                      blocker_code="template_graph_error", summary="reviewer topology error", retryable=True)
            return
        combined_findings = codex_result.get("raw", "")
        final_verdict = codex_result.get("verdict")

        if final_verdict == "approve":
            logf.write("\n# claude adversarial reviewer\n")
            claude_last_msg_path = o.LOGS_DIR / f"{task_id}.claude.last.txt"
            claude_result = _run_review_agent(
                "claude",
                prompt=claude_prompt,
                worktree=target_wt,
                timeout=review_timeout,
                logf=logf,
                last_msg_path=claude_last_msg_path,
            )
            if claude_result.get("error"):
                o.braid_template_record_use(reviewer_template, topology_error=True)
                blocker = claude_result.get("blocker_code") or ("llm_timeout" if "timeout" in claude_result["error"] else "llm_exit_error")
                _mark_review_target_for_retry(target_id, claude_result["error"])
                fail_task(task_id, "running", claude_result["error"],
                          blocker_code=blocker, summary="claude reviewer failed", retryable=True)
                return
            if claude_result.get("verdict") == "topology_error":
                if is_self_repair_target:
                    verifier = _run_self_repair_blocker_verifier(
                        target,
                        project,
                        target_wt,
                        review_timeout,
                        "template_graph_error",
                        "claude reviewer topology error",
                        memory_ctx,
                    )
                    _self_repair_reopen_current_issue(
                        target,
                        blocker_code="template_graph_error",
                        reason=verifier.get("reason") or "claude reviewer topology error",
                        deliberation={**verifier, "stage": "blocker_verifier", "task_id": task_id},
                        summary_suffix="review-topology",
                    )
                    o.move_task(target_id, "awaiting-review", "abandoned",
                                reason=verifier.get("reason") or "claude reviewer topology error",
                                mutator=lambda t: t.update({"finished_at": o.now_iso(), "self_repair_verifier": verifier}))
                    def mut_self(t):
                        t["finished_at"] = o.now_iso()
                    o.move_task(task_id, "running", "done",
                                reason=f"reopened self-repair target {target_id}", mutator=mut_self)
                    o.tick_self_repair_queue()
                    return
                o.braid_template_record_use(reviewer_template, topology_error=True)
                _mark_review_target_for_retry(target_id, "claude reviewer topology error")
                fail_task(task_id, "running", "claude reviewer topology error",
                          blocker_code="template_graph_error", summary="claude reviewer topology error", retryable=True)
                return
            combined_findings = (combined_findings + "\n\n" + claude_result.get("raw", "")).strip()
            final_verdict = claude_result.get("verdict")

        if final_verdict == "approve":
            for gate_name in gate_names:
                gate_memory_ctx = read_memory_context(
                    project["name"],
                    project["path"],
                    role="reviewer",
                    query=review_query,
                    gate_name=gate_name,
                    task_id=task_id,
                    changed_files_text=changed_files,
                )
                try:
                    gate_result = _run_specialized_review_gate(
                        gate_name,
                        task_id=task_id,
                        target=target,
                        target_wt=target_wt,
                        target_base=target_base,
                        changed_files=changed_files,
                        diff_text=diff_text,
                        roadmap_ctx=roadmap_ctx,
                        memory_ctx=gate_memory_ctx,
                        panel=gate_panels.get(gate_name) or review_panel,
                        timeout=review_timeout,
                        logf=logf,
                    )
                except Exception as exc:
                    _review_gate_protocol_failure(
                        task_id,
                        target_id,
                        gate_name,
                        f"{gate_name} raised {exc.__class__.__name__}: {exc}",
                        summary=f"{gate_name} protocol failure",
                    )
                    return
                if gate_result.get("error"):
                    blocker = gate_result.get("blocker_code") or "review_gate_protocol_error"
                    _mark_review_target_for_retry(
                        target_id,
                        gate_result["error"],
                        blocker_code=blocker,
                        metadata={"gate_name": gate_name},
                    )
                    o.append_event(
                        "review-gate",
                        "protocol_error",
                        task_id=task_id,
                        feature_id=target.get("feature_id"),
                        details={"target_task_id": target_id, "gate_name": gate_name, "error": gate_result["error"][:240]},
                    )
                    fail_task(task_id, "running", gate_result["error"],
                              blocker_code=blocker, summary=f"{gate_name} failed", retryable=True)
                    return
                if gate_result.get("verdict") == "topology_error":
                    if is_self_repair_target:
                        verifier = _run_self_repair_blocker_verifier(
                            target,
                            project,
                            target_wt,
                            review_timeout,
                            "template_graph_error",
                            f"{gate_name} topology error",
                            memory_ctx,
                        )
                        _self_repair_reopen_current_issue(
                            target,
                            blocker_code="template_graph_error",
                            reason=verifier.get("reason") or f"{gate_name} topology error",
                            deliberation={**verifier, "stage": "blocker_verifier", "task_id": task_id},
                            summary_suffix="gate-topology",
                        )
                        o.move_task(target_id, "awaiting-review", "abandoned",
                                    reason=verifier.get("reason") or f"{gate_name} topology error",
                                    mutator=lambda t: t.update({"finished_at": o.now_iso(), "self_repair_verifier": verifier}))
                        def mut_self(t):
                            t["finished_at"] = o.now_iso()
                        o.move_task(task_id, "running", "done",
                                    reason=f"reopened self-repair target {target_id}", mutator=mut_self)
                        o.tick_self_repair_queue()
                        return
                    _mark_review_target_for_retry(target_id, f"{gate_name} topology error")
                    fail_task(task_id, "running", f"{gate_name} topology error",
                              blocker_code="template_graph_error", summary=f"{gate_name} topology error", retryable=True)
                    return
                combined_findings = (combined_findings + "\n\n" + gate_result.get("raw", "")).strip()
                final_verdict = gate_result.get("verdict")
                if final_verdict != "approve":
                    break

        self_repair_adjudication = None
        if final_verdict == "approve" and is_self_repair_target:
            try:
                self_repair_adjudication = _run_self_repair_final_adjudication(
                    target,
                    target,
                    target_wt,
                    target_base,
                    changed_files,
                    diff_text,
                    combined_findings,
                    memory_ctx,
                    review_timeout,
                )
            except Exception as exc:
                _review_gate_protocol_failure(
                    task_id,
                    target_id,
                    "self-repair-adjudication",
                    f"self-repair-adjudication raised {exc.__class__.__name__}: {exc}",
                    summary="self-repair final adjudication failed",
                )
                return
            if self_repair_adjudication.get("error"):
                _mark_review_target_for_retry(
                    target_id,
                    self_repair_adjudication["error"],
                    blocker_code=self_repair_adjudication.get("blocker_code") or "review_gate_protocol_error",
                    metadata={"gate_name": "self-repair-adjudication"},
                )
                fail_task(task_id, "running", self_repair_adjudication["error"],
                          blocker_code=self_repair_adjudication.get("blocker_code") or "review_gate_protocol_error", summary="self-repair final adjudication failed", retryable=True)
                return
            combined_findings = (combined_findings + "\n\n" + self_repair_adjudication.get("raw", "")).strip()
            final_verdict = "approve" if self_repair_adjudication.get("verdict") == "approve" else (
                "request_change" if self_repair_adjudication.get("verdict") == "request_change" else self_repair_adjudication.get("verdict")
            )

        hard_gate_findings = policy_findings + secret_findings + supply_chain_findings + compile_findings + ratio_findings
        if final_verdict == "approve" and hard_gate_findings:
            final_verdict = "request_change"
            combined_findings = (
                combined_findings
                + "\n\nPolicy gate findings:\n"
                + "\n".join(f"- {item}" for item in hard_gate_findings)
            )

    if final_verdict is None:
        _mark_review_target_for_retry(target_id, "review verdict missing")
        fail_task(task_id, "running", "no review verdict trailer",
                  blocker_code="model_output_invalid", summary="review verdict missing", retryable=False)
        return

    if final_verdict == "topology_error":
        o.braid_template_record_use(reviewer_template, topology_error=True)
        _mark_review_target_for_retry(target_id, "reviewer topology error")
        fail_task(task_id, "running", "reviewer topology error",
                  blocker_code="template_graph_error", summary="reviewer topology error", retryable=True)
        return

    o.braid_template_record_use(reviewer_template, topology_error=False)

    def mut_target(t):
        t["review_verdict"] = final_verdict
        t["reviewed_at"] = o.now_iso()
        t["reviewed_by"] = task_id
        t["policy_review_findings"] = policy_findings + secret_findings + supply_chain_findings + compile_findings + ratio_findings
        t["review_council_panel"] = list(review_panel)
        t["review_gates"] = ["functional-review", "council-review", *gate_names] + (["self-repair-adjudication"] if is_self_repair_target else [])
        t["review_gate_panels"] = {name: list(gate_panels.get(name) or ()) for name in gate_names}
        if is_self_repair_target:
            t["review_gate_panels"]["self-repair-adjudication"] = list(
                (o.load_config().get("self_repair") or {}).get("approval_panel")
                or (o.load_config().get("self_repair") or {}).get("council_members")
                or ()
            )

    if final_verdict == "approve":
        o.move_task(target_id, "awaiting-review", "awaiting-qa",
                    reason=f"approved by {task_id}", mutator=mut_target)
    elif final_verdict == "replan" and is_self_repair_target:
        _self_repair_reopen_current_issue(
            target,
            blocker_code="self_repair_replan_requested",
            reason=(self_repair_adjudication or {}).get("reason") or "self-repair final adjudication requested replan",
            deliberation={**(self_repair_adjudication or {}), "stage": "final_adjudication", "task_id": task_id},
        )
        def mut_replan_target(t):
            mut_target(t)
            t["self_repair_adjudication"] = self_repair_adjudication
        o.move_task(
            target_id,
            "awaiting-review",
            "abandoned",
            reason=(self_repair_adjudication or {}).get("reason") or "self-repair final adjudication requested replan",
            mutator=mut_replan_target,
        )
        o.tick_self_repair_queue()
    else:
        _handle_review_request_change(task_id, project_name, target, combined_findings[:12000], mut_target)

    def mut_self(t):
        t["finished_at"] = o.now_iso()
    o.move_task(task_id, "running", "done",
                reason=f"reviewed {target_id} -> {final_verdict}", mutator=mut_self)


def _handle_review_request_change(reviewer_task_id, project_name, target, review_findings, mut_target):
    """Route reviewer request_change into feedback retry or terminal failure.

    >>> _review_request_change_doctest()
    ((('fail', 'review_feedback_exhausted', False, 21), ('alert', 'review rounds exhausted (8)')), (('enqueue', 2, 'review-address-feedback'), ('move', 'queued', 'review feedback round 2', 2)))
    """
    rounds = int(target.get("review_feedback_rounds", 0)) + 1
    MAX_ROUNDS = 8
    target_id = target["task_id"]
    if rounds > MAX_ROUNDS:
        def mut_fail_target(t):
            mut_target(t)
            t["review_feedback_rounds"] = rounds
        fail_task(
            target_id,
            "awaiting-review",
            f"review rounds exhausted ({MAX_ROUNDS})",
            blocker_code="review_feedback_exhausted",
            summary=f"review rounds exhausted ({MAX_ROUNDS})",
            detail=review_findings,
            retryable=False,
            mutator=mut_fail_target,
        )
        o._write_pr_alert(
            project_name,
            target_id,
            target.get("pr_number") or "review-feedback",
            f"review rounds exhausted ({MAX_ROUNDS})",
            target.get("pr_url"),
        )
        return

    feedback_task = o.new_task(
        role="implementer",
        engine="codex",
        project=target["project"],
        feature_id=target.get("feature_id"),
        parent_task_id=target_id,
        braid_template="review-address-feedback",
        base_branch=target.get("base_branch"),
        worktree=target.get("worktree"),
        source=f"review-feedback:{reviewer_task_id}",
        summary=f"Address reviewer findings on {target_id} (round {rounds})",
        engine_args={
            "review_findings": review_findings,
            "target_task_id": target_id,
            "round": rounds,
            "self_repair": dict((target.get("engine_args") or {}).get("self_repair") or {}),
            "evidence": (target.get("engine_args") or {}).get("evidence") or "",
            "issue_key": (target.get("engine_args") or {}).get("issue_key"),
            "issue_summary": (target.get("engine_args") or {}).get("issue_summary") or target.get("summary") or "",
        },
    )
    o.enqueue_task(feedback_task)
    def mut_requeue_target(t):
        mut_target(t)
        t["review_feedback_rounds"] = rounds
    o.move_task(
        target_id, "awaiting-review", "queued",
        reason=f"review feedback round {rounds}", mutator=mut_requeue_target,
    )


def _review_request_change_doctest():
    calls = []
    class FakeO:
        def new_task(self, **kwargs):
            return {"task_id": "task-feedback", **kwargs}
        def enqueue_task(self, task):
            calls.append(("enqueue", task["engine_args"]["round"], task["braid_template"]))
        def move_task(self, task_id, from_state, to_state, reason="", mutator=None):
            body = {"review_feedback_rounds": 0}
            if mutator:
                mutator(body)
            calls.append(("move", to_state, reason, body.get("review_feedback_rounds")))
        def _write_pr_alert(self, project_name, target_id, pr_number, reason, pr_url):
            calls.append(("alert", reason))
    old_o = _review_request_change_doctest.__globals__["o"]
    old_fail_task = _review_request_change_doctest.__globals__["fail_task"]
    def fake_fail_task(task_id, from_state, reason, **kwargs):
        body = {"review_feedback_rounds": 0}
        if kwargs.get("mutator"):
            kwargs["mutator"](body)
        calls.append(("fail", kwargs.get("blocker_code"), kwargs.get("retryable"), body.get("review_feedback_rounds")))
    _review_request_change_doctest.__globals__["o"] = FakeO()
    _review_request_change_doctest.__globals__["fail_task"] = fake_fail_task
    try:
        _handle_review_request_change("reviewer-1", "demo", {"task_id": "task-a", "project": "demo", "feature_id": "f1", "base_branch": "main", "worktree": "/tmp/wt", "review_feedback_rounds": 20}, "need tests", lambda t: t.update({"reviewed_by": "reviewer-1"}))
        exhausted = (calls[0], calls[1])
        calls.clear()
        _handle_review_request_change("reviewer-2", "demo", {"task_id": "task-b", "project": "demo", "feature_id": "f2", "base_branch": "main", "worktree": "/tmp/wt", "review_feedback_rounds": 1}, "need docs", lambda t: t.update({"reviewed_by": "reviewer-2"}))
        retried = (calls[0], calls[1])
        return (exhausted, retried)
    finally:
        _review_request_change_doctest.__globals__["o"] = old_o
        _review_request_change_doctest.__globals__["fail_task"] = old_fail_task


def _complete_review_feedback_target(target_id, target_state, *, task_id, round_no, info):
    """Return the target to awaiting-review, tolerating stale from-state races.

    >>> calls = []
    >>> old = {
    ...     "move_task": _complete_review_feedback_target.__globals__["o"].move_task,
    ...     "find_task": _complete_review_feedback_target.__globals__["o"].find_task,
    ...     "append_event": _complete_review_feedback_target.__globals__["o"].append_event,
    ... }
    >>> def fake_move(task_id_arg, from_state, to_state, reason="", mutator=None):
    ...     calls.append(("move", task_id_arg, from_state, to_state, reason))
    ...     if from_state == "awaiting-review":
    ...         raise FileNotFoundError("stale")
    >>> _complete_review_feedback_target.__globals__["o"].move_task = fake_move
    >>> _complete_review_feedback_target.__globals__["o"].find_task = lambda tid, states=(): ("claimed", {"task_id": tid})
    >>> _complete_review_feedback_target.__globals__["o"].append_event = lambda *args, **kwargs: calls.append(("event", args, kwargs["details"]["state"]))
    >>> _complete_review_feedback_target("task-1", "awaiting-review", task_id="feedback-1", round_no=2, info="abc123")
    'claimed'
    >>> calls
    [('move', 'task-1', 'awaiting-review', 'awaiting-review', 'review feedback applied (feedback-1)'), ('event', ('review-feedback', 'target_state_changed'), 'claimed')]
    >>> for key, value in old.items():
    ...     setattr(_complete_review_feedback_target.__globals__["o"], key, value)
    """
    def mut_target(t):
        t["finished_at"] = o.now_iso()
        t["review_feedback_rounds"] = int(round_no or t.get("review_feedback_rounds", 0))
        if info not in ("", "clean"):
            t["artifacts"] = t.get("artifacts", []) + [info]

    try:
        o.move_task(target_id, target_state, "awaiting-review",
                    reason=f"review feedback applied ({task_id})", mutator=mut_target)
        return "awaiting-review"
    except FileNotFoundError:
        current = o.find_task(
            target_id,
            states=("queued", "claimed", "running", "awaiting-review", "awaiting-qa", "done", "failed", "blocked", "abandoned"),
        )
        if not current:
            raise
        current_state, _ = current
        if current_state in ("queued", "awaiting-review", "awaiting-qa"):
            o.move_task(target_id, current_state, "awaiting-review",
                        reason=f"review feedback applied ({task_id})", mutator=mut_target)
            return "awaiting-review"
        o.append_event(
            "review-feedback",
            "target_state_changed",
            task_id=task_id,
            details={"target_task_id": target_id, "state": current_state},
        )
        return current_state


def run_review_feedback_task(task, cfg, timeout, log_path):
    task_id = task["task_id"]
    eargs = task.get("engine_args") or {}
    target_id = eargs.get("target_task_id")
    round_no = eargs.get("round")
    review_findings = eargs.get("review_findings", "")
    bt = task.get("braid_template") or "review-address-feedback"

    if not target_id:
        fail_task(task_id, "claimed", "review-feedback: missing target_task_id",
                  blocker_code="runtime_precondition_failed", summary="review-feedback missing target_task_id", retryable=False)
        return

    found = o.find_task(target_id, states=("queued", "awaiting-review", "awaiting-qa", "done", "failed"))
    if not found:
        feature = o.read_feature(task.get("feature_id")) if task.get("feature_id") else None
        if (
            (feature and feature.get("status") == "finalizing")
            or _newer_review_feedback_exists(task.get("feature_id"), target_id, task_id, task.get("created_at"))
        ):
            o.move_task(
                task_id,
                "claimed",
                "abandoned",
                reason=f"review-feedback superseded for {target_id}",
                mutator=lambda t: t.update({"finished_at": o.now_iso(), "abandoned_reason": f"superseded target {target_id}"}),
            )
            return
        if _self_repair_enabled(task):
            _self_repair_reopen_current_issue(
                task,
                blocker_code="qa_target_missing",
                reason=f"review-feedback target missing: {target_id}",
                deliberation={
                    "stage": "blocker_verifier",
                    "verdict": "replan",
                    "chosen_strategy": "reopen_issue",
                    "reason": f"review-feedback target missing: {target_id}",
                    "task_id": task_id,
                },
                summary_suffix="target-missing",
            )
            o.move_task(
                task_id,
                "claimed",
                "abandoned",
                reason=f"review-feedback target missing: {target_id}",
                mutator=lambda t: t.update({"finished_at": o.now_iso()}),
            )
            o.tick_self_repair_queue()
            return
        fail_task(task_id, "claimed", f"review-feedback: target {target_id} not found in queue",
                  blocker_code="qa_target_missing", summary="review-feedback target missing", retryable=True)
        return
    target_state, target = found

    wt_str = task.get("worktree") or target.get("worktree")
    if not wt_str or not pathlib.Path(wt_str).exists():
        feature = o.read_feature(task.get("feature_id")) if task.get("feature_id") else None
        if (
            (feature and feature.get("status") == "finalizing")
            or _newer_review_feedback_exists(task.get("feature_id"), target_id, task_id, task.get("created_at"))
        ):
            o.move_task(
                task_id,
                "claimed",
                "abandoned",
                reason=f"review-feedback stale worktree for {target_id}",
                mutator=lambda t: t.update({"finished_at": o.now_iso(), "abandoned_reason": f"stale worktree for {target_id}"}),
            )
            return
        if _self_repair_enabled(task):
            _self_repair_reopen_current_issue(
                task,
                blocker_code="qa_target_missing",
                reason=f"review-feedback target worktree missing: {wt_str}",
                deliberation={
                    "stage": "blocker_verifier",
                    "verdict": "replan",
                    "chosen_strategy": "reopen_issue",
                    "reason": f"review-feedback target worktree missing: {wt_str}",
                    "task_id": task_id,
                },
                summary_suffix="worktree-missing",
            )
            o.move_task(
                task_id,
                "claimed",
                "abandoned",
                reason=f"review-feedback target worktree missing: {wt_str}",
                mutator=lambda t: t.update({"finished_at": o.now_iso()}),
            )
            o.tick_self_repair_queue()
            return
        fail_task(task_id, "claimed", f"review-feedback: target worktree missing: {wt_str}",
                  blocker_code="qa_target_missing", summary="review-feedback target worktree missing", retryable=True)
        return
    wt = pathlib.Path(wt_str)
    base_branch = task.get("base_branch") or target.get("base_branch") or base_branch_for_task(target)

    try:
        project = o.get_project(cfg, task["project"])
    except KeyError:
        fail_task(task_id, "claimed", "review-feedback: unknown project",
                  blocker_code="runtime_unknown_project", summary="review-feedback unknown project", retryable=False)
        return

    graph_body, graph_hash = o.braid_template_load(bt)
    if graph_body is None:
        if task.get("braid_generate_if_missing", True):
            regen = o.new_task(
                role="planner", engine="claude",
                project=project["name"],
                summary=f"Generate BRAID template for {bt}",
                source=f"regen-for:{task_id}",
                braid_template=bt,
                engine_args={"mode": "template-gen"},
            )
            o.enqueue_task(regen)
            def mut_block(t):
                t["topology_error"] = "template_missing"
            o.move_task(task_id, "claimed", "blocked",
                        reason="template missing", mutator=mut_block)
            return
        fail_task(task_id, "claimed", "template missing, regen disabled",
                  blocker_code="template_missing", summary="template missing", retryable=False)
        return

    lock_fh = None
    try:
        lock_fh = acquire_lock(f"{project['name']}.lock", mode="shared", timeout_sec=60)
        memory_ctx = read_memory_context(project["name"], project["path"])
        diff_text = _git(wt, "diff", base_branch).stdout or ""
        if len(diff_text) > 30000:
            diff_text = diff_text[:30000] + "\n\n[...diff truncated at 30k chars...]\n"
        prompt = (
            "[BRAID REASONING GRAPH — traverse deterministically. If you cannot, "
            "emit exactly one line `BRAID_TOPOLOGY_ERROR: <reason>` as the final line and stop. "
            "If traversal is locally underspecified but salvageable, emit "
            "`BRAID_REFINE: <node-id>: <missing-edge-condition>` as the final line and stop.]\n\n"
            f"{graph_body}\n\n"
            "[END BRAID GRAPH]\n\n"
            f"[TARGET TASK]\n{target_id}\n\n"
            f"[REVIEW FEEDBACK TO ADDRESS]\n{review_findings}\n\n"
            f"[CURRENT DIFF vs {base_branch}]\n{diff_text or f'(empty diff vs {base_branch})'}\n\n"
            f"[PROJECT MEMORY]\n{memory_ctx}\n\n"
            "[OUTPUT CONTRACT]\n"
            "Emit exactly one of these as the final line of your response:\n"
            "  BRAID_OK: <one-line summary of what you changed>\n"
            "  BRAID_REFINE: <node-id>: <missing-edge-condition>\n"
            "  BRAID_TOPOLOGY_ERROR: <reason the graph could not be traversed>\n"
        )

        last_msg_path = o.LOGS_DIR / f"{task_id}.last.txt"
        def mut_running(t):
            t["braid_template_path"] = f"braid/templates/{bt}.mmd"
            t["braid_template_hash"] = graph_hash
            t["worktree"] = str(wt)
            t["base_branch"] = base_branch
            t["started_at"] = o.now_iso()
            t["log_path"] = str(log_path)
        o.move_task(task_id, "claimed", "running",
                    reason=f"review-feedback for {target_id}", mutator=mut_running)

        cmd = [
            "codex", "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C", str(wt),
            "--ephemeral",
            "--json",
            "-o", str(last_msg_path),
            prompt,
        ]

        with pathlib.Path(log_path).open("w") as logf:
            logf.write(f"# review-feedback task={task_id} target={target_id} round={round_no}\n")
            logf.write(f"# template={bt} hash={graph_hash}\n")
            logf.write(f"# worktree: {wt}\n\n---\n")
            logf.flush()
            try:
                proc = _run_bounded(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                def mut_fail(t):
                    t["finished_at"] = o.now_iso()
                    t["failure"] = f"codex timeout {timeout}s"
                fail_task(task_id, "running", f"review-feedback timeout {timeout}s",
                          blocker_code="llm_timeout", summary="review-feedback timeout", retryable=True, mutator=mut_fail)
                return
            if proc.stderr:
                logf.write(proc.stderr)
            if proc.stdout:
                logf.write(proc.stdout)
        _record_task_costs_from_text(task_id, "codex", "gpt-5.4", proc.stdout or "")

        trailer = ""
        if last_msg_path.exists():
            lines = [l.strip() for l in last_msg_path.read_text().splitlines() if l.strip()]
            trailer = _find_braid_trailer(lines)

        if trailer.startswith("BRAID_REFINE"):
            enqueue_braid_refine(task, project["name"], trailer, from_state="running")
            return

        if trailer.startswith("BRAID_TOPOLOGY_ERROR"):
            if _self_repair_enabled(task):
                verifier = _run_self_repair_blocker_verifier(
                    task,
                    project,
                    wt,
                    timeout,
                    "template_graph_error",
                    trailer,
                    memory_ctx,
                )
                _self_repair_reopen_current_issue(
                    task,
                    blocker_code="template_graph_error",
                    reason=verifier.get("reason") or "review-feedback topology error",
                    deliberation={**verifier, "stage": "blocker_verifier", "task_id": task_id},
                    summary_suffix="review-feedback-topology",
                )
                def mut_abandon(t):
                    t["finished_at"] = o.now_iso()
                    t["topology_error"] = trailer
                    t["self_repair_verifier"] = verifier
                o.move_task(task_id, "running", "abandoned",
                            reason=verifier.get("reason") or trailer[:80], mutator=mut_abandon)
                o.tick_self_repair_queue()
                return
            o.braid_template_record_use(bt, topology_error=True)
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
                t["topology_error"] = trailer
                o.set_task_blocker(
                    t,
                    "template_graph_error",
                    summary="review-feedback topology error",
                    detail=trailer,
                    source="worker",
                    retryable=True,
                )
            fail_task(task_id, "running", trailer[:80],
                      blocker_code="template_graph_error", summary="review-feedback topology error",
                      retryable=True, mutator=mut_fail)
            return

        if not trailer.startswith("BRAID_OK"):
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
            fail_task(task_id, "running", f"review-feedback no BRAID trailer (exit {proc.returncode})",
                      blocker_code="model_output_invalid", summary="review-feedback trailer missing",
                      retryable=False, mutator=mut_fail)
            return

        o.braid_template_record_use(bt, topology_error=False)
        ok, info = _autocommit_worktree(pathlib.Path(wt), target, log_path)
        if not ok:
            if "repo-memory secret-scan hit" in info:
                write_repo_memory_secret_alert(
                    project["name"],
                    task_id,
                    info,
                    _repo_memory_secret_hits(pathlib.Path(wt)),
                    log_path=str(log_path),
                )
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
                t["failure"] = f"auto-commit: {info}"
            fail_task(task_id, "running", f"auto-commit: {info}",
                      blocker_code="auto_commit_failed", summary="auto-commit failed",
                      retryable=True, mutator=mut_fail)
            return

        _complete_review_feedback_target(
            target_id,
            target_state,
            task_id=task_id,
            round_no=round_no,
            info=info,
        )

        def mut_done(t):
            t["finished_at"] = o.now_iso()
            t["review_feedback_commit"] = info
        o.move_task(task_id, "running", "done",
                    reason=f"review-feedback finished for {target_id}", mutator=mut_done)
    finally:
        if lock_fh is not None:
            lock_fh.close()


# --- pr-feedback handler ----------------------------------------------------
#
# pr-feedback is a codex-engine task created by orchestrator.pr_sweep when an
# open task PR needs maintenance — either a merge conflict with its feature
# base or new actionable review comments. The handler re-uses the target
# task's existing worktree (no fresh make_worktree), loads the
# `pr-address-feedback` BRAID graph, hands codex the comments + conflict flag
# + diff, and relies on the graph to walk codex through rebase -> fix ->
# commit. On BRAID_OK the worker re-runs smoke.sh inside the worktree and, if
# green, force-pushes the agent branch with --force-with-lease.
#
# The target task stays in queue/done/ the whole time — its pr_sweep.* fields
# track the iteration count, last-applied sha, and handled comment ids so the
# next pr-sweep tick knows what has already been addressed.

def _format_conflict_preview(conflict_preview):
    if not conflict_preview:
        return ""

    def bullets(value):
        if isinstance(value, str):
            items = [line.strip() for line in value.splitlines() if line.strip()]
        else:
            items = [str(item).strip() for item in (value or []) if str(item).strip()]
        return "\n".join(f"- {item}" for item in items) or "- (none)"

    return (
        "[CONFLICT PREVIEW]\n"
        "Our commits since base\n"
        f"{bullets(conflict_preview.get('our_commits'))}\n\n"
        "Their commits since divergence\n"
        f"{bullets(conflict_preview.get('their_commits'))}\n\n"
        "Likely conflict files\n"
        f"{bullets(conflict_preview.get('likely_conflict_files'))}\n\n"
    )


def build_pr_feedback_prompt(*, target, graph_body, base_branch, conflicts, comments, pr_number, check_failures=None, conflict_preview=None):
    """Build the codex prompt for a pr-feedback task.

    >>> prompt = build_pr_feedback_prompt(
    ...     target={"task_id": "task-1", "summary": "Demo"},
    ...     graph_body="flowchart TD",
    ...     base_branch="feature/demo",
    ...     conflicts=True,
    ...     comments=[],
    ...     pr_number=12,
    ...     conflict_preview={"our_commits": "abc", "their_commits": "def", "likely_conflict_files": ["a.py", "b.py"]},
    ... )
    >>> "[CONFLICT PREVIEW]" in prompt and "Likely conflict files" in prompt
    True
    >>> prompt2 = build_pr_feedback_prompt(
    ...     target={"task_id": "task-1", "summary": "Demo"},
    ...     graph_body="flowchart TD",
    ...     base_branch="feature/demo",
    ...     conflicts=False,
    ...     comments=[],
    ...     pr_number=12,
    ...     conflict_preview=None,
    ... )
    >>> "[CONFLICT PREVIEW]" in prompt2
    False
    """
    comment_blocks = []
    for i, c in enumerate(comments or [], 1):
        comment_blocks.append(
            f"### Comment {i} by @{c.get('author','?')} at {c.get('created_at','?')}\n"
            f"path={c.get('path','?')} line={c.get('line','?')} thread_id={c.get('thread_id','?')}\n"
            f"{c.get('body','').strip()}"
        )
    comments_text = "\n\n".join(comment_blocks) or "(no review comments — rebase-only run)"
    check_blocks = []
    for i, c in enumerate(check_failures or [], 1):
        check_blocks.append(
            f"### Failed check {i}: {c.get('name','check')} [{c.get('conclusion','UNKNOWN')}]\n"
            f"{c.get('details_url','(no details url)')}"
        )
    checks_text = "\n\n".join(check_blocks) or "(no failed checks captured)"
    conflict_preview_text = _format_conflict_preview(conflict_preview)

    header = (
        f"PR #{pr_number} for target {target.get('task_id')} needs maintenance.\n"
        f"Original summary: {target.get('summary','(no summary)')}\n"
        f"Base branch: {base_branch}\n"
        f"Conflicts with base: {conflicts}\n"
    )

    return (
        "[BRAID REASONING GRAPH — traverse deterministically. If you cannot, "
        "emit exactly one line `BRAID_TOPOLOGY_ERROR: <reason>` as the final line and stop. "
        "If traversal is locally underspecified but salvageable, emit "
        "`BRAID_REFINE: <node-id>: <missing-edge-condition>` as the final line and stop.]\n\n"
        f"{graph_body}\n\n"
        "[END BRAID GRAPH]\n\n"
        f"[PR FEEDBACK CONTEXT]\n{header}\n\n"
        f"{conflict_preview_text}"
        "[REVIEW COMMENTS TO ADDRESS]\n"
        f"{comments_text}\n\n"
        "[FAILED CHECKS TO REPAIR]\n"
        f"{checks_text}\n\n"
        "[ACTIONS YOU CAN TAKE]\n"
        f"- You are inside the target worktree on branch agent/{target.get('task_id')}.\n"
        f"- The base branch `{base_branch}` has already been fetched from origin.\n"
        f"- If there are conflicts, rebase onto origin/{base_branch} and resolve them.\n"
        "- Apply fixes requested by the review comments.\n"
        "- Treat each review comment as unresolved until the offending condition is actually gone in the current branch head.\n"
        "- If a failed check points at workflow or CI breakage, repair the underlying cause in the branch.\n"
        "- Commit your changes using the existing agent git identity if needed.\n"
        "- Do NOT push — worker.py will re-run smoke and push with --force-with-lease "
        "after it validates your work.\n\n"
        "[OUTPUT CONTRACT]\n"
        "Emit exactly one of these as the final line of your response:\n"
        "  BRAID_OK: <one-line summary of what you did>\n"
        "  BRAID_REFINE: <node-id>: <missing-edge-condition>\n"
        "  BRAID_TOPOLOGY_ERROR: <reason the graph could not be traversed>\n"
    )


def build_feature_pr_feedback_prompt(*, task, graph_body, comments, pr_number, conflicts, check_failures=None):
    """Build the codex prompt for a feature->main PR follow-up slice.

    The worktree is rooted on `feature/<id>` and will later open a normal
    task PR back into that feature branch, so the prompt is comment-focused
    rather than instructing a direct rebase onto `main`.
    """
    comment_blocks = []
    for i, c in enumerate(comments or [], 1):
        comment_blocks.append(
            f"### Comment {i} by @{c.get('author','?')} at {c.get('created_at','?')}\n"
            f"path={c.get('path','?')} line={c.get('line','?')} thread_id={c.get('thread_id','?')}\n"
            f"{c.get('body','').strip()}"
        )
    comments_text = "\n\n".join(comment_blocks) or "(no review comments — conflict-only run)"
    check_blocks = []
    for i, c in enumerate(check_failures or [], 1):
        check_blocks.append(
            f"### Failed check {i}: {c.get('name','check')} [{c.get('conclusion','UNKNOWN')}]\n"
            f"{c.get('details_url','(no details url)')}"
        )
    checks_text = "\n\n".join(check_blocks) or "(no failed checks captured)"
    return (
        "[BRAID REASONING GRAPH — traverse deterministically. If you cannot, "
        "emit exactly one line `BRAID_TOPOLOGY_ERROR: <reason>` as the final line and stop. "
        "If traversal is locally underspecified but salvageable, emit "
        "`BRAID_REFINE: <node-id>: <missing-edge-condition>` as the final line and stop.]\n\n"
        f"{graph_body}\n\n"
        "[END BRAID GRAPH]\n\n"
        f"[FEATURE PR FEEDBACK CONTEXT]\n"
        f"Feature PR #{pr_number} for feature {task.get('feature_id')} needs maintenance.\n"
        f"Original summary: {task.get('summary','(no summary)')}\n"
        "Target branch of the final PR: main\n"
        f"Conflicts with main: {conflicts}\n\n"
        "[REVIEW COMMENTS TO ADDRESS]\n"
        f"{comments_text}\n\n"
        "[FAILED CHECKS TO REPAIR]\n"
        f"{checks_text}\n\n"
        "[ACTIONS YOU CAN TAKE]\n"
        f"- You are inside a fresh worktree on branch agent/{task.get('task_id')} based on feature/{task.get('feature_id')}.\n"
        "- Fetch `origin/main` before changing code.\n"
        "- If there are conflicts, merge `origin/main` into your current branch and resolve them.\n"
        "- Do NOT rebase this branch onto `main`; the follow-up PR must still target the feature branch.\n"
        "- Apply fixes requested by the feature PR review comments.\n"
        "- Treat each review comment as unresolved until the offending condition is actually gone in the current branch head.\n"
        "- If a failed check points at workflow or CI breakage, repair the underlying cause in the feature branch.\n"
        "- Commit your changes using the existing agent git identity if needed.\n\n"
        "[OUTPUT CONTRACT]\n"
        "Emit exactly one of these as the final line of your response:\n"
        "  BRAID_OK: <one-line summary of what you did>\n"
        "  BRAID_REFINE: <node-id>: <missing-edge-condition>\n"
        "  BRAID_TOPOLOGY_ERROR: <reason the graph could not be traversed>\n"
    )


def run_pr_feedback_task(task, cfg):
    task_id = task["task_id"]
    eargs = task.get("engine_args") or {}
    target_id = eargs.get("target_task_id")
    pr_number = eargs.get("pr_number")
    conflicts = bool(eargs.get("conflicts", False))
    comments = eargs.get("comments") or []
    base_branch = eargs.get("base_branch") or "main"
    conflict_preview = eargs.get("conflict_preview")

    log_path = o.LOGS_DIR / f"{task_id}.log"
    o.LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if not target_id:
        fail_task(task_id, "claimed", "pr-feedback: missing target_task_id",
                  blocker_code="runtime_precondition_failed", summary="pr-feedback missing target_task_id", retryable=False)
        return

    target_file = o.queue_dir("done") / f"{target_id}.json"
    target = o.read_json(target_file, None)
    if target is None:
        fail_task(task_id, "claimed", f"pr-feedback: target {target_id} not in done/",
                  blocker_code="qa_target_missing", summary="pr-feedback target missing", retryable=False)
        return

    wt_str = target.get("worktree")
    if not wt_str or not pathlib.Path(wt_str).exists():
        fail_task(task_id, "claimed", f"pr-feedback: target worktree missing: {wt_str}",
                  blocker_code="qa_target_missing", summary="pr-feedback target worktree missing", retryable=False)
        return
    wt = pathlib.Path(wt_str)
    agent_branch = f"agent/{target_id}"

    try:
        project = o.get_project(cfg, task["project"])
    except KeyError:
        fail_task(task_id, "claimed", "pr-feedback: unknown project",
                  blocker_code="runtime_unknown_project", summary="pr-feedback unknown project", retryable=False)
        return

    bt = task.get("braid_template") or "pr-address-feedback"
    graph_body, graph_hash = o.braid_template_load(bt)
    if graph_body is None:
        if task.get("braid_generate_if_missing", True):
            regen = o.new_task(
                role="planner", engine="claude",
                project=project["name"],
                summary=f"Generate BRAID template for {bt}",
                source=f"regen-for:{task_id}",
                braid_template=bt,
                engine_args={"mode": "template-gen"},
            )
            o.enqueue_task(regen)
            def mut_block(t):
                t["topology_error"] = "template_missing"
            o.move_task(task_id, "claimed", "blocked",
                        reason="template missing", mutator=mut_block)
            return
        fail_task(task_id, "claimed", "template missing, regen disabled",
                  blocker_code="template_missing", summary="template missing", retryable=False)
        return

    lock_fh = None
    try:
        lock_fh = acquire_lock(f"{project['name']}.lock", mode="shared", timeout_sec=60)

        # Fetch latest base branch so codex sees a fresh tip when it rebases.
        _git(wt, "fetch", "origin", base_branch, timeout=120)

        prompt = build_pr_feedback_prompt(
            target=target, graph_body=graph_body,
            base_branch=base_branch, conflicts=conflicts, comments=comments,
            pr_number=pr_number,
            check_failures=eargs.get("check_failures") or [],
            conflict_preview=conflict_preview,
        )

        timeout = eargs.get(
            "timeout_sec",
            cfg.get("slots", {}).get("codex", {}).get("timeout_sec", DEFAULT_TIMEOUTS["codex"]),
        )

        last_msg_path = o.LOGS_DIR / f"{task_id}.last.txt"

        def mut_running(t):
            t["braid_template_path"] = f"braid/templates/{bt}.mmd"
            t["braid_template_hash"] = graph_hash
            t["worktree"] = str(wt)
            t["base_branch"] = base_branch
            t["started_at"] = o.now_iso()
            t["log_path"] = str(log_path)
        o.move_task(task_id, "claimed", "running",
                    reason=f"pr-feedback for {target_id}", mutator=mut_running)

        cmd = [
            "codex", "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C", str(wt),
            "--ephemeral",
            "--json",
            "-o", str(last_msg_path),
            prompt,
        ]

        with log_path.open("w") as logf:
            logf.write(f"# pr-feedback task={task_id} target={target_id} pr=#{pr_number}\n")
            logf.write(f"# template={bt} hash={graph_hash}\n")
            logf.write(f"# base_branch={base_branch} conflicts={conflicts}\n")
            logf.write(f"# comments={len(comments)}\n")
            logf.write(f"# worktree: {wt}\n\n---\n")
            logf.flush()
            try:
                proc = _run_bounded(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                def mut_fail(t):
                    t["finished_at"] = o.now_iso()
                    t["failure"] = f"codex timeout {timeout}s"
                fail_task(task_id, "running", f"pr-feedback timeout {timeout}s",
                          blocker_code="llm_timeout", summary="pr-feedback timeout", retryable=True, mutator=mut_fail)
                return
            if proc.stderr:
                logf.write(proc.stderr)
            if proc.stdout:
                logf.write(proc.stdout)
        _record_task_costs_from_text(task_id, "codex", "gpt-5.4", proc.stdout or "")

        trailer = ""
        if last_msg_path.exists():
            lines = [l.strip() for l in last_msg_path.read_text().splitlines() if l.strip()]
            trailer = _find_braid_trailer(lines)

        if trailer.startswith("BRAID_REFINE"):
            enqueue_braid_refine(task, project["name"], trailer, from_state="running")
            return

        if trailer.startswith("BRAID_TOPOLOGY_ERROR"):
            if not topology_reason_is_valid(trailer):
                def mut_false(t):
                    t["finished_at"] = o.now_iso()
                    t["false_blocker_claim"] = trailer
                fail_task(task_id, "running", f"false blocker claim: {trailer[:80]}",
                          blocker_code="false_blocker_claim", summary="false blocker claim",
                          retryable=False, mutator=mut_false)
                return
            if topology_reason_code(trailer) == "baseline_red":
                def mut_fail(t):
                    t["finished_at"] = o.now_iso()
                    t["failure"] = trailer
                    o.set_task_blocker(
                        t,
                        "qa_smoke_failed",
                        summary="pr-feedback baseline smoke red",
                        detail=trailer,
                        source="worker",
                        retryable=True,
                    )
                fail_task(task_id, "running", "pr-feedback baseline smoke red",
                          blocker_code="qa_smoke_failed", summary="pr-feedback baseline smoke red",
                          retryable=True, mutator=mut_fail)
                return
            o.braid_template_record_use(bt, topology_error=True)
            regen = o.new_task(
                role="planner", engine="claude",
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
                o.set_task_blocker(
                    t,
                    "template_graph_error",
                    summary="pr-feedback topology error",
                    detail=trailer,
                    source="worker",
                    retryable=True,
                )
            o.move_task(task_id, "running", "blocked",
                        reason=trailer[:80], mutator=mut_block)
            return

        if not trailer.startswith("BRAID_OK"):
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
            fail_task(task_id, "running", f"pr-feedback no BRAID trailer (exit {proc.returncode})",
                      blocker_code="model_output_invalid", summary="pr-feedback trailer missing",
                      retryable=False, mutator=mut_fail)
            return

        # Re-run smoke inside the worktree. Same invocation shape as run_qa_slot:
        # bash <canonical-smoke.sh> with REPO_ROOT=<worktree> so we use the latest
        # smoke script against the worktree's code.
        qa_cfg = project.get("qa") or {}
        smoke_rel = qa_cfg.get("smoke")
        if not smoke_rel:
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
                t["failure"] = "no qa.smoke configured"
            fail_task(task_id, "running", "no qa.smoke",
                      blocker_code="qa_contract_error", summary="QA smoke contract missing",
                      retryable=False, mutator=mut_fail)
            return

        smoke_abs = pathlib.Path(project["path"]) / smoke_rel
        smoke_env = os.environ.copy()
        smoke_env["REPO_ROOT"] = str(wt)
        with log_path.open("a") as logf:
            logf.write(f"\n# pr-feedback smoke re-run: {smoke_abs}\n")
            logf.write(f"# REPO_ROOT={wt}\n\n")
            logf.flush()
            try:
                smoke_proc = _run_bounded(
                    ["bash", str(smoke_abs)],
                    cwd=project["path"],
                    env=smoke_env,
                    stdout=logf, stderr=subprocess.STDOUT, text=True,
                    timeout=DEFAULT_TIMEOUTS["qa"],
                )
            except subprocess.TimeoutExpired:
                def mut_fail(t):
                    t["finished_at"] = o.now_iso()
                    t["failure"] = "smoke re-run timeout"
                fail_task(task_id, "running", "smoke re-run timeout",
                          blocker_code="qa_smoke_failed", summary="smoke re-run timeout",
                          retryable=True, mutator=mut_fail)
                return

        if smoke_proc.returncode != 0:
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
                t["failure"] = f"smoke re-run exit {smoke_proc.returncode}"
            fail_task(task_id, "running", "pr-feedback smoke red",
                      blocker_code="qa_smoke_failed", summary="pr-feedback smoke red",
                      retryable=True, mutator=mut_fail)
            return

        # Smoke green — force-push with lease so upstream gets the fix.
        push = _git_agent(
            wt, "push", "--force-with-lease", "origin", agent_branch, timeout=120,
        )
        if push.returncode != 0:
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
                t["failure"] = f"pr-feedback push failed: {push.stderr.strip()[:200]}"
            fail_task(task_id, "running", "pr-feedback push failed",
                      blocker_code="delivery_push_failed", summary="pr-feedback push failed",
                      retryable=True, mutator=mut_fail)
            return

        new_sha = (_git(wt, "rev-parse", "HEAD").stdout or "").strip() or None

        def mut_target(t):
            s = dict(t.get("pr_sweep") or {})
            s["last_feedback_at"] = o.now_iso()
            s["last_feedback_sha"] = new_sha
            s["last_feedback_task_id"] = task_id
            t["pr_sweep"] = s
            if new_sha:
                t["push_commit_sha"] = new_sha
        o.update_task_in_place(target_file, mut_target)

        o.braid_template_record_use(bt, topology_error=False)

        # Self-heal close-out: mark any bot review threads this feedback
        # round addressed as resolved, so the next pr-sweep tick sees
        # isResolved=true on the PR and auto-merges. chatgpt-codex-
        # connector does NOT mark its own threads resolved after a fix —
        # if the orchestrator does not do it, the PR stays blocked in
        # Case 3.5 forever. Failures are logged but not fatal: the fix
        # is already pushed, and a stuck unresolved thread is a re-check,
        # not a regression.
        eligible_thread_comments = _eligible_thread_comments_for_auto_resolution(wt, comments or [])
        thread_ids = [c.get("thread_id") for c in eligible_thread_comments if c.get("thread_id")]
        resolved_count = 0
        resolution_evidence = [
            {
                "thread_id": c.get("thread_id"),
                "comment_id": c.get("id"),
                "path": c.get("path"),
                "line": c.get("line"),
                "evidence": c.get("resolution_evidence"),
            }
            for c in eligible_thread_comments
        ]
        if thread_ids:
            resolved_count, failed_count = o._resolve_review_threads(
                project["path"], thread_ids,
            )
            with log_path.open("a") as lf:
                lf.write(
                    f"\n# resolveReviewThread: {resolved_count} resolved, "
                    f"{failed_count} failed ({len(set(thread_ids))} unique thread(s))\n"
                )
        elif comments:
            with log_path.open("a") as lf:
                lf.write("\n# resolveReviewThread: skipped; no thread comment passed local resolution checks\n")

        review_ok = review_reason = None
        if pr_number:
            review_ok, review_reason = o._request_codex_review(
                project["path"],
                pr_number,
                reason="autonomous PR feedback fix",
                commit_sha=new_sha,
            )

        def mut_ok(t):
            t["finished_at"] = o.now_iso()
            t["pr_feedback_new_sha"] = new_sha
            if thread_ids:
                t["resolved_thread_count"] = resolved_count
                t["resolved_thread_evidence"] = resolution_evidence
            if pr_number:
                if review_ok:
                    t["review_requested_at"] = o.now_iso()
                    t["review_request_error"] = None
                else:
                    t["review_request_error"] = review_reason
        o.move_task(task_id, "running", "done",
                    reason=f"pr-feedback pushed {new_sha}", mutator=mut_ok)

    finally:
        if lock_fh is not None:
            lock_fh.close()


# --- codex slot -------------------------------------------------------------

def run_codex_slot(task, cfg):
    task_id = task["task_id"]
    bt = task.get("braid_template")

    # pr-feedback mode: re-use an existing target worktree to address review
    # comments and/or rebase, then re-run smoke and force-push. Distinct from
    # the normal "agent implements a slice from scratch" path because it
    # operates on a task that is already merged-waiting in done/.
    mode = (task.get("engine_args") or {}).get("mode")
    if mode == "pr-feedback":
        return run_pr_feedback_task(task, cfg)
    if bt == "review-address-feedback":
        timeout = task.get("engine_args", {}).get(
            "timeout_sec",
            cfg.get("slots", {}).get("codex", {}).get("timeout_sec", DEFAULT_TIMEOUTS["codex"]),
        )
        log_path = o.LOGS_DIR / f"{task_id}.log"
        o.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        return run_review_feedback_task(task, cfg, timeout, log_path)

    if not bt:
        fail_task(task_id, "claimed", "codex task has no braid_template",
                  blocker_code="runtime_precondition_failed", summary="codex task missing braid_template", retryable=False)
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
                o.set_task_blocker(
                    t,
                    "template_missing",
                    summary="BRAID template missing",
                    detail=bt,
                    source="worker",
                    retryable=True,
                )
            o.move_task(task_id, "claimed", "blocked", reason="template missing", mutator=mut)
            return
        fail_task(task_id, "claimed", "template missing, regen disabled",
                  blocker_code="template_missing", summary="template missing", retryable=False)
        return

    # Load project + create worktree.
    try:
        project = o.get_project(cfg, task["project"])
    except KeyError:
        fail_task(task_id, "claimed", "unknown project",
                  blocker_code="runtime_unknown_project", summary="unknown project", retryable=False)
        return

    lock_fh = None
    wt_path = None
    branch = None
    base_branch = "main"
    self_repair_meta = dict((task.get("engine_args") or {}).get("self_repair") or {})
    try:
        # Shared lock to coexist with other codex workers; blocks against regression exclusive.
        lock_fh = acquire_lock(f"{project['name']}.lock", mode="shared", timeout_sec=60)

        base_branch = base_branch_for_task(task)
        if base_branch != "main":
            try:
                ensure_feature_branch(
                    project["path"],
                    base_branch,
                    allow_dirty_main=bool(self_repair_meta.get("enabled")),
                )
            except MainDirtyOrAhead as exc:
                exc_text = str(exc)
                blocker_code = "runtime_env_dirty" if "fetch origin main failed" in exc_text.lower() else "project_main_dirty"
                blocker_summary = (
                    "feature branch refresh failed"
                    if blocker_code == "runtime_env_dirty"
                    else "project main checkout dirty or ahead"
                )
                def mut_block_main(t):
                    t["finished_at"] = o.now_iso()
                    t["topology_error"] = "main_dirty_or_ahead"
                    t["topology_error_message"] = exc_text
                    o.set_task_blocker(
                        t,
                        blocker_code,
                        summary=blocker_summary,
                        detail=exc_text,
                        source="worker",
                        retryable=True,
                    )
                o.move_task(
                    task_id,
                    "claimed",
                    "blocked",
                    reason=exc_text[:80],
                    mutator=mut_block_main,
                )
                try:
                    write_regression_alert(
                        project["name"],
                        task_id,
                        f"main_dirty_or_ahead: {exc_text}",
                        None,
                    )
                except Exception as alert_exc:
                    log(f"main_dirty_or_ahead alert write failed: {alert_exc}")
                return

        wt_path, branch, base_branch = make_worktree(
            project["path"], task_id, base_branch=base_branch,
        )

        memory_ctx = read_memory_context(project["name"], project["path"])
        if mode == "feature-pr-feedback":
            prompt = build_feature_pr_feedback_prompt(
                task=task,
                graph_body=graph_body,
                comments=(task.get("engine_args") or {}).get("comments") or [],
                pr_number=(task.get("engine_args") or {}).get("feature_pr_number"),
                conflicts=bool((task.get("engine_args") or {}).get("conflicts", False)),
                check_failures=(task.get("engine_args") or {}).get("check_failures") or [],
            )
        else:
            prompt = build_codex_prompt(task, graph_body, memory_ctx)

        timeout = task.get("engine_args", {}).get(
            "timeout_sec",
            cfg.get("slots", {}).get("codex", {}).get("timeout_sec", DEFAULT_TIMEOUTS["codex"]),
        )

        if self_repair_meta.get("enabled"):
            pre_execute = _run_self_repair_pre_execute_council(
                task,
                project,
                wt_path,
                timeout,
                memory_ctx,
                graph_body,
            )
            if pre_execute.get("error"):
                _self_repair_reopen_current_issue(
                    task,
                    blocker_code=pre_execute.get("blocker_code") or "llm_exit_error",
                    reason=pre_execute["error"],
                    deliberation={
                        "stage": "pre_execute",
                        "verdict": "replan",
                        "chosen_strategy": "reopen_issue",
                        "reason": pre_execute["error"],
                        "task_id": task_id,
                    },
                    summary_suffix="pre-execute-error",
                )
                o.move_task(
                    task_id,
                    "claimed",
                    "abandoned",
                    reason="self-repair pre-execution council failed",
                    mutator=lambda t: t.update({"finished_at": o.now_iso(), "self_repair_council_error": pre_execute["error"]}),
                )
                o.tick_self_repair_queue()
                return
            if pre_execute.get("verdict") != "approve":
                _self_repair_reopen_current_issue(
                    task,
                    blocker_code="self_repair_replan_requested",
                    reason=pre_execute.get("reason") or "self-repair pre-execution council requested replan",
                    deliberation={**pre_execute, "stage": "pre_execute", "task_id": task_id},
                )
                def mut_abandon_precheck(t):
                    t["finished_at"] = o.now_iso()
                    t["self_repair_council"] = pre_execute
                o.move_task(
                    task_id,
                    "claimed",
                    "abandoned",
                    reason=pre_execute.get("reason") or "self-repair replan requested before execution",
                    mutator=mut_abandon_precheck,
                )
                o.tick_self_repair_queue()
                return

        log_path = o.LOGS_DIR / f"{task_id}.log"
        last_msg_path = o.LOGS_DIR / f"{task_id}.last.txt"

        def mut_running(t):
            t["braid_template_path"] = f"braid/templates/{bt}.mmd"
            t["braid_template_hash"] = graph_hash
            t["worktree"] = str(wt_path)
            t["base_branch"] = base_branch
            t["started_at"] = o.now_iso()
            t["log_path"] = str(log_path)
        o.move_task(task_id, "claimed", "running", reason="codex exec", mutator=mut_running)

        cmd = [
            "codex", "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-C", str(wt_path),
            "--ephemeral",
            "--json",
            "-o", str(last_msg_path),
            prompt,
        ]

        with log_path.open("w") as logf:
            logf.write(f"# codex exec task={task_id} template={bt} hash={graph_hash}\n")
            logf.write(f"# worktree: {wt_path}\n")
            logf.write(f"# graph:\n{graph_body}\n\n---\n")
            logf.flush()
            try:
                proc = _run_bounded(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                # Codex hit the slot wall — that's slot exhaustion, not a graph
                # traversal failure. Don't pollute the BRAID topology_errors
                # metric (regenerating the template won't fix a small budget).
                def mut_fail(t):
                    t["finished_at"] = o.now_iso()
                    t["failure"] = f"codex timeout {timeout}s"
                fail_task(task_id, "running", f"codex timeout {timeout}s",
                          blocker_code="llm_timeout", summary="codex timeout", retryable=True, mutator=mut_fail)
                return
            if proc.stderr:
                logf.write(proc.stderr)
            if proc.stdout:
                logf.write(proc.stdout)

        trailer = ""
        if last_msg_path.exists():
            lines = [l.strip() for l in last_msg_path.read_text().splitlines() if l.strip()]
            # Paper's guidance: scan the last few non-empty lines.
            trailer = _find_braid_trailer(lines)
        _record_task_costs_from_text(task_id, "codex", "gpt-5.4", proc.stdout or "")

        if proc.returncode != 0 and not trailer:
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
            fail_task(task_id, "running", f"codex exit {proc.returncode}",
                      blocker_code="llm_exit_error", summary="codex exit error", retryable=True, mutator=mut_fail)
            return

        if trailer.startswith("BRAID_REFINE"):
            enqueue_braid_refine(task, project["name"], trailer, from_state="running")
            return

        if trailer.startswith("BRAID_TOPOLOGY_ERROR"):
            if not topology_reason_is_valid(trailer):
                # False blocker claim — solver tried to exit with an
                # unrecognized reason (e.g. "pre-existing unrelated
                # failure"). Do NOT pollute topology_errors, do NOT
                # enqueue a useless regen, do NOT block downstream work
                # on this project.
                if self_repair_meta.get("enabled"):
                    blocker_code = "validator_malfunction" if _is_validator_malfunction(trailer) else "false_blocker_claim"
                    verifier = _run_self_repair_blocker_verifier(
                        task,
                        project,
                        wt_path,
                        timeout,
                        blocker_code,
                        trailer,
                        memory_ctx,
                    )
                    _self_repair_reopen_current_issue(
                        task,
                        blocker_code=blocker_code,
                        reason=verifier.get("reason") or f"{blocker_code}: {trailer[:120]}",
                        deliberation={**verifier, "stage": "blocker_verifier", "task_id": task_id},
                        summary_suffix="validator-malfunction" if blocker_code == "validator_malfunction" else "replan",
                    )
                    def mut_abandon_false(t):
                        t["finished_at"] = o.now_iso()
                        t["false_blocker_claim"] = trailer
                        t["self_repair_verifier"] = verifier
                    o.move_task(
                        task_id,
                        "running",
                        "abandoned",
                        reason=verifier.get("reason") or "self-repair verifier reopened false blocker claim",
                        mutator=mut_abandon_false,
                    )
                    o.tick_self_repair_queue()
                    return
                def mut_fail_false(t):
                    t["finished_at"] = o.now_iso()
                    t["topology_error"] = None
                    t["false_blocker_claim"] = trailer
                    blocker_code = "validator_malfunction" if _is_validator_malfunction(trailer) else "false_blocker_claim"
                    o.set_task_blocker(
                        t,
                        blocker_code,
                        summary="validator malfunction" if blocker_code == "validator_malfunction" else "false blocker claim",
                        detail=trailer,
                        source="worker",
                        retryable=True if blocker_code == "validator_malfunction" else False,
                    )
                fail_task(
                    task_id, "running", f"false blocker claim: {trailer[:80]}",
                    blocker_code="validator_malfunction" if _is_validator_malfunction(trailer) else "false_blocker_claim",
                    summary="validator malfunction" if _is_validator_malfunction(trailer) else "false blocker claim",
                    retryable=True if _is_validator_malfunction(trailer) else False, mutator=mut_fail_false,
                )
                return
            if topology_reason_code(trailer) == "baseline_red":
                if self_repair_meta.get("enabled"):
                    verifier = _run_self_repair_blocker_verifier(
                        task,
                        project,
                        wt_path,
                        timeout,
                        "qa_smoke_failed",
                        trailer,
                        memory_ctx,
                    )
                    _self_repair_reopen_current_issue(
                        task,
                        blocker_code="qa_smoke_failed",
                        reason=verifier.get("reason") or "baseline smoke red before fix",
                        deliberation={**verifier, "stage": "blocker_verifier", "task_id": task_id},
                        summary_suffix="baseline-red",
                    )
                    def mut_abandon_baseline(t):
                        t["finished_at"] = o.now_iso()
                        t["failure"] = trailer
                        t["self_repair_verifier"] = verifier
                    o.move_task(
                        task_id,
                        "running",
                        "abandoned",
                        reason=verifier.get("reason") or "self-repair verifier reopened baseline red",
                        mutator=mut_abandon_baseline,
                    )
                    o.tick_self_repair_queue()
                    return
                def mut_fail_baseline(t):
                    t["finished_at"] = o.now_iso()
                    t["failure"] = trailer
                    o.set_task_blocker(
                        t,
                        "qa_smoke_failed",
                        summary="baseline smoke red before fix",
                        detail=trailer,
                        source="worker",
                        retryable=True,
                    )
                fail_task(
                    task_id, "running", "baseline smoke red before fix",
                    blocker_code="qa_smoke_failed", summary="baseline smoke red before fix",
                    retryable=True, mutator=mut_fail_baseline,
                )
                return

            if self_repair_meta.get("enabled"):
                verifier = _run_self_repair_blocker_verifier(
                    task,
                    project,
                    wt_path,
                    timeout,
                    "template_graph_error",
                    trailer,
                    memory_ctx,
                )
                _self_repair_reopen_current_issue(
                    task,
                    blocker_code="template_graph_error",
                    reason=verifier.get("reason") or "self-repair topology traversal requested replan",
                    deliberation={**verifier, "stage": "blocker_verifier", "task_id": task_id},
                    summary_suffix="topology",
                )
                def mut_abandon_topology(t):
                    t["finished_at"] = o.now_iso()
                    t["topology_error"] = trailer
                    t["self_repair_verifier"] = verifier
                o.move_task(
                    task_id,
                    "running",
                    "abandoned",
                    reason=verifier.get("reason") or "self-repair verifier reopened topology blocker",
                    mutator=mut_abandon_topology,
                )
                o.tick_self_repair_queue()
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
                o.set_task_blocker(
                    t,
                    "template_graph_error",
                    summary="template graph traversal error",
                    detail=trailer,
                    source="worker",
                    retryable=True,
                )
            o.move_task(task_id, "running", "blocked", reason=trailer[:80], mutator=mut_block)
            return

        if trailer.startswith("BRAID_OK"):
            o.braid_template_record_use(bt, topology_error=False)
            ok, info = _autocommit_worktree(pathlib.Path(wt_path), task, log_path)
            if not ok:
                if "repo-memory secret-scan hit" in info:
                    write_repo_memory_secret_alert(
                        project["name"],
                        task_id,
                        info,
                        _repo_memory_secret_hits(pathlib.Path(wt_path)),
                        log_path=str(log_path),
                    )
                def mut_fail(t):
                    t["finished_at"] = o.now_iso()
                    t["failure"] = f"auto-commit: {info}"
                fail_task(task_id, "running", f"auto-commit: {info}",
                          blocker_code="auto_commit_failed", summary="auto-commit failed", retryable=True, mutator=mut_fail)
                return
            def mut_ok(t):
                t["finished_at"] = o.now_iso()
                t["artifacts"] = t.get("artifacts", []) + [branch]
                if info not in ("", "clean"):
                    t["artifacts"].append(info)
            o.move_task(task_id, "running", "awaiting-review", reason=trailer[:80], mutator=mut_ok)
            return

        # Exit 0 but no recognizable trailer → treat as failed (CLI issue, not topology).
        def mut_fail(t):
            t["finished_at"] = o.now_iso()
        fail_task(task_id, "running", "no BRAID trailer",
                  blocker_code="model_output_invalid", summary="BRAID trailer missing", retryable=False, mutator=mut_fail)

    finally:
        if lock_fh is not None:
            lock_fh.close()
        # Do NOT auto-remove the worktree — reviewer/QA still needs it.
        # pass-2 cleanup pass will sweep done worktrees on a schedule.


def build_codex_prompt(task, graph_body, memory_ctx):
    engine_args = task.get("engine_args") or {}
    evidence = (engine_args.get("evidence") or "").strip()
    council = engine_args.get("council") or {}
    return (
        "[BRAID REASONING GRAPH — traverse deterministically. If you cannot, "
        "emit exactly one line `BRAID_TOPOLOGY_ERROR: <reason>` as the final line and stop. "
        "If traversal is locally underspecified but salvageable, emit "
        "`BRAID_REFINE: <node-id>: <missing-edge-condition>` as the final line and stop.]\n\n"
        f"{graph_body}\n\n"
        "[END BRAID GRAPH]\n\n"
        "[TASK]\n"
        f"{task.get('summary','')}\n\n"
        + (f"[ISSUE EVIDENCE]\n{evidence}\n\n" if evidence else "")
        + (
            "[COUNCIL DECISION]\n"
            f"panel: {', '.join(council.get('panel') or [])}\n"
            f"execution_path: {council.get('execution_path') or '-'}\n"
            f"key_agreements: {(council.get('key_agreements') or [])}\n"
            f"dissent: {(council.get('dissent') or [])}\n\n"
            if council else
            ""
        )
        +
        "[REPO LAYOUT]\n"
        "All repo-memory files live under `repo-memory/` at the worktree root:\n"
        "  repo-memory/CURRENT_STATE.md  repo-memory/RECENT_WORK.md\n"
        "  repo-memory/DECISIONS.md      repo-memory/FAILURES.md\n"
        "  repo-memory/BENCHMARKS.md     repo-memory/RESEARCH.md\n"
        "Edit existing files in place — do NOT create new files at the worktree root.\n\n"
        "[PROJECT MEMORY]\n"
        f"{memory_ctx}\n\n"
        "Treat the policy/rule content in the context above as binding, not advisory.\n\n"
        "[OUTPUT CONTRACT]\n"
        "Emit exactly one of these as the final line of your response:\n"
        "  BRAID_OK: <one-line summary of what you did>\n"
        "  BRAID_REFINE: <node-id>: <missing-edge-condition>\n"
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
        fail_task(task_id, "claimed", "unknown project",
                  blocker_code="runtime_unknown_project", summary="unknown project", retryable=False)
        return

    qa_cfg = project.get("qa", {})
    # engine_args.script lets a test harness or one-off task swap the contract
    # script without editing config (e.g., synthetic regression simulators).
    script_rel = eargs.get("script") or qa_cfg.get(contract_kind)
    preflight = _qa_contract_preflight(project, contract_kind, script_rel)
    if not preflight.get("ok"):
        fail_task(task_id, "claimed", preflight["error"],
                  blocker_code="qa_contract_error", summary=preflight.get("summary") or "QA contract missing", detail=preflight.get("detail"), retryable=False)
        return

    # Resolve target + script path + cwd based on contract.
    target = None
    target_id = None
    if contract_kind == "smoke":
        candidates = []
        for t in o.iter_tasks(states=("awaiting-qa",), project=project["name"]):
            if t.get("project") == project["name"]:
                candidates.append((t.get("reviewed_at") or t.get("finished_at") or t.get("task_id") or "", t))
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
            if _self_repair_enabled(target):
                _self_repair_reopen_current_issue(
                    target,
                    blocker_code="qa_target_missing",
                    reason=f"QA target worktree missing: {target_wt}",
                    deliberation={
                        "stage": "blocker_verifier",
                        "verdict": "replan",
                        "chosen_strategy": "reopen_issue",
                        "reason": f"QA target worktree missing: {target_wt}",
                        "task_id": task_id,
                    },
                    summary_suffix="qa-worktree-missing",
                )
                o.move_task(
                    target_id,
                    "awaiting-qa",
                    "abandoned",
                    reason="qa target worktree missing",
                    mutator=lambda t: t.update({"finished_at": o.now_iso(), "qa_failure": "worktree missing"}),
                )
                def mut_self(t):
                    t["finished_at"] = o.now_iso()
                o.move_task(task_id, "claimed", "done",
                            reason=f"reopened self-repair target {target_id}", mutator=mut_self)
                o.tick_self_repair_queue()
                return
            def mut_target_fail(t):
                t["finished_at"] = o.now_iso()
                t["qa_failure"] = "worktree missing"
                o.set_task_blocker(
                    t,
                    "qa_target_missing",
                    summary="QA target worktree missing",
                    detail=f"target worktree missing: {target_wt}",
                    source="worker",
                    retryable=False,
                )
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
        script_abs = pathlib.Path(preflight["script_abs"])
        run_cwd = target_wt
        smoke_env_repo_root = target_wt
    else:  # regression
        script_abs = pathlib.Path(preflight["script_abs"])
        run_cwd = project["path"]
        smoke_env_repo_root = None
    if preflight.get("restored"):
        o.append_event(
            "qa",
            "qa_script_restored",
            task_id=task_id,
            feature_id=task.get("feature_id"),
            details={"project": project["name"], "script": script_rel},
        )

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
                proc = _run_bounded(
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
                        o.set_task_blocker(
                            t,
                            "qa_smoke_failed",
                            summary="smoke timeout",
                            detail=f"smoke timeout {timeout}s",
                            source="worker",
                            retryable=True,
                        )
                    o.move_task(target_id, "awaiting-qa", "failed",
                                reason="smoke timeout", mutator=mut_t_to)
                    def mut_d_to(t):
                        t["finished_at"] = o.now_iso()
                    o.move_task(task_id, "running", "done",
                                reason=f"target timeout, driver done", mutator=mut_d_to)
                else:
                    fail_task(task_id, "running", f"qa timeout {timeout}s",
                              blocker_code="qa_smoke_failed", summary="QA timeout", retryable=True)
                return

        if proc.returncode == 0:
            if contract_kind == "smoke" and target_id:
                # Resolve the base branch once from the target's persisted
                # state. Tasks set by run_codex_slot carry `base_branch` — fall
                # back to deriving it from feature_id for older records.
                target_base = target.get("base_branch") or base_branch_for_task(target)
                feature = None
                feature_id = target.get("feature_id")
                if feature_id:
                    feature = o.read_feature(feature_id)
                self_repair_meta = dict((feature or {}).get("self_repair") or {})
                qa_gate = _run_semantic_qa_gate(
                    project["name"],
                    project["path"],
                    target_wt,
                    target_base,
                    target,
                    log_path,
                    timeout,
                )
                qa_gate_raw = (qa_gate.get("raw") or "")[:12000]
                if qa_gate.get("error"):
                    def mut_target_gate_fail(t):
                        t["finished_at"] = o.now_iso()
                        t["qa_failure"] = qa_gate["error"]
                        t["qa_gate_output"] = qa_gate_raw
                        o.set_task_blocker(
                            t,
                            "qa_scope_inadequate",
                            summary="semantic QA gate failed",
                            detail=qa_gate["error"],
                            source="worker",
                            retryable=True,
                        )
                    o.move_task(target_id, "awaiting-qa", "failed",
                                reason="semantic qa gate failed", mutator=mut_target_gate_fail)
                    def mut_driver_gate_fail(t):
                        t["finished_at"] = o.now_iso()
                    o.move_task(task_id, "running", "done",
                                reason=f"semantic qa gate failed for {target_id}", mutator=mut_driver_gate_fail)
                    return
                if qa_gate.get("verdict") == "topology_error":
                    if _self_repair_enabled(target):
                        verifier = _run_self_repair_blocker_verifier(
                            target,
                            project,
                            target_wt,
                            timeout,
                            "template_graph_error",
                            qa_gate_raw or "semantic QA gate topology error",
                            memory_ctx,
                        )
                        _self_repair_reopen_current_issue(
                            target,
                            blocker_code="template_graph_error",
                            reason=verifier.get("reason") or "semantic QA gate topology error",
                            deliberation={**verifier, "stage": "blocker_verifier", "task_id": task_id},
                            summary_suffix="qa-topology",
                        )
                        o.move_task(target_id, "awaiting-qa", "abandoned",
                                    reason=verifier.get("reason") or "semantic qa topology error",
                                    mutator=lambda t: t.update({"finished_at": o.now_iso(), "qa_gate_output": qa_gate_raw, "self_repair_verifier": verifier}))
                        def mut_driver_gate_topology(t):
                            t["finished_at"] = o.now_iso()
                        o.move_task(task_id, "running", "done",
                                    reason=f"self-repair reopened {target_id} after qa topology", mutator=mut_driver_gate_topology)
                        o.tick_self_repair_queue()
                        return
                    def mut_target_gate_topology(t):
                        t["finished_at"] = o.now_iso()
                        t["qa_gate_output"] = qa_gate_raw
                        o.set_task_blocker(
                            t,
                            "template_graph_error",
                            summary="semantic QA gate topology error",
                            detail=qa_gate_raw or "semantic QA gate topology error",
                            source="worker",
                            retryable=True,
                        )
                    o.move_task(target_id, "awaiting-qa", "failed",
                                reason="semantic qa topology error", mutator=mut_target_gate_topology)
                    def mut_driver_gate_topology(t):
                        t["finished_at"] = o.now_iso()
                    o.move_task(task_id, "running", "done",
                                reason=f"semantic qa topology error for {target_id}", mutator=mut_driver_gate_topology)
                    return
                if qa_gate.get("verdict") != "approve":
                    def mut_target_scope_fail(t):
                        t["finished_at"] = o.now_iso()
                        t["qa_failure"] = "semantic QA scope inadequate"
                        t["qa_gate_output"] = qa_gate_raw
                        o.set_task_blocker(
                            t,
                            "qa_scope_inadequate",
                            summary="QA scope inadequate for change",
                            detail=qa_gate_raw or "semantic QA scope inadequate",
                            source="worker",
                            retryable=True,
                        )
                    o.move_task(target_id, "awaiting-qa", "failed",
                                reason="semantic qa scope inadequate", mutator=mut_target_scope_fail)
                    def mut_driver_scope_fail(t):
                        t["finished_at"] = o.now_iso()
                    o.move_task(task_id, "running", "done",
                                reason=f"semantic qa scope inadequate for {target_id}", mutator=mut_driver_scope_fail)
                    return

                deploy_mode = _self_repair_deploy_mode(target, project["name"])
                if self_repair_meta.get("enabled") and deploy_mode == "local-main":
                    deploy_ok, deploy_reason, deploy_info = deploy_self_repair(
                        target,
                        project,
                        target_wt,
                        log_path,
                        restart_launch_agents=bool(self_repair_meta.get("restart_launch_agents", True)),
                    )
                    if deploy_ok:
                        def mut_target_self_repair_ok(t):
                            t["finished_at"] = o.now_iso()
                            t["qa_passed_at"] = o.now_iso()
                            t["qa_gate_output"] = qa_gate_raw
                            t["local_deploy"] = dict(deploy_info or {})
                        o.move_task(target_id, "awaiting-qa", "done",
                                    reason=f"self-repair deployed via {task_id}", mutator=mut_target_self_repair_ok)
                        if feature_id:
                            try:
                                o.update_feature(
                                    feature_id,
                                    lambda f: (
                                        f.update(
                                            {
                                                "status": "merged",
                                                "finalized_at": o.now_iso(),
                                                "merged_at": o.now_iso(),
                                                "finalize_error": None,
                                                "local_deploy": dict(deploy_info or {}),
                                            }
                                        )
                                    ),
                                )
                            except FileNotFoundError:
                                pass
                    else:
                        blocker_code = (
                            "runtime_env_dirty"
                            if any(token in deploy_reason for token in ("canonical", "restart", "workflow-check"))
                            else "delivery_push_failed"
                        )
                        def mut_target_self_repair_fail(t):
                            t["finished_at"] = o.now_iso()
                            t["qa_passed_at"] = o.now_iso()
                            t["qa_gate_output"] = qa_gate_raw
                            t["deploy_failure"] = deploy_reason
                            t["local_deploy"] = dict(deploy_info or {})
                            o.set_task_blocker(
                                t,
                                blocker_code,
                                summary="self-repair deploy failed",
                                detail=deploy_reason,
                                source="worker",
                                retryable=True,
                            )
                        o.move_task(target_id, "awaiting-qa", "failed",
                                    reason=f"self-repair deploy failed: {deploy_reason}",
                                    mutator=mut_target_self_repair_fail)

                    def mut_driver_self_repair(t):
                        t["finished_at"] = o.now_iso()
                    o.move_task(task_id, "running", "done",
                                reason=f"self-repair deploy {'ok' if deploy_ok else 'failed'} for {target_id}",
                                mutator=mut_driver_self_repair)
                    return

                # Pattern B: always write pr-body.md; push only if opted-in.
                pr_body_path = None
                try:
                    pr_body_path = write_pr_body(
                        target=target, project=project,
                        qa_log_path=log_path, driver_task_id=task_id,
                        base_branch=target_base,
                    )
                except Exception as exc:
                    log(f"write_pr_body failed: {exc}")

                pushed_info = None
                push_failure = None
                if project.get("auto_push"):
                    try:
                        ok, reason, sha, ccount, pushed_at = push_worktree_branch(
                            target=target, project=project,
                            worktree=target_wt, branch=f"agent/{target_id}",
                            log_path=log_path,
                            base_branch=target_base,
                        )
                    except Exception as exc:
                        ok, reason, sha, ccount, pushed_at = False, f"push crashed: {exc}", None, 0, None
                        log(f"push_worktree_branch crashed: {exc}")
                    if ok:
                        pushed_info = {
                            "pushed_at": pushed_at,
                            "push_commit_sha": sha,
                            "push_commit_count": ccount,
                            "push_branch": f"agent/{target_id}",
                            "push_base_branch": target_base,
                        }
                        pr_number = target.get("pr_number")
                        pr_url = target.get("pr_url")
                        if pr_number:
                            pushed_info["pr_number"] = pr_number
                            if pr_url:
                                pushed_info["pr_url"] = pr_url
                        else:
                            # Pattern C: also open the PR via gh.
                            try:
                                pr_ok, pr_reason, pr_url, pr_number = create_pr(
                                    target=target, project=project,
                                    branch=f"agent/{target_id}",
                                    pr_body_path=pr_body_path,
                                    log_path=log_path,
                                    base_branch=target_base,
                                )
                            except Exception as exc:
                                pr_ok, pr_reason = False, f"create_pr crashed: {exc}"
                                pr_url, pr_number = None, None
                                log(f"create_pr crashed: {exc}")
                            if pr_ok:
                                pushed_info["pr_url"] = pr_url
                                pushed_info["pr_number"] = pr_number
                                pushed_info["pr_created_at"] = o.now_iso()
                            else:
                                pushed_info["pr_create_failure"] = pr_reason
                        if pr_number:
                            review_ok, review_reason = o._request_codex_review(
                                project["path"],
                                pr_number,
                                reason="autonomous branch update after QA",
                                commit_sha=sha,
                            )
                            if review_ok:
                                pushed_info["review_requested_at"] = o.now_iso()
                                pushed_info["review_request_error"] = None
                            else:
                                pushed_info["review_request_error"] = review_reason
                    else:
                        push_failure = reason

                if push_failure:
                    def mut_target_push_fail(t):
                        t["finished_at"] = o.now_iso()
                        t["qa_passed_at"] = o.now_iso()
                        t["push_failure"] = push_failure
                        o.set_task_blocker(
                            t,
                            "delivery_push_failed",
                            summary="push failed after smoke passed",
                            detail=push_failure,
                            source="worker",
                            retryable=True,
                        )
                        if pr_body_path:
                            t["pr_body_path"] = str(pr_body_path)
                    o.move_task(target_id, "awaiting-qa", "failed",
                                reason=f"smoke ok but push failed: {push_failure}",
                                mutator=mut_target_push_fail)
                else:
                    def mut_target_ok(t):
                        t["finished_at"] = o.now_iso()
                        t["qa_passed_at"] = o.now_iso()
                        t["qa_gate_output"] = qa_gate_raw
                        if pr_body_path:
                            t["pr_body_path"] = str(pr_body_path)
                        if pushed_info:
                            t.update(pushed_info)
                    o.move_task(target_id, "awaiting-qa", "done",
                                reason=f"smoke pass via {task_id}", mutator=mut_target_ok)

                # Driver task ran the script successfully regardless of push outcome.
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
                o.set_task_blocker(
                    t,
                    "qa_smoke_failed",
                    summary="smoke failed",
                    detail=f"smoke exit {proc.returncode}",
                    source="worker",
                    retryable=True,
                )
            o.move_task(target_id, "awaiting-qa", "failed",
                        reason=f"smoke fail via {task_id}", mutator=mut_target_fail)
            def mut_driver_fail(t):
                t["finished_at"] = o.now_iso()
            o.move_task(task_id, "running", "done",
                        reason=f"smoke fail for {target_id}", mutator=mut_driver_fail)
        else:
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
            fail_task(task_id, "running", f"{contract_kind} exit {proc.returncode}",
                      blocker_code="qa_smoke_failed", summary=f"{contract_kind} failed",
                      retryable=True, mutator=mut_fail)
    except TimeoutError as exc:
        fail_task(task_id, "claimed", str(exc),
                  blocker_code="runtime_precondition_failed", summary="QA precondition failed", retryable=True)
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
        tb_text = traceback.format_exc()
        _handle_worker_crash(task, slot, exc, tb_text)
    finally:
        o.clear_claim_pid(task["task_id"])
        o.write_agent_status(f"worker-{slot}", "idle", "")

    return 0


if __name__ == "__main__":
    sys.exit(main())

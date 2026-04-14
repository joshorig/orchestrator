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
import difflib
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
import tempfile
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


def ensure_feature_branch(project_path, feature_branch):
    """Create `feature_branch` locally + on origin if it doesn't already exist.

    Idempotent. Safe to call repeatedly — the first caller creates the branch,
    subsequent callers observe it already present and return. Uses origin/main
    as the starting point so every feature branch begins from the current
    trunk HEAD at the moment of first use.

    Returns True if the branch was newly created, False if it already existed.
    """
    preflight_main(project_path)

    # Fast path: branch already present locally.
    probe = subprocess.run(
        ["git", "-C", project_path, "rev-parse", "--verify", "--quiet", feature_branch],
        capture_output=True, text=True,
    )
    if probe.returncode == 0:
        return False

    # Fetch origin so we can check + start from a fresh main.
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

    # Create the branch locally off origin/main and push it to origin so sibling
    # worktrees can fetch it. Use the agent git identity for the push.
    create = subprocess.run(
        ["git", "-C", project_path, "branch", feature_branch, "origin/main"],
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


def scan_for_secrets(text):
    """Return a list of (label, snippet) for any secret patterns found."""
    if not text:
        return []
    hits = []
    for pat, label in _SECRET_PATTERNS:
        m = pat.search(text)
        if m:
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 20)
            hits.append((label, text[start:end].replace("\n", " ")))
    return hits


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
      - aborts if `git diff <base_branch>` hits secret patterns
      - uses a distinct `devmini-orchestrator` commit identity
      - leaves the worktree intact on failure for human inspection

    base_branch: the ref the agent branch was created from. Used for the
    secret-scan diff and the ahead-count check. Typically "main" or
    "feature/<id>".
    """
    if branch == "main" or branch.endswith("/main"):
        return (False, "refuse to push main", None, 0, None)

    wt = pathlib.Path(worktree)
    if not wt.exists():
        return (False, "worktree missing", None, 0, None)

    # Secret scan: look at the full diff vs base_branch AND staged-but-uncommitted.
    full_diff = _git(wt, "diff", base_branch).stdout or ""
    staged = _git(wt, "diff", "--cached").stdout or ""
    unstaged = _git(wt, "diff").stdout or ""
    combined = full_diff + "\n" + staged + "\n" + unstaged
    hits = scan_for_secrets(combined)
    if hits:
        labels = ", ".join(sorted({h[0] for h in hits}))
        try:
            with open(log_path, "a") as f:
                f.write(f"\n# PUSH ABORTED: secret-scan hit ({labels})\n")
                for label, snippet in hits[:8]:
                    f.write(f"#   {label}: ...{snippet}...\n")
        except Exception:
            pass
        return (False, f"secret-scan hit: {labels}", None, 0, None)

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
        o.move_task(task_id, "claimed", "failed", reason=f"unknown project {project_name}")
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
        o.move_task(task_id, "claimed", "failed", reason="template missing, regen disabled")
        return

    memdir = pathlib.Path(project["path"]) / "repo-memory"
    current_path = memdir / "CURRENT_STATE.md"
    recent_path = memdir / "RECENT_WORK.md"
    decisions_path = memdir / "DECISIONS.md"
    if not current_path.exists() or not recent_path.exists() or not decisions_path.exists():
        o.move_task(task_id, "claimed", "failed", reason="repo-memory incomplete")
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
        "--output-format", "text",
        "--model", "sonnet",
        "--max-budget-usd", "1.00",
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
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=logf, text=True, timeout=timeout,
                cwd="/tmp",
            )
        except subprocess.TimeoutExpired:
            o.move_task(task_id, "running", "failed", reason=f"claude timeout {timeout}s")
            return
        logf.write(f"\n# exit: {proc.returncode}\n# stdout:\n{proc.stdout or ''}\n")

    if proc.returncode != 0:
        o.move_task(task_id, "running", "failed", reason=f"claude exit {proc.returncode}")
        return

    candidate = extract_markdown_document(proc.stdout or "")
    if not markdown_sane(candidate):
        o.move_task(task_id, "running", "failed", reason="candidate markdown invalid")
        return
    if scan_for_secrets(candidate):
        o.move_task(task_id, "running", "failed", reason="candidate contains secret-like content")
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
        o.move_task(task_id, "running", "failed", reason="delta exceeds 200 lines")
        return
    if recent_path.read_text() != recent_text:
        o.move_task(task_id, "running", "failed", reason="RECENT_WORK changed during synthesis")
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

DAG_IMPLEMENT_NODE_POSITIVE_PATTERNS = (
    "node", "operator", "processing-element", "processing element", "dag", "pipeline-node",
)
DAG_HISTORIAN_POSITIVE_PATTERNS = (
    "history", "memory", "recent_work", "recent work", "historian", "current_state", "repo memory",
)
TRP_PIPELINE_STAGE_POSITIVE_PATTERNS = (
    "stage", "pipeline", "platform-runtime", "platform runtime", "ingest", "jmh",
)
TRP_UI_COMPONENT_POSITIVE_PATTERNS = (
    "component", "page", ".tsx", "playwright", "a11y", "apps/",
)

PROJECT_CROSS_MARKERS = {
    "lvc": ("dag-framework", "trade-research-platform", "apps/", ".tsx", "playwright", "a11y"),
    "dag": ("lvc-standard", "trade-research-platform", "apps/", ".tsx", "playwright", "a11y"),
    "trp": ("lvc-standard", "dag-framework", "src/main/java", "operator", "repo-memory/"),
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
        "and propose at most 3 bounded CODEX execution slices for the next actionable work.\n\n"
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
    >>> classify_slice("trp-ui-component", "Optimize ingest pipeline stage jmh smoke")[0]
    False
    >>> classify_slice("trp-ui-component", "Append RECENT_WORK history entry")[0]
    False
    >>> classify_slice("trp-ui-component", "Fix lvc-standard operator semantics")[0]
    False
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
        if _contains_any(s, TRP_PIPELINE_STAGE_POSITIVE_PATTERNS):
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
    roadmap_entry = (task.get("engine_args") or {}).get("roadmap_entry") or {}
    system_prompt = planner_system_prompt(project["name"], roadmap_entry.get("body", ""))
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
    TEMPLATE_ROLE = planner_template_roles(project["name"])

    feature_id = task.get("feature_id")

    enqueued = []
    dropped = []
    sibling_task_ids = []
    for idx, s in enumerate(slices[:3]):
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
            feature_id=feature_id,
            depends_on=[],
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
                resolved.append(sibling_task_ids[dep])
        if resolved is None:
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
        t["finished_at"] = o.now_iso()
        t["log_path"] = str(log_path)
    o.move_task(task_id, "running", "done", reason=f"decomposed into {len(enqueued)}", mutator=mut)


def run_claude_reviewer(task, cfg, timeout, log_path):
    """Claude reviewer: claim one awaiting-review target and transition it.

    Loads the lvc-reviewer-pass BRAID template as system context plus the
    target's git diff vs main as user context, runs claude, parses verdict,
    promotes target → awaiting-qa on APPROVE or enqueues a review-feedback
    codex pass on REQUEST_CHANGE. The reviewer task itself is marked done with
    the verdict in its log.
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
    target_base = target.get("base_branch") or base_branch_for_task(target)
    diff_text = "(no worktree available)"
    if target_wt and pathlib.Path(target_wt).exists():
        try:
            stat = subprocess.run(
                ["git", "-C", target_wt, "diff", target_base, "--stat"],
                capture_output=True, text=True, timeout=30,
            ).stdout
            body = subprocess.run(
                ["git", "-C", target_wt, "diff", target_base],
                capture_output=True, text=True, timeout=30,
            ).stdout
            if len(body) > 30000:
                body = body[:30000] + "\n\n[...diff truncated at 30k chars...]\n"
            diff_text = (stat + "\n\n" + body).strip() or f"(empty diff vs {target_base})"
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
        _handle_review_request_change(task_id, project_name, target, raw[:6000], mut_target)

    def mut_self(t):
        t["finished_at"] = o.now_iso()
    o.move_task(task_id, "running", "done",
                reason=f"reviewed {target_id} -> {verdict}", mutator=mut_self)


def _handle_review_request_change(reviewer_task_id, project_name, target, review_findings, mut_target):
    """Route reviewer request_change into feedback retry or terminal failure.

    >>> _review_request_change_doctest()
    ((('move', 'failed', 'review rounds exhausted (3)', 4), ('alert', 'review rounds exhausted (3)')), (('enqueue', 2, 'review-address-feedback'), ('move', 'queued', 'review feedback round 2', 2)))
    """
    rounds = int(target.get("review_feedback_rounds", 0)) + 1
    MAX_ROUNDS = 3
    target_id = target["task_id"]
    if rounds > MAX_ROUNDS:
        def mut_fail_target(t):
            mut_target(t)
            t["review_feedback_rounds"] = rounds
        o.move_task(
            target_id, "awaiting-review", "failed",
            reason=f"review rounds exhausted ({MAX_ROUNDS})", mutator=mut_fail_target,
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
    _review_request_change_doctest.__globals__["o"] = FakeO()
    try:
        _handle_review_request_change("reviewer-1", "demo", {"task_id": "task-a", "project": "demo", "feature_id": "f1", "base_branch": "main", "worktree": "/tmp/wt", "review_feedback_rounds": 3}, "need tests", lambda t: t.update({"reviewed_by": "reviewer-1"}))
        exhausted = (calls[0], calls[1])
        calls.clear()
        _handle_review_request_change("reviewer-2", "demo", {"task_id": "task-b", "project": "demo", "feature_id": "f2", "base_branch": "main", "worktree": "/tmp/wt", "review_feedback_rounds": 1}, "need docs", lambda t: t.update({"reviewed_by": "reviewer-2"}))
        retried = (calls[0], calls[1])
        return (exhausted, retried)
    finally:
        _review_request_change_doctest.__globals__["o"] = old_o


def run_review_feedback_task(task, cfg, timeout, log_path):
    task_id = task["task_id"]
    eargs = task.get("engine_args") or {}
    target_id = eargs.get("target_task_id")
    round_no = eargs.get("round")
    review_findings = eargs.get("review_findings", "")
    bt = task.get("braid_template") or "review-address-feedback"

    if not target_id:
        o.move_task(task_id, "claimed", "failed", reason="review-feedback: missing target_task_id")
        return

    target_file = o.queue_dir("queued") / f"{target_id}.json"
    target = o.read_json(target_file, None)
    if target is None:
        o.move_task(task_id, "claimed", "failed",
                    reason=f"review-feedback: target {target_id} not in queued/")
        return

    wt_str = task.get("worktree") or target.get("worktree")
    if not wt_str or not pathlib.Path(wt_str).exists():
        o.move_task(task_id, "claimed", "failed",
                    reason=f"review-feedback: target worktree missing: {wt_str}")
        return
    wt = pathlib.Path(wt_str)
    base_branch = task.get("base_branch") or target.get("base_branch") or base_branch_for_task(target)

    try:
        project = o.get_project(cfg, task["project"])
    except KeyError:
        o.move_task(task_id, "claimed", "failed", reason="review-feedback: unknown project")
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
        o.move_task(task_id, "claimed", "failed", reason="template missing, regen disabled")
        return

    lock_fh = None
    try:
        lock_fh = acquire_lock(f"{project['name']}.lock", mode="shared", timeout_sec=60)
        memory_ctx = read_memory_context(project["path"])
        diff_text = _git(wt, "diff", base_branch).stdout or ""
        if len(diff_text) > 30000:
            diff_text = diff_text[:30000] + "\n\n[...diff truncated at 30k chars...]\n"
        prompt = (
            "[BRAID REASONING GRAPH — traverse deterministically. If you cannot, "
            "emit exactly one line `BRAID_TOPOLOGY_ERROR: <reason>` as the final line and stop.]\n\n"
            f"{graph_body}\n\n"
            "[END BRAID GRAPH]\n\n"
            f"[TARGET TASK]\n{target_id}\n\n"
            f"[REVIEW FEEDBACK TO ADDRESS]\n{review_findings}\n\n"
            f"[CURRENT DIFF vs {base_branch}]\n{diff_text or f'(empty diff vs {base_branch})'}\n\n"
            f"[PROJECT MEMORY]\n{memory_ctx}\n\n"
            "[OUTPUT CONTRACT]\n"
            "Emit exactly one of these as the final line of your response:\n"
            "  BRAID_OK: <one-line summary of what you changed>\n"
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
            "-o", str(last_msg_path),
            prompt,
        ]

        with pathlib.Path(log_path).open("w") as logf:
            logf.write(f"# review-feedback task={task_id} target={target_id} round={round_no}\n")
            logf.write(f"# template={bt} hash={graph_hash}\n")
            logf.write(f"# worktree: {wt}\n\n---\n")
            logf.flush()
            try:
                proc = subprocess.run(
                    cmd, stdout=logf, stderr=subprocess.STDOUT, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                def mut_fail(t):
                    t["finished_at"] = o.now_iso()
                    t["failure"] = f"codex timeout {timeout}s"
                o.move_task(task_id, "running", "failed",
                            reason=f"review-feedback timeout {timeout}s", mutator=mut_fail)
                return

        trailer = ""
        if last_msg_path.exists():
            lines = [l.strip() for l in last_msg_path.read_text().splitlines() if l.strip()]
            for line in reversed(lines[-20:]):
                if line.startswith("BRAID_OK") or line.startswith("BRAID_TOPOLOGY_ERROR"):
                    trailer = line
                    break

        if trailer.startswith("BRAID_TOPOLOGY_ERROR"):
            o.braid_template_record_use(bt, topology_error=True)
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
                t["topology_error"] = trailer
            o.move_task(task_id, "running", "failed", reason=trailer[:80], mutator=mut_fail)
            return

        if not trailer.startswith("BRAID_OK"):
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
            o.move_task(task_id, "running", "failed",
                        reason=f"review-feedback no BRAID trailer (exit {proc.returncode})",
                        mutator=mut_fail)
            return

        o.braid_template_record_use(bt, topology_error=False)
        ok, info = _autocommit_worktree(pathlib.Path(wt), target, log_path)
        if not ok:
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
                t["failure"] = f"auto-commit: {info}"
            o.move_task(task_id, "running", "failed",
                        reason=f"auto-commit: {info}", mutator=mut_fail)
            return

        def mut_target(t):
            t["finished_at"] = o.now_iso()
            t["review_feedback_rounds"] = int(round_no or t.get("review_feedback_rounds", 0))
            if info not in ("", "clean"):
                t["artifacts"] = t.get("artifacts", []) + [info]
        o.move_task(target_id, "queued", "awaiting-review",
                    reason=f"review feedback applied ({task_id})", mutator=mut_target)

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


def build_pr_feedback_prompt(*, target, graph_body, base_branch, conflicts, comments, pr_number, conflict_preview=None):
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
            f"{c.get('body','').strip()}"
        )
    comments_text = "\n\n".join(comment_blocks) or "(no review comments — rebase-only run)"
    conflict_preview_text = _format_conflict_preview(conflict_preview)

    header = (
        f"PR #{pr_number} for target {target.get('task_id')} needs maintenance.\n"
        f"Original summary: {target.get('summary','(no summary)')}\n"
        f"Base branch: {base_branch}\n"
        f"Conflicts with base: {conflicts}\n"
    )

    return (
        "[BRAID REASONING GRAPH — traverse deterministically. If you cannot, "
        "emit exactly one line `BRAID_TOPOLOGY_ERROR: <reason>` as the final line and stop.]\n\n"
        f"{graph_body}\n\n"
        "[END BRAID GRAPH]\n\n"
        f"[PR FEEDBACK CONTEXT]\n{header}\n\n"
        f"{conflict_preview_text}"
        "[REVIEW COMMENTS TO ADDRESS]\n"
        f"{comments_text}\n\n"
        "[ACTIONS YOU CAN TAKE]\n"
        f"- You are inside the target worktree on branch agent/{target.get('task_id')}.\n"
        f"- The base branch `{base_branch}` has already been fetched from origin.\n"
        f"- If there are conflicts, rebase onto origin/{base_branch} and resolve them.\n"
        "- Apply fixes requested by the review comments.\n"
        "- Commit your changes using the existing agent git identity if needed.\n"
        "- Do NOT push — worker.py will re-run smoke and push with --force-with-lease "
        "after it validates your work.\n\n"
        "[OUTPUT CONTRACT]\n"
        "Emit exactly one of these as the final line of your response:\n"
        "  BRAID_OK: <one-line summary of what you did>\n"
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
        o.move_task(task_id, "claimed", "failed", reason="pr-feedback: missing target_task_id")
        return

    target_file = o.queue_dir("done") / f"{target_id}.json"
    target = o.read_json(target_file, None)
    if target is None:
        o.move_task(task_id, "claimed", "failed",
                    reason=f"pr-feedback: target {target_id} not in done/")
        return

    wt_str = target.get("worktree")
    if not wt_str or not pathlib.Path(wt_str).exists():
        o.move_task(task_id, "claimed", "failed",
                    reason=f"pr-feedback: target worktree missing: {wt_str}")
        return
    wt = pathlib.Path(wt_str)
    agent_branch = f"agent/{target_id}"

    try:
        project = o.get_project(cfg, task["project"])
    except KeyError:
        o.move_task(task_id, "claimed", "failed", reason="pr-feedback: unknown project")
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
        o.move_task(task_id, "claimed", "failed",
                    reason="template missing, regen disabled")
        return

    lock_fh = None
    try:
        lock_fh = acquire_lock(f"{project['name']}.lock", mode="shared", timeout_sec=60)

        # Fetch latest base branch so codex sees a fresh tip when it rebases.
        _git(wt, "fetch", "origin", base_branch, timeout=120)

        prompt = build_pr_feedback_prompt(
            target=target, graph_body=graph_body,
            base_branch=base_branch, conflicts=conflicts, comments=comments,
            pr_number=pr_number, conflict_preview=conflict_preview,
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
                proc = subprocess.run(
                    cmd, stdout=logf, stderr=subprocess.STDOUT, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                def mut_fail(t):
                    t["finished_at"] = o.now_iso()
                    t["failure"] = f"codex timeout {timeout}s"
                o.move_task(task_id, "running", "failed",
                            reason=f"pr-feedback timeout {timeout}s", mutator=mut_fail)
                return

        trailer = ""
        if last_msg_path.exists():
            lines = [l.strip() for l in last_msg_path.read_text().splitlines() if l.strip()]
            for line in reversed(lines[-20:]):
                if line.startswith("BRAID_OK") or line.startswith("BRAID_TOPOLOGY_ERROR"):
                    trailer = line
                    break

        if trailer.startswith("BRAID_TOPOLOGY_ERROR"):
            if not topology_reason_is_valid(trailer):
                def mut_false(t):
                    t["finished_at"] = o.now_iso()
                    t["false_blocker_claim"] = trailer
                o.move_task(task_id, "running", "failed",
                            reason=f"false blocker claim: {trailer[:80]}", mutator=mut_false)
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
            o.move_task(task_id, "running", "blocked",
                        reason=trailer[:80], mutator=mut_block)
            return

        if not trailer.startswith("BRAID_OK"):
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
            o.move_task(task_id, "running", "failed",
                        reason=f"pr-feedback no BRAID trailer (exit {proc.returncode})",
                        mutator=mut_fail)
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
            o.move_task(task_id, "running", "failed",
                        reason="no qa.smoke", mutator=mut_fail)
            return

        smoke_abs = pathlib.Path(project["path"]) / smoke_rel
        smoke_env = os.environ.copy()
        smoke_env["REPO_ROOT"] = str(wt)
        with log_path.open("a") as logf:
            logf.write(f"\n# pr-feedback smoke re-run: {smoke_abs}\n")
            logf.write(f"# REPO_ROOT={wt}\n\n")
            logf.flush()
            try:
                smoke_proc = subprocess.run(
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
                o.move_task(task_id, "running", "failed",
                            reason="smoke re-run timeout", mutator=mut_fail)
                return

        if smoke_proc.returncode != 0:
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
                t["failure"] = f"smoke re-run exit {smoke_proc.returncode}"
            o.move_task(task_id, "running", "failed",
                        reason="pr-feedback smoke red", mutator=mut_fail)
            return

        # Smoke green — force-push with lease so upstream gets the fix.
        push = _git_agent(
            wt, "push", "--force-with-lease", "origin", agent_branch, timeout=120,
        )
        if push.returncode != 0:
            def mut_fail(t):
                t["finished_at"] = o.now_iso()
                t["failure"] = f"pr-feedback push failed: {push.stderr.strip()[:200]}"
            o.move_task(task_id, "running", "failed",
                        reason="pr-feedback push failed", mutator=mut_fail)
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
        thread_ids = [
            c.get("thread_id") for c in (comments or []) if c.get("thread_id")
        ]
        resolved_count = 0
        if thread_ids:
            resolved_count, failed_count = o._resolve_review_threads(
                project["path"], thread_ids,
            )
            with log_path.open("a") as lf:
                lf.write(
                    f"\n# resolveReviewThread: {resolved_count} resolved, "
                    f"{failed_count} failed ({len(set(thread_ids))} unique thread(s))\n"
                )

        def mut_ok(t):
            t["finished_at"] = o.now_iso()
            t["pr_feedback_new_sha"] = new_sha
            if thread_ids:
                t["resolved_thread_count"] = resolved_count
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
    base_branch = "main"
    try:
        # Shared lock to coexist with other codex workers; blocks against regression exclusive.
        lock_fh = acquire_lock(f"{project['name']}.lock", mode="shared", timeout_sec=60)

        base_branch = base_branch_for_task(task)
        if base_branch != "main":
            try:
                ensure_feature_branch(project["path"], base_branch)
            except MainDirtyOrAhead as exc:
                def mut_block_main(t):
                    t["finished_at"] = o.now_iso()
                    t["topology_error"] = "main_dirty_or_ahead"
                    t["topology_error_message"] = str(exc)
                o.move_task(
                    task_id,
                    "claimed",
                    "blocked",
                    reason=str(exc)[:80],
                    mutator=mut_block_main,
                )
                try:
                    write_regression_alert(
                        project["name"],
                        task_id,
                        f"main_dirty_or_ahead: {exc}",
                        None,
                    )
                except Exception as alert_exc:
                    log(f"main_dirty_or_ahead alert write failed: {alert_exc}")
                return

        wt_path, branch, base_branch = make_worktree(
            project["path"], task_id, base_branch=base_branch,
        )

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
            ok, info = _autocommit_worktree(pathlib.Path(wt_path), task, log_path)
            if not ok:
                def mut_fail(t):
                    t["finished_at"] = o.now_iso()
                    t["failure"] = f"auto-commit: {info}"
                o.move_task(task_id, "running", "failed", reason=f"auto-commit: {info}", mutator=mut_fail)
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
                # Resolve the base branch once from the target's persisted
                # state. Tasks set by run_codex_slot carry `base_branch` — fall
                # back to deriving it from feature_id for older records.
                target_base = target.get("base_branch") or base_branch_for_task(target)
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
                    else:
                        push_failure = reason

                if push_failure:
                    def mut_target_push_fail(t):
                        t["finished_at"] = o.now_iso()
                        t["qa_passed_at"] = o.now_iso()
                        t["push_failure"] = push_failure
                        if pr_body_path:
                            t["pr_body_path"] = str(pr_body_path)
                    o.move_task(target_id, "awaiting-qa", "failed",
                                reason=f"smoke ok but push failed: {push_failure}",
                                mutator=mut_target_push_fail)
                else:
                    def mut_target_ok(t):
                        t["finished_at"] = o.now_iso()
                        t["qa_passed_at"] = o.now_iso()
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

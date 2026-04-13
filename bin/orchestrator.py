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
import datetime as dt
import errno
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import uuid

DEV_ROOT = pathlib.Path(os.environ.get("DEV_ROOT", "/Volumes/devssd"))
STATE_ROOT = DEV_ROOT / "orchestrator"
CONFIG_PATH = STATE_ROOT / "config" / "orchestrator.json"
QUEUE_ROOT = STATE_ROOT / "queue"
AGENT_STATE_DIR = STATE_ROOT / "state" / "agents"
RUNTIME_DIR = STATE_ROOT / "state" / "runtime"
CLAIMS_DIR = RUNTIME_DIR / "claims"
LOCKS_DIR = RUNTIME_DIR / "locks"
TRANSITIONS_LOG = RUNTIME_DIR / "transitions.log"
REPORT_DIR = STATE_ROOT / "reports"
LOGS_DIR = STATE_ROOT / "logs"
TELEGRAM_INBOX = STATE_ROOT / "telegram" / "inbox"
TELEGRAM_OUTBOX = STATE_ROOT / "telegram" / "outbox"
BRAID_DIR = STATE_ROOT / "braid"
BRAID_TEMPLATES = BRAID_DIR / "templates"
BRAID_GENERATORS = BRAID_DIR / "generators"
BRAID_INDEX = BRAID_DIR / "index.json"

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


def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def load_config():
    return json.loads(CONFIG_PATH.read_text())


def get_project(config, name):
    for p in config["projects"]:
        if p["name"] == name:
            return p
    raise KeyError(f"unknown project: {name}")


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
    engine_args=None,
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
        "state": "queued",
        "braid_template": braid_template,
        "braid_template_path": None,
        "braid_template_hash": None,
        "braid_generate_if_missing": braid_generate_if_missing,
        "worktree": None,
        "log_path": None,
        "artifacts": [],
        "engine_args": engine_args or {},
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
    """
    queued = queue_dir("queued")
    candidates = sorted(queued.glob("*.json"))
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


# --- BRAID template registry -------------------------------------------------

def load_braid_index():
    if not BRAID_INDEX.exists():
        return {}
    return json.loads(BRAID_INDEX.read_text())


def save_braid_index(idx):
    write_json_atomic(BRAID_INDEX, idx)


def braid_template_path(task_type):
    return BRAID_TEMPLATES / f"{task_type}.mmd"


def braid_template_load(task_type):
    p = braid_template_path(task_type)
    if not p.exists():
        return None, None
    body = p.read_text()
    digest = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
    return body, digest


def braid_template_write(task_type, body, generator_model):
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


# --- Agent status ------------------------------------------------------------

def write_agent_status(role, status, detail=""):
    write_json_atomic(
        AGENT_STATE_DIR / f"{role}.json",
        {"role": role, "status": status, "detail": detail, "updated_at": now_iso()},
    )


def agent_statuses():
    AGENT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return [read_json(p, {}) for p in sorted(AGENT_STATE_DIR.glob("*.json"))]


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
    """Emit one claude planning task per project. Claude decomposes into codex slices.

    Gated: skips if any slot already has >1 outstanding task in queued/claimed/running.
    Prevents the 3-min tick cadence from unbounded queue growth when workers can't keep up.
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
        task = new_task(
            role="planner",
            engine="claude",
            project=project["name"],
            summary=f"Review repo-memory and decompose next actionable work for {project['name']}.",
            source="tick-planner",
            braid_template=None,  # planning runs are freeform; they emit slices
        )
        enqueue_task(task)
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
    out.write_text("\n".join(lines))
    return out


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
    idx = load_braid_index()
    if idx:
        lines.append("")
        lines.append("BRAID:")
        for tt, e in sorted(idx.items()):
            lines.append(
                f"  {tt}: uses={e.get('uses',0)} errs={e.get('topology_errors',0)}"
            )
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

    sub.add_parser("reap")

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
    elif args.cmd == "reap":
        n = reap()
        print(f"reaped {n}")
    elif args.cmd == "report":
        print(report(args.kind))
    elif args.cmd == "process-telegram":
        process_telegram()
    elif args.cmd == "transition":
        move_task(args.task, args.from_state, args.to_state, reason=args.reason)
        print(f"{args.task}: {args.from_state} -> {args.to_state}")


if __name__ == "__main__":
    main()

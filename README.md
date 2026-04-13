# devmini orchestrator

Always-on task orchestrator for agent-driven engineering work across canonical repos on this node. Pairs a high-intelligence generator (`claude`) with a cheap solver (`codex`) following the BRAID architecture (arXiv:2512.15959), plus a bounded QA slot for contract verification. Launchd respawns one short-lived worker per slot; each worker claims exactly one task and exits.

## Quickstart

```bash
# inspect state
python3 bin/orchestrator.py status

# enqueue a manual codex task for lvc-standard
python3 bin/orchestrator.py enqueue \
  --engine codex \
  --role implementer \
  --project lvc-standard \
  --braid-template lvc-implement-operator \
  --summary "Reduce hot-path allocs in the in-proc publisher"

# force a planner / reviewer / qa tick right now
python3 bin/orchestrator.py planner
python3 bin/orchestrator.py reviewer
python3 bin/orchestrator.py qa

# rescue: sweep stale claims (dead pids) back to queued
python3 bin/orchestrator.py reap

# delivery hygiene: remove worktrees for merged/closed PRs
python3 bin/orchestrator.py cleanup-worktrees [--dry-run]

# generate a status report (also pushed via telegram bot when running)
python3 bin/orchestrator.py report morning
```

## Architecture

### State machine

```
queued → claimed → running → { awaiting_review | awaiting_qa | blocked | done | failed | abandoned }
```

Transitions are recorded append-only in `state/runtime/transitions.log`. Every substate is a real directory under `queue/`; workers move tasks between directories with `os.rename`, which is atomic on APFS — whichever worker wins the rename owns the task. A pid file in `state/runtime/claims/` lets `orchestrator.py reap` detect stale claims from dead workers.

### Slots

| Slot | Role | Timeout | What it runs |
|---|---|---|---|
| `claude` | BRAID **generator** | 1800s | Decomposes parent tasks into codex slices; generates Mermaid reasoning graphs for new task types. Runs rarely — templates amortize across N executions. |
| `codex` | BRAID **solver** | 1800s | Executes bounded implementation slices inside an assigned worktree, traversing a cached Mermaid graph received as system context. Signals `BRAID_OK` or `BRAID_TOPOLOGY_ERROR` via output trailer. |
| `qa` | Contract runner | 900s | Executes `<repo>/qa/smoke.sh` or `<repo>/qa/regression.sh`. No LLM involved. |

Concurrency is enforced in `config/orchestrator.json`: one active task per slot at a time. Workers follow the one-task-then-exit model — launchd handles throttling via `ThrottleInterval`.

### BRAID templates

Task types are backed by cached reasoning graphs in `braid/templates/<task_type>.mmd`. The four graph construction principles from paper Appendix A.4 are hardcoded as requirements in the generator prompts:

1. Node atomicity — under 15 tokens per node
2. Encode constraints and structure, never output text
3. Every edge carries an explicit labeled condition
4. Converge on `Check:` nodes before `End`; failed checks loop back

When a codex task's `braid_template` is set but the `.mmd` file is missing, the worker transitions the task to `blocked` with `topology_error=template_missing` and auto-enqueues a claude regeneration task. The reaper re-queues the blocked task once the template lands.

Seeded task types (pass 1, lvc-standard only):

| Task type | Purpose | Solver |
|---|---|---|
| `lvc-implement-operator` | Add or modify a hot-path operator with zero-alloc invariants | codex |
| `lvc-historian-update` | Append an entry to `repo-memory/RECENT_WORK.md` | codex |
| `lvc-reviewer-pass` | Architectural + doc-drift review of a worktree diff | codex (small graph, still benefits from structure) |

## Delivery (worktrees → origin → PR → cleanup)

Codex executes inside a dedicated branch `agent/<task_id>` in an isolated worktree. On a clean smoke-pass the QA worker always emits a **PR body artifact**, and — if the project opts in — pushes the branch to origin and opens a PR via `gh`. A periodic sweep cleans up worktrees whose PRs have been merged or closed.

### 1. PR body artifact (always written)

Path: `artifacts/<target_task_id>/pr-body.md`. Contents:

- `@codex please review this change.` ping on line 1 so GitHub's codex review fires automatically on PR open
- Task id, summary, parent, branch name
- BRAID template + hash + reviewer verdict
- `git log main..HEAD` commit list and `git diff main --stat`
- Last 40 lines of the smoke log (`logs/<driver_task_id>.log`)
- A ready-to-paste `gh pr create --body-file ...` command as a human fallback

Deterministic from task state — no LLM in the loop.

### 2. Auto-push + auto-PR (opt-in per project)

Set `"auto_push": true` on a project entry in `config/orchestrator.json`. On smoke pass the worker will:

1. **Secret scan** over the full `git diff main` + staged + unstaged diff. Any hit (patterns for `.env`, `telegram.json`, `credentials.json`, `BEGIN ... PRIVATE KEY`, `ghp_`/`ghs_`, `xoxb-`, `sk-...`, `AKIA...`) aborts delivery and transitions the target task to `failed/` with `push_failure` set. The worktree is left intact for human inspection.
2. **Auto-commit** any remaining uncommitted changes under a distinct identity (`devmini-orchestrator <devmini-orchestrator@joshorig.com>`) — separate from the human's git identity so automated commits are traceable via `git log --author`.
3. **Push** `agent/<task_id>` to origin. Never pushes `main`.
4. **Open the PR** via `gh pr create --head agent/<task_id> --base main --title <summary> --body-file artifacts/<task_id>/pr-body.md`.

Task state on success: target → `done` with `pushed_at`, `push_commit_sha`, `push_commit_count`, `push_branch`, `pr_body_path`, `pr_url`, `pr_number`, `pr_created_at`. The driver QA task always → `done` because its script succeeded.

Failures:

| Failure stage | Target state | Fields set | Notes |
|---|---|---|---|
| secret scan hit | `failed` | `push_failure=secret-scan hit: <labels>` | worktree preserved |
| `git push` error | `failed` | `push_failure=<git stderr>` | worktree preserved |
| `gh pr create` error | `done` | `pr_create_failure=<reason>` | push succeeded; human opens PR from pr-body.md |
| `gh` not installed or unauthenticated | `done` | `pr_create_failure=...` | same as above |

Defaults: `auto_push: false` on all three projects. Opt in per project when you're ready to let the agent touch origin.

### 3. Worktree cleanup on PR resolution

`orchestrator.py cleanup-worktrees` scans `queue/done/` for tasks that have a `pr_number` and no `cleaned_at`, queries `gh pr view <n> --json state,mergedAt,closedAt` in the project's canonical repo, and on **MERGED** or **CLOSED**:

- Removes the worktree via `git worktree remove --force <path>`
- Deletes the local branch via `git branch -D agent/<task_id>`
- Stamps `cleaned_at`, `pr_final_state`, `pr_merged_at`, `pr_closed_at` on the task JSON in place (task stays in `done/`)

Remote branches are **not** touched — GitHub's per-repo "Delete branch on merge" setting (or the human's one-click delete on close) owns remote cleanup.

Run it yourself anytime: `python3 bin/orchestrator.py cleanup-worktrees [--dry-run]`. A launchd plist (`com.devmini.orchestrator.cleanup-worktrees`) runs it hourly.

## Directory layout

```
bin/
  orchestrator.py       # state machine CLI + dispatcher
  worker.py             # per-slot runner spawned by launchd
  telegram_bot.py       # real python-telegram-bot, long-polling, allowlist-gated
  start_tmux_agents.sh  # creates the Termius-visible session
braid/
  templates/*.mmd           # cached Mermaid reasoning graphs (tracked)
  generators/*.prompt.md    # the prompts that produced them (tracked)
  index.json                # live usage + topology_error counters (gitignored)
config/
  orchestrator.json         # slots, projects, timeouts (tracked)
  telegram.example.json     # template for operators to copy (tracked)
  telegram.json             # real bot token + allowed chat ids (gitignored, chmod 600)
  claude.env                # claude credentials (gitignored, chmod 600)
roles/                      # role-specific prompt fragments per slot
repo-memory/                # this repo's own engineering memory
queue/                      # runtime — per-state subdirs, gitignored
  queued/ claimed/ running/ blocked/
  awaiting-review/ awaiting-qa/
  done/ failed/ abandoned/
state/                      # runtime — claims, locks, transitions.log (gitignored)
logs/                       # runtime — per-task stdout/stderr (gitignored)
reports/                    # runtime — morning/evening markdown reports (gitignored)
artifacts/                  # runtime — worker-produced artifacts (gitignored)
telegram/                   # runtime — legacy stub inbox/outbox (gitignored)
tmux/                       # runtime — session state (gitignored)
```

### `braid/index.json` schema

The live counter file (gitignored) holds one entry per task type:

```json
{
  "<task_type>": {
    "path": "braid/templates/<task_type>.mmd",
    "hash": "sha256:...",
    "generator_model": "claude-opus",
    "created_at": "2026-04-13T...",
    "uses": 0,
    "topology_errors": 0
  }
}
```

`uses` and `topology_errors` let us compute a crude performance-per-dollar analog over time (paper §6). Template hashes must match the actual `.mmd` file or the worker rejects them.

## Running

The orchestrator is launchd-driven. All plists live in `~/Library/LaunchAgents/com.devmini.orchestrator.*.plist`:

| Plist | Cadence | Purpose |
|---|---|---|
| `worker.claude` / `worker.codex` / `worker.qa` | KeepAlive + ThrottleInterval | One task per run |
| `planner` | StartInterval=180s | Enqueues per-project planner ticks |
| `reviewer` | StartInterval=300s | Promotes `awaiting_review` tasks |
| `qa-scheduler` | StartInterval=900s | Promotes `awaiting_qa` tasks |
| `reaper` | StartInterval=60s | Recovers stale claims |
| `cleanup-worktrees` | StartInterval=3600s | Removes worktrees + local branches for merged/closed PRs |
| `regression` | Weekly (lvc-standard) | Full JMH sweep under exclusive project lock |
| `telegram-bot` | KeepAlive | Real bot, long-polling, allowlist-gated |
| `reports-morning` / `reports-evening` | 08:00 / 18:00 | Markdown status + bot push |
| `tmux` | On-load | Termius-visible status/logs/shell session |

Load / unload all at once:

```bash
for p in ~/Library/LaunchAgents/com.devmini.orchestrator.*.plist; do
  launchctl unload "$p" 2>/dev/null
  launchctl load "$p"
done
launchctl list | grep devmini
```

## Mobile control (Telegram)

Long-polling bot at `bin/telegram_bot.py`. No public ports, no webhook. Config at `config/telegram.json` (chmod 600, gitignored) holds the bot token and an allowlist of chat IDs. Unknown chats are logged to `logs/telegram-reject.log` and never see a response. Reports written to `reports/` are auto-pushed every 60s.

Commands: `/status`, `/queue`, `/planner`, `/reviewer`, `/qa`, `/regression <project>`, `/report morning|evening`, `/enqueue <summary>`. Unknown commands return the help string — no arbitrary shell.

## Agent guidance

Future agent tasks that edit files in this repo MUST:

1. Read `repo-memory/CURRENT_STATE.md` and `repo-memory/DECISIONS.md` before proposing changes. Many design choices here are intentional and load-bearing (atomic rename claim, exit-then-respawn workers, engine-field routing) — don't undo them without a decision entry.
2. Append one entry to `repo-memory/RECENT_WORK.md` on completion. Incremental, append-only; no rewrites.
3. Record any real post-mortem in `repo-memory/FAILURES.md` using the schema already in that file.
4. Never commit `config/telegram.json`, `config/claude.env`, or anything under `logs/`, `queue/`, `state/`, `reports/`, `artifacts/`. The `.gitignore` already covers these — don't force-add.
5. Never modify runtime state files directly from inside a task. If a task needs to transition, use the `orchestrator.py transition` CLI, which records an entry in `state/runtime/transitions.log`.
6. Preserve the atomic-rename invariant: tasks move between substates via `os.rename` only. Do not write-then-delete.
7. BRAID templates are append-only in practice — new task types add a new `.mmd`, never mutate an existing one in place. Regeneration writes to `<name>.mmd.tmp` then `os.rename`s.
8. The token-savior MCP is retrieval-only. Do not route task-state reads/writes through it.

## Links

- BRAID paper: [arXiv:2512.15959](https://arxiv.org/abs/2512.15959) (Amcalar & Cinar, 2025)
- Engineering memory skills: `/Volumes/devssd/repos/skills/engineering-memory/`
- Canonical repos served by this orchestrator: `lvc-standard`, `dag-framework`, `trade-research-platform` (see `config/orchestrator.json`)

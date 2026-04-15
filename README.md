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

# delivery ticks + hygiene
python3 bin/orchestrator.py features [--status open]
python3 bin/orchestrator.py pr-sweep [--dry-run]
python3 bin/orchestrator.py feature-finalize [--dry-run]
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

## Delivery (feature branches → task PRs → feature PR → human)

Codex still executes inside isolated `agent/<task_id>` worktrees, but delivery now happens in two stages: each task PR targets a shared `feature/<id>` branch, then a single feature PR targets `main` once every child task has landed. `pr-sweep` only auto-merges PRs whose base starts with `feature/`; feature PRs remain human-reviewed and human-merged.

### 1. Planner emits feature sets

Each planner tick creates a feature record under `state/features/<feature_id>.json` and binds emitted codex slices to that `feature_id`. The git branch itself is lazy-created by the first codex worker that needs it. Feature records track `child_task_ids`, `status`, and the eventual `final_pr_number` / `final_pr_url`.

### 2. Codex task PRs target `feature/<id>`

On smoke pass the QA worker always writes `artifacts/<task_id>/pr-body.md`, uses the distinct `devmini-orchestrator <devmini-orchestrator@joshorig.com>` commit identity, and if `auto_push` is enabled pushes `agent/<task_id>` plus opens:

```bash
gh pr create --head agent/<task_id> --base feature/<id> --title "<summary>" --body-file artifacts/<task_id>/pr-body.md
```

The PR body still carries the `@codex` mention, BRAID provenance, diff stat, smoke log tail, and a manual fallback command. Task JSON is stamped with `push_base_branch`, `push_branch`, `pr_number`, and `pr_url` when the PR opens.

Push safety now has two layers:

- `repo-memory/*.md` writes are still hard-blocked on the built-in secret detector before historian auto-commit or memory-synthesis apply.
- Generic branch pushes only hard-block on secret findings when `detect-secrets-hook` is installed and the target repo has a `.secrets.baseline`. The older entropy/path heuristic remains advisory-only in the push log so path-like diffs cannot fail delivery on their own.

### 3. Auto-merge to feature branch when green

`python3 bin/orchestrator.py pr-sweep [--dry-run]` polls open task PRs from `queue/done/`. When a PR is mergeable, free of actionable comments, free of unresolved allowlisted-bot review threads, and its base ref starts with `feature/`, `pr-sweep` runs `gh pr merge --squash --delete-branch`. It never auto-merges a PR whose base is `main`. Formal approval is not required for task PRs; human review is reserved for the final feature PR to `main`.

On `gh` auth problems or HTTP 401s, the sweep logs and skips without mutating task or feature state.

### 4. pr-sweep addresses PR feedback autonomously

When a task PR on a feature branch needs attention, `pr-sweep` enqueues a codex `pr-address-feedback` slice bound to the same `feature_id`. That BRAID graph handles rebases, failed-check repairs, review-comment edits, and follow-up pushes without human intervention unless the configured feedback-round ceiling is exhausted. Dispatch fires in four cases:

- **Actionable review comments** from auto-handle authors (`chatgpt-codex-connector`, `copilot`, `github-advanced-security`).
- **Conflicts or stale bases** — `gh` reports `mergeable=CONFLICTING` or `mergeStateStatus` in `DIRTY`/`BEHIND`.
- **Failed required checks** — `gh` reports `mergeStateStatus=BLOCKED` or `UNSTABLE` and `statusCheckRollup` contains failed / errored / timed-out checks. The failing check names and URLs are injected into the repair prompt.
- **Drift** — `gh` still says `MERGEABLE`, but the worktree HEAD is `drift_threshold` or more commits behind its base (default 5, optional per-project override in `config/orchestrator.json`). `pr-sweep` synthesises `mergeStateStatus=BEHIND` so the sync fires before the delta widens.

Allowlisted bot top-level comments are not treated as actionable just because of author alone; summary/praise comments such as "Didn't find any major issues" are ignored unless they contain an explicit finding marker. On conflict/drift dispatch the task's `engine_args.conflict_preview` carries a `[CONFLICT PREVIEW]` block (conflict list + diff stats + recent base log, 4000-char budget) that `worker.py` injects into the codex prompt so the solver sees the rebase surface before it starts. The parent task's `pr_sweep.conflict_task_id` pins the active guard slice; subsequent sweeps of the same parent skip dispatch while that guard task is still in `queued`/`claimed`/`running`, so a slow codex rebase can't triple-enqueue.

The same sweep also watches feature PRs that are already in `finalizing`. If a feature->`main` PR picks up actionable comments, unresolved allowlisted-bot review threads, enters `CONFLICTING` / `DIRTY` / `BEHIND`, or goes `BLOCKED` / `UNSTABLE` with failed required checks, `pr-sweep` enqueues a follow-up codex slice on the existing `feature/<id>` line. That slice merges `origin/main` into the feature branch when needed, repairs the failed workflow or review issue, and lands through the normal task-PR-to-feature flow before the final feature PR is reconsidered.

### 5. feature-finalize opens the feature->main PR

`python3 bin/orchestrator.py feature-finalize [--dry-run]` scans open features and waits until every child task is:

- in `queue/done/`
- stamped with `cleaned_at`
- stamped with `pr_final_state=MERGED`

Once ready, it aggregates the child PR evidence into `artifacts/<feature_id>/final-pr-body.md`, confirms `feature/<id>` exists on origin, and opens:

```bash
gh pr create --base main --head feature/<id> --title "<feature summary>" --body-file artifacts/<feature_id>/final-pr-body.md
```

This PR is for human review only. The orchestrator never calls `gh pr merge` on a feature PR, but it can still service review comments and merge conflicts on that PR by dispatching follow-up codex work back onto the same feature branch.

### 6. Cleanup on feature merge

`python3 bin/orchestrator.py cleanup-worktrees [--dry-run]` still removes task worktrees and local `agent/<task_id>` branches once task PRs are merged or closed. It also watches features in `finalizing`: when the feature PR is merged it deletes the local `feature/<id>` branch and stamps the feature `merged`; when the feature PR is closed without merge it stamps the feature `abandoned`.

Remote branches are never deleted from the orchestrator side. GitHub settings or humans own remote cleanup.

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
  gh-token.example          # template for GitHub PAT file (tracked)
  gh-token                  # real GitHub token, single-line, gitignored, chmod 600
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
| `worker.claude` / `worker.qa` | KeepAlive + ThrottleInterval | One task per run |
| `worker.codex-{1..6}` | KeepAlive + ThrottleInterval=15 | 6-slot codex fleet |
| `planner` | StartInterval=180s | Enqueues per-project planner ticks |
| `reviewer` | StartInterval=300s | Promotes `awaiting_review` tasks |
| `qa-scheduler` | StartInterval=900s | Promotes `awaiting_qa` tasks |
| `reaper` | StartInterval=60s | Recovers stale claims |
| `pr-sweep` | StartInterval=600 | Auto-merge task PRs into feature branches + address PR feedback |
| `feature-finalize` | StartInterval=600 | Opens feature->main PRs |
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

### Durable gh auth

The orchestrator now loads `GH_TOKEN` from `config/gh-token` automatically in
its Python entrypoints before any `gh` invocation. Use a classic PAT or a
fine-grained PAT with the repository scopes your workflows need.

Setup:

```bash
cp config/gh-token.example config/gh-token
chmod 600 config/gh-token
$EDITOR config/gh-token
```

Interactive shell:

```bash
source bin/gh_env.sh
gh auth status
```

Optional launchd-wide export for tools that do not go through the orchestrator
Python entrypoints:

```bash
source bin/gh_env.sh
launchctl setenv GH_TOKEN "$GH_TOKEN"
```

## Mobile control (Telegram)

Long-polling bot at `bin/telegram_bot.py`. No public ports, no webhook. Config at `config/telegram.json` (chmod 600, gitignored) holds the bot token and an allowlist of chat IDs. Unknown chats are logged to `logs/telegram-reject.log` and never see a response. Reports written to `reports/` are auto-pushed every 60s.

Commands: `/status`, `/queue`, `/planner`, `/reviewer`, `/qa`, `/cleanup`, `/ask <question>`, `/regression <project>`, `/report morning|evening`, `/enqueue <summary>`. Unknown commands return the help string — no arbitrary shell.

`/ask` is read-only. It gathers live orchestrator state, selected logs, roadmap context, and PR metadata when a PR number is mentioned, then asks the local LLM to answer from that evidence. It does not execute arbitrary shell or mutate runtime state.

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

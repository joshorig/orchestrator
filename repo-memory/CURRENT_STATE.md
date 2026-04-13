# devmini orchestrator — CURRENT STATE

_This is the orchestrator's own engineering memory. Updated incrementally; never rewritten wholesale._

## What this system is

An always-on, launchd-driven task orchestrator that runs on a 16GB Mac mini and coordinates agent-driven engineering work across a small set of canonical repos. It pairs a claude "generator" slot with a codex "solver" slot following the BRAID architecture, plus a non-LLM QA slot for contract verification. Workers are short-lived: each one claims a single task, executes it inside an isolated worktree, and exits so launchd can respawn a fresh process.

## What exists today (pass 1 slice — lvc-standard only)

- Full state machine in `bin/orchestrator.py` with all nine task states, atomic rename-based claim, append-only `transitions.log`, and dead-pid reaper.
- `bin/worker.py` implements all three slots (`claude`, `codex`, `qa`) plus planner decomposition and the slice post-validation classifier (`classify_slice`). One task per run, timeout-gated from `config/orchestrator.json`.
- Real `bin/telegram_bot.py` (python-telegram-bot, long-polling, allowlist-gated) with command handlers and 60-second report-push job.
- 13 launchd plists installed under `~/Library/LaunchAgents/com.devmini.orchestrator.*`. Tmux session plist exists and works; Termius sessions attach to `status`, `logs`, and `shell` windows.
- Three BRAID templates seeded for lvc-standard: `lvc-implement-operator`, `lvc-historian-update`, `lvc-reviewer-pass`. Generator prompts live alongside in `braid/generators/`.
- QA contract on lvc-standard: `qa/smoke.sh` (unit tests, <2 min) + `qa/regression.sh` (full JMH sweep, reduced fork/warmup to fit 8h cap). Regression runs under an exclusive per-project advisory lock; smoke is per-change.
- Synthetic regression-failure harness (`qa/regression-sim.sh` inside lvc-standard) that exercises the alert pipeline without a real 4-hour JMH run.
- `repo-memory/` seeded on lvc-standard with all six files. Historian task type is proven end-to-end (61 uses, 0 topology errors as of 2026-04-13).

## What's proven (vertical slice verification, 2026-04-13)

- Happy path: enqueue → claim → worktree → solve → `awaiting-review` → reviewer → `awaiting-qa` → smoke → `done`. Confirmed on the historian task type.
- Stale-claim recovery: killing a codex worker mid-run correctly routes the task back to `queued/` within one reaper tick.
- Topology-error path: removing a template file triggers `blocked → template-regenerate → queued` with the full loop visible in `transitions.log`.
- Telegram: allowlist rejection logged; report push works end-to-end.
- Regression lock: codex implementer tasks correctly wait on the shared lock while regression holds the exclusive lock.
- Regression failure: synthetic harness produces the blocked task + Telegram alert + hard stop on further implementer tasks. Confirmed.

## What's explicitly deferred to pass 2

- **Per-repo memory for `dag_framework` and `trade-research-platform`** — neither has a `repo-memory/` directory yet.
- **Playwright QA contract for trade-research-platform** (application type, needs browser smoke).
- **BRAID pre-flight Check node** that runs a failing test against clean canonical `main` before accepting "unrelated blocker" as a terminal state. Lesson from `lvc-standard` FAILURES.md 2026-04-13 entry.
- **Automated BRAID graph linter** (node token counts, labeled edges, terminal Check nodes). Currently only manual spot-check during seeding.
- **Log rotation** for `logs/*.log`. 20MB accumulated in one day.
- **Production PPD dashboard** over `braid/index.json` counters.
- **Fine-tuned Architect model** (BRAID paper §7 future work).
- **Sandboxing** claude/codex processes beyond `--dangerously-skip-permissions`.

## Live BRAID stats (pass-1 measurements, 2026-04-13)

| Template | Uses | Topology errors | Notes |
|---|---|---|---|
| `lvc-historian-update` | 61 | 0 | Rock solid; small graph |
| `lvc-reviewer-pass` | 86 | 1 | Single error traced to malformed worktree diff |
| `lvc-implement-operator` | 14 | 4 | Highest error rate; investigation in pass-1 showed 1 of the 4 was a worktree-flake misdiagnosis (see lvc-standard FAILURES 2026-04-13). Real traversal error rate ≤ 3/14 |

These counters are live and mutate on every task claim, so the file is gitignored. Query via `cat braid/index.json` or via the status CLI.

## Hard invariants (do not violate)

- **Atomic rename claims**: tasks move between substates with `os.rename` only. Never write-then-delete.
- **Engine field**: every task carries an explicit `engine` field (`claude`, `codex`, or `qa`). Worker slots filter on it; there is no implicit routing.
- **Bounded workers**: one task per run, then `sys.exit(0)`. Launchd handles throttling.
- **No shell injection from Telegram**: unknown commands return the help string; never execute arbitrary shell.
- **Secrets never committed**: `config/telegram.json`, `config/claude.env`, anything under `logs/`, `queue/`, `state/`, `reports/` is gitignored.
- **BRAID templates are append-only**: regenerating writes to `<name>.mmd.tmp` then renames atomically. Never mutate an existing template in place.

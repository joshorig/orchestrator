# devmini orchestrator — CURRENT STATE

_This is the orchestrator's own engineering memory. Updated incrementally; never rewritten wholesale._

## What this system is

An always-on, launchd-driven task orchestrator that runs on a 16GB Mac mini and coordinates agent-driven engineering work across a small set of canonical repos. It pairs a claude "generator" slot with a codex "solver" slot following the BRAID architecture, plus a non-LLM QA slot for contract verification. Workers are short-lived: each one claims a single task, executes it inside an isolated worktree, and exits so launchd can respawn a fresh process.

## What exists today (pass 1+2 — three-repo slice + feature-branch delivery)

- Full state machine in `bin/orchestrator.py` with all nine task states, atomic rename-based claim, append-only `transitions.log`, and dead-pid reaper.
- `bin/worker.py` implements all three slots (`claude`, `codex`, `qa`) plus planner decomposition, slice post-validation classifier, and the pr-feedback task mode. One task per run, timeout-gated from `config/orchestrator.json`.
- Real `bin/telegram_bot.py` (python-telegram-bot, long-polling, allowlist-gated) with command handlers and 60-second report-push job.
- **Feature-branch delivery model** (commit `cc23abd`, hardened in 2026-04-14 worktree): `tick_planner` emits feature headers under `state/features/`, codex tasks inherit `feature_id`, `atomic_claim` serializes siblings within a feature, task PRs target `feature/<id>`, `pr-sweep` auto-merges clean task PRs and enqueues pr-feedback tasks for actionable review comments, failed required checks, drift, and conflicts, and `finalizing` feature PRs can also trigger follow-up codex slices for review comments, failed checks, or `main` merge conflicts while `feature-finalize` keeps the feature→main PR itself human-reviewed.
- **6-slot codex fleet**: `worker.codex` + `worker.codex-{2..6}` launchd plists all invoke `worker.py codex`; per-feature serialization means siblings queue while features run in parallel. No per-project cap — any one project can theoretically occupy all 6 slots.
- 20 launchd plists installed under `~/Library/LaunchAgents/com.devmini.orchestrator.*` (6× codex workers, claude, qa, telegram-bot, planner, reviewer, qa-scheduler, reaper, regression, cleanup-worktrees, pr-sweep, feature-finalize, reports-morning, reports-evening, tmux). All loaded; no error states.
- **Nine BRAID templates** covering all three canonical projects plus cross-cutting flows: `lvc-implement-operator` (regenerated flat in commit `74d50e0`, R4-clean with distinct `ReviseCheck<N>` per gate), `lvc-historian-update`, `lvc-reviewer-pass`, `dag-implement-node`, `dag-historian-update`, `trp-implement-pipeline-stage`, `trp-ui-component`, `pr-address-feedback`, `memory-synthesis`. Matching generator prompts in `braid/generators/`. All 9 lint-clean under R1-R7 (one pre-existing R7 warning on `lvc-reviewer-pass`, non-blocking).
- **Planner dispatch wired for all four task types per project** (`tick_planner` + `classify_slice` in commit `74d50e0`): seed tasks get project-specific historian templates, implementer children get project-specific implementer templates picked by `classify_slice` positive-keyword + anti-pattern heuristics (24 new inline doctests).
- **PPD dashboard** (`bin/ppd_report.py`, commit `74d50e0`): reads `braid/index.json` + `logs/*.log`, emits `reports/ppd-<YYYYMMDD>.md` with per-template error rates + per-slot token counts + crude `tasks_completed / total_tokens` PPD. Plumbed into morning report push.
- **Memory synthesis automation** (`tick_memory_synthesis` in `orchestrator.py`, commit `74d50e0`): walks projects weekly, enqueues a `memory-synthesis` claude task when `repo-memory/CURRENT_STATE.md` mtime > 7 days. Delta-only updates enforced by the graph's Check gates.
- **Automated BRAID lint gate** (`orchestrator.py:lint_template`) wired into `braid_template_write` so no regenerated template can land if it violates any of 7 rules (R1 atomicity, R2 labeled edges, R3 terminal `Check:` before `End`, R4 distinct Revise per gate, R5 reachability, R6 syntax, R7 repo-literal heuristic). CLI: `orchestrator.py lint-templates [--template <name> | --all]`. Known-bad `lvc-implement-operator` fails R4 as expected; all other live templates pass.
- **BRAID hardening pass** (2026-04-14 worktree): `pr-sweep` now persists running-guard telemetry to `state/runtime/pr-sweep-metrics.json`, ignores non-actionable bot summary comments, and dispatches repair slices for failed required checks; `tick-template-audit` writes `reports/template-audit-<date>.md`; `reap()` can auto-enqueue template regeneration when recent topology-error rate stays high; codex dispatch pauses for that template until a new hash lands.
- **Runtime event stream + log rotation** (2026-04-14 worktree): task transitions and agent-status changes append structured rows to `state/runtime/events.jsonl`; `rotate_logs()` gzips stale `logs/*.log` after 7 days and evicts oldest archives to keep retained bytes under the 1GB cap, with `cleanup-worktrees` invoking the sweep on its normal cadence.
- **repo-memory secrets gate** (2026-04-14 worktree): historian/memory-synthesis writes to `repo-memory/*.md` are blocked on secret-like content before commit/apply, and failures emit Telegram-pushable markdown alerts under `reports/`.
- **Generic push secret scan softened** (2026-04-14 worktree): the old entropy-based diff scan is now advisory-only for normal code pushes after it falsely flagged git pathnames as secrets; hard blocking for generic branch pushes now requires `detect-secrets-hook` plus a repo `.secrets.baseline`.
- **Per-repo memory + qa contracts on all three canonical repos**:
  - `lvc-standard` — `qa/smoke.sh` (unit tests + `:benchmarks:jmhSmokeCheck` alloc gate, <4 min) + `qa/regression.sh` (full JMH sweep under exclusive per-project lock).
  - `dag_framework` — `qa/smoke.sh` + `qa/regression.sh` + `qa/jmh_diff.py`; memory files seeded.
  - `trade-research-platform` — `qa/smoke.sh` runs UI typecheck/lint/contract/vitest + Playwright chromium smoke + a11y specs + `:platform-runtime:jmhSmokeAll`; memory files seeded.
- Synthetic regression-failure harness (`qa/regression-sim.sh` inside lvc-standard) that exercises the alert pipeline without a real 4-hour JMH run.
- Historian task type is proven end-to-end across many runs; pr-address-feedback is hand-authored but has zero live runs yet.

## What's proven (vertical slice verification, 2026-04-13)

- Happy path: enqueue → claim → worktree → solve → `awaiting-review` → reviewer → `awaiting-qa` → smoke → `done`. Confirmed on the historian task type.
- Stale-claim recovery: killing a codex worker mid-run correctly routes the task back to `queued/` within one reaper tick.
- Topology-error path: removing a template file triggers `blocked → template-regenerate → queued` with the full loop visible in `transitions.log`.
- Telegram: allowlist rejection logged; report push works end-to-end.
- Regression lock: codex implementer tasks correctly wait on the shared lock while regression holds the exclusive lock.
- Regression failure: synthetic harness produces the blocked task + Telegram alert + hard stop on further implementer tasks. Confirmed.

## What's explicitly deferred to pass 3

- **Log rotation** for `logs/*.log`. Still accumulating unbounded (894+ files at last check).
- **Full-stack compose integration in TRP regression** — current regression runs Playwright against the Vite dev server only. Real JVM backend + dependencies are not exercised end-to-end. Prompt authored; not yet executed.
- **Pattern D: auto-merge feature→main** — deliberate hold until pattern C has observation time in the wild. Failure mode is "bad change on main, no human saw it."
- **Fine-tuned Architect model** (BRAID paper §7 future work).
- **Sandboxing** claude/codex processes beyond `--dangerously-skip-permissions`.

## Known concerns (active, not yet deferred)

- **`lvc-implement-operator` legacy error count**: the 14 topology errors in `braid/index.json` were accumulated under the old R4-violating subgraph shape. The template was regenerated in commit `74d50e0` into a flat shape with distinct `ReviseCheck<N>` per gate and is lint-clean. Counter is not reset by design (append-only stat); watch for the counter to **stop growing** on the next operator task rather than expecting a drop. If it grows, the generator prompt fix did not fully take and further investigation is needed.
- **TRP regression does not exercise the real stack**: Playwright webServer is the Vite dev server, not a compose-started backend. JVM test + JMH are run but not integrated with a live UI. A hand-off prompt has been drafted to add a `compose-stack` Playwright project + EXIT-trapped compose lifecycle stage to `qa/regression.sh`. Not yet executed.
- **Documentation drift on task-PR merge policy**: the implementation auto-merges clean feature-branch task PRs without requiring a formal `APPROVED` review decision; the human review gate is the final feature PR to `main`. README + repo-memory should describe that contract explicitly to avoid future "fixes" that add an approval gate the runtime never intended.

## Live BRAID stats (pass-2 measurements, 2026-04-13 post-regen)

| Template | Uses | Topology errors | Notes |
|---|---|---|---|
| `lvc-historian-update` | 116 | 0 | Rock solid; small graph; unchanged generator |
| `lvc-reviewer-pass` | 159 | 1 | Single lifetime error traced to a malformed worktree diff |
| `lvc-implement-operator` | 30 | 14 | **Regenerated flat in commit `74d50e0`**, new hash `a513adbf...`. 14 errors are legacy (old subgraph shape); counter should stop growing on next operator run |
| `dag-implement-node` | 0 | 0 | Hand-authored; no live runs yet |
| `dag-historian-update` | 0 | 0 | Hand-authored; no live runs yet |
| `trp-implement-pipeline-stage` | 0 | 0 | Hand-authored; no live runs yet |
| `trp-ui-component` | 0 | 0 | Hand-authored; no live runs yet |
| `pr-address-feedback` | 0 | 0 | Hand-authored; no live runs yet |
| `memory-synthesis` | 0 | 0 | Hand-authored; weekly schedule, no runs yet |

`braid/index.json` is live and mutates on every task claim, so it is gitignored (the hash is tracked in the file, the counters are not). Query via `cat braid/index.json` or via the status CLI.

## Hard invariants (do not violate)

- **Atomic rename claims**: tasks move between substates with `os.rename` only. Never write-then-delete.
- **Engine field**: every task carries an explicit `engine` field (`claude`, `codex`, or `qa`). Worker slots filter on it; there is no implicit routing.
- **Bounded workers**: one task per run, then `sys.exit(0)`. Launchd handles throttling.
- **No shell injection from Telegram**: unknown commands return the help string; never execute arbitrary shell.
- **Secrets never committed**: `config/telegram.json`, `config/claude.env`, anything under `logs/`, `queue/`, `state/`, `reports/` is gitignored.
- **BRAID templates are append-only**: regenerating writes to `<name>.mmd.tmp` then renames atomically. Never mutate an existing template in place.

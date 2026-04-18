# devmini orchestrator — CURRENT STATE

_This is the orchestrator's own engineering memory. Updated incrementally; never rewritten wholesale._

## What this system is

An always-on, launchd-driven task orchestrator that runs on a 16GB Mac mini and coordinates agent-driven engineering work across a small set of canonical repos. It pairs a claude "generator" slot with a codex "solver" slot following the BRAID architecture, plus a non-LLM QA slot for contract verification. Workers are short-lived: each one claims a single task, executes it inside an isolated worktree, and exits so launchd can respawn a fresh process.

## Current priority reset (2026-04-15)

The roadmap has been reprioritized around autonomous-runtime correctness rather than more throughput or more BRAID surface area. The immediate priorities are now:

- typed blocker/reason contracts
- deterministic task reset and retry semantics
- event-sourced workflow diagnosis
- declarative self-heal policy
- synthetic canary workflows
- environment normalization
- a guarded self-repair lane for orchestrator bugs

Parallelism, visual graph ingestion, and broader fleet rollout stay secondary until these land.

## What exists today (pass 1+2 — three-repo slice + feature-branch delivery)

- Full state machine in `bin/orchestrator.py` with all nine task states, atomic rename-based claim, append-only `transitions.log`, and dead-pid reaper.
- `bin/worker.py` implements all three slots (`claude`, `codex`, `qa`) plus planner decomposition, slice post-validation classifier, and the pr-feedback task mode. One task per run, timeout-gated from merged orchestrator config (`config/orchestrator.example.json` plus local override).
- Real `bin/telegram_bot.py` (python-telegram-bot, long-polling, allowlist-gated) with command handlers and 60-second report-push job.
- **Feature-branch delivery model** (commit `cc23abd`, hardened in 2026-04-14 worktree): `tick_planner` emits feature headers under `state/features/`, codex tasks inherit `feature_id`, `atomic_claim` serializes siblings within a feature, task PRs target `feature/<id>`, `pr-sweep` auto-merges clean task PRs and enqueues pr-feedback tasks for actionable review comments, failed required checks, drift, and conflicts, and `finalizing` feature PRs can also trigger follow-up codex slices for review comments, failed checks, or `main` merge conflicts while `feature-finalize` keeps the feature→main PR itself human-reviewed.
- **6-slot codex fleet**: `worker.codex` + `worker.codex-{2..6}` launchd plists all invoke `worker.py codex`; per-feature serialization means siblings queue while features run in parallel. No per-project cap — any one project can theoretically occupy all 6 slots.
- 24 launchd plists installed under `~/Library/LaunchAgents/com.devmini.orchestrator.*` (6× codex workers, claude, qa, telegram-bot, planner, reviewer, qa-scheduler, reaper, regression, cleanup-worktrees, pr-sweep, feature-finalize, workflow-check, canary-workflows, reports-morning, reports-evening, tmux, memory-synth). All loaded; no error states.
- **Twelve BRAID templates** covering all three canonical projects plus cross-cutting flows: `lvc-implement-operator`, `lvc-historian-update`, `lvc-reviewer-pass`, `dag-implement-node`, `dag-historian-update`, `trp-historian-update`, `trp-implement-pipeline-stage`, `trp-ui-component`, `trp-reviewer-pass`, `pr-address-feedback`, `memory-synthesis`, and `orchestrator-self-repair`. Matching generator prompts live under `braid/generators/`. TRP now has its own historian and reviewer contracts instead of falling back to lvc-shaped review assumptions. All live templates are lintable under R1-R7 (one pre-existing R7 warning on `lvc-reviewer-pass`, non-blocking).
- **Planner dispatch wired for all four task types per project** (`tick_planner` + `classify_slice` in commit `74d50e0`): seed tasks get project-specific historian templates, implementer children get project-specific implementer templates picked by `classify_slice` positive-keyword + anti-pattern heuristics (24 new inline doctests).
- **PPD dashboard** (`bin/ppd_report.py`, commit `74d50e0`): reads `braid/index.json` + `logs/*.log`, emits `reports/ppd-<YYYYMMDD>.md` with per-template error rates + per-slot token counts + crude `tasks_completed / total_tokens` PPD. Plumbed into morning report push.
- **Memory synthesis automation** (`tick_memory_synthesis` in `orchestrator.py`, commit `74d50e0`): walks projects weekly, enqueues a `memory-synthesis` claude task when `repo-memory/CURRENT_STATE.md` mtime > 7 days. Delta-only updates enforced by the graph's Check gates.
- **Automated BRAID lint gate** (`orchestrator.py:lint_template`) wired into `braid_template_write` so no regenerated template can land if it violates any of 7 rules (R1 atomicity, R2 labeled edges, R3 terminal `Check:` before `End`, R4 distinct Revise per gate, R5 reachability, R6 syntax, R7 repo-literal heuristic). CLI: `orchestrator.py lint-templates [--template <name> | --all]`. Known-bad `lvc-implement-operator` fails R4 as expected; all other live templates pass.
- **BRAID hardening pass** (2026-04-14 worktree): `pr-sweep` now persists running-guard telemetry to `state/runtime/pr-sweep-metrics.json`, ignores non-actionable bot summary comments, and dispatches repair slices for failed required checks; `tick-template-audit` writes `reports/template-audit-<date>.md`; `reap()` can auto-enqueue template regeneration when recent topology-error rate stays high; codex dispatch pauses for that template until a new hash lands.
- **Runtime event stream + log rotation** (2026-04-14 worktree): task transitions and agent-status changes append structured rows to `state/runtime/events.jsonl`; `rotate_logs()` gzips stale `logs/*.log` after 7 days and evicts oldest archives to keep retained bytes under the 1GB cap, with `cleanup-worktrees` invoking the sweep on its normal cadence.
- **Secret scan advisory-only** (runtime updated 2026-04-15): entropy hits, repo-memory findings, and `detect-secrets-hook` output are all logged for operator awareness, but none of them block historian/memory-synthesis writes, auto-commit, or generic branch pushes.
- **Workflow checker** (2026-04-15 worktree): `workflow-check` runs every 30 minutes, diagnoses blocked feature workflows, reports to Telegram, and performs bounded self-heal actions from the declarative repair-policy table. It now also surfaces canary freshness/staleness incidents and environment degradation as first-class issues.
- **Environment normalization** (2026-04-15 worktree): `env-health` validates required binaries, `GH_TOKEN`, launchd jobs, canonical repo cleanliness/fetchability, worktree-root presence, and per-project QA script paths. Planner/canary dispatch and task claiming skip projects with blocking environment issues instead of burning live work on a degraded host.
- **Guarded self-repair lane** (2026-04-15 worktree): `enqueue-self-repair` opens a bounded feature on the orchestrator repo itself using the `orchestrator-self-repair` template and the orchestrator repo's own `qa/smoke.sh` / `qa/regression.sh`, but still leaves the final feature PR to human review before merge to `main`.
- **Runtime-owned operator/config surface** (2026-04-18 worktree): credentials are file-backed and can be read/written through `orchestrator.py creds`, Telegram operator approval lives in `state/runtime/allowlist.json`, and new repos can be added through `orchestrator.py register-project` instead of hand-editing tracked docs or local config by hand.
- **Structured runtime metrics** (2026-04-18 worktree): `state/runtime/metrics.jsonl` now records queue, environment, feature-frontier, workflow-check, and agent-status metrics; reports/status consume the latest snapshot instead of relying only on narrative summaries.
- **Dedicated security + architecture review gates** (2026-04-18 worktree): internal review now runs explicit council-backed `security-review-pass` and `architectural-fit-review-pass` BRAID gates after the functional/adversarial review pair and before QA. Substantive failures route into reviewer feedback rework; gate infrastructure failures stay in the retryable self-heal path rather than becoming a new terminal stop.
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

- **Control-plane contract is still too implicit**: task/feature/blocker semantics are split across mutable JSON fields, free-text logs, prompt trailers, and code-path-specific heuristics. This is the largest blocker to reliable autonomous operation.
- **Retry semantics are lossy/stale**: requeued tasks can carry historical terminal metadata into a new live attempt. That is survivable for humans but dangerous for automation and reporting.
- **Workflow diagnosis/repair is now materially closer to autonomous operation**: typed blockers, event-backed summaries, declarative repair policy, canary incidents, bounded environment repair, and the council-backed self-repair lane all land in one control plane. Remaining work is mostly breadth and operational hardening, not missing first-class repair paths.
- **Environment drift is no longer diagnosis-only**: `workflow-check` can now invoke bounded environment repair actions (`GH_TOKEN` reload, launch agent reload, worktrees-root creation, git fetch-state repair) before reopening the loop through self-repair. Irreparable cases still surface explicitly for guarded follow-up.
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
| `trp-historian-update` | 0 | 0 | Hand-authored; no live runs yet |
| `trp-implement-pipeline-stage` | 0 | 0 | Hand-authored; no live runs yet |
| `trp-reviewer-pass` | 0 | 0 | Hand-authored; no live runs yet |
| `trp-ui-component` | 0 | 0 | Hand-authored; no live runs yet |
| `pr-address-feedback` | 0 | 0 | Hand-authored; no live runs yet |
| `memory-synthesis` | 0 | 0 | Hand-authored; weekly schedule, no runs yet |

`braid/index.json` is live runtime state and mutates on task claims and template lifecycle events, so it is intentionally untracked and gitignored. Query via `cat braid/index.json` or via the status CLI.

## Hard invariants (do not violate)

- **Atomic rename claims**: tasks move between substates with `os.rename` only. Never write-then-delete.
- **Engine field**: every task carries an explicit `engine` field (`claude`, `codex`, or `qa`). Worker slots filter on it; there is no implicit routing.
- **Bounded workers**: one task per run, then `sys.exit(0)`. Launchd handles throttling.
- **No shell injection from Telegram**: unknown commands return the help string; never execute arbitrary shell.
- **Secrets never committed**: `config/telegram.json`, `config/claude.env`, anything under `logs/`, `queue/`, `state/`, `reports/` is gitignored.
- **BRAID templates are append-only**: regenerating writes to `<name>.mmd.tmp` then renames atomically. Never mutate an existing template in place.

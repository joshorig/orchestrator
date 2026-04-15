# devmini orchestrator — Roadmap

_Append-only. Top-to-bottom is priority order. Status mutates in place; entries are never deleted. DONE/ABANDONED stay for history and are skipped by the planner._

**Theme:** Harden the orchestrator from "working vertical slice" into a trusted autonomous runtime. As of 2026-04-15, autonomy hardening takes precedence over throughput expansion, visual BRAID futures, and fleet widening. The next phase is to make the control plane typed, deterministic, and self-healing before we ask it to do more.

**Critical path:** R-017/R-018 (typed workflow contracts + task reset semantics) → R-019/R-020 (event-sourced diagnosis + repair policy) → R-021 (synthetic canary workflows) → R-022 (environment normalization) → R-023 (guarded orchestrator self-repair lane) → revisit R-003/R-011/R-013 once the runtime is trustworthy.

## Active

### [R-017] Typed blocker taxonomy + workflow contracts
- **Status:** TODO
- **Feature id:** null
- **Goal:** Replace free-text failure interpretation with explicit machine-checked blocker classes so planner, checker, reaper, and Telegram reports reason over the same contract.
- **Scope:** Introduce canonical reason codes for task failure/block (`planner_contract_error`, `template_missing_edge`, `runtime_env_dirty`, `delivery_auth_expired`, `qa_baseline_red`, `task_state_corruption`, etc.); stamp them on task/feature records at transition time; make `workflow-check`, `pr-sweep`, and retry logic consume these codes instead of string matching; add transition/doctest coverage for every code path.
- **Out of scope:** Model-side ontology learning, cross-repo shared taxonomies outside this orchestrator, human-friendly root-cause prose generation beyond a short summary layer.
- **Acceptance:** No repair path depends on parsing arbitrary stderr/log prose; `workflow-check` reports exact blocker classes with confidence; Telegram output names the blocker code and attempted repair action deterministically.
- **Depends on:** none.
- **Notes:** This is the highest-leverage fix for the recent secret-scan, `BRAID_REFINE`, and planner crash incidents. Without typed contracts, autonomy remains prompt-driven rather than runtime-driven.

### [R-018] Deterministic task reset / retry API
- **Status:** TODO
- **Feature id:** null
- **Goal:** Retrying a task must produce a clean new attempt state instead of carrying stale terminal fields that can mislead later automation.
- **Scope:** Add a first-class reset/retry path that archives prior terminal metadata (`finished_at`, `failure_reason`, `false_blocker_claim`, stale blocker fields, prior repair attempt stamps) into attempt history and clears the live task record before requeue; thread the same semantics through manual transitions, workflow-check retries, and template-refine re-dispatch.
- **Out of scope:** Database migration, per-field historical diff rendering, multi-host idempotency tokens.
- **Acceptance:** A retried task in `queued/` or `running/` contains only live-attempt fields plus archived attempt history; downstream tools never have to guess whether a field is stale.
- **Depends on:** [R-017].
- **Notes:** This removes the current "cosmetic but dangerous" stale-state debt visible on retried tasks.

### [R-019] Event-sourced workflow diagnosis
- **Status:** TODO
- **Feature id:** null
- **Goal:** Workflow health and frontier computation should be reconstructed from the append-only event stream and transition ledger, not inferred from mutable JSON blobs with partial state.
- **Scope:** Promote `state/runtime/events.jsonl` + `transitions.log` into the canonical diagnostic substrate; add an event-backed workflow summarizer that computes frontier task, blocker ancestry, repair history, and stuckness duration; make `workflow-check` and status/report paths consume that summary.
- **Out of scope:** Full external event store, remote log shipping, BI dashboards.
- **Acceptance:** For any open feature, one command can explain "what is blocked, since when, by which event chain, after which repairs" without reading per-task JSON directly.
- **Depends on:** [R-017], [R-018].
- **Notes:** The current file-backed state machine is fine; the weakness is using mutable task files as both execution substrate and forensic truth.

### [R-020] Declarative repair policy engine
- **Status:** TODO
- **Feature id:** null
- **Goal:** Self-heal behavior should be configured as explicit repair classes with bounded actions, cooldowns, and escalation rules instead of being embedded as ad-hoc if/else logic in the checker.
- **Scope:** Introduce a repair-policy table mapping blocker classes to allowed actions (`retry_task`, `restart_workers_then_retry`, `feature_finalize`, `pr_sweep`, `template_refine`, `escalate_only`), cooldown windows, max attempts, and confidence thresholds; have `workflow-check` execute policy rather than custom branching.
- **Out of scope:** General-purpose rule engine, self-modifying policy, ML-learned repair selection.
- **Acceptance:** Adding or disabling a repair path does not require editing the main diagnostic logic; repeated failures obey cooldowns and bounded retry budgets automatically.
- **Depends on:** [R-017], [R-019].
- **Notes:** This is the path from "known signatures" to a controlled autonomous repair loop.

### [R-021] Synthetic canary workflows
- **Status:** TODO
- **Feature id:** null
- **Goal:** The orchestrator should continuously prove that its own plan -> implement -> review -> QA -> finalize path still works by running tiny known-safe canary workflows on a schedule.
- **Scope:** Add a synthetic repo or dedicated canary lane inside an existing low-risk repo; schedule end-to-end canary features every few hours; assert key invariants (planner emits children, retry works, template-refine works, PR opens, finalization path stays healthy); surface failures as first-class runtime incidents.
- **Out of scope:** Expensive full-stack regressions, load/perf benchmarking, cross-host chaos tests.
- **Acceptance:** A broken workflow path is detected by a canary failure before a user-facing feature stalls on the same class of bug.
- **Depends on:** [R-019], [R-020].
- **Notes:** This should become the runtime's equivalent of CI for the orchestrator itself.

### [R-022] Environment normalization for autonomous operation
- **Status:** TODO
- **Feature id:** null
- **Goal:** Local machine drift should stop being a major blocker class for autonomous work.
- **Scope:** Normalize and validate launchd env, GH auth, required binaries, repo cleanliness preconditions, worktree root state, and per-project health before slots claim work; add an explicit environment health report and preflight gates with typed failure codes.
- **Out of scope:** Full containerization of the orchestrator, multi-host scheduling, replacing launchd.
- **Acceptance:** The system can explain "workflow blocked because host env is degraded" in one typed status, and routine auth/path/worktree drift is caught before a live task is burned on it.
- **Depends on:** [R-017].
- **Notes:** This is the operational half of autonomy. Without it, the control plane remains too dependent on host folklore.

### [R-023] Guarded orchestrator self-repair lane
- **Status:** TODO
- **Feature id:** null
- **Goal:** Unknown orchestrator bugs should have a controlled path to generate, review, and land fixes against the orchestrator repo itself instead of only escalating to humans.
- **Scope:** Create a separate self-repair workflow class that can open an orchestrator feature branch/PR from a diagnosed runtime bug, run the orchestrator self-test suite plus synthetic canaries, and require explicit human approval before merge to `main`.
- **Out of scope:** Fully autonomous self-merge to `main`, unreviewed hot patching of the live control plane, recursive self-modification without tests.
- **Acceptance:** A diagnosed orchestrator bug can be turned into a bounded repair branch with evidence, without mixing that work into normal project delivery lanes.
- **Depends on:** [R-017], [R-020], [R-021], [R-022].
- **Notes:** This is how the system eventually handles "issue requires a fix to the workflow" safely instead of treating it as out-of-band operator work.

### [R-001] Planner cap + planner_enabled toggle + telegram controls
- **Status:** DONE (commit `b63b6c4`, 2026-04-14)
- **Feature id:** null
- **Goal:** Planner can be scoped to one active feature per project AND enabled/disabled per-project via Telegram, so a targeted test run on one repo does not fan out across the three-project fleet.
- **Scope:** `_project_has_open_feature` helper in `bin/orchestrator.py`; `planner_disabled` / `set_planner_disabled` backed by flag files under `state/runtime/planner-disabled/`; `planner_status_text` renderer; three new gates in `tick_planner` (hard_stopped → open_feature → planner_disabled → lock → plan); three new Telegram handlers `/planner_status`, `/planner_enable`, `/planner_disable` in `bin/telegram_bot.py`; inline doctests for every new helper.
- **Out of scope:** Cross-project feature caps, reviewer/QA slot toggles, historical audit log of enable/disable events, config-file mutation.
- **Acceptance:** Landed green — 278 insertions across `bin/orchestrator.py` + `bin/telegram_bot.py`, zero deletions; +19 new doctest examples (orchestrator 36→55), worker untouched at 58, combined 113 passing; `pr-sweep --dry-run` output unchanged; round-trip smoke `set_planner_disabled(..., True) → planner_disabled() → set_planner_disabled(..., False)` prints `True True True False`; three Telegram handlers registered at `telegram_bot.py:275-277` with matching help text block.
- **Depends on:** none.
- **Notes:** Blocks R-002. Four new helpers anchored at `orchestrator.py:1688/1995/2009/2528`.

### [R-002] Telegram bot activation + minimum-viable plist fleet
- **Status:** DONE (runtime activated, 2026-04-14)
- **Feature id:** null
- **Goal:** Bring the workflow online for the first time — bot live, minimum plists loaded, stale queue cleared — scoped to lvc-standard only.
- **Scope:** `config/telegram.json` is **already configured** (real bot_token, `allowed_chat_ids=[5596375259]`, `push_reports=true`, chmod 600 after pass-2 tightening); `launchctl load` the minimum set (planner, reviewer, qa-scheduler, pr-sweep, reaper, cleanup-worktrees, feature-finalize, worker.claude, **1×** worker.codex, worker.qa, telegram-bot — 11 plists); one-shot pre-flight `mv queue/done/task-20260414-003015-*.json queue/abandoned/` for the three orphan historian tasks; `set_planner_disabled dag-framework` and `set_planner_disabled trade-research-platform` to keep them out of the planner sweep.
- **Out of scope:** Loading the 5 extra codex plists (codex-2..6) — deferred to R-011. Loading the regression plist cadence hack (sun/wed schedule auto-fires without a load step).
- **Acceptance:** Runtime now active on the scoped fleet: planner/reviewer/qa/pr-sweep/cleanup/feature-finalize and the minimum worker set are live, Telegram `/status` responds, and `dag-framework` / `trade-research-platform` are planner-disabled so the canary stays scoped to lvc-standard.
- **Depends on:** [R-001].
- **Notes:** Runtime activation, not code. telegram.json was seeded in the pass-1 bootstrap (commit `5900d4d`) and is gitignored; no population step is needed. The stale-sweep is a pre-flight runtime step kept out of the R-001 commit by design.

### [R-003] Vertical-slice canary run on lvc-standard
- **Status:** IN_PROGRESS (deprioritized behind R-017..R-023, 2026-04-15)
- **Feature id:** null
- **Goal:** First end-to-end feature runs planner → codex → reviewer → qa → pr-sweep → feature-finalize on lvc-standard without human intervention beyond the feature→main merge click, with the BRAID health canary green.
- **Scope:** Let the planner pick the top TODO from lvc `ROADMAP.md` — currently `[R-001] Prometheus/OTel metrics exporter module`; let the planner enqueue one feature for it; observe the pass through every state; capture the full transition trace in `repo-memory/RECENT_WORK.md`; compute first live BRAID PPD numbers for `lvc-implement-operator`. Pinned pre-run baselines: `lvc-implement-operator` uses=30 topology_errors=14, `lvc-historian-update` uses=116, `lvc-reviewer-pass` uses=159.
- **Out of scope:** dag-framework / trp runs (still disabled by R-001 flags); parallel features; deliberate regression-failure drills (separate pass-2 entry).
- **Acceptance:** One task PR auto-merged to `feature/<id>` without human review beyond auto-handle authors; feature-finalize opens the feature→main PR; `braid/index.json[lvc-implement-operator].topology_errors` **does not grow** from its pinned 14 (the canary gate in CURRENT_STATE "Known concerns"); `braid/index.json[lvc-implement-operator].uses` increments by 1+; no reaper-salvaged stale claims in `transitions.log`.
- **Depends on:** [R-001], [R-002].
- **Notes:** This remains useful as a real-world proving ground, but it is no longer the top priority. Current failures are feeding the autonomy-hardening roadmap above; do not widen scope from this canary until R-017..R-022 materially land.

### [R-004] drift_threshold per-project override
- **Status:** DONE (commit pending in orchestrator worktree, 2026-04-14)
- **Feature id:** null
- **Goal:** `config/orchestrator.json` projects table supports an optional `drift_threshold` that overrides the global `CONFIG_DEFAULTS["drift_threshold"] = 5` for a single project.
- **Scope:** Extend the project schema with optional `drift_threshold`, thread through `load_config()` so it lands alongside the other per-project config, update `pr_sweep` to read project-level first and fall back to the global default, new inline doctest covering override + fallback paths.
- **Out of scope:** Per-PR overrides, dynamic threshold tuning based on merge latency, UI for the override.
- **Acceptance:** Landed in worktree: `pr_sweep` now reads `project["drift_threshold"]` first and falls back to the global default, with doctest coverage proving a project-local threshold of 10 suppresses a drift-sync dispatch that the global default of 5 would have triggered.
- **Depends on:** none.
- **Notes:** Explicitly called out as a pass-2 gap in the 1c13b8c RECENT_WORK follow-on.

### [R-005] pr-sweep running-guard telemetry
- **Status:** DONE (commit pending in orchestrator worktree, 2026-04-14)
- **Feature id:** null
- **Goal:** Observability for how often the new `pr_sweep.conflict_task_id` guard actually suppresses duplicate dispatch, so the sweep cadence can be tuned.
- **Scope:** New counter in `state/runtime/pr-sweep-metrics.json` (atomic rewrite via `write_json_atomic`), incremented each time the guard short-circuits a dispatch path; surfaced in `status_text()` and the morning PPD report; doctest exercising increment + persistence.
- **Out of scope:** Prometheus export, histograms, cross-sweep aggregation windows, per-PR breakdowns.
- **Acceptance:** Landed in worktree: `state/runtime/pr-sweep-metrics.json` persists guard-skip counts, `status_text()` exposes the total + by-reason breakdown, and `report("morning")` includes the same telemetry.
- **Depends on:** [R-002] (need live pr-sweep runs to populate the counter).
- **Notes:** Closes the observability gap flagged in the 1c13b8c follow-on. Without this, "is the guard working?" is answered only by grepping `transitions.log`.

### [R-006] BRAID generator-prompt linter — close the last 5%
- **Status:** DONE (commit pending in orchestrator worktree, 2026-04-14)
- **Feature id:** null
- **Goal:** Every generated template body is linted on write with the R1..R7 rules already in place; the remaining false-positive on `lvc-reviewer-pass` R7 is resolved, and the linter runs weekly as a historian sweep to catch rot on templates that have not been regenerated recently.
- **Scope:** Fix the R7 literal-detector heuristic that flags `lvc-reviewer-pass` (currently suppressed as a known non-blocker); add dedicated unit tests per rule (R1 node-atomicity, R2 labeled-edges, R3 terminal-check, R4 distinct-revise-per-gate, R5 reachability, R6 syntax, R7 repo-literal); wire a new `tick_template_audit` that runs `lint-templates --all` and appends findings to `reports/template-audit-<date>.md`.
- **Out of scope:** ML-based quality scoring, auto-regen on lint failure (that's R-007), dynamic principle learning.
- **Acceptance:** Landed in worktree: the R7 heuristic no longer false-positives on `lvc-reviewer-pass`, `lint_template()` doctests cover R1..R7 including the reviewer-pass regression case, and `tick-template-audit` writes `reports/template-audit-<date>.md` with per-template findings.
- **Depends on:** [R-003] (need real topology_errors signal to calibrate R7 without regressing).
- **Notes:** Partial land already in CURRENT_STATE "Live BRAID stats". This entry closes the last 5%.

### [R-007] Auto-regen on sustained topology_errors
- **Status:** DONE (commit pending in orchestrator worktree, 2026-04-14)
- **Feature id:** null
- **Goal:** When a template's live error rate crosses a threshold, automatically enqueue a claude regeneration task instead of waiting for a human to notice the counter growing.
- **Scope:** Reap-time check: if `errors / uses > 0.10` over the last N runs (configurable), auto-enqueue `engine=claude, role=planner` with `braid_template_write` contract for that template; pause codex dispatch of that task type until the new hash lands; `transitions.log` stamp so the decision is auditable.
- **Out of scope:** Multi-template parallel regen, gradient-based quality scoring, cross-project template sharing.
- **Acceptance:** Landed in worktree: `braid/index.json` now tracks recent outcomes, `reap()` auto-enqueues a claude template regen when recent topology-error rate crosses threshold, and `atomic_claim("codex")` skips paused template types until a new template hash lands.
- **Depends on:** [R-005] (metrics), [R-006] (linter must gate regen output).
- **Notes:** The "dynamic mid-run re-planning beyond basic retry" gap flagged out-of-scope in the pass-1 plan — this entry finally addresses it at the template-lifecycle level (still static mid-task, adaptive across tasks).

### [R-008] Log rotation + structured event stream
- **Status:** DONE (commit pending in orchestrator worktree, 2026-04-14)
- **Feature id:** null
- **Goal:** Prevent `logs/*.log` from growing unbounded and give the historian a structured event stream to summarise from, instead of grepping free-form logs.
- **Scope:** Rotate per-task logs to gzipped archive after 7 days; consolidate agent-status events into `state/runtime/events.jsonl` (append-only, one JSON per line, schema: `ts`, `role`, `event`, `task_id`, `feature_id`, `details`); size cap at 1GB total with rolling eviction on oldest archive.
- **Out of scope:** Remote log shipping, full ELK/Loki integration, log-based alerting (that's pass-3).
- **Acceptance:** Landed in worktree: task/agent transitions append structured rows to `state/runtime/events.jsonl`, `rotate_logs()` gzips stale logs after 7 days, `cleanup-worktrees` invokes rotation, and archive eviction keeps total retained log bytes bounded by the configured 1GB cap.
- **Depends on:** [R-002] (need sustained operation to justify rotation effort).
- **Notes:** Deferred from pass-1 plan §5. Low priority until the first real disk-pressure event.

### [R-010] Secrets scanning on repo-memory writes
- **Status:** DONE (commit pending in orchestrator worktree, 2026-04-14)
- **Feature id:** null
- **Goal:** Every historian-enqueued write to `repo-memory/*.md` passes through a secrets detector before commit, surfacing suspicious content without blocking delivery.
- **Scope:** Lightweight regex + entropy detector invoked from the historian worker path; findings logged for operator review, with optional Telegram/report surfacing for human inspection.
- **Out of scope:** Full TruffleHog integration, historical log scanning, credential rotation workflow.
- **Acceptance:** Landed in worktree: repo-memory markdown writes are scanned before historian auto-commit and during memory-synthesis candidate application, synthetic AWS-key content is surfaced clearly in logs/reports, and task delivery continues without a hard stop.
- **Depends on:** none.
- **Notes:** Deferred from pass-1 plan §5. Becomes first-class once `repo-memory` is the system of record and the historian is appending on every task completion.

### [R-011] 6-slot codex fleet activation + cross-feature parallelism
- **Status:** TODO (explicitly blocked on R-017..R-022)
- **Feature id:** null
- **Goal:** Unblock the dormant `worker.codex-2..6.plist` slots and let the orchestrator run multiple features in parallel across projects without deadlock or VM-resource contention.
- **Scope:** Load the 5 extra codex plists; lift the R-001 one-active-feature-per-project cap to a configurable `max_active_features` (default 1, override per project); add cross-repo advisory locks to prevent simultaneous compose-heavy regression sweeps from colliding on the colima VM; 3-to-6 concurrent-feature soak test with memory pressure monitoring.
- **Out of scope:** Dynamic auto-scaling, cross-host orchestration, budget-based worker throttling.
- **Acceptance:** Six codex workers claim six distinct `feature_id`s concurrently without deadlock or duplicate claims; memory pressure stays under 14GB on the 16GB M4 throughout the soak; no repo ends up with corrupt feature state; `transitions.log` shows the intended interleaving.
- **Depends on:** [R-003], [R-005], [R-008] (observability must land before unleashing parallelism).
- **Notes:** Parallelism is no longer the next move. Throughput expansion stays gated behind typed workflow contracts, deterministic recovery, canary coverage, and environment normalization.

### [R-012] Dynamic mid-task refinement — `BRAID_REFINE` contract
- **Status:** DONE
- **Feature id:** null
- **Goal:** Codex solvers can signal "graph insufficient, request a targeted refinement" mid-task and receive a patched template without aborting — replacing the current all-or-nothing `BRAID_TOPOLOGY_ERROR → full regen` loop.
- **Scope:** New solver output contract `BRAID_REFINE: <node-id>: <missing-edge-condition>`; targeted template-refine path that asks claude for a minimal full replacement `.mmd` keyed to the current template hash; lint gate on the rewritten template; re-dispatch the original task with the refreshed template as system context.
- **Out of scope:** Full BRAID paper §7 Architect-model fine-tune, multi-round refinement in a single task, visual graph ingestion (R-013).
- **Acceptance:** Synthetic drill where a solver hits a missing-condition edge mid-traversal emits a `BRAID_REFINE` trailer; within one claude round-trip the patched template is linted, written, and the original task is re-dispatched and completes with `BRAID_OK`.
- **Depends on:** [R-006], [R-007].
- **Notes:** Implemented in `bin/worker.py` / `bin/orchestrator.py` as a bounded one-round refine loop. The current version intentionally rewrites the full Mermaid body through `braid_template_write()` instead of applying an in-place graph patch.

### [R-013] Visual graph ingestion (BRAID paper §7)
- **Status:** TODO (deferred until control-plane hardening lands)
- **Feature id:** null
- **Goal:** Solvers receive a rendered PNG of the Mermaid graph alongside the source, letting vision-capable engine variants reason over topology visually instead of parsing Mermaid text.
- **Scope:** Add `mermaid-cli` to the worker harness; render template → PNG at dispatch time; attach the image to the codex prompt when the active engine variant supports image inputs; fall back to source-only for non-vision engines; cache renders keyed on template hash so regens invalidate automatically.
- **Out of scope:** Interactive graph editing, live topology visualisation, animated traversal replays.
- **Acceptance:** A codex run with an image-enabled engine traverses the visual graph and emits `BRAID_OK` on a task where the Mermaid source has been deliberately stripped from the prompt (image only).
- **Depends on:** [R-012].
- **Notes:** Interesting, but not on the critical path to autonomous operation. Do not spend runtime complexity budget here before the orchestrator itself is more deterministic.

### [R-015] Durable gh auth — replace keychain OAuth token with long-lived PAT
- **Status:** IN_PROGRESS
- **Feature id:** null
- **Goal:** The orchestrator's gh auth must survive token expiry without requiring an interactive `gh auth login` on the autonomous dev box.
- **Scope:** Audit the current auth path — launchd-spawned `gh` reads a `gho_*` OAuth token from the macOS Login Keychain (service `gh:github.com`, account `joshorig`, scopes `admin:public_key, gist, read:org, repo`, created 2026-04-11) and succeeds, while the interactive shell gets `(default) invalid` on the same binary and same `$HOME`. The divergence is undiagnosed (best theory: keychain partition-list or Mach session inheritance that launchd has but interactive zsh doesn't) and fragile against rotation or reboot. Replace with a durable token path: classic PAT (no expiry) or fine-grained PAT (max 1y) written to `config/gh-token` (chmod 600, gitignored), exported via `launchctl setenv GH_TOKEN` so both launchd and interactive shell read the same credential, with a load step in the orchestrator launchd bootstrap and a documented rebuild path in `DECISIONS.md`.
- **Out of scope:** GitHub App installation token minting (defer until PAT is insufficient), automatic token rotation, per-workflow scoping.
- **Acceptance:** `gh auth status` succeeds from both `launchctl kickstart pr-sweep` and the interactive shell reading the same source; no production path depends on the opaque keychain entry; the token source is visible in `config/` and documented so a reboot or user migration can rebuild it in one step.
- **Depends on:** none.
- **Notes:** Raised by the user during PR #14 self-heal investigation ("on an autonomous dev box we can't rely on token expiry gh login"). The current keychain path works today — this is risk reduction, not a live outage fix. Diagnosis captured in `RECENT_WORK.md` and referenced by [R-014] as the auth substrate both pr-sweep paths depend on. Code/docs landed: `config/gh-token.example`, `bin/gh_env.sh`, orchestrator-side `GH_TOKEN` file loading, and README/DECISIONS updates. Remaining step is operational: provision the real token file and export it into launchd.

### [R-016] Cherry-pick `unresolved-bot-review.yml` GH workflow to lvc-standard main
- **Status:** IN_PROGRESS
- **Feature id:** null
- **Goal:** The GH-side merge backstop `.github/workflows/unresolved-bot-review.yml` (lvc-standard commit `2a187bf`, currently only on a feature branch) must exist on `main` so feature→main PRs are gated on unresolved allowlisted-bot review threads even if the orchestrator's Case 2.5 path regresses.
- **Scope:** Cherry-pick the single workflow file to lvc-standard `main` as its own commit; add job name `unresolved-review-threads` to the required status check list in repo Settings → Branches for `main` and `feature/*`; verify with a dry-run feature→main PR containing a deliberately unresolved allowlisted-bot thread.
- **Out of scope:** Widening the allowlist beyond `chatgpt-codex-connector`, paginating past 100 review threads, flipping the workflow from blocking to advisory, adding the workflow to dag-framework or trade-research-platform.
- **Acceptance:** A feature→main PR with an unresolved allowlisted-bot thread fails the `unresolved-review-threads` check; resolving the thread (manually or via Case 2.5 self-heal) flips it green; the orchestrator's Case 2.5 path continues as the primary self-heal with this workflow as the backstop.
- **Depends on:** none.
- **Notes:** The workflow file is now present on local `lvc-standard/main`; the remaining step is to commit/push it there and wire `unresolved-review-threads` as a required check in GitHub branch protection.

## Completed

### [R-009] trade-research-platform regression — real-stack compose replay (equities + crypto), Option A corrected — 2026-04-14 (trp commit `dd04de7`, PR #27)
`qa/regression.sh` now runs the full Playwright sweep against a compose-started backend (Option A corrected), replacing the Vite-dev-server shortcut. New `qa/docker-compose.regression.yml` brings up the replay-backed stack, the runner trap-tears-down on every failure path, and the sweep covers both equities and crypto fixtures end-to-end alongside the existing JMH + gradle-test + typecheck + lint + contract stages. Verified green on the `20260414-041636` run: `qa-artifacts/trade-research-platform/regression/20260414-041636/status.txt` reads `regression OK` and the artifact set includes `e2e-compose.log`, `e2e-full.log`, `playwright-report/`, `screens/`, `jmh-results.json`, `jmh-full.log`, `fixture-build.log`, `contract.log`, `diff.md`, `gradle-test.log`, `unit.log`, `typecheck.log`, `lint.log`, and the compose-logs directory. Satisfies the original R-009 acceptance criteria: compose comes up, full sweep runs against live backend, teardown survives every path, artifacts land under `/Volumes/devssd/qa-artifacts/trade-research-platform/regression/<ts>/`. Pass-2 crypto-specific Playwright assertions and cross-browser matrix remain out of scope for this entry — those sit with the trp project's own roadmap.

### [R-014] pr-sweep self-heal on unresolved bot review threads — 2026-04-14 (commits `f06bf27`, `de95220`, `211e153`)
Closed a merge-gate gap found live on lvc-standard PR #13: external review bots (allowlist: `chatgpt-codex-connector`) leave review-thread comments that `gh pr view --json comments` does NOT expose, so Case 2's REST-comment scan never saw them and Case 4 merged straight through with the threads unresolved. New `_unresolved_bot_review_threads` helper queries the GraphQL `reviewThreads { isResolved, comments { databaseId, author, body, url, createdAt } }` edge; Case 2.5 blocks merge AND dispatches a `pr-address-feedback` round against the offending comments (mirrors Case 2's dispatch contract — `PR_SWEEP_MAX_FEEDBACK_ROUNDS` cap, `handled_comment_ids` dedup, alert-on-cap escalation). `_resolve_review_threads` helper runs the `resolveReviewThread` mutation per unique `thread_id` after the fix lands (chatgpt-codex-connector does NOT auto-resolve its own threads, so the orchestrator has to) and is called from `worker.run_pr_feedback_task` after the push, stamping `resolved_thread_count` on the child task. Mirror GH-side gate at `.github/workflows/unresolved-bot-review.yml` in lvc-standard (feature commit `2a187bf`, cherry-pick to main tracked as [R-016]) runs the same GraphQL query as a required status check — the belt to Case 2.5's braces. Ordering fix in `211e153` moves Case 2.5 above Case 3's BLOCKED/UNSTABLE silent-stamp: the GH Action required check makes the PR UNSTABLE on the very condition the gate is meant to heal, so Case 3 was swallowing the self-heal signal before it ever reached Case 2.5 — the initial `de95220` dispatch code was unreachable until the reorder. Verified end-to-end on PR #14: Case 2.5 dispatched `task-20260414-192316-855e57` → codex pushed `bea658da3e4fd0a88c1157109af4095412e490f2` → `resolveReviewThread: 1 resolved, 0 failed` → next pr-sweep tick saw `isResolved=true`, fell through to Case 4, auto-merged into `feature/feature-20260414-045850-b7d47e` → `pr_final_state: MERGED`, subsequent ticks idle at `0 checked`. Doctests 58/58 green throughout. Follow-ons: [R-015] durable gh auth (the keychain-backed OAuth token the whole self-heal path depends on), [R-016] cherry-pick the backstop workflow to lvc-standard main.

### [R-000] pr-sweep BEHIND/drift detection + running guard + conflict preview — 2026-04-14 (commit `1c13b8c`, docs follow-up `1154442`)
Case 1 conflict dispatch widened to `CONFLICTING / DIRTY / BEHIND`, drift probe synthesises `BEHIND` on MERGEABLE PRs whose worktree has drifted `drift_threshold` (default 5) commits behind base, running-task guard via `pr_sweep.conflict_task_id` suppresses duplicate dispatch while a feedback slice is in flight, and the new `[CONFLICT PREVIEW]` block (conflict list + diff stats + recent base log, 4000-char budget) is threaded into `pr-address-feedback` prompts so the codex solver sees the rebase surface up front. 5 new helpers in `bin/orchestrator.py`, 1 new helper in `bin/worker.py`, 5 new inline doctests, 58/58 doctests green. Docs captured in `README.md §4`, `bin/orchestrator.py` pr_sweep header, and `RECENT_WORK.md`. Pushed to local `main` only — no remote configured for the orchestrator repo.

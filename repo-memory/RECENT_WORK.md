# devmini orchestrator — RECENT WORK

_Append-only log. New entries go at the top. One entry per completed task or milestone. Historian role maintains this after each completed task; humans append during bootstrapping._

---

## 2026-04-16 — Worker pickup fix: nudge launchd workers on enqueue/retry

**Summary:** Fixed the control-plane bug where queued engine tasks could sit unclaimed until a manual `python3 bin/worker.py ...` kick, even though the corresponding launchd worker was loaded. The orchestrator now nudges the relevant launchd worker(s) whenever it enqueues or retries a task, instead of relying purely on KeepAlive/throttle timing after a clean no-work exit.

**Changed:**
- `bin/orchestrator.py` — added `_worker_launch_labels(...)` and `_nudge_engine_workers(...)` to map task engines to launchd worker labels and `kickstart` them opportunistically.
- `bin/orchestrator.py` — `enqueue_task()` now nudges the matching engine worker immediately after writing a queued task.
- `bin/orchestrator.py` — `reset_task_for_retry()` now nudges the matching engine worker after a retry reset lands back in `queue/queued`.
- `bin/orchestrator.py` — completed the earlier slot-occupancy fix by keeping `engine_outstanding()` for backlog and using `engine_active_counts()` for reviewer/QA gating.

**Validation:** `python3 -m py_compile bin/orchestrator.py`, `python3 -m doctest bin/orchestrator.py`, and live verification that QA no longer self-gates on a queued driver task and that queued engine work can be nudged into claim/running without manual per-slot intervention.

## 2026-04-15 — Policy coverage: every blocker code now has an explicit repair policy

**Summary:** Completed the declarative repair-policy table so every blocker code in the typed workflow contract now has an explicit policy outcome. Transient runtime failures are auto-retried within the bounded attempt budget, while structural/configuration blockers now resolve deterministically to report-only or wait states instead of falling through as uncovered cases.

**Changed:**
- `bin/orchestrator.py` — added explicit `WORKFLOW_REPAIR_POLICY` coverage for the remaining blocker codes, including `llm_exit_error`, `model_output_invalid`, `invalid_braid_refine`, `auto_commit_failed`, `delivery_push_failed`, `qa_smoke_failed`, `template_refine_exhausted`, `delivery_auth_expired`, `false_blocker_claim`, `qa_contract_error`, `qa_target_missing`, `runtime_env_dirty`, `runtime_precondition_failed`, `runtime_unknown_project`, `missing_child`, and the workflow-level codes already surfaced by `workflow-check`.
- `bin/orchestrator.py` — aligned policy `kind` matching for environment and missing-child issues so the new coverage is live rather than nominal.

**Validation:** `python3 -m py_compile bin/orchestrator.py bin/worker.py bin/telegram_bot.py`, `python3 bin/orchestrator.py workflow-check`, and a policy audit confirming zero uncovered `BLOCKER_CODES`.

## 2026-04-15 — Timeout policy: codex 60m + auto-retry on `llm_timeout`

**Summary:** Increased the codex slot timeout from 30 minutes to 60 minutes and taught `workflow-check` to auto-retry `llm_timeout` frontier blockers within the existing bounded attempt budget.

**Changed:**
- `config/orchestrator.json` — `slots.codex.timeout_sec` raised from `1800` to `3600`.
- `bin/worker.py` — updated the codex default timeout fallback to `3600` seconds.
- `bin/orchestrator.py` — added `llm_timeout_retry` to `WORKFLOW_REPAIR_POLICY`, mapping `frontier_task_blocked + llm_timeout` to `retry_task`.
- `README.md` — updated the codex slot documentation to reflect the new 60-minute wall clock.

**Validation:** `python3 -m py_compile bin/orchestrator.py bin/worker.py bin/telegram_bot.py` and `python3 bin/orchestrator.py workflow-check`.

## 2026-04-15 — R-017 completion: typed blockers cover QA and feedback loops

**Summary:** Finished `R-017` by extending typed blocker coverage through the remaining execution paths that still mattered for autonomous recovery: QA driver failures, QA target failures, review-feedback loops, and PR-feedback loops. The runtime can now classify those blockers explicitly instead of falling back to brittle log-string inference.

**Changed:**
- `bin/worker.py` — completed typed blocker stamping for QA task setup failures, smoke failures, push failures after smoke, review-feedback failures, and PR-feedback failures.
- `bin/worker.py` — reused the shared `fail_task(...)` helper so blocker metadata, retryability, and failure summaries stay consistent across slots.
- `bin/orchestrator.py` — earlier blocker taxonomy/inference additions now have matching worker coverage across planner, implementer, reviewer, QA, and follow-up feedback loops.
- `repo-memory/ROADMAP.md` — marked `R-017` done.

**Validation:** `python3 -m py_compile bin/orchestrator.py bin/worker.py bin/telegram_bot.py`, `python3 -m doctest bin/orchestrator.py`, and `python3 bin/orchestrator.py workflow-check`.

## 2026-04-15 — R-022 / R-023 completion: environment health gates + guarded self-repair lane

**Summary:** Closed out `R-022` and `R-023`. The runtime now has a first-class environment health model that names host/project drift before work is burned on it, and a guarded self-repair lane that can open bounded orchestrator-repo fixes through the normal feature/PR flow without allowing unattended self-merge.

**Changed:**
- `bin/orchestrator.py` — added cached `environment_health()` / `environment_health_text()` with typed issues for missing binaries, missing `GH_TOKEN`, unloaded launchd jobs, missing worktree root, canonical repo fetch/cleanliness failures, and missing QA scripts.
- `bin/orchestrator.py` — planner dispatch, canary dispatch, and `atomic_claim()` now gate on blocking environment issues instead of claiming work that cannot run safely.
- `bin/orchestrator.py` — `status`, `report`, `workflow-check`, Telegram `/env`, and investigation context now surface environment degradation explicitly.
- `bin/orchestrator.py` — added `enqueue-self-repair` plus Telegram `/self_repair`, `self_repair` feature metadata, and direct codex task creation for the orchestrator repo using the new `orchestrator-self-repair` BRAID template.
- `config/orchestrator.json` — added `environment_health`, `self_repair`, and a manual-only `devmini-orchestrator` project entry with orchestrator-local QA scripts.
- `qa/{smoke,regression}.sh` — added orchestrator self-test scripts that run py-compile, doctests, `workflow-check`, canary tick, and report generation.
- `braid/templates/orchestrator-self-repair.mmd` and `braid/generators/orchestrator-self-repair.prompt.md` — added the dedicated self-repair graph/template pair.
- `README.md`, `bin/telegram_bot.py`, and `repo-memory/CURRENT_STATE.md` — documented the new env-health and self-repair surfaces and updated current-state descriptions.

**Validation:** `python3 -m py_compile bin/orchestrator.py bin/worker.py bin/telegram_bot.py`, `python3 -m doctest bin/orchestrator.py`, `python3 bin/orchestrator.py env-health --refresh`, `python3 bin/orchestrator.py workflow-check`, and `python3 bin/orchestrator.py enqueue-self-repair --summary 'Test self-repair lane' --evidence 'verification only'` all ran successfully. In the live dirty worktree, `env-health` correctly reports `project_main_dirty devmini-orchestrator`, `workflow-check` surfaces that alongside the real `lvc` blocker, and `enqueue-self-repair` fails safely with `{"reason": "environment_degraded"}` rather than opening a repair branch from an already-dirty canonical checkout.

**Follow-on:** The guarded lane is intentionally manual-triggered. The next step, if wanted later, is teaching `workflow-check` to recommend or pre-fill self-repair evidence for specific orchestrator bug classes without auto-enqueuing them.

---

## 2026-04-15 — R-021 completion: synthetic canary lane scheduled, surfaced, and freshness-checked

**Summary:** Closed out `R-021` with a real synthetic canary lane that reuses the normal planner/feature path, is scheduled by launchd, and is visible as first-class runtime health. The orchestrator can now queue bounded canary features on a cadence, stamp them explicitly on feature records, surface them in status/report/workflow-check, and detect stale/missing canary coverage separately from ordinary feature blockers.

**Changed:**
- `bin/orchestrator.py` — added `synthetic_canary` config defaults, dict-aware `load_config()` merging, `create_feature(..., canary=...)`, `is_canary_feature()`, `list_canary_features()`, `_latest_canary_feature()`, `_canary_interval_due()`, and `tick_canary_workflows(force=False)`.
- `bin/orchestrator.py` — canary features now flow through the normal planner task path (`tick-canary` creates a feature + planner task with synthetic roadmap payload), and feature/workflow summaries now carry `canary` metadata.
- `bin/orchestrator.py` — `status`, `report`, `features`, and `workflow-check` reports now mark canary features explicitly; `workflow-check` also has canary-specific freshness/staleness issue classes (`canary_missing_recent_success`, `canary_stale`) plus an `enqueue_canary` repair action.
- `config/orchestrator.json` — enabled the first synthetic canary lane on `lvc-standard` with a 6-hour interval, 24-hour success SLA, 2-hour max frontier age, and a bounded safety-oriented roadmap body.
- `README.md` — documented `tick-canary-workflows` and the new `canary-workflows` launchd scheduler role.
- `~/Library/LaunchAgents/com.devmini.orchestrator.canary-workflows.plist` — installed and loaded a 6-hour launchd job that runs `python3 bin/orchestrator.py tick-canary-workflows`.

**Validation:** `python3 -m py_compile bin/orchestrator.py bin/worker.py bin/telegram_bot.py`, `python3 -m doctest bin/orchestrator.py`, `python3 bin/orchestrator.py workflow-check`, and `python3 bin/orchestrator.py report morning` all pass. `launchctl list | rg com.devmini.orchestrator.canary-workflows` shows the scheduler loaded. A live `python3 bin/orchestrator.py tick-canary-workflows` call returned `{"enqueued": 0, "reason": "project_busy"}`, which is the intended safety gate while `lvc-standard` still has an open real feature.

**Follow-on:** The main remaining improvement is quality of the canary payload itself. The lane is operational now, but a later pass should make the synthetic roadmap body assert specific repair/refine invariants more directly instead of only driving a bounded end-to-end feature through the existing pipeline.

---

## 2026-04-15 — Durable gh token loading + BRAID refine loop + unresolved-review workflow on lvc main

**Summary:** Standardized GitHub CLI auth on a file-backed `config/gh-token`
path loaded by the orchestrator Python entrypoints, added `bin/gh_env.sh` plus
`config/gh-token.example` for interactive/bootstrap use, and documented the
shared PAT flow in `README.md` and `DECISIONS.md`. In parallel, `bin/worker.py`
and `bin/orchestrator.py` now support a one-round `BRAID_REFINE: <node-id>:
<missing-edge-condition>` loop that blocks the original codex task, asks claude
for a minimal full-template rewrite, lint-writes the refreshed `.mmd`, and
re-queues the blocked task once the template hash changes. Also copied
`.github/workflows/unresolved-bot-review.yml` onto `lvc-standard/main`; the
remaining step there is manual required-check wiring in GitHub branch
protection.

---

## 2026-04-14 — post-canary fixes: pre-review auto-commit + review-feedback loop + depends_on + feature auto-abandon

**Summary:** Closed the four post-canary gaps without touching `feature-20260414-045850-b7d47e` or its failed task files. `bin/worker.py` now auto-commits codex output before `awaiting-review`, reviewer `request_change` verdicts loop through a new `review-address-feedback` codex slice on the existing worktree instead of dead-ending, planner-emitted sibling ordering now flows through `depends_on` and is enforced by `atomic_claim`, and `feature_finalize` now abandons features whose children all ended in `failed/` or `abandoned/` with no retry left in flight.

**Changed:**
- `bin/worker.py` — added `_autocommit_worktree`, `_autocommit_doctest_case`, `_handle_review_request_change`, `_review_request_change_doctest`, and `run_review_feedback_task`; `run_codex_slot` now routes `review-address-feedback` tasks directly and auto-commits normal codex work before `running -> awaiting-review`; `run_claude_reviewer` now requeues review-feedback work for up to 3 rounds and escalates via the existing PR-alert path after exhaustion.
- `bin/orchestrator.py` — `new_task()` now accepts `depends_on`, `worktree`, and `base_branch`; planner decomposition prompts/emission now support optional zero-based `depends_on` indices and resolve them to sibling task ids; `atomic_claim()` honors `depends_on` and skips dep-blocked slices while preserving FIFO claim of independent work; `_feature_all_children_failed_without_retry()` feeds a new `feature_finalize()` abandonment branch with local `git branch -d` cleanup on success.
- `braid/generators/review-address-feedback.prompt.md`, `braid/templates/review-address-feedback.mmd`, `braid/index.json` — new hand-authored pre-PR feedback BRAID template, flat flowchart with 5 distinct `ReviseCheck<N>` nodes, registry hash `sha256:80ae72841a6ccc055b6d8900dfe116470b227de58d088512b2c9bbc245970a87`.

**Validation:**
- Doctests: combined example count moved from `113` to `119` (`N+6`); `python3 -m doctest bin/orchestrator.py bin/worker.py` passes clean.
- `python3 bin/orchestrator.py lint-templates --template review-address-feedback` passes with `0` warnings.
- `python3 bin/orchestrator.py status` exits `0`.
- Canary killed by untracked-file blindness — see failed `task-20260414-045947-0db815` (empty diff) and `task-20260414-045947-eed73f` (invisible new tests).

**Follow-on:**
- Re-dispatch the two failed canary slices against the now commit-clean worker path.
- If review-feedback rounds exhaust again, inspect the alert artifact rather than letting the target silently fail in place.

---

## 2026-04-14 — pr-sweep BEHIND/drift detection + running guard + conflict preview (commit `1c13b8c`)

**Summary:** Extended `pr_sweep` to keep more PRs moving without human babysitting. Case 1 (conflict dispatch) now fires on `mergeable=CONFLICTING` OR `mergeStateStatus in (DIRTY, BEHIND)`, a drift probe synthesises BEHIND on MERGEABLE PRs whose worktree has fallen `drift_threshold` or more commits behind base (default 5), a running-task guard keyed by `pr_sweep.conflict_task_id` suppresses duplicate dispatch while a feedback slice is still in flight, and a new `[CONFLICT PREVIEW]` block (conflict file list + diff stats + recent base log, 4000-char budget) is threaded into `pr-address-feedback` prompts so the codex solver sees the rebase surface up front. BEHIND was removed from the silent-stamp predicate since it is now actionable.

**Changed:**
- `bin/orchestrator.py` (+229) — `CONFIG_DEFAULTS = {"drift_threshold": 5}` applied via `load_config()` setdefault; Case 1 predicate widened at line 707 (`CONFLICTING/DIRTY/BEHIND`); drift probe between lines 685–693 (`dispatch_reason = "drift_sync"` when synthesised); running-task guard at line 729 pinned via `pr_sweep.conflict_task_id`; silent-stamp predicate trimmed at line 787 to `BLOCKED/UNSTABLE`; `_enqueue_pr_feedback` populates `engine_args.conflict_preview` on conflict dispatch. New helpers: `_task_exists_in_queue`, `_run_git_capture`, `_git_drift_ahead_count`, `_build_conflict_preview`, `_trim_conflict_preview`. 3 new inline doctests (B: BEHIND triggers Case 1; C: drift count 7 with CLEAN synthesises BEHIND; D: running guard skips first dispatch and dispatches after guard task leaves the queue) via `__globals__` subprocess fakes.
- `bin/worker.py` (+54) — `_format_conflict_preview` helper renders the `[CONFLICT PREVIEW]` block with three subsections, `build_pr_feedback_prompt` accepts `conflict_preview=None` (injected after `[PR FEEDBACK CONTEXT]`, before `[REVIEW COMMENTS TO ADDRESS]`), `run_pr_feedback_task` threads `eargs.get("conflict_preview")` into the builder. 2 new inline doctests (rendered-when-present / omitted-when-None).

**Validation:**
- `python3 -m doctest -v bin/orchestrator.py bin/worker.py` → 58 tests, 58 passed.
- `python3 bin/orchestrator.py pr-sweep --dry-run` → exit 0, `0 checked, 0 merged, 0 feedback enqueued, 0 alerted, 0 skipped`.
- `grep -n 'merge_state in' bin/orchestrator.py` → BEHIND present at line 707 (Case 1), absent from line 787 (silent stamp).
- Only 2 files touched, no new files.

**Follow-on:**
- Push is a no-op for now — the orchestrator repo has no remote configured, so commit `1c13b8c` lives on local `main` only. Configure a remote (`git remote add origin …`) or confirm local-only is intentional.
- Pass-2 gap: `drift_threshold` is a global default in `CONFIG_DEFAULTS`. Per-project override via the `projects` table in `config/orchestrator.json` is the obvious extension; not yet wired.
- No metric yet for how often the running guard catches duplicate dispatch; currently only observable via `state/runtime/transitions.log` stamps.

---

## 2026-04-14 — Option A landed: operator fix + dispatch wiring + PPD + memory synthesis (commit `74d50e0`)

**Summary:** Single atomic commit closes every item in `prompts/option-a-work-order.md`. Operator template regenerated into a flat shape with distinct `ReviseCheck<N>` per gate (R4 clean), planner dispatch wired for all four new project-specific task types, `bin/ppd_report.py` added with morning-report integration, and memory-synthesis task type wired into a weekly per-project tick.

**Changed:**
- `bin/orchestrator.py` (+355) — `tick_planner` now emits project-correct `braid_template` per project (lvc/dag/trp historian + implementer), `tick_memory_synthesis` walks projects and enqueues a synthesis task when `repo-memory/CURRENT_STATE.md` mtime > 7 days, new `memory-synthesis` task type plumbing.
- `bin/worker.py` (+431) — `classify_slice` extended with positive-keyword + anti-pattern lists for `dag-implement-node`, `dag-historian-update`, `trp-implement-pipeline-stage`, `trp-ui-component`; 24 new inline doctests (accept/reject per type).
- `bin/ppd_report.py` (new, 175 lines) — reads `braid/index.json` + scans `logs/*.log` for per-slot token counts, emits `reports/ppd-<YYYYMMDD>.md` with error-rate table + crude `tasks_completed / total_tokens` PPD. Plumbed into morning report.
- `braid/generators/lvc-implement-operator.prompt.md` (+40 lines) — hardened with mandatory "one distinct Revise per Check" rule, worked flat-topology example, explicit DO-NOT section forbidding subgraph / shared Revise / unlabeled edges / prose nodes.
- `braid/templates/lvc-implement-operator.mmd` — regenerated via the linter-gated template_missing path. Hash changed `0b503b66... → a513adbf...`. Now R4-clean with distinct `ReviseCheck<N>` nodes.
- `braid/generators/{dag-historian-update, dag-implement-node, memory-synthesis, trp-implement-pipeline-stage, trp-ui-component}.prompt.md` — hand-authored, committed.
- `braid/templates/{dag-historian-update, dag-implement-node, memory-synthesis, trp-implement-pipeline-stage, trp-ui-component}.mmd` — all landed via `braid_template_write`, all lint-clean.

**Validation:**
- `lint-templates --all` → 9 checked, 0 errors, 1 R7 warning (pre-existing on `lvc-reviewer-pass`, non-blocking).
- `lvc-implement-operator` specifically passes (was the R4-violating blocker).
- Commit is atomic; 15 files / +1284 −86.

**Follow-on:**
- Next operator task should traverse the new flat graph cleanly; watch `topology_errors` counter — currently pinned at 14 from the old shape, should stop growing.
- PPD report plist (`com.devmini.orchestrator.ppd-report.plist`) and memory-synth plist (`com.devmini.orchestrator.memory-synth.plist`) need to be loaded as part of the workflow re-enable step.
- First weekly memory-synthesis tick lands after CURRENT_STATE.md ages past 7 days.

---

## 2026-04-13 — BRAID lint gate + 4 new generator prompts (uncommitted)

**Summary:** Landed the automated BRAID lint gate that was flagged as a pass-2 gap since the beginning. Seven rules (R1 node atomicity, R2 labeled edges, R3 terminal `Check:`, R4 distinct Revise per gate, R5 reachability, R6 syntax, R7 repo-literal heuristic) wired into `braid_template_write`, with inline doctests and a `lint-templates [--all | --template <name>]` CLI. First test cycle against the live templates proved the diagnosis of the `lvc-implement-operator` 46% topology error rate: the linter rejects that template with exactly `error R4: distinct Revise nodes are underspecified for the number of Check gates`.

**Changed:**
- `bin/orchestrator.py` — `LintError` dataclass, `lint_template()` with R1..R7 + inline doctests, `lint_templates_command()`, `lint-templates` CLI subparser, `braid_template_write()` now calls `lint_template` before `os.rename` and refuses to land a non-compliant body.
- `braid/generators/dag-implement-node.prompt.md`, `braid/generators/dag-historian-update.prompt.md`, `braid/generators/trp-implement-pipeline-stage.prompt.md`, `braid/generators/trp-ui-component.prompt.md` — new hand-authored generator prompts. Templates intentionally absent; first task of each type will hit `template_missing → claude regen → linter gate → .mmd landed`.

**Validation:** `lint-templates --all` → `lvc-historian-update: OK`, `lvc-reviewer-pass: OK`, `pr-address-feedback: OK`, skipped dirty `lvc-implement-operator`. Single-template run against `lvc-implement-operator` → exit 1 with R4 error + R7 warning. Known-good templates unchanged (regression-safe).

**Known-good templates skipped from `--all` run**: the operator template is correctly skipped in bulk runs as "dirty"; it can only be linted explicitly via `--template lvc-implement-operator`. Prevents it from blocking the bulk sweep while the fix-and-regenerate work is pending.

**Follow-on:** Regenerate `lvc-implement-operator.mmd` via the template_missing path once the generator prompt is hardened to require distinct Revise nodes per gate. Tracked in CURRENT_STATE.md "Known concerns".

---

## 2026-04-15 — Roadmap reprioritized around autonomous-runtime correctness

**Summary:** Updated the orchestrator's own roadmap and memory to make control-plane reliability the top priority. The trigger was live evidence that the biggest blockers were orchestration failures rather than solver capability: secret-scan false blockers, `BRAID_REFINE` parser brittleness, stale retry state, and workflow bugs that required manual diagnosis.

**Changed:**

- `repo-memory/ROADMAP.md` — inserted a new top-priority sequence `[R-017]..[R-023]` covering typed blocker taxonomy, deterministic retry/reset semantics, event-sourced workflow diagnosis, declarative repair policy, synthetic canaries, environment normalization, and a guarded self-repair lane. Marked parallelism / visual BRAID work as secondary until those land.
- `repo-memory/CURRENT_STATE.md` — added a priority-reset section and new active concerns describing implicit contracts, stale retry state, signature-based repair, and environment drift as the present autonomy blockers.
- `repo-memory/DECISIONS.md` — recorded the architectural decision that control-plane correctness now takes precedence over throughput expansion and future-work features.

**Why:** The system is already a credible autonomous operator prototype, but the next gains come from making it more deterministic, typed, and self-healing rather than making it broader or faster.

---

## 2026-04-15 — R-017 first pass: typed blocker metadata + workflow-check consumption

**Summary:** Added the first typed blocker layer to the orchestrator so new task failures can carry a machine-readable blocker contract instead of forcing `workflow-check` to infer everything from free-text `topology_error` / `failure` strings.

**Changed:**

- `bin/orchestrator.py` — added canonical `BLOCKER_CODES`, blocker helpers (`make_blocker`, `set_task_blocker`, `clear_task_blocker`, `task_blocker`), stored `blocker` on new tasks, cleared blockers automatically when tasks re-enter active/non-terminal flow, and threaded blocker codes into transition events.
- `bin/orchestrator.py` — updated `workflow-check` to prefer typed blocker metadata, use blocker codes in issue keys, and include blocker details in the markdown report. Legacy blocker inference remains as a compatibility shim for existing tasks already sitting in the queue.
- `bin/worker.py` — stamped explicit blocker codes for the live blocker classes we have actually hit in production so far: `template_missing`, `template_missing_edge`, `template_refine_exhausted`, `project_main_dirty`, `false_blocker_claim`, `invalid_braid_refine`, and `template_graph_error`.
- `repo-memory/ROADMAP.md` — marked `[R-017]` as in progress.

**Why:** This is the minimum useful step toward deterministic workflow diagnosis. The checker can now reason over a shared blocker taxonomy, while older tasks remain readable through the inference fallback.

---

## 2026-04-15 — R-018 first pass: deterministic attempt reset for retries

**Summary:** Replaced the ad-hoc retry path that only nulled a few fields with a shared attempt-reset API. Retries now archive the prior attempt snapshot before a task goes back to `queued`.

**Changed:**

- `bin/orchestrator.py` — new task fields `attempt` and `attempt_history`.
- `bin/orchestrator.py` — added `reset_task_for_retry(...)`, which archives the previous attempt state (`failure`, `topology_error`, `false_blocker_claim`, `blocker`, timestamps, log path, refine request, etc.), clears transient live-attempt fields, stamps retry metadata, increments `attempt`, and requeues the task.
- `bin/orchestrator.py` — switched `workflow-check` retries and reaper-driven retries (dead pid recovery, template regenerated, template refined) over to the shared reset API.
- `bin/orchestrator.py` — added `retry-task --task <id> --reason ... [--from <state>] [--source <tag>]` so manual operator recovery uses the same semantics instead of raw state moves.
- `repo-memory/ROADMAP.md` — marked `[R-018]` as in progress.

**Why:** This removes the stale-state leak where retried tasks kept old terminal metadata in the live record. The queue now preserves history without forcing diagnostics to guess whether a blocker/failure field is current.

---

## 2026-04-15 — R-018 second pass: retry lineage visible in task/status/report output

**Summary:** Finished the operator-facing half of the retry work. Attempt lineage is now visible in the standard inspection surfaces, and the checker fallback no longer crashes when it encounters older tasks without typed blocker metadata.

**Changed:**

- `bin/orchestrator.py` — queue samples now annotate retried tasks with `a<attempt>`.
- `bin/orchestrator.py` — `task_text()` now shows current attempt, blocker metadata, last retry metadata, and the last three archived attempt entries.
- `bin/orchestrator.py` — `status_text()` now reports how many tasks in the queue tree are on attempt `>1`.
- `bin/orchestrator.py` — `report()` now includes a `## Retries` section listing the most recent retried tasks and their last retry source/timestamp.
- `bin/orchestrator.py` — hardened `_workflow_check_known_task_action()` so tasks lacking both typed blocker metadata and legacy-inferable blocker fields degrade to "diagnosed only" instead of crashing `workflow-check`.

**Validation:** `python3 -m py_compile bin/orchestrator.py bin/worker.py bin/telegram_bot.py` passed. `python3 bin/orchestrator.py workflow-check` now completes and reports one genuine unresolved frontier issue instead of raising an exception.

**Why:** Retry semantics are not useful if attempt history is invisible. This makes retry lineage inspectable in the default operational surfaces and closes the null-blocker crash path found during validation.

---

## 2026-04-15 — R-019 first pass: transition-backed workflow summary

**Summary:** Started the event-sourced diagnosis work by parsing `transitions.log` into structured rows and using that event history to compute per-feature frontier timing for workflow diagnosis and investigation context.

**Changed:**

- `bin/orchestrator.py` — added `read_transitions(...)` and `task_state_entered_at(...)` to turn the append-only transition log into structured runtime evidence.
- `bin/orchestrator.py` — added `feature_workflow_summary(feature)` which computes planner/frontier state, the frontier's current-state entry timestamp, transition reason, attempt number, and blocker metadata.
- `bin/orchestrator.py` — `workflow-check` now attaches that workflow summary to issues and includes frontier timing/reason in `workflow-check_*.md` reports.
- `bin/orchestrator.py` — `_build_investigation_context()` now includes `FEATURE_WORKFLOWS`, so `/ask` gets the same event-backed workflow view instead of rebuilding diagnosis from mutable task files alone.
- `README.md` — documented the new `retry-task` command as the operator-facing retry path now that raw requeues are no longer the intended recovery mechanism.
- `repo-memory/ROADMAP.md` — marked `[R-018]` done and `[R-019]` in progress.

**Validation:** `workflow-check` now completes and reports the real unresolved frontier task (`task-20260415-101139-786d4a`) with its current-state timing instead of failing inside the checker.

**Why:** This is the first step toward making workflow diagnosis event-backed rather than JSON-blob-centric. It is not full event sourcing yet, but it moves the reporting/checker path onto the transition ledger for causal timing.

---

## 2026-04-15 — R-019 second pass: feature workflow summary reused in status/report

**Summary:** Extended the transition-backed workflow summary so it is no longer only a `workflow-check` internal. The normal operator surfaces now show live frontier state for open features.

**Changed:**

- `bin/orchestrator.py` — `feature_workflow_summary(feature)` now carries `project` and feature `summary`, and `open_feature_workflow_summaries()` returns the active set.
- `bin/orchestrator.py` — `status_text()` now lists the current frontier task/state for open features instead of only per-project feature counts.
- `bin/orchestrator.py` — `report()` now adds an `## Open Feature Workflows` section showing each feature's frontier task, frontier state, attempt, and event-backed `entered_at` timestamp.
- `bin/orchestrator.py` — investigation context now includes `FEATURE_WORKFLOWS`, sourced from the same shared summary helper rather than a bespoke diagnosis path.

**Validation:** `python3 bin/orchestrator.py status`, `python3 bin/orchestrator.py report morning`, and `python3 bin/orchestrator.py workflow-check` all run cleanly. The current open feature now shows up in both `status` and the morning report as `task-20260415-101139-786d4a` in `failed` since `2026-04-15T21:27:32`.

**Why:** This reduces duplication in feature diagnosis and moves the operator-facing surfaces onto the same workflow summary that powers the checker.

---

## 2026-04-15 — R-019 third pass: recent event activity folded into feature workflow summary

**Summary:** Extended the shared feature workflow summary beyond `transitions.log` by pulling in recent `events.jsonl` rows, then replaced the older bespoke feature-diagnosis helper with a view derived from the shared summary.

**Changed:**

- `bin/orchestrator.py` — added `read_events(...)` for structured reads from `state/runtime/events.jsonl`.
- `bin/orchestrator.py` — `feature_workflow_summary(feature)` now includes `recent_events`, capturing the last feature-scoped runtime events.
- `bin/orchestrator.py` — `_recent_feature_diagnosis()` now derives its output from `open_feature_workflow_summaries(...)` instead of rebuilding planner/frontier state independently.
- `bin/orchestrator.py` — `status_text()` and `report()` now show the last feature-scoped event alongside frontier state, so the open-feature view carries both "when did the frontier enter this state?" and "what most recently happened in this workflow?".

**Validation:** `python3 bin/orchestrator.py status`, `python3 bin/orchestrator.py report morning`, and `python3 bin/orchestrator.py workflow-check` all still run cleanly. The live open feature now shows `last_event=2026-04-15T21:38:44 implementer:task_transition` in both `status` and the report.

**Why:** This is the first point where the shared workflow summary combines both transition timing and recent event activity, which is materially closer to the event-backed diagnosis target than the earlier state-only views.

---

## 2026-04-15 — R-019 fourth pass: open-feature aggregation now uses shared workflow summary

**Summary:** Removed more of the remaining ad-hoc feature counting logic. Open-feature aggregation in status-style surfaces now reads from `open_feature_workflow_summaries(...)` instead of separately scanning feature files and rebuilding counts.

**Changed:**

- `bin/orchestrator.py` — `status_text()` now uses the shared workflow summaries as its source for open-feature counts and per-project rollups.
- `bin/orchestrator.py` — `planner_status_text()` now computes `open-features` from the shared workflow summaries instead of re-scanning `state/features/` directly.
- `bin/orchestrator.py` — `_workflow_snapshot()` now derives `open_features_by_project` from `open_feature_workflow_summaries(...)` instead of from a separate `list_features(status=\"open\")` path.

**Validation:** `python3 bin/orchestrator.py status`, `python3 - <<'PY' ... planner_status_text(); _workflow_snapshot() ... PY`, and `python3 bin/orchestrator.py workflow-check` all still run cleanly. The outputs agree on the same single open `lvc-standard` feature.

**Why:** This further reduces duplication around open-feature diagnosis and gets more of the runtime’s read paths onto the same shared workflow summary.

---

## 2026-04-15 — R-019 completion: workflow-check now diagnoses from the shared event-backed summary

**Summary:** Closed out `R-019`. The shared workflow summary now carries frontier timing, recent feature-scoped events, repair activity, and diagnosis inputs, and `workflow-check` consumes that summary directly instead of re-deriving workflow state on its own.

**Changed:**

- `bin/orchestrator.py` — added `read_events(...)` for structured reads from `events.jsonl`.
- `bin/orchestrator.py` — `reset_task_for_retry(...)` now emits `retry_reset` events, and `_workflow_check_record_attempt(...)` emits `repair_attempt` events, so repair history is visible in the runtime event stream.
- `bin/orchestrator.py` — `feature_workflow_summary(feature)` now includes frontier age, recent events, repair history, and workflow-check metadata.
- `bin/orchestrator.py` — `tick_workflow_check()` now calls `_workflow_issue_from_summary(feature, workflow, cfg)` so diagnosis runs off the shared workflow summary rather than a separate bespoke feature walk.
- `bin/orchestrator.py` — workflow-check reports now include frontier age and recent repair activity where available.
- `repo-memory/ROADMAP.md` — marked `[R-019]` done.

**Validation:** `python3 -m py_compile bin/orchestrator.py bin/worker.py bin/telegram_bot.py` passed. `python3 bin/orchestrator.py workflow-check` now emits a report showing the live unresolved frontier with event-backed timing (`frontier entered current state`, `frontier age`) from the shared summary.

**Why:** This is sufficient to call `R-019` complete: the main diagnosis surfaces and the checker now share one workflow-summary model backed by both transitions and recent runtime events, rather than maintaining separate feature-diagnosis logic.

---

## 2026-04-15 — R-020 completion: workflow-check repair selection moved to a declarative policy table

**Summary:** Closed out `R-020` by replacing the remaining hard-coded workflow-check repair selection with an explicit repair-policy table and shared policy matcher.

**Changed:**

- `bin/orchestrator.py` — added `WORKFLOW_REPAIR_POLICY`, a central table describing repair behavior by issue kind / blocker code / task state, including `retry_task`, `restart_workers_then_retry`, `feature_finalize`, `pr_sweep`, and advisory-only cases.
- `bin/orchestrator.py` — added `_workflow_policy_matches(...)` and `_workflow_policy_decision(...)` so repair choice is data-driven instead of being embedded in the diagnostic flow.
- `bin/orchestrator.py` — task repair selection in workflow-check now runs through the policy engine, and planner-empty / ready-for-finalize / final-PR-blocked issue kinds also resolve through that same path.
- `bin/orchestrator.py` — retry resets and workflow-check repair attempts emit explicit runtime events (`retry_reset`, `repair_attempt`), which are folded into the shared workflow summary.
- `config/orchestrator.json` — added `workflow_repair_policy: \"default\"` so the selected repair-policy set is explicit in config.

**Validation:** `python3 -m py_compile bin/orchestrator.py bin/worker.py bin/telegram_bot.py` passed and `python3 bin/orchestrator.py workflow-check` still completes cleanly. The current unresolved frontier on `feature-20260415-101058-14d56c` correctly stays advisory-only because no policy rule matches its timeout/failure shape yet.

**Why:** Repair selection is now centralized and inspectable. Adding, removing, or changing a known repair path no longer requires editing the main workflow diagnosis flow.

---

## 2026-04-13 — Feature-branch delivery model + 6-slot codex fleet (commit `cc23abd`)

**Summary:** Upgrade from pattern C (per-task PRs opened against `main`) to feature-branch delivery. Planner bundles logically-related tasks into a feature; codex tasks inherit `feature_id` and are serialized per feature by `atomic_claim`; task PRs target `feature/<id>` and auto-merge when green; a separate `feature-finalize` tick opens the single feature→main PR for human review.

**Changed:**

- `bin/orchestrator.py` — feature entity module (`state/features/<id>.json`, `create_feature`, `list_features`, `update_feature`, `append_feature_child`, `in_flight_feature_ids`), `tick_planner` now emits one feature per eligible project per tick, `atomic_claim` reads claimed+running to compute a `busy_features` set and skips siblings. New functions: `pr_sweep(dry_run)` (auto-merge feature-base PRs, enqueue pr-feedback tasks on new actionable comments, escalate conflicts via Telegram alert after 3 feedback rounds), `feature_finalize(dry_run)` (walks open features, opens the feature→main PR once every child is done+cleaned+MERGED), `cleanup_worktrees` extended to detect feature final-PR resolution → mark feature merged/abandoned. CLI subcommands: `features`, `pr-sweep`, `feature-finalize`.
- `bin/worker.py` — `base_branch_for_task(task)` derives `feature/<id>` or `main` per task, `ensure_feature_branch` lazily creates the feature branch off `origin/main` on first worker hit, `make_worktree` takes a base branch, `write_pr_body` / `push_worktree_branch` / `create_pr` / `run_claude_reviewer` all thread `target_base` through their git diff/log/rev-list calls. New `run_pr_feedback_task` handler reuses the target's existing worktree, loads `pr-address-feedback` BRAID template, fetches `origin/<base>`, runs `codex exec -C <wt>`, re-runs smoke inside the worktree, force-pushes with `--force-with-lease`, stamps `target.pr_sweep.*` via `update_task_in_place`.
- `braid/generators/pr-address-feedback.prompt.md`, `braid/templates/pr-address-feedback.mmd` — hand-authored. Two sub-regions (conflict path + comment path) join before a five-gate check sequence: `Check: baseline smoke`, `Check: conflicts resolved`, `Check: each comment addressed`, `Check: post-fix smoke`, `Check: no new files outside scope`, `Check: commit authored under agent identity`.
- `braid/index.json` — new `pr-address-feedback` entry.
- Six new launchd plists: `worker.codex-{2..6}.plist` (5 extra codex workers identical to `worker.codex` except for Label), `pr-sweep.plist` (StartInterval=600), `feature-finalize.plist` (StartInterval=600).
- `README.md` — replaced the pattern-C delivery section with the feature-branch model; added CLI docs for `features`, `pr-sweep`, `feature-finalize`; updated the plist table.
- `repo-memory/DECISIONS.md` — new entry "Feature-branch delivery model + 6-slot codex fleet" at the top of the file.

**Also landed in pass 2 (no separate commits found, state confirmed on disk):**

- `dag_framework/` — `repo-memory/{CURRENT_STATE,DECISIONS,FAILURES,RECENT_WORK,RESEARCH}.md` seeded; `qa/{smoke.sh,regression.sh,jmh_diff.py}` wired.
- `trade-research-platform/` — same `repo-memory/` set seeded; `qa/smoke.sh` wired with the full application-type contract (typecheck + lint + contract tests + vitest + Playwright chromium smoke + a11y specs + `:platform-runtime:jmhSmokeAll`).

**Validation:**

- `python3 -c "import orchestrator, worker"` — clean.
- `orchestrator.py pr-sweep --dry-run` — executes; degrades gracefully on the pre-existing `gh auth` HTTP 401 issue (counts tasks as `skipped`, no state mutation).
- All 7 worker slots (`worker.claude`, `worker.codex`, `worker.codex-{2..6}`, `worker.qa`) loaded via `launchctl list`; `telegram-bot` alive; all timer-driven ticks loaded.

**BRAID stats snapshot:** historian 114/0, reviewer 154/1, operator **28/13** (flagged — see CURRENT_STATE.md "Known concerns"), pr-address-feedback 0/0.

**Flagged concern:** `braid/templates/lvc-implement-operator.mmd` was autonomously regenerated at 2026-04-13T21:27:01 into a subgraph-style graph with a single shared `Revise` node. The working-tree file matches the `braid/index.json` hash `0b503b66...` and is what the runtime is using right now, but HEAD still has the prior flat version — the file is uncommitted. Topology error rate since that regen sits at ~46% (13 of 28 runs). Suspected root cause: the shared Revise node drops the check-specific context a solver needs. Fix path documented in `CURRENT_STATE.md` → "Known concerns".

---

## 2026-04-13 — Pass-2 first item: BRAID pre-flight CheckBaseline + topology-error whitelist

**Summary:** Closed the #30 misdiagnosis class at both the graph level and the worker level. Highest-leverage pass-2 item per the plan's "Out of scope for pass 1" list.

**Changed:**
- `braid/templates/lvc-implement-operator.mmd` — added `CheckBaseline` after `ReadTests`; failed path routes to `EmitBaselineRed → End` via `BRAID_TOPOLOGY_ERROR: baseline_red`.
- `braid/generators/lvc-implement-operator.prompt.md` — `CheckBaseline` listed as mandatory Check 0; new "Topology-error exit contract" section enumerating `template_missing`, `baseline_red`, `graph_unreachable`, `graph_malformed` as the only legitimate reason codes.
- `bin/worker.py` — added `VALID_TOPOLOGY_REASONS` + `topology_reason_is_valid()`; codex trailer handler now rejects unrecognized reasons as `false_blocker_claim` and moves the task to `failed/` without polluting `topology_errors` or enqueuing regen.

**Validation:** 12-case table (6 accept / 6 reject) run inline — all pass, including exact wording of the original misdiagnosis ("unrelated pre-existing classpath issue outside my change set").

**Follow-on:** Next time an operator task runs, confirm the solver actually traverses `CheckBaseline` and emits `baseline_red` on a seeded red-baseline test. Also worth revisiting the `lvc-implement-operator` `topology_errors` counter — 1 of the current 4 was the spurious #30 entry.

---

## 2026-04-13 — Pass-1 vertical slice closed, repo made agent-driven

**Summary:** Orchestrator initialized as its own git repo with a substantive README and this `repo-memory/` directory. Closes out pass-1 follow-ups #30/#31/#32 (InterprocessIpcPolicyTest misdiagnosis post-mortem, codex slot timeout bump 600s→1800s, planner slice classifier).

**Changed:**
- `bin/worker.py` — added `classify_slice()` post-validator with anti-pattern + positive-keyword lists. Validated against pass-1 ground truth: 8/8 known misclassifications reject, 11/11 correct classifications accept.
- `config/orchestrator.json` — `slots.codex.timeout_sec` 600 → 1800 to fit JMH-heavy slices.
- `README.md`, `repo-memory/{CURRENT_STATE,RECENT_WORK,DECISIONS,FAILURES,RESEARCH}.md`, `.gitignore` — repo bootstrap.

**BRAID stats snapshot:** historian 61/0, reviewer 86/1, operator 14/4 (real ≤3/14 after flake correction).

---

## 2026-04-13 — Pass-1 vertical slice verified end-to-end

**Summary:** Historian task type traversed the full state machine on lvc-standard. Regression lock, stale-claim recovery, topology-error regeneration loop, and Telegram alert path all confirmed.

**What ran:** ~86 reviewer passes, ~61 historian passes, ~14 operator passes. Synthetic regression-sim harness exercised the blocked/alert/hard-stop pipeline.

**Outcome:** Vertical slice declared complete; pass-2 work (other repos, Playwright QA, graph linter, log rotation) deferred to follow-up sessions.

---

## 2026-04-12 — Bootstrap scaffold in place

**Summary:** Initial directory layout, env config, launchd plist skeletons, and stub orchestrator.py. No workers running yet; 73 planner tasks accumulated in `queue/` from launchd fires.

**Followed by:** One-time cleanup — stale tasks moved to `queue/abandoned/` before flipping workers on.

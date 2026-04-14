# devmini orchestrator — RECENT WORK

_Append-only log. New entries go at the top. One entry per completed task or milestone. Historian role maintains this after each completed task; humans append during bootstrapping._

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

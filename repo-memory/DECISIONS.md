# devmini orchestrator — DECISIONS

_Architectural decisions with enough context that a future agent can tell whether a proposed change is undoing something load-bearing. Schema:_

```
## <date> — <one-line title>
**Context:** <what forced the choice>
**Decision:** <what we chose>
**Consequences:** <what it locks in, what it rules out>
```

---

## 2026-04-13 — Feature-branch delivery model + 6-slot codex fleet

**Context:** Pattern C shipped tiny PRs one task at a time. Planner bundles logically-related tasks into a feature. Execution fanout was 1 codex slot.

**Decision:**

- `planner.create_feature()` stays header-only; the first worker lazily creates `feature/<id>`
- codex tasks inherit `feature_id`; `atomic_claim()` serializes siblings within a feature
- task PRs target `feature/<id>` and auto-merge there when mergeable and approved
- `pr-sweep` addresses review comments via the `pr-address-feedback` BRAID graph
- `feature-finalize` opens the feature->main PR for human review only
- codex capacity scales from 1 slot to a 6-slot global fleet

**Consequences:** Locks in `feature_id` on every codex task, per-feature serialization in `atomic_claim`, and human review at feature granularity. Rules out per-task human review, auto-merge to `main`, and remote branch deletion from the orchestrator side. Known gaps: degraded `gh auth` still causes `pr-sweep`/`feature-finalize` to skip, and the first autonomous PR-feedback run depends on the `pr-address-feedback` template being present.

## 2026-04-13 — Upgrade pattern B → pattern C: worker opens the PR + worktree cleanup

**Context:** Pattern B (the earlier decision below) committed + pushed `agent/<task_id>` but required a human to still run `gh pr create` by hand. That removed the biggest source of delivery friction but left two gaps: (1) a human still had to type the PR creation step, meaning the feedback loop from agent → reviewable PR was not fully automated, and (2) nothing cleaned up worktrees after PRs resolved — every merged/closed PR left a stale `worktrees/<repo>/<task_id>/` + local `agent/<task_id>` branch, which would accumulate without bound.

**Decision:** Two coordinated changes:

1. **Worker opens the PR.** `bin/worker.py` gains a `create_pr(target, project, branch, pr_body_path, log_path)` helper that runs `gh pr create --head agent/<task_id> --base main --title <summary> --body-file <pr_body_path>` from the project's canonical repo path. It runs immediately after a successful push (not as a separate task). Success stamps `pr_url`, `pr_number`, `pr_created_at` on the target task. Failure stamps `pr_create_failure=<reason>` but keeps the target in `done/` because smoke + push both succeeded — a human can still open the PR manually from the already-written `pr-body.md`. `gh` not installed, `gh auth` broken, and duplicate-PR errors all fall into this graceful path.
2. **Periodic worktree cleanup.** `bin/orchestrator.py` gains a `cleanup_worktrees(dry_run=False)` function plus a `cleanup-worktrees` CLI subcommand. It walks `queue/done/` for tasks with `pr_number` set and no `cleaned_at`, queries `gh pr view <n> --json state,mergedAt,closedAt` in the project repo, and on MERGED or CLOSED removes the worktree via `git worktree remove --force` and deletes the local branch via `git branch -D`. The task JSON stays in `done/`; we stamp `cleaned_at`, `pr_final_state`, `pr_merged_at`, `pr_closed_at` in place. A new launchd plist `com.devmini.orchestrator.cleanup-worktrees` runs this hourly.

**Scope boundaries (explicit non-goals):**

- **Remote branches are not deleted.** GitHub's per-repo "Delete branch on merge" setting (or a human's one-click delete on close) is the source of truth. Cleaning remote refs from the agent's side would race with users and would also require a higher GitHub token scope than we want the orchestrator to hold.
- **No auto-merge.** Pattern D (the agent merging its own PR) stays out of scope until we have observation time on pattern C. Merge is still a human action.
- **Failed tasks are not cleaned.** Target tasks in `failed/` have their worktrees preserved by policy — a human may want to salvage state before anything is removed.
- **Cleanup is idempotent** by the `cleaned_at` check, so a failed mid-run (e.g. `git worktree remove` crashes) can be retried safely on the next tick.

**Consequences:** The orchestrator now has a full closed loop for deliverable changes: codex produces a branch, smoke gates it, reviewer approves it, QA pushes + PRs it with a `@codex` mention, and when a human merges or closes, cleanup harvests the local state within an hour. Pass-1 delivery latency drops from "manual forever" to "~3600s after a human decision". The `cleaned_at` stamp gives us a simple audit trail — `queue/done/` retains every task file as a historical record, with cleanup visible via a field rather than a file move. Rules out silent state drift from accumulated worktrees on a 16GB M4; `/Volumes/devssd/worktrees/` bloat is bounded by the PR decision latency rather than the task count. Also rules out race conditions between the orchestrator and a human manually cleaning up — `git worktree remove` and `git branch -D` are idempotent and both tolerate absence.

**Known gap caught during rollout:** `gh auth status` reported an invalid token for account `joshorig` when this landed. The code handles it gracefully (PR create fails, target stays `done` with `pr_create_failure`, pr-body.md is still written for a human fallback), but until `gh auth login -h github.com` is re-run, pattern C effectively degrades to pattern B. This is documented so the next person is not surprised that `pr_url` is null on otherwise-successful tasks.

---

## 2026-04-13 — Pattern B delivery: always-on pr-body artifact + opt-in auto-push

**Context:** Before this change the orchestrator had zero delivery path out of the worktree. Codex produced a branch inside `worktrees/<repo>/<task_id>/`, smoke ran against it, a reviewer approved it, and then nothing happened — no commit back to origin, no PR, no signal to a human that the change was ready. Four delivery patterns were considered (fully manual; worker auto-commit + push with human PR; worker opens the PR via `gh`; auto-merge on green). Pattern B was chosen as the prototype: strictly bounded (human stays in the loop for PR creation), but removes the "nothing happens" dead-end that made pass-1 useless for actual shipping.

Additionally, any PR opened this way should include the QA evidence the orchestrator already has on hand — the diff, smoke log tail, BRAID provenance, reviewer verdict — so the reviewer doesn't have to reconstruct state, and so that codex's GitHub code review can be invoked automatically via an `@codex` mention in the body.

**Decision:** Implement pattern B in `bin/worker.py` with two distinct pieces, both fired on the smoke-success branch of `run_qa_slot`:

1. **`write_pr_body(target, project, qa_log_path, driver_task_id)`** — always runs on smoke pass, regardless of opt-in. Writes `artifacts/<target_id>/pr-body.md` with: `@codex please review` ping, task metadata, BRAID template + hash, reviewer verdict, `git log main..HEAD`, `git diff main --stat`, last 40 lines of the smoke log, and a `gh pr create --body-file …` template. Deterministic from task state — no LLM involved in the pr-body itself.
2. **`push_worktree_branch(target, project, worktree, branch, log_path)`** — runs only if `project.auto_push == True`. Scans the full diff for secret patterns (`.env`, `telegram.json`, `credentials.json`, private keys, `ghp_`/`ghs_`, `xoxb-`, `sk-`, `AKIA`) and aborts the push if any hit. Auto-commits remaining uncommitted work under a dedicated `devmini-orchestrator <devmini-orchestrator@joshorig.com>` identity (distinct from the human's identity so automated commits are traceable in `git log --author`). Refuses to push a branch named `main`. Pushes `-u origin agent/<task_id>`.

Task state mutations on smoke pass:

- pr-body write failure → logged, non-fatal, target still → done.
- auto_push disabled → target → done with `pr_body_path` set.
- push success → target → done with `pushed_at`, `push_commit_sha`, `push_commit_count`, `push_branch`, `pr_body_path`.
- push failure (secret scan, git failure, no commits ahead) → target → failed with `push_failure=<reason>`, `qa_passed_at`, `pr_body_path`. Driver QA task still → done because its smoke script succeeded.

Config: `"auto_push": false` explicitly set on all three projects (`lvc-standard`, `dag-framework`, `trade-research-platform`). Opt-in per project; default is always off.

**Consequences:** The orchestrator now has a deliverable contract — every smoke-green worktree produces a pr-body.md a human can paste into `gh pr create --body-file`, and with one config flip, the branch is pre-pushed. The `@codex` mention in the body means codex's GitHub reviewer fires automatically when the PR opens, so code review and QA evidence land together without a separate step. Rules out unattended PR creation (pattern C) and auto-merge (pattern D) until we have more observation time — both are future work. The distinct commit identity prevents co-mingling agent commits with human commits in `git blame` / `git log --author` queries. Secret scan is defense in depth: the engineering-memory skill already forbids committing these files, but the orchestrator enforces it at the push boundary. The "driver done even on push failure" split preserves the invariant that QA slot state reflects what the script did; delivery failures are attached to the target, not the driver.

---

## 2026-04-13 — BRAID pre-flight CheckBaseline + topology-error reason whitelist

**Context:** The 2026-04-13 InterprocessIpcPolicyTest misdiagnosis (see `lvc-standard/repo-memory/FAILURES.md`) showed that a codex solver could exit a task via `BRAID_TOPOLOGY_ERROR: unrelated pre-existing classpath issue` without the orchestrator ever verifying the claim. The failure was a transient worktree flake that did not reproduce on canonical main, but it still polluted the `topology_errors` counter and triggered a pointless regeneration. 1 of the 4 recorded operator-template topology errors in pass-1 turned out to be spurious.

**Decision:** Close the gap with two coordinated changes:
1. **Template (`braid/templates/lvc-implement-operator.mmd`)**: insert a new `CheckBaseline[Check: baseline smoke green on unmodified worktree]` node right after `ReadTests`. Failed check routes to `EmitBaselineRed → BRAID_TOPOLOGY_ERROR: baseline_red → End`. Generator prompt updated to require this as Check 0 and to spec the legitimate topology-error reason codes.
2. **Worker (`bin/worker.py`)**: add a `VALID_TOPOLOGY_REASONS` whitelist (`template_missing`, `baseline_red`, `graph_unreachable`, `graph_malformed`) and a `topology_reason_is_valid()` gate. Codex trailers whose reason does not contain one of these codes are rejected as `false_blocker_claim` — the task moves to `failed/` (not `blocked/`), `topology_errors` is NOT incremented, and no regen task is enqueued.

**Consequences:** The solver's only legitimate path to exit with a "pre-existing failure" claim is now through `CheckBaseline`, which forces it to run the smoke suite on the unmodified worktree first. The whitelist is defense in depth: even if a graph regression re-opens a direct exit, any prose-style "unrelated" reason is caught at the worker level. Rules out ad-hoc blame-shifting by the solver; the graph and the worker both agree that `baseline_red` is the only sanctioned test-failure escape. Valid rejection patterns confirmed against all wording variants from the original misdiagnosis.

---

## 2026-04-13 — Post-validate planner-emitted slices with a keyword classifier

**Context:** In pass-1 the planner emitted several `lvc-implement-operator` slices whose summaries were actually CI/docs/release work ("Rewrite CI version-bump workflow", "Audit docs and release automation"). Prompt-level guidance alone did not prevent it.

**Decision:** Added `classify_slice()` in `worker.py` with an anti-pattern list (ci/version/release/readme/dependabot/lockfile/gradle-wrapper) and a positive-keyword list (hot-path/zero-alloc/jmh/operator/publisher/poller/ringbuffer/mmap/aeron/ipc/signal/cursor). Slices matching any anti-pattern or lacking every positive keyword are dropped and logged.

**Consequences:** Defense-in-depth against prompt drift. Must be kept in sync if new legitimate operator-adjacent task summaries appear that fall outside the keyword set; expand the positive list rather than remove the check. The log trail of dropped slices becomes training signal for a future automated linter.

---

## 2026-04-13 — Codex slot default timeout 600s → 1800s

**Context:** JMH-running slices (smoke gradle test + solver reasoning) were hitting the 600s wall mid-build and getting reaped as timeouts. The failures were miscounted as topology errors in pass-1 stats.

**Decision:** Bumped `slots.codex.timeout_sec` to 1800 in `config/orchestrator.json` and the `DEFAULT_TIMEOUTS` fallback in `worker.py`. Per-task `engine_args.timeout_sec` override still honored.

**Consequences:** Slower worst-case wall time but no more false topology errors. Launchd throttle on the codex worker plist unchanged (15s between respawns).

---

## 2026-04-13 — Exit-then-respawn worker model

**Context:** Free-roaming agent processes drift, leak memory, and are hard to bound. On a 16GB M4 three long-running LLM processes would compete with colima + IDE.

**Decision:** Workers are one-task-then-`sys.exit(0)`. Launchd's `KeepAlive` + `ThrottleInterval` respawns a fresh process between tasks.

**Consequences:** Memory pressure is bounded per task. Restart is free — just kill the worker, launchd handles the rest. Rules out in-memory caches on the worker side; any state that must persist lives on disk under `state/` or `queue/`.

---

## 2026-04-13 — Atomic rename claim on APFS

**Context:** Multiple slots may race to pick the next task. Needed a lock-free claim mechanism that works without a database.

**Decision:** Workers pick the oldest file in `queue/queued/` and `os.rename` it to `queue/claimed/`. APFS guarantees rename atomicity — whichever worker wins the rename owns the task. A pid file under `state/runtime/claims/` lets the reaper detect dead workers.

**Consequences:** No external dependencies (sqlite, redis, file locks). The state machine is visible as directories in the filesystem. Rules out cross-machine scale — rename atomicity does not hold across network filesystems.

---

## 2026-04-13 — Explicit `engine` field on every task

**Context:** Earlier iterations tried to route tasks by `role` alone (planner → claude, implementer → codex, etc.). Breaks down for unusual cases like "generate a BRAID template" (planner role, but claude engine) and "historian update" (historian role, codex engine).

**Decision:** Every task carries an explicit `engine` field (`claude` | `codex` | `qa`). Workers filter queues on it. Roles remain but are advisory for prompt construction, not for routing.

**Consequences:** Planner code must set both fields. The slot ↔ engine mapping is 1:1, enforced at claim time. Rules out implicit routing "magic" — if a task has no engine field, the claimer crashes rather than guess.

---

## 2026-04-13 — BRAID as the structural backbone (not just a style guide)

**Context:** The claude-plans / codex-executes split could have been ad-hoc prompt engineering. The BRAID paper (arXiv:2512.15959) shows that caching Mermaid reasoning graphs from a high-intelligence generator and having a cheap solver traverse them yields 30–74× performance-per-dollar with matching or better accuracy.

**Decision:** Adopt BRAID as the primary architectural pattern. Cache templates in `braid/templates/<task_type>.mmd`. Enforce the four graph construction principles from paper Appendix A.4 in generator prompts. Record `uses` and `topology_errors` counters per template to compute a PPD analog.

**Consequences:** Every recurring task type needs a template before a codex slot will run it. First-time task types incur generator cost; subsequent runs amortize to near-zero. Rules out free-form codex execution — by policy, codex without a graph is treated as a possible prompt injection surface.

---

## 2026-04-13 — Per-project exclusive lock for regression runs

**Context:** JMH results are noise-sensitive. A full regression run taking 3–4 hours must not share CPU with codex implementer tasks on the same project.

**Decision:** Regression tasks acquire an exclusive advisory lock at `state/runtime/locks/<project>.lock`. Codex workers targeting the same project acquire a shared lock on the same file. Regression that cannot get the exclusive lock within 5 minutes abandons that run.

**Consequences:** Regression throughput is bounded by the lock wait, not by the schedule. Rules out concurrent JMH and implementer work on a given project — intentional trade-off for measurement quality.

---

## 2026-04-13 — Telegram bot is real (long-polling), not a file-stub

**Context:** The initial stub wrote JSON files to `telegram/inbox/` and a launchd poller consumed them. Security principle: no public ports, ever. But the stub made mobile control clumsy and added latency.

**Decision:** Replaced the stub with a real `python-telegram-bot` long-polling process at `bin/telegram_bot.py`. Never a webhook. Allowlist-gated at the chat-id level. Unknown commands return help, never execute shell.

**Consequences:** Mobile control is instant and ergonomic. Rules out any path from Telegram to arbitrary shell — the handler dispatch table is the only entry point. The legacy file-stub poller plist (`com.devmini.orchestrator.telegram.plist`) is removed.

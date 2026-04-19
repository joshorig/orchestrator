# devmini orchestrator — DECISIONS

_Architectural decisions with enough context that a future agent can tell whether a proposed change is undoing something load-bearing. Schema:_

```
## <date> — <one-line title>
**Context:** <what forced the choice>
**Decision:** <what we chose>
**Consequences:** <what it locks in, what it rules out>
```

---

## 2026-04-20 — Third-party skill adoption uses a repo-controlled trusted root with pinned SHAs
**Context:** Wave C’s remaining scope was third-party skill adoption, but the repo had no trust boundary around external `SKILL.md` content. `worker.py` only loaded the local engineering-memory skills, there was no `agent-scan` audit surface, and the exact folder names proposed in the audit prose did not all exist in the upstream repos at the tagged releases. Harness scenario numbers `43/44/45` were also already occupied by existing Wave C coverage.

**Decision:** Add a repo-controlled `.claude/skills` root, a config-backed `skills.trusted_skills` allowlist, and an `agent-scan` CLI that writes audit reports under `state/runtime/agent_scans/` (and `state/runtime/mcp_audits/` for MCP roots). The worker will only load external skills that both exist under the trusted root and appear in `trusted_skills`; anything else is refused with an `untrusted_skill_rejected` event. Pin the imported skills to explicit upstream SHAs:
- `performance-profiler` ← `alirezarezvani/claude-skills` `engineering/performance-profiler` @ `3960661ae5bd81ef3af1ac56e83b042fe3a6bfea`
- `env-secrets-manager` ← `alirezarezvani/claude-skills` `engineering/env-secrets-manager` @ `3960661ae5bd81ef3af1ac56e83b042fe3a6bfea`
- `code-reviewer` ← `alirezarezvani/claude-skills` `engineering-team/code-reviewer` @ `3960661ae5bd81ef3af1ac56e83b042fe3a6bfea`
- `performing-sca-dependency-scanning-with-snyk` ← `mukul975/Anthropic-Cybersecurity-Skills` @ `c0ab6cfccb0ad151d130b1c243a05af6120861ff`
- `analyzing-sbom-for-supply-chain-vulnerabilities` ← `mukul975/Anthropic-Cybersecurity-Skills` @ `c0ab6cfccb0ad151d130b1c243a05af6120861ff`
- `implementing-secret-scanning-with-gitleaks` ← `mukul975/Anthropic-Cybersecurity-Skills` @ `c0ab6cfccb0ad151d130b1c243a05af6120861ff`
- `detecting-ai-model-prompt-injection-attacks` ← `mukul975/Anthropic-Cybersecurity-Skills` @ `c0ab6cfccb0ad151d130b1c243a05af6120861ff`
- `testing-api-security-with-owasp-top-10` ← `mukul975/Anthropic-Cybersecurity-Skills` @ `c0ab6cfccb0ad151d130b1c243a05af6120861ff`

Where the upstream repo did not provide the exact Java/Python/MCP-specific folders described in the audit prose, use the closest pinned skill that covers the same risk class and record that mapping here instead of inventing a path that does not exist. Because harness scenarios `43/44/45` are already used, the skill-adoption scenarios start at `46`.

**Consequences:** Third-party skills become explicit supply-chain dependencies rather than ambient prompt text. External skills can influence gate prompts only after a static audit plus an allowlist entry. This locks in SHA pinning and refusal-by-default for any copied-but-untrusted skill folder. It also means the repo’s skill numbering in the audit and in the harness are intentionally decoupled where collisions already existed.

## 2026-04-15 — Roadmap priority shifts from throughput/features to control-plane correctness
**Context:** Live operation exposed that the main blockers to autonomous development were not lack of slots or missing surface features, but orchestration brittleness: advisory checks blocking progress, free-text failure parsing, stale retry state, and workflow bugs requiring manual interpretation. The previous roadmap emphasized vertical-slice proof, fleet widening, and BRAID future-work before fully hardening the runtime contract.

**Decision:** Reprioritize the roadmap so typed workflow contracts, deterministic retry/reset semantics, event-sourced diagnosis, declarative repair policy, synthetic canaries, environment normalization, and a guarded orchestrator self-repair lane take precedence over parallelism expansion, visual graph ingestion, and other throughput/future-work items. Existing entries stay in the roadmap for history, but they are no longer the critical path.

**Consequences:** The next engineering phase is about making the orchestrator less heuristic and more deterministic. Throughput work like the 6-slot fleet and model-surface work like visual BRAID ingestion remain valid, but they are explicitly downstream of control-plane hardening.

## 2026-04-15 — BRAID refine uses a bounded full-template rewrite, not in-place graph patching
**Context:** `R-012` needed a way for codex solvers to request targeted topology repair mid-task without dropping straight into the heavyweight `BRAID_TOPOLOGY_ERROR -> full regen` loop. The original roadmap wording suggested patching the existing Mermaid file in place, but the repo already had a reliable generator + lint + atomic write path and no patch-format contract for Mermaid graphs.

**Decision:** Add a new codex trailer contract `BRAID_REFINE: <node-id>: <missing-edge-condition>` on implementer / review-feedback / pr-feedback passes. A `BRAID_REFINE` trailer blocks the current task, enqueues a claude `template-refine` task carrying the current template hash and refine request, asks claude for a minimal full replacement Mermaid graph, then lint-writes that full body through `braid_template_write()`. The reaper re-queues the blocked original task once it detects the template hash changed. The first version is intentionally bounded to one refine round per task; a second refine request falls back to the existing full regeneration path.

**Consequences:** This reuses the existing template validation and atomic-write machinery and avoids introducing a Mermaid patch DSL. It also means `R-012` is only a partial realization of the BRAID paper’s dynamic replanning idea: it is hash-based, one-round, and text-template-driven rather than a richer architect loop.

## 2026-04-13 — Feature-branch delivery model + 6-slot codex fleet

**Context:** Pattern C shipped tiny PRs one task at a time. Planner bundles logically-related tasks into a feature. Execution fanout was 1 codex slot.

**Decision:**

- `planner.create_feature()` stays header-only; the first worker lazily creates `feature/<id>`
- codex tasks inherit `feature_id`; `atomic_claim()` serializes siblings within a feature
- task PRs target `feature/<id>` and auto-merge there when mergeable and clean
- `pr-sweep` addresses actionable review comments, failed required checks, drift/conflicts, and unresolved allowlisted-bot review threads via the `pr-address-feedback` BRAID graph, for both task PRs and finalizing feature PRs
- `feature-finalize` opens the feature->main PR for human review only
- codex capacity scales from 1 slot to a 6-slot global fleet

**Consequences:** Locks in `feature_id` on every codex task, per-feature serialization in `atomic_claim`, and human review at feature granularity. Task PRs are allowed to auto-merge once they are clean enough for `pr-sweep` to advance them; the human gate lives at the feature PR to `main`, even though the orchestrator may still dispatch follow-up codex work to clear comments, repair failed checks, or merge `main` conflicts on that PR. Allowlisted bot authors alone are not enough to trigger repair: non-actionable summary comments are ignored unless the body contains an explicit finding marker. Rules out auto-merge to `main` and remote branch deletion from the orchestrator side. Known gaps: degraded `gh auth` still causes `pr-sweep`/`feature-finalize` to skip, and the first autonomous PR-feedback run depends on the `pr-address-feedback` template being present.

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

**Consequences:** The orchestrator now has a full closed loop for deliverable changes: codex produces a branch, smoke gates it, reviewer approves it, QA pushes + PRs it with a `@codex` mention, and when a human merges or closes, cleanup harvests the local state within an hour. Pass-1 delivery latency drops from "manual forever" to "~3600s after a human decision". The `cleaned_at` stamp gives us a simple audit trail — `queue/done/` retains every task file as a historical record, with cleanup visible via a field rather than a file move. Rules out silent state drift from accumulated worktrees on a 16GB M4; `${DEV_ROOT}/worktrees/` bloat is bounded by the PR decision latency rather than the task count. Also rules out race conditions between the orchestrator and a human manually cleaning up — `git worktree remove` and `git branch -D` are idempotent and both tolerate absence.

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

**Consequences:** The orchestrator now has a deliverable contract — every smoke-green worktree produces a pr-body.md a human can paste into `gh pr create --body-file`, and with one config flip, the branch is pre-pushed. The `@codex` mention in the body means codex's GitHub reviewer fires automatically when the PR opens, so code review and QA evidence land together without a separate step. Rules out unattended PR creation (pattern C) and auto-merge (pattern D) until we have more observation time — both are future work. The distinct commit identity prevents co-mingling agent commits with human commits in `git blame` / `git log --author` queries. Secret scan findings are logged for operator awareness, but they no longer block `repo-memory` writes, auto-commit, or generic code pushes. The "driver done even on push failure" split preserves the invariant that QA slot state reflects what the script did; delivery failures are attached to the target, not the driver.

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

**Decision:** Bumped `slots.codex.timeout_sec` to 1800 in orchestrator config and the `DEFAULT_TIMEOUTS` fallback in `worker.py`. Per-task `engine_args.timeout_sec` override still honored.

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

---

## 2026-04-15 — Orchestrator gh auth reads a file-backed GH_TOKEN

**Context:** The autonomous runtime was relying on a `gho_*` keychain-backed
OAuth token that behaved differently under launchd and interactive shells.
That path was fragile against token expiry, reboot/session drift, and operator
recovery.

**Decision:** Standardize on `config/gh-token` as the durable token source.
`bin/orchestrator.py` loads `GH_TOKEN` from that file at process start when the
environment does not already provide it; `bin/gh_env.sh` exposes the same file
for interactive shells and optional `launchctl setenv GH_TOKEN ...` bootstrap.

**Consequences:**
- `gh` calls from orchestrator/worker/telegram entrypoints no longer depend on
  the opaque macOS keychain state.
- The rebuild path is explicit: repopulate `config/gh-token`, `chmod 600`, and
  optionally `launchctl setenv GH_TOKEN "$GH_TOKEN"`.
- PAT lifecycle management is now an operator concern, but the credential
  source is visible, documented, and shared across launchd and shell usage.

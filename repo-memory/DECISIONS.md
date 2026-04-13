# devmini orchestrator — DECISIONS

_Architectural decisions with enough context that a future agent can tell whether a proposed change is undoing something load-bearing. Schema:_

```
## <date> — <one-line title>
**Context:** <what forced the choice>
**Decision:** <what we chose>
**Consequences:** <what it locks in, what it rules out>
```

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

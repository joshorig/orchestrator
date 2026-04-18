# Codex Repo Rules

Follow [AGENTS.md](AGENTS.md) as the primary repository policy.

Codex-specific expectation:

- Never treat the canonical checkout as your normal coding workspace in this repo.
- Start in a dedicated operator worktree unless the task is an explicit deploy/promotion step.
- Active self-repair features own their runtime/control-plane scope; do not overlap them without explicit user intervention.

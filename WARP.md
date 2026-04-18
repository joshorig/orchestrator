# Warp Repo Rules

Follow [AGENTS.md](AGENTS.md) as the primary repository policy.

Warp-specific expectation:

- Use a dedicated operator worktree for real edits.
- Keep the canonical checkout clean so runtime health and self-repair logic remain trustworthy.
- Do not trample an open self-repair lane; treat it as the owner of the relevant orchestrator surface.

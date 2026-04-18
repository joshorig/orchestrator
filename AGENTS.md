# Orchestrator Agent Rules

These rules apply to every agent operating in this repository, whether autonomous or user-prompted.

## Canonical Checkout

- Do not edit the canonical checkout at `/Volumes/devssd/orchestrator` directly for ordinary implementation work.
- Treat canonical `main` as runtime infrastructure, not as a scratch workspace.
- Only deployment/promotion steps may intentionally write to canonical `main`.

## Required Worktree Policy

- Before doing any non-trivial work in this repository, create a dedicated operator worktree.
- Preferred entrypoint:

```bash
python3 bin/orchestrator.py reserve-operator-worktree --name "<short-purpose>"
```

- Work inside the returned worktree path, not in the canonical checkout.

## Self-Repair Ownership

- If there is any open or finalizing self-repair feature for `devmini-orchestrator`, assume that lane owns the affected control-plane surface.
- Do not start overlapping manual/autonomous work on the same runtime bug, workflow bug, queue bug, launchd bug, or deploy bug while that self-repair lane is active.
- If you must inspect state while self-repair is active, do so read-only unless the user explicitly authorizes intervention.

## Overlap Guard

- Before reserving a manual operator worktree or starting prompted repair work, check for active self-repair ownership first.
- The helper above refuses by default when an active self-repair feature exists.
- Override only when the user explicitly decides to intervene despite the active self-repair lane.

## Deployment Rule

- Changes developed in an operator worktree must be promoted back deliberately.
- Do not leave ad hoc uncommitted edits in the canonical checkout.
- If you find the canonical checkout dirty, treat that as an environment/control-plane condition to resolve before further repair.

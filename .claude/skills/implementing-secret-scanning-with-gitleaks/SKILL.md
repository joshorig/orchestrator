---
name: implementing-secret-scanning-with-gitleaks
description: Local guidance for integrating repo secret scanning safely without bundling untrusted helper scripts.
domain: cybersecurity
subdomain: devsecops
tags:
- devsecops
- secret-scanning
- secure-sdlc
version: 1.0.1
author: sanitized-local
license: Apache-2.0
---

# Implementing Secret Scanning with Gitleaks

Use this skill to review whether a repository has an adequate secret-scanning workflow and to propose safe local integration steps.

## Safe scope

- review existing secret-scanning config
- check for pre-commit and CI integration gaps
- suggest local command invocations without embedding credentials
- review baseline usage and remediation flow at a policy level

Do not use this skill to download binaries, call network endpoints, inspect live secrets, or run destructive git-history rewrites automatically.

## Review checklist

- Is there a committed scanner config?
- Is local pre-commit coverage defined?
- Is CI scanning present?
- Are high-confidence findings blocking?
- Is there a documented manual remediation path for exposed credentials?

## Suggested local workflow

1. Confirm the repo has a secret-scanning configuration file.
2. Confirm pre-commit hooks or equivalent local checks exist.
3. Confirm CI runs the same scanner on diffs or full repo state.
4. If historical findings exist, track them in a reviewed baseline with an owner.
5. If a live secret is suspected, stop and escalate to a human rotation workflow.

## Output contract

```text
scanner_config:
- present: true|false
  path: ...

coverage:
- local_hook: true|false
- ci_gate: true|false
- baseline_reviewed: true|false

findings:
- severity: low|medium|high
  kind: missing_config|missing_hook|missing_ci|non_blocking_high_confidence|baseline_without_owner
  path: ...
  note: ...
```

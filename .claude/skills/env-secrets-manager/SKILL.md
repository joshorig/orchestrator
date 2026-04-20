# Env & Secrets Manager

Local-only guidance for reviewing environment files, secret carriers, and configuration hygiene.

## Safe scope

Use this skill only for:
- identifying which files are secret carriers
- verifying `.gitignore` coverage
- checking that sample env files omit values
- reviewing startup validation for required configuration
- writing a manual rotation checklist for operators

Do not use this skill to contact external systems, run network commands, inspect live credentials, or print sensitive values.

## Operator workflow

1. Inventory candidate secret-carrier files:
   - `.env*`
   - `config/*`
   - deployment manifests
   - CI variable templates
2. Confirm committed examples contain keys only, not values.
3. Confirm runtime-only files are ignored by git.
4. Check app startup or CI validation for missing required vars.
5. If a secret-handling defect exists, report it as one of:
   - missing ignore rule
   - committed value in sample file
   - missing required-var validation
   - unclear rotation ownership

## Review checklist

- `.env.example` and similar committed templates contain placeholders only.
- Local runtime env files are ignored.
- Secret-like files are not checked into source control.
- Required configuration is validated at process startup or in CI.
- Rotation instructions identify owner, scope, and verification steps without exposing values.

## Output contract

Return a short structured note:

```text
secret_carriers:
- path: ...
  purpose: ...

findings:
- severity: low|medium|high
  kind: ignore_gap|committed_value|validation_gap|rotation_gap
  path: ...
  note: ...

rotation_checklist:
- owner
- systems affected
- verification steps
```

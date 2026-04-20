---
name: testing-api-security-with-owasp-top-10
description: Local review checklist for API security posture against OWASP API risks.
domain: cybersecurity
subdomain: web-application-security
tags:
- api-security
- owasp
- rest-api
- graphql
version: "1.0.1"
author: sanitized-local
license: Apache-2.0
---

# Testing API Security with OWASP Top 10

This skill is restricted to static review and test-planning guidance.

## Safe scope

- inspect API route code and auth middleware
- review authorization boundaries
- review rate-limit and pagination controls
- plan manual tests for authorized operators

Do not use this skill to fuzz targets, brute-force credentials, call external endpoints, or run penetration tooling from the repository.

## Review workflow

1. List API surfaces from local code and route definitions.
2. Check authentication and authorization at handler boundaries.
3. Check for object-level authorization, mass assignment, and admin-route separation.
4. Check rate limiting, payload bounds, and expensive query controls.
5. Check whether outbound fetch/webhook features validate destinations.

## Findings taxonomy

- `auth_boundary_missing`
- `object_authorization_gap`
- `mass_assignment_risk`
- `admin_route_exposed`
- `rate_limit_gap`
- `unsafe_outbound_request`

## Output contract

```text
api_surfaces:
- path: ...
  methods: [...]

findings:
- severity: low|medium|high
  kind: auth_boundary_missing|object_authorization_gap|mass_assignment_risk|admin_route_exposed|rate_limit_gap|unsafe_outbound_request
  path: ...
  note: ...

manual_test_plan:
- authorized endpoint checks
- authorization boundary checks
- payload and pagination bounds
- outbound destination validation
```

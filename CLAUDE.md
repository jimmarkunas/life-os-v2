# LIFE OS v2 — Claude Code Guide

## Repository role — hard lock

`jimmarkunas/life-os-v2` is the **sole active LIFE OS implementation repository**.

All current product development, bug fixes, refactors, tests, runtime implementation, configuration work, branches, pull requests, and production-boundary changes happen here unless Jim explicitly names a different repository for the exact task.

`jimmarkunas/life-os-automation` is V1/reference-only implementation code. Consult it only when a current V2 task explicitly needs a proven old behavior/asset for reuse, when a still-canonical governance/runtime document physically housed there must be read, or when Jim explicitly requests bounded V1 governance/legacy maintenance. Never switch implementation into V1 because an old handoff, search result, or historical file points there.

## Standard implementation header — mandatory

Every PM/TL/Codex/Claude implementation prompt and compiled handoff must begin literally with:

```text
Repository: jimmarkunas/life-os-v2
Base: fresh origin/main
V1 use: prohibited unless this handoff explicitly names a V1 asset for reference/reuse
```

Do not paraphrase or omit this header. If stale context appears to direct ordinary implementation into V1, stop with:

`WRONG_REPOSITORY_STOP — active LIFE OS development belongs in jimmarkunas/life-os-v2`

**Canonical development authority:** `jimmarkunas/life-os-automation/docs/life-os-development-policy.md`.  
**v2 repository supplement:** `docs/life-os-development-policy.md`.  
**Architecture authority:** `docs/DECISIONS.md` and `docs/ARCHITECTURE.md`.

This file is a thin execution guide. It does not duplicate or override LIFE OS governance.

## Before coding

1. Confirm the repository is exactly `jimmarkunas/life-os-v2`; otherwise stop with `WRONG_REPOSITORY_STOP` unless Jim explicitly authorized the other repo.
2. Fresh-read current `origin/main` and record its SHA.
3. State `OUTCOME`, exact `MUTATION SURFACE`, and smallest deterministic `PROOF`.
4. Use the current Notion product/domain canon for durable requirements.
5. Read `Development Projects` only when ownership/concurrency matters.
6. For structured Notion queries, follow the canonical `jimmarkunas/life-os-automation/docs/notion-query-conservation-policy.md`.

## Execution discipline

- Ordinary coding run: **≤5 minutes**.
- Production-code addition/material rewrite: **≤50 lines** unless Jim explicitly approves more.
- Default production mutation surface: **≤3 files**; default test surface: **≤2 files**.
- Claude/Codex: **≤2%** of the active five-hour allowance per run when telemetry exists; target stop 1.8%.
- One implementer per mutation. No duplicate Claude/Codex rediscovery.
- Tech Lead handoff should be approximately: `standard implementation header → exact files/symbols → mutation → proof → stop`.
- No repository-wide archaeology, open-ended debugging, or speculative cleanup.
- One failure boundary gets one bounded retry. Then stop with the exact blocker.

## V1 reuse rule

When a V2 task explicitly references V1 to strengthen the new build:

- inspect only the named/relevant V1 behavior;
- extract the smallest proven mechanic, algorithm, parser behavior, mapping, or fixture insight;
- re-express it inside current V2 architecture and contracts;
- do not copy V1 orchestration, persistence, QA scaffolding, recovery systems, test estates, or duplicated live paths wholesale;
- return to V2 for all mutation and proof.

## Production and architecture

- LIFE OS remains one composable platform.
- `LIFE OS Daily Runs` is the sole recurring scheduler under the live Production Contract.
- Standard free public GitHub-hosted Actions may execute approved bounded production code from protected `main`; they do not schedule production.
- No second scheduler, workflow family, datastore, event bus, trigger-file RPC, QA hierarchy, generic orchestration framework, recovery subsystem, or duplicate live implementation without explicit architectural approval.
- Public repo: no production secrets, private identifiers, canonical records, personal scoring policy, or private payload fixtures/logs/artifacts.
- Product flow target: `Acquire → Normalize → Decide → Persist → Present`.
- Mail/Newsletter own acquisition/parsing; Jobs owns terminal evidence, identity, Fit, qualification/lifecycle, dedupe, persistence/read-back; shared Core owns reusable mechanics only.

## Testing

- Repository target: **≤100 collected tests**.
- While above 100, **no package, including test-only packages, may increase collected-test count**.
- New regressions for an existing contract become named cases inside that contract by default.
- Delete obsolete tests when behavior is removed, merged, replaced, or already protected by a stronger boundary.
- Use targeted proof first, affected regression once, full suite at most once before landing, then main CI.
- Jim-approved TestKit is limited to `tests/testkit/{cases.py,builders.py,boundaries.py,harnesses.py}` and must remain test-only and policy-free.

If a package declares:

`Production-Frozen: true`

then **no production code, production configuration, or production workflow file may change**. Any such diff is `SCOPE_EXPANSION_STOP`.

## Delivery

Return only:

- `STATUS: LANDED | TARGETED PROOF FAILED | BLOCKED`
- `OUTCOME:`
- `REMOVAL:`
- `FILES:`
- `PROOF:`
- `BUDGET:`
- `REMOTE SHA:`
- `BLOCKER:`
- `NEXT:`

If requested work conflicts with locked architecture:

`ARCHITECTURAL DRIFT DETECTED — NO MUTATION`
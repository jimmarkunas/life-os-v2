# LIFE OS v2 — Claude Code Guide

**Canonical development authority:** `jimmarkunas/life-os-automation/docs/life-os-development-policy.md`.  
**v2 repository supplement:** `docs/life-os-development-policy.md`.  
**Architecture authority:** `docs/DECISIONS.md` and `docs/ARCHITECTURE.md`.

This file is a thin execution guide. It does not duplicate or override LIFE OS governance.

## Before coding

1. Fresh-read current `origin/main` and record its SHA.
2. State `OUTCOME`, exact `MUTATION SURFACE`, and smallest deterministic `PROOF`.
3. Use the current Notion product/domain canon for durable requirements.
4. Read `Development Projects` only when ownership/concurrency matters.
5. For structured Notion queries, follow the canonical `jimmarkunas/life-os-automation/docs/notion-query-conservation-policy.md`.

## Execution discipline

- Ordinary coding run: **≤5 minutes**.
- Production-code addition/material rewrite: **≤50 lines** unless Jim explicitly approves more.
- Default production mutation surface: **≤3 files**; default test surface: **≤2 files**.
- Claude/Codex: **≤2%** of the active five-hour allowance per run when telemetry exists; target stop 1.8%.
- One implementer per mutation. No duplicate Claude/Codex rediscovery.
- Tech Lead handoff should be approximately: `exact repo/base → exact files/symbols → mutation → proof → stop`.
- No repository-wide archaeology, open-ended debugging, or speculative cleanup.
- One failure boundary gets one bounded retry. Then stop with the exact blocker.

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
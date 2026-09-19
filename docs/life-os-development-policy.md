# LIFE OS v2 Development Supplement

**Status:** Repository-specific supplement for `jimmarkunas/life-os-v2`.  
**Canonical development authority:** `jimmarkunas/life-os-automation/docs/life-os-development-policy.md`.  
**Architecture authority:** `docs/DECISIONS.md` and `docs/ARCHITECTURE.md`.  
**Runtime authority:** `jimmarkunas/life-os-automation/docs/life-os-production-contract.md` for scheduled/runtime behavior.  
**Live coordination:** Notion `Development Projects` only for current ownership, concurrency, takeover, and handoff.

This file does **not** duplicate or override the canonical LIFE OS Development Policy. It records only v2-specific repository mechanics and enforcement.

## Repository role lock

`jimmarkunas/life-os-v2` is the **sole active LIFE OS implementation repository**.

All current product development, bug fixes, refactors, tests, runtime implementation, configuration work, branches, pull requests, and production-boundary changes target V2 unless Jim explicitly names another repository for the exact task.

`jimmarkunas/life-os-automation` is V1/reference-only implementation code. It may be consulted only for explicit V1→V2 reuse/comparison, for still-canonical governance/runtime documents physically housed there, or for bounded V1 governance/legacy maintenance explicitly requested by Jim. Historical V1 code, old handoffs, or search results are never sufficient reason to move implementation back to V1.

Every implementation handoff must begin:

`Repository: jimmarkunas/life-os-v2`

If stale context appears to target V1, stop with:

`WRONG_REPOSITORY_STOP — active LIFE OS development belongs in jimmarkunas/life-os-v2`

When V1 is used to strengthen V2, extract only the smallest proven behavior needed and re-express it inside current V2 architecture. Do not port V1 QA scaffolding, orchestration, persistence, recovery machinery, duplicate live paths, or old test estates wholesale.

## v2 repository rules

1. **ONE V2 PLATFORM.** Do not create a second v2 architecture, scheduler, orchestration stack, datastore, or live implementation for an existing behavior.
2. **PUBLIC CODE / PRIVATE DATA.** `life-os-v2` is public. Production secrets, private identifiers, canonical records, personal scoring policy, runtime checkpoints, and private payloads never enter Git history, fixtures, logs, Actions artifacts, or caches.
3. **STATELESS PRODUCTION EXECUTOR.** Standard free public GitHub-hosted Actions may execute approved bounded production code from protected `main`. They do not schedule production. `LIFE OS Daily Runs` remains the sole recurring scheduler under the live Production Contract. No `schedule`/cron, paid/larger runner, workflow chain, or second orchestration layer is authorized.
4. **NO QA-ERA ARCHITECTURE.** Do not migrate numbered QA namespaces, trigger-file RPC, handoff manifests, Continuity-style coordination, or recovery frameworks into v2.
5. **ONE LIVE PATH PER BEHAVIOR.** Retire superseded code/tests after accepted cutover when safe. Git owns history.
6. **DOMAIN FLOW.** Keep product flows explainable as `Acquire → Normalize → Decide → Persist → Present`; domain policy stays with its owner.
7. **ARCHITECTURAL DRIFT.** If requested work conflicts with the canonical Development Policy or `docs/DECISIONS.md` / `docs/ARCHITECTURE.md`, stop with `ARCHITECTURAL DRIFT DETECTED — NO MUTATION`.

## Bootstrap

For ordinary v2 development, read only:

1. this repository's `AGENTS.md` role lock;
2. the canonical cross-repo Development Policy;
3. the relevant current Notion product/domain canon;
4. `docs/DECISIONS.md` / `docs/ARCHITECTURE.md` only when architecture is material;
5. the exact affected implementation surface/direct callers;
6. Notion `Development Projects` only when ownership/concurrency matters.

Do not use `life-os-automation` implementation as a second active codebase. It may be consulted selectively only for explicit V1→V2 reuse/reference or still-canonical governance/runtime authority.

## Compiled implementation handoffs

For pre-scoped Codex/Claude work, the Tech Lead compiles the context first. Default handoff:

`Repository: jimmarkunas/life-os-v2 → exact base → exact files/symbols → exact mutation → exact proof → stop`

Implementers edit and prove; they do not rediscover LIFE OS architecture. One implementer owns one mutation by default.

## v2 CI enforcement

The canonical Development Policy's efficiency/test rules apply here. In addition, the existing v2 development guard is the repository enforcement surface for these mechanics:

- repository target: **≤100 collected tests**;
- while above 100, **no package, including test-only packages, may increase collected-test count**;
- at/below 100, new collected tests require `Production-Critical-Test: <specific failure prevented>` unless Jim approves an exception;
- regressions for an already-covered contract should become named cases inside that contract rather than new incident-shaped tests/files;
- `Production-Frozen: true` means production code, production configuration, and production workflow files may not change in that package; any such diff is `SCOPE_EXPANSION_STOP`;
- `Jim-Approved-Budget: <justification>` is required when the canonical Development Policy's production line/file budget is explicitly exceeded.

Test count is a liability ceiling, not a quality metric. Production-critical guarantees are the preservation target.

## LIFE OS TestKit

Jim has explicitly approved a lightweight test-only TestKit under:

`tests/testkit/{cases.py,builders.py,boundaries.py,harnesses.py}`

This is a ceiling, not a requirement to populate every module. Each helper must be immediately consumed by real tests and reduce duplicated setup/context.

TestKit may own reusable synthetic builders, deterministic external-boundary substitutes, coherent case execution, and repeated real-composition setup. It may not own product policy or become a custom runner, plugin system, registry/configuration language, datastore, scheduler, orchestration engine, evidence store, inheritance framework, or production dependency.

## Notion query safety

The canonical query rule is:

`jimmarkunas/life-os-automation/docs/notion-query-conservation-policy.md`

The local `docs/notion-query-conservation-policy.md` is a v2 pointer/supplement only. Metered rows/SQL require Jim's explicit per-query approval. Repository-owned Public API access through an already-approved execution boundary is allowed when it follows the canonical policy and public/private-data rules.

## Completion

Use the canonical Development Policy's `LANDED / ACCEPTED / CLOSED / DEGRADED` meanings and completion report. Repo-specific CI mechanics do not replace real changed-boundary acceptance when production behavior changes.
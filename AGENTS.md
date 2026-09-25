# LIFE OS Repository Role Lock

`jimmarkunas/life-os-v2` is the **sole active LIFE OS implementation repository**. All current product development, bug fixes, refactors, tests, runtime implementation, configuration work, branches, pull requests, and production-boundary changes target this repository unless Jim explicitly names another repository for the exact task.

`jimmarkunas/life-os-automation` is **V1/reference-only as an implementation codebase**. Do not implement, repair, refactor, extend, or add tests there merely because historical context points there. V1 may be consulted only for explicitly authorized V2 reuse, still-canonical governance/runtime documents physically housed there, or bounded V1 governance/legacy maintenance explicitly requested by Jim. Reuse only the smallest needed behavior; do not port V1 architecture, QA scaffolding, orchestration, persistence, tests, or recovery machinery wholesale.

## Standard implementation header — mandatory

Every PM/TL/Codex/Claude implementation prompt and compiled handoff must begin literally with:

```text
Repository: jimmarkunas/life-os-v2
Base: fresh origin/main
V1 use: prohibited unless this handoff explicitly names a V1 asset for reference/reuse
```

If stale context targets V1, stop with:

`WRONG_REPOSITORY_STOP — active LIFE OS development belongs in jimmarkunas/life-os-v2`

## Required authority

`docs/life-os-development-policy.md` is mandatory before coding and points to the canonical cross-repository policy:

`jimmarkunas/life-os-automation/docs/life-os-development-policy.md`

The canonical policy owns generic development method and governance. This file owns only V2 repository routing and role-entry mechanics.

For Jobs recovery/decomposition/simplification work, also read `docs/ARCHITECTURE.md` → **Jobs codebase simplification roadmap**. That section is implementation guidance subordinate to the current Jobs OS product canon and must not activate before its stated gate/R0 preconditions.

V2-specific enforcement may stop with: `LINE_BUDGET_STOP`, `TOKEN_BUDGET_STOP`, or `TEST_BUDGET_STOP`.

## Optional role triggers

Load a role only when Jim explicitly triggers it:

- `you are the Tech Lead`, `be the Tech Lead`, `act as Tech Lead`, or `you are my backup Tech Lead` → `docs/roles/TECH_LEAD.md`.
- `create a handoff`, `make a session handoff`, or `prepare the next chat` → `docs/roles/SESSION_HANDOFF.md`.

Role overlays do not replace canonical authority, current product requirements, or implementation. Do not auto-load them in ordinary work.

# LIFE OS Repository Role Lock

`jimmarkunas/life-os-v2` is the **sole active LIFE OS implementation repository**.

All current product development, bug fixes, refactors, tests, runtime implementation, configuration work, branches, pull requests, and production-boundary changes must target this repository unless Jim explicitly gives a different repository instruction for the exact task.

`jimmarkunas/life-os-automation` is **V1/reference-only as an implementation codebase**. Do not bootstrap from, implement in, repair, refactor, extend, or add tests to V1 merely because an older handoff, historical document, or code search points there.

V1 may be consulted only when a current V2 task explicitly needs a proven V1 behavior/asset for reuse, when a still-canonical governance/runtime document physically housed there must be read, or when Jim explicitly requests bounded V1 governance/legacy maintenance. When reused, extract only the smallest proven behavior needed by V2; do not port V1 architecture, QA-era scaffolding, orchestration, persistence, tests, or recovery machinery wholesale.

Every implementation handoff must begin:

`Repository: jimmarkunas/life-os-v2`

If stale context appears to target V1, stop with:

`WRONG_REPOSITORY_STOP — active LIFE OS development belongs in jimmarkunas/life-os-v2`

# LIFE OS Agent Guardrails

`docs/life-os-development-policy.md` is mandatory before any coding task.

Hard stops for every implementation agent:

- Fresh-read current `origin/main` before analysis or mutation; stale implementation analysis is invalid if main moved.
- Maximum production-code addition/material rewrite: **50 lines per package/run**. Deletions do not count. Over 50 requires Jim's explicit approval and a `Jim-Approved-Budget:` justification in the commit message.
- Default mutation surface: **3 production files maximum**. Larger scope requires Jim's explicit approval.
- Claude/Codex token budget: **2% maximum of the active 5-hour allowance per run**; target stop at 1.8%. If reliable usage telemetry is unavailable, broad autonomous exploration is prohibited.
- Ordinary coding run: **5 minutes maximum**. Major feature: **60 minutes maximum to production acceptance attempt**. At 60 minutes stop and report the blocker; do not continue by default.
- No new framework, runner, workflow family, datastore, QA layer, diagnostic path, abstraction, or duplicate implementation unless the changed production behavior requires it.
- Tests are a liability budget, not an achievement metric. Target repository ceiling is **100 collected tests**. Above 100, code-changing packages may add no tests and must delete obsolete/non-production-critical tests. New tests at/below 100 require `Production-Critical-Test: <reason>`.
- When behavior is removed, merged, or replaced, delete its obsolete tests in the same package.
- Targeted proof first; affected regression once; full suite once before landing. Never rerun green proof without changed code or a new factual failure.
- One implementer per mutation by default. No duplicate Claude/Codex rediscovery.

Stop codes: `BASE_MOVED_STOP`, `LINE_BUDGET_STOP`, `TOKEN_BUDGET_STOP`, `TIMEBOX_STOP`, `TEST_BUDGET_STOP`, `SCOPE_EXPANSION_STOP`, `WRONG_REPOSITORY_STOP`.

# LIFE OS Optional Role Triggers

These role files are optional overlays. They do **not** replace canonical LIFE OS authority, the Development Policy, the Production Contract, current Notion product requirements, or current implementation.

Load a role only when Jim explicitly triggers it.

## Trigger map

- `you are the Tech Lead`
- `be the Tech Lead`
- `act as Tech Lead`
- `you are my backup Tech Lead`

On any of those triggers, read and apply:

`docs/roles/TECH_LEAD.md`

For continuity/handoff requests such as:

- `create a handoff`
- `make a session handoff`
- `prepare the next chat`

read and apply:

`docs/roles/SESSION_HANDOFF.md`

Do not auto-load role files in ordinary work. Keep role behavior subordinate to Jim's current instruction and live canonical sources.
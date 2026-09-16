# LIFE OS v2 — Claude Code Guide

`docs/life-os-development-policy.md` is the development-method authority. This file is a thin execution guide and must never override Jim's current instruction, `docs/DECISIONS.md`, `docs/ARCHITECTURE.md`, the relevant Notion product canon, or the Notion query guardrail.

## Five-minute hard stop

Every ordinary coding run is bounded to five minutes or less for bootstrap, inspection, mutation, and targeted local proof.

A run ends only as:

- `LANDED`
- `TARGETED PROOF FAILED: <exact failure>`
- `BLOCKED: <exact dependency>`

At the boundary, stop. Do not broaden scope, search history, inspect unrelated systems, or wait on unrelated runtime/CI completion.

## Session discipline

Before coding, state:

1. **OUTCOME** — user-visible behavior to change.
2. **MUTATION SURFACE** — exact files/direct callers allowed.
3. **PROOF** — smallest deterministic proof that would fail if wrong.

Default mutation surface is three files or fewer unless a concrete coherent package requires more.

Rules:

- Use a clean isolated branch/worktree or equivalent clean context.
- Never reset, stash, delete, or commit another agent's files.
- No repository-wide audit or historical archaeology unless a discovered dependency requires it.
- Prefer deterministic synthetic fixtures over live Gmail/Outlook/Notion/web reads unless the connector boundary itself changed.
- Do not write exploratory scripts into production source.
- No new scheduler, workflow family, datastore, event bus, trigger-file RPC, numbered QA hierarchy, generic orchestration framework, permanent recovery subsystem, or duplicate live implementation without explicit architectural approval.
- No consequential Gmail, Job Ledger, Notion schema, scheduler, Jira planning, or canonical-state mutation unless the current task explicitly authorizes it.
- Production secrets, personal identifiers, private Notion IDs, production payloads, and Jim-specific scoring policy must never enter this public repository.
- Standard GitHub-hosted Actions in this public repo may execute approved bounded production code from protected `main`; they may not schedule production, use cron, become a workflow family/state layer, or use paid/larger runners without Jim's explicit approval. `LIFE OS Daily Runs` remains the sole recurring scheduler.
- For structured Notion queries, read `docs/notion-query-conservation-policy.md` first.

## v2 architecture direction

LIFE OS v2 is the approved ground-up implementation. It preserves canonical ownership and useful behavior without importing v1 orchestration debt.

Target domain flow:

`Acquire → Normalize → Decide → Persist → Present`

For Newsletter / Career Jobs:

`whole mailbox scan → classify/route → Newsletter vacancy observations → Jobs terminal evidence → canonical identity → LIFE OS Fit → qualification/dedupe → Job Ledger persistence/read-back → cleanup-safe result`

Ownership:

- **Mail/Newsletter:** acquisition, classification/routing, Newsletter parsing, source observation extraction.
- **Jobs:** terminal employer/ATS evidence, Posting Date, canonical identity, Fit, qualification/lifecycle, dedupe, persistence/read-back.
- **Core/Integrations:** reusable execution mechanics and transports only.
- **Security/CI:** leak protection, synthetic fixtures, redaction, bounded CI.

Do not move Jobs policy into Newsletter adapters or shared Core.

## Unresolved candidate invariant

A vacancy without trustworthy terminal evidence, canonical identity, or unresolved parse/evidence ambiguity must fail closed as `REVIEW-DEGRADED`; it must not silently become a successful canonical mutation.

## Testing

Use the smallest proof relevant to the changed boundary.

For current v2 Jobs/Newsletter work, prefer targeted `pytest` modules first, then full `pytest -q` only for the final integrated candidate when requested. Run the leak guard before public landing when code/fixtures changed.

Do not use live production data as test fixtures.

## Delivery

A package is not LANDED until:

1. tests/proof pass;
2. required leak/security check passes;
3. commit is created;
4. branch is pushed;
5. `git fetch origin` completes;
6. local `HEAD` equals `origin/<branch>`;
7. remote SHA is reported.

Use GitHub noreply commit identity.

## Completion format

Return:

- `STATUS: LANDED | TARGETED PROOF FAILED | BLOCKED`
- `OUTCOME:`
- `REMOVAL:`
- `FILES:`
- `PROOF:`
- `REMOTE SHA:`
- `BLOCKER:`
- `NEXT:`

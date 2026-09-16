# LIFE OS v2 Development Policy

**Status:** Canonical LIFE OS v2 development methodology.  
**Owner:** Jim Markunas. Material policy changes require Jim's explicit approval.  
**Architecture authority:** `docs/DECISIONS.md` and `docs/ARCHITECTURE.md`.  
**Live coordination:** Notion `Development Projects` is used only for current ownership, concurrent mutation, takeover, or handoff.

## 1. Mission

Ship useful LIFE OS product behavior through a codebase that is obvious enough to change safely in minutes, not hours.

`user outcome → bounded mutation → targeted proof → land or stop`

The development process must stay smaller than the product work.

## 2. Five-minute coding boundary

Every ordinary coding run is hard-bounded to five minutes or less for bootstrap, inspection, mutation, and targeted local proof.

A run ends as one of:

- `LANDED`
- `TARGETED PROOF FAILED: <exact failure>`
- `BLOCKED: <exact dependency>`

At the boundary, stop. Do not broaden scope, search history, invent recovery work, or wait on unrelated acceptance.

Larger work must be decomposed into coherent independently provable mutations, not meaningless micro-tasks.

## 3. Architecture guardrails

1. **ONE V2 PLATFORM.** v2 is the approved ground-up implementation. Do not create a second v2 architecture, scheduler, orchestration stack, or duplicate datastore/source of truth.
2. **ONE LIVE PATH PER BEHAVIOR.** A behavior has one production implementation. When v2 replaces a v1 boundary and acceptance is complete, retire the superseded live path when safe.
3. **DOMAIN FLOW.** Product flows should remain explainable as `Acquire → Normalize → Decide → Persist → Present`.
4. **DOMAIN OWNERSHIP.** Product-specific qualification, lifecycle, routing, prioritization, and presentation policy stay in the owning domain. Shared infrastructure owns only genuinely reusable mechanics.
5. **REUSE BEFORE ABSTRACTION.** Do not introduce a framework, dispatcher, adapter hierarchy, orchestration layer, or shared base class for one consumer.
6. **NO QA-ERA ARCHITECTURE.** Do not migrate numbered QA namespaces, trigger-file RPC, handoff manifests, Continuity-style coordination, or recovery frameworks into v2.
7. **GIT IS THE ARCHIVE.** Do not keep obsolete implementations or one-shot recovery utilities in the live product merely for reference.
8. **PUBLIC CODE / PRIVATE DATA.** Production secrets, personal identifiers, canonical private records, personal scoring policy, and private runtime IDs never enter Git history, fixtures, logs, or public tests.
9. **NO PRODUCTION GITHUB-HOSTED ACTIONS.** GitHub-hosted Actions may be used for bounded public CI only. LIFE OS production, recovery, or routine acceptance must not depend on billed Actions minutes, artifacts, or caches.
10. **NO SCHEDULER CREATION.** Do not create or enable a recurring scheduler in v2 without Jim's explicit approval. The previous `LIFE OS Daily Runs` scheduler is currently disabled during rebuild/cutover.

If requested work conflicts with locked architecture, report:

`ARCHITECTURAL DRIFT DETECTED — NO MUTATION`

## 4. Authority and bootstrap

Use the smallest live context that owns the question:

1. Jim's explicit current instruction.
2. This policy for development method.
3. `docs/DECISIONS.md` and `docs/ARCHITECTURE.md` for v2 architecture.
4. Relevant current Notion product-domain page for durable requirements.
5. Notion `Development Projects` only when ownership/concurrency/takeover/handoff matters.
6. Current implementation for what exists.
7. v1 repository only as a selective reference for proven algorithms, schemas, and production semantics explicitly being migrated.
8. Historical chats, old handoffs, retired QA artifacts, and prior phase documents are background only.

Ordinary bootstrap is limited to this policy, the relevant product canon, and the exact affected implementation surface/direct callers.

Do not perform repository-wide audits or historical archaeology unless a concrete dependency makes one necessary.

## 5. Fast path

1. Define the user-visible outcome.
2. Name the mutation surface.
3. Name the smallest deterministic proof.
4. Inspect narrowly.
5. Mutate once.
6. Remove superseded local code if applicable.
7. Prove once.
8. Commit/push and verify the remote SHA, or stop with the exact failure/blocker.

No silent scope expansion and no "while we're here" work.

## 6. Deterministic proof

Prefer local deterministic evidence over live-system debugging.

- Connector behavior: representative synthetic fixtures.
- Domain rules: one fast domain proof.
- Shared runtime/writer changes: targeted tests plus one real changed-boundary proof when required.
- Consequential source-system write: direct post-write verification of the actual target.

Do not rerun the same passing proof unless code changed or a new failure creates a new factual question.

## 7. Review

One coherent mutation gets one implementer and one Tech Lead review by default.

Review starts from the candidate diff and supplied proof:

1. inspect complete candidate surface;
2. check scope, semantics, security, and proof;
3. collect all material findings in one pass;
4. issue one consolidated correction request if needed;
5. re-review only the correction delta and affected semantics;
6. accept/land or stop.

Additional reviewers require a distinct safety reason.

## 8. Workspace and delivery

Use a clean isolated checkout/worktree or equivalent clean context. Never reset/stash/delete another agent's work.

Before mutation verify:

- correct repository;
- intended base/branch;
- no unrelated pre-existing diff;
- exact mutation boundary.

A package is not `LANDED` until it is committed, pushed, `origin` is fetched, and local `HEAD` equals the intended remote branch SHA.

One failed execution path gets one fallback. If both fail:

`ENVIRONMENT_BLOCKER — STOP`

## 9. Notion query safety

For Notion database/data-source querying, bulk QA, reconciliation, counts, duplicate detection, or migration analysis, follow `docs/notion-query-conservation-policy.md` before the first structured query.

Preferred order:

`exact fetch → saved view → repository-owned/public API read → metered rows/SQL only with Jim's explicit per-query approval`

A generic `go`, debugging need, or emergency does not authorize metered rows/SQL.

## 10. Failure rules

One failure boundary gets one retry owner. Retry the smallest deterministic failed unit once when useful.

Do not respond to a failure by creating infrastructure, recovery machinery, another workflow, another scheduler, or a new QA generation.

## 11. Completion contract

Every landed package reports:

- **OUTCOME**
- **REMOVAL**
- **PROOF**
- **REMOTE SHA**
- **NEXT**

Use terms consistently:

- `LANDED` — code is on the intended remote branch and targeted proof passed.
- `ACCEPTED` — the exact changed real boundary executed successfully and authoritative state was read back.
- `CLOSED` — accepted and no known defect/recovery debt remains in the promised outcome.
- `DEGRADED` — fail-closed safety state, never completion.

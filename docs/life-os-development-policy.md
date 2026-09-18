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
9. **GITHUB-HOSTED ACTIONS MAY EXECUTE PRODUCTION, NOT SCHEDULE IT.** Because `life-os-v2` is public, standard GitHub-hosted runners are approved as the stateless production executor for bounded v2 runtime work. `LIFE OS Daily Runs` remains the sole recurring scheduler. Production workflows may not use `schedule`/cron, larger/paid runners, Actions artifacts/caches as canonical state, or a second orchestration layer. Production execution must run approved code from protected `main`, receive secrets only through protected GitHub secrets/environment injection, preserve the 45-second benchmark and five-minute absolute runtime cap, and return bounded result/read-back evidence.
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

Ordinary bootstrap is limited to this policy, the relevant product canon, and the exact affected implementation surface/direct callers. For pre-scoped implementation, prefer exact symbols/direct callers over whole-file reads; file length or file count is never itself a required bootstrap scope.

Do not perform repository-wide audits or historical archaeology unless a concrete dependency makes one necessary.

### 4.1 Compiled implementation handoffs

For a pre-scoped package, the Tech Lead owns architecture/context compilation. The implementer should not need to rediscover the architecture before coding.

- Handoffs specify the changed claim, exact symbols/direct callers, allowed mutation surface, required proof, and stop conditions.
- Inspect at symbol/function level. Do not read whole files when only bounded sections are relevant.
- Reuse already-established context during an active package; do not reread unchanged canonical sources without a concrete ambiguity.
- Accepted packages are regression boundaries, not research assignments.
- One implementer owns one package by default. Do not run parallel rediscovery of the same implementation surface.
- State the expected mutation surface and proof before coding. Unexpected production-file expansion requires STOP and Tech Lead review.
- Targeted proof first; affected regression once; full suite once before landing.
- If an implementer must rediscover broad architecture to execute a pre-scoped package, treat the handoff as defective and return to the Tech Lead rather than expanding context.

### 4.2 Metered coding-agent prompt discipline

Codex and other metered coding agents are execution resources, not architecture-discovery resources. The Tech Lead/ChatGPT must do the architecture investigation and code-path identification before delegating whenever that work can be done without consuming the metered coding-agent allowance.

For a pre-scoped Codex or equivalent handoff, default to roughly 100–200 words and provide:

`exact repo/base → exact files/symbols → exact mutation → exact proof → stop`

- Do not send open-ended prompts such as “inspect the repo,” “read relevant requirements,” “determine the architecture,” or broad codebase exploration when the Tech Lead can compile that context first.
- Do not repeat governance already owned by this policy unless a specific rule is material to the mutation.
- The Tech Lead should identify the existing integration/client/presentation path and mutation surface before spending coding-agent credits.
- Codex should primarily edit and prove, not rediscover LIFE OS.
- If exact files/symbols cannot yet be named, keep the investigation with the Tech Lead until they can, unless Jim explicitly approves metered discovery.
- If a coding-agent prompt is materially longer or broader than necessary, stop and rescope it before execution.

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

### 5.1 Clean-as-you-touch

Lean efficiency is continuous package hygiene, not a future cleanup phase.

Every code-changing package owns the exact surface it touches. Before landing, remove directly-obsolete residue made visible or unnecessary by that mutation when removal is safe and provable within the package boundary, including:

- superseded local paths, wrappers, imports, branches, helpers, or configuration;
- obsolete tests or fixtures for behavior removed or replaced;
- exhausted one-shot diagnostics, migrations, or recovery utilities on the touched path;
- duplicate local implementation replaced by the canonical path.

This is not permission for "while we're here" cleanup. Do not expand into neighboring modules, repository-wide refactors, speculative abstraction, or unrelated debt. If cleanup requires new product semantics, a new architecture concept, or exceeds current file/line/time/token budgets, stop and report the concrete residue; it becomes separate work only if it blocks the active outcome or Jim explicitly promotes it.

A package may report `REMOVAL: none — no directly-obsolete residue on touched surface`, but the question must be asked before landing.

## 6. Deterministic proof

Prefer local deterministic evidence over live-system debugging.

- Connector behavior: representative synthetic fixtures.
- Domain rules: one fast domain proof.
- Shared runtime/writer changes: targeted tests plus one real changed-boundary proof when required.
- Consequential source-system write: direct post-write verification of the actual target.

Do not rerun the same passing proof unless code changed or a new failure creates a new factual question.

### 6.1 Package evidence standard

**Green CI proves the code runs. Boundary evidence proves the package works.**

Before implementation, every package must name:

- the changed claim;
- the failure mode that could still make that claim false;
- the lowest real boundary capable of exposing that failure;
- one negative control that must remain fail-closed or unchanged.

Every package requires:

1. **Targeted deterministic proof** for the exact changed logic.
2. **Lowest-real-boundary proof** using the nearest actual interface that can falsify the claim.
3. **Relevant regression proof** for previously accepted behavior the package can realistically affect.
4. **Main CI proof** on the merged SHA before acceptance.

"Lowest real boundary" does **not** mean "hit a live external system every time." Fake or control unreliable outside dependencies when useful, while keeping the business pipeline, repository contract, serialization/mapping, and decision logic real enough to expose the package's failure mode.

Use a real external source-system mutation/read-back only when that source system itself can invalidate the claim, or for final integrated acceptance. Consequential real writes still require direct post-write verification.

Prefer the smallest proof that can disprove the package. Do not create a testing framework, acceptance service, QA datastore, second canonical ledger, test orchestrator, evidence datastore, scheduler, or new recovery architecture to enforce this standard.

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

`REMOVAL` is mandatory clean-as-you-touch evidence: name what was deleted or consolidated, or report `none — no directly-obsolete residue on touched surface`.

Use terms consistently:

- `LANDED` — code is on the intended remote branch and targeted proof passed.
- `ACCEPTED` — the exact changed real boundary executed successfully and authoritative state was read back.
- `CLOSED` — accepted and no known defect/recovery debt remains in the promised outcome.
- `DEGRADED` — fail-closed safety state, never completion.

For package sequencing, use these review checkpoints without creating another workflow system:

`CODE GREEN → BOUNDARY PROVEN → MERGED → MAIN CI GREEN → PACKAGE ACCEPTED`

These are evidence checkpoints only. `PACKAGE ACCEPTED` means this policy's `ACCEPTED` standard has been met for the package. A dependent next package must not begin merely because a PR merged; it may begin only after the prior package is explicitly accepted.

A package acceptance report should stay concise and factual: exact changed claim, bounded files changed, targeted proof, boundary before/after evidence, negative control, relevant regression result, merged/main SHA, main CI result, and any known defect. Test counts alone are not acceptance evidence.

## 12. Hard efficiency budgets

These are mandatory stop conditions for every development agent, including ChatGPT, Codex, Claude, and role-specific agents.

1. **FRESH-MAIN GATE.** Before implementation or code-review analysis, fetch current `origin/main` and record its SHA. If main moved after prior analysis, stale implementation conclusions are invalid. Inspect only the affected delta before proceeding. Stop as `BASE_MOVED_STOP` if the new delta changes the claimed mutation surface.
2. **50-LINE CODE BUDGET.** A package/run may add or materially rewrite at most 50 production-code lines. Deletions are free. Formatting or moving code does not reset the budget. Above 50 requires Jim's explicit approval before continuing and a commit trailer `Jim-Approved-Budget: <justification>`.
3. **THREE-FILE DEFAULT.** A package/run may touch at most three production files by default. More requires Jim's explicit approval before file four. Two test files is the default test-surface ceiling.
4. **NET-BLOAT CHECK.** Bug fixes, cleanup, consolidation, and refactors should default to net production LOC <= 0. Positive net LOC requires a direct explanation of why deletion/reuse could not satisfy the changed claim.
5. **TOKEN BUDGET.** Claude and Codex may consume at most 2% of the active five-hour allowance in a single run. Target stop is 1.8%. At or above 2%, stop as `TOKEN_BUDGET_STOP` and ask Jim before continuing. If reliable usage telemetry is unavailable, broad autonomous exploration, repository-wide reading, and open-ended debugging are prohibited; the run must use exact files/symbols and one correction cycle maximum.
6. **60-MINUTE FEATURE SLA.** A major feature must reach a production acceptance attempt within 60 wall-clock minutes from implementation start. At 30 minutes reassess scope; at 45 minutes remove nonessential work; at 55 minutes prepare the acceptance attempt; at 60 minutes stop as `TIMEBOX_STOP` and report the exact blocker. Do not continue by default.
7. **DEVELOPMENT WASTE DEBT.** Time beyond the 60-minute SLA is reported at Jim's rate of $100/hour as `waste_debt = overrun_hours × $100`. This is an accountability metric, not a promise of cash, betting wins, or investment returns. Do not create gambling/trading automation to compensate for overruns. The next approved recovery package should simplify/delete/automate enough friction to repay at least the equivalent user time.
8. **TESTS ARE A LIABILITY BUDGET.** Repository target is at most 100 collected tests. While above 100, a code-changing package may add no tests and must delete obsolete or non-production-critical tests. Once at/below 100, CI hard-fails above 100 unless Jim explicitly approves an exception.
9. **PRODUCTION-CRITICAL TEST STANDARD.** A test survives only if it protects an accepted user-visible production path, a real regression, a canonical contract boundary, or a necessary fail-closed negative control. Tests for retired internals, duplicate implementation details, superseded runners, old QA phases, or behavior already covered by a stronger boundary test should be deleted.
10. **DELETE TESTS WITH CODE.** When behavior is removed, merged, replaced, or made unreachable, delete its obsolete tests in the same package. A refactor that reduces production paths but leaves their obsolete test surface behind is incomplete.
11. **TEST CREATION GATE.** At/below the 100-test ceiling, each new test requires a commit trailer `Production-Critical-Test: <specific failure prevented>`. Test count is never a quality metric.
12. **PROOF BUDGET.** During implementation run only the smallest targeted proof. Run the affected regression once. Run the full suite at most once before landing, then rely on main CI. Never rerun unchanged green proof.
13. **ONE IMPLEMENTER.** One mutation gets one implementer by default. Claude and Codex must not independently rediscover or solve the same package unless Jim or the Tech Lead identifies a distinct safety reason.
14. **TWO-CONSUMER ABSTRACTION RULE.** Do not create a shared abstraction, framework, dispatcher, base class, or generic helper layer until two active production consumers require it. One consumer means keep the logic local.
15. **ONE LIVE PATH / REMOVE THE OLD ONE.** Replacement work must retire the superseded live path as part of the same bounded sequence when safe. Git is the archive; dead runners, diagnostics, wrappers, and recovery utilities do not remain merely for reference.
16. **NO SCRIPT-TO-DOMAIN BACKFLOW.** Production/domain modules must not import implementation helpers from executable `scripts/*` surfaces. Scripts compose library/domain code; library/domain code does not depend on scripts.
17. **NO RABBIT-HOLE READS.** Read only the Development Policy, relevant product canon, exact affected symbols, and direct callers. Repository-wide audits, history archaeology, and unrelated source reads require a concrete dependency that is stated before expansion.
18. **STOP CODES.** Hard-stop labels are `BASE_MOVED_STOP`, `LINE_BUDGET_STOP`, `TOKEN_BUDGET_STOP`, `TIMEBOX_STOP`, `TEST_BUDGET_STOP`, and `SCOPE_EXPANSION_STOP`. A stop is not a failure to perform; it is the required behavior when a budget is exhausted.

The measurable subset is enforced by the existing CI workflow via `scripts/ci/development_guard.py`; this policy remains the authority when a rule cannot be measured by GitHub CI.

Every development completion report must also include `BUDGET: production LOC +/-, production files touched, tests +/-, token delta when observable, elapsed wall-clock time`.

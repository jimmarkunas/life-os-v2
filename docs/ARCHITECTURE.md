# LIFE OS v2 Architecture

## Product architecture

LIFE OS v2 is one composable platform. Shared infrastructure owns only reusable mechanics; each product domain owns its own policy and canonical writes.

Every product flow should be explainable as:

`Acquire → Normalize → Decide → Persist → Present`

Stages may be combined when they add no value. A feature must not create an orchestration framework simply to move data between adjacent stages.

## Runtime model

- `LIFE OS Daily Runs` remains the sole recurring scheduler under `jimmarkunas/life-os-automation/docs/life-os-production-contract.md`.
- ChatGPT owns scheduling, judgment, decomposition, review, approval-aware orchestration, and acceptance; it does not become the deterministic production runtime, recovery engine, classifier, or lane-by-lane execution loop.
- Standard free public GitHub-hosted Actions may execute approved bounded v2 production code from protected `main`; Actions do not schedule production or own canonical state.
- A scheduled feature request starts one bounded runtime execution.
- One execution owns the feature transaction from source read through canonical write/read-back.
- No feature may depend on a chain of GitHub jobs for internal state transitions.
- No Git commit is used as a datastore or message bus for production payloads.
- Failure is reported as DEGRADED with known-good canonical state preserved.

### Performance contract

- **Benchmark:** ≤45 seconds for a normal production feature execution.
- **Warning:** >45 seconds requires profiling before more functionality is added.
- **Hard stop:** 5 minutes. The runtime must terminate and report DEGRADED.
- Network-heavy acquisition such as Newsletter enrichment may use bounded parallel I/O, but it must still remain inside the 5-minute hard stop.

## Shared platform components

Only mechanics with multiple real consumers belong here:

- `core/runtime` — RunContext, deadlines, result/status model.
- `core/config` — validated private runtime configuration loaded from environment/secrets.
- `core/http` — bounded timeouts/retries, rate-limit handling, safe redaction.
- `core/security` — redaction, secret/PII guards, production-data write protection.
- `integrations/*` — thin Gmail, Outlook, Notion, Jira, Calendar, Todoist/other connector clients.
- `jobs/*` — canonical job identity, normalization, qualification, lifecycle, persistence contract used by Newsletter, US Remote, Scale-up, and later Skilled Worker.

Do not create a generic dispatcher, workflow engine, event bus, recovery framework, or second scheduler.

## Product domains

- `mail` — full-mailbox acquisition and routing.
- `newsletter` — routed-newsletter ingestion and source parsing.
- `jobs` — shared canonical Job engine and Job Ledger persistence.
- `hiring` — direct-human hiring/recruiter evidence and Hiring Pipeline state.
- `interviews` — interview context, preparation, coaching, notes, follow-up.
- `work` — Jira/accountability/current commitments.
- `dashboard` — Daily Command Center and other composable presentation surfaces.
- `followups` — temporary open loops sourced from canonical systems.
- `tasks` — lightweight personal action/capture integration.
- `finance` — private financial operating layer; no cardholder data in this repository.
- `habits` — recurring routine behavior.

## Newsletter target flow

One feature execution:

1. Scan the full Gmail/Outlook mailbox window.
2. Classify each new message as automated job source, human hiring, or unrelated.
3. Route confirmed automated job alerts immediately to the configured newsletter folder/label.
4. Fetch only new/unprocessed routed newsletters.
5. Parse vacancy observations.
6. Normalize, dedupe, and establish safe canonical Job identity through the shared Jobs Engine.
7. Idempotently upsert/reconcile the canonical private Job Ledger and verify authoritative read-back.
8. Resolve terminal employer/ATS evidence with bounded parallel I/O; reconcile stronger evidence back to the same canonical Job and verify read-back.
9. Evaluate Fit, evidence authority, lane eligibility, freshness, compensation, work-mode/visa, and other qualification state against the persisted canonical Job. Evaluation controls presentation/actionability, not canonical existence.
10. Mark/checkpoint source mail only after every legitimate vacancy is durably persisted/reconciled with authoritative read-back or reaches a true terminal source/identity disposition. Fit/scoring failure, low Fit, or missing JD must not strand otherwise durably ingested source mail.

There is no separate census artifact, handoff manifest, trigger publication, second ingestion job, global ledger repair pass, or per-lane Continuity job.

## Jobs codebase simplification roadmap

This roadmap is implementation guidance subordinate to the canonical Jobs OS product canon and the cross-repository Development Policy. It is **not active until `JOBS_USER_OUTCOME_GATE` is executable and passing and R0 has restored the working Newsletter Jobs vertical slice**. Do not start cleanup while either Primary User Imperative is broken.

Target state for Codex/Tech Lead work:

- one obvious production entry point per behavior;
- one obvious owner per responsibility;
- one canonical policy authority, with repo-local docs pointing rather than competing;
- one executable end-to-end Jobs outcome gate;
- minimal wrappers and compatibility paths;
- no historical architecture living beside current architecture merely for reference.

### TST-1 — TestKit consolidation

Immediately after current R0 acceptance, migrate the existing scattered collected tests into the composable TestKit layer. Preserve meaningful behavioral coverage while collapsing duplicate setup and scenario variants into a smaller number of durable contract tests. Scenario data and reusable setup belong in TestKit rather than one collected test function per case.

**Exit:** existing behavior remains covered, the collected-test surface is materially reduced, and TestKit is the normal home for reusable test mechanics/cases.

### TST-2 — New-test staging guardrail

All newly created test scenarios must enter `tests/testkit/candidates.py` first rather than creating new collected `test_*` functions/files across the product tree. The CI/development guard must reject newly collected tests outside the approved TestKit architecture while grandfathering existing tests until TST-1 migrates them. Do not raise the test limit or weaken the existing ratchet.

**Exit:** new test scenarios can be added without increasing collected-test count; additions outside the approved TestKit path fail closed.

### TST-3 — Candidate inventory and triage

`tests/testkit/candidates.py` is a temporary staging inventory for Jim + Tech Lead/ChatGPT review, not a permanent archive or second framework. Periodically classify staged scenarios as `PRODUCTIZE`, `MERGE_DEDUPLICATE`, or `KILL`, then move durable coverage into the appropriate TestKit contract or delete temporary/redundant cases.

**Exit:** every staged candidate has an explicit disposition; temporary investigation tests do not accumulate indefinitely.

TST-1 through TST-3 run after R0 acceptance and before JCS-1. They are test-surface consolidation/guardrail work, not product behavior changes.

### JCS-1 — Execution-surface convergence

Inspect the remaining Newsletter workflow/entry surfaces, especially `.github/workflows/newsletter-preflight-manual.yml`, `.github/workflows/newsletter-uat-manual.yml`, and the canonical production runtime. Preserve any unique operator/preflight capability, but retire or converge redundant execution surfaces once the same behavior is proven elsewhere. Do not create another workflow or orchestration layer.

**Exit:** fewer ambiguous Newsletter execution surfaces; `JOBS_USER_OUTCOME_GATE = PASS`.

### JCS-2 — Jobs ownership and entrypoint simplification

Reduce ambiguity among neighboring Newsletter/Jobs modules such as `newsletter_adapter.py`, `newsletter_contract.py`, and `newsletter_runtime.py`. Keep separation only where responsibilities are materially distinct. Prefer deletion/thinning of wrappers and one clear composition root over new abstractions. Do not change user behavior.

**Exit:** a maintainer can identify acquisition/parsing, canonical identity/persistence, enrichment, scoring, presentation, and source-finalization owners without competing paths; `JOBS_USER_OUTCOME_GATE = PASS`.

### JCS-3 — Instruction-entropy reduction

Review `AGENTS.md`, `CLAUDE.md`, `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/REBUILD_PLAN.md`, role docs, and repo-local policy pointers for stale or overlapping instructions. Preserve required repository-routing/security mechanics, but convert obsolete V2 migration/rebuild guidance into concise pointers to current canonical authority or remove it when Git history is sufficient. Do not create another governance document.

**Exit:** Codex/Claude have one obvious authority chain and no stale roadmap/architecture text that competes with current Jobs OS canon.

### JCS-4 — Dead compatibility/wrapper subtraction

After current behavior is proven, remove obsolete wrappers, compatibility paths, retired migration scaffolding, and duplicated helpers that no longer have a live caller or unique production purpose. Git is the archive; do not retain dead implementation merely as reference.

**Exit:** no superseded live path remains beside the canonical path; targeted proofs and `JOBS_USER_OUTCOME_GATE` pass.

These packages are subtractive simplification, not a redesign. Each package must preserve the last known-good vertical slice and follow: `working slice → remove one ambiguity → run the same gate → continue only on PASS`.

## Public-code / private-data boundary

Public repository may contain:

- reusable source code;
- schemas;
- generic defaults;
- documentation;
- synthetic fixtures;
- tests.

Public repository must never contain:

- credentials/tokens/secrets;
- personal email addresses or message IDs/content;
- production Notion/Jira/Calendar IDs or URLs;
- financial/account data;
- cardholder data or sensitive authentication data;
- production runtime checkpoints/snapshots;
- personalized tracking URLs;
- actual hiring/interview/job records.

Production configuration is injected at runtime through protected secrets/environment variables and private canonical systems.
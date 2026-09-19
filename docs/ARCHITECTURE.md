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
6. Resolve terminal employer/ATS evidence with bounded parallel I/O.
7. Normalize, dedupe, score, and qualify through the shared Jobs Engine.
8. Idempotently upsert the canonical private Job Ledger.
9. Mark/checkpoint source mail only after persistence succeeds.

There is no separate census artifact, handoff manifest, trigger publication, second ingestion job, global ledger repair pass, or per-lane Continuity job.

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
# LIFE OS v2 Decision Log

Only durable architectural decisions belong here. Product-specific requirements stay in the owning Notion domain page.

## D-001 — Public code, private production data

The repository is public. Production data, credentials, private identifiers, canonical records, runtime checkpoints, and personal configuration never enter Git history, Actions artifacts/caches, fixtures, or logs.

## D-002 — Ground-up architecture, selective logic harvest

v2 is not a lift-and-shift of v1 orchestration. Proven algorithms may be reimplemented or selectively harvested after sanitization; v1 workflow/recovery/trigger architecture does not migrate by default.

## D-003 — One composable platform

Shared infrastructure exists only for mechanics with multiple real consumers. Domain-specific policy remains in its domain. No second scheduler, generic workflow engine, event bus, duplicate datastore, or per-domain technology stack.

## D-004 — One bounded execution per feature

A production feature execution owns its transaction from source read through canonical persistence/read-back. Internal state transitions do not spawn separate runner jobs.

Benchmark is 45 seconds. Five minutes is the absolute platform runtime limit. Timeout means DEGRADED.

## D-005 — Newsletter first vertical slice

The first production-critical vertical slice is full mailbox scan → classification/routing → routed Newsletter ingestion → shared Career/Jobs Engine → canonical private Job Ledger.

Confirmed automated job alerts are routed before Newsletter ingestion. Human hiring mail remains separate.

## D-006 — GitHub is not the production datastore

GitHub holds source, tests, documentation, and execution definitions. Canonical state stays in Gmail/Outlook, Notion, Jira, Calendar, Todoist/approved task system, connected Finance sources, or another explicitly approved canonical system.

## D-007 — Security boundary

Production secrets are provided only to trusted production execution on approved code. Pull-request/fork code never receives production secrets. Production payloads are not uploaded as Actions artifacts or caches.

Payment card data and sensitive authentication data are prohibited from this repository and its CI/runtime logs. Finance integrations must keep such data inside approved providers/private systems.

## D-008 — Documentation without bloat

- Product decisions: owning Notion domain page.
- Cross-platform architecture decisions: this file.
- Implementation evidence/reasoning: PR.
- Current concurrency/ownership: Notion `Development Projects` only.

No implementation diary or duplicate roadmap is created.

## D-009 — Parallel delivery with strict ownership

v2 development uses parallel workstreams with non-overlapping mutation surfaces.

- Tech Lead 1 owns architecture, integration, merge/cutover decisions, and production-boundary acceptance.
- Tech Lead 2 is the independent reviewer/second pair of eyes and does not implement by default.
- Product Manager owns requirements decomposition and UAT.
- Agent 1 owns shared platform core/integrations.
- Agent 2 owns Mail Intelligence and Newsletter.
- Claude Code owns heavy multi-file Career/Jobs implementation and sanitized logic harvest.
- Codex owns public-repo security, CI, synthetic-data protection, and performance/security harnesses.

One implementation owner exists per package. Review is consolidated rather than creating reviewer chains, duplicate agents, or parallel implementations of the same behavior.

# LIFE OS v2 Rebuild Plan

This is a ground-up rebuild of the platform architecture, not a lift-and-shift of v1 orchestration. Proven domain logic may be harvested only after review and sanitization.

## Delivery plan

| Step | Outcome | Parallel owner | Key deliverables | Production acceptance |
|---:|---|---|---|---|
| 0 | Public-safe foundation | Security/CI agent | `.gitignore`, synthetic-test policy, secret/PII scan, PR-safe CI, protected production workflow rules | Public repo contains no production/private data; PR code receives no production secrets |
| 1 | Shared runtime kernel | Platform agent | RunContext, deadline budget, standard result model, config loader, bounded HTTP/retry/redaction utilities | Synthetic run proves one request → one bounded result; hard stop at 5:00 |
| 2 | Shared Jobs Engine | Jobs agent | Stable Job Key, normalized candidate schema, dedupe, Fit/qualification interfaces, lifecycle, idempotent Job Ledger repository | Same synthetic candidate produces identical result across source adapters |
| 3 | Mail Router v2 | Mail/Newsletter agent | Full Gmail/Outlook scan, automated/human/unrelated classification, immediate newsletter-folder routing, private checkpoint | Normal mailbox routing benchmark ≤45s; no persisted mail payload in GitHub |
| 4 | Newsletter vertical slice | Mail/Newsletter + Jobs agents | Routed-folder fetch, parsers, bounded parallel employer evidence, shared Jobs Engine qualification, idempotent Job Ledger upsert | End-to-end real-source PASS; target ≤90s, hard stop ≤3:00 for Newsletter, absolute platform cap 5:00 |
| 5 | Jobs acquisition expansion | Jobs + acquisition agents | US Remote and Scale-up adapters feeding the same Jobs Engine | No duplicate identity/qualification/persistence code; each lane ≤5:00 |
| 6 | Hiring + Interview core | Hiring/Interview agent | Human hiring routing, Hiring Pipeline reconciliation, interview context/prep/notes/follow-up | Hiring remains independent of Newsletter failure; no second hiring-status datastore |
| 7 | Work + Dashboard core | Dashboard/Work agent | Jira commitments, GTV action, sprint/accountability projection, Daily Command Center | DCC reads canonical systems directly; normal refresh benchmark ≤45s |
| 8 | Personal action layer | Tasks/Followup agent | Todoist/lightweight capture, promotion rules, Followup OS states and DCC projection | No duplicate task datastore; canonical ownership preserved |
| 9 | Accountability + coaching | Product agents | Megibow activity dashboard; Interview OS Straight Line coaching/navigation | Deterministic source-to-output behavior; no generic duplicate framework |
| 10 | Finance + Habits | Finance/Habits agent | Finance operating views/workflows and routine tracking | No payment-card data in repo/logs/artifacts; private sources remain canonical |
| 11 | Deferred expansion | Jobs/Library/Networking agents | Skilled Worker Phase 2, LinkedIn connection integration, Library backlog | Reuse existing shared components; no separate platform stack |
| 12 | Cutover | Tech Lead | Shadow production, performance/security proof, switch `LIFE OS Daily Runs` to v2, archive v1 runtime | Multiple clean production runs; every enabled feature <5:00; v1 production disabled |

## Initial parallel workstreams

These four streams may start immediately because their mutation surfaces are intentionally separate:

| Workstream | Branch | Owns | Must not own |
|---|---|---|---|
| Platform Core | `agent/platform-core` | `lifeos/core/**`, base runtime/config interfaces | Mail parsing, Jobs policy, CI/security policy |
| Mail + Newsletter | `agent/mail-newsletter` | `lifeos/mail/**`, `lifeos/newsletter/**`, mail fixtures/tests | Jobs qualification/persistence internals |
| Jobs Engine | `agent/jobs-engine` | `lifeos/jobs/**`, Jobs fixtures/tests | Mailbox routing, GitHub workflow/security configuration |
| Security + CI | `agent/security-ci` | `.github/**`, `SECURITY.md`, repo leak guards, synthetic-data policy | Product-domain behavior |

Integration rule: one PR per coherent package; no agent may edit another workstream's owned surface without Tech Lead approval.

## Runtime SLO

| Runtime class | Benchmark | Hard limit |
|---|---:|---:|
| Normal API-backed feature | 45 sec | 5 min |
| Dashboard/report projection | 45 sec | 5 min |
| Mail routing | 45 sec | 5 min |
| Newsletter end-to-end | 90 sec target | 3 min target cap; 5 min absolute platform kill |
| Any feature | — | **5 min maximum** |

A hard-limit breach returns DEGRADED and stops. It does not create a new recovery subsystem.

## Decision/documentation rule

- Durable product decisions stay in the owning Notion domain page.
- Public architectural decisions stay in `docs/DECISIONS.md`.
- GitHub PRs hold implementation reasoning tied to code changes.
- Notion `Development Projects` holds only active ownership/concurrency.
- Do not create duplicate planning databases, implementation diaries, or per-package governance documents.

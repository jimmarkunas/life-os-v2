# LIFE OS v2 Rebuild Plan

This is a ground-up rebuild of the platform architecture, not a lift-and-shift of v1 orchestration. Proven domain logic may be harvested only after review and sanitization.

## Delivery plan

| Step | Outcome | Parallel owner | Key deliverables | Production acceptance |
|---:|---|---|---|---|
| 0 | Public-safe foundation | Codex | `.gitignore`, synthetic-test policy, secret/PII scan, PR-safe CI, protected production workflow rules | Public repo contains no production/private data; PR code receives no production secrets |
| 1 | Shared runtime kernel | Agent 1 | RunContext, deadline budget, standard result model, config loader, bounded HTTP/retry/redaction utilities | Synthetic run proves one request → one bounded result; hard stop at 5:00 |
| 2 | Shared Career / Jobs Engine | Claude Code | Company/Job identity, Stable Job Key, normalized candidate schema, dedupe, Fit/qualification interfaces, lifecycle, idempotent Job Ledger repository | Same synthetic candidate produces identical result across source adapters |
| 3 | Mail Router v2 | Agent 2 | Full Gmail/Outlook scan, automated/human/unrelated classification, immediate newsletter-folder routing, private checkpoint | Normal mailbox routing benchmark ≤45s; no persisted mail payload in GitHub |
| 4 | Newsletter vertical slice | Agent 2 + Claude Code | Routed-folder fetch, parsers, bounded parallel employer evidence, shared Jobs Engine qualification, complete disposition, idempotent Job Ledger upsert/read-back | End-to-end real-source PASS; target ≤90s, hard stop ≤3:00 for Newsletter, absolute platform cap 5:00 |
| 5 | Career acquisition expansion | Claude Code | US Remote and Scale-up adapters feeding the same Career/Jobs Engine | No duplicate identity/qualification/persistence code; each lane ≤5:00 |
| 6 | Hiring + Interview core | Agent 2 + Claude Code | Human hiring routing, Hiring Pipeline reconciliation, interview context/prep/notes/follow-up | Hiring remains independent of Newsletter failure; no second hiring-status datastore |
| 7 | Work + Dashboard core | Agent 1 | Jira commitments, GTV action, sprint/accountability projection, Daily Command Center | DCC reads canonical systems directly; normal refresh benchmark ≤45s |
| 8 | Personal action layer | Agent 1 + Agent 2 | Todoist/lightweight capture, promotion rules, Followup OS states and DCC projection | No duplicate task datastore; canonical ownership preserved |
| 9 | Accountability + coaching | Claude Code | Megibow activity dashboard; Interview OS Straight Line coaching/navigation | Deterministic source-to-output behavior; no generic duplicate framework |
| 10 | Finance + Habits | Agent 1 | Finance operating views/workflows and routine tracking | No payment-card data in repo/logs/artifacts; private sources remain canonical |
| 11 | Deferred expansion | Assigned by Tech Lead 1 at promotion | Skilled Worker Phase 2, LinkedIn connection integration, Library/Archive/Style backlog | Reuse existing shared components; no separate platform stack |
| 12 | Cutover | Tech Lead 1 | Shadow production, performance/security proof, switch `LIFE OS Daily Runs` to v2, archive v1 runtime | Multiple clean production runs; every enabled feature <5:00; v1 production disabled |

## Delivery team ownership

| Role | Primary responsibility | Active Wave 1 surface | Guardrail |
|---|---|---|---|
| **Tech Lead 1 — ChatGPT** | Architecture owner, interface decisions, integration order, merge/cutover decisions, production-boundary acceptance, performance enforcement | Cross-workstream integration; no default feature branch | Does not duplicate implementation owned by an agent |
| **Tech Lead 2** | Independent second pair of eyes: diff review, architecture/security/performance review, correction consolidation | Read-only review of bounded integration PRs | Reviews; does not become a shadow implementer unless explicitly reassigned |
| **Product Manager — ChatGPT** | Requirements decomposition, user-visible acceptance criteria, UAT design/execution, Notion domain requirements | Newsletter/Career UAT first | Product requirements live in owning Notion domain pages; no duplicate requirements repo |
| **Agent 1 — ChatGPT** | Shared platform kernel and reusable platform mechanics | `agent/platform-core` → `lifeos/core/**`, `lifeos/integrations/**` | No Career policy, Newsletter parsing, or CI/security ownership |
| **Agent 2 — ChatGPT** | Mail Intelligence and Newsletter product flow | `agent/mail-newsletter` → `lifeos/mail/**`, `lifeos/newsletter/**` | Consumes shared Career/Jobs interfaces; does not implement duplicate qualification/persistence |
| **Claude Code** | Heavy multi-file Career implementation and selective v1 logic harvest | `agent/jobs-engine` → `lifeos/career/**` | No v1 orchestration migration; sanitize everything before crossing into public repo |
| **Codex** | Public-repo security, CI, synthetic fixtures policy, leak prevention, performance harness | `agent/security-ci` → `.github/**`, security tooling/tests | No production data or product-domain business logic |

### Review and merge flow

`Product Manager acceptance criteria → implementing owner → Tech Lead 2 review → Tech Lead 1 integration/merge decision → Product Manager UAT → Tech Lead 1 production acceptance when applicable`

One review pass should consolidate all material findings. Do not create serial reviewer chains or parallel implementations of the same package.

## Initial parallel workstreams

These four streams start in parallel because their mutation surfaces are intentionally separate:

| Workstream | Branch | Owner | Owns | Must not own |
|---|---|---|---|---|
| Platform Core | `agent/platform-core` | Agent 1 | `lifeos/core/**`, `lifeos/integrations/**`, base runtime/config interfaces | Mail parsing, Career policy, CI/security policy |
| Mail + Newsletter | `agent/mail-newsletter` | Agent 2 | `lifeos/mail/**`, `lifeos/newsletter/**`, mail fixtures/tests | Career qualification/persistence internals |
| Career / Jobs Engine | `agent/jobs-engine` | Claude Code | `lifeos/career/**`, Career fixtures/tests, sanitized reusable v1 Career logic | Mailbox routing, GitHub workflow/security configuration |
| Security + CI | `agent/security-ci` | Codex | `.github/**`, `SECURITY.md`, leak guards, synthetic-data policy, performance/security harness | Product-domain behavior |

Integration rule: one PR per coherent package; no agent may edit another workstream's owned surface without Tech Lead 1 approval.

## First integration milestone — Newsletter

The first production-critical vertical slice is:

`whole-mailbox scan → classify → move confirmed automated alerts to J Newsletters → fetch J Newsletters → parse → bounded parallel employer evidence → shared Career/Jobs Engine → complete disposition → idempotent Job Ledger write → authoritative read-back → checkpoint`

There are no Git handoff files, trigger-file RPC layers, per-message runtime artifacts, separate Continuity jobs, or full-ledger rescans in the normal hot path.

Acceptance requires:

- every extracted vacancy ends as `created / updated / duplicate / excluded / REVIEW-DEGRADED`;
- one canonical vacancy regardless of source count;
- source cleanup only after complete reconciliation and persistence read-back;
- no human hiring message routed as a Newsletter;
- zero production/private data committed to the public repo;
- target runtime ≤90 seconds, Newsletter target cap ≤3:00, absolute platform kill at 5:00.

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

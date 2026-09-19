# LIFE OS v2

> **ACTIVE IMPLEMENTATION REPOSITORY.** All current LIFE OS product development, fixes, refactors, tests, runtime implementation, configuration work, branches, and pull requests belong here. `jimmarkunas/life-os-automation` is V1/reference-only implementation code and is consulted only for explicit reuse/comparison or still-canonical governance/runtime documents. See `AGENTS.md`.

LIFE OS v2 is a public-code/private-data personal operating platform.

The repository contains reusable platform code only. Production credentials, personal data, account identifiers, message content, financial data, runtime state, and canonical records must never be committed.

## Platform rules

- One composable platform; shared mechanics, domain-owned policy.
- Product flow: `Acquire → Normalize → Decide → Persist → Present`.
- One bounded runtime invocation per feature execution.
- Typical production feature benchmark: **≤45 seconds**.
- Hard production timeout: **5 minutes**. A timeout is DEGRADED, never success.
- GitHub is source code + CI/execution; canonical production data stays in its owning private system.
- Public CI uses synthetic data only. Production secrets are never exposed to pull-request code.
- No production payloads in GitHub commits, Actions artifacts, Actions caches, fixtures, or logs.

## Current priority

The first complete vertical slice is **Mail Intelligence → Newsletter → shared Jobs Engine → canonical Job Ledger**.

Target Newsletter flow:

`full mailbox scan → classify → route automated alerts → fetch routed newsletters → parse → enrich in parallel → dedupe/qualify → idempotent Job Ledger upsert → checkpoint`

No census-file maze, handoff manifests, trigger chains, per-step virtual machines, or duplicate persistence layers.

## Docs

- [Repository role lock](AGENTS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Rebuild plan](docs/REBUILD_PLAN.md)
- [Decision log](docs/DECISIONS.md)
- [Security](SECURITY.md)

# US Remote Runtime Consolidation Brief

**Status:** Immediate bounded follow-up after Newsletter production incident closure.  
**Owner:** Tech Lead assigns one implementer only.  
**Purpose:** Reduce implementation surface and agent context cost without changing product behavior or architecture.

## Why this exists

The Thin US Remote Composition Root extraction reduced `scripts/run_us_remote_production.py` from 588 lines to 165 lines, but moved roughly 500 lines of execution mechanics into `lifeos/jobs/us_remote_runtime.py`. The original package was only about +77 net production LOC, not +500 net; however, the result concentrated too much cross-domain glue in one runtime module and subsequent reliability work increased that surface further.

The problem is therefore **concentration and context cost**, not merely raw repository size.

## Outcome

Make the US Remote production path obvious enough that a bounded defect normally requires reading one small orchestration function plus the directly affected domain primitive—not a 500+ line runtime module.

## Locked constraints

- No behavior change.
- No new scheduler, queue, datastore, checkpoint, recovery framework, dispatcher, plugin system, or generic orchestration framework.
- Preserve one shared Jobs ingest and canonical Job Ledger.
- Preserve Gmail queue/Processed semantics and accepted Newsletter/Jobs identity, Fit, persistence, lifecycle, and terminal-evidence semantics.
- Do not split code merely to lower line counts. Extraction is allowed only when a responsibility already has a clear existing domain owner or when duplicated glue can be deleted.
- Prefer deletion/consolidation over abstraction.

## Required method

1. Measure the current runtime by responsibility/function and direct callers.
2. Identify code that belongs in an existing domain module, duplicated glue, compatibility re-exports, and test-only coupling.
3. Produce a deletion/move plan before mutation with expected net production LOC change.
4. Execute one bounded consolidation pass.
5. Prove behavior preservation with targeted runtime regressions plus one full CI gate.

## Acceptance

- `run_us_remote_production.py` remains a thin composition root.
- `us_remote_runtime.py` no longer contains unrelated transport/fetcher/config/policy mechanics that already have an existing domain home.
- No duplicated implementation is introduced.
- Net production LOC must decrease; a zero/increase result is not accepted without Jim's explicit approval.
- Existing production result shape, exit semantics, fail-closed behavior, and accepted E2E Newsletter behavior remain unchanged.

This work must not interrupt the active Newsletter production incident. Production recovery comes first; consolidation begins immediately after authoritative E2E closure.
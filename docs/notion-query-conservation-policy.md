# LIFE OS v2 Notion Query Supplement

**Status:** Repository-specific supplement for `jimmarkunas/life-os-v2`.  
**Canonical authority:** `jimmarkunas/life-os-automation/docs/notion-query-conservation-policy.md`.

This file intentionally does not duplicate the full LIFE OS Notion Query Conservation Policy.

## v2-specific rules

- Exact page/data-source/view fetch remains the preferred first read when the target is known.
- Saved non-metered views are preferred when a filtered row set is required.
- Repository-owned Python using the official Notion Public API may perform bounded structured reads through an already-approved v2 execution boundary when the canonical query policy permits it.
- Standard free public GitHub-hosted Actions in `life-os-v2` may execute approved bounded Notion-related production/read-back work from protected `main`; they may not schedule production, create a second orchestration layer, expose private payloads, or create a new workflow merely to avoid the query-approval gate.
- Metered ChatGPT/MCP rows/SQL remains last resort and requires Jim's explicit approval for the exact query or bounded batch.
- Generic `go`, debugging need, surrounding task approval, or emergency does not authorize metered rows/SQL.
- Unattended runtime may not depend on an approval-gated metered path. If lower-cost evidence cannot prove required state, fail closed as `DEGRADED` and preserve known-good state.
- Production Notion tokens, private data-source IDs, canonical records, and private payload fixtures never enter this public repository.
- Query conservation never weakens Stable Job Key identity/dedupe, human-owned lifecycle state, Applied preservation, or authoritative read-back.

If this supplement conflicts with the canonical policy, the canonical `life-os-automation` policy wins and this file must be corrected.
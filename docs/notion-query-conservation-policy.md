# LIFE OS v2 Notion Query Conservation Policy

**Status:** Normative LIFE OS v2 rule for interactive QA/repair and future runtime work.

## Goal

Operate within the current Notion plan without making metered ChatGPT/MCP Notion rows/SQL queries a normal dependency.

## Startup enforcement

Any agent whose task may involve Notion database/data-source querying, bulk QA, reconciliation, counting, duplicate detection, migration analysis, or structured inspection must read this policy before its first structured query.

Exact page/data-source/view fetch is allowed. Saved non-metered views are preferred when available. Bulk structured inspection should use repository-owned code plus the official Notion Public API through an authorized execution path rather than repeated metered connector queries.

## Approval gate

Notion rows/SQL is a last-resort path.

- Interactive work: before any metered rows/SQL query, ask Jim for explicit approval for that specific query or clearly bounded batch.
- The request must state why it is needed, what lower-cost path was attempted or why it is insufficient, and the exact scope.
- Generic task approval, `go`, debugging need, or emergency does not authorize rows/SQL.
- Approval does not persist across unrelated queries, turns, tasks, or projects.
- Unattended runtime may not use an approval-gated metered path. If lower-cost reads cannot prove required state, return `DEGRADED` and preserve known-good state.

A violation must be reported as:

`GUARDRAIL VIOLATION — NOTION METERED QUERY PATH`

and the metered path must stop unless Jim explicitly re-authorizes it after being informed.

## Query precedence

Use the lowest-cost reliable method that can prove the required state:

1. Exact fetch by known page/data-source/view identifier.
2. Saved view when a filtered row set is needed and a non-metered view exists.
3. Repository-owned Python using the official Notion Public API for bulk structured reads, exact-key reconciliation, aggregate/invariant validation, or incremental sync.
4. Metered ChatGPT/MCP rows/SQL only when none of the above is sufficient and Jim explicitly approves that exact query/batch.

## Hard rules

- Never spend a metered query to rediscover data already available from a known exact target, saved view, current repository snapshot, or exact-key lookup.
- Batch required fields into one official-API read rather than repeated connector queries.
- Do not repeat a metered query unless the prior response was incomplete/ambiguous, no lower-cost path can resolve it, and Jim explicitly approves the retry.
- Entitlement/quota failure is `DEGRADED`, never zero/empty-success.
- Preserve known-good state when any Notion read path is degraded.
- Query conservation must never weaken exact Stable Job Key dedupe, human-owned lifecycle state, Applied preservation, or authoritative read-back requirements.
- Public repository code must not contain production Notion tokens, private data-source IDs, personal records, or production payload fixtures.

## Public API supplement

When repository-owned code performs bulk or exact-key Notion reads:

- query only explicitly configured private data sources;
- paginate completely;
- respect 429 `Retry-After` responses through the shared bounded HTTP/retry boundary;
- fail closed on permission, timeout, truncation, malformed response, or incomplete pagination;
- never log or commit the Notion API token;
- do not create a new workflow merely to avoid the approval gate;
- do not use GitHub-hosted Actions as production Notion compute.

For canonical Jobs, prefer exact/narrow Stable Job Key lookup over full-ledger scans.

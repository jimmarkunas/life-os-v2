# LIFE OS v2 Development Supplement

**Status:** Repository-specific supplement for `jimmarkunas/life-os-v2`. The canonical cross-repository Development Policy is:

`jimmarkunas/life-os-automation/docs/life-os-development-policy.md`

It owns generic development method and governance. This supplement does not duplicate or override it. `AGENTS.md` owns V2 repository routing, the mandatory implementation header, V1 boundary, and optional role-entry triggers; `CLAUDE.md` is the thin Claude Code entry guide.

## V2 authority pointers

- Architecture authority: `docs/DECISIONS.md` and `docs/ARCHITECTURE.md`.
- Scheduled/runtime authority: `jimmarkunas/life-os-automation/docs/life-os-production-contract.md`.
- Live coordination: Notion `Development Projects` only for ownership, concurrency, takeover, or handoff when the canonical policy requires that read.
- Product requirements: the relevant current Notion product/domain canon.

## V2-specific repository mechanics

V2 is public code and the sole active implementation repository. Production secrets, private identifiers, canonical records, personal scoring policy, runtime checkpoints, and private payloads remain outside Git, fixtures, logs, Actions artifacts, and caches. V1 implementation is reference-only under the boundary in `AGENTS.md`.

The existing V2 CI guard at `scripts/ci/development_guard.py` enforces repository-level budget metadata, test-ratchet/justification, and `Production-Frozen` surface checks derived from canonical policy. It is an enforcement mechanism, not a policy owner.

V2-specific prohibitions remain: no GitHub Actions workflow chains, and no migration of V1 handoff manifests or Continuity-style coordination into V2. These remain subordinate to the canonical Development Policy, `docs/DECISIONS.md`, and `docs/ARCHITECTURE.md`.

## V2 TestKit boundary

Jim-approved test-only helpers, when a task genuinely needs them, live under:

`tests/testkit/{cases.py,builders.py,boundaries.py,harnesses.py}`

They must be immediately consumed by real tests, reduce duplicated setup/context, own no product policy, and remain outside production dependencies. The canonical policy’s generic TestKit restrictions apply.

## Notion policy relationship

`docs/notion-query-conservation-policy.md` is a V2 pointer/supplement. The canonical rule remains:

`jimmarkunas/life-os-automation/docs/notion-query-conservation-policy.md`

The local supplement may explain V2 execution boundaries but cannot authorize metered rows/SQL beyond the canonical policy.

# LIFE OS v2 GitHub Actions Conservation Policy

**Status:** LIFE OS v2 development/CI operating guardrail.

## Goal

Keep GitHub Actions as bounded public CI, not production/recovery infrastructure, while minimizing billed runner minutes and latency.

## Rules

1. **No production runtime on GitHub-hosted Actions.** Production, recovery, recurring acceptance, and scheduler execution must not depend on billed GitHub-hosted Actions minutes, artifacts, or caches.
2. **CI stays narrow.** Ordinary pushes/PRs run only the bounded leak/security/unit checks required to protect the public repository.
3. **Heavy acceptance is explicit and local/self-hosted when needed.** Do not attach broad real-system acceptance to ordinary pushes.
4. **Reuse before creating.** Do not create a workflow for an incident, import, QA gate, migration, recovery slot, or retry when local/direct execution can perform it.
5. **No timestamp-specific recovery workflows.** One-shot recovery workflow YAML does not remain live after bounded use.
6. **Batch safe changes.** Do not spend a full CI/acceptance cycle after every micro-edit.
7. **Two-failure rule.** After two failures using the same bounded approach, stop retrying and reassess the boundary.
8. **Least privilege.** Default workflow permission is `contents: read`; write permissions require an explicit narrow justification.
9. **No workflow cascades.** Generated commits must not fan out into unrelated heavyweight jobs.
10. **Historical QA is inert.** Do not recreate v1 QA/recovery workflow families in v2.
11. **No scheduler creation.** No GitHub workflow may create, enable, replace, or emulate a LIFE OS recurring scheduler without Jim's explicit approval.
12. **Secrets stay secret.** CI must never print, artifact, cache, or commit production secrets/private payloads.
13. **Guardrail cleanup consumes no Actions by default.** Verify guardrail/docs/workflow cleanup by source inspection and local/static proof rather than triggering CI solely for documentation changes.
14. **Public-repo protection is mandatory.** Leak guard, synthetic fixture enforcement, and commit-metadata privacy remain required CI boundaries.

## Development fast path

Canonical development method lives in `docs/life-os-development-policy.md`.

Default:

`inspect once → bounded mutation → targeted local proof → push once → CI once → handoff`

Do not use Actions to compensate for a brittle local/runtime boundary.

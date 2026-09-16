# LIFE OS v2 GitHub Actions Conservation Policy

**Status:** LIFE OS v2 development/CI/production-executor operating guardrail.

## Goal

Use free standard GitHub-hosted runners in this public repository as a bounded stateless executor while keeping scheduling singular, runtime lean, and private data out of Git/Actions persistence.

## Rules

1. **Production execution is allowed; production scheduling is not.** Standard GitHub-hosted runners in this public repository may execute bounded approved production code from protected `main`. `LIFE OS Daily Runs` remains the sole recurring scheduler. No workflow may use `schedule`/cron or create/replace a recurring scheduler.
2. **Free standard runners only.** Production/CI must use standard GitHub-hosted runners available free for public repositories. Larger/paid runners are prohibited unless Jim explicitly approves them.
3. **CI stays narrow.** Ordinary pushes/PRs run only the bounded leak/security/unit checks required to protect the public repository.
4. **Production is one bounded job per requested feature/run.** Do not decompose product stages into workflow chains, workflow families, or per-transition jobs.
5. **No Actions state layer.** Artifacts and caches are not canonical state, handoff storage, or required production dependencies. Production payloads/private data are never uploaded to them.
6. **No timestamp-specific recovery workflows.** One-shot recovery workflow YAML does not remain live after bounded use.
7. **Batch safe changes.** Do not spend a full CI/acceptance cycle after every micro-edit.
8. **Two-failure rule.** After two failures using the same bounded approach, stop retrying and reassess the boundary.
9. **Least privilege.** Default workflow permission is `contents: read`; write permissions require an explicit narrow justification.
10. **No workflow cascades.** Generated commits/events must not fan out into unrelated heavyweight jobs.
11. **Historical QA is inert.** Do not recreate v1 QA/recovery workflow families in v2.
12. **Secrets stay secret.** Production credentials are injected only through protected GitHub secrets/environment configuration into approved `main` code. Never print, artifact, cache, or commit secrets/private payloads.
13. **Runtime budget is product architecture.** Normal production feature execution targets <=45 seconds. The application hard-stops at five minutes DEGRADED. Workflow setup/teardown must stay minimal and may not justify slower domain design.
14. **Public-repo protection is mandatory.** Leak guard, synthetic fixture enforcement, commit-metadata privacy, protected `main`, and exact post-write read-back remain required boundaries.

## Development fast path

Canonical development method lives in `docs/life-os-development-policy.md`.

Default:

`inspect once → bounded mutation → targeted local proof → push once → CI once → handoff`

Do not use Actions to compensate for brittle product/runtime boundaries.

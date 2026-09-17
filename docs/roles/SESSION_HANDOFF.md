# LIFE OS Session Handoff — Optional Template

**Activation:** Use only when Jim asks for a handoff, next-chat bootstrap, or continuity summary.

The handoff is a compact orientation artifact. It does **not** override live canonical sources and should not become a second source of truth.

Keep it short. Prefer current facts over history.

## Template

```text
LIFE OS SESSION HANDOFF
Generated: <timestamp CT>

CURRENT MAIN:
<sha>

CURRENT PRODUCT STATE:
- <5–10 bullets maximum>

ACTIVE WORK:
- <agent / branch / exact ownership>

LAST ACCEPTED WORK:
- <only recent accepted work needed for continuity>

OPEN DEFECTS:
- <real unresolved defects only>

CURRENT DECISIONS:
- <recent decisions not yet reflected in canonical docs>

DO NOT REOPEN:
- <recently settled questions>

IMMEDIATE NEXT STEP:
<one action>
```

## Handoff rules

- Do not dump the full conversation.
- Do not copy large historical timelines unless they are directly needed.
- Do not embed mutable policy that already exists in canonical docs.
- Do not treat old SHAs, branches, package states, or ownership as durable truth.
- Make clear what is verified versus reported.
- Include only unresolved defects that can still affect the next session.
- Keep `DO NOT REOPEN` limited to settled issues likely to waste time if rediscovered.
- End with exactly one concrete next step.

## Successor-session startup

When a new chat receives this handoff:

1. Treat it as orientation only.
2. If Tech Lead behavior is requested, load `docs/roles/TECH_LEAD.md`.
3. Fresh-read only the live canonical sources needed for the first task.
4. Do not perform a repository-wide or Notion-wide reorientation pass.
5. Start useful work quickly.

# LIFE OS v2 Public Repository Threat Model

## Scope

This model covers the public GitHub repository, GitHub Actions workflows, public fixtures, test output, logs, caches, and artifacts. It does not claim to make the deployed LIFE OS system PCI DSS compliant by itself.

## Assets to Protect

- API keys, OAuth access/refresh tokens, cookies, session tokens, private keys, and passwords.
- Personal mailbox content, message IDs, recruiter/user email addresses, hiring records, Job Ledger exports, and production checkpoints.
- Private Notion/Jira/Calendar/database identifiers and personalized provider URLs.
- Financial records, payment card PAN, CVV/CVC, PIN/PIN blocks, and magnetic-stripe track data.

## Primary Risks

- A contributor accidentally commits production exports, runtime checkpoints, or real provider fixtures.
- A product test logs private payloads or raw identifiers.
- A workflow exposes production secrets to pull-request code, especially from forks.
- A convenience cache or artifact uploads private runtime output.
- Maintainer commits expose private personal email metadata in public Git history.
- Finance work drifts regulated payment data into public source, tests, logs, or fixtures.

## Controls

- `scripts/security/leak_guard.py` scans tracked/untracked text files for common secrets, private emails, private IDs, card data, personalized tracking URLs, blocked runtime paths, and non-synthetic fixtures.
- Security tests encode expected fail/pass behavior for leaks and log redaction.
- CI runs with read-only default permissions and only on `pull_request`/`push`; it does not use `pull_request_target`.
- CI does not inject production secrets, upload artifacts, or cache runtime data.
- Fixture paths must be synthetic by name/content and must not imply real production exports.
- LIFE OS maintainer commits must use GitHub noreply metadata; CI checks controlled branch pushes for private Gmail-style commit emails.

## Boundaries

Production data belongs in approved private canonical systems and provider runtimes. GitHub is source, docs, synthetic tests, and CI only. Production execution architecture and cutover are owned by Tech Lead 1.

# Security Policy

LIFE OS v2 uses a public-code/private-data model.

## Prohibited repository content

Never commit or intentionally emit into GitHub logs, artifacts, caches, fixtures, PR comments, or test output:

- passwords, API keys, OAuth tokens, refresh tokens, private keys, cookies, session tokens;
- personal production email addresses, message IDs, message bodies, calendar events, recruiter/interview records, financial records, or canonical private URLs/IDs;
- payment card PAN, CVV/CVC, PIN/PIN blocks, track data, or other sensitive authentication data;
- production account numbers, banking credentials, tax identifiers, or identity documents;
- production runtime snapshots/checkpoints containing private payloads.

## Production secret boundary

- Production secrets are injected at runtime from protected GitHub secrets/environments or another explicitly approved secret store.
- Pull requests and fork-originated code must never receive production secrets.
- Do not use `pull_request_target` to execute untrusted checked-out code with secrets.
- Workflow permissions default to read-only and are elevated only for the exact required mutation.
- Production execution runs approved code from the protected production branch/ref only.

## Data handling

- Public CI uses synthetic fixtures only.
- Production data is processed in memory and written directly to the owning private canonical system.
- Production payloads must not be committed back to GitHub.
- Production payloads must not be placed in GitHub Actions artifacts or caches.
- Logs must use redacted identifiers and counts rather than raw private payloads.

## Payment-card / PCI boundary

The repository and CI are designed to remain outside the cardholder-data environment: LIFE OS source code must not store, transmit, or log cardholder data or sensitive authentication data. Financial integrations must rely on approved private providers and tokenized/abstracted records.

Repository design alone does not certify PCI DSS compliance; deployment, provider scope, operational controls, and organizational processes must also satisfy the applicable requirements.

## Leak prevention

CI must fail on detected secret patterns, prohibited production-data fixtures, or disallowed sensitive identifiers before merge. A suspected exposure blocks release until the credential/data is revoked or contained and the public history is verified safe.

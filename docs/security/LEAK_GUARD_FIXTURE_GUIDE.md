# Leak Guard Synthetic Fixture Guide

LIFE OS v2 is a public repository. Production identifiers, private records, credentials, personal email addresses, and live provider payloads must never enter Git history, fixtures, logs, or repository-local scratch files.

## Canonical provider-shaped synthetic emails

When a test needs a real provider fingerprint in the sender domain, keep the provider hostname inside a reserved `.invalid` domain instead of using the live domain directly.

Use:

- `synthetic-alert@linkedin.com.invalid`
- `synthetic-alert@jobright.ai.invalid`
- `synthetic-alert@dice.com.invalid`

This preserves parser/classifier evidence such as `linkedin.com` while remaining synthetically non-routable. The leak guard already permits domains ending in `.invalid`.

Do not use a live-looking address such as `alerts@linkedin[.]com`, even when the address is publicly documented. The guard intentionally cannot prove whether an arbitrary real-domain address is public, personal, or copied from production evidence.

## Other synthetic identifiers

Use explicit synthetic values, for example:

- message IDs: `synthetic-message-id`
- thread IDs: `synthetic-thread-id`
- Notion references: `synthetic-data-source-id`
- ordinary test email domains: `example.com`, `example.org`, or `*.invalid`

Do not add one-off allowlists for real production IDs.

## Live diagnostics

The leak guard scans tracked and untracked repository text files. Keep temporary live-production diagnostics outside the repository checkout. Convert any behavior needed for regression proof into a synthetic fixture before committing it.

## Intentionally blocked fixture patterns

Fixture paths or names implying live data remain prohibited, including names containing `prod`, `production`, `real`, `export`, or `snapshot`. Runtime/checkpoint/private-data directories remain prohibited as well.

These restrictions are safety boundaries, not fixture conventions to bypass.

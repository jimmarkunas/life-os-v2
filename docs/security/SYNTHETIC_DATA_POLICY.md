# Synthetic Data Policy

Public tests may use only synthetic data.

## Allowed

- Example domains: `example.com`, `example.net`, `example.org`.
- Reserved/synthetic domains: `lifeos.test`, `synthetic.lifeos.test`, `test.local`, `localhost`, `invalid`.
- `.invalid` addresses are allowed for synthetic email fixtures.
- Fake names, fake companies, fake jobs, fake mailbox subjects, and generated identifiers that cannot be confused for provider IDs.

## Prohibited

- Real user or recruiter email addresses.
- Real Gmail, Outlook, Notion, Jira, Calendar, or provider IDs/URLs.
- Job Ledger exports, production snapshots, checkpoints, or runtime dumps.
- Real financial records.
- Payment card PAN, CVV/CVC, PIN/PIN blocks, or track data.
- Personalized provider/tracking URLs.

## Fixture Contract

- Fixture files must be explicitly marked as synthetic in content or use reserved example/synthetic domains.
- Fixture paths must not include `prod`, `production`, `real`, `export`, or `snapshot`.
- Runtime output paths such as `runtime`, `runtime-data`, `checkpoints`, `snapshots`, `artifacts`, `private`, and `secrets` are not repository paths.

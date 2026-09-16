# Commit Metadata Privacy

LIFE OS is a public repository. LIFE OS maintainer commits must not expose private personal email addresses in Git author or committer metadata.

## Maintainer Rule

Use the GitHub noreply identity for LIFE OS maintainer commits:

`248513254+jimmarkunas@users.noreply.github.com`

Do not rewrite existing public history without Jim's explicit approval. If old commits contain private metadata, treat that as a separate history/privacy decision.

## Local Setup

Set the repository-local identity before committing LIFE OS maintainer work:

```sh
git config user.email "248513254+jimmarkunas@users.noreply.github.com"
```

CI includes a lightweight guard for LIFE OS-controlled branch pushes so new maintainer commits do not accidentally add private Gmail-style metadata.

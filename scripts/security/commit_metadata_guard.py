#!/usr/bin/env python3
"""Prevent new LIFE OS maintainer commits from exposing private email metadata."""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from typing import Sequence


BLOCKED_DOMAINS = ("@gmail.com", "@googlemail.com")


@dataclass(frozen=True)
class CommitIdentity:
    sha: str
    role: str
    name: str
    email: str

    def blocked_reason(self) -> str | None:
        normalized = self.email.lower()
        if normalized.endswith(BLOCKED_DOMAINS):
            return "private email domain"
        return None


def git_lines(args: Sequence[str]) -> list[str]:
    proc = subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.splitlines()


def identities(revision: str) -> list[CommitIdentity]:
    args = ["log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce"]
    if ".." not in revision and "..." not in revision:
        args.append("--max-count=1")
    args.append(revision)
    rows = git_lines(args)
    parsed: list[CommitIdentity] = []
    for row in rows:
        sha, author_name, author_email, committer_name, committer_email = row.split("\x00")
        parsed.append(CommitIdentity(sha, "author", author_name, author_email))
        parsed.append(CommitIdentity(sha, "committer", committer_name, committer_email))
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check new commit metadata for private maintainer emails.")
    parser.add_argument("revision", nargs="?", default="HEAD", help="Commit or revision range to inspect.")
    args = parser.parse_args(argv)

    failures = [
        identity
        for identity in identities(args.revision)
        if identity.blocked_reason() is not None
    ]
    if failures:
        print("Commit metadata guard failed. LIFE OS maintainer commits must use GitHub noreply email.", file=sys.stderr)
        for failure in failures:
            print(
                f"{failure.sha[:12]} {failure.role} {failure.name} <{failure.email}>: {failure.blocked_reason()}",
                file=sys.stderr,
            )
        return 1

    print("Commit metadata guard passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

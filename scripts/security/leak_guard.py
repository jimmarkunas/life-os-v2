#!/usr/bin/env python3
"""Repository leak guard for LIFE OS v2."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]

ALLOWED_EMAIL_DOMAINS = {
    "example.com",
    "example.net",
    "example.org",
    "invalid",
    "localhost",
    "lifeos.test",
    "synthetic.lifeos.test",
    "test.local",
    "users.noreply.github.com",
}

BLOCKED_PATH_PARTS = {
    "artifacts",
    "checkpoints",
    "exports",
    "private",
    "prod",
    "production",
    "runtime",
    "runtime-data",
    "secrets",
    "snapshots",
}

FIXTURE_PATH_PARTS = {"fixture", "fixtures", "testdata", "test-data"}
FIXTURE_SYNTHETIC_MARKERS = (
    "synthetic",
    "fake",
    "example.com",
    "example.org",
    "example.net",
    "lifeos.test",
)

SKIP_DIRS = {".git", ".pytest_cache", ".venv", "__pycache__", "node_modules", "venv"}
TEXT_EXTENSIONS = {
    "",
    ".cfg",
    ".csv",
    ".env",
    ".ini",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    rule: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.message}"


CONTENT_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |)PRIVATE KEY-----"),
        "private key material is prohibited",
    ),
    (
        "github-token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"),
        "GitHub token-like value is prohibited",
    ),
    (
        "openai-token",
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        "API token-like value is prohibited",
    ),
    (
        "slack-token",
        re.compile(r"\bxox(?:b|p|a|r)-[A-Za-z0-9-]{20,}\b"),
        "Slack token-like value is prohibited",
    ),
    (
        "oauth-token-field",
        re.compile(r"(?i)\b(?:access|refresh|id)_token\b\s*[:=]\s*['\"][A-Za-z0-9._~+/=-]{16,}['\"]"),
        "OAuth token field with a concrete value is prohibited",
    ),
    (
        "password-field",
        re.compile(r"(?i)\bpassword\b\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
        "password field with a concrete value is prohibited",
    ),
    (
        "notion-url",
        re.compile(r"https?://(?:www\.)?notion\.so/[A-Za-z0-9_-]*[a-f0-9]{32}\b", re.I),
        "private Notion page/database URL is prohibited",
    ),
    (
        "notion-id-field",
        re.compile(r"(?i)\b(?:notion_)?(?:page|database)_id\b\s*[:=]\s*['\"]?[a-f0-9-]{32,36}['\"]?"),
        "private Notion page/database identifier is prohibited",
    ),
    (
        "message-id-field",
        re.compile(r"(?i)\b(?:gmail|outlook|email|message)_?id\b\s*[:=]\s*['\"][A-Za-z0-9_-]{8,}['\"]"),
        "real mail/message identifiers are prohibited",
    ),
    (
        "tracking-url",
        re.compile(r"https?://[^\s)>\"]*[?&](?:email|recipient|tracking|token)=", re.I),
        "personalized provider/tracking URL is prohibited",
    ),
    (
        "card-security-code",
        re.compile(r"(?i)['\"]?\b(?:cvv|cvc|cid|pin)\b['\"]?\s*[:=]\s*['\"]?\d{3,6}['\"]?"),
        "card security code or PIN is prohibited",
    ),
    (
        "card-track-data",
        re.compile(r"(?:%B\d{13,19}\^|\;\d{13,19}=)"),
        "magnetic-stripe track data is prohibited",
    ),
)

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,}|localhost|invalid)\b", re.I)
PAN_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")


def repo_files(paths: Sequence[str]) -> list[Path]:
    if paths:
        return [Path(path) for path in paths]

    try:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=REPO_ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return [Path(line) for line in proc.stdout.splitlines() if line.strip()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [
            path.relative_to(REPO_ROOT)
            for path in REPO_ROOT.rglob("*")
            if path.is_file() and not any(part in SKIP_DIRS for part in path.parts)
        ]


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS


def luhn_valid(value: str) -> bool:
    digits = [int(char) for char in re.sub(r"\D", "", value)]
    if len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def allowed_email_domain(domain: str) -> bool:
    normalized = domain.lower()
    return normalized in ALLOWED_EMAIL_DOMAINS or normalized.endswith(".invalid")


def path_findings(path: Path) -> Iterable[Finding]:
    parts = {part.lower() for part in path.parts}
    blocked = parts & BLOCKED_PATH_PARTS
    if blocked:
        part = sorted(blocked)[0]
        yield Finding(path, 0, "blocked-path", f"path component '{part}' is reserved for private/runtime data")

    if parts & FIXTURE_PATH_PARTS:
        name = path.name.lower()
        if any(token in name for token in ("prod", "production", "real", "export", "snapshot")):
            yield Finding(path, 0, "fixture-name", "fixture path must not imply real production data")


def content_findings(path: Path, text: str) -> Iterable[Finding]:
    lower_text = text.lower()
    parts = {part.lower() for part in path.parts}
    if parts & FIXTURE_PATH_PARTS and not any(marker in lower_text for marker in FIXTURE_SYNTHETIC_MARKERS):
        yield Finding(path, 0, "fixture-synthetic-marker", "fixtures must be explicitly marked as synthetic")

    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern, message in CONTENT_RULES:
            if pattern.search(line):
                yield Finding(path, line_number, rule, message)

        for match in EMAIL_RE.finditer(line):
            domain = match.group(1).lower()
            if not allowed_email_domain(domain):
                yield Finding(path, line_number, "private-email", "only synthetic/example email domains are allowed")

        for match in PAN_CANDIDATE_RE.finditer(line):
            if luhn_valid(match.group(0)):
                yield Finding(path, line_number, "payment-card-pan", "payment card PAN is prohibited")


def scan_file(path: Path) -> list[Finding]:
    findings = list(path_findings(path))
    absolute = path if path.is_absolute() else REPO_ROOT / path
    if not absolute.exists() or not absolute.is_file() or not is_text_file(absolute):
        return findings

    try:
        text = absolute.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return findings
    return findings + list(content_findings(path, text))


def scan(paths: Sequence[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in repo_files(paths):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        findings.extend(scan_file(path))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan repository files for public-repo data leaks.")
    parser.add_argument("paths", nargs="*", help="Optional paths to scan. Defaults to tracked and untracked repo files.")
    args = parser.parse_args(argv)

    findings = scan(args.paths)
    if findings:
        print("Leak guard failed. Remove private/production data or use synthetic fixtures only.", file=sys.stderr)
        for finding in findings:
            print(finding.render(), file=sys.stderr)
        return 1

    print("Leak guard passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

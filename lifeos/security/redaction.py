"""Small redaction helper for log-safe output."""

from __future__ import annotations

import re
from typing import Any


REDACTED = "[REDACTED]"

_REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{8,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b[A-Za-z0-9_-]{8,}(?:@|%40)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?i)\b(?:access|refresh|id)_token\b\s*[:=]\s*['\"][^'\"]+['\"]"),
    re.compile(r"(?i)\b(?:gmail|outlook|email|message)_?id\b\s*[:=]\s*['\"]?[^'\"\s,}]+['\"]?"),
    re.compile(r"(?i)['\"]?\b(?:cvv|cvc|cid|pin)\b['\"]?\s*[:=]\s*['\"]?\d{3,6}['\"]?"),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),
)

_TRACKING_QUERY_RE = re.compile(r"(?i)([?&](?:email|recipient|tracking|token|utm_[a-z]+)=)[^&#\s)>\"]+")


def redact(value: Any) -> str:
    """Return a string safe for logs by replacing likely private values."""

    text = str(value)
    for pattern in _REDACTION_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _TRACKING_QUERY_RE.sub(r"\1" + REDACTED, text)

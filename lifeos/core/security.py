"""Generic public-safe redaction for structured runtime logging.

This module deliberately does not configure or wrap a logging framework. Domains
may pass structured fields through :func:`safe_log_fields` before emitting them
with their existing logger. The projection is conservative: known secret/private
field names are removed, common email/URL/auth material is scrubbed from free
text, caller-supplied secret values are replaced, and unknown objects are reduced
to their type name rather than repr()'d.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "<redacted>"

_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "cookie",
    "token",
    "secret",
    "password",
    "apikey",
    "privatekey",
    "email",
    "sender",
    "recipient",
    "messageid",
    "body",
    "payload",
    "content",
    "trackingurl",
    "accountid",
    "notionid",
    "jiraid",
    "calendarid",
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_AUTH_RE = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def _normalized_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).casefold())


def _sensitive_key(key: object) -> bool:
    normalized = _normalized_key(key)
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    text = value
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    text = _AUTH_RE.sub(REDACTED, text)
    text = _EMAIL_RE.sub(REDACTED, text)
    text = _URL_RE.sub(REDACTED, text)
    return text


def redact(value: Any, *, secrets: Iterable[str] = ()) -> Any:
    """Return a JSON-friendly redacted copy of ``value``.

    ``secrets`` should contain runtime values already known to be private, such
    as injected tokens. The input is never mutated.
    """
    secret_values = tuple(str(secret) for secret in secrets if secret)
    return _redact(value, secret_values)


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, bytes):
        return REDACTED
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            result[key_text] = REDACTED if _sensitive_key(key) else _redact(item, secrets)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact(item, secrets) for item in value]
    return type(value).__name__


def safe_log_fields(fields: Mapping[str, Any], *, secrets: Iterable[str] = ()) -> dict[str, Any]:
    """Project structured fields into a public-safe logging dictionary."""
    redacted = redact(fields, secrets=secrets)
    if not isinstance(redacted, dict):  # defensive: Mapping input must stay a mapping
        raise TypeError("safe_log_fields requires a string-keyed mapping")
    return redacted

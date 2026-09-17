from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.security import leak_guard


def _scan_written(relative_path: str, content: str):
    with TemporaryDirectory() as directory:
        path = Path(directory) / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return leak_guard.scan_file(path)


def test_provider_fingerprint_is_allowed_under_invalid_suffix() -> None:
    findings = _scan_written(
        "tests/fixtures/newsletter_synthetic.json",
        '{"synthetic": true, "sender": "synthetic-alert@linkedin.com.invalid"}',
    )

    assert findings == []


def test_live_provider_domain_remains_blocked() -> None:
    live_domain_sender = "synthetic-alert@linkedin" + ".com"
    findings = _scan_written(
        "tests/fixtures/newsletter_synthetic.json",
        f'{{"synthetic": true, "sender": "{live_domain_sender}"}}',
    )

    assert any(finding.rule == "private-email" for finding in findings)

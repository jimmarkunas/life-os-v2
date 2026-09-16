from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.security import leak_guard


class LeakGuardTests(unittest.TestCase):
    def scan_written(self, relative_path: str, content: str):
        with TemporaryDirectory() as directory:
            path = Path(directory) / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return leak_guard.scan_file(path)

    def test_allows_explicit_synthetic_fixture(self) -> None:
        findings = self.scan_written(
            "tests/fixtures/newsletter_synthetic.json",
            '{"synthetic": true, "sender": "recruiter@example.com"}',
        )

        self.assertEqual(findings, [])

    def test_allows_invalid_synthetic_fixture_email(self) -> None:
        findings = self.scan_written(
            "tests/fixtures/newsletter_synthetic.json",
            '{"synthetic": true, "sender": "recruiter@fixture.invalid"}',
        )

        self.assertEqual(findings, [])

    def test_rejects_private_email_in_fixture(self) -> None:
        findings = self.scan_written(
            "tests/fixtures/newsletter_synthetic.json",
            '{"synthetic": true, "sender": "person@' + 'real-company.example"}',
        )

        self.assertTrue(any(finding.rule == "private-email" for finding in findings))

    def test_rejects_production_named_fixture(self) -> None:
        findings = self.scan_written("tests/fixtures/production_export.json", '{"synthetic": true}')

        self.assertTrue(any(finding.rule == "fixture-name" for finding in findings))

    def test_rejects_blocked_runtime_path(self) -> None:
        findings = self.scan_written("checkpoints/newsletter.json", '{"synthetic": true}')

        self.assertTrue(any(finding.rule == "blocked-path" for finding in findings))

    def test_rejects_token_like_values(self) -> None:
        findings = self.scan_written("config.txt", 'refresh_' + 'token = "abcdefghijklmnop' + 'qrstuvwxyz123456"')

        self.assertTrue(any(finding.rule == "oauth-token-field" for finding in findings))

    def test_rejects_payment_card_data(self) -> None:
        findings = self.scan_written(
            "tests/fixtures/card_synthetic.json",
            '{"synthetic": true, "pan": "4111 1111 '
            + '1111 1111", "cv'
            + 'v": "123"}',
        )

        rules = {finding.rule for finding in findings}
        self.assertIn("payment-card-pan", rules)
        self.assertIn("card-security-code", rules)


if __name__ == "__main__":
    unittest.main()

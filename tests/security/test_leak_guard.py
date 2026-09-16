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

    def test_allows_message_id_variable_assignment(self) -> None:
        findings = self.scan_written("router.py", "message_id=message_id")

        self.assertEqual(findings, [])

    def test_rejects_concrete_message_id_field(self) -> None:
        findings = self.scan_written("fixture.txt", "message_" + 'id="abc123456789"')

        self.assertTrue(any(finding.rule == "message-id-field" for finding in findings))

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

    def test_known_good_synthetic_corpus_passes(self) -> None:
        findings = self.scan_written(
            "tests/fixtures/generic_jobs_synthetic.json",
            """
            {
              "synthetic": true,
              "owner": "user@example.com",
              "gmail_fixture": {
                "message_id": "synthetic-message-id",
                "thread_ref": "synthetic-thread-id",
                "subject": "Synthetic role digest"
              },
              "notion_fixture": {
                "data_source_id": "synthetic-data-source-id",
                "page_ref": "synthetic-page-ref"
              },
              "job": {
                "stable_job_key": "synthetic-key",
                "apply_url": "https://synthetic-boards.example/jobs/12345",
                "policy_schema": {"minimum_score": "runtime-configured"}
              },
              "runtime_config_schema": {
                "calendar_id": "synthetic-calendar",
                "notion_token_env": "NOTION_API_TOKEN"
              }
            }
            """,
        )

        self.assertEqual(findings, [])

    def test_rejects_private_workspace_and_provider_identifiers(self) -> None:
        content = "\n".join(
            [
                '{"synthetic": true,',
                '"data_' + 'source_id": "0123456789abcdef0123456789abcdef",',
                '"jira_url": "https://lifeos-private' + '.atlassian.net/browse/LIFE-123",',
                '"calendar_' + 'id": "primary' + '@group.calendar' + '.google.com",',
                '"calendar_url": "https://calendar.google' + '.com/calendar/u/0/r/eventedit/abc123",',
                '"gmail_thread_' + 'id": "18af4c0ffee123",',
                '"tracking_url": "https://provider.example/jobs?recip' + 'ient=person@example.com",',
                '"processed_' + 'message_ids": ["synthetic-message-id"]',
                "}",
            ]
        )

        findings = self.scan_written("tests/fixtures/provider_ids_synthetic.json", content)

        rules = {finding.rule for finding in findings}
        self.assertIn("notion-id-field", rules)
        self.assertIn("jira-url", rules)
        self.assertIn("calendar-id-field", rules)
        self.assertIn("calendar-url", rules)
        self.assertIn("gmail-thread-id-field", rules)
        self.assertIn("tracking-url", rules)
        self.assertIn("runtime-checkpoint-payload", rules)

    def test_failure_output_uses_category_and_path_not_source_line_content(self) -> None:
        path = Path("tests/fixtures/provider_ids_synthetic.json")
        finding = leak_guard.Finding(path, 7, "tracking-url", "personalized provider/tracking URL is prohibited")

        rendered = finding.render()

        self.assertIn("tests/fixtures/provider_ids_synthetic.json:7: tracking-url", rendered)
        self.assertNotIn("provider.example", rendered)
        self.assertNotIn("recipient=", rendered)


if __name__ == "__main__":
    unittest.main()

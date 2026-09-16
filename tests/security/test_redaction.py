from __future__ import annotations

import unittest

from lifeos.security import REDACTED, redact


class RedactionTests(unittest.TestCase):
    def test_redacts_email_token_message_id_and_card_data(self) -> None:
        raw = (
            "user=person@"
            + 'real-company.com refresh_'
            + 'token="abcdefghijklmnopqrstuvwxyz" '
            + "message_"
            + "id=abc123456789 card=4111-1111-"
            + "1111-1111 cv"
            + "v=123"
        )

        redacted = redact(raw)

        self.assertNotIn("person@" + "real-company.com", redacted)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", redacted)
        self.assertNotIn("abc123456789", redacted)
        self.assertNotIn("4111-1111-" + "1111-1111", redacted)
        self.assertNotIn("cv" + "v=123", redacted)
        self.assertIn(REDACTED, redacted)

    def test_redacts_personalized_tracking_query_values(self) -> None:
        raw = "https://provider.example/jobs?utm_source=mail&recip" + "ient=person@" + "example.com&safe=1"

        redacted = redact(raw)

        self.assertNotIn("mail", redacted)
        self.assertNotIn("person@" + "example.com", redacted)
        self.assertIn("safe=1", redacted)


if __name__ == "__main__":
    unittest.main()

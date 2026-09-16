from __future__ import annotations

import unittest

from lifeos.core.security import REDACTED, redact, safe_log_fields


class RedactionTests(unittest.TestCase):
    def test_safe_log_fields_redacts_nested_private_material(self) -> None:
        source = {
            "event": "synthetic-request-failed",
            "count": 3,
            "authorization": "Bearer synthetic-auth-token",
            "sender_email": "person@example.com",
            "message_id": "synthetic-message-123",
            "body_text": "private synthetic body",
            "nested": {
                "access_token": "synthetic-token",
                "detail": "contact person@example.com at https://example.invalid/path?token=abc",
            },
        }

        safe = safe_log_fields(source)

        self.assertEqual(safe["event"], "synthetic-request-failed")
        self.assertEqual(safe["count"], 3)
        self.assertEqual(safe["authorization"], REDACTED)
        self.assertEqual(safe["sender_email"], REDACTED)
        self.assertEqual(safe["message_id"], REDACTED)
        self.assertEqual(safe["body_text"], REDACTED)
        self.assertEqual(safe["nested"]["access_token"], REDACTED)
        serialized = repr(safe)
        self.assertNotIn("person@example.com", serialized)
        self.assertNotIn("example.invalid", serialized)
        self.assertNotIn("synthetic-auth-token", serialized)
        self.assertNotIn("synthetic-message-123", serialized)
        self.assertNotIn("private synthetic body", serialized)

    def test_explicit_runtime_secret_is_removed_from_otherwise_safe_text(self) -> None:
        secret = "synthetic-runtime-secret-value"
        safe = redact({"detail": f"request failed with {secret}"}, secrets=(secret,))
        self.assertEqual(safe["detail"], f"request failed with {REDACTED}")
        self.assertNotIn(secret, repr(safe))

    def test_unknown_objects_never_use_repr(self) -> None:
        class PrivateObject:
            def __repr__(self) -> str:
                return "synthetic-secret-from-repr"

        safe = safe_log_fields({"object": PrivateObject()})
        self.assertEqual(safe["object"], "PrivateObject")
        self.assertNotIn("synthetic-secret-from-repr", repr(safe))

    def test_redaction_does_not_mutate_input(self) -> None:
        source = {"nested": {"token": "synthetic-token"}}
        safe_log_fields(source)
        self.assertEqual(source["nested"]["token"], "synthetic-token")


if __name__ == "__main__":
    unittest.main()

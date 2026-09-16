from __future__ import annotations

import unittest

from scripts.security.commit_metadata_guard import CommitIdentity


class CommitMetadataGuardTests(unittest.TestCase):
    def test_blocks_private_gmail_metadata(self) -> None:
        identity = CommitIdentity("abc123", "author", "Maintainer", "person@" + "gmail.com")

        self.assertEqual(identity.blocked_reason(), "private email domain")

    def test_allows_github_noreply_metadata(self) -> None:
        identity = CommitIdentity(
            "abc123",
            "author",
            "Jim Markunas",
            "248513254+jimmarkunas@" + "users.noreply.github.com",
        )

        self.assertIsNone(identity.blocked_reason())


if __name__ == "__main__":
    unittest.main()

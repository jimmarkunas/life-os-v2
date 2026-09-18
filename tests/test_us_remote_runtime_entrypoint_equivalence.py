"""Focused behavior-equivalence proof for the extracted US Remote runtime.

These tests exercise the real scripts.run_us_remote_production.main() entrypoint
while replacing only external/config boundaries with deterministic synthetic
fakes. They exist specifically to prove two branches relocated by the thin
composition-root refactor: dry-run result/exit semantics and runtime deadline
failure semantics.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lifeos.core.runtime import DeadlineExceeded
from lifeos.jobs import us_remote_runtime as runtime
from scripts import run_us_remote_production as entry


_SYNTHETIC_ENV = {
    "GMAIL_OAUTH_CLIENT_ID": "synthetic-client-id",
    "GMAIL_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
    "GMAIL_OAUTH_REFRESH_TOKEN": "synthetic-refresh-token",
    "NOTION_API_TOKEN": "synthetic-notion-token",
    "NOTION_JOB_LEDGER_DATA_SOURCE_ID": "synthetic-data-source",
}


class _FakeNewsletterProcessor:
    def __init__(self, *, boundary_name: str) -> None:
        self.boundary_name = boundary_name

    def process_window(self, _mailboxes, _start, _end):
        return SimpleNamespace(
            state=runtime.NewsletterExecutionState.PASS,
            observations=(),
            messages=(),
            errors=(),
        )


class _EmptyGmailBacklog:
    mailbox = "gmail"

    def enumerate_unprocessed_ids(self, _boundary_name):
        return ()


class _FakeWebAcquirer:
    def __init__(self, **_kwargs) -> None:
        pass

    def acquire(self, _registry, **_kwargs):
        return SimpleNamespace(complete=True, observations=(), sources=())


class _DeadlineMailRouter:
    def __init__(self, **_kwargs) -> None:
        pass

    def route_window(self, _mailboxes, _start, _end):
        raise DeadlineExceeded("synthetic deadline")


class UsRemoteEntrypointEquivalenceTests(unittest.TestCase):
    def _base_patches(self):
        return (
            patch.dict(os.environ, {"NEWSLETTER_PRIVATE_POLICY_PATH": "synthetic-policy"}, clear=True),
            patch.object(entry, "_require_env", return_value=dict(_SYNTHETIC_ENV)),
            patch.object(
                entry,
                "_load_private_policy",
                return_value=(object(), {}, object(), "Synthetic-US", "Newsletter"),
            ),
            patch.object(entry, "_exchange_gmail_access_token", return_value="synthetic-access-token"),
            patch.object(entry, "load_registry", return_value={}),
            patch.object(entry, "browser_evidence", return_value=None),
            patch.object(entry, "HttpClient", return_value=object()),
            patch.object(entry, "NotionTransport", return_value=object()),
            patch.object(entry, "GmailMailboxTransport", return_value=_EmptyGmailBacklog()),
            patch.object(runtime, "fallback_fetcher", return_value=None),
        )

    def _run(self, argv: list[str], *extra_patches):
        stdout = io.StringIO()
        with contextlib.ExitStack() as stack:
            for patcher in self._base_patches() + extra_patches:
                stack.enter_context(patcher)
            with contextlib.redirect_stdout(stdout):
                exit_code = entry.main(argv)
        return exit_code, json.loads(stdout.getvalue())

    def test_dry_run_preserves_result_shape_and_zero_exit(self) -> None:
        exit_code, body = self._run(
            ["--dry-run", "--timeout-seconds", "60"],
            patch.object(runtime, "NewsletterProcessor", _FakeNewsletterProcessor),
            patch.object(runtime, "USRemoteAcquirer", _FakeWebAcquirer),
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(body["status"], "PASS")
        self.assertIs(body["dry_run"], True)
        self.assertEqual(body["newsletter_observations"], 0)
        self.assertEqual(body["web_observations"], 0)
        self.assertEqual(body["web_sources_complete"], 0)
        self.assertEqual(body["web_sources_not_due"], 0)
        self.assertEqual(body["web_sources_total"], 0)
        self.assertIs(body["browser_fallback_available"], False)
        self.assertIn("newsletter_fetch_parse", body["timings"])
        self.assertIn("web_acquire", body["timings"])

    def test_deadline_exhaustion_preserves_degraded_exit_contract(self) -> None:
        exit_code, body = self._run(
            ["--timeout-seconds", "60"],
            patch.object(runtime, "MailRouter", _DeadlineMailRouter),
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(body["status"], "DEGRADED")
        self.assertEqual(body["reason"], "execution-deadline-exhausted")
        self.assertIn("elapsed_seconds", body)
        self.assertEqual(body["timings"], {})
        self.assertNotIn("synthetic deadline", json.dumps(body))


if __name__ == "__main__":
    unittest.main()

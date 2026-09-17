from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

from lifeos.core.http import HttpClient
from scripts import run_newsletter_production as entry
from scripts.test_run_newsletter_production import PRIVATE_POLICY, REQUIRED_ENV, SyntheticProductionBackend


def test_manual_uat_skips_mail_router_but_processes_staged_backlog() -> None:
    backend = SyntheticProductionBackend()
    fake_client = HttpClient(backend=backend)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(PRIVATE_POLICY, handle)
        policy_path = handle.name

    env = dict(REQUIRED_ENV)
    env["NEWSLETTER_PRIVATE_POLICY_PATH"] = policy_path
    env["GITHUB_WORKFLOW"] = entry.UAT_WORKFLOW_NAME

    try:
        with patch.dict(os.environ, env, clear=True), patch.object(
            entry, "HttpClient", return_value=fake_client
        ), patch.object(
            entry.MailRouter,
            "route_window",
            side_effect=AssertionError("manual UAT must not invoke Mail Router"),
        ):
            exit_code = entry.main(["--timeout-seconds", "30"])
    finally:
        os.unlink(policy_path)

    assert exit_code == 0
    assert backend.routed_message_ids == []
    assert backend.processed_message_ids == ["msg-1"]
    assert len(backend.pages) == 1

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def workflow_text() -> dict[Path, str]:
    return {
        path.relative_to(REPO_ROOT): path.read_text(encoding="utf-8")
        for path in WORKFLOW_DIR.glob("*.yml")
    }


def has_protected_main_checkout(text: str) -> bool:
    return bool(re.search(r"(?m)^\s+ref:\s+main\s*$", text))


def has_main_job_guard(text: str) -> bool:
    return "github.ref == 'refs/heads/main'" in text


def is_secret_workflow_protected_main_only(text: str) -> bool:
    if "pull_request:" in text or "pull_request_target" in text:
        return False
    if not has_protected_main_checkout(text):
        return False
    if "workflow_dispatch:" in text and not has_main_job_guard(text):
        return False
    return True


class WorkflowGuardTests(unittest.TestCase):
    def test_workflows_do_not_define_recurring_schedules(self) -> None:
        offenders = [str(path) for path, text in workflow_text().items() if "schedule:" in text]

        self.assertEqual(offenders, [])

    def test_workflows_do_not_use_pull_request_target(self) -> None:
        offenders = [str(path) for path, text in workflow_text().items() if "pull_request_target" in text]

        self.assertEqual(offenders, [])

    def test_pull_request_workflows_do_not_reference_protected_secrets(self) -> None:
        offenders = [
            str(path)
            for path, text in workflow_text().items()
            if "pull_request:" in text and "secrets." in text
        ]

        self.assertEqual(offenders, [])

    def test_secret_using_workflows_are_protected_main_only(self) -> None:
        offenders = [
            str(path)
            for path, text in workflow_text().items()
            if "secrets." in text
            and not is_secret_workflow_protected_main_only(text)
        ]

        self.assertEqual(offenders, [])

    def test_workflows_do_not_persist_artifacts_or_caches(self) -> None:
        prohibited = ("actions/upload-artifact", "actions/cache")
        offenders = [
            str(path)
            for path, text in workflow_text().items()
            if any(action in text for action in prohibited)
        ]

        self.assertEqual(offenders, [])

    def test_pr_safe_ci_checkout_uses_no_persisted_credentials(self) -> None:
        ci = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")

        self.assertIn("pull_request:", ci)
        self.assertIn("persist-credentials: false", ci)
        self.assertNotIn("secrets.", ci)


if __name__ == "__main__":
    unittest.main()

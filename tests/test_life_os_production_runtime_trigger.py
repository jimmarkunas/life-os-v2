"""Executes the actual "Validate trigger" bash step extracted from
.github/workflows/life-os-production-runtime.yml against synthetic trigger
bodies, proving the real validation logic behaves correctly -- not just
that expected substrings exist in the YAML text.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "life-os-production-runtime.yml"


def _extract_trigger_script() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    after_name = text.split("name: Validate trigger", 1)[1]
    block = after_name.split("run: |", 1)[1]
    # The step's shell block is indented; stop at the next top-level step
    # ("      - name:") which is not part of this run block.
    lines = block.splitlines()
    script_lines: list[str] = []
    for line in lines[1:]:
        if re.match(r"^      - name:", line):
            break
        script_lines.append(line)
    script = "\n".join(script_lines)
    # De-indent: the step body is indented 10 spaces under "run: |".
    return "\n".join(line[10:] if line.startswith(" " * 10) else line for line in script.splitlines())


TRIGGER_SCRIPT = _extract_trigger_script()


def _run_trigger(trigger_body: str) -> tuple[int, dict[str, str]]:
    with tempfile.TemporaryDirectory() as tmp:
        output_path = Path(tmp) / "github_output"
        output_path.write_text("", encoding="utf-8")
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        (bin_dir / "python").symlink_to(sys.executable)
        env = dict(os.environ)
        env["TRIGGER_BODY"] = trigger_body
        env["GITHUB_OUTPUT"] = str(output_path)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        completed = subprocess.run(
            ["bash", "-c", TRIGGER_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        outputs: dict[str, str] = {}
        for line in output_path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                outputs[key] = value
        return completed.returncode, outputs


def _trigger(module: str, *, historical_hours: str | None = None, window_hours: str | None = None) -> str:
    lines = ["slot=2026-09-18T00:00:00Z", f"module={module}"]
    if window_hours is not None:
        lines.append(f"window_hours={window_hours}")
    if historical_hours is not None:
        lines.append(f"historical_inbox_recovery_hours={historical_hours}")
    lines.append("nonce=synthetic-nonce-1")
    return "\n".join(lines)


def test_case_a_normal_us_remote_has_no_historical_flag() -> None:
    code, outputs = _run_trigger(_trigger("us-remote"))
    assert code == 0
    assert outputs["module"] == "us-remote"
    assert outputs["historical_inbox_recovery_hours"] == ""
    assert outputs["full_web_sweep"] == "false"
    assert outputs["window_hours"] == "24"


def test_case_b_historical_mail_recovery_trigger_is_valid_and_bounded() -> None:
    code, outputs = _run_trigger(_trigger("us-remote", historical_hours="168"))
    assert code == 0
    assert outputs["module"] == "us-remote"
    assert outputs["historical_inbox_recovery_hours"] == "168"
    # Web semantics are completely unaffected by historical Mail recovery.
    assert outputs["full_web_sweep"] == "false"
    assert outputs["web_lookback_hours"] == "24"
    assert outputs["window_hours"] == "24"


def test_case_c_value_above_hard_cap_fails_closed() -> None:
    code, _outputs = _run_trigger(_trigger("us-remote", historical_hours="2161"))
    assert code != 0


def test_case_d_zero_negative_and_non_numeric_fail_closed() -> None:
    for bad_value in ("0", "-5", "not-a-number"):
        code, _outputs = _run_trigger(_trigger("us-remote", historical_hours=bad_value))
        assert code != 0, f"expected failure for historical_inbox_recovery_hours={bad_value!r}"


def test_case_e_wrong_module_with_historical_hours_fails_closed() -> None:
    code, _outputs = _run_trigger(_trigger("us-remote-recovery", historical_hours="168"))
    assert code != 0


def test_existing_modules_unaffected_when_field_absent() -> None:
    code, outputs = _run_trigger(_trigger("us-remote-recovery"))
    assert code == 0
    assert outputs["window_hours"] == "1440"
    assert outputs["full_web_sweep"] == "true"
    assert outputs["historical_inbox_recovery_hours"] == ""


def test_execute_step_only_appends_historical_flag_when_present() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    execute = text.split("name: Execute US Remote runtime", 1)[1]
    assert "--historical-inbox-recovery-hours" in execute
    assert "steps.trigger.outputs.historical_inbox_recovery_hours" in execute
    assert 'if [ -n "${{ steps.trigger.outputs.historical_inbox_recovery_hours }}" ]; then' in execute

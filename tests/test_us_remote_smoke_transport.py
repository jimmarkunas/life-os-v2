from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "life-os-production-runtime.yml"


def test_us_remote_smoke_is_small_and_read_only() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    smoke = text.split("us-remote-smoke)", 1)[1].split(";;", 1)[0]

    assert 'default_window="1"' in smoke
    assert 'web_lookback_hours="1"' in smoke
    assert 'full_web_sweep="false"' in smoke
    assert 'dry_run="true"' in smoke
    assert "extra+=(--dry-run)" in text

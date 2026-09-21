from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pytest
from lifeos.core.scheduling import resolve_slot, require_lanes

CT = ZoneInfo("America/Chicago")

def test_complete_hourly_matrix_and_branch_lanes():
    full = ("mail_census", "newsletter", "us_web", "hiring_mail", "scale_up",
            "drive_index", "jira_snapshot", "job_snapshot", "bills", "calendar",
            "hiring_projections", "daily_report")
    mail = ("mail_census", "newsletter", "hiring_mail", "daily_report")
    evening = full + ("scheduled_boundaries", "accountability")
    for hour in range(24):
        result = resolve_slot(datetime(2026, 9, 15, hour, 37, tzinfo=CT))
        assert result.slot.strftime("%H:00") == f"{hour:02}:00"
        expected = mail if hour in (0, 1, 2, 3, 4, 5, 7, 8, 10, 11, 13, 14, 15, 16, 17, 19, 20, 21, 22, 23) else full
        if hour == 18: expected = evening
        assert result.required_lanes == expected
    assert resolve_slot(datetime(2026, 9, 15, 0, tzinfo=CT)).branch == "midnight"
    assert resolve_slot(datetime(2026, 9, 15, 0, tzinfo=CT)).required_lanes == mail
    assert resolve_slot(datetime(2026, 9, 15, 6, tzinfo=CT)).required_lanes == full
    assert resolve_slot(datetime(2026, 9, 15, 9, tzinfo=CT)).required_lanes == full
    assert resolve_slot(datetime(2026, 9, 15, 12, tzinfo=CT)).required_lanes == full
    assert resolve_slot(datetime(2026, 9, 15, 18, tzinfo=CT)).required_lanes == evening
    assert resolve_slot(datetime(2026, 9, 14, 6, tzinfo=CT)).branch == "monday_boundary"
    assert resolve_slot(datetime(2026, 9, 14, 6, tzinfo=CT)).required_lanes == full + ("scheduled_boundaries",)

def test_sunday_evening_and_dst_folds_are_local_slots():
    sunday = resolve_slot(datetime(2026, 9, 13, 18, tzinfo=CT))
    weekday = resolve_slot(datetime(2026, 9, 15, 18, tzinfo=CT))
    assert sunday.branch == "sunday_boundary"
    assert sunday.required_lanes == weekday.required_lanes
    spring = resolve_slot(datetime(2026, 3, 8, 8, tzinfo=timezone.utc))
    fall = resolve_slot(datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc))
    assert spring.slot.hour == 3 and fall.slot.hour == 1
    assert spring.slot.fold == 0 and fall.slot.fold == 1

def test_delayed_execution_uses_later_slot_and_missing_lanes_fail_closed():
    delayed = resolve_slot(datetime(2026, 9, 14, 10, 5, tzinfo=timezone.utc))
    assert delayed.slot.hour == 5
    with pytest.raises(ValueError, match="missing required"):
        require_lanes(delayed, {"mail_census": "runtime"})

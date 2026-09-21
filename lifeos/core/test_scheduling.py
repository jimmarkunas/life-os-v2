from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pytest
from lifeos.core.scheduling import resolve_slot, require_lanes

CT = ZoneInfo("America/Chicago")

def test_complete_hourly_matrix_and_branch_lanes():
    for hour in range(24):
        result = resolve_slot(datetime(2026, 9, 14, hour, 37, tzinfo=CT))
        assert result.slot.strftime("%H:00") == f"{hour:02}:00"
        assert "daily_report" in result.required_lanes
    assert resolve_slot(datetime(2026, 9, 14, 0, tzinfo=CT)).branch == "midnight"
    assert resolve_slot(datetime(2026, 9, 14, 6, tzinfo=CT)).branch == "monday_boundary"

def test_sunday_evening_and_dst_folds_are_local_slots():
    assert resolve_slot(datetime(2026, 9, 13, 18, tzinfo=CT)).branch == "sunday_boundary"
    spring = resolve_slot(datetime(2026, 3, 8, 8, tzinfo=timezone.utc))
    fall = resolve_slot(datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc))
    assert spring.slot.hour == 3 and fall.slot.hour == 1
    assert spring.slot.fold == 0 and fall.slot.fold == 1

def test_delayed_execution_uses_later_slot_and_missing_lanes_fail_closed():
    delayed = resolve_slot(datetime(2026, 9, 14, 10, 5, tzinfo=timezone.utc))
    assert delayed.slot.hour == 5
    with pytest.raises(ValueError, match="missing required"):
        require_lanes(delayed, {"mail_census": "runtime"})

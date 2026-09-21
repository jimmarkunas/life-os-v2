"""Deterministic LIFE OS scheduled-slot contract."""
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
FULL = ("mail_census", "newsletter", "us_web", "hiring_mail", "scale_up",
        "drive_index", "jira_snapshot", "job_snapshot", "bills", "calendar",
        "hiring_projections", "daily_report")
MAIL = ("mail_census", "newsletter", "hiring_mail", "daily_report")

@dataclass(frozen=True, slots=True)
class SlotContract:
    slot: datetime
    branch: str
    required_lanes: tuple[str, ...]

def resolve_slot(execution_at: datetime) -> SlotContract:
    if execution_at.tzinfo is None or execution_at.utcoffset() is None:
        raise ValueError("execution timestamp must be offset-aware")
    local = execution_at.astimezone(CT).replace(minute=0, second=0, microsecond=0)
    hour = local.hour
    if hour in (6, 9, 12, 18):
        lanes = FULL if hour != 18 else FULL + ("scheduled_boundaries", "accountability")
        branch = "full_source"
        if hour == 18: branch = "evening_boundary"
    else:
        lanes, branch = MAIL, "ordinary_hourly"
    if hour == 0:
        branch = "midnight"
    if hour == 6 and local.weekday() == 0:
        lanes, branch = FULL + ("scheduled_boundaries",), "monday_boundary"
    if hour == 18 and local.weekday() == 6:
        branch = "sunday_boundary"
    return SlotContract(local, branch, lanes)

def require_lanes(contract: SlotContract, owners: dict[str, str]) -> dict[str, str]:
    missing = [lane for lane in contract.required_lanes if not owners.get(lane)]
    if missing:
        raise ValueError("missing required production lane: " + ", ".join(missing))
    return {lane: owners[lane] for lane in contract.required_lanes}

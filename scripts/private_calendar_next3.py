#!/usr/bin/env python3
from __future__ import annotations

import json, sys
from datetime import datetime, timedelta, timezone

from lifeos.core.config import ConfigField, RuntimeConfig
from lifeos.core.http import HttpClient, HttpError
from lifeos.core.runtime import DeadlineExceeded, RunContext
from lifeos.integrations.calendar import GoogleCalendarTransport, GoogleCalendarTransportError


def _event_payload(event: dict) -> dict:
    start = event.get("start") if isinstance(event.get("start"), dict) else {}
    end = event.get("end") if isinstance(event.get("end"), dict) else {}
    start_value, end_value = start.get("dateTime") or start.get("date"), end.get("dateTime") or end.get("date")
    event_id, title = event.get("id") or event.get("iCalUID"), event.get("summary")
    if not event_id or not title or not start_value or not end_value:
        raise ValueError("calendar event evidence incomplete")
    all_day = "date" in start and "dateTime" not in start
    timezone_value = start.get("timeZone") or end.get("timeZone") or ("all-day" if all_day else "UTC")
    return {"event_ref": str(event_id), "title": str(title), "start": str(start_value),
            "end": str(end_value), "timezone": str(timezone_value), "all_day": all_day}


def main() -> int:
    try:
        fields = (ConfigField("GOOGLE_CALENDAR_API_TOKEN"), ConfigField("GOOGLE_CALENDAR_ID", required=False))
        config, context = RuntimeConfig.load(fields), RunContext.start(timeout_seconds=45)
        now = datetime.now(timezone.utc)
        events = GoogleCalendarTransport.from_config(context=context, http=HttpClient(), config=config).list_events(
            now, now + timedelta(days=14), page_size=3
        )
        payload = {"status": "PASS", "surface": "private_next_3_meetings",
                   "generated_at": now.isoformat(), "events": [_event_payload(event) for event in events[:3]]}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (Exception,) as exc:
        safe = (DeadlineExceeded, GoogleCalendarTransportError, HttpError, ValueError)
        reason = type(exc).__name__ if isinstance(exc, safe) or exc.__class__.__name__ == "ConfigurationError" else "RuntimeError"
        print(json.dumps({"status": "DEGRADED", "surface": "private_next_3_meetings", "reason": reason}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""One-off live proof that GitHub Actions can fetch a real Greenhouse full JD.

Non-production diagnostic only. No LIFE OS runtime imports, persistence, browser,
Notion, scheduler, or Job Ledger. Emits bounded metadata only.
"""
from __future__ import annotations

import json
import sys
from html import unescape
from urllib.request import Request, urlopen

BOARD = "figma"
JOB_ID = "6020719004"
URL = f"https://boards-api.greenhouse.io/v1/boards/{BOARD}/jobs?content=true"


def main() -> int:
    request = Request(
        URL,
        headers={"Accept": "application/json", "User-Agent": "LIFE-OS-Greenhouse-POC/1.0"},
    )
    with urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError(f"greenhouse_http_status={response.status}")
        payload = json.load(response)

    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    job = next((item for item in jobs if str(item.get("id")) == JOB_ID), None)
    if not isinstance(job, dict):
        raise RuntimeError(f"greenhouse_job_missing={JOB_ID}")

    title = str(job.get("title") or "").strip()
    apply_url = str(job.get("absolute_url") or "").strip()
    content = unescape(unescape(str(job.get("content") or ""))).strip()

    if not title:
        raise RuntimeError("greenhouse_title_missing")
    if not apply_url.startswith(("https://", "http://")):
        raise RuntimeError("greenhouse_apply_url_missing")
    if len(content) < 500:
        raise RuntimeError(f"greenhouse_jd_too_short={len(content)}")

    print("provider=greenhouse")
    print(f"job_id={JOB_ID}")
    print(f"title={title}")
    print(f"apply_url={apply_url}")
    print(f"jd_chars={len(content)}")
    print("PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL:{type(exc).__name__}:{exc}", file=sys.stderr)
        raise

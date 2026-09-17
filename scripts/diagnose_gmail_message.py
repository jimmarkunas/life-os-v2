#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}?format=full"


def _read_json_error(exc: HTTPError) -> dict:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw else {}
    except Exception:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) else {}
    details = []
    for item in error.get("errors") or []:
        if isinstance(item, dict):
            details.append(
                {
                    "domain": str(item.get("domain") or ""),
                    "reason": str(item.get("reason") or ""),
                    "message": str(item.get("message") or "")[:240],
                }
            )
    return {
        "status": int(exc.code),
        "message": str(error.get("message") or "")[:240],
        "status_text": str(error.get("status") or "")[:120],
        "errors": details,
    }


def _request_json(request: Request) -> dict:
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv: list[str]) -> int:
    message_ids = argv[1:] or ["1a07f4a63a8a9b48"]
    required = ("GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET", "GMAIL_OAUTH_REFRESH_TOKEN")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        print(json.dumps({"blocked": "missing-env", "missing": missing}, sort_keys=True))
        return 2

    token_body = urlencode(
        {
            "client_id": os.environ["GMAIL_OAUTH_CLIENT_ID"],
            "client_secret": os.environ["GMAIL_OAUTH_CLIENT_SECRET"],
            "refresh_token": os.environ["GMAIL_OAUTH_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    token_payload = _request_json(
        Request(TOKEN_URL, data=token_body, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    )
    access_token = str(token_payload["access_token"])

    def fetch(message_id: str) -> dict:
        request = Request(
            GMAIL_URL.format(message_id=message_id),
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            payload = _request_json(request)
        except HTTPError as exc:
            return {
                "message_id": message_id,
                "operation": "gmail.messages.get",
                "result": "http-error",
                "error": _read_json_error(exc),
            }
        return {
            "message_id": message_id,
            "operation": "gmail.messages.get",
            "result": "ok",
            "payload_id_matches": payload.get("id") == message_id,
            "has_payload": isinstance(payload.get("payload"), dict),
        }

    results = []
    with ThreadPoolExecutor(max_workers=min(8, len(message_ids))) as pool:
        futures = [pool.submit(fetch, message_id) for message_id in message_ids]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["message_id"])
    print(json.dumps({"results": results}, sort_keys=True))
    return 1 if any(item["result"] != "ok" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

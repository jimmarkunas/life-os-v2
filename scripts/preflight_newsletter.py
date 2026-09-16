#!/usr/bin/env python3
"""Read-only Newsletter UAT preflight.

Validates required private configuration and live canonical Job Ledger
schema/option compatibility before any production Newsletter mutation.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

from lifeos.core.http import HttpClient, RetryPolicy
from lifeos.core.runtime import RunContext

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "contracts" / "job_ledger_schema.json"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
REQUIRED_ENV = (
    "NOTION_API_TOKEN",
    "NOTION_JOB_LEDGER_DATA_SOURCE_ID",
    "GMAIL_OAUTH_CLIENT_ID",
    "GMAIL_OAUTH_CLIENT_SECRET",
    "GMAIL_OAUTH_REFRESH_TOKEN",
)


def _load_contract() -> dict[str, dict[str, object]]:
    raw = json.loads(CONTRACT_PATH.read_text())
    if not isinstance(raw, dict) or not raw:
        raise RuntimeError("Job Ledger schema contract is empty or invalid")
    contract: dict[str, dict[str, object]] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("type"), str):
            raise RuntimeError(f"invalid contract entry for {name!r}")
        allowed_values = spec.get("allowed_values")
        if allowed_values is not None and not (
            isinstance(allowed_values, list) and all(isinstance(value, str) for value in allowed_values)
        ):
            raise RuntimeError(f"invalid allowed_values for {name!r}")
        contract[str(name)] = dict(spec)
    return contract


def _require_env() -> dict[str, str]:
    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        raise RuntimeError("missing required runtime configuration: " + ", ".join(missing))
    return {name: os.environ[name] for name in REQUIRED_ENV}


def _live_property_contract(payload: dict) -> dict[str, dict[str, object]]:
    properties = payload.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError("Notion data-source response did not include properties")

    result: dict[str, dict[str, object]] = {}
    for name, prop in properties.items():
        if not isinstance(prop, dict) or not prop.get("type"):
            continue
        property_type = str(prop["type"])
        spec: dict[str, object] = {"type": property_type}
        type_config = prop.get(property_type)
        if isinstance(type_config, dict) and isinstance(type_config.get("options"), list):
            spec["allowed_values"] = sorted(
                str(option.get("name"))
                for option in type_config["options"]
                if isinstance(option, dict) and option.get("name")
            )
        result[str(name)] = spec
    return result


def main() -> int:
    contract = _load_contract()
    env = _require_env()
    context = RunContext.start(timeout_seconds=30.0)
    http = HttpClient()
    data_source_id = env["NOTION_JOB_LEDGER_DATA_SOURCE_ID"]
    payload = http.request_json(
        context,
        "GET",
        f"{NOTION_API}/data_sources/{quote(data_source_id, safe='')}",
        headers={
            "Authorization": f"Bearer {env['NOTION_API_TOKEN']}",
            "Notion-Version": NOTION_VERSION,
            "Accept": "application/json",
        },
        timeout_seconds=10.0,
        retry=RetryPolicy(max_attempts=2, backoff_seconds=0.1, max_backoff_seconds=1.0),
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Notion data-source response was not an object")

    live = _live_property_contract(payload)
    missing = sorted(name for name in contract if name not in live)
    type_mismatches = sorted(
        f"{name}: expected {spec['type']}, got {live[name]['type']}"
        for name, spec in contract.items()
        if name in live and live[name].get("type") != spec.get("type")
    )
    option_mismatches: list[str] = []
    for name, spec in contract.items():
        expected_values = set(spec.get("allowed_values") or [])
        if not expected_values or name not in live:
            continue
        live_values = set(live[name].get("allowed_values") or [])
        missing_values = sorted(expected_values - live_values)
        if missing_values:
            option_mismatches.append(f"{name}: missing canonical options {missing_values}")

    if missing or type_mismatches or option_mismatches:
        if missing:
            print("MISSING PROPERTIES:")
            for name in missing:
                print(f"- {name}")
        if type_mismatches:
            print("TYPE MISMATCHES:")
            for item in type_mismatches:
                print(f"- {item}")
        if option_mismatches:
            print("OPTION MISMATCHES:")
            for item in option_mismatches:
                print(f"- {item}")
        return 1

    print("NEWSLETTER PREFLIGHT PASS")
    print(f"required_runtime_values={len(REQUIRED_ENV)}")
    print(f"canonical_job_ledger_properties={len(contract)}")
    print("gmail_check=configuration-present")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # fail closed without printing secret values
        print(f"NEWSLETTER PREFLIGHT FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)

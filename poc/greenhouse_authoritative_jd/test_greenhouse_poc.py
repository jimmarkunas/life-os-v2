import json
from pathlib import Path
from unittest.mock import patch

import pytest

from greenhouse_poc import evaluate_greenhouse_job


HERE = Path(__file__).resolve().parent

PROFILE = {
    "title_patterns": {
        "DIRECT": [r"\btechnical program manager\b", r"\btpm\b"],
        "ADJACENT": [r"\bprogram manager\b", r"\btechnical project manager\b"],
        "METHOD_EQUIVALENT": [r"\bdelivery\b", r"\btransformation\b"],
        "UNSUPPORTED": [r"\baccount executive\b", r"\brecruiter\b"]
    },
    "direct_specialization_patterns": [r"\binfrastructure\b"],
    "dimension_patterns": {
        "role_seniority": [r"\b8\+ years\b", r"\bexecutive leadership\b"],
        "functional": [r"\bprogram management\b", r"\bstakeholder management\b", r"\bexecution\b", r"\bcoordination\b", r"\brisk management\b"],
        "technical_platform": [r"\binfrastructure\b", r"\bcloud-native\b", r"\bdistributed systems\b", r"\bAWS\b", r"\bGCP\b", r"\bKafka\b", r"\bstorage\b", r"\bcaching\b"],
        "delivery_complexity": [r"\blarge-scale\b", r"\bmigrations\b", r"\bmulti-team\b", r"\breliability\b", r"\bhigh-availability\b", r"\bdependencies\b"],
        "competitive_advantage": [r"\bsecurity\b", r"\bcompliance\b", r"\bFedRAMP\b", r"\bSOC 2\b", r"\bexecutive-level\b"]
    },
    "evidence_patterns": {
        "DIRECT": [r"\bprogram management\b", r"\binfrastructure\b", r"\bcloud-native\b", r"\bdistributed systems\b", r"\bmigrations\b", r"\bstakeholder\b", r"\bcross-functional\b", r"\bsecurity\b", r"\bcompliance\b"],
        "ADJACENT": [r"\bdelivery\b", r"\bexecution\b", r"\breliability\b", r"\boperations\b"],
        "METHOD_EQUIVALENT": [r"\bcoordination\b", r"\bdependencies\b", r"\bcommunications?\b"],
        "UNSUPPORTED": [r"\bsite reliability engineering\b"]
    },
    "material_patterns": [r"\byears\b", r"\bexperience\b", r"\btrack record\b", r"\bskills\b", r"\bexpertise\b"],
    "ignore_patterns": [r"\bequal opportunity\b", r"\bbenefits\b", r"\bprivacy\b"],
    "hard_family_patterns": [r"\baccount executive\b", r"\brecruiter\b"]
}


def _fixture():
    return json.loads((HERE / "greenhouse_real_job.json").read_text(encoding="utf-8"))


def test_real_greenhouse_jd_scores_without_network_or_browser():
    payload = _fixture()
    with patch("socket.socket", side_effect=AssertionError("network forbidden in POC")):
        result = evaluate_greenhouse_job(payload, PROFILE, job_id=6020719004)

    job = result["job"]
    assert job["provider"] == "greenhouse"
    assert job["company"] == "Figma"
    assert job["title"] == "Technical Program Manager - Infrastructure"
    assert job["apply_url"].endswith("gh_jid=6020719004")
    assert job["evidence_kind"] == "employer_ats_jd"
    assert job["evidence_authority"] == "authoritative_provider_api"
    assert "distributed systems" in job["jd_text"]
    assert "&lt;" not in job["jd_text"]

    assert result["network_fetches"] == 0
    assert result["browser_fetches"] == 0
    assert result["requirements"]
    assert result["fit"]["authority"] == "AUTHORITATIVE"
    assert result["fit"]["evidence_kind"] == "employer_ats_jd"
    assert isinstance(result["fit"]["score"], int)
    assert result["fit"]["score"] >= 72


def test_missing_provider_jd_fails_closed_even_with_generic_source_text():
    payload = _fixture()
    payload["jobs"][0]["content"] = ""
    payload["jobs"][0]["source_description_text"] = "Program manager with infrastructure experience."

    with pytest.raises(ValueError, match="missing authoritative full JD"):
        evaluate_greenhouse_job(payload, PROFILE, job_id=6020719004)

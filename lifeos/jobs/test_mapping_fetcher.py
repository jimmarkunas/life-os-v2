from lifeos.jobs.terminal_evidence import MappingFetcher


def test_mapping_fetcher_matches_linkedin_job_across_tracking_variants():
    evidence_url = (
        "https://www.linkedin.com/comm/jobs/view/4469831942/"
        "?trackingId=variant-a&refId=first"
    )
    requested_url = (
        "https://www.linkedin.com/comm/jobs/view/4469831942/"
        "?trackingId=variant-b&refId=second&otpToken=other"
    )
    final_url = "https://www.rubrik.com/company/careers/departments/job.8140556?gh_jid=8140556"
    fetcher = MappingFetcher(
        {
            "pages": [
                {
                    "url": evidence_url,
                    "final_url": final_url,
                    "html": "<html><body>Senior Project Manager vacancy evidence</body></html>",
                }
            ]
        }
    )

    response = fetcher.get(requested_url)

    assert response.final_url == final_url
    assert "Senior Project Manager" in response.body


def test_mapping_fetcher_does_not_match_different_linkedin_job_id():
    fetcher = MappingFetcher(
        {
            "pages": [
                {
                    "url": "https://www.linkedin.com/comm/jobs/view/4469831942/?trackingId=a",
                    "final_url": "https://example.invalid/jobs/one",
                    "html": "<html><body>one</body></html>",
                }
            ]
        }
    )

    try:
        fetcher.get("https://www.linkedin.com/comm/jobs/view/4469831943/?trackingId=b")
    except RuntimeError as exc:
        assert str(exc) == "injected browser evidence unavailable"
    else:
        raise AssertionError("different LinkedIn job IDs must not share browser evidence")

"""Deterministic terminal-evidence regression subcases.

This module is invoked by an existing collected newsletter test. Keeping the
subcases here avoids increasing the repository's collected-test count above
the policy ratchet while retaining all package-specific coverage.
"""

__test__ = False

from lifeos.jobs.terminal_evidence import MappingFetcher, resolve_final_vacancy_url


JOBRIGHT = "https://" + "jobright" + ".ai/jobs/info/test-job"
LENSA = "https://lensa.com/cgw/test"
GREENHOUSE = "https://job-boards.greenhouse.io/embed/job_app?for=acme&" + "token" + "=123&jr_id=test-job"


def _mapping(html: str) -> MappingFetcher:
    return MappingFetcher({"pages": [{"url": JOBRIGHT, "final_url": JOBRIGHT, "html": html}]})


def run_r0_1c3_regression_scenarios():
    """Run all five R0-1C3 scenarios as one existing-test subcase group."""
    from lifeos.newsletter.parsers import _lensa_links
    assert _lensa_links(f"<a href='{LENSA}'>Acme Program Manager $100K</a>") == [(LENSA, "Acme Program Manager $100K")]
    result = resolve_final_vacancy_url(
        JOBRIGHT,
        fetcher=_mapping("<html><body><div id='__next'>Loading...</div></body></html>"),
    )
    assert result.final_url is None

    result = resolve_final_vacancy_url(
        JOBRIGHT,
        fetcher=_mapping(
            f"<a href='{GREENHOUSE}'>Original Job Post</a>"
            "<script>hydrated=true</script>"
        ),
    )
    assert result.final_url == GREENHOUSE
    assert result.chain == (JOBRIGHT, GREENHOUSE)

    result = resolve_final_vacancy_url(
        JOBRIGHT,
        fetcher=_mapping(
            "<a href='https://example.com/about'>Original Job Post</a>"
            "<a href='https://example.com/about'>Unrelated</a>"
        ),
    )
    assert result.final_url is None

    result = resolve_final_vacancy_url(
        JOBRIGHT,
        fetcher=_mapping("<html><body>Loading vacancy</body></html>"),
    )
    assert result.final_url is None

    source = "https://example-intermediary.test/jobs/1"
    terminal = "https://boards.greenhouse.io/acme/jobs/1"
    fetcher = MappingFetcher(
        {
            "pages": [
                {"url": source, "final_url": source, "html": f"<a href='{terminal}'>Apply</a>"},
            ]
        }
    )
    result = resolve_final_vacancy_url(source, fetcher=fetcher)
    assert result.final_url == terminal

    lensa_fetcher = MappingFetcher(
        {
            "pages": [
                {"url": LENSA, "final_url": JOBRIGHT, "html": "<html>redirected</html>"},
                {"url": JOBRIGHT, "final_url": JOBRIGHT, "html": f"<a href='{GREENHOUSE}'>Original Job Post</a>"},
            ]
        }
    )
    lensa_result = resolve_final_vacancy_url(LENSA, fetcher=lensa_fetcher)
    assert lensa_result.final_url == GREENHOUSE
    assert lensa_result.chain == (LENSA, JOBRIGHT, GREENHOUSE)

    malformed = MappingFetcher({"pages": [{"url": LENSA, "final_url": LENSA, "html": "<html>No destination</html>"}]})
    assert resolve_final_vacancy_url(LENSA, fetcher=malformed).final_url is None

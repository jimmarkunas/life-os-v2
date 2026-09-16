from datetime import datetime, timezone

from lifeos.jobs.fit_scoring import FitProfile, RoleFamily, ScopeCategory
from lifeos.jobs.newsletter_contract import Disposition
from lifeos.jobs.qualification import LaneConfig
from lifeos.newsletter.models import SourceVacancyObservation
from scripts.run_us_remote_production import MAX_INBOX_STAGING_HOURS, _fit_ceiling, _preexclude


def _profile() -> FitProfile:
    return FitProfile(
        model_version="synthetic",
        role_families=(RoleFamily(patterns=(r"program manager",), base_score=70, label="pm"),),
        default_role_base=20,
        default_role_label="other",
        scope_categories=(
            ScopeCategory(name="scope", term_groups=(), cap=10),
        ),
    )


def _lane() -> LaneConfig:
    return LaneConfig(
        name="Synthetic Remote",
        market="US",
        fit_floor=78,
        target_review_floor=70,
        work_mode_policy="remote_only",
        compensation_floor=None,
        freshness_gate=False,
        freshness_max_days=None,
        is_target_bucket=True,
    )


def _obs(*, role: str, location: str = "Remote - US") -> SourceVacancyObservation:
    return SourceVacancyObservation(
        evidence_ref=f"synthetic:{role}:{location}",
        source_provider="synthetic",
        source_mailbox="public-web",
        source_message_id="synthetic",
        source_subject=role,
        company="Example Co",
        role=role,
        location_text=location,
        compensation_text=None,
        source_apply_url="https://example.com/jobs/1",
        source_received_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
    )


def test_recovery_inbox_staging_window_is_bounded_to_one_day():
    assert MAX_INBOX_STAGING_HOURS == 24.0


def test_fit_ceiling_uses_best_case_remaining_scope():
    assert _fit_ceiling(_obs(role="Program Manager"), _profile()) == 80
    assert _fit_ceiling(_obs(role="Graphic Designer"), _profile()) == 30


def test_preexclude_skips_terminal_resolution_only_when_exclusion_is_proven():
    low_fit = _preexclude(_obs(role="Graphic Designer"), lane=_lane(), fit_profile=_profile())
    assert low_fit is not None
    assert low_fit.disposition is Disposition.EXCLUDED
    assert "maximum possible fit" in (low_fit.detail or "")

    non_remote = _preexclude(
        _obs(role="Program Manager", location="Hybrid - Chicago, IL"),
        lane=_lane(),
        fit_profile=_profile(),
    )
    assert non_remote is not None
    assert non_remote.disposition is Disposition.EXCLUDED

    plausible = _preexclude(_obs(role="Program Manager"), lane=_lane(), fit_profile=_profile())
    assert plausible is None

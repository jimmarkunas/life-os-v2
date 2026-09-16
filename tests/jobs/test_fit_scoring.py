from __future__ import annotations

from lifeos.jobs.fit_scoring import FitEvidence, FitProfile, PenaltyRule, RoleFamily, ScopeCategory, score

# Entirely synthetic profile -- no real career terms, proving the generic
# mechanics work for ANY injected profile, not just Jim's.
FAKE_PROFILE = FitProfile(
    model_version="test-1",
    role_families=(
        RoleFamily(patterns=(r"\bsynthetic senior role\b",), base_score=80, label="Synthetic Senior Role"),
        RoleFamily(patterns=(r"\bsynthetic role\b",), base_score=60, label="Synthetic Role"),
    ),
    default_role_base=20,
    default_role_label="weak synthetic alignment",
    scope_categories=(
        ScopeCategory(
            name="widgets",
            term_groups=(
                (("widget-alpha", "widget-beta"), 5, "widget scope"),
                (("gadget",), 5, "gadget scope"),
            ),
            cap=8,
        ),
    ),
    penalties=(PenaltyRule(terms=("off-target-synthetic",), penalty=30, reason="off-target synthetic role"),),
)


def test_role_family_base_score_applies():
    result = score(FitEvidence(role="Synthetic Role"), profile=FAKE_PROFILE)
    assert "Synthetic Role" in result.rationale
    assert result.score >= 60


def test_default_fallback_when_no_family_matches():
    result = score(FitEvidence(role="Completely Unrelated Title"), profile=FAKE_PROFILE)
    assert result.score == 20


def test_scope_category_points_are_additive_and_capped():
    result = score(
        FitEvidence(role="Synthetic Role", description_text="works with widget-alpha and gadget systems"),
        profile=FAKE_PROFILE,
    )
    # base 60 + widget scope 5 + gadget scope 5 = 70, under the cap of 8 total... capped to 60+8=68
    assert result.score == 68


def test_penalty_reduces_score():
    result = score(FitEvidence(role="off-target-synthetic role"), profile=FAKE_PROFILE)
    baseline = score(FitEvidence(role="Synthetic Role"), profile=FAKE_PROFILE)
    assert result.score < baseline.score


def test_score_never_below_zero_or_above_100():
    heavy_penalty_profile = FitProfile(
        model_version="test-1",
        role_families=(),
        default_role_base=10,
        default_role_label="x",
        penalties=(PenaltyRule(terms=("bad",), penalty=1000, reason="huge"),),
    )
    result = score(FitEvidence(role="bad"), profile=heavy_penalty_profile)
    assert result.score == 0


def test_provider_score_never_influences_result():
    """FitEvidence deliberately has no provider_score field at all -- this
    test documents that omission is intentional: a provider match percentage
    can never contribute points, by construction, not just by convention."""
    assert not hasattr(FitEvidence(role="x"), "provider_score")


def test_rationale_is_deterministic_and_reproducible():
    a = score(FitEvidence(role="Synthetic Senior Role", description_text="widget-alpha"), profile=FAKE_PROFILE)
    b = score(FitEvidence(role="Synthetic Senior Role", description_text="widget-alpha"), profile=FAKE_PROFILE)
    assert a == b


def test_model_version_carried_from_profile():
    result = score(FitEvidence(role="x"), profile=FAKE_PROFILE)
    assert result.model_version == "test-1"

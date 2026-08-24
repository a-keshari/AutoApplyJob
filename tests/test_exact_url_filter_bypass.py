"""Tests for bypassing job-match filters when exact search URLs are configured."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from core.filter import score_job


def _config(search_urls):
    return SimpleNamespace(
        search_criteria=SimpleNamespace(
            job_titles=["Power Platform Architect"],
            locations=["Austin, TX"],
            search_urls=search_urls,
            remote_only=False,
            salary_min=200000,
            keywords_include=["Dataverse"],
            keywords_exclude=["clearance"],
        ),
        bot=SimpleNamespace(min_match_score=75),
        company_blacklist=["Blocked Corp"],
    )


def _job(platform="linkedin"):
    return SimpleNamespace(
        title="Unrelated Role",
        company="Blocked Corp",
        location="Nowhere",
        salary="$20,000",
        description="Requires clearance and unrelated technology.",
        external_id="job-123",
        platform=platform,
    )


def test_linkedin_exact_url_bypasses_all_matching_filters():
    config = _config([
        "https://www.linkedin.com/jobs/search/?keywords=Power%20Platform&f_AL=true"
    ])
    db = MagicMock()
    db.exists.return_value = False

    result = score_job(_job("linkedin"), config, db)

    assert result.pass_filter is True
    assert result.score == 100
    assert result.skip_reason is None


def test_exact_url_mode_still_blocks_already_applied_jobs():
    config = _config([
        "https://www.linkedin.com/jobs/search/?keywords=Power%20Platform&f_AL=true"
    ])
    db = MagicMock()
    db.exists.return_value = True

    result = score_job(_job("linkedin"), config, db)

    assert result.pass_filter is False
    assert result.score == 0
    assert result.skip_reason == "Already applied"


def test_linkedin_exact_url_does_not_bypass_indeed_filters():
    config = _config([
        "https://www.linkedin.com/jobs/search/?keywords=Power%20Platform&f_AL=true"
    ])
    db = MagicMock()
    db.exists.return_value = False

    result = score_job(_job("indeed"), config, db)

    assert result.pass_filter is False
    assert result.score == 0
    assert result.skip_reason == "Blacklisted company: Blocked Corp"


def test_indeed_exact_url_bypasses_filters_for_indeed_only():
    config = _config([
        "https://www.indeed.com/jobs?q=power+platform&l=United+States&fromage=1"
    ])
    db = MagicMock()
    db.exists.return_value = False

    result = score_job(_job("indeed"), config, db)

    assert result.pass_filter is True
    assert result.score == 100
    assert result.skip_reason is None

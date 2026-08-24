"""Tests for exact platform search URLs."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bot.search.indeed import IndeedSearcher
from bot.search.linkedin import LinkedInSearcher
from config.settings import SearchCriteria


def _criteria(search_urls: list[str]):
    return SimpleNamespace(
        job_titles=["Software Engineer"],
        locations=["Austin, TX"],
        search_urls=search_urls,
        remote_only=False,
        max_results_per_search=100,
    )


def test_search_criteria_defaults_to_no_exact_urls():
    criteria = SearchCriteria(job_titles=["Engineer"], locations=["Austin, TX"])
    assert criteria.search_urls == []


def test_linkedin_exact_url_takes_precedence_over_generated_search():
    exact_url = (
        "https://www.linkedin.com/jobs/search/?keywords=Power%20Platform"
        "&location=United%20States&f_TPR=r86400&f_AL=true"
    )
    searcher = LinkedInSearcher()
    page = MagicMock()

    with (
        patch.object(searcher, "_search_url", return_value=iter(["linkedin-job"])) as exact,
        patch.object(searcher, "_search_page", return_value=iter(["generated-job"])) as generated,
    ):
        results = list(searcher.search(_criteria([exact_url]), page=page))

    assert results == ["linkedin-job"]
    exact.assert_called_once_with(page, exact_url, 100)
    generated.assert_not_called()


def test_indeed_exact_url_takes_precedence_over_generated_search():
    exact_url = "https://www.indeed.com/jobs?q=power+platform&l=Texas&fromage=1"
    searcher = IndeedSearcher()
    page = MagicMock()

    with (
        patch.object(searcher, "_search_url", return_value=iter(["indeed-job"])) as exact,
        patch.object(searcher, "_search_page", return_value=iter(["generated-job"])) as generated,
    ):
        results = list(searcher.search(_criteria([exact_url]), page=page))

    assert results == ["indeed-job"]
    exact.assert_called_once_with(page, exact_url, 100)
    generated.assert_not_called()


def test_platform_ignores_other_platform_exact_urls_and_keeps_existing_search():
    criteria = _criteria(["https://www.indeed.com/jobs?q=developer&l=Austin%2C+TX"])
    searcher = LinkedInSearcher()
    page = MagicMock()

    with (
        patch.object(searcher, "_search_url", return_value=iter(["exact-job"])) as exact,
        patch.object(searcher, "_search_page", return_value=iter(["generated-job"])) as generated,
    ):
        results = list(searcher.search(criteria, page=page))

    assert results == ["generated-job"]
    exact.assert_not_called()
    generated.assert_called_once_with(
        page, "Software Engineer", "Austin, TX", criteria, 100
    )


def test_only_jobs_search_urls_are_accepted_for_each_platform():
    criteria = _criteria([
        "https://www.linkedin.com/feed/",
        "https://www.linkedin.com/jobs/search/?keywords=Developer",
        "https://indeed.com/jobs?q=Developer",
        "https://example.com/jobs?q=Developer",
    ])

    assert LinkedInSearcher._custom_search_urls(criteria) == [
        "https://www.linkedin.com/jobs/search/?keywords=Developer"
    ]
    assert IndeedSearcher._custom_search_urls(criteria) == [
        "https://indeed.com/jobs?q=Developer"
    ]

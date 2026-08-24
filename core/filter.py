"""Job filter and scoring engine.

Implements: FR-044 (job scoring), FR-045 (ATS detection).

Scores jobs 0-100 based on title match, salary match, location match,
and keyword match. Applies hard disqualifiers for excluded keywords,
blacklisted companies, and duplicate jobs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from bot.search.base import RawJob
    from config.settings import AppConfig
    from db.database import Database


@dataclass
class ScoredJob:
    """A job after scoring — includes match score and pass/fail verdict."""

    id: str  # UUID for filename generation
    raw: "RawJob"
    score: int
    pass_filter: bool
    skip_reason: str | None


# ATS fingerprints for URL-based detection
ATS_FINGERPRINTS = {
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "myworkdayjobs.com": "workday",
    "ashbyhq.com": "ashby",
    "taleo.net": "taleo",
    "icims.com": "icims",
    "linkedin.com/jobs": "linkedin",
    "linkedin.com": "linkedin",
    "indeed.com": "indeed",
}


def detect_ats(url: str) -> str | None:
    """Detect ATS platform from a job URL.

    Args:
        url: The apply URL to check.

    Returns:
        ATS name string or None if unrecognized.
    """
    url_lower = url.lower()
    for domain, ats in ATS_FINGERPRINTS.items():
        if domain in url_lower:
            return ats
    return None


def _uses_exact_search_url(criteria, platform: str) -> bool:
    """Return True when this platform is driven by a configured exact search URL."""
    for value in getattr(criteria, "search_urls", []) or []:
        url = str(value).strip()
        if not url:
            continue
        parsed = urlparse(url)
        host = parsed.netloc.lower().split(":", 1)[0]
        if platform == "linkedin":
            if host in {"linkedin.com", "www.linkedin.com"} and parsed.path.startswith("/jobs"):
                return True
        elif platform == "indeed":
            if (host == "indeed.com" or host.endswith(".indeed.com")) and parsed.path.startswith("/jobs"):
                return True
    return False


def score_job(
    raw_job: "RawJob",
    config: "AppConfig",
    db: "Database | None" = None,
) -> ScoredJob:
    """Score a job against user criteria and return a ScoredJob.

    Scoring breakdown (0-100):
      - Title match: 0-35 points
      - Salary match: 0-20 points
      - Location match: 0-20 points
      - Keyword match: 0-25 points

    Hard disqualifiers (score=0):
      - Exclude keyword found in title or description
      - Company in blacklist
      - Job already in database (deduplication)

    When a valid exact search URL is configured for the job's platform,
    that URL is treated as the user's shortlist: matching/scoring filters are
    bypassed, but duplicate protection is still enforced.

    Args:
        raw_job: The unscored job listing.
        config: Application configuration with search criteria.
        db: Optional database for deduplication check.

    Returns:
        ScoredJob with score, pass/fail, and skip reason.
    """
    job_id = str(uuid.uuid4())
    criteria = config.search_criteria

    title_lower = raw_job.title.lower()
    desc_lower = raw_job.description.lower()
    combined_lower = f"{title_lower} {desc_lower}"

    # --- Hard disqualifiers ---

    # Deduplication always applies, including exact-URL mode.
    if db is not None and db.exists(raw_job.external_id, raw_job.platform):
        return ScoredJob(
            id=job_id, raw=raw_job, score=0,
            pass_filter=False, skip_reason="Already applied",
        )

    # An exact platform search URL is already the user's shortlist.
    if _uses_exact_search_url(criteria, raw_job.platform):
        return ScoredJob(
            id=job_id, raw=raw_job, score=100,
            pass_filter=True, skip_reason=None,
        )

    # Blacklisted company
    company_lower = raw_job.company.lower()
    for blacklisted in config.company_blacklist:
        if blacklisted.lower() in company_lower:
            return ScoredJob(
                id=job_id, raw=raw_job, score=0,
                pass_filter=False,
                skip_reason=f"Blacklisted company: {blacklisted}",
            )

    # Exclude keywords
    for kw in criteria.keywords_exclude:
        if kw.lower() in combined_lower:
            return ScoredJob(
                id=job_id, raw=raw_job, score=0,
                pass_filter=False,
                skip_reason=f"Excluded keyword: {kw}",
            )

    # --- Scoring ---

    score = 0

    # Title match (0-35)
    for target_title in criteria.job_titles:
        target_lower = target_title.lower()
        if target_lower in title_lower:
            score += 35
            break
        # Partial overlap: check if any word from target is in title
        target_words = set(target_lower.split())
        title_words = set(title_lower.split())
        overlap = target_words & title_words
        if len(overlap) >= len(target_words) * 0.5:
            score += 20
            break

    # Salary match (0-20)
    if criteria.salary_min is None:
        score += 20  # No salary requirement — full points
    elif raw_job.salary is None:
        score += 10  # Unknown salary — partial credit
    else:
        salary_num = _extract_salary_number(raw_job.salary)
        if salary_num is not None and salary_num >= criteria.salary_min:
            score += 20
        elif salary_num is None:
            score += 10  # Couldn't parse — give benefit of doubt

    # Location match (0-20)
    job_location_lower = raw_job.location.lower()
    if criteria.remote_only and "remote" in job_location_lower:
        score += 20
    elif not criteria.remote_only:
        for loc in criteria.locations:
            if loc.lower() in job_location_lower:
                score += 20
                break
        else:
            # Check for same country (rough heuristic)
            if any(
                loc.lower().split(",")[-1].strip() in job_location_lower
                for loc in criteria.locations
                if "," in loc
            ):
                score += 10

    # Keyword match (0-25, +5 per keyword, max 25)
    kw_score = 0
    for kw in criteria.keywords_include:
        if kw.lower() in combined_lower:
            kw_score += 5
    score += min(kw_score, 25)

    # --- Threshold check ---
    min_score = config.bot.min_match_score
    pass_filter = score >= min_score

    skip_reason = None if pass_filter else f"Score {score} below threshold {min_score}"

    return ScoredJob(
        id=job_id, raw=raw_job, score=score,
        pass_filter=pass_filter, skip_reason=skip_reason,
    )


def _extract_salary_number(salary_str: str) -> int | None:
    """Extract an annual salary number from a salary string.

    Handles formats like "$120,000", "$120K", "$60/hr", "120000-150000".
    Returns the lower bound as an annual integer, or None if unparsable.
    """
    import re

    cleaned = salary_str.replace(",", "").replace("$", "").strip().lower()

    # Try to find numbers
    numbers = re.findall(r"[\d.]+", cleaned)
    if not numbers:
        return None

    try:
        value = float(numbers[0])
    except ValueError:
        return None

    # K suffix
    if "k" in cleaned:
        value *= 1000

    # Hourly -> annual (assume 2080 hours/year)
    if "/hr" in cleaned or "per hour" in cleaned or "hourly" in cleaned:
        value *= 2080

    return int(value)

"""Indeed job search engine using Playwright.

Implements: FR-044 (Indeed search).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Iterator
from urllib.parse import quote_plus, urlparse

from bot.search.base import BaseSearcher, RawJob

if TYPE_CHECKING:
    from config.settings import SearchCriteria

logger = logging.getLogger(__name__)

INDEED_SEARCH_URL = "https://www.indeed.com/jobs"


class IndeedSearcher(BaseSearcher):
    """Search Indeed jobs via browser automation."""

    def search(self, criteria: SearchCriteria, page=None) -> Iterator[RawJob]:
        """Search Indeed for jobs matching criteria.

        Exact Indeed search URLs take precedence when configured. Otherwise
        the existing title/location URL generation is used unchanged.

        Args:
            criteria: Search parameters.
            page: Playwright Page instance.

        Yields:
            RawJob objects for each job found.
        """
        if page is None:
            logger.warning("IndeedSearcher requires a Playwright page")
            return

        max_results = getattr(criteria, "max_results_per_search", 100)
        found = 0
        custom_urls = self._custom_search_urls(criteria)

        if custom_urls:
            for url in custom_urls:
                if found >= max_results:
                    return
                try:
                    for raw_job in self._search_url(page, url, max_results - found):
                        found += 1
                        yield raw_job
                except Exception as e:
                    logger.error("Indeed exact-URL search failed for '%s': %s", url, e)
            return

        for title in criteria.job_titles:
            for location in criteria.locations:
                if found >= max_results:
                    return

                try:
                    yield from self._search_page(
                        page, title, location, max_results - found
                    )
                except Exception as e:
                    logger.error(
                        "Indeed search failed for '%s' in '%s': %s",
                        title, location, e,
                    )

    @staticmethod
    def _custom_search_urls(criteria) -> list[str]:
        urls: list[str] = []
        for value in getattr(criteria, "search_urls", []) or []:
            url = str(value).strip()
            if not url:
                continue
            parsed = urlparse(url)
            host = parsed.netloc.lower().split(":", 1)[0]
            if (host == "indeed.com" or host.endswith(".indeed.com")) and parsed.path.startswith("/jobs"):
                urls.append(url)
        return urls

    def _search_page(
        self, page, title: str, location: str, remaining: int
    ) -> Iterator[RawJob]:
        """Search a single title+location combination on Indeed."""
        url = (
            f"{INDEED_SEARCH_URL}"
            f"?q={quote_plus(title)}"
            f"&l={quote_plus(location)}"
        )

        logger.info("Indeed: searching '%s' in '%s'", title, location)
        yield from self._search_url(page, url, remaining)

    def _search_url(self, page, url: str, remaining: int) -> Iterator[RawJob]:
        """Search and paginate from an exact Indeed search URL without modifying it."""
        logger.info("Indeed: opening search URL %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        found = 0
        page_num = 0

        while found < remaining:
            job_cards = page.query_selector_all(
                ".job_seen_beacon, "
                ".jobsearch-ResultsList .result, "
                "[data-jk]"
            )

            if not job_cards:
                logger.info("Indeed: no job cards found on page %d", page_num)
                break

            for card in job_cards:
                if found >= remaining:
                    return

                try:
                    raw_job = self._extract_job(page, card)
                    if raw_job:
                        found += 1
                        yield raw_job
                except Exception as e:
                    logger.debug("Indeed: failed to extract job card: %s", e)

            # Try next page
            page_num += 1
            next_link = page.query_selector(
                "a[data-testid='pagination-page-next'], "
                ".np[aria-label='Next Page']"
            )
            if not next_link:
                break

            next_link.click()
            time.sleep(2)

    def _extract_job(self, page, card) -> RawJob | None:
        """Extract job details from an Indeed job card."""
        # Click card to load details
        title_link = card.query_selector(
            "h2.jobTitle a, "
            ".jobTitle > a, "
            "a[data-jk]"
        )

        if not title_link:
            return None

        title = title_link.inner_text().strip()
        if not title:
            return None

        try:
            title_link.click()
            time.sleep(1)
        except Exception as e:
            logger.debug("Indeed: failed to click job title: %s", e)
            pass

        # Company name
        company_el = card.query_selector(
            "[data-testid='company-name'], "
            ".companyName, "
            ".company_location .companyName"
        )
        company = company_el.inner_text().strip() if company_el else "Unknown"

        # Location
        location_el = card.query_selector(
            "[data-testid='text-location'], "
            ".companyLocation, "
            ".company_location .companyLocation"
        )
        location_text = location_el.inner_text().strip() if location_el else ""

        # Job ID
        job_id = card.get_attribute("data-jk") or ""
        if not job_id and title_link:
            href = title_link.get_attribute("href") or ""
            if "jk=" in href:
                job_id = href.split("jk=")[1].split("&")[0]

        if not job_id:
            return None

        # Description from detail panel
        desc_el = page.query_selector(
            "#jobDescriptionText, "
            ".jobsearch-jobDescriptionText, "
            "[id='jobDescriptionText']"
        )
        description = desc_el.inner_text().strip() if desc_el else ""

        # Salary
        salary_el = card.query_selector(
            ".salary-snippet-container, "
            ".metadata.salary-snippet-container, "
            "[data-testid='attribute_snippet_testid']"
        )
        salary = salary_el.inner_text().strip() if salary_el else None

        apply_url = f"https://www.indeed.com/viewjob?jk={job_id}"

        return RawJob(
            title=title,
            company=company,
            location=location_text,
            salary=salary,
            description=description,
            apply_url=apply_url,
            platform="indeed",
            external_id=f"indeed-{job_id}",
            posted_at=None,
        )

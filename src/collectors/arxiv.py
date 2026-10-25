"""
Collects the weekly number of new arXiv submissions in selected subject categories.

Source: the arXiv API (https://info.arxiv.org/help/api/). Its metadata is
available under CC0 1.0. The terms of use allow one request every three seconds
over a single connection (HOST_INTERVALS in pipeline.py); using the data does
not imply an endorsement by arXiv. ``export.arxiv.org`` disallows crawling in
its robots.txt, but the API is offered for programmatic use, so it is called
with ``respect_robots=False``.

For every category in CATEGORIES and every ISO week (Monday 00:00 to Sunday
23:59 UTC, keyed by the Monday), one query
``cat:<category> AND submittedDate:[<Monday>0000 TO <Sunday>2359]`` with
``max_results=1`` returns the number of matching papers in
``opensearch:totalResults``. ``cat:`` includes cross-listed papers, and
``submittedDate`` is the submission time of the first version, so that each
paper counts once per category, in the week of its first version. No entry data
(titles, authors) is read or stored.

The API lists a paper only after its announcement: the submissions of a week
appear by the following Tuesday, papers on hold and submissions around holidays
later. A week is therefore counted once it ended at least SETTLE_DAYS days
before the run day, and it is stored once, with the count at that time.

Each run fetches the settled weeks since the last stored week of every
category. A new category gets 8 weeks of history (104 weeks in a backfill run;
the date filter works back to 2010).

Schedule: once a month (``min_days_between_runs = 27``); the API keeps the
history, so the weeks since the last run are counted together. A run that
stops early, at the run deadline, at a rate limit or after a failed request,
is marked incomplete, so that the next run continues with the remaining weeks.
"""
import logging
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any, Optional
from urllib.parse import urlencode

import defusedxml.ElementTree as DefusedElementTree
from defusedxml import DefusedXmlException

from collectors.base import Collector, CollectorResult, RunContext, series_start
from schema import ArxivSubmissionRecord
from state import CollectorEntry, is_backfilled
from web import BlockedError, FetchError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "arxiv_submissions"
SOURCE_ID = "arxiv_api"
LICENSE = "CC0-1.0 (arXiv metadata)"
API_URL = "https://export.arxiv.org/api/query"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
OPENSEARCH_NS = "{http://a9.com/-/spec/opensearch/1.1/}"
# Part of the id of the entry with which the API reports an error inside a feed
ERROR_ENTRY_MARKER = "/api/errors"
# Counted categories: code -> name. A change of this basket is a measurement change.
CATEGORIES: dict[str, str] = {
    "cs.AI": "Artificial Intelligence",
    "cs.LG": "Machine Learning",
    "cs.CL": "Computation and Language",
    "cs.CV": "Computer Vision and Pattern Recognition",
    "cs.RO": "Robotics",
    "cs.CR": "Cryptography and Security",
    "cs.DC": "Distributed, Parallel, and Cluster Computing",
    "cs.NI": "Networking and Internet Architecture",
    "cs.SE": "Software Engineering",
    "cs.PL": "Programming Languages",
    "cs.DB": "Databases",
    "cs.AR": "Hardware Architecture",
    "cs.ET": "Emerging Technologies",
    "cs.HC": "Human-Computer Interaction",
    "cs.IR": "Information Retrieval",
    "cs.MA": "Multiagent Systems",
    "quant-ph": "Quantum Physics",
    "stat.ML": "Machine Learning (Statistics)",
    "eess.SP": "Signal Processing",
    "eess.SY": "Systems and Control",
}
NEW_CATEGORY_WEEKS = 8
BACKFILL_WEEKS = 104
# Days that must pass after the end of a week before it is counted (see the module docstring)
SETTLE_DAYS = 7
# The collector stops after this many failed requests in a row, e.g. when the API is down
MAX_CONSECUTIVE_FAILURES = 3


def last_settled_monday(today: date) -> date:
    """Returns the Monday of the latest ISO week that ended at least SETTLE_DAYS days before a day.

    Args:
        today: Day of the run.

    Returns:
        date: The Monday.
    """
    day = today - timedelta(days=7 + SETTLE_DAYS)
    return day - timedelta(days=day.weekday())


def next_monday(day: date) -> date:
    """Returns the day itself if it is a Monday, else the following Monday.

    Args:
        day: The day.

    Returns:
        date: The Monday.
    """
    return day + timedelta(days=-day.weekday() % 7)


def weeks_between(first: date, last: date) -> list[date]:
    """Returns the Mondays from ``first`` to ``last``, both included.

    Args:
        first: First Monday.
        last: Last Monday.

    Returns:
        list[date]: The Mondays; empty if ``first`` is after ``last``.
    """
    if first > last:
        return []
    return [first + timedelta(weeks=n) for n in range((last - first).days // 7 + 1)]


def query_url(category: str, first_day: date, last_day: date) -> str:
    """Builds the API URL that counts the papers of a category first submitted in a range of days.

    Args:
        category: arXiv category, e.g. ``cs.LG``.
        first_day: First day; from 00:00 UTC.
        last_day: Last day; up to 23:59 UTC (the filter is inclusive to the minute).

    Returns:
        str: The query URL with ``max_results=1`` (the API answers ``max_results=0`` with HTTP 500).
    """
    search = f"cat:{category} AND submittedDate:[{first_day:%Y%m%d}0000 TO {last_day:%Y%m%d}2359]"
    return f"{API_URL}?" + urlencode({"search_query": search, "max_results": 1})


def parse_total_results(content: bytes, url: str) -> int:
    """Reads the number of results from an API response.

    Args:
        content: The Atom feed.
        url: The requested URL, for error messages.

    Returns:
        int: The value of ``opensearch:totalResults``.

    Raises:
        FetchError: If the body is not valid XML, reports an API error, has no total,
            or is inconsistent: a positive total without an entry or an entry with a total of 0.
    """
    try:
        root = DefusedElementTree.fromstring(content)
    except (DefusedElementTree.ParseError, DefusedXmlException) as e:
        raise FetchError(url, f"response is not valid XML ({e.__class__.__name__})") from e
    entries = root.findall(f"{ATOM_NS}entry")
    for entry in entries:
        if ERROR_ENTRY_MARKER in (entry.findtext(f"{ATOM_NS}id") or ""):
            message = " ".join((entry.findtext(f"{ATOM_NS}summary") or "").split())
            raise FetchError(url, f"API error: {message[:200]}")
    try:
        total = int((root.findtext(f"{OPENSEARCH_NS}totalResults") or "").strip())
    except ValueError as e:
        raise FetchError(url, "response without totalResults") from e
    if total < 0 or (total > 0) != bool(entries):
        raise FetchError(url, f"inconsistent response: totalResults {total} with {len(entries)} entries")
    return total


class ArxivCollector(Collector):
    """Writes the weekly number of new arXiv submissions per subject category."""
    name = "arxiv"
    datasets = (DATASET,)
    min_days_between_runs = 27

    def __init__(self, categories: Mapping[str, str] = CATEGORIES) -> None:
        """Initializes the collector.

        Args:
            categories: Category names by code; replaceable in tests.
        """
        self._categories = dict(categories)

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Fetches the settled weeks since the last stored week of every category and emits one record per category.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; incomplete if weeks remain because of the run
            deadline, a rate limit or a failed request. The progress per category is
            kept in the seen store.
        """
        seen = ctx.seen(DATASET)
        last_week = last_settled_monday(ctx.today)
        history_weeks = BACKFILL_WEEKS if ctx.backfill else NEW_CATEGORY_WEEKS
        history_start = last_week - timedelta(weeks=history_weeks - 1)
        counters: dict[str, Any] = {"categories": len(self._categories), "records": 0, "weeks": 0, "requests": 0,
                                    "failed": 0, "deferred": 0}
        records: list[ArxivSubmissionRecord] = []
        complete, stopped = True, False
        error: Optional[str] = None
        successes = consecutive_failures = 0
        try:
            for position, (category, category_name) in enumerate(self._categories.items()):
                if stopped:
                    counters["deferred"] = len(self._categories) - position
                    break
                first_week = next_monday(series_start(seen.get(category), history_start, key="last_week"))
                counts: dict[date, int] = {}
                for monday in weeks_between(first_week, last_week):
                    if ctx.past_deadline():
                        complete, stopped = False, True
                        break
                    url = query_url(category, monday, monday + timedelta(days=6))
                    counters["requests"] += 1
                    try:
                        counts[monday] = parse_total_results(ctx.web.get(url, respect_robots=False).content, url)
                    except RateLimitError as e:
                        logger.warning("arXiv API rate limit reached at %s: %s", category, e)
                        complete, stopped = False, True
                        break
                    except BlockedError as e:
                        logger.error("arXiv API refused the request for %s: %s", category, e)
                        complete, stopped, error = False, True, f"arXiv API refused the requests: {e}"
                        break
                    except FetchError as e:
                        logger.warning("arXiv count of %s in the week of %s failed: %s", category, monday, e)
                        counters["failed"] += 1
                        consecutive_failures += 1
                        complete = False
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            stopped = True
                            error = f"stopped after {consecutive_failures} failed requests in a row: {e}"
                        break
                    successes += 1
                    consecutive_failures = 0
                if counts:
                    records.append(self._record(ctx, entry, category, category_name, counts))
                    # Each record covers new weeks, so it is a new item with revision 0
                    seen.mark(category, category_name, ctx.today, data={"last_week": max(counts).isoformat()})
                    counters["weeks"] += len(counts)
        finally:
            ctx.emit(DATASET, records)
            counters["records"] += len(records)
        if error is None and counters["failed"] and not successes:
            error = f"all {counters['failed']} attempted requests failed"
        logger.info("arXiv: %d records with %d weeks for %d categories.", counters["records"], counters["weeks"],
                    len(self._categories))
        return CollectorResult(counters=counters, complete=complete, error=error)

    @staticmethod
    def _record(ctx: RunContext, entry: CollectorEntry, category: str, category_name: str,
                counts: Mapping[date, int]) -> ArxivSubmissionRecord:
        """Builds the record of the counted weeks of a category.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            category: Category code.
            category_name: Category name.
            counts: Submissions by Monday of the week.

        Returns:
            ArxivSubmissionRecord: The record.
        """
        first, last = min(counts), max(counts)
        return ArxivSubmissionRecord(
            **ctx.envelope(
                DATASET, f"arxiv:{category}:{first}:{last}", revision=0, source_id=SOURCE_ID,
                source_url=query_url(category, first, last + timedelta(days=6)), license=LICENSE,
                backfilled=is_backfilled(first, entry.first_run),
            ),
            category=category,
            category_name=category_name,
            weekly_submissions=dict(sorted(counts.items())),
        )

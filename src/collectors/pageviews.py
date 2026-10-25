"""
Collects the daily views of the English Wikipedia articles of the tracked technologies.

The article of a technology is resolved at run time from its Wikidata id
(sitelink ``enwiki``), so that a renamed article keeps its series. For each
technology the collector fetches the days since the last stored day from the
Wikimedia Pageviews API (CC0), with the agent type ``user`` (views by people,
without known crawlers and automated traffic). A new technology gets the
history from July 2015, the first month of the API.

The collector runs once a month, because the API keeps the daily views and
each run fetches all days since the last stored day; a monthly record holds
the same days as four weekly records with less overhead. At the run deadline
it stops; the remaining articles follow in the next run.
"""
import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from collectors.base import Collector, CollectorResult, RunContext, batches, series_start
from registry import CONFIG_PATH, active_technologies
from schema import PageviewRecord
from state import CollectorEntry, is_backfilled
from web import FetchError, NotFoundError

logger = logging.getLogger(__name__)

DATASET = "wikipedia_pageviews"
SOURCE_ID = "wikimedia_pageviews"
LICENSE = "CC0-1.0"
PROJECT = "en.wikipedia"
PAGEVIEWS_URL = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia.org/"
                 "all-access/user/{title}/daily/{start}/{end}")
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
# The first day with pageview data in the Wikimedia API
HISTORY_START = date(2015, 7, 1)
# Ids per Wikidata API request
API_BATCH_SIZE = 50
# Records are written in batches, so that an interrupted run keeps its progress
EMIT_BATCH_SIZE = 100


def english_titles_for_qids(ctx: RunContext, qids: Sequence[str]) -> dict[str, str]:
    """Returns the English Wikipedia titles of Wikidata items.

    Args:
        ctx: The run context.
        qids: Wikidata ids.

    Returns:
        dict[str, str]: Title by Wikidata id, for items with an English article.
    """
    titles: dict[str, str] = {}
    for batch in batches(sorted(set(qids)), API_BATCH_SIZE):
        data = ctx.web.get(WIKIDATA_API_URL, respect_robots=False, params={
            "action": "wbgetentities", "ids": "|".join(batch), "props": "sitelinks", "sitefilter": "enwiki",
            "format": "json",
        }).json()
        for qid, item in (data.get("entities") or {}).items():
            title = ((item.get("sitelinks") or {}).get("enwiki") or {}).get("title")
            if title:
                titles[qid] = title
    return titles


def article_url(title: str, start: date, end: date) -> str:
    """Builds the Pageviews API URL of an article and a day range.

    Args:
        title: Article title.
        start: First day.
        end: Last day.

    Returns:
        str: The URL.
    """
    encoded = quote(title.replace(" ", "_"), safe="")
    return PAGEVIEWS_URL.format(title=encoded, start=f"{start:%Y%m%d}", end=f"{end:%Y%m%d}")


def fetch_daily_views(ctx: RunContext, title: str, start: date, end: date) -> dict[date, int]:
    """Fetches the daily views of an article.

    Args:
        ctx: The run context.
        title: Article title.
        start: First day.
        end: Last day.

    Returns:
        dict[date, int]: Views by day; days without data are missing.
    """
    try:
        data = ctx.web.get(article_url(title, start, end), respect_robots=False).json()
    except NotFoundError:
        return {}
    views: dict[date, int] = {}
    for item in data.get("items", []):
        timestamp = str(item.get("timestamp", ""))
        try:
            day = datetime.strptime(timestamp[:8], "%Y%m%d").date()
        except ValueError:
            continue
        if isinstance(item.get("views"), int):
            views[day] = item["views"]
    return views


class PageviewsCollector(Collector):
    """Writes the new daily views of the English Wikipedia articles of the tracked technologies."""
    name = "wikipedia_pageviews"
    datasets = (DATASET,)
    min_days_between_runs = 27

    def __init__(self, registry_path: Path = CONFIG_PATH) -> None:
        """Initializes the collector.

        Args:
            registry_path: The registry file.
        """
        self._registry_path = registry_path

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Fetches the days since the last stored day for every article and emits one record per article.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; the progress per article is kept in the seen store.
        """
        technologies = [t for t in active_technologies(self._registry_path) if t.wikidata]
        titles = english_titles_for_qids(ctx, [t.wikidata for t in technologies if t.wikidata])
        seen = ctx.seen(DATASET)
        end = ctx.today - timedelta(days=1)
        records: list[PageviewRecord] = []
        counters: dict[str, Any] = {"articles": 0, "records": 0, "days": 0, "failed": 0, "without_article": 0}
        complete = True
        for position, technology in enumerate(technologies):
            title = titles.get(technology.wikidata or "")
            if title is None:
                logger.warning("No English Wikipedia article for %s (%s).", technology.id, technology.wikidata)
                counters["without_article"] += 1
                continue
            counters["articles"] += 1
            if ctx.past_deadline():
                counters["deferred"] = len(technologies) - position
                complete = False
                break
            start = series_start(seen.get(technology.id), HISTORY_START)
            if start > end:
                continue
            try:
                views = fetch_daily_views(ctx, title, start, end)
            except FetchError as e:
                logger.warning("Pageviews of %s failed: %s", title, e)
                counters["failed"] += 1
                continue
            if not views:
                continue
            first, last = min(views), max(views)
            # Each record covers new days, so it is a new item with revision 0
            seen.mark(technology.id, title, ctx.today, data={"last_day": last.isoformat()})
            records.append(PageviewRecord(
                **ctx.envelope(
                    DATASET, f"pageviews:{technology.id}:{first.isoformat()}:{last.isoformat()}",
                    revision=0, source_id=SOURCE_ID, source_url=article_url(title, first, last),
                    license=LICENSE, backfilled=is_backfilled(first, entry.first_run),
                ),
                technology_id=technology.id,
                wikipedia_project=PROJECT,
                wikipedia_title=title,
                wikidata_qid=technology.wikidata,
                daily_views=dict(sorted(views.items())),
            ))
            counters["days"] += len(views)
            if len(records) >= EMIT_BATCH_SIZE:
                ctx.emit(DATASET, records)
                counters["records"] += len(records)
                records = []
        ctx.emit(DATASET, records)
        counters["records"] += len(records)
        logger.info("Pageviews: %d records with %d days for %d articles.", counters["records"], counters["days"],
                    counters["articles"])
        failed = counters["failed"]
        error = f"{failed} of {counters['articles']} articles failed" if failed and failed == counters["articles"] else None
        return CollectorResult(counters=counters, complete=complete, error=error)

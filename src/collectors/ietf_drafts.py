"""
Collects weekly counts of Internet-Draft submissions from the IETF Datatracker.

Source: the documented Datatracker API (``/api/v1/``), resource
``doc/newrevisiondocevent`` with one event per submitted version of a
document. Per scope and ISO week (Monday 00:00 to the next Monday 00:00 UTC;
the API reads times without offset as UTC) the collector reads the number of
events (``meta.total_count`` of a request with ``limit=1``) for documents of
type draft, once for all versions and once for revision 00 (new drafts).
Without the type filter the endpoint would also count agendas and slides.
Scopes:
- ``all``: all drafts;
- ``group_type:wg``, ``group_type:individ``, ``group_type:rg``: drafts of
  IETF working groups, individual submissions and IRTF research groups;
- ``area:<acronym>``: drafts of the working groups whose parent is the IETF
  area (art, gen, int, ops, rtg, sec, wit). The filter on the group type is
  needed because the pseudo-group of the individual submissions (acronym
  ``none``) has the GEN area as parent; without it, ``area:gen`` would count
  the individual submissions. The group and its area are those at the time
  of observation, so that a week is attributed to the areas that existed
  when it was counted.

Each run stores per scope one record with the complete weeks since the last
stored week (seen data ``first_week`` and ``last_week`` per scope). A new
scope gets the last 8 weeks, a backfill run the last 104 weeks; for a scope
with stored weeks, a backfill run also fetches the older weeks up to that
limit, newest first, so that the stored weeks stay contiguous.

The collector runs once a month (``min_days_between_runs = 27``); the API keeps the
history, so the weeks since the last run are counted together. At the run deadline it stops; the complete
weeks fetched so far are stored and the remaining weeks follow in the next run.
"""
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional
from urllib.parse import urlencode

from collectors.base import Collector, CollectorResult, RunContext
from schema import IetfDraftActivityRecord
from state import CollectorEntry, SeenEntry, SeenStore, is_backfilled
from web import BlockedError, FetchError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "ietf_draft_activity"
SOURCE_ID = "ietf_datatracker"
LICENSE = "IETF Trust (Datatracker API)"
EVENTS_URL = "https://datatracker.ietf.org/api/v1/doc/newrevisiondocevent/"
AREAS = ("art", "gen", "int", "ops", "rtg", "sec", "wit")
# Query filters of each scope, in addition to the week and the document type. The area scopes count only
# working groups, because the pseudo-group of the individual submissions has the GEN area as parent.
SCOPES: dict[str, dict[str, str]] = {
    "all": {},
    "group_type:wg": {"doc__group__type": "wg"},
    "group_type:individ": {"doc__group__type": "individ"},
    "group_type:rg": {"doc__group__type": "rg"},
    **{f"area:{area}": {"doc__group__parent__acronym": area, "doc__group__type": "wg"} for area in AREAS},
}
NEW_SCOPE_WEEKS = 8
BACKFILL_WEEKS = 104
# Scopes that may fail in a row before the collector assumes that the Datatracker is unavailable
MAX_CONSECUTIVE_FAILURES = 2


class DeadlineReached(Exception):
    """The run deadline passed before all weeks of a scope were fetched."""


def last_complete_week(today: date) -> date:
    """Returns the Monday of the last ISO week that ended before ``today``.

    Args:
        today: The run day.

    Returns:
        date: The Monday; on a Monday it is the Monday of the previous week.
    """
    return today - timedelta(days=today.weekday() + 7)


def mondays(first: date, last: date) -> list[date]:
    """Returns the Mondays from ``first`` to ``last`` (both Mondays, inclusive) in chronological order."""
    weeks = []
    while first <= last:
        weeks.append(first)
        first += timedelta(weeks=1)
    return weeks


def count_url(scope: str, monday: date, new_only: bool) -> str:
    """Builds the API URL that counts the draft versions of a scope submitted in one week.

    Args:
        scope: Key of SCOPES.
        monday: Monday of the ISO week.
        new_only: If True, only revision 00 (new drafts) is counted.

    Returns:
        str: The URL; ``meta.total_count`` of the response is the count.
    """
    params = {"time__gte": f"{monday.isoformat()}T00:00:00",
              "time__lt": f"{(monday + timedelta(weeks=1)).isoformat()}T00:00:00",
              "doc__type": "draft", **SCOPES[scope]}
    if new_only:
        params["rev"] = "00"
    params.update({"limit": "1", "format": "json"})
    return f"{EVENTS_URL}?{urlencode(params, safe=':')}"


def scope_url(scope: str) -> str:
    """Returns the API URL of the draft versions of a scope, used as ``source_url``."""
    return f"{EVENTS_URL}?{urlencode({'doc__type': 'draft', **SCOPES[scope]})}"


def fetch_count(ctx: RunContext, url: str) -> int:
    """Reads the number of events that match a query.

    Args:
        ctx: The run context.
        url: A URL from ``count_url``.

    Returns:
        int: ``meta.total_count`` of the response.

    Raises:
        FetchError: If the request fails.
        ValueError: If the response has no valid ``total_count``.
    """
    data = ctx.web.get(url, respect_robots=False).json()
    meta = data.get("meta") if isinstance(data, dict) else None
    total = meta.get("total_count") if isinstance(meta, dict) else None
    if isinstance(total, bool) or not isinstance(total, int):
        raise ValueError(f"Datatracker response without total_count: {url}")
    return total


@dataclass(frozen=True)
class WeekPlan:
    """Weeks of a scope that are stored in one record.

    Attributes:
        weeks (list[date]): Mondays in the order in which they are fetched.
        forward (bool): True for the weeks after the last stored week, False for
            older weeks of a backfill run, which are fetched newest first.
    """
    weeks: list[date]
    forward: bool


def _seen_week(previous: Optional[SeenEntry], key: str) -> Optional[date]:
    """Returns a week stored in the seen data of a scope."""
    value = previous.data.get(key) if previous else None
    return date.fromisoformat(value) if isinstance(value, str) else None


def week_plans(previous: Optional[SeenEntry], last_week: date, backfill: bool) -> list[WeekPlan]:
    """Returns the weeks to fetch for a scope.

    Args:
        previous: Seen entry of the scope; its data holds ``first_week`` and ``last_week``.
        last_week: Monday of the last complete week.
        backfill: Whether the run imports the history.

    Returns:
        list[WeekPlan]: The weeks after the last stored week (or the initial weeks of a new
        scope), and in a backfill run the older weeks before the first stored week.
    """
    history_start = last_week - timedelta(weeks=(BACKFILL_WEEKS if backfill else NEW_SCOPE_WEEKS) - 1)
    first = _seen_week(previous, "first_week")
    last = _seen_week(previous, "last_week")
    plans = []
    forward = mondays(last + timedelta(weeks=1) if last else history_start, last_week)
    if forward:
        plans.append(WeekPlan(forward, forward=True))
    if backfill and first is not None and first > history_start:
        plans.append(WeekPlan(list(reversed(mondays(history_start, first - timedelta(weeks=1)))), forward=False))
    return plans


class IetfDraftsCollector(Collector):
    """Writes weekly counts of new drafts and draft revisions per scope."""
    name = "ietf_drafts"
    datasets = (DATASET,)
    min_days_between_runs = 27

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Fetches the complete weeks since the last stored week for every scope and emits one record per scope.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; the progress per scope is kept in the seen store.
        """
        seen = ctx.seen(DATASET)
        last_week = last_complete_week(ctx.today)
        counters: dict[str, Any] = {"scopes": len(SCOPES), "records": 0, "weeks": 0, "requests": 0, "failed": 0}
        complete = True
        error: Optional[str] = None
        consecutive_failures = 0
        for position, scope in enumerate(SCOPES):
            stop: Optional[Exception] = None
            for plan in week_plans(seen.get(scope), last_week, ctx.backfill):
                fetched, stop = self._fetch_weeks(ctx, scope, plan.weeks, counters)
                if fetched:
                    self._emit(ctx, entry, seen, scope, plan, fetched)
                    counters["records"] += 1
                    counters["weeks"] += len(fetched)
                if stop is not None:
                    break
            if stop is None:
                consecutive_failures = 0
                continue
            complete = False
            if isinstance(stop, DeadlineReached):
                logger.info("Run deadline reached; %d scopes follow in the next run.", len(SCOPES) - position)
                counters["deferred"] = len(SCOPES) - position
                break
            if isinstance(stop, RateLimitError):
                logger.warning("Datatracker rate limit reached (%s); the remaining weeks follow in the next run.",
                               stop)
                counters["deferred"] = len(SCOPES) - position
                break
            counters["failed"] += 1
            consecutive_failures += 1
            logger.warning("Datatracker counts of scope %s failed: %s", scope, stop)
            if isinstance(stop, BlockedError) or consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                error = f"Datatracker failed: {stop}"
                logger.error("Datatracker requests stopped after scope %s: %s", scope, stop)
                counters["deferred"] = len(SCOPES) - position - 1
                break
        logger.info("IETF drafts: %d records with %d weeks for %d scopes.", counters["records"], counters["weeks"],
                    len(SCOPES))
        return CollectorResult(counters=counters, complete=complete, error=error)

    @staticmethod
    def _fetch_weeks(ctx: RunContext, scope: str, weeks: list[date],
                     counters: dict[str, Any]) -> tuple[dict[date, tuple[int, int]], Optional[Exception]]:
        """Fetches the counts of the weeks of a scope in the given order.

        Args:
            ctx: The run context.
            scope: Key of SCOPES.
            weeks: Mondays to fetch.
            counters: Counters of the run; ``requests`` is incremented.

        Returns:
            tuple: ``(revisions, new drafts)`` by Monday for the weeks fetched completely, and the
            reason why the fetch stopped early (DeadlineReached, FetchError or ValueError), or None.
        """
        fetched: dict[date, tuple[int, int]] = {}
        for monday in weeks:
            if ctx.past_deadline():
                return fetched, DeadlineReached()
            try:
                counters["requests"] += 1
                revisions = fetch_count(ctx, count_url(scope, monday, new_only=False))
                counters["requests"] += 1
                new_drafts = fetch_count(ctx, count_url(scope, monday, new_only=True))
            except (FetchError, ValueError) as e:
                return fetched, e
            fetched[monday] = (revisions, new_drafts)
        return fetched, None

    @staticmethod
    def _emit(ctx: RunContext, entry: CollectorEntry, seen: SeenStore, scope: str, plan: WeekPlan,
              fetched: dict[date, tuple[int, int]]) -> None:
        """Writes the record of the fetched weeks of a scope and stores the new week range in the seen store.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            seen: The seen store of the dataset.
            scope: Key of SCOPES.
            plan: The plan the weeks belong to.
            fetched: ``(revisions, new drafts)`` by Monday; contiguous with the stored weeks.
        """
        weeks = sorted(fetched)
        first, last = weeks[0], weeks[-1]
        record = IetfDraftActivityRecord(
            **ctx.envelope(DATASET, f"ietf-drafts:{scope}:{first.isoformat()}:{last.isoformat()}", revision=0,
                           source_id=SOURCE_ID, source_url=scope_url(scope), license=LICENSE,
                           backfilled=is_backfilled(first, entry.first_run)),
            scope=scope,
            weekly_new_drafts={week: fetched[week][1] for week in weeks},
            weekly_revisions={week: fetched[week][0] for week in weeks},
        )
        previous = seen.get(scope)
        data = dict(previous.data) if previous else {}
        if plan.forward:
            data["last_week"] = last.isoformat()
            data.setdefault("first_week", first.isoformat())
        else:
            data["first_week"] = first.isoformat()
        # Each record covers new weeks, so it is a new item with revision 0
        seen.mark(scope, scope, ctx.today, data=data)
        ctx.emit(DATASET, [record])

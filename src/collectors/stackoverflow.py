"""
Collects the weekly number of new Stack Overflow questions per tag of the tracked technologies.

For every Stack Overflow tag in the registry (``stackoverflow_tags``) the
collector counts the questions created in each complete ISO week (Monday
00:00:00 to Sunday 23:59:59 UTC) with the ``/questions`` method of the Stack
Exchange API. A filter created for this project reduces the response to the
total and the quota fields, so that one request gives one weekly count.
Deleted questions are not counted. The current number of questions per tag
comes from ``/tags/{tags}/info`` (up to 100 tags per request). A tag that is
unknown or a synonym of another tag is missing from that response; its total
is null and a warning names the tag, so that the registry can be corrected.

Each run fetches the weeks since the last stored week of a tag, and only
weeks that ended before the run day. A new tag gets the last 4 weeks in a
normal run and the last 104 weeks in a backfill run. The daily quota of the
API (300 requests per IP address without a key, 10,000 with a key), a long
backoff or a throttling error can end a run early. The stored progress
advances only for the weeks fetched, so that the next run continues with the
remaining weeks.

The collector runs once a month (a monthly record holds the same weeks as
four weekly records with less overhead); a run that stops at the quota stays
due and continues in the next run. It uses only the API, because the robots.txt
files of stackoverflow.com and stackexchange.com disallow automated access to
the websites. The API terms require naming the Stack Exchange Network as the
source of the data. The question content is licensed under CC BY-SA 4.0; only
counts are stored.
"""
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlencode

from collectors.base import Collector, CollectorResult, RunContext, batches, series_start
from registry import CONFIG_PATH, active_technologies
from schema import StackOverflowTagRecord
from state import CollectorEntry, SeenEntry, SeenStore, content_hash, is_backfilled
from web import BlockedError, FetchError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "stackoverflow_questions"
SOURCE_ID = "stackexchange_api"
LICENSE = "Stack Exchange API (counts; content CC BY-SA 4.0)"
API_URL = "https://api.stackexchange.com/2.3"
QUESTIONS_URL = API_URL + "/questions"
TAGS_INFO_URL = API_URL + "/tags/{tags}/info"
SITE = "stackoverflow"
# Filter created for this project: the response contains only total, quota_max, quota_remaining,
# backoff (when present) and the error fields
TOTAL_FILTER = "!9n30I5fDgPX4"
# Weeks fetched for a new tag in a normal run and in a backfill run
NEW_TAG_WEEKS = 4
BACKFILL_WEEKS = 104
# Tags per tags/info request (maximum of the API)
TAGS_PER_REQUEST = 100
# The collector stops when fewer requests remain in the daily quota
MIN_QUOTA_REMAINING = 10
# Longest backoff in seconds that the collector waits for; a longer backoff ends the run
MAX_BACKOFF_S = 30
# Tags that fail one after another with network, server or format errors before the collector stops
MAX_CONSECUTIVE_FAILURES = 3
# Records are written in batches, so that an interrupted run keeps its progress
EMIT_BATCH_SIZE = 100


@dataclass(frozen=True)
class Stop:
    """Reason why the collector ended before all tags were collected.

    Attributes:
        reason (str): deadline, quota, backoff, api_error, rate_limit, blocked or failures; written to the run log.
        error (Optional[str]): Message for stops that need attention; None for quota, throttling and deadline stops.
    """
    reason: str
    error: Optional[str] = None


@dataclass(frozen=True)
class TagWork:
    """The weeks to fetch for a tag in this run.

    Attributes:
        technology_id (str): Id of the technology in the registry.
        tag (str): Stack Overflow tag.
        weeks (tuple[date, ...]): Mondays of the weeks to fetch, oldest first.
    """
    technology_id: str
    tag: str
    weeks: tuple[date, ...]


def _int(value: Any) -> Optional[int]:
    """Returns an integer value, or None for missing values, booleans and other types."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def last_complete_week(today: date) -> date:
    """Returns the Monday of the last ISO week that ended before a day.

    Args:
        today: The run day (UTC).

    Returns:
        date: The Monday of the week before the week of ``today``.
    """
    return today - timedelta(days=today.weekday() + 7)


def week_bounds(monday: date) -> tuple[int, int]:
    """Returns the first and the last second of an ISO week (UTC) as Unix times.

    Args:
        monday: The Monday of the week.

    Returns:
        tuple[int, int]: Monday 00:00:00 and Sunday 23:59:59 UTC.
    """
    start = datetime.combine(monday, time.min, tzinfo=timezone.utc)
    return int(start.timestamp()), int((start + timedelta(days=7)).timestamp()) - 1


def weeks_to_fetch(previous: Optional[SeenEntry], today: date, backfill: bool) -> list[date]:
    """Returns the weeks to fetch for a tag: the complete weeks after the last stored week.

    Args:
        previous: Seen entry of the tag; ``data["last_week"]`` holds the Monday of the last stored week.
        today: The run day (UTC).
        backfill: True in a backfill run, which gives a new tag BACKFILL_WEEKS weeks instead of NEW_TAG_WEEKS.

    Returns:
        list[date]: Mondays of the weeks, oldest first; empty if no week ended since the last stored week.
    """
    last = last_complete_week(today)
    history_start = last - timedelta(weeks=(BACKFILL_WEEKS if backfill else NEW_TAG_WEEKS) - 1)
    start = series_start(previous, history_start, key="last_week")
    # series_start gives the day after the last stored Monday; the next week starts on the following Monday
    first = start + timedelta(days=(7 - start.weekday()) % 7)
    return [first + timedelta(weeks=week) for week in range((last - first).days // 7 + 1)]


def tags_info_url(tags: Sequence[str]) -> str:
    """Builds the tags/info URL of several tags.

    Args:
        tags: The tags; characters such as # and + are percent-encoded, semicolons separate the tags.

    Returns:
        str: The URL without query parameters.
    """
    return TAGS_INFO_URL.format(tags=";".join(quote(tag, safe="") for tag in tags))


def questions_params(tag: str, monday: date) -> dict[str, Any]:
    """Returns the query parameters that count the questions of a tag created in one week.

    Args:
        tag: The tag.
        monday: The Monday of the week.

    Returns:
        dict: Parameters of the /questions method, without the key.
    """
    start, end = week_bounds(monday)
    return {"tagged": tag, "fromdate": start, "todate": end, "site": SITE, "filter": TOTAL_FILTER}


def source_url(tag: str, first: date, last: date) -> str:
    """Builds an API URL that returns the number of questions of a tag in the weeks of a record.

    Args:
        tag: The tag.
        first: Monday of the first week.
        last: Monday of the last week.

    Returns:
        str: The URL, without the key.
    """
    params = {**questions_params(tag, first), "todate": week_bounds(last)[1]}
    return f"{QUESTIONS_URL}?{urlencode(params)}"


class StackExchangeApi:
    """Sends requests to the Stack Exchange API and keeps track of the quota and the backoff.

    Attributes:
        quota_remaining (Optional[int]): Requests left in the daily quota, from the latest response.
        excessive_backoff (Optional[int]): A requested backoff in seconds that is longer than MAX_BACKOFF_S.
    """

    def __init__(self, ctx: RunContext) -> None:
        """Initializes the client.

        Args:
            ctx: The run context; its settings provide the optional key.
        """
        self._web = ctx.web
        self._key = ctx.settings.stackexchange_key
        self._wait_s = 0
        self.quota_remaining: Optional[int] = None
        self.excessive_backoff: Optional[int] = None

    def get(self, url: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Sends a GET request, after the backoff requested by the previous response.

        Args:
            url: The API URL.
            params: Query parameters without the key.

        Returns:
            dict: The parsed response.

        Raises:
            RateLimitError: For HTTP 429.
            BlockedError: If the API refuses the request (HTTP 401 or 403).
            FetchError: For other failures; API errors such as throttle violations come as HTTP 400.
            ValueError: If the response is not a JSON object.
        """
        if self._wait_s:
            self._web.sleep(self._wait_s)
            self._wait_s = 0
        query = dict(params)
        if self._key:
            # Only sent to the Stack Exchange API, as a parameter, so that error messages do not contain it
            query["key"] = self._key
        data = self._web.get(url, params=query, respect_robots=False).json()
        if not isinstance(data, dict):
            raise ValueError("response is not a JSON object")
        quota = _int(data.get("quota_remaining"))
        if quota is not None:
            self.quota_remaining = quota
        backoff = _int(data.get("backoff"))
        if backoff is not None and backoff > MAX_BACKOFF_S:
            logger.warning("The Stack Exchange API requests a backoff of %d s; the collector stops.", backoff)
            self.excessive_backoff = backoff
        elif backoff is not None and backoff > 0:
            logger.info("The Stack Exchange API requests a backoff of %d s before the next request.", backoff)
            self._wait_s = backoff
        return data


def fetch_tag_totals(api: StackExchangeApi, tags: Sequence[str]) -> dict[str, int]:
    """Fetches the number of questions of up to TAGS_PER_REQUEST tags.

    Args:
        api: The API client.
        tags: The tags.

    Returns:
        dict[str, int]: Questions per tag name as returned by the API; unknown tags are missing and
        synonyms appear under the name of their master tag.
    """
    data = api.get(tags_info_url(tags), {"site": SITE, "pagesize": TAGS_PER_REQUEST})
    totals: dict[str, int] = {}
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        name, count = item.get("name"), _int(item.get("count"))
        if isinstance(name, str) and count is not None:
            totals[name] = count
    return totals


def fetch_week_count(api: StackExchangeApi, tag: str, monday: date) -> int:
    """Fetches the number of questions of a tag created in one week.

    Args:
        api: The API client.
        tag: The tag.
        monday: The Monday of the week.

    Returns:
        int: The number of questions.

    Raises:
        ValueError: If the response has no total.
    """
    total = _int(api.get(QUESTIONS_URL, questions_params(tag, monday)).get("total"))
    if total is None or total < 0:
        raise ValueError(f"response without a total for {tag} in the week of {monday.isoformat()}")
    return total


def limit_stop(ctx: RunContext, api: StackExchangeApi, failures_in_a_row: int = 0) -> Optional[Stop]:
    """Tells whether the collector must stop before the next request.

    Args:
        ctx: The run context.
        api: The API client with the latest quota and backoff.
        failures_in_a_row: Tags that failed one after another.

    Returns:
        Optional[Stop]: The reason to stop, or None to continue.
    """
    if ctx.past_deadline():
        return Stop("deadline")
    if api.quota_remaining is not None and api.quota_remaining < MIN_QUOTA_REMAINING:
        return Stop("quota")
    if api.excessive_backoff is not None:
        return Stop("backoff")
    if failures_in_a_row >= MAX_CONSECUTIVE_FAILURES:
        return Stop("failures", f"{failures_in_a_row} tags in a row failed; the collector stopped")
    return None


def refusal_stop(error: Exception) -> Optional[Stop]:
    """Returns the stop for a failure that ends the collection.

    Args:
        error: The failure of a request or of the parsing of a response.

    Returns:
        Optional[Stop]: A stop for API errors, rate limits and refused requests; None for other failures.
    """
    if isinstance(error, RateLimitError):
        return Stop("rate_limit")
    if isinstance(error, BlockedError):
        # Refused requests are not repeated (fetch rules)
        return Stop("blocked", f"the Stack Exchange API refused a request ({error.reason}); the collector stopped")
    if isinstance(error, FetchError) and error.status == 400:
        # The API answers its errors with HTTP 400, e.g. throttle_violation (error_id 502) and an exhausted quota
        logger.warning("The Stack Exchange API answered HTTP 400 (an API error such as a throttle violation).")
        return Stop("api_error")
    return None


class StackOverflowCollector(Collector):
    """Writes the new weekly question counts of the Stack Overflow tags of the tracked technologies."""
    name = "stackoverflow"
    datasets = (DATASET,)
    # Monthly; the source keeps the history, so the weeks since the last run are fetched together
    min_days_between_runs = 27

    def __init__(self, registry_path: Path = CONFIG_PATH) -> None:
        """Initializes the collector.

        Args:
            registry_path: The registry file.
        """
        self._registry_path = registry_path

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Fetches the weeks since the last stored week for every tag and emits one record per tag with new weeks.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; incomplete if the collector stopped early. The progress per tag
            is kept in the seen store.
        """
        seen = ctx.seen(DATASET)
        tags = [(technology.id, tag) for technology in active_technologies(self._registry_path)
                for tag in technology.stackoverflow_tags]
        work = [TagWork(technology_id, tag, tuple(weeks)) for technology_id, tag in tags
                if (weeks := weeks_to_fetch(seen.get(tag), ctx.today, ctx.backfill))]
        counters: dict[str, Any] = {"tags": len(tags), "records": 0, "weeks": 0, "failed": 0, "missing_totals": 0}
        api = StackExchangeApi(ctx)
        totals, stop = self._fetch_totals(ctx, api, [item.tag for item in work], counters)
        records: list[StackOverflowTagRecord] = []
        failures_in_a_row = 0
        for position, item in enumerate(work):
            stop = stop or limit_stop(ctx, api, failures_in_a_row)
            if stop is not None:
                counters["deferred"] = len(work) - position
                break
            counts: dict[date, int] = {}
            failure: Optional[Exception] = None
            # Weeks are fetched in order and the first failure ends the tag, so that the stored weeks have no gaps
            for monday in item.weeks:
                stop = limit_stop(ctx, api, failures_in_a_row)
                if stop is not None:
                    break
                try:
                    counts[monday] = fetch_week_count(api, item.tag, monday)
                except (FetchError, ValueError) as e:
                    stop = refusal_stop(e)
                    failure = e if stop is None else None
                    break
            if counts:
                records.append(self._record(ctx, entry, item, counts, totals.get(item.tag)))
                counters["weeks"] += len(counts)
            if failure is not None:
                logger.warning("Stack Overflow weekly counts of %s failed: %s", item.tag, failure)
                counters["failed"] += 1
                failures_in_a_row += 1
            elif stop is None:
                failures_in_a_row = 0
            if len(records) >= EMIT_BATCH_SIZE:
                counters["records"] += self._emit(ctx, seen, records)
                records = []
            if stop is not None:
                counters["deferred"] = len(work) - position
                break
        counters["records"] += self._emit(ctx, seen, records)
        if api.quota_remaining is not None:
            counters["quota_remaining"] = api.quota_remaining
        return self._result(counters, len(work), stop)

    @staticmethod
    def _fetch_totals(ctx: RunContext, api: StackExchangeApi, tags: Sequence[str],
                      counters: dict[str, Any]) -> tuple[dict[str, int], Optional[Stop]]:
        """Fetches the number of questions of the tags in batches of TAGS_PER_REQUEST.

        Args:
            ctx: The run context.
            api: The API client.
            tags: The tags with new weeks in this run.
            counters: The counters of the run; ``missing_totals`` is updated.

        Returns:
            tuple: Questions per tag, and the reason if the collector has to stop.
        """
        totals: dict[str, int] = {}
        for batch in batches(tags, TAGS_PER_REQUEST):
            stop = limit_stop(ctx, api)
            if stop is not None:
                return totals, stop
            try:
                found = fetch_tag_totals(api, batch)
            except (FetchError, ValueError) as e:
                stop = refusal_stop(e)
                if stop is not None:
                    return totals, stop
                logger.warning("Stack Overflow tag totals of %d tags failed: %s", len(batch), e)
                continue
            totals.update(found)
            for tag in batch:
                if tag not in found:
                    logger.warning("Stack Overflow tag %s is missing from the tags/info response: it is unknown or "
                                   "a synonym of another tag. Its total is null.", tag)
                    counters["missing_totals"] += 1
        return totals, None

    @staticmethod
    def _record(ctx: RunContext, entry: CollectorEntry, item: TagWork, counts: Mapping[date, int],
                total: Optional[int]) -> StackOverflowTagRecord:
        """Builds the record of the new weeks of a tag.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            item: The tag.
            counts: Questions per week (Monday).
            total: Questions with the tag, if known.

        Returns:
            StackOverflowTagRecord: The record; each record covers new weeks, so its revision is 0.
        """
        first, last = min(counts), max(counts)
        return StackOverflowTagRecord(
            **ctx.envelope(
                DATASET, f"stackoverflow:{item.tag}:{first.isoformat()}:{last.isoformat()}", revision=0,
                source_id=SOURCE_ID, source_url=source_url(item.tag, first, last), license=LICENSE,
                backfilled=is_backfilled(first, entry.first_run),
            ),
            technology_id=item.technology_id,
            tag=item.tag,
            weekly_questions=dict(sorted(counts.items())),
            total_questions=total,
        )

    @staticmethod
    def _emit(ctx: RunContext, seen: SeenStore, records: Sequence[StackOverflowTagRecord]) -> int:
        """Advances the stored progress of the tags of the records and writes the records.

        The progress is updated right before the records are written, so that it never covers weeks
        whose records were lost.

        Args:
            ctx: The run context.
            seen: The seen store of the dataset.
            records: The records.

        Returns:
            int: The number of records.
        """
        for record in records:
            seen.mark(record.tag, content_hash({"technology_id": record.technology_id}), ctx.today,
                      data={"last_week": max(record.weekly_questions).isoformat()})
        ctx.emit(DATASET, records)
        return len(records)

    @staticmethod
    def _result(counters: dict[str, Any], tags_due: int, stop: Optional[Stop]) -> CollectorResult:
        """Logs the outcome and builds the result.

        Args:
            counters: The counters of the run.
            tags_due: Tags with weeks to fetch in this run.
            stop: The reason why the collector stopped early, if it did.

        Returns:
            CollectorResult: Counters, completeness and error.
        """
        logger.info("Stack Overflow: %d records with %d weeks for %d tags; %d failed.", counters["records"],
                    counters["weeks"], counters["tags"], counters["failed"])
        error = None
        if stop is not None:
            counters["stopped"] = stop.reason
            error = stop.error
            logger.warning("Stack Overflow: stopped (%s) with %d tags left; %s requests left in the daily quota. "
                           "The next run continues.", stop.reason, counters.get("deferred", 0),
                           counters.get("quota_remaining", "unknown"))
        attempted = tags_due - counters.get("deferred", 0)
        failed = counters["failed"]
        if error is None and failed and failed == attempted:
            error = f"{failed} of {tags_due} tags failed"
        return CollectorResult(counters=counters, complete=stop is None, error=error)

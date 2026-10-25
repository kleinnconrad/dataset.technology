"""
Collects Hacker News stories with at least 100 points and weekly counts of the
stories and comments that mention the terms of the tracked technologies.

Source: the HN Search API by Algolia (https://hn.algolia.com/api), a documented
API without key that allows 10,000 requests per hour and IP address. Only
metadata is stored: no user names, story texts or comment texts.

Stories (``hackernews_stories``): a story collects most of its points within
two days, so each run fetches the stories with at least 100 points that were
created between the cursor and 48 hours before the start of the run. A query
returns at most 1,000 hits; a window with more hits is split in halves. The
first run starts 7 days before the end of its window (365 days in a backfill
run). A seen store keyed by the story id prevents duplicates; its entries are
kept for 30 days. The technologies of a story are the active registry
technologies whose Hacker News terms occur in the title as whole words or
phrases (``term_pattern``).

Mentions (``hackernews_mentions``): for every term of an active technology, the
number of stories with the term in the title and of comments that contain it,
per complete ISO week (Monday 00:00 to Sunday 23:59:59 UTC, keyed by the
Monday) since the last stored week. A new term gets 4 weeks of history (52
weeks in a backfill run). The search runs without typo tolerance and prefix
matching, and only in the title or the comment text, so that only whole words
count; terms with more than one word or with punctuation are searched as quoted
phrases (``search_query``). Terms with + or # or a leading dot are not counted
(``is_countable``). A count that the service reports as not exhaustive is
replaced by the sum of its days, and such a day by the sum of its 6-hour
windows; a week that stays inexact is listed in ``approximate_weeks``.

A normal run sends at most MAX_MENTION_REQUESTS_PER_RUN count requests; a
backfill run has no such budget, only the run deadline. The terms that were
never counted come first, then those whose oldest pending week is the
earliest; the remaining terms follow in the next runs. Reaching the budget is
normal operation and does not make the run incomplete.

Schedule: every run (twice a week). The stories are collected first.
"""
import logging
import re
import string
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, NamedTuple, Optional
from urllib.parse import urlencode, urlsplit

from collectors.base import Collector, CollectorResult, RunContext, series_start
from registry import CONFIG_PATH, Technology, active_technologies
from schema import HackerNewsMentionRecord, HackerNewsStoryRecord
from state import CollectorEntry, SeenStore, content_hash, is_backfilled
from web import BlockedError, FetchError, RateLimitError

logger = logging.getLogger(__name__)

DATASET_STORIES = "hackernews_stories"
DATASET_MENTIONS = "hackernews_mentions"
SOURCE_ID = "hn_algolia"
LICENSE = "Hacker News content (Y Combinator) via the HN Search API by Algolia; metadata only"
API_URL = "https://hn.algolia.com/api/v1"
SEARCH_URL = f"{API_URL}/search"
SEARCH_BY_DATE_URL = f"{API_URL}/search_by_date"
ITEM_URL = "https://news.ycombinator.com/item?id={story_id}"

# Stories
MIN_POINTS = 100
# Stories are fetched when they are at least this old, so that their points have settled
MATURITY_S = 48 * 3600
# Length of the first window: days before its end (the cursor of later runs replaces it)
FIRST_RUN_DAYS = 7
BACKFILL_DAYS = 365
# The API returns at most this many hits per query; it is also the largest page size
MAX_HITS = 1000
# Only these attributes are requested and highlighting is switched off (an empty list), so that no story
# texts are transferred. User names arrive only as author_ tags in _tags, of which only the type tags are read.
STORY_ATTRIBUTES = "objectID,title,url,points,num_comments,created_at_i,_tags"
NO_HIGHLIGHTING = "[]"
# Tags that mark the type of a post, in the order in which they are checked
STORY_TYPE_TAGS = ("launch_hn", "show_hn", "ask_hn")
STORY_SEEN_RETENTION_DAYS = 30
CURSOR_KEY = "stories_until"
STORY_ID_PATTERN = re.compile(r"[0-9]+")

# Mentions
NEW_TERM_WEEKS = 4
BACKFILL_TERM_WEEKS = 52
MAX_MENTION_REQUESTS_PER_RUN = 1500
# Requests for one week of a term without splitting: the stories and the comments
REQUESTS_PER_WEEK = 2
CountKind = Literal["stories", "comments"]
# Searched tag and attribute per kind of count
COUNT_KINDS: dict[str, tuple[str, str]] = {"stories": ("story", "title"), "comments": ("comment", "comment_text")}
# Windows into which a count that is not exhaustive is split: days, then 6-hour windows
SPLIT_SECONDS = (86_400, 21_600)
# Separators between the words of a term: spaces, hyphens and slashes. Dots belong to the words,
# as in the search service, which finds "node.js" but not "node js" for the query "node.js".
TERM_SEPARATORS = re.compile(r"[\s/-]+")
SINGLE_WORD_PATTERN = re.compile(r"[^\W_]+")
# Characters that a term keeps in its slug (see term_slug)
SLUG_CHARACTERS = frozenset(string.ascii_lowercase + string.digits + ".-")

# The collector stops after this many failed requests in a row, e.g. when the service is down
MAX_CONSECUTIVE_FAILURES = 3
# Records are written in batches, so that an interrupted run keeps its progress
EMIT_BATCH_SIZE = 200


def _is_int(value: Any) -> bool:
    """Tells whether a JSON value is an integer and not a boolean."""
    return isinstance(value, int) and not isinstance(value, bool)


def _iso(unix: int) -> str:
    """Formats a Unix time as an ISO 8601 UTC timestamp."""
    return datetime.fromtimestamp(unix, timezone.utc).isoformat()


def unix_time(day: date) -> int:
    """Returns the Unix time of 00:00 UTC of a day.

    Args:
        day: The day.

    Returns:
        int: Seconds since 1970-01-01 00:00 UTC.
    """
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def last_complete_monday(today: date) -> date:
    """Returns the Monday of the latest ISO week that ended before a day.

    Args:
        today: Day of the run.

    Returns:
        date: The Monday of the week before the week of ``today``.
    """
    return today - timedelta(days=today.weekday() + 7)


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


def term_pattern(term: str) -> re.Pattern[str]:
    """Compiles the pattern that finds a Hacker News term in a title as a whole word or phrase.

    Letters match in any case. Spaces, hyphens and slashes between the words of
    the term match any run of these characters ("open source" also finds
    "open-source"); dots and the symbols + and # are part of a word. A match must
    not follow a word character, +, # or a dot and must not be followed by a word
    character, + or #, so that "c" is not found in "c++" and "rust" not in
    "trust". A term that ends in + or # may be followed by a digit ("C++20").

    Args:
        term: The term in lower case.

    Returns:
        re.Pattern: The compiled pattern.

    Raises:
        ValueError: If the term consists of separators only.
    """
    words = [word for word in TERM_SEPARATORS.split(term) if word]
    if not words:
        raise ValueError(f"term {term!r} contains no word")
    body = TERM_SEPARATORS.pattern.join(re.escape(word) for word in words)
    end = r"(?![\w+#])" if words[-1][-1].isalnum() else r"(?![^\W\d]|[+#])"
    return re.compile(r"(?<![\w+#.])" + body + end, re.IGNORECASE)


def title_patterns(technologies: Iterable[Technology]) -> list[tuple[str, re.Pattern[str]]]:
    """Compiles the title patterns of the Hacker News terms of technologies.

    Args:
        technologies: The active technologies.

    Returns:
        list[tuple[str, re.Pattern]]: ``(technology id, pattern)`` per term; a term
        without a word is logged and left out.
    """
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for technology in technologies:
        for term in technology.hn_terms:
            try:
                patterns.append((technology.id, term_pattern(term)))
            except ValueError as e:
                logger.warning("Hacker News term of %s ignored: %s", technology.id, e)
    return patterns


def matching_technologies(title: str, patterns: Sequence[tuple[str, re.Pattern[str]]]) -> list[str]:
    """Returns the technologies whose terms occur in a title.

    Args:
        title: The story title.
        patterns: Patterns from ``title_patterns``.

    Returns:
        list[str]: Sorted technology ids without duplicates.
    """
    return sorted({technology_id for technology_id, pattern in patterns if pattern.search(title)})


def is_countable(term: str) -> bool:
    """Tells whether the mentions of a term are counted in the search service.

    Terms that start with a dot (.net) are not counted, because the search
    service also finds host names such as example.net for them. Terms with +
    or # (c++, c#, f#) are counted: samples of the hits on 2026-10-25 contained
    the term in all titles. Terms with dots or slashes between words (node.js,
    http/3) are counted as quoted phrases.

    Args:
        term: The term in lower case.

    Returns:
        bool: True if the term is counted.
    """
    return not term.startswith(".")


def search_query(term: str) -> str:
    """Returns the search query of a term.

    Args:
        term: The term in lower case.

    Returns:
        str: A single word as it is (``rust``); any other term as a quoted phrase
        (``"large language model"``, ``"node.js"``).
    """
    return term if SINGLE_WORD_PATTERN.fullmatch(term) else f'"{term}"'


def term_slug(term: str) -> str:
    """Returns a term as written in record ids.

    The encoding is reversible, so that two terms never share a slug ("fine
    tuning" and "fine-tuning" are different terms): lower-case letters, digits,
    dots and hyphens are kept, a space becomes an underscore, and every other
    character is percent-encoded as UTF-8, the underscore included.

    Args:
        term: The term in lower case.

    Returns:
        str: The slug, e.g. ``large_language_model``, ``node.js`` or ``http%2F3``.
    """
    return "".join(
        char if char in SLUG_CHARACTERS else "_" if char == " " else "".join(f"%{byte:02X}" for byte in char.encode())
        for char in term
    )


def mention_key(technology_id: str, term: str) -> str:
    """Returns the key of a term in the seen store of the mentions.

    Args:
        technology_id: Id of the technology.
        term: The term.

    Returns:
        str: ``<technology_id>:<term>``.
    """
    return f"{technology_id}:{term}"


def stories_url(start: int, end: int) -> str:
    """Builds the URL of the stories with at least MIN_POINTS points created in a window.

    Args:
        start: First second of the window (Unix time).
        end: First second after the window (Unix time).

    Returns:
        str: The ``search_by_date`` URL.
    """
    return f"{SEARCH_BY_DATE_URL}?" + urlencode({
        "tags": "story",
        "numericFilters": f"created_at_i>={start},created_at_i<{end},points>={MIN_POINTS}",
        "hitsPerPage": MAX_HITS,
        "attributesToRetrieve": STORY_ATTRIBUTES,
        "attributesToHighlight": NO_HIGHLIGHTING,
    })


def count_url(term: str, kind: CountKind, start: int, end: int) -> str:
    """Builds the URL that counts the stories or comments with a term created in a window.

    Args:
        term: The term in lower case.
        kind: ``stories`` (term in the title) or ``comments`` (term in the comment text).
        start: First second of the window (Unix time).
        end: First second after the window (Unix time).

    Returns:
        str: The ``search`` URL; it requests no hits, only their number.
    """
    tag, attribute = COUNT_KINDS[kind]
    return f"{SEARCH_URL}?" + urlencode({
        "query": search_query(term),
        "tags": tag,
        "restrictSearchableAttributes": attribute,
        "typoTolerance": "false",
        "queryType": "prefixNone",
        "numericFilters": f"created_at_i>={start},created_at_i<{end}",
        "hitsPerPage": 0,
    })


def fetch_json(ctx: RunContext, url: str) -> dict[str, Any]:
    """Fetches a URL of the API and parses the JSON object it returns.

    Args:
        ctx: The run context.
        url: The URL.

    Returns:
        dict: The parsed object.

    Raises:
        FetchError: If the request fails (including the subclasses raised by
            ``WebClient.get``) or the body is not a JSON object.
    """
    response = ctx.web.get(url, respect_robots=False)
    try:
        data = response.json()
    except ValueError as e:
        raise FetchError(url, "response is not valid JSON") from e
    if not isinstance(data, dict):
        raise FetchError(url, "response is not a JSON object")
    return data


def parse_count(data: Mapping[str, Any], url: str) -> tuple[int, bool]:
    """Reads the number of hits from a count response.

    Args:
        data: The parsed response.
        url: The requested URL, for error messages.

    Returns:
        tuple[int, bool]: The number of hits, and False if the service reports the
        number as not exhaustive (``exhaustive.nbHits`` or ``exhaustiveNbHits`` false).

    Raises:
        FetchError: If the response has no number of hits.
    """
    hits = data.get("nbHits")
    if not _is_int(hits) or hits < 0:
        raise FetchError(url, "response without nbHits")
    nested = data.get("exhaustive")
    inexact = (isinstance(nested, dict) and nested.get("nbHits") is False) or data.get("exhaustiveNbHits") is False
    return hits, not inexact


def parse_story_hits(data: Mapping[str, Any], url: str) -> tuple[list[dict[str, Any]], int]:
    """Reads the hits and their total number from a stories response.

    Args:
        data: The parsed response.
        url: The requested URL, for error messages.

    Returns:
        tuple[list[dict], int]: The hits of the page and the number of hits of the query.

    Raises:
        FetchError: If the response has no list of hits or no number of hits.
    """
    hits = data.get("hits")
    total = data.get("nbHits")
    if not isinstance(hits, list) or not _is_int(total):
        raise FetchError(url, "response without hits")
    return [hit for hit in hits if isinstance(hit, dict)], total


def domain_of(url: Optional[str]) -> Optional[str]:
    """Returns the host of a URL without a leading ``www.``.

    Args:
        url: The URL.

    Returns:
        Optional[str]: The host in lower case, or None if the URL has no valid host.
    """
    if not url:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host


def story_type_of(tags: Any) -> str:
    """Returns the type of a post from its tags.

    Args:
        tags: The ``_tags`` list of a hit.

    Returns:
        str: launch_hn, show_hn, ask_hn or story.
    """
    tag_set = {tag for tag in tags if isinstance(tag, str)} if isinstance(tags, list) else set()
    return next((tag for tag in STORY_TYPE_TAGS if tag in tag_set), "story")


def story_fields(hit: Mapping[str, Any], patterns: Sequence[tuple[str, re.Pattern[str]]]) -> Optional[dict[str, Any]]:
    """Returns the dataset fields of a story hit.

    Only the id, title, URL, points, number of comments, creation time and tags
    are read; the author and the story text are not used.

    Args:
        hit: A hit of a stories response.
        patterns: Title patterns from ``title_patterns``.

    Returns:
        Optional[dict]: The fields of a ``HackerNewsStoryRecord`` without the common
        fields, or None if the hit lacks a required field or has fewer than MIN_POINTS points.
    """
    story_id = str(hit.get("objectID", "")).strip()
    title = hit.get("title")
    created = hit.get("created_at_i")
    points = hit.get("points")
    if not (STORY_ID_PATTERN.fullmatch(story_id) and isinstance(title, str) and title.strip()
            and _is_int(created) and _is_int(points) and points >= MIN_POINTS):
        return None
    try:
        created_at = datetime.fromtimestamp(created, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    url = hit.get("url")
    url = url.strip() if isinstance(url, str) and url.strip() else None
    comments = hit.get("num_comments")
    title = title.strip()
    return {
        "story_id": story_id,
        "created_at": created_at,
        "title": title,
        "url": url,
        "domain": domain_of(url),
        "story_type": story_type_of(hit.get("_tags")),
        "points": points,
        "comments": comments if _is_int(comments) else 0,
        "technology_ids": matching_technologies(title, patterns),
    }


def stories_window_end(ctx: RunContext) -> int:
    """Returns the end of the stories window of a run: 48 hours before its start.

    Args:
        ctx: The run context.

    Returns:
        int: Unix time.
    """
    return int(ctx.started.timestamp()) - MATURITY_S


class _PendingTerm(NamedTuple):
    """A term with weeks to count.

    The tuples sort in the order in which the terms are visited: terms that were
    never counted first, then by the oldest pending week, the technology id and the term.
    """
    counted_before: bool
    first_week: date
    technology_id: str
    term: str


@dataclass
class _Progress:
    """State of one run of the collector.

    Attributes:
        counters (dict): Counters for the run log.
        stories_until (int): Unix time up to which all stories were processed (the cursor).
        stopped (bool): True if the run sends no further requests.
        incomplete (bool): True if the run deadline or the rate limit stopped the run.
        error (Optional[str]): Error for the run log.
        successes (int): Successful requests.
        consecutive_failures (int): Failed requests since the last successful one.
    """
    counters: dict[str, Any]
    stories_until: int
    stopped: bool = False
    incomplete: bool = False
    error: Optional[str] = None
    successes: int = 0
    consecutive_failures: int = 0

    def succeeded(self) -> None:
        """Records a successful request."""
        self.successes += 1
        self.consecutive_failures = 0

    def stop_incomplete(self) -> None:
        """Stops the run at the deadline or the rate limit; the rest follows in the next run."""
        self.stopped = True
        self.incomplete = True


class HackerNewsCollector(Collector):
    """Writes Hacker News stories with at least 100 points and weekly mention counts of the technology terms."""
    name = "hackernews"
    datasets = (DATASET_STORIES, DATASET_MENTIONS)
    min_days_between_runs = 0

    def __init__(self, registry_path: Path = CONFIG_PATH) -> None:
        """Initializes the collector.

        Args:
            registry_path: The registry file.
        """
        self._registry_path = registry_path

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Collects the settled stories since the cursor, then the pending weekly mention counts.

        Args:
            ctx: The run context.
            entry: Run history of this collector; its cursor holds the end of the last stories window.

        Returns:
            CollectorResult: The new cursor and counters; incomplete if the run deadline
            or the rate limit stopped the collector.
        """
        technologies = [t for t in active_technologies(self._registry_path) if t.hn_terms]
        counters: dict[str, Any] = {
            "stories": 0, "story_requests": 0, "story_windows_split": 0, "stories_seen": 0, "stories_invalid": 0,
            "terms": 0, "terms_skipped": 0, "terms_pending": 0, "terms_counted": 0, "terms_deferred": 0,
            "mention_records": 0, "mention_weeks": 0, "approximate_weeks": 0, "count_requests": 0, "failed": 0,
        }
        progress = _Progress(counters, self._stories_start(ctx, entry))
        self._collect_stories(ctx, entry, title_patterns(technologies), progress)
        self._collect_mentions(ctx, entry, technologies, progress)
        counters["stories_until"] = _iso(progress.stories_until)
        error = progress.error
        if error is None and counters["failed"] and not progress.successes:
            error = f"all {counters['failed']} attempted requests failed"
        logger.info("Hacker News: %d stories up to %s; %d mention records for %d terms (%d deferred, %d not "
                    "countable).", counters["stories"], counters["stories_until"], counters["mention_records"],
                    counters["terms_counted"], counters["terms_deferred"], counters["terms_skipped"])
        return CollectorResult(cursor={CURSOR_KEY: progress.stories_until}, counters=counters, error=error,
                               complete=not progress.incomplete)

    # -- stories ----------------------------------------------------------------

    @staticmethod
    def _stories_start(ctx: RunContext, entry: CollectorEntry) -> int:
        """Returns the start of the stories window.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            int: The cursor, or on the first run FIRST_RUN_DAYS (BACKFILL_DAYS in a
            backfill run) before the end of the window (Unix time).
        """
        value = entry.cursor.get(CURSOR_KEY)
        if _is_int(value):
            return value
        if value is not None:
            logger.warning("Invalid Hacker News cursor %r ignored.", value)
        days = BACKFILL_DAYS if ctx.backfill else FIRST_RUN_DAYS
        return stories_window_end(ctx) - days * 86_400

    def _collect_stories(self, ctx: RunContext, entry: CollectorEntry,
                         patterns: Sequence[tuple[str, re.Pattern[str]]], progress: _Progress) -> None:
        """Emits the stories created between the cursor and the window end and advances the cursor.

        Windows are processed in chronological order, so that the cursor marks the
        end of the stories already processed. A failed window ends the stories part;
        it is fetched again in the next run.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            patterns: Title patterns of the technology terms.
            progress: State of the run; ``stories_until`` is advanced.
        """
        counters = progress.counters
        seen = ctx.seen(DATASET_STORIES)
        seen.prune(ctx.today, STORY_SEEN_RETENTION_DAYS)
        end = stories_window_end(ctx)
        # A stack whose last element is the earliest window
        windows = [(progress.stories_until, end)] if progress.stories_until < end else []
        records: list[HackerNewsStoryRecord] = []
        try:
            while windows and not progress.stopped:
                if ctx.past_deadline():
                    progress.stop_incomplete()
                    break
                start, stop = windows.pop()
                url = stories_url(start, stop)
                counters["story_requests"] += 1
                try:
                    hits, total = parse_story_hits(fetch_json(ctx, url), url)
                except FetchError as e:
                    self._handle_failure(progress, e, f"Stories created from {_iso(start)} to {_iso(stop)}")
                    break
                progress.succeeded()
                if (total > MAX_HITS or len(hits) >= MAX_HITS) and stop - start > 1:
                    middle = (start + stop) // 2
                    windows.extend([(middle, stop), (start, middle)])
                    counters["story_windows_split"] += 1
                    continue
                for hit in hits:
                    record = self._story_record(ctx, entry, hit, patterns, seen, counters)
                    if record is not None:
                        records.append(record)
                progress.stories_until = stop
                if len(records) >= EMIT_BATCH_SIZE:
                    ctx.emit(DATASET_STORIES, records)
                    counters["stories"] += len(records)
                    records = []
        finally:
            ctx.emit(DATASET_STORIES, records)
            counters["stories"] += len(records)

    @staticmethod
    def _story_record(ctx: RunContext, entry: CollectorEntry, hit: Mapping[str, Any],
                      patterns: Sequence[tuple[str, re.Pattern[str]]], seen: SeenStore,
                      counters: dict[str, Any]) -> Optional[HackerNewsStoryRecord]:
        """Builds the record of a story hit that is not yet in the seen store and marks it as seen.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            hit: A hit of a stories response.
            patterns: Title patterns of the technology terms.
            seen: Seen store of the stories.
            counters: Counters of the run.

        Returns:
            Optional[HackerNewsStoryRecord]: The record, or None for an invalid or already stored story.
        """
        fields = story_fields(hit, patterns)
        if fields is None:
            counters["stories_invalid"] += 1
            return None
        story_id = fields["story_id"]
        if story_id in seen:
            counters["stories_seen"] += 1
            return None
        record = HackerNewsStoryRecord(
            **ctx.envelope(
                DATASET_STORIES, f"hn-story:{story_id}", revision=0, source_id=SOURCE_ID,
                source_url=ITEM_URL.format(story_id=story_id), license=LICENSE,
                backfilled=is_backfilled(fields["created_at"].date(), entry.first_run),
            ),
            **fields,
        )
        seen.mark(story_id, content_hash(fields), ctx.today)
        return record

    # -- mentions ---------------------------------------------------------------

    def _collect_mentions(self, ctx: RunContext, entry: CollectorEntry, technologies: Sequence[Technology],
                          progress: _Progress) -> None:
        """Counts the pending weeks of the terms in the order of ``_PendingTerm``, within the request budget.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            technologies: The active technologies with Hacker News terms.
            progress: State of the run.
        """
        counters = progress.counters
        seen = ctx.seen(DATASET_MENTIONS)
        last_week = last_complete_monday(ctx.today)
        history_weeks = BACKFILL_TERM_WEEKS if ctx.backfill else NEW_TERM_WEEKS
        history_start = last_week - timedelta(weeks=history_weeks - 1)
        pending: list[_PendingTerm] = []
        for technology in technologies:
            for term in technology.hn_terms:
                counters["terms"] += 1
                if not is_countable(term):
                    logger.debug("Mentions of %r (%s) are not counted: the term contains + or # or starts with "
                                 "a dot.", term, technology.id)
                    counters["terms_skipped"] += 1
                    continue
                previous = seen.get(mention_key(technology.id, term))
                first_week = next_monday(series_start(previous, history_start, key="last_week"))
                if first_week <= last_week:
                    pending.append(_PendingTerm(previous is not None, first_week, technology.id, term))
        pending.sort()
        counters["terms_pending"] = len(pending)
        budget = None if ctx.backfill else MAX_MENTION_REQUESTS_PER_RUN
        records: list[HackerNewsMentionRecord] = []
        visited = 0
        try:
            for item in pending:
                if progress.stopped:
                    break
                if ctx.past_deadline():
                    progress.stop_incomplete()
                    break
                weeks = weeks_between(item.first_week, last_week)
                # The first term of a run is always visited, so that a term whose pending weeks need more
                # requests than the whole budget is still counted
                if budget is not None and visited and counters["count_requests"] + REQUESTS_PER_WEEK * len(weeks) > budget:
                    break
                visited += 1
                record = self._count_term(ctx, entry, item, weeks, seen, progress)
                if record is not None:
                    records.append(record)
                if len(records) >= EMIT_BATCH_SIZE:
                    ctx.emit(DATASET_MENTIONS, records)
                    counters["mention_records"] += len(records)
                    records = []
        finally:
            ctx.emit(DATASET_MENTIONS, records)
            counters["mention_records"] += len(records)
        counters["terms_deferred"] = len(pending) - visited

    def _count_term(self, ctx: RunContext, entry: CollectorEntry, item: _PendingTerm, weeks: Sequence[date],
                    seen: SeenStore, progress: _Progress) -> Optional[HackerNewsMentionRecord]:
        """Counts the stories and comments with a term per week, until a week fails or the run stops.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            item: The term.
            weeks: Mondays of the pending weeks, in chronological order.
            seen: Seen store of the mentions; the last counted week is stored in it.
            progress: State of the run.

        Returns:
            Optional[HackerNewsMentionRecord]: The record of the counted weeks, or None if no week was counted.
        """
        stories: dict[date, int] = {}
        comments: dict[date, int] = {}
        approximate: list[date] = []
        for monday in weeks:
            if ctx.past_deadline():
                progress.stop_incomplete()
                break
            start, end = unix_time(monday), unix_time(monday + timedelta(weeks=1))
            try:
                story_count, stories_exact = self._count(ctx, item.term, "stories", start, end, progress)
                comment_count, comments_exact = self._count(ctx, item.term, "comments", start, end, progress)
            except FetchError as e:
                self._handle_failure(progress, e, f"Mentions of {item.term!r} in the week of {monday.isoformat()}")
                break
            stories[monday] = story_count
            comments[monday] = comment_count
            if not (stories_exact and comments_exact):
                approximate.append(monday)
        if not stories:
            return None
        first, last = min(stories), max(stories)
        record = HackerNewsMentionRecord(
            **ctx.envelope(
                DATASET_MENTIONS, f"hn-mentions:{item.technology_id}:{term_slug(item.term)}:{first}:{last}",
                revision=0, source_id=SOURCE_ID,
                source_url=count_url(item.term, "stories", unix_time(first), unix_time(last + timedelta(weeks=1))),
                license=LICENSE, backfilled=is_backfilled(first, entry.first_run),
            ),
            technology_id=item.technology_id,
            term=item.term,
            weekly_stories=stories,
            weekly_comments=comments,
            approximate_weeks=approximate,
        )
        # Each record covers new weeks, so it is a new item with revision 0
        seen.mark(mention_key(item.technology_id, item.term), search_query(item.term), ctx.today,
                  data={"last_week": last.isoformat()})
        progress.counters["terms_counted"] += 1
        progress.counters["mention_weeks"] += len(stories)
        progress.counters["approximate_weeks"] += len(approximate)
        return record

    def _count(self, ctx: RunContext, term: str, kind: CountKind, start: int, end: int, progress: _Progress,
               level: int = 0) -> tuple[int, bool]:
        """Counts the stories or comments with a term created in a window.

        A count that the service reports as not exhaustive is replaced by the sum
        of the counts of its days, and the count of such a day by the sum of its
        6-hour windows (SPLIT_SECONDS).

        Args:
            ctx: The run context.
            term: The term.
            kind: ``stories`` or ``comments``.
            start: First second of the window (Unix time).
            end: First second after the window (Unix time).
            progress: State of the run.
            level: Number of splits that led to this window.

        Returns:
            tuple[int, bool]: The count, and False if a part stayed not exhaustive.

        Raises:
            FetchError: If a request fails.
        """
        url = count_url(term, kind, start, end)
        progress.counters["count_requests"] += 1
        hits, exhaustive = parse_count(fetch_json(ctx, url), url)
        progress.succeeded()
        if exhaustive or level >= len(SPLIT_SECONDS):
            return hits, exhaustive
        step = SPLIT_SECONDS[level]
        total, exact = 0, True
        for part_start in range(start, end, step):
            part_hits, part_exact = self._count(ctx, term, kind, part_start, min(part_start + step, end), progress,
                                                level + 1)
            total += part_hits
            exact = exact and part_exact
        return total, exact

    # -- failures ---------------------------------------------------------------

    @staticmethod
    def _handle_failure(progress: _Progress, error: FetchError, what: str) -> None:
        """Records a failed request and decides whether the run stops.

        A rate limit stops the run as incomplete. A refusal (HTTP 401/403 or a
        challenge) stops it with an error, so that the host is not requested again.
        Other failures are counted; MAX_CONSECUTIVE_FAILURES in a row stop the run
        with an error, because the service is then likely unavailable.

        Args:
            progress: State of the run.
            error: The failure.
            what: Description of the request for the log.
        """
        if isinstance(error, RateLimitError):
            logger.warning("%s stopped at the rate limit of the HN Search API: %s", what, error)
            progress.stop_incomplete()
        elif isinstance(error, BlockedError):
            logger.error("%s refused by the HN Search API: %s", what, error)
            progress.stopped = True
            progress.error = f"HN Search API refused the requests: {error}"
        else:
            logger.warning("%s failed: %s", what, error)
            progress.counters["failed"] += 1
            progress.consecutive_failures += 1
            if progress.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                progress.stopped = True
                progress.error = f"stopped after {progress.consecutive_failures} failed requests in a row: {error}"

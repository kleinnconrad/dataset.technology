"""
Collects popularity and activity measures of the GitHub repositories of the tracked technologies.

For every repository in the registry (``github_repos``) the collector reads
four resources of the GitHub REST API (API version 2022-11-28):
- the repository (``/repos/{owner}/{repo}``): stars, forks, open issues and
  pull requests, watchers (subscribers), size, language, license, topics,
  creation and push times and the archived flag. GitHub redirects a renamed
  or transferred repository to its current name, which is stored as
  ``full_name`` next to the permanent numeric ``repo_id``;
- the contributors (``/contributors?per_page=1&anon=true``): the number of
  contributors including anonymous ones is the page number of the ``last``
  link. GitHub does not list the contributors of repositories whose history
  is too large for the API (HTTP 403), which gives null;
- the commits per week of the last 52 weeks without merge commits
  (``/stats/participation``). GitHub computes these statistics on request and
  answers HTTP 202 until they are ready; such repositories are requested once
  more at the end of the run, and get null if the statistics are still missing;
- the latest release (``/releases/latest``); HTTP 404 means that the
  repository publishes no releases on GitHub.

GitHub keeps no history of these values, so each run writes one snapshot
record per repository. Only counts and metadata of the repositories are
stored, no data of users or contributors (GitHub terms of service and
acceptable use policies).

The collector runs once a week. With the GITHUB_TOKEN of the workflow the API
allows 1,000 requests per hour, without a token 60 requests per hour and IP
address. The collector stops when fewer than 20 requests remain, at the run
deadline, or when GitHub refuses a request; the next run collects all
repositories again.
"""
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlsplit

from collectors.base import Collector, CollectorResult, RunContext
from registry import CONFIG_PATH, active_technologies
from schema import GitHubRepositoryRecord
from state import CollectorEntry
from web import BlockedError, FetchError, NotFoundError, RateLimitError, Response

logger = logging.getLogger(__name__)

DATASET = "github_repositories"
SOURCE_ID = "github_rest_api"
LICENSE = "GitHub REST API (GitHub terms of service)"
API_URL = "https://api.github.com"
REPO_URL = API_URL + "/repos/{repo}"
CONTRIBUTORS_URL = API_URL + "/repos/{repo}/contributors"
PARTICIPATION_URL = API_URL + "/repos/{repo}/stats/participation"
LATEST_RELEASE_URL = API_URL + "/repos/{repo}/releases/latest"
MEDIA_TYPE = "application/vnd.github+json"
API_VERSION = "2022-11-28"
# The collector stops when fewer requests remain in the current rate-limit window
MIN_RATE_LIMIT_REMAINING = 20
# Number of weeks at the end of the participation statistics that are summed for commits_last_4_weeks
RECENT_WEEKS = 4
# Minimum time between the first HTTP 202 answer for statistics and the second request at the end of the run
STATS_RETRY_DELAY_S = 60.0
# Repositories that fail one after another with network, server or format errors before the collector stops
MAX_CONSECUTIVE_FAILURES = 3
# Records are written in batches, so that an interrupted run keeps its progress
EMIT_BATCH_SIZE = 100
# One entry of a Link header, e.g. <https://api.github.com/...&page=2>; rel="next"
LINK_PATTERN = re.compile(r'<([^>]*)>\s*;\s*rel="([^"]*)"')


class StatisticsPending(Exception):
    """GitHub is still computing the statistics of a repository (HTTP 202)."""


@dataclass(frozen=True)
class Stop:
    """Reason why the collector ended before all repositories were collected.

    Attributes:
        reason (str): deadline, rate_limit, blocked or failures; written to the run log.
        error (Optional[str]): Message for stops that need attention; None for deadline and rate-limit stops.
    """
    reason: str
    error: Optional[str] = None


@dataclass(frozen=True)
class RepositoryInfo:
    """The values of the repository resource.

    Attributes:
        repo_id (Optional[int]): Numeric repository id.
        full_name (str): Current owner/name.
        stars (int): Number of stars.
        forks (int): Number of forks.
        open_issues (int): Number of open issues and pull requests.
        watchers (int): Number of subscribers.
        size_kb (Optional[int]): Size in kilobytes.
        primary_language (Optional[str]): Main language.
        license_spdx (Optional[str]): SPDX id of the license; NOASSERTION if GitHub cannot determine it.
        topics (tuple[str, ...]): Topics.
        created_at (Optional[datetime]): Creation time (UTC).
        pushed_at (Optional[datetime]): Time of the latest push (UTC).
        archived (bool): Whether the repository is archived.
    """
    repo_id: Optional[int]
    full_name: str
    stars: int
    forks: int
    open_issues: int
    watchers: int
    size_kb: Optional[int]
    primary_language: Optional[str]
    license_spdx: Optional[str]
    topics: tuple[str, ...]
    created_at: Optional[datetime]
    pushed_at: Optional[datetime]
    archived: bool


@dataclass
class Snapshot:
    """The values of one repository collected in a run.

    Attributes:
        technology_id (str): Id of the technology in the registry.
        repo (str): Repository as listed in the registry.
        info (RepositoryInfo): Values of the repository resource.
        contributors (Optional[int]): Number of contributors; None if GitHub does not list them.
        weekly_commits (Optional[list[int]]): Commits per week of the last 52 weeks, oldest first;
            None if the statistics are not available.
        statistics_pending (bool): True while GitHub is computing the statistics.
        release_tag (Optional[str]): Tag of the latest release.
        release_published_at (Optional[datetime]): Publication time of the latest release.
    """
    technology_id: str
    repo: str
    info: RepositoryInfo
    contributors: Optional[int]
    weekly_commits: Optional[list[int]]
    statistics_pending: bool
    release_tag: Optional[str]
    release_published_at: Optional[datetime]


def _int(value: Any) -> Optional[int]:
    """Returns an integer value, or None for missing values, booleans and other types."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _required_int(data: Mapping[str, Any], key: str) -> int:
    """Returns an integer field of a response.

    Raises:
        ValueError: If the field is missing or not an integer.
    """
    value = _int(data.get(key))
    if value is None:
        raise ValueError(f"repository response without {key}")
    return value


def _text(value: Any) -> Optional[str]:
    """Returns a non-empty string, or None."""
    return value if isinstance(value, str) and value else None


def parse_datetime(value: Any) -> Optional[datetime]:
    """Parses a time of the API such as ``2017-02-10T19:23:51Z``.

    Args:
        value: The value from the response.

    Returns:
        Optional[datetime]: The time in UTC, or None if the value is missing or invalid.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_repository(data: Any) -> RepositoryInfo:
    """Reads the values of a repository resource.

    Args:
        data: The parsed JSON of ``/repos/{owner}/{repo}``.

    Returns:
        RepositoryInfo: The values.

    Raises:
        ValueError: If the response is not an object, or the name or a count is missing.
    """
    if not isinstance(data, dict):
        raise ValueError("repository response is not an object")
    full_name = data.get("full_name")
    if not isinstance(full_name, str) or "/" not in full_name:
        raise ValueError("repository response without full_name")
    license_info = data.get("license")
    topics = data.get("topics")
    return RepositoryInfo(
        repo_id=_int(data.get("id")),
        full_name=full_name,
        stars=_required_int(data, "stargazers_count"),
        forks=_required_int(data, "forks_count"),
        open_issues=_required_int(data, "open_issues_count"),
        # watchers_count equals the number of stars; subscribers_count is the number of watchers
        watchers=_required_int(data, "subscribers_count"),
        size_kb=_int(data.get("size")),
        primary_language=_text(data.get("language")),
        license_spdx=_text(license_info.get("spdx_id")) if isinstance(license_info, dict) else None,
        topics=tuple(topic for topic in topics if isinstance(topic, str)) if isinstance(topics, list) else (),
        created_at=parse_datetime(data.get("created_at")),
        pushed_at=parse_datetime(data.get("pushed_at")),
        archived=data.get("archived") is True,
    )


def last_page_number(link_header: str) -> Optional[int]:
    """Returns the page number of the link with ``rel="last"`` in a Link header.

    Args:
        link_header: Value of the Link header; may be empty.

    Returns:
        Optional[int]: The number of the last page, or None if the header has no such link.
    """
    for url, relations in LINK_PATTERN.findall(link_header):
        if "last" in relations.split():
            pages = parse_qs(urlsplit(url).query).get("page", [])
            if pages and pages[0].isdigit():
                return int(pages[0])
    return None


def _repo_path(repo: str) -> str:
    """Returns owner/name encoded for a URL path."""
    return quote(repo, safe="/")


class GitHubApi:
    """Sends requests to the GitHub REST API and keeps track of the rate limit.

    Attributes:
        remaining (Optional[int]): Requests left in the current rate-limit window, from the latest response.
        reset_at (Optional[datetime]): Start of the next rate-limit window.
    """

    def __init__(self, ctx: RunContext) -> None:
        """Initializes the client.

        Args:
            ctx: The run context; its settings provide the optional token.
        """
        self._web = ctx.web
        self._headers = {"Accept": MEDIA_TYPE, "X-GitHub-Api-Version": API_VERSION}
        if ctx.settings.github_token:
            # Sent only with requests to api.github.com; httpx drops it on redirects to other hosts
            self._headers["Authorization"] = f"Bearer {ctx.settings.github_token}"
        self.remaining: Optional[int] = None
        self.reset_at: Optional[datetime] = None

    @property
    def exhausted(self) -> bool:
        """True if fewer than MIN_RATE_LIMIT_REMAINING requests remain."""
        return self.remaining is not None and self.remaining < MIN_RATE_LIMIT_REMAINING

    def get(self, url: str, params: Optional[Mapping[str, Any]] = None) -> Response:
        """Sends a GET request and reads the rate-limit headers of the response.

        Args:
            url: The API URL.
            params: Query parameters.

        Returns:
            Response: The successful response, including HTTP 202 and 204.

        Raises:
            RateLimitError: If the rate limit is exhausted.
            BlockedError: If GitHub refuses the request (HTTP 401 or 403 without rate-limit headers).
            NotFoundError: For HTTP 404.
            FetchError: For other failures.
        """
        response = self._web.get(url, params=params, headers=self._headers, respect_robots=False)
        remaining = response.headers.get("x-ratelimit-remaining", "").strip()
        if remaining.isdigit():
            self.remaining = int(remaining)
        reset = response.headers.get("x-ratelimit-reset", "").strip()
        if reset.isdigit():
            self.reset_at = datetime.fromtimestamp(int(reset), timezone.utc)
        return response


def fetch_repository(api: GitHubApi, repo: str) -> RepositoryInfo:
    """Fetches the repository resource; a renamed repository is reached through the redirect of GitHub.

    Args:
        api: The API client.
        repo: owner/name as listed in the registry.

    Returns:
        RepositoryInfo: The values.

    Raises:
        NotFoundError: If the repository does not exist or is not public.
        ValueError: If the response lacks required values.
    """
    return parse_repository(api.get(REPO_URL.format(repo=_repo_path(repo))).json())


def fetch_contributor_count(api: GitHubApi, full_name: str) -> Optional[int]:
    """Fetches the number of contributors including anonymous ones.

    With one contributor per page, the number of the last page is the number of contributors.

    Args:
        api: The API client.
        full_name: Current owner/name.

    Returns:
        Optional[int]: The number; 0 for an empty repository; None if GitHub does not list
        the contributors because the history is too large.

    Raises:
        ValueError: If the response has an unexpected format.
    """
    try:
        response = api.get(CONTRIBUTORS_URL.format(repo=_repo_path(full_name)), params={"per_page": 1, "anon": "true"})
    except BlockedError as e:
        if e.status != 403:
            raise
        # Documented answer for repositories whose history or contributor list is too large for the API
        logger.info("GitHub does not list the contributors of %s (HTTP 403).", full_name)
        return None
    if response.status == 204 or not response.content:
        return 0
    last_page = last_page_number(response.headers.get("link", ""))
    if last_page is not None:
        return last_page
    items = response.json()
    if not isinstance(items, list):
        raise ValueError(f"contributors response of {full_name} is not a list")
    return len(items)


def fetch_weekly_commits(api: GitHubApi, full_name: str) -> Optional[list[int]]:
    """Fetches the commits per week of the last 52 weeks (participation statistics).

    The weeks are rolling: the last one ends at UTC midnight of the request day.

    Args:
        api: The API client.
        full_name: Current owner/name.

    Returns:
        Optional[list[int]]: Commits of all contributors per week, oldest week first;
        None if GitHub has no statistics for the repository (HTTP 204 or 404, or an object without counts).

    Raises:
        StatisticsPending: If GitHub is still computing the statistics (HTTP 202).
        ValueError: If the response has an unexpected format.
    """
    try:
        response = api.get(PARTICIPATION_URL.format(repo=_repo_path(full_name)))
    except NotFoundError:
        return None
    if response.status == 202:
        raise StatisticsPending(full_name)
    if response.status == 204 or not response.content:
        return None
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"participation response of {full_name} is not an object")
    weekly = data.get("all")
    if weekly is None:
        return None
    if not isinstance(weekly, list) or any(_int(count) is None for count in weekly):
        raise ValueError(f"participation response of {full_name} without weekly counts")
    return weekly


def fetch_latest_release(api: GitHubApi, full_name: str) -> tuple[Optional[str], Optional[datetime]]:
    """Fetches the latest release that is neither a draft nor a prerelease.

    Args:
        api: The API client.
        full_name: Current owner/name.

    Returns:
        tuple: Tag and publication time; both None if the repository publishes no releases on GitHub.

    Raises:
        ValueError: If the response is not an object.
    """
    try:
        data = api.get(LATEST_RELEASE_URL.format(repo=_repo_path(full_name))).json()
    except NotFoundError:
        return None, None
    if not isinstance(data, dict):
        raise ValueError(f"release response of {full_name} is not an object")
    return _text(data.get("tag_name")), parse_datetime(data.get("published_at"))


def fetch_snapshot(api: GitHubApi, technology_id: str, repo: str) -> Snapshot:
    """Fetches the four resources of a repository.

    Args:
        api: The API client.
        technology_id: Id of the technology in the registry.
        repo: owner/name as listed in the registry.

    Returns:
        Snapshot: The values; ``statistics_pending`` is True if GitHub answered HTTP 202 for the statistics.

    Raises:
        NotFoundError: If the repository does not exist.
        RateLimitError: If the rate limit is exhausted.
        BlockedError: If GitHub refuses a request.
        FetchError: For other failed requests.
        ValueError: If a response has an unexpected format.
    """
    info = fetch_repository(api, repo)
    # The statistics are requested first, so that GitHub has time to compute missing ones before the second request
    try:
        weekly, pending = fetch_weekly_commits(api, info.full_name), False
    except StatisticsPending:
        weekly, pending = None, True
    contributors = fetch_contributor_count(api, info.full_name)
    release_tag, release_published_at = fetch_latest_release(api, info.full_name)
    return Snapshot(technology_id=technology_id, repo=repo, info=info, contributors=contributors,
                    weekly_commits=weekly, statistics_pending=pending, release_tag=release_tag,
                    release_published_at=release_published_at)


def snapshot_record(ctx: RunContext, snapshot: Snapshot) -> GitHubRepositoryRecord:
    """Builds the record of a repository snapshot.

    Args:
        ctx: The run context.
        snapshot: The collected values.

    Returns:
        GitHubRepositoryRecord: The record, keyed by the repository as listed in the registry and the run day.
    """
    info = snapshot.info
    weekly = snapshot.weekly_commits
    return GitHubRepositoryRecord(
        **ctx.envelope(
            DATASET, f"github:{snapshot.repo}:{ctx.today.isoformat()}", revision=0, source_id=SOURCE_ID,
            source_url=REPO_URL.format(repo=_repo_path(info.full_name)), license=LICENSE, backfilled=False,
        ),
        technology_id=snapshot.technology_id,
        repo=snapshot.repo,
        repo_id=info.repo_id,
        full_name=info.full_name,
        stars=info.stars,
        forks=info.forks,
        open_issues=info.open_issues,
        watchers=info.watchers,
        size_kb=info.size_kb,
        primary_language=info.primary_language,
        repo_license=info.license_spdx,
        topics=list(info.topics),
        created_at=info.created_at,
        pushed_at=info.pushed_at,
        archived=info.archived,
        contributors=snapshot.contributors,
        commits_last_4_weeks=sum(weekly[-RECENT_WEEKS:]) if weekly is not None else None,
        commits_last_52_weeks=sum(weekly) if weekly is not None else None,
        latest_release_tag=snapshot.release_tag,
        latest_release_at=snapshot.release_published_at,
    )


def limit_stop(ctx: RunContext, api: GitHubApi, failures_in_a_row: int = 0) -> Optional[Stop]:
    """Tells whether the collector must stop before the next repository.

    Args:
        ctx: The run context.
        api: The API client with the latest rate-limit values.
        failures_in_a_row: Repositories that failed one after another.

    Returns:
        Optional[Stop]: The reason to stop, or None to continue.
    """
    if ctx.past_deadline():
        return Stop("deadline")
    if api.exhausted:
        return Stop("rate_limit")
    if failures_in_a_row >= MAX_CONSECUTIVE_FAILURES:
        return Stop("failures", f"{failures_in_a_row} repositories in a row failed; the collector stopped")
    return None


def refusal_stop(error: Exception) -> Optional[Stop]:
    """Returns the stop for a failure that ends the collection.

    Args:
        error: The failure of a request or of the parsing of a response.

    Returns:
        Optional[Stop]: A stop for exhausted rate limits and refused requests; None for other failures.
    """
    if isinstance(error, RateLimitError):
        return Stop("rate_limit")
    if isinstance(error, BlockedError):
        # Refused requests are not repeated (fetch rules); a bad token also ends up here (HTTP 401)
        return Stop("blocked", f"GitHub refused a request ({error.reason}); the collector stopped")
    return None


class GitHubCollector(Collector):
    """Writes a snapshot of every GitHub repository of the tracked technologies."""
    name = "github_repositories"
    datasets = (DATASET,)
    min_days_between_runs = 6

    def __init__(self, registry_path: Path = CONFIG_PATH) -> None:
        """Initializes the collector.

        Args:
            registry_path: The registry file.
        """
        self._registry_path = registry_path

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Fetches every repository and emits one snapshot record per repository.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; incomplete if the collector stopped early.
        """
        repos = [(technology.id, repo) for technology in active_technologies(self._registry_path)
                 for repo in technology.github_repos]
        api = GitHubApi(ctx)
        counters: dict[str, Any] = {"repositories": len(repos), "records": 0, "failed": 0, "renamed": 0,
                                    "without_contributors": 0, "statistics_pending": 0}
        records: list[GitHubRepositoryRecord] = []
        pending: list[Snapshot] = []
        pending_since: Optional[datetime] = None
        failures_in_a_row = 0
        stop: Optional[Stop] = None
        for position, (technology_id, repo) in enumerate(repos):
            stop = limit_stop(ctx, api, failures_in_a_row)
            if stop is not None:
                counters["deferred"] = len(repos) - position
                break
            try:
                snapshot = fetch_snapshot(api, technology_id, repo)
            except NotFoundError as e:
                logger.warning("GitHub repository %s of %s not found: %s", repo, technology_id, e)
                counters["failed"] += 1
                failures_in_a_row = 0
                continue
            except (FetchError, ValueError) as e:
                stop = refusal_stop(e)
                if stop is not None:
                    counters["deferred"] = len(repos) - position
                    break
                logger.warning("GitHub repository %s of %s failed: %s", repo, technology_id, e)
                counters["failed"] += 1
                failures_in_a_row += 1
                continue
            failures_in_a_row = 0
            if snapshot.info.full_name.lower() != repo.lower():
                logger.info("GitHub repository %s is now %s (repository id %s).", repo, snapshot.info.full_name,
                            snapshot.info.repo_id)
                counters["renamed"] += 1
            if snapshot.contributors is None:
                counters["without_contributors"] += 1
            if snapshot.statistics_pending:
                pending_since = pending_since or ctx.now()
                pending.append(snapshot)
                continue
            records.append(snapshot_record(ctx, snapshot))
            if len(records) >= EMIT_BATCH_SIZE:
                ctx.emit(DATASET, records)
                counters["records"] += len(records)
                records = []
        if pending:
            if stop is None and pending_since is not None:
                stop = self._request_statistics_again(ctx, api, pending, pending_since)
            # Snapshots whose statistics are still missing are written with null commit counts
            counters["statistics_pending"] = sum(snapshot.statistics_pending for snapshot in pending)
            records.extend(snapshot_record(ctx, snapshot) for snapshot in pending)
        ctx.emit(DATASET, records)
        counters["records"] += len(records)
        if api.remaining is not None:
            counters["rate_limit_remaining"] = api.remaining
        return self._result(api, counters, stop)

    @staticmethod
    def _request_statistics_again(ctx: RunContext, api: GitHubApi, pending: list[Snapshot],
                                  pending_since: datetime) -> Optional[Stop]:
        """Requests the statistics of the repositories for which GitHub answered HTTP 202.

        Args:
            ctx: The run context.
            api: The API client.
            pending: Snapshots without statistics; updated in place.
            pending_since: Time of the first HTTP 202 answer of the run.

        Returns:
            Optional[Stop]: The reason if the collector had to stop, else None.
        """
        stop = limit_stop(ctx, api)
        if stop is not None:
            return stop
        waited_s = (ctx.now() - pending_since).total_seconds()
        if waited_s < STATS_RETRY_DELAY_S:
            ctx.web.sleep(STATS_RETRY_DELAY_S - waited_s)
        for snapshot in pending:
            stop = limit_stop(ctx, api)
            if stop is not None:
                return stop
            try:
                snapshot.weekly_commits = fetch_weekly_commits(api, snapshot.info.full_name)
                snapshot.statistics_pending = False
            except StatisticsPending:
                logger.info("GitHub has not computed the statistics of %s yet; the commit counts stay empty.",
                            snapshot.info.full_name)
            except (FetchError, ValueError) as e:
                stop = refusal_stop(e)
                if stop is not None:
                    return stop
                logger.warning("Statistics of GitHub repository %s failed: %s", snapshot.info.full_name, e)
        return None

    @staticmethod
    def _result(api: GitHubApi, counters: dict[str, Any], stop: Optional[Stop]) -> CollectorResult:
        """Logs the outcome and builds the result.

        Args:
            api: The API client with the latest rate-limit values.
            counters: The counters of the run.
            stop: The reason why the collector stopped early, if it did.

        Returns:
            CollectorResult: Counters, completeness and error.
        """
        logger.info("GitHub: %d records for %d repositories; %d failed, %d renamed.", counters["records"],
                    counters["repositories"], counters["failed"], counters["renamed"])
        error = None
        if stop is not None:
            counters["stopped"] = stop.reason
            error = stop.error
            logger.warning("GitHub: stopped (%s) with %d repositories left; %s requests left in the rate-limit "
                           "window, which resets at %s.", stop.reason, counters.get("deferred", 0), api.remaining,
                           api.reset_at.isoformat() if api.reset_at else "an unknown time")
        attempted = counters["repositories"] - counters.get("deferred", 0)
        failed = counters["failed"]
        if error is None and failed and failed == attempted:
            error = f"{failed} of {counters['repositories']} repositories failed"
        return CollectorResult(counters=counters, complete=stop is None, error=error)

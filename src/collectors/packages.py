"""
Collects the daily downloads of the npm, PyPI and crates.io packages of the tracked technologies.

Sources (documented APIs, called with ``respect_robots=False`` under their terms):
- npm: the download counts API of the public npm registry. The range endpoint
  returns the downloads of one package per UTC day since 2015-01-10. A request
  covers at most 18 months; a longer range is clipped without an error, so the
  collector requests chunks of at most 540 days.
- PyPI: pypistats.org, downloads per UTC day without mirrors. The service keeps
  about 180 days. Package names are case-sensitive in the URL path; the
  registry name is sent in lower case.
- crates.io: the downloads endpoint of a crate returns 90 days, including the
  incomplete current UTC day, split into the five most recently published
  versions (``version_downloads``) and the sum of all other versions
  (``meta.extra_downloads``). The daily total is the sum of both.

Fetch strategy: the dataset is a series. Per package the collector fetches the
days after the last stored day and writes one record with these days per
package and run. PyPI and crates.io days are stored up to the day before the
run. A new npm package gets 365 days (in a backfill run all days since
2015-01-10); a new PyPI package or crate gets the days that the source keeps.

npm gap days: the npm statistics contain outage days on which every package
reports 0, and the latest days are published with a delay of two to three
days. npm days are therefore stored only up to three days before the run, and
a day counts as a gap when more than half of the reference packages of the run
(npm packages with at least 1,000 downloads per day on average) report 0 on it;
with fewer than two reference packages no gaps are detected. Gap days are not
stored. The gap days of the last 60 days are kept in the seen store and
requested again by later runs; such a day is stored once it has a value, so a
record can contain days before the last day of the previous record. All gap
days that have not received data are kept in the cursor of the collector: a
day that cannot be checked in a run, e.g. an older day of a package that is
added to the registry later, counts as a gap if the package reports 0 on it
and an earlier run detected it as a gap. The zeros before the first day with
downloads of a new package are not stored either, because the API reports 0
for the days before a package was published and, for some packages (e.g.
express until 2021-09-30), for parts of their history.

Schedule: once a month (``min_days_between_runs = 27``); the registries keep
at least 90 days (crates.io), 180 days (PyPI) and 18 months (npm). At the run deadline the
collector stops; at a rate limit it stops requesting that registry. The
remaining packages follow in the next run.
"""
import logging
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from collectors.base import Collector, CollectorResult, RunContext, series_start
from registry import CONFIG_PATH, Package, Technology, active_technologies
from schema import PackageDownloadRecord
from state import CollectorEntry, SeenEntry, is_backfilled
from web import BlockedError, FetchError, NotFoundError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "package_downloads"
# Ecosystems of this collector in the order of processing; docker images are collected by docker_hub.py
ECOSYSTEMS = ("npm", "pypi", "crates")
NPM_SOURCE_ID = "npm_downloads"
PYPI_SOURCE_ID = "pypistats"
CRATES_SOURCE_ID = "crates_io"
NPM_LICENSE = "npm public registry API (npm open source terms)"
PYPI_LICENSE = "pypistats.org (PyPI download statistics)"
CRATES_LICENSE = "crates.io API (crates.io data access policy)"
NPM_RANGE_URL = "https://api.npmjs.org/downloads/range/{start}:{end}/{package}"
PYPISTATS_URL = "https://pypistats.org/api/packages/{package}/overall?mirrors=false"
CRATES_DOWNLOADS_URL = "https://crates.io/api/v1/crates/{crate}/downloads"
# First day of the npm download statistics
NPM_HISTORY_START = date(2015, 1, 10)
# Days fetched for a new npm package outside backfill runs
NPM_NEW_PACKAGE_DAYS = 365
# Days per npm request; the API clips ranges longer than 18 months (549 days) without an error
NPM_MAX_REQUEST_DAYS = 540
# npm days are stored up to this many days before the run, because the latest days are published with a delay
NPM_DELAY_DAYS = 3
# Packages with at least this many downloads per day on average are the reference packages of the gap check
NPM_REFERENCE_MIN_AVERAGE = 1_000
# Minimum number of reference packages that must report a day before it can count as a gap
NPM_MIN_REFERENCE_PACKAGES = 2
# Gap days are requested again for this many days after the day and then given up
NPM_GAP_RETRY_DAYS = 60
# Key of the npm gap days in the cursor: days detected as gaps that have not received data since
CURSOR_GAP_DAYS = "npm_gap_days"
# Category of the pypistats rows without mirrors
PYPI_CATEGORY = "without_mirrors"
# Records are written in batches, so that an interrupted run keeps its progress
EMIT_BATCH_SIZE = 100


def _parse_day(value: Any) -> Optional[date]:
    """Parses a ``YYYY-MM-DD`` string; returns None for other values."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _is_count(value: Any) -> bool:
    """Tells whether a value is a non-negative integer (booleans excluded)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _data_day(entry: Optional[SeenEntry], key: str) -> Optional[date]:
    """Returns a day stored in the data of a seen entry, or None."""
    return _parse_day(entry.data.get(key)) if entry else None


def _data_days(entry: Optional[SeenEntry], key: str) -> set[date]:
    """Returns the days of a list stored in the data of a seen entry."""
    values = entry.data.get(key) if entry else None
    if not isinstance(values, list):
        return set()
    return {day for day in (_parse_day(value) for value in values) if day is not None}


# -- npm ----------------------------------------------------------------------

def npm_range_url(package: str, start: date, end: date) -> str:
    """Builds the URL of the npm range endpoint for a package and a day range.

    Args:
        package: Package name; scoped names (``@scope/name``) are used as written.
        start: First day.
        end: Last day.

    Returns:
        str: The URL.
    """
    return NPM_RANGE_URL.format(start=start.isoformat(), end=end.isoformat(), package=quote(package, safe="@/"))


def request_chunks(start: date, end: date, size: int = NPM_MAX_REQUEST_DAYS) -> Iterator[tuple[date, date]]:
    """Splits a day range into consecutive ranges of at most ``size`` days.

    Args:
        start: First day.
        end: Last day.
        size: Maximum number of days per range.

    Yields:
        tuple[date, date]: First and last day of each range.
    """
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=size - 1), end)
        yield chunk_start, chunk_end
        chunk_start = chunk_end + timedelta(days=1)


def parse_npm_range(data: Any, start: date, end: date) -> dict[date, int]:
    """Parses a response of the npm range endpoint.

    Args:
        data: The decoded JSON response.
        start: First requested day; earlier days are ignored.
        end: Last requested day; later days are ignored.

    Returns:
        dict[date, int]: Downloads per day.

    Raises:
        ValueError: If the response contains no list of days.
    """
    items = data.get("downloads") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("npm response without a list of days")
    values: dict[date, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        day = _parse_day(item.get("day"))
        count = item.get("downloads")
        if day is not None and _is_count(count) and start <= day <= end:
            values[day] = count
    return values


def fetch_npm_downloads(ctx: RunContext, package: str, start: date, end: date) -> dict[date, int]:
    """Fetches the daily downloads of an npm package, in chunks of at most NPM_MAX_REQUEST_DAYS days.

    Args:
        ctx: The run context.
        package: Package name.
        start: First day.
        end: Last day.

    Returns:
        dict[date, int]: Downloads per day as reported by the API, including zeros.

    Raises:
        NotFoundError: If the package does not exist.
        FetchError: If a request fails.
        ValueError: If a response cannot be parsed.
    """
    values: dict[date, int] = {}
    for chunk_start, chunk_end in request_chunks(start, end):
        response = ctx.web.get(npm_range_url(package, chunk_start, chunk_end), respect_robots=False)
        values.update(parse_npm_range(response.json(), chunk_start, chunk_end))
    return values


def is_npm_reference(values: Mapping[date, int]) -> bool:
    """Tells whether a package counts as a reference package of the gap check.

    Args:
        values: Downloads per day of the package in this run.

    Returns:
        bool: True if the package has at least NPM_REFERENCE_MIN_AVERAGE downloads per day on average.
    """
    return bool(values) and sum(values.values()) / len(values) >= NPM_REFERENCE_MIN_AVERAGE


def check_npm_days(series: Iterable[Mapping[date, int]]) -> dict[date, bool]:
    """Tells for each day whether it is a gap day of the npm statistics.

    A day is checked if at least NPM_MIN_REFERENCE_PACKAGES reference packages
    report it; it is a gap day if more than half of them report 0.

    Args:
        series: Downloads per day of each npm package fetched in the run.

    Returns:
        dict[date, bool]: True for gap days and False for checked days with data;
        days that could not be checked are missing.
    """
    reported: Counter[date] = Counter()
    zeros: Counter[date] = Counter()
    for values in series:
        if not is_npm_reference(values):
            continue
        for day, count in values.items():
            reported[day] += 1
            if count == 0:
                zeros[day] += 1
    return {day: 2 * zeros[day] > total for day, total in reported.items() if total >= NPM_MIN_REFERENCE_PACKAGES}


def without_leading_zeros(values: Mapping[date, int]) -> dict[date, int]:
    """Removes the days before the first day with downloads.

    Args:
        values: Downloads per day.

    Returns:
        dict[date, int]: The days from the first day with downloads on; empty if there is none.
    """
    with_downloads = [day for day, count in values.items() if count > 0]
    if not with_downloads:
        return {}
    first = min(with_downloads)
    return {day: count for day, count in values.items() if day >= first}


@dataclass(frozen=True)
class NpmSeries:
    """The days of one npm package fetched in a run.

    Attributes:
        technology (Technology): Technology of the package.
        package (Package): The package.
        last_day (Optional[date]): Last day handled by the previous runs; None for a new package.
        retry_days (frozenset[date]): Gap days of previous runs that are requested again.
        values (dict[date, int]): Downloads per fetched day; for a new package
            without the zeros before its first day with downloads.
    """
    technology: Technology
    package: Package
    last_day: Optional[date]
    retry_days: frozenset[date]
    values: dict[date, int]


def split_npm_days(series: NpmSeries, checked: Mapping[date, bool], retry_from: date,
                   known_gaps: AbstractSet[date] = frozenset()) -> tuple[dict[date, int], set[date], Optional[date]]:
    """Splits the fetched days of an npm package into days to store and gap days to request again.

    A day after the last day of the previous runs is stored unless it is a gap
    day. A day that cannot be checked in this run (e.g. an older day of a package
    added to the registry later) counts as a gap if the package reports 0 and an
    earlier run detected the day as a gap. A gap day of a previous run is stored
    once the package reports downloads on it or the day is checked and no longer a gap.

    Args:
        series: The fetched days of the package.
        checked: Result of ``check_npm_days`` for the run.
        retry_from: Gap days before this day are given up.
        known_gaps: Gap days detected by earlier runs that have not received data since.

    Returns:
        tuple: The days to store with their downloads, the gap days to request
        again in later runs, and the new last handled day (None if no day was fetched).
    """
    stored: dict[date, int] = {}
    gaps: set[date] = set()
    new_days: list[date] = []
    for day, count in series.values.items():
        if series.last_day is None or day > series.last_day:
            new_days.append(day)
            is_gap = checked.get(day)
            if is_gap is None:
                is_gap = count == 0 and day in known_gaps
            if is_gap:
                gaps.add(day)
            else:
                stored[day] = count
        elif day in series.retry_days:
            if count > 0 or checked.get(day) is False:
                stored[day] = count
            else:
                gaps.add(day)
    # Gap days that the responses did not cover stay due
    gaps.update(day for day in series.retry_days if day not in series.values)
    last_day = max(new_days) if new_days else series.last_day
    return stored, {day for day in gaps if day >= retry_from}, last_day


# -- PyPI ---------------------------------------------------------------------

def pypistats_url(package: str) -> str:
    """Builds the pypistats.org URL of the daily downloads of a package without mirrors.

    Args:
        package: Package name as written in the registry; it is sent in lower case.

    Returns:
        str: The URL.
    """
    return PYPISTATS_URL.format(package=quote(package.lower(), safe=""))


def parse_pypistats(data: Any) -> dict[date, int]:
    """Parses a response of the pypistats.org overall endpoint.

    Args:
        data: The decoded JSON response.

    Returns:
        dict[date, int]: Downloads without mirrors per day.

    Raises:
        ValueError: If the response contains no list of rows.
    """
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("pypistats response without a list of rows")
    values: dict[date, int] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("category", PYPI_CATEGORY) != PYPI_CATEGORY:
            continue
        day = _parse_day(row.get("date"))
        count = row.get("downloads")
        if day is not None and _is_count(count):
            values[day] = count
    return values


def fetch_pypi_downloads(ctx: RunContext, package: str) -> dict[date, int]:
    """Fetches the daily downloads of a PyPI package that pypistats.org keeps (about 180 days).

    Args:
        ctx: The run context.
        package: Package name as written in the registry.

    Returns:
        dict[date, int]: Downloads without mirrors per day.

    Raises:
        NotFoundError: If pypistats.org does not know the package.
        FetchError: If the request fails.
        ValueError: If the response cannot be parsed.
    """
    return parse_pypistats(ctx.web.get(pypistats_url(package), respect_robots=False).json())


# -- crates.io ----------------------------------------------------------------

def crates_downloads_url(crate: str) -> str:
    """Builds the URL of the downloads endpoint of a crate.

    Args:
        crate: Crate name.

    Returns:
        str: The URL.
    """
    return CRATES_DOWNLOADS_URL.format(crate=quote(crate, safe=""))


def parse_crates_downloads(data: Any) -> dict[date, int]:
    """Parses a response of the crates.io downloads endpoint into daily totals.

    Args:
        data: The decoded JSON response.

    Returns:
        dict[date, int]: Downloads of all versions per day: the five most recently
        published versions (``version_downloads``) plus all other versions
        (``meta.extra_downloads``).

    Raises:
        ValueError: If one of the two lists is missing, so that a total would be incomplete.
    """
    versions = data.get("version_downloads") if isinstance(data, dict) else None
    meta = data.get("meta") if isinstance(data, dict) else None
    extra = meta.get("extra_downloads") if isinstance(meta, dict) else None
    if not isinstance(versions, list) or not isinstance(extra, list):
        raise ValueError("crates.io response without version_downloads and meta.extra_downloads")
    totals: dict[date, int] = {}
    for item in [*versions, *extra]:
        if not isinstance(item, dict):
            continue
        day = _parse_day(item.get("date"))
        count = item.get("downloads")
        if day is not None and _is_count(count):
            totals[day] = totals.get(day, 0) + count
    return totals


def fetch_crates_downloads(ctx: RunContext, crate: str) -> dict[date, int]:
    """Fetches the daily downloads of a crate for the last 90 days.

    Args:
        ctx: The run context.
        crate: Crate name.

    Returns:
        dict[date, int]: Downloads per day; the current UTC day is incomplete.

    Raises:
        NotFoundError: If the crate does not exist.
        FetchError: If the request fails.
        ValueError: If the response cannot be parsed.
    """
    return parse_crates_downloads(ctx.web.get(crates_downloads_url(crate), respect_robots=False).json())


# -- records ------------------------------------------------------------------

def download_record(ctx: RunContext, entry: CollectorEntry, technology: Technology, ecosystem: str, package: str,
                    days: Mapping[date, int]) -> PackageDownloadRecord:
    """Builds the record of the new days of a package.

    Args:
        ctx: The run context.
        entry: Run history of the collector.
        technology: Technology of the package.
        ecosystem: npm, pypi or crates.
        package: Package name as written in the registry.
        days: Downloads per day; must not be empty.

    Returns:
        PackageDownloadRecord: The record.
    """
    first, last = min(days), max(days)
    if ecosystem == "npm":
        source_id, license_text, url = NPM_SOURCE_ID, NPM_LICENSE, npm_range_url(package, first, last)
    elif ecosystem == "pypi":
        source_id, license_text, url = PYPI_SOURCE_ID, PYPI_LICENSE, pypistats_url(package)
    else:
        source_id, license_text, url = CRATES_SOURCE_ID, CRATES_LICENSE, crates_downloads_url(package)
    return PackageDownloadRecord(
        **ctx.envelope(
            DATASET, f"downloads:{ecosystem}:{package}:{first.isoformat()}:{last.isoformat()}",
            revision=0, source_id=source_id, source_url=url, license=license_text,
            backfilled=is_backfilled(first, entry.first_run),
        ),
        technology_id=technology.id,
        ecosystem=ecosystem,
        package=package,
        daily_downloads=dict(sorted(days.items())),
    )


class _Run:
    """State of one run: counters, buffered records, known npm gap days and the registries that stopped answering."""

    def __init__(self, ctx: RunContext, entry: CollectorEntry, packages: int) -> None:
        """Initializes the run state.

        Args:
            ctx: The run context.
            entry: Run history of the collector; its cursor holds the known npm gap days.
            packages: Number of packages to collect.
        """
        self.ctx = ctx
        self.entry = entry
        self.seen = ctx.seen(DATASET)
        self.records: list[PackageDownloadRecord] = []
        self.counters: dict[str, Any] = {
            "packages": packages, "requested": 0, "records": 0, "days": 0, "failed": 0, "deferred": 0,
            "npm_reference_packages": 0, "npm_gap_days": 0, "npm_filled_days": 0,
        }
        stored_gaps = entry.cursor.get(CURSOR_GAP_DAYS)
        self.known_gaps: set[date] = {day for day in map(_parse_day, stored_gaps) if day is not None} \
            if isinstance(stored_gaps, list) else set()
        self.cursor_changed = False
        self.deadline_reached = False
        self.rate_limited: list[str] = []
        self.errors: list[str] = []

    def update_known_gaps(self, checked: Mapping[date, bool]) -> None:
        """Adds the gap days of this run to the known gap days and removes the days that received data.

        Args:
            checked: Result of ``check_npm_days`` for the run.
        """
        updated = (self.known_gaps | {day for day, is_gap in checked.items() if is_gap}) - {
            day for day, is_gap in checked.items() if not is_gap}
        self.cursor_changed = self.cursor_changed or updated != self.known_gaps
        self.known_gaps = updated

    def past_deadline(self, remaining: int) -> bool:
        """Checks the run deadline; at the deadline the remaining packages are deferred.

        Args:
            remaining: Number of packages not yet processed in the current ecosystem.

        Returns:
            bool: True if the collector must stop.
        """
        if not self.deadline_reached and self.ctx.past_deadline():
            logger.warning("Package downloads: run deadline reached; the remaining packages follow in the next run.")
            self.deadline_reached = True
        if self.deadline_reached:
            self.counters["deferred"] += remaining
        return self.deadline_reached

    def fetch(self, package: Package, request: Callable[[], dict[date, int]]) -> tuple[Optional[dict[date, int]], bool]:
        """Sends the request(s) of one package and handles the failures.

        Args:
            package: The package.
            request: Function that fetches the downloads per day.

        Returns:
            tuple: The downloads per day (None on failure) and whether the
            registry must not be requested again in this run (rate limit or block).
        """
        stop = False
        try:
            values = request()
        except RateLimitError as e:
            logger.warning("Package downloads: %s is rate limited (%s); its remaining packages follow in the next "
                           "run.", package.ecosystem, e)
            self.rate_limited.append(package.ecosystem)
            self.counters["deferred"] += 1
            return None, True
        except BlockedError as e:
            logger.error("Package downloads: %s refuses automated requests (%s); it is not requested again in "
                         "this run.", package.ecosystem, e)
            self.errors.append(f"{package.ecosystem} refuses automated requests: {e}")
            stop = True
        except NotFoundError as e:
            logger.warning("Package %s not found: %s", package.key, e)
        except FetchError as e:
            logger.warning("Downloads of %s failed: %s", package.key, e)
        except ValueError as e:
            logger.warning("Downloads of %s could not be parsed: %s", package.key, e)
        else:
            self.counters["requested"] += 1
            return values, False
        self.counters["requested"] += 1
        self.counters["failed"] += 1
        return None, stop

    def store(self, technology: Technology, package: Package, days: Mapping[date, int], data: dict[str, Any]) -> None:
        """Updates the seen entry of a package and buffers the record of its new days.

        Args:
            technology: Technology of the package.
            package: The package.
            days: Downloads per day to store; may be empty if only the seen data changes.
            data: New seen data of the package (last handled day, gap days).
        """
        record = download_record(self.ctx, self.entry, technology, package.ecosystem, package.name, days) if days else None
        self.seen.mark(package.key, technology.id, self.ctx.today, data=data)
        if record is None:
            return
        self.records.append(record)
        self.counters["days"] += len(days)
        if len(self.records) >= EMIT_BATCH_SIZE:
            self.flush()

    def flush(self) -> None:
        """Writes the buffered records."""
        self.ctx.emit(DATASET, self.records)
        self.counters["records"] += len(self.records)
        self.records = []

    def result(self) -> CollectorResult:
        """Writes the remaining records and returns the result of the run.

        Returns:
            CollectorResult: Counters, completeness and an error if a registry
            refused the requests or every request failed.
        """
        self.flush()
        # Seen entries can change without a record (npm gap days), so the store is saved here as well
        self.ctx.save_seen()
        errors = list(self.errors)
        failed, requested = self.counters["failed"], self.counters["requested"]
        if failed and failed == requested:
            errors.append(f"{failed} of {requested} packages failed")
        logger.info("Package downloads: %d records with %d days; %d of %d packages requested, %d failed.",
                    self.counters["records"], self.counters["days"], requested, self.counters["packages"], failed)
        cursor = None
        if self.cursor_changed:
            cursor = {**self.entry.cursor, CURSOR_GAP_DAYS: sorted(day.isoformat() for day in self.known_gaps)}
        return CollectorResult(cursor=cursor, counters=self.counters,
                               complete=not (self.deadline_reached or self.rate_limited),
                               error="; ".join(errors) or None)


class PackageDownloadsCollector(Collector):
    """Writes the new daily downloads of the npm, PyPI and crates.io packages of the tracked technologies."""
    name = "package_downloads"
    datasets = (DATASET,)
    min_days_between_runs = 27

    def __init__(self, registry_path: Path = CONFIG_PATH) -> None:
        """Initializes the collector.

        Args:
            registry_path: The registry file.
        """
        self._registry_path = registry_path

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Fetches the new days of every package and emits one record per package with new days.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; the progress per package is kept in the seen store.
        """
        technologies = active_technologies(self._registry_path)
        targets = {ecosystem: [(technology, package) for technology in technologies
                               for package in technology.packages if package.ecosystem == ecosystem]
                   for ecosystem in ECOSYSTEMS}
        run = _Run(ctx, entry, sum(len(packages) for packages in targets.values()))
        self._collect_npm(run, targets["npm"])
        self._collect_daily(run, "pypi", targets["pypi"], fetch_pypi_downloads)
        self._collect_daily(run, "crates", targets["crates"], fetch_crates_downloads)
        return run.result()

    def _collect_npm(self, run: _Run, targets: Sequence[tuple[Technology, Package]]) -> None:
        """Fetches all npm packages, checks the days for gaps and stores the days with data.

        The gap check needs the values of all packages, so the records are built
        after the last request.

        Args:
            run: The run state.
            targets: The npm packages with their technologies.
        """
        ctx = run.ctx
        end = ctx.today - timedelta(days=NPM_DELAY_DAYS)
        retry_from = ctx.today - timedelta(days=NPM_GAP_RETRY_DAYS)
        fetched: list[NpmSeries] = []
        for position, (technology, package) in enumerate(targets):
            if run.past_deadline(len(targets) - position):
                break
            previous = run.seen.get(package.key)
            last_day = _data_day(previous, "last_day")
            retry_days = frozenset(day for day in _data_days(previous, "gap_days") if day >= retry_from)
            if last_day is None:
                new_start = end - timedelta(days=NPM_NEW_PACKAGE_DAYS - 1)
                start = NPM_HISTORY_START if ctx.backfill else max(NPM_HISTORY_START, new_start)
            else:
                start = min([last_day + timedelta(days=1), *retry_days])
            if start > end:
                continue
            values, stop = run.fetch(package, lambda: fetch_npm_downloads(ctx, package.name, start, end))
            if stop:
                run.counters["deferred"] += len(targets) - position - 1
                break
            if values is None:
                continue
            if last_day is None:
                values = without_leading_zeros(values)
            fetched.append(NpmSeries(technology, package, last_day, retry_days, values))
        if not fetched:
            return
        run.counters["npm_reference_packages"] = sum(is_npm_reference(series.values) for series in fetched)
        checked = check_npm_days(series.values for series in fetched)
        gap_days = sorted(day for day, is_gap in checked.items() if is_gap)
        run.counters["npm_gap_days"] = len(gap_days)
        if gap_days:
            logger.info("npm: %d gap days without data: %s", len(gap_days), ", ".join(map(date.isoformat, gap_days)))
        # Days that cannot be checked in this run are decided by the gap days of the earlier runs
        known_gaps = frozenset(run.known_gaps)
        run.update_known_gaps(checked)
        for series in fetched:
            stored, gaps, last_day = split_npm_days(series, checked, retry_from, known_gaps)
            if last_day is None:
                continue
            if series.last_day is not None:
                run.counters["npm_filled_days"] += sum(day <= series.last_day for day in stored)
            data: dict[str, Any] = {"last_day": last_day.isoformat()}
            if gaps:
                data["gap_days"] = sorted(day.isoformat() for day in gaps)
            run.store(series.technology, series.package, stored, data)

    def _collect_daily(self, run: _Run, ecosystem: str, targets: Sequence[tuple[Technology, Package]],
                       fetch: Callable[[RunContext, str], dict[date, int]]) -> None:
        """Fetches the packages of a registry that returns its whole retained history per request.

        Args:
            run: The run state.
            ecosystem: pypi or crates.
            targets: The packages of the registry with their technologies.
            fetch: Function that returns the downloads per day of a package.
        """
        ctx = run.ctx
        # Yesterday can still be incomplete at the time of the run (late log batches, update after 01:00 UTC)
        end = ctx.today - timedelta(days=2)
        for position, (technology, package) in enumerate(targets):
            if run.past_deadline(len(targets) - position):
                break
            start = series_start(run.seen.get(package.key), date.min)
            if start > end:
                continue
            values, stop = run.fetch(package, lambda: fetch(ctx, package.name))
            if stop:
                run.counters["deferred"] += len(targets) - position - 1
                break
            new_days = {day: count for day, count in (values or {}).items() if start <= day <= end}
            if new_days:
                run.store(technology, package, new_days, {"last_day": max(new_days).isoformat()})

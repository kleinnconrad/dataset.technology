"""
Collects the daily Indeed job postings indexes and the share of job postings that mention AI.

Source: the data repositories of Indeed Hiring Lab on GitHub, under CC BY 4.0 with the
requirement to cite Indeed Hiring Lab as the source. The files are read from
raw.githubusercontent.com, which serves repository files for programmatic use:
- ``job_postings_tracker``: per country the index of all job postings
  (``<CC>/aggregate_job_postings_<CC>.csv``, AGGREGATE_COUNTRIES) and the index per
  occupational category (``<CC>/job_postings_by_sector_<CC>.csv``, SECTOR_COUNTRIES),
  daily from 1 February 2020 (= 100), updated weekly;
- ``ai-tracker``: per country the share of job postings that mention AI or generative AI
  terms, in percent, as a 7-day trailing average (``AI_posting.csv``), daily from
  1 January 2019, updated about monthly.

Stored series:
- aggregate files: the seasonally adjusted index (column ``indeed_job_postings_index_SA``)
  of total and new postings, as the categories ``total postings`` and ``new postings``;
  the non-seasonally adjusted index is not stored;
- sector files: the index of total postings (variable ``total postings``) of the categories
  in SECTOR_CATEGORIES, where present. Hiring Lab seasonally adjusts each sector series
  separately (README of the repository), so these are stored as seasonally adjusted. The
  index of new postings per category is not stored, because the record has no field for
  the variable;
- AI tracker: the share per country, for every country in the file.

Fetch strategy: each file is downloaded with a conditional request (``If-None-Match`` with the
ETag of the previous download, kept in the cursor); a file that did not change (HTTP 304)
is skipped. The ETag of a gzip-compressed response differs from that of an uncompressed one;
the HTTP client always requests gzip. The files contain quoted fields with commas; they are
read with the csv module, which accepts LF and CRLF line endings.

Revisions: Hiring Lab revises the whole history at each update (seasonal adjustment). The
datasets store each day once, as first published: a run stores the days after the last
stored day of each series (series key ``<country>:<category>:sa`` for job postings and
``<country>`` for the AI share), one record per series and run. The first record of a
series holds its whole history and is marked as backfilled if its first day predates the
first run; later records hold days published after the previous run and are not marked,
although the files lag the days they describe by one to six weeks.

Schedule: weekly. At the run deadline the collector stops; the remaining files follow in the
next run.
"""
import csv
import io
import logging
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from collectors.base import Collector, CollectorResult, RunContext, series_start
from schema import AiJobPostingRecord, JobPostingRecord, RecordBase
from state import CollectorEntry, SeenStore, is_backfilled
from web import BlockedError, FetchError, RateLimitError

logger = logging.getLogger(__name__)

JOBS_DATASET = "job_postings"
AI_DATASET = "ai_job_postings"
DATASETS = (JOBS_DATASET, AI_DATASET)
JOBS_SOURCE_ID = "indeed_job_postings"
AI_SOURCE_ID = "indeed_ai_tracker"
LICENSE = "CC-BY-4.0 (Indeed Hiring Lab)"
JOBS_BASE_URL = "https://raw.githubusercontent.com/hiring-lab/job_postings_tracker/master"
AGGREGATE_URL = JOBS_BASE_URL + "/{country}/aggregate_job_postings_{country}.csv"
SECTOR_URL = JOBS_BASE_URL + "/{country}/job_postings_by_sector_{country}.csv"
AI_URL = "https://raw.githubusercontent.com/hiring-lab/ai-tracker/main/AI_posting.csv"
# EA is the euro area
AGGREGATE_COUNTRIES: tuple[str, ...] = ("AU", "CA", "DE", "EA", "ES", "FR", "GB", "IE", "IT", "NL", "US")
SECTOR_COUNTRIES: tuple[str, ...] = ("AU", "CA", "DE", "FR", "GB", "US")
# Technology-related categories and Banking & Finance as a non-technology baseline
SECTOR_CATEGORIES: tuple[str, ...] = (
    "Software Development", "IT Infrastructure, Operations & Support", "IT Systems & Solutions", "Data & Analytics",
    "Scientific Research & Development", "Electrical Engineering", "Mechanical Engineering", "Banking & Finance",
)
AGGREGATE_VARIABLES: tuple[str, ...] = ("total postings", "new postings")
SECTOR_VARIABLE = "total postings"
AGGREGATE_COLUMNS = ("date", "indeed_job_postings_index_SA", "variable")
SECTOR_COLUMNS = ("date", "indeed_job_postings_index", "variable", "display_name")
AI_COLUMNS = ("date", "jobcountry", "AI_share_postings")
# Read timeout per request; the sector files have about 12 MB
TIMEOUT_S = 60.0


class CsvFormatError(ValueError):
    """A file lacks a required column."""


@dataclass(frozen=True)
class SourceFile:
    """A data file of Indeed Hiring Lab.

    Attributes:
        kind (str): aggregate, sector or ai.
        url (str): Raw URL of the file.
        country (Optional[str]): Country of a job postings file; None for the AI tracker,
            which contains several countries.
    """
    kind: str
    url: str
    country: Optional[str] = None

    @property
    def name(self) -> str:
        """File name; the key of the ETag in the cursor."""
        return self.url.rsplit("/", 1)[-1]

    @property
    def dataset(self) -> str:
        """Dataset of the records of the file."""
        return AI_DATASET if self.kind == "ai" else JOBS_DATASET

    @property
    def expected_series(self) -> tuple[str, ...]:
        """Series names that the file can contain; empty for the AI tracker, which has no fixed list."""
        if self.kind == "aggregate":
            return AGGREGATE_VARIABLES
        return SECTOR_CATEGORIES if self.kind == "sector" else ()

    def series_key(self, series: str) -> str:
        """Returns the key of a series in the seen store.

        Args:
            series: Category of a job postings file, or country of the AI tracker.

        Returns:
            str: ``<country>:<category>:sa`` (all stored job postings series are seasonally
            adjusted) or ``<country>``.
        """
        return series if self.kind == "ai" else f"{self.country}:{series}:sa"

    def record_id(self, series: str, first: date, last: date) -> str:
        """Returns the record id of the days of a series stored in one run.

        Args:
            series: Category of a job postings file, or country of the AI tracker.
            first: First stored day.
            last: Last stored day.

        Returns:
            str: ``jobs:<country>:<category slug>:<first>:<last>`` or ``ai-jobs:<country>:<first>:<last>``.
        """
        if self.kind == "ai":
            return f"ai-jobs:{series}:{first.isoformat()}:{last.isoformat()}"
        return f"jobs:{self.country}:{slugify(series)}:{first.isoformat()}:{last.isoformat()}"


def slugify(text: str) -> str:
    """Converts a category name to the form used in record ids.

    Args:
        text: The name, e.g. ``IT Infrastructure, Operations & Support``.

    Returns:
        str: Lower-case words joined by hyphens, e.g. ``it-infrastructure-operations-support``.
    """
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def source_files(aggregate_countries: Sequence[str] = AGGREGATE_COUNTRIES,
                 sector_countries: Sequence[str] = SECTOR_COUNTRIES, ai_tracker: bool = True) -> list[SourceFile]:
    """Returns the files to fetch, the small files first.

    Args:
        aggregate_countries: Countries of the aggregate files.
        sector_countries: Countries of the sector files.
        ai_tracker: Whether to fetch the AI tracker.

    Returns:
        list[SourceFile]: The files.
    """
    files = [SourceFile("aggregate", AGGREGATE_URL.format(country=country), country) for country in aggregate_countries]
    if ai_tracker:
        files.append(SourceFile("ai", AI_URL))
    files.extend(SourceFile("sector", SECTOR_URL.format(country=country), country) for country in sector_countries)
    return files


def csv_rows(text: str, columns: Sequence[str]) -> Iterator[dict[str, str]]:
    """Reads the rows of a CSV file after checking its columns.

    Args:
        text: Content of the file; CRLF or LF line endings.
        columns: Columns that must be present.

    Yields:
        dict[str, str]: Each row by column name.

    Raises:
        CsvFormatError: If a required column is missing.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿"), newline=""))
    missing = [column for column in columns if column not in (reader.fieldnames or [])]
    if missing:
        raise CsvFormatError(f"missing columns {missing}; found {reader.fieldnames}")
    yield from reader


def _day_and_value(row: Mapping[str, Optional[str]], column: str) -> Optional[tuple[date, float]]:
    """Returns the day and the value of a row, or None if either is missing or invalid."""
    try:
        day, value = date.fromisoformat((row.get("date") or "").strip()), float((row.get(column) or "").strip())
    except ValueError:
        return None
    return (day, value) if math.isfinite(value) else None


def parse_aggregate(text: str) -> dict[str, dict[date, float]]:
    """Reads the seasonally adjusted index of total and new postings from an aggregate file.

    Args:
        text: Content of the file.

    Returns:
        dict: Index by category (total postings, new postings) and day.

    Raises:
        CsvFormatError: If a required column is missing.
    """
    series: dict[str, dict[date, float]] = {}
    for row in csv_rows(text, AGGREGATE_COLUMNS):
        variable = (row.get("variable") or "").strip()
        parsed = _day_and_value(row, "indeed_job_postings_index_SA")
        if variable in AGGREGATE_VARIABLES and parsed:
            series.setdefault(variable, {})[parsed[0]] = parsed[1]
    return series


def parse_sectors(text: str, categories: Sequence[str] = SECTOR_CATEGORIES) -> dict[str, dict[date, float]]:
    """Reads the index of total postings of selected categories from a sector file.

    Args:
        text: Content of the file.
        categories: Categories to keep (``display_name``).

    Returns:
        dict: Index by category and day, for the categories present in the file.

    Raises:
        CsvFormatError: If a required column is missing.
    """
    wanted = set(categories)
    series: dict[str, dict[date, float]] = {}
    for row in csv_rows(text, SECTOR_COLUMNS):
        category = (row.get("display_name") or "").strip()
        if category not in wanted or (row.get("variable") or "").strip() != SECTOR_VARIABLE:
            continue
        parsed = _day_and_value(row, "indeed_job_postings_index")
        if parsed:
            series.setdefault(category, {})[parsed[0]] = parsed[1]
    return series


def parse_ai_shares(text: str) -> dict[str, dict[date, float]]:
    """Reads the share of job postings that mention AI from the AI tracker.

    Args:
        text: Content of the file.

    Returns:
        dict: Share in percent by country and day.

    Raises:
        CsvFormatError: If a required column is missing.
    """
    series: dict[str, dict[date, float]] = {}
    for row in csv_rows(text, AI_COLUMNS):
        country = (row.get("jobcountry") or "").strip()
        parsed = _day_and_value(row, "AI_share_postings")
        if country and parsed:
            series.setdefault(country, {})[parsed[0]] = parsed[1]
    return series


def parse_file(source: SourceFile, text: str) -> dict[str, dict[date, float]]:
    """Reads the series of a file.

    Args:
        source: The file.
        text: Its content.

    Returns:
        dict: Values by series (category or country) and day.

    Raises:
        CsvFormatError: If a required column is missing.
    """
    if source.kind == "aggregate":
        return parse_aggregate(text)
    if source.kind == "sector":
        return parse_sectors(text)
    return parse_ai_shares(text)


class JobPostingsCollector(Collector):
    """Writes the new days of the Indeed job postings indexes and of the AI share of job postings."""
    name = "job_postings"
    datasets = DATASETS
    min_days_between_runs = 6

    def __init__(self, files: Optional[Sequence[SourceFile]] = None) -> None:
        """Initializes the collector.

        Args:
            files: Files to fetch; None fetches all files of ``source_files()``.
        """
        self._files = tuple(files) if files is not None else tuple(source_files())

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Fetches every changed file and emits the days after the last stored day of each series.

        Args:
            ctx: The run context.
            entry: Run history of this collector; the cursor holds the ETag per file.

        Returns:
            CollectorResult: The updated cursor and counters.
        """
        etags: dict[str, str] = dict(entry.cursor.get("etags") or {})
        counters: dict[str, Any] = {"files": len(self._files), "downloaded": 0, "not_modified": 0, "failed": 0,
                                    "records": 0, "days": 0, "missing_series": 0}
        complete = True
        error: Optional[str] = None
        requested = 0
        for position, source in enumerate(self._files):
            if ctx.past_deadline():
                counters["deferred"] = len(self._files) - position
                complete = False
                break
            seen = ctx.seen(source.dataset)
            # Without stored series (first run, lost state) the file is downloaded in full
            headers = {"If-None-Match": etags[source.name]} if source.name in etags and self._has_state(
                source, seen) else None
            requested += 1
            try:
                response = ctx.web.get(source.url, headers=headers, respect_robots=False, timeout_s=TIMEOUT_S)
            except RateLimitError as e:
                logger.warning("Rate limit at %s; the remaining files follow in the next run: %s", source.url, e)
                counters["deferred"] = len(self._files) - position
                complete = False
                break
            except BlockedError as e:
                logger.error("Request refused; no further requests in this run: %s", e)
                error = f"blocked: {e}"
                complete = False
                break
            except FetchError as e:
                logger.warning("Hiring Lab file %s failed: %s", source.name, e)
                counters["failed"] += 1
                continue
            if response.status == 304:
                counters["not_modified"] += 1
                continue
            try:
                series = parse_file(source, response.text)
            except (CsvFormatError, csv.Error) as e:
                logger.warning("Hiring Lab file %s has an unexpected format: %s", source.name, e)
                counters["failed"] += 1
                continue
            counters["downloaded"] += 1
            records = self._records(ctx, entry, source, series, seen, counters)
            ctx.emit(source.dataset, records)
            counters["records"] += len(records)
            if response.headers.get("etag"):
                etags[source.name] = response.headers["etag"]
        if error is None and counters["failed"] and counters["failed"] == requested:
            error = f"all {requested} requested files failed"
        logger.info("Job postings: %d records with %d days from %d downloaded files, %d not modified, %d failed.",
                    counters["records"], counters["days"], counters["downloaded"], counters["not_modified"],
                    counters["failed"])
        return CollectorResult(cursor={**entry.cursor, "etags": etags}, counters=counters, complete=complete,
                               error=error)

    @staticmethod
    def _has_state(source: SourceFile, seen: SeenStore) -> bool:
        """Tells whether the seen store holds a series of a file.

        Args:
            source: The file.
            seen: The seen store of its dataset.

        Returns:
            bool: True if at least one series of the file was stored before.
        """
        if source.kind == "ai":
            return len(seen) > 0
        return any(source.series_key(series) in seen for series in source.expected_series)

    @staticmethod
    def _records(ctx: RunContext, entry: CollectorEntry, source: SourceFile, series: Mapping[str, dict[date, float]],
                 seen: SeenStore, counters: dict[str, Any]) -> list[RecordBase]:
        """Builds one record per series with the days after its last stored day.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            source: The file.
            series: Values by series and day.
            seen: The seen store of the dataset of the file.
            counters: Counters of the run; updated in place.

        Returns:
            list[RecordBase]: ``JobPostingRecord`` or ``AiJobPostingRecord`` objects.
        """
        missing = [name for name in source.expected_series if source.series_key(name) in seen and name not in series]
        if missing:
            logger.warning("Hiring Lab file %s no longer contains the series %s.", source.name, ", ".join(missing))
            counters["missing_series"] += len(missing)
        records: list[RecordBase] = []
        for name, values in sorted(series.items()):
            key = source.series_key(name)
            previous = seen.get(key)
            start = series_start(previous, date.min)
            new = {day: value for day, value in sorted(values.items()) if day >= start}
            if not new:
                continue
            first, last = min(new), max(new)
            # Each record covers new days, so it is a new item with revision 0
            seen.mark(key, source.name, ctx.today, data={"last_day": last.isoformat()})
            # Only the first record of a series holds history; later records hold days published after the
            # previous run, even if the source publishes them weeks after the day they describe
            envelope = ctx.envelope(
                source.dataset, source.record_id(name, first, last), revision=0,
                source_id=AI_SOURCE_ID if source.kind == "ai" else JOBS_SOURCE_ID, source_url=source.url,
                license=LICENSE, backfilled=previous is None and is_backfilled(first, entry.first_run),
            )
            if source.kind == "ai":
                records.append(AiJobPostingRecord(**envelope, country=name, daily_share_pct=new))
            else:
                records.append(JobPostingRecord(**envelope, country=source.country, category=name,
                                                seasonally_adjusted=True, daily_index=new))
            counters["days"] += len(new)
        return records

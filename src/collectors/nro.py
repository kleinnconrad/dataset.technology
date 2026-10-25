"""
Collects the Internet number resources per regional internet registry, country and status from the
combined delegated statistics of the NRO.

Source: the extended delegated statistics of the Number Resource Organization (NRO), a combined file
of the five regional internet registries (AFRINIC, APNIC, ARIN, LACNIC, RIPE NCC) and the IANA pools
on ftp.ripe.net (a website; robots.txt is respected). The file is generated daily at about 11:05 UTC
and has about 774,000 lines (57 MB): a version line with the date of the file, three summary lines
and one line per resource range in the format
``registry|cc|type|start|value|date|status|opaque-id|extensions``.

Stored values: one snapshot per file date with one record per registry, country and status, about
400 records per snapshot (401 in the file of 24 October 2026). A record holds the number of IPv4
addresses and ranges, the IPv6 address space in /32 equivalents and the number of IPv6 prefixes, and
the number of AS numbers and AS number ranges. The country is the country in which the holder of the
resources is legally based, as registered with the registry (RSEF format), not the country in which
the addresses are used. Pools that are not delegated (available, reserved) have the country ZZ and
the registry name in the opaque-id field. The records name the URL of the file in the archive
directory of its date, which stays valid; ``latest`` serves the same file as that directory.

Counting rules:
- IPv4: the value of a line is the number of addresses. Ranges that are not aligned to a CIDR prefix
  count with their number of addresses.
- IPv6: the value is the prefix length; a /n prefix counts as 2^(32 - n) /32 prefixes, a fraction
  for prefixes longer than /32. The space is summed exactly and converted at the end.
- AS numbers: the value is the number of AS numbers in the range.
- Statuses: assigned, available and reserved. The combined file folds ``allocated`` into
  ``assigned``; ``allocated`` is read as assigned as well. Lines with another status (e.g. ``ietf``
  in older files) are skipped and counted. A line without a country counts under ZZ.

Fetch strategy: the current file (``latest``) is downloaded with a conditional request
(``If-None-Match`` with the ETag of the previous download, kept in the cursor) and read line by
line. A file date that is already stored is skipped; the stored file dates are kept in the seen
store, which is saved together with the records.

History: a regular run stores only the current file. A backfill run also imports the archived file
of the first day of every quarter since October 2021 (one file of 47 to 57 MB per quarter). Archive
files that are not imported at the run deadline follow in the next runs, at most
ARCHIVE_FILES_PER_REGULAR_RUN per regular run. An archive quarter without a file (HTTP 404) is kept
in the cursor and not requested again. The archive holds this file from 6 September 2021; older
directories hold only the combined-stat file, which is not imported.

Schedule: quarterly (``min_days_between_runs = 85``); the statistics change slowly, and a
snapshot has about 400 records.

License: the combined file states no license. The registries publish their statistics for public
use: APNIC makes them freely available for download and use on the condition that APNIC is not held
responsible; the RIPE NCC allows use with proper identification of the source. Only derived counts
are stored.
"""
import io
import logging
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import astuple, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

from collectors.base import Collector, CollectorResult, RunContext
from schema import IpDelegationRecord
from state import CollectorEntry, SeenStore, content_hash, is_backfilled
from web import BlockedError, FetchError, NotFoundError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "ip_address_delegations"
SOURCE_ID = "nro_delegated_stats"
LICENSE = "Derived counts from the NRO extended delegated statistics (AFRINIC, APNIC, ARIN, LACNIC, RIPE NCC)"
STATS_URL = "https://ftp.ripe.net/pub/stats/ripencc/nro-stats/"
LATEST_URL = STATS_URL + "latest/nro-delegated-stats"
ARCHIVE_URL = STATS_URL + "{day:%Y%m%d}/nro-delegated-stats"
# First month imported by a backfill run
ARCHIVE_START = date(2021, 10, 1)
# Months between two archive snapshots (quarterly)
ARCHIVE_STEP_MONTHS = 3
# Archive files that a regular run imports while a backfill is unfinished, so that the run keeps time
# for the collectors after this one
ARCHIVE_FILES_PER_REGULAR_RUN = 12
# Read timeout of one download of 47 to 57 MB
DOWNLOAD_TIMEOUT_S = 300.0
# Status in the file -> stored status
STATUSES = {"assigned": "assigned", "allocated": "assigned", "available": "available", "reserved": "reserved"}
RESOURCE_TYPES = frozenset({"ipv4", "ipv6", "asn"})
NO_COUNTRY = "ZZ"
# IPv6 address space is summed exactly as a number of /128 addresses
ADDRESSES_PER_SLASH32 = 2 ** 96
# Cursor values of the archive import
ARCHIVE_PENDING = "pending"
ARCHIVE_DONE = "done"

GroupKey = tuple[str, str, str]


class StatsFormatError(ValueError):
    """A statistics file has no readable version line or no resource lines."""


@dataclass
class ResourceCounts:
    """Resources of one registry, country and status in a statistics file.

    Attributes:
        ipv4_addresses (int): Number of IPv4 addresses.
        ipv4_ranges (int): Number of IPv4 ranges.
        ipv6_addresses (int): Number of IPv6 addresses (exact sum of the prefix sizes).
        ipv6_prefixes (int): Number of IPv6 prefixes.
        asns (int): Number of AS numbers.
        asn_ranges (int): Number of AS number ranges.
    """
    ipv4_addresses: int = 0
    ipv4_ranges: int = 0
    ipv6_addresses: int = 0
    ipv6_prefixes: int = 0
    asns: int = 0
    asn_ranges: int = 0

    def add(self, resource_type: str, value: int) -> None:
        """Adds one resource line.

        Args:
            resource_type: ipv4, ipv6 or asn.
            value: Number of addresses (ipv4), prefix length (ipv6) or number of AS numbers (asn).
        """
        if resource_type == "ipv4":
            self.ipv4_addresses += value
            self.ipv4_ranges += 1
        elif resource_type == "ipv6":
            self.ipv6_addresses += 2 ** (128 - value)
            self.ipv6_prefixes += 1
        else:
            self.asns += value
            self.asn_ranges += 1

    @property
    def ipv6_slash32_equivalents(self) -> float:
        """IPv6 address space in units of /32 prefixes."""
        return self.ipv6_addresses / ADDRESSES_PER_SLASH32


def archive_url(month: date) -> str:
    """Returns the URL of the archived file of a day.

    Args:
        month: The day, the first day of a month for the backfill.

    Returns:
        str: The URL.
    """
    return ARCHIVE_URL.format(day=month)


def archive_months(today: date) -> list[date]:
    """Returns the first days of the quarters from ARCHIVE_START that lie before the run day.

    The file of a day is generated at about 11:05 UTC, so the run day itself is excluded.

    Args:
        today: Day of the run.

    Returns:
        list[date]: The first days of the quarters, in chronological order.
    """
    months: list[date] = []
    month = ARCHIVE_START
    while month < today:
        months.append(month)
        for _ in range(ARCHIVE_STEP_MONTHS):
            month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def iter_lines(content: bytes) -> Iterator[str]:
    """Yields the lines of a downloaded file one by one, without decoding the file as a whole.

    Args:
        content: The file.

    Yields:
        str: Each line, with its line break.
    """
    yield from io.TextIOWrapper(io.BytesIO(content), encoding="utf-8", errors="replace")


def _parse_day(value: str) -> Optional[date]:
    """Parses a date written as YYYYMMDD; returns None for other values."""
    if len(value) != 8 or not value.isdigit():
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def parse_version_line(line: str) -> tuple[date, Optional[int]]:
    """Reads the file date and the number of records from the version line of a statistics file.

    The version line is ``version|registry|serial|records|startdate|enddate|UTCoffset``. In the NRO
    file the serial is the date of the file (YYYYMMDD); if it is not a date, the end date is used.

    Args:
        line: The version line.

    Returns:
        tuple: The file date and the number of records stated in the line (None if not a number).

    Raises:
        StatsFormatError: If the line is not a version line or has no date.
    """
    parts = line.strip().split("|")
    if len(parts) < 6 or not parts[0].isdigit():
        raise StatsFormatError(f"not a version line: {line[:80]!r}")
    file_date = _parse_day(parts[2]) or _parse_day(parts[5])
    if file_date is None:
        raise StatsFormatError(f"the version line has no date: {line[:80]!r}")
    return file_date, int(parts[3]) if parts[3].isdigit() else None


def read_version(lines: Iterator[str]) -> tuple[date, Optional[int]]:
    """Consumes the lines up to the version line and reads it.

    Args:
        lines: The lines of the file; the iterator is left after the version line.

    Returns:
        tuple: The file date and the number of records stated in the version line.

    Raises:
        StatsFormatError: If the file has no version line.
    """
    for raw_line in lines:
        line = raw_line.strip()
        if line and not line.startswith("#"):
            return parse_version_line(line)
    raise StatsFormatError("the file has no version line")


def aggregate_resources(lines: Iterable[str]) -> tuple[dict[GroupKey, ResourceCounts], Counter[str]]:
    """Sums the resource lines of a statistics file per registry, country and status.

    Comment lines and the summary lines are skipped. Lines with an unknown type or status and lines
    that cannot be read are skipped and counted; a line without a country counts under ZZ.

    Args:
        lines: The lines after the version line.

    Returns:
        tuple: Resources by (registry, country, status), and counters with the number of resource
        lines (``lines``) and of the skipped or corrected lines (``malformed``, ``unknown_type``,
        ``unknown_status``, ``empty_country``).
    """
    groups: dict[GroupKey, ResourceCounts] = {}
    counters: Counter[str] = Counter()
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if parts[-1] == "summary":
            continue
        counters["lines"] += 1
        if len(parts) < 7:
            counters["malformed"] += 1
            continue
        registry, country, resource_type, _start, value, _day, status = (part.strip() for part in parts[:7])
        resource_type = resource_type.lower()
        if resource_type not in RESOURCE_TYPES:
            counters["unknown_type"] += 1
            continue
        try:
            amount = int(value)
        except ValueError:
            counters["malformed"] += 1
            continue
        if not registry or amount < 0 or (resource_type == "ipv6" and amount > 128):
            counters["malformed"] += 1
            continue
        stored_status = STATUSES.get(status.lower())
        if stored_status is None:
            counters["unknown_status"] += 1
            continue
        if not country:
            counters["empty_country"] += 1
            country = NO_COUNTRY
        key = (registry.lower(), country.upper(), stored_status)
        groups.setdefault(key, ResourceCounts()).add(resource_type, amount)
    return groups, counters


def build_records(ctx: RunContext, entry: CollectorEntry, file_date: date, groups: dict[GroupKey, ResourceCounts],
                  source_url: str) -> list[IpDelegationRecord]:
    """Builds the records of one snapshot.

    Args:
        ctx: The run context.
        entry: Run history of the collector.
        file_date: Date of the statistics file.
        groups: Resources by (registry, country, status).
        source_url: URL of the file.

    Returns:
        list[IpDelegationRecord]: One record per registry, country and status.
    """
    backfilled = is_backfilled(file_date, entry.first_run)
    records: list[IpDelegationRecord] = []
    for (registry, country, status), counts in sorted(groups.items()):
        records.append(IpDelegationRecord(
            **ctx.envelope(DATASET, f"nro:{file_date.isoformat()}:{registry}:{country}:{status}", revision=0,
                           source_id=SOURCE_ID, source_url=source_url, license=LICENSE, backfilled=backfilled),
            registry=registry,
            country=country,
            status=status,
            ipv4_addresses=counts.ipv4_addresses,
            ipv4_ranges=counts.ipv4_ranges,
            ipv6_slash32_equivalents=counts.ipv6_slash32_equivalents,
            ipv6_prefixes=counts.ipv6_prefixes,
            asns=counts.asns,
            asn_ranges=counts.asn_ranges,
            file_date=file_date,
        ))
    return records


class NroCollector(Collector):
    """Writes one snapshot of the delegated Internet number resources per statistics file."""
    name = "nro"
    datasets = (DATASET,)
    min_days_between_runs = 85

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Stores the current statistics file and, while a backfill is unfinished, archived monthly files.

        Args:
            ctx: The run context.
            entry: Run history of this collector; the cursor holds the ETag of the current file, its
                date, the state of the archive import and archive months without a file.

        Returns:
            CollectorResult: The updated cursor and counters.
        """
        cursor = dict(entry.cursor)
        seen = ctx.seen(DATASET)
        if ctx.backfill:
            cursor["archive"] = ARCHIVE_PENDING
        missing = set(cursor.get("archive_missing") or [])
        # None stands for the current file
        jobs: list[Optional[date]] = [None]
        deferred = 0
        if cursor.get("archive") == ARCHIVE_PENDING:
            pending = self._pending_months(ctx.today, seen, missing)
            limit = len(pending) if ctx.backfill else ARCHIVE_FILES_PER_REGULAR_RUN
            jobs.extend(pending[:limit])
            deferred = len(pending[limit:])
        counters: dict[str, Any] = {
            "files": 0, "not_modified": 0, "already_stored": 0, "records": 0, "failed": 0, "archive_missing": 0,
            "lines": 0, "malformed": 0, "unknown_type": 0, "unknown_status": 0, "empty_country": 0,
        }
        complete = True
        error: Optional[str] = None
        requested = 0
        for position, month in enumerate(jobs):
            if ctx.past_deadline():
                deferred += sum(1 for job in jobs[position:] if job is not None)
                complete = False
                break
            url = LATEST_URL if month is None else archive_url(month)
            headers = None
            # Without the stored snapshot (first run, lost state) the file is downloaded in full
            if month is None and cursor.get("etag") and cursor.get("file_date") in seen:
                headers = {"If-None-Match": cursor["etag"]}
            requested += 1
            try:
                response = ctx.web.get(url, headers=headers, timeout_s=DOWNLOAD_TIMEOUT_S)
            except RateLimitError as e:
                logger.warning("Rate limit at %s; the remaining files follow in the next run: %s", url, e)
                deferred += sum(1 for job in jobs[position:] if job is not None)
                complete = False
                break
            except BlockedError as e:
                logger.error("Request refused; no further requests in this run: %s", e)
                error = f"blocked: {e}"
                complete = False
                break
            except NotFoundError as e:
                if month is None:
                    logger.warning("NRO statistics file not found: %s", e)
                    counters["failed"] += 1
                else:
                    logger.warning("No archived NRO statistics for %s; the month is not requested again.", month)
                    missing.add(month.isoformat())
                    counters["archive_missing"] += 1
                continue
            except FetchError as e:
                logger.warning("NRO statistics %s failed: %s", url, e)
                counters["failed"] += 1
                continue
            if response.status == 304:
                counters["not_modified"] += 1
                continue
            try:
                file_date = self._store_file(ctx, entry, seen, response.content, counters)
            except StatsFormatError as e:
                logger.warning("NRO statistics %s cannot be read: %s", url, e)
                counters["failed"] += 1
                continue
            if month is None:
                cursor["file_date"] = file_date.isoformat()
                counters["file_date"] = file_date.isoformat()
                etag = response.headers.get("etag")
                if etag:
                    cursor["etag"] = etag
                else:
                    cursor.pop("etag", None)
            elif file_date != month:
                logger.warning("The archived NRO statistics of %s have the date %s; the month is not requested again.",
                               month, file_date)
                missing.add(month.isoformat())
        if deferred:
            counters["deferred"] = deferred
            complete = False
        if cursor.get("archive") == ARCHIVE_PENDING and not self._pending_months(ctx.today, seen, missing):
            cursor["archive"] = ARCHIVE_DONE
        if missing:
            cursor["archive_missing"] = sorted(missing)
        if error is None and counters["failed"] and counters["failed"] == requested:
            error = f"all {requested} requested files failed"
        logger.info("NRO statistics: %d records from %d files, %d not modified, %d already stored, %d failed.",
                    counters["records"], counters["files"], counters["not_modified"], counters["already_stored"],
                    counters["failed"])
        return CollectorResult(cursor=cursor, counters=counters, complete=complete, error=error)

    @staticmethod
    def _pending_months(today: date, seen: SeenStore, missing: set[str]) -> list[date]:
        """Returns the archive months that are neither stored nor known to be missing.

        Args:
            today: Day of the run.
            seen: The seen store of the dataset, keyed by file date.
            missing: Months (ISO dates) without an archived file.

        Returns:
            list[date]: The months, in chronological order.
        """
        return [month for month in archive_months(today)
                if month.isoformat() not in seen and month.isoformat() not in missing]

    @staticmethod
    def _store_file(ctx: RunContext, entry: CollectorEntry, seen: SeenStore, content: bytes,
                    counters: dict[str, Any]) -> date:
        """Aggregates a downloaded statistics file and emits its snapshot unless its date is stored.

        Args:
            ctx: The run context.
            entry: Run history of the collector.
            seen: The seen store of the dataset, keyed by file date.
            content: The downloaded file.
            counters: Counters of the run; updated in place.

        Returns:
            date: The date of the file.

        Raises:
            StatsFormatError: If the file has no version line or no resource lines.
        """
        lines = iter_lines(content)
        file_date, declared = read_version(lines)
        counters["files"] += 1
        key = file_date.isoformat()
        if key in seen:
            logger.info("NRO statistics of %s are already stored.", key)
            counters["already_stored"] += 1
            return file_date
        groups, line_counters = aggregate_resources(lines)
        for name, value in line_counters.items():
            counters[name] = counters.get(name, 0) + value
        if not groups:
            raise StatsFormatError("the file has no resource lines")
        if declared is not None and line_counters["lines"] != declared:
            logger.warning("NRO statistics of %s: %d resource lines, the version line states %d.", key,
                           line_counters["lines"], declared)
            counters["count_mismatch"] = counters.get("count_mismatch", 0) + 1
        # The archive URL of the file date stays valid, unlike the URL of the current file
        records = build_records(ctx, entry, file_date, groups, archive_url(file_date))
        item_hash = content_hash([[*group, *astuple(counts)] for group, counts in sorted(groups.items())])
        seen.mark(key, item_hash, ctx.today, data={"records": len(records)})
        ctx.emit(DATASET, records)
        counters["records"] += len(records)
        logger.info("NRO statistics of %s: %d records from %d resource lines.", key, len(records),
                    line_counters["lines"])
        return file_date

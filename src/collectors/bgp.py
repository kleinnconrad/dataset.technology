"""
Collects the daily size of the global BGP routing table for IPv4 and IPv6 from bgp.potaroo.net.

Source: the BGP routing table analysis of Geoff Huston (APNIC) on bgp.potaroo.net (a website;
robots.txt asks for 10 seconds between requests, which the HTTP client honors). Four text files
hold the number of prefixes in the routing table and the number of origin autonomous systems (ASes)
for IPv4 and IPv6, with one line per measurement (``<unix timestamp> <value>``), about one per hour.
Each file holds the whole series: IPv4 prefixes from 30 June 1988 (monthly until 1993), IPv4 origin
ASes from 30 September 1996, IPv6 prefixes and origin ASes from 10 February 2003. All values are
measured at a single vantage point (AS65000); routing tables seen elsewhere differ slightly.

Stored values: per IP version one series record per run with the value of the last measurement of
each UTC day (``daily_prefixes``, ``daily_ases``), for the days after the last stored day (seen key
``v4`` or ``v6``). A day is stored once it is complete: it lies before the day of the run and before
the day of the latest measurement in both files of the IP version. A file whose latest measurement
is more than MAX_FILE_LAG_DAYS behind the other file of its IP version does not hold back the other
series. The value of a day is that of the measurement with the latest timestamp, also where lines
are out of order (a few lines per file). Unreadable lines are skipped, and days without a
measurement (the IPv4 prefix series has 77 gaps of more than three days) are missing in the record.
If a file of an IP version cannot be fetched, the IP version is skipped and follows in the next run.

History: the first run imports the whole history (11,936 days of IPv4 prefixes on 25 October 2026).

Schedule: monthly.

License: none published; the records name bgp.potaroo.net as their source.
"""
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional

from collectors.base import Collector, CollectorResult, RunContext, series_start
from schema import BgpTableRecord
from state import CollectorEntry, SeenStore, content_hash, is_backfilled
from web import BlockedError, FetchError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "bgp_routing_table"
SOURCE_ID = "potaroo_bgp"
LICENSE = "bgp.potaroo.net (Geoff Huston, APNIC); no license published"
BASE_URL = "https://bgp.potaroo.net"
# Read timeout of one file of a few megabytes
DOWNLOAD_TIMEOUT_S = 120.0
# A file that lags the other file of its IP version by more days is treated as no longer updated
MAX_FILE_LAG_DAYS = 7
EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class TableFiles:
    """The two files of the routing table of one IP version.

    Attributes:
        ip_version (int): 4 or 6.
        prefixes_url (str): File with the number of announced prefixes.
        ases_url (str): File with the number of origin ASes.
    """
    ip_version: int
    prefixes_url: str
    ases_url: str

    @property
    def key(self) -> str:
        """Seen key and record id part of the IP version, e.g. v4."""
        return f"v{self.ip_version}"


TABLES: tuple[TableFiles, ...] = (
    TableFiles(4, f"{BASE_URL}/as2.0/bgp-active.txt", f"{BASE_URL}/as2.0/bgp-as-count.txt"),
    TableFiles(6, f"{BASE_URL}/v6/as2.0/bgp-active.txt", f"{BASE_URL}/v6/as2.0/bgp-as-count.txt"),
)


@dataclass(frozen=True)
class DailySeries:
    """The daily values of one measurement file.

    Attributes:
        values (dict[date, int]): Value of the last measurement of each UTC day, in chronological order.
        latest_day (Optional[date]): UTC day of the latest measurement; None if the file has none.
        malformed (int): Number of lines that could not be read.
    """
    values: dict[date, int]
    latest_day: Optional[date]
    malformed: int


def parse_measurements(text: str) -> DailySeries:
    """Reads a measurement file and keeps the value of the last measurement of each UTC day.

    Each line is ``<unix timestamp> <value>``. Lines are not always in chronological order, so the
    measurement with the latest timestamp of a day is kept regardless of its position; of two lines
    with the same timestamp the later one wins.

    Args:
        text: The file content.

    Returns:
        DailySeries: The daily values, the day of the latest measurement and the number of
        unreadable lines.
    """
    latest: dict[date, tuple[int, int]] = {}
    malformed = 0
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        if len(parts) != 2:
            malformed += 1
            continue
        try:
            timestamp, value = int(parts[0]), int(parts[1])
            day = date.fromordinal(EPOCH_ORDINAL + timestamp // SECONDS_PER_DAY)
        except (ValueError, OverflowError):
            malformed += 1
            continue
        if value < 0:
            malformed += 1
            continue
        previous = latest.get(day)
        if previous is None or timestamp >= previous[0]:
            latest[day] = (timestamp, value)
    values = {day: value for day, (_timestamp, value) in sorted(latest.items())}
    return DailySeries(values=values, latest_day=max(latest) if latest else None, malformed=malformed)


def last_complete_day(today: date, latest_days: list[date]) -> tuple[date, int]:
    """Returns the last day that is complete in the files of an IP version.

    A day is complete when it lies before the day of the run and before the day of the latest
    measurement of each file. A file whose latest measurement lags the newest file by more than
    MAX_FILE_LAG_DAYS is ignored, so that a file that is no longer updated does not hold back the
    other series.

    Args:
        today: Day of the run.
        latest_days: Day of the latest measurement of each file.

    Returns:
        tuple: The last complete day and the number of ignored (stale) files.
    """
    # A measurement dated after the run day (wrong clock at the source) counts as today
    latest = [min(day, today) for day in latest_days]
    newest = max(latest)
    current = [day for day in latest if (newest - day).days <= MAX_FILE_LAG_DAYS]
    end = min([today, *current]) - timedelta(days=1)
    return end, len(latest) - len(current)


class BgpCollector(Collector):
    """Writes the new days of the size of the IPv4 and IPv6 BGP routing tables."""
    name = "bgp"
    datasets = (DATASET,)
    min_days_between_runs = 27

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Downloads the four files and emits one record per IP version with the new complete days.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; the progress per IP version is kept in the seen store.
        """
        seen = ctx.seen(DATASET)
        files = [(table, url) for table in TABLES for url in (table.prefixes_url, table.ases_url)]
        counters: dict[str, Any] = {"files": 0, "records": 0, "days": 0, "failed": 0, "malformed_lines": 0,
                                    "stale_files": 0}
        complete = True
        error: Optional[str] = None
        requested = 0
        fetched: dict[str, DailySeries] = {}
        for position, (table, url) in enumerate(files):
            if url == table.ases_url and table.prefixes_url not in fetched:
                # The IP version is skipped because its prefix file failed
                continue
            if ctx.past_deadline():
                counters["deferred"] = len(files) - position
                complete = False
                break
            requested += 1
            try:
                response = ctx.web.get(url, timeout_s=DOWNLOAD_TIMEOUT_S)
            except RateLimitError as e:
                logger.warning("Rate limit at %s; the remaining files follow in the next run: %s", url, e)
                counters["deferred"] = len(files) - position
                complete = False
                break
            except BlockedError as e:
                logger.error("Request refused; no further requests in this run: %s", e)
                error = f"blocked: {e}"
                complete = False
                break
            except FetchError as e:
                logger.warning("BGP file %s failed: %s", url, e)
                counters["failed"] += 1
                continue
            series = parse_measurements(response.text)
            counters["malformed_lines"] += series.malformed
            if series.latest_day is None:
                logger.warning("BGP file %s has no measurements.", url)
                counters["failed"] += 1
                continue
            counters["files"] += 1
            fetched[url] = series
            if url == table.ases_url:
                record = self._record(ctx, entry, seen, table, fetched[table.prefixes_url], series, counters)
                if record is not None:
                    ctx.emit(DATASET, [record])
                    counters["records"] += 1
        if error is None and counters["failed"] and counters["failed"] == requested:
            error = f"all {requested} requested files failed"
        logger.info("BGP routing table: %d records with %d days from %d files, %d failed.", counters["records"],
                    counters["days"], counters["files"], counters["failed"])
        return CollectorResult(counters=counters, complete=complete, error=error)

    @staticmethod
    def _record(ctx: RunContext, entry: CollectorEntry, seen: SeenStore, table: TableFiles, prefixes: DailySeries,
                ases: DailySeries, counters: dict[str, Any]) -> Optional[BgpTableRecord]:
        """Builds the record of an IP version with the complete days after the last stored day.

        Args:
            ctx: The run context.
            entry: Run history of the collector.
            seen: The seen store of the dataset.
            table: The files of the IP version.
            prefixes: Daily numbers of prefixes.
            ases: Daily numbers of origin ASes.
            counters: Counters of the run; updated in place.

        Returns:
            Optional[BgpTableRecord]: The record, or None if there is no new complete day.
        """
        latest_days = [day for day in (prefixes.latest_day, ases.latest_day) if day is not None]
        end, stale = last_complete_day(ctx.today, latest_days)
        if stale:
            logger.warning("A BGP file of IPv%d lags more than %d days behind the other.", table.ip_version,
                           MAX_FILE_LAG_DAYS)
            counters["stale_files"] += stale
        # The first run imports the whole history
        start = series_start(seen.get(table.key), date.min)
        new_prefixes = {day: value for day, value in prefixes.values.items() if start <= day <= end}
        new_ases = {day: value for day, value in ases.values.items() if start <= day <= end}
        days = new_prefixes.keys() | new_ases.keys()
        if not days:
            return None
        first, last = min(days), max(days)
        # Each record covers new days, so it is a new item with revision 0
        seen.mark(table.key, content_hash([table.prefixes_url, table.ases_url]), ctx.today,
                  data={"last_day": last.isoformat()})
        counters["days"] += len(days)
        return BgpTableRecord(
            **ctx.envelope(DATASET, f"bgp:{table.key}:{first.isoformat()}:{last.isoformat()}", revision=0,
                           source_id=SOURCE_ID, source_url=table.prefixes_url, license=LICENSE,
                           backfilled=is_backfilled(first, entry.first_run)),
            ip_version=table.ip_version,
            daily_prefixes=new_prefixes,
            daily_ases=new_ases,
        )

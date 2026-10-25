"""
Collects the daily shares of IPv6-capable and IPv6-preferring users per economy from the APNIC Labs
IPv6 measurement.

Source: APNIC Labs measures the IPv6 capability of end users with online advertisements that fetch
test objects over IPv4 and IPv6, and publishes per economy (ISO 3166-1 alpha-2 code) and for the
world (XA) a JSON file with the daily results since October 2013 on data1.labs.apnic.net (a website;
robots.txt is respected). The files have about 4 MB each and are regenerated daily.

Stored values: the unsmoothed daily values (window ``raw``) of each day, as defined in the data
format documentation of APNIC Labs:
- ``daily_capable_pct``: share of the experiments that fetched an IPv6-only object (``capable_pc``),
- ``daily_preferred_pct``: share of the experiments that used IPv6 to fetch an object available over
  IPv4 and IPv6 (``preferred_pc``),
- ``daily_samples``: number of experiments (``seen``). For the world the source weights the
  economies with its model of internet users per economy, so the value is not an integer; it is
  rounded.
Percentages are rounded to PCT_DECIMALS decimal places (0.0001 percentage points), below the
sampling error of the measurement. Days without measurements are either absent from a file (66 days
in the German series) or have ``seen`` 0 (31 days in the German series); the latter are skipped.

Economies: the world and 11 economies with many internet users in all regions (ECONOMIES). Each
economy is a series (seen key: the economy code) with one record per run that holds the days after
the last stored day up to the day before yesterday, because the latest days can still change.

History: the first run imports the whole history (from 7 October 2013 for the world).

Schedule: monthly. At the run deadline the collector stops; the remaining economies follow in the
next run.

License: "(C) APNIC Pty/Ltd. re-use with attribution permitted"; the documentation provides the data
on a "hold harmless" basis with attribution.
"""
import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from collectors.base import Collector, CollectorResult, RunContext, series_start
from schema import Ipv6AdoptionRecord
from state import CollectorEntry, is_backfilled
from web import BlockedError, FetchError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "ipv6_adoption"
SOURCE_ID = "apnic_labs_ipv6"
LICENSE = "APNIC Labs IPv6 measurement (re-use with attribution permitted)"
ECONOMY_URL = "https://data1.labs.apnic.net/v6stats/v6economy/{economy}.json"
WORLD_URL = "https://data1.labs.apnic.net/v6stats/v6region/XA.json"
WORLD = "XA"
# The world and economies with many internet users in all regions
ECONOMIES: tuple[str, ...] = ("XA", "US", "CN", "IN", "DE", "JP", "GB", "FR", "BR", "KR", "ID", "NG")
# Unsmoothed daily values; the files also hold 10, 30, 60 and 90 day averages
WINDOW = "raw"
PCT_DECIMALS = 4
# Days are stored when they are at least this many days old (the latest days can still change)
SETTLED_AFTER_DAYS = 2
# Read timeout of one file of about 4 MB
DOWNLOAD_TIMEOUT_S = 120.0


class AdoptionFormatError(ValueError):
    """A measurement file does not have the expected structure."""


@dataclass
class DailyAdoption:
    """The daily values of one economy.

    Attributes:
        capable_pct (dict[date, float]): Share of IPv6-capable users in percent per day.
        preferred_pct (dict[date, float]): Share of IPv6-preferring users in percent per day.
        samples (dict[date, int]): Number of measured users per day.
        empty_days (int): Days without measurements, skipped.
        malformed (int): Entries that could not be read, skipped.
    """
    capable_pct: dict[date, float] = field(default_factory=dict)
    preferred_pct: dict[date, float] = field(default_factory=dict)
    samples: dict[date, int] = field(default_factory=dict)
    empty_days: int = 0
    malformed: int = 0


def economy_url(economy: str) -> str:
    """Returns the URL of the measurement file of an economy or of the world.

    Args:
        economy: ISO 3166-1 alpha-2 code, or XA for the world.

    Returns:
        str: The URL.
    """
    return WORLD_URL if economy == WORLD else ECONOMY_URL.format(economy=economy)


def _is_number(value: Any) -> bool:
    """Tells whether a JSON value is a finite number."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_adoption(data: Any) -> DailyAdoption:
    """Reads the unsmoothed daily values from a measurement file.

    Args:
        data: The parsed JSON file, an object with a ``data`` list of daily entries.

    Returns:
        DailyAdoption: The values per day, without days that have no measurements.

    Raises:
        AdoptionFormatError: If the file has no list of daily entries.
    """
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise AdoptionFormatError("the file has no list of daily entries")
    adoption = DailyAdoption()
    for item in items:
        raw = item.get(WINDOW) if isinstance(item, dict) else None
        if not isinstance(raw, dict):
            adoption.malformed += 1
            continue
        try:
            day = date.fromisoformat(str(item.get("date")))
        except ValueError:
            adoption.malformed += 1
            continue
        samples, capable, preferred = raw.get("seen"), raw.get("capable_pc"), raw.get("preferred_pc")
        if not (_is_number(samples) and _is_number(capable) and _is_number(preferred)):
            adoption.malformed += 1
            continue
        if samples <= 0:
            adoption.empty_days += 1
            continue
        adoption.capable_pct[day] = round(capable, PCT_DECIMALS)
        adoption.preferred_pct[day] = round(preferred, PCT_DECIMALS)
        adoption.samples[day] = round(samples)
    return adoption


class Ipv6AdoptionCollector(Collector):
    """Writes the new days of the IPv6 adoption of the world and the tracked economies."""
    name = "ipv6_adoption"
    datasets = (DATASET,)
    min_days_between_runs = 27

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Downloads the file of every economy and emits one record per economy with the new settled days.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; the progress per economy is kept in the seen store.
        """
        seen = ctx.seen(DATASET)
        end = ctx.today - timedelta(days=SETTLED_AFTER_DAYS)
        counters: dict[str, Any] = {"economies": len(ECONOMIES), "records": 0, "days": 0, "failed": 0,
                                    "empty_days": 0, "malformed": 0}
        complete = True
        error: Optional[str] = None
        requested = 0
        for position, economy in enumerate(ECONOMIES):
            # The first run imports the whole history
            start = series_start(seen.get(economy), date.min)
            if start > end:
                continue
            if ctx.past_deadline():
                counters["deferred"] = len(ECONOMIES) - position
                complete = False
                break
            url = economy_url(economy)
            requested += 1
            try:
                response = ctx.web.get(url, timeout_s=DOWNLOAD_TIMEOUT_S)
            except RateLimitError as e:
                logger.warning("Rate limit at %s; the remaining economies follow in the next run: %s", url, e)
                counters["deferred"] = len(ECONOMIES) - position
                complete = False
                break
            except BlockedError as e:
                logger.error("Request refused; no further requests in this run: %s", e)
                error = f"blocked: {e}"
                complete = False
                break
            except FetchError as e:
                logger.warning("IPv6 measurement of %s failed: %s", economy, e)
                counters["failed"] += 1
                continue
            try:
                adoption = parse_adoption(response.json())
            except ValueError as e:
                logger.warning("IPv6 measurement of %s cannot be read: %s", economy, e)
                counters["failed"] += 1
                continue
            counters["empty_days"] += adoption.empty_days
            counters["malformed"] += adoption.malformed
            days = sorted(day for day in adoption.samples if start <= day <= end)
            if not days:
                continue
            first, last = days[0], days[-1]
            # Each record covers new days, so it is a new item with revision 0
            seen.mark(economy, url, ctx.today, data={"last_day": last.isoformat()})
            record = Ipv6AdoptionRecord(
                **ctx.envelope(DATASET, f"ipv6:{economy}:{first.isoformat()}:{last.isoformat()}", revision=0,
                               source_id=SOURCE_ID, source_url=url, license=LICENSE,
                               backfilled=is_backfilled(first, entry.first_run)),
                economy=economy,
                daily_capable_pct={day: adoption.capable_pct[day] for day in days},
                daily_preferred_pct={day: adoption.preferred_pct[day] for day in days},
                daily_samples={day: adoption.samples[day] for day in days},
            )
            ctx.emit(DATASET, [record])
            counters["records"] += 1
            counters["days"] += len(days)
        if error is None and counters["failed"] and counters["failed"] == requested:
            error = f"all {requested} requested files failed"
        logger.info("IPv6 adoption: %d records with %d days, %d economies failed.", counters["records"],
                    counters["days"], counters["failed"])
        return CollectorResult(counters=counters, complete=complete, error=error)

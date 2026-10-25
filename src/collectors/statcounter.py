"""
Collects the monthly market shares of operating systems, browsers, search engines, device platforms and AI
chatbots measured by StatCounter Global Stats.

Source: every chart of gs.statcounter.com can be downloaded as CSV (``chart.php`` with ``csv=1``). The
data is licensed CC BY-SA 3.0, with a link to StatCounter as attribution.

Fetch strategy: seven statistics (operating systems of all devices, of desktops and of mobile devices,
browsers, search engines, device platforms, AI chatbots) for three regions (worldwide, Germany, United
States), one request per statistic and region, 21 requests per run. The CSV download is a website
request: robots.txt (gs.statcounter.com has none) and text-and-data-mining reservations are checked.
A CSV has one row per month ("YYYY-MM") and one column per category, with shares in percent. The
columns are the top categories of the requested period, so they vary with the range (about 20 for a
range of 18 years, fewer for one month); the category names are stored as given ("OS X" and "macOS"
are separate columns). Empty cells are skipped. Months before the start of a statistic are rows of
zeros at the end of the CSV (July to December 2008 for most statistics, March 2025 for AI chatbots in
the United States); a month without any positive share is not stored.

History: StatCounter revises the shares of a month for 45 days, so only months that ended at least 45
days before the run day are stored. A series (statistic and region) is requested from July 2008, the
first month of StatCounter Global Stats (AI chatbots: March 2025, tracked since 23 March 2025); each
run requests the months after the last stored month in one request.

Schedule: monthly. At the run deadline or a rate limit the collector stops; the remaining series follow
in the next run. A refusal (HTTP 401/403, challenge page) or a TDM reservation stops all requests of
the run and marks the run as failed.
"""
import csv
import io
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional
from urllib.parse import quote

from collectors.base import Collector, CollectorResult, RunContext
from schema import MarketShareRecord
from state import CollectorEntry, SeenEntry, content_hash, is_backfilled
from web import BlockedError, FetchError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "market_shares"
SOURCE_ID = "statcounter"
LICENSE = "CC-BY-SA-3.0 (StatCounter Global Stats, https://gs.statcounter.com)"
CHART_URL = "https://gs.statcounter.com/chart.php"
# First month of StatCounter Global Stats
HISTORY_START = date(2008, 7, 1)
# First month of the AI chatbot statistic (tracked since 23 March 2025)
AI_CHATBOT_START = date(2025, 3, 1)
# StatCounter revises the shares of a month for this many days after the end of the month
REVISION_DAYS = 45
# The CSV is generated on request; the CSV of an 18-year range took 16 s on 2026-10-25
CSV_TIMEOUT_S = 60.0
# device_hidden value and display name of all devices
ALL_DEVICES = ("desktop+mobile+tablet+console", "Desktop & Mobile & Tablet & Console")


@dataclass(frozen=True)
class Statistic:
    """A StatCounter chart, identified by the hidden form values of the website.

    Attributes:
        stat_type (str): Value of ``statType_hidden``, e.g. os_combined.
        stat_label (str): Display name of the statistic (``statType``).
        device (str): Value of ``device_hidden``; several devices are joined with "+".
        device_label (str): Display name of the devices (``device``).
        history_start (date): First month to request for a new series.
    """
    stat_type: str
    stat_label: str
    device: str
    device_label: str
    history_start: date = HISTORY_START


# Measured statistics by the id stored in the ``statistic`` field
STATISTICS: dict[str, Statistic] = {
    "os": Statistic("os_combined", "Operating System", *ALL_DEVICES),
    "desktop_os": Statistic("os_combined", "Operating System", "desktop", "Desktop"),
    "mobile_os": Statistic("os_combined", "Operating System", "mobile", "Mobile"),
    "browser": Statistic("browser", "Browser", *ALL_DEVICES),
    "search_engine": Statistic("search_engine", "Search Engine", *ALL_DEVICES),
    "platform": Statistic("comparison", "Platform", "desktop+mobile+tablet", "Desktop vs Mobile vs Tablet"),
    "ai_chatbot": Statistic("ai_chatbot", "AI Chatbot", *ALL_DEVICES, history_start=AI_CHATBOT_START),
}
# Display names of the regions by the code stored in the ``region`` field (``region_hidden``)
REGIONS: dict[str, str] = {"ww": "Worldwide", "DE": "Germany", "US": "United States of America"}


def add_months(month: date, count: int) -> date:
    """Returns the first day of the month ``count`` months after ``month``.

    Args:
        month: A day of the start month.
        count: Number of months; negative values go back.

    Returns:
        date: The first day of the resulting month.
    """
    index = month.year * 12 + month.month - 1 + count
    return date(index // 12, index % 12 + 1, 1)


def last_storable_month(today: date) -> date:
    """Returns the latest month that ended at least REVISION_DAYS days before ``today``.

    A month ends at the start of the next month; its shares are final REVISION_DAYS days later.

    Args:
        today: The run day.

    Returns:
        date: The first day of the latest month whose shares are no longer revised.
    """
    cutoff = today - timedelta(days=REVISION_DAYS)
    return add_months(cutoff.replace(day=1), -1)


def first_month_to_fetch(previous: Optional[SeenEntry], history_start: date) -> date:
    """Returns the first month to request for a series.

    Args:
        previous: Seen entry of the series; ``data["last_period"]`` holds the last stored month (YYYY-MM).
        history_start: First month of a new series.

    Returns:
        date: The first day of the month after the last stored month, or ``history_start``.
    """
    last = previous.data.get("last_period") if previous else None
    return add_months(datetime.strptime(last, "%Y-%m").date(), 1) if last else history_start


def chart_url(statistic: Statistic, region: str, start: date, end: date) -> str:
    """Builds the CSV download URL of a statistic, a region and a range of months.

    The parameters and their order are those of the download link of the website.

    Args:
        statistic: The statistic.
        region: Region code (key of REGIONS).
        start: First month.
        end: Last month.

    Returns:
        str: The URL.
    """
    params = (
        ("device", statistic.device_label), ("device_hidden", statistic.device), ("multi-device", "true"),
        ("statType_hidden", statistic.stat_type), ("region_hidden", region), ("granularity", "monthly"),
        ("statType", statistic.stat_label), ("region", REGIONS[region]),
        ("fromInt", f"{start:%Y%m}"), ("toInt", f"{end:%Y%m}"),
        ("fromMonthYear", f"{start:%Y-%m}"), ("toMonthYear", f"{end:%Y-%m}"), ("csv", "1"),
    )
    return CHART_URL + "?" + "&".join(f"{key}={quote(value, safe='')}" for key, value in params)


def parse_csv(text: str, start: date, end: date) -> dict[date, dict[str, float]]:
    """Reads the monthly shares from a StatCounter CSV.

    Args:
        text: The CSV: a header "Date" plus one column per category, and one row per month (YYYY-MM).
        start: First month to keep.
        end: Last month to keep.

    Returns:
        dict: Shares in percent per category, per month (first day of the month). Empty or
        non-numeric cells are skipped; months without any positive share are left out.

    Raises:
        ValueError: If the text is not a StatCounter CSV, e.g. an HTML page.
    """
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or header[0].strip() != "Date" or len(header) < 2:
        raise ValueError(f"not a StatCounter CSV (header {header[:3] if header else None!r})")
    categories = [name.strip() for name in header[1:]]
    shares: dict[date, dict[str, float]] = {}
    for row in reader:
        if not row or not row[0].strip():
            continue
        try:
            month = datetime.strptime(row[0].strip(), "%Y-%m").date()
        except ValueError:
            logger.warning("Skipped a StatCounter row with the date %r.", row[0])
            continue
        if not start <= month <= end:
            continue
        values: dict[str, float] = {}
        for category, cell in zip(categories, row[1:]):
            value = parse_share(cell)
            if value is not None:
                values[category] = value
        if any(value > 0 for value in values.values()):
            shares[month] = values
    return shares


def parse_share(cell: str) -> Optional[float]:
    """Reads a share in percent from a CSV cell.

    Args:
        cell: The cell text.

    Returns:
        Optional[float]: The share, or None for an empty, non-numeric, negative or non-finite cell.
    """
    try:
        value = float(cell)
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def fetch_csv(ctx: RunContext, url: str) -> str:
    """Downloads a CSV, respecting the text-and-data-mining reservations of the host.

    Args:
        ctx: The run context.
        url: The CSV URL.

    Returns:
        str: The CSV text.

    Raises:
        BlockedError: If the host refuses automated requests or reserves text and data mining.
        RateLimitError: If the host reports a rate limit.
        FetchError: For other failures.
    """
    if ctx.web.tdm_reserved(url):
        raise BlockedError(url, "text and data mining reserved (TDMRep)")
    response = ctx.web.get(url, timeout_s=CSV_TIMEOUT_S)
    if ctx.web.tdm_reserved(response.url, response):
        raise BlockedError(url, "text and data mining reserved (TDMRep)")
    return response.content.decode("utf-8-sig", errors="replace")


class StatCounterCollector(Collector):
    """Writes the new months of the StatCounter market shares per statistic and region."""
    name = "statcounter"
    datasets = (DATASET,)
    min_days_between_runs = 27

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Requests the months after the last stored month of every series and emits one record per series.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; the progress per series is kept in the seen store.
        """
        seen = ctx.seen(DATASET)
        end = last_storable_month(ctx.today)
        series = [(statistic_id, region) for statistic_id in STATISTICS for region in REGIONS]
        counters: dict[str, Any] = {"series": len(series), "last_storable_month": f"{end:%Y-%m}", "requested": 0,
                                    "records": 0, "months": 0, "up_to_date": 0, "failed": 0}
        complete = True
        error: Optional[str] = None
        for position, (statistic_id, region) in enumerate(series):
            statistic = STATISTICS[statistic_id]
            key = f"{statistic_id}:{region}"
            start = first_month_to_fetch(seen.get(key), statistic.history_start)
            if start > end:
                counters["up_to_date"] += 1
                continue
            if ctx.past_deadline():
                counters["deferred"] = len(series) - position
                complete = False
                break
            url = chart_url(statistic, region, start, end)
            counters["requested"] += 1
            try:
                shares = parse_csv(fetch_csv(ctx, url), start, end)
            except RateLimitError as e:
                logger.warning("StatCounter rate limit at %s; the remaining series follow in the next run: %s", key, e)
                counters["deferred"] = len(series) - position
                complete = False
                break
            except BlockedError as e:
                logger.error("StatCounter refused the request; no further requests in this run: %s", e)
                counters["deferred"] = len(series) - position
                error = f"blocked: {e}"
                complete = False
                break
            except (FetchError, ValueError) as e:
                logger.warning("StatCounter series %s failed: %s", key, e)
                counters["failed"] += 1
                continue
            if not shares:
                logger.info("StatCounter series %s has no shares from %s to %s.", key, f"{start:%Y-%m}", f"{end:%Y-%m}")
                continue
            first, last = min(shares), max(shares)
            record = MarketShareRecord(
                **ctx.envelope(
                    DATASET, f"statcounter:{statistic_id}:{region}:{first:%Y-%m}:{last:%Y-%m}",
                    revision=0, source_id=SOURCE_ID, source_url=url, license=LICENSE,
                    backfilled=is_backfilled(first, entry.first_run),
                ),
                statistic=statistic_id,
                region=region,
                monthly_shares=dict(sorted(shares.items())),
            )
            # Each record covers new months, so it is a new item with revision 0
            seen.mark(key, content_hash(record.model_dump(mode="json", include={"monthly_shares"})), ctx.today,
                      data={"last_period": f"{last:%Y-%m}"})
            ctx.emit(DATASET, [record])
            counters["records"] += 1
            counters["months"] += len(shares)
        failed = counters["failed"]
        if error is None and failed and failed == counters["requested"]:
            error = f"all {failed} requested StatCounter series failed"
        logger.info("StatCounter: %d records with %d months; %d series up to date, %d failed.", counters["records"],
                    counters["months"], counters["up_to_date"], failed)
        return CollectorResult(counters=counters, complete=complete, error=error)

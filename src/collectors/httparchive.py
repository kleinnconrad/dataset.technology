"""
Collects monthly metrics of the web from the reports of the HTTP Archive.

Source: the HTTP Archive crawls several million pages with a desktop and a mobile client, once a month
since 2019 and twice a month before, and publishes the timeseries of its reports as JSON files on its
CDN (``cdn.httparchive.org/v1/static/reports/<report>.json``). The data has no formal license; the
maintainers state that it is free to use and ask for attribution.

Fetch strategy: one request per report, five per run. The report files are documented public data files, so they are
requested without the robots.txt check (robots.txt allows all paths). A report is a list of entries
with the crawl date ("YYYY_MM_DD"), the client (desktop or mobile) and the value fields. The collector
stores pctHttps (share of requests over HTTPS), h2 (share of requests over HTTP/2) and h3 (share of
requests over HTTP/3) in percent, and the median (p50) of bytesTotal (page weight) and of bytesJs
(JavaScript bytes) in KB, as the metrics bytesTotal_p50 and bytesJs_p50. Values are stored as
reported, including the zeros of the crawls before a protocol was measured. Entries without a valid
date or value are skipped; the two clients are separate series, as a crawl can miss one of them.

History: a series (metric and client) gets the whole history on its first run (pctHttps and h2 from
January 2016, h3 from January 2020, byte metrics from November 2010 for desktop and May 2011 for
mobile); later runs store the crawls after the last stored crawl date.

Schedule: monthly. At the run deadline or a rate limit the collector stops; the remaining reports follow
in the next run. A refusal (HTTP 401/403) stops all requests of the run and marks the run as failed.
"""
import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from collectors.base import Collector, CollectorResult, RunContext
from schema import WebMetricRecord
from state import CollectorEntry, content_hash, is_backfilled
from web import BlockedError, FetchError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "web_metrics"
SOURCE_ID = "httparchive"
LICENSE = "HTTP Archive (free use, attribution requested)"
REPORT_URL = "https://cdn.httparchive.org/v1/static/reports/{report}.json"
CLIENTS = ("desktop", "mobile")


@dataclass(frozen=True)
class Metric:
    """A value of an HTTP Archive report that is stored as a metric.

    Attributes:
        report (str): Name of the report file.
        field (str): Field of the report entries that holds the value.
        unit (str): Unit of the value.
    """
    report: str
    field: str
    unit: str


# Stored metrics by the name in the ``metric`` field
METRICS: dict[str, Metric] = {
    "pctHttps": Metric("pctHttps", "percent", "percent"),
    "h2": Metric("h2", "percent", "percent"),
    "h3": Metric("h3", "percent", "percent"),
    "bytesTotal_p50": Metric("bytesTotal", "p50", "KB"),
    "bytesJs_p50": Metric("bytesJs", "p50", "KB"),
}


def report_url(report: str) -> str:
    """Returns the URL of the timeseries file of a report."""
    return REPORT_URL.format(report=report)


def parse_crawl_date(value: Any) -> Optional[date]:
    """Parses a crawl date such as "2016_01_15".

    Args:
        value: The date field of a report entry.

    Returns:
        Optional[date]: The date, or None if the value is not a date.
    """
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip().replace("_", "-"))
    except ValueError:
        return None


def parse_value(value: Any) -> Optional[float]:
    """Parses a numeric value of a report entry.

    Args:
        value: The value field; numbers and numeric strings are accepted.

    Returns:
        Optional[float]: The value, or None if it is missing, not numeric or not finite.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_report(data: Any, field: str) -> dict[str, dict[date, float]]:
    """Reads the values of one field per client and crawl date from a report.

    Args:
        data: The parsed JSON of the report.
        field: The value field, e.g. percent or p50.

    Returns:
        dict: Values per crawl date, per client (desktop, mobile).

    Raises:
        ValueError: If the report is not a list of entries.
    """
    if not isinstance(data, list):
        raise ValueError(f"the report is a {type(data).__name__}, not a list of entries")
    values: dict[str, dict[date, float]] = {client: {} for client in CLIENTS}
    for item in data:
        if not isinstance(item, dict) or item.get("client") not in values:
            continue
        crawl = parse_crawl_date(item.get("date"))
        value = parse_value(item.get(field))
        if crawl is not None and value is not None:
            values[item["client"]][crawl] = value
    return values


class HttpArchiveCollector(Collector):
    """Writes the new crawls of the HTTP Archive metrics per metric and client."""
    name = "httparchive"
    datasets = (DATASET,)
    min_days_between_runs = 27

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Requests every report and emits one record per metric and client with the crawls after the last stored one.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; the progress per series is kept in the seen store.
        """
        seen = ctx.seen(DATASET)
        metrics = list(METRICS.items())
        counters: dict[str, Any] = {"metrics": len(metrics), "requested": 0, "records": 0, "values": 0, "failed": 0}
        complete = True
        error: Optional[str] = None
        for position, (metric_id, metric) in enumerate(metrics):
            if ctx.past_deadline():
                counters["deferred"] = len(metrics) - position
                complete = False
                break
            url = report_url(metric.report)
            counters["requested"] += 1
            try:
                by_client = parse_report(ctx.web.get(url, respect_robots=False).json(), metric.field)
            except RateLimitError as e:
                logger.warning("HTTP Archive rate limit at %s; the remaining reports follow in the next run: %s",
                               metric.report, e)
                counters["deferred"] = len(metrics) - position
                complete = False
                break
            except BlockedError as e:
                logger.error("HTTP Archive refused the request; no further requests in this run: %s", e)
                counters["deferred"] = len(metrics) - position
                error = f"blocked: {e}"
                complete = False
                break
            except (FetchError, ValueError) as e:
                logger.warning("HTTP Archive report %s failed: %s", metric.report, e)
                counters["failed"] += 1
                continue
            new: list[tuple[str, WebMetricRecord]] = []
            for client in CLIENTS:
                key = f"{metric_id}:{client}"
                previous = seen.get(key)
                last_stored = previous.data.get("last_period") if previous else None
                after = date.fromisoformat(last_stored) if last_stored else date.min
                values = {crawl: value for crawl, value in by_client[client].items() if crawl > after}
                if not values:
                    continue
                first, last = min(values), max(values)
                new.append((key, WebMetricRecord(
                    **ctx.envelope(
                        DATASET, f"httparchive:{metric_id}:{client}:{first.isoformat()}:{last.isoformat()}",
                        revision=0, source_id=SOURCE_ID, source_url=url, license=LICENSE,
                        backfilled=is_backfilled(first, entry.first_run),
                    ),
                    metric=metric_id,
                    client=client,
                    unit=metric.unit,
                    monthly_values=dict(sorted(values.items())),
                )))
            # Each record covers new crawls, so it is a new item with revision 0
            for key, record in new:
                seen.mark(key, content_hash(record.model_dump(mode="json", include={"monthly_values"})), ctx.today,
                          data={"last_period": max(record.monthly_values).isoformat()})
                counters["values"] += len(record.monthly_values)
            ctx.emit(DATASET, [record for _key, record in new])
            counters["records"] += len(new)
        failed = counters["failed"]
        if error is None and failed and failed == counters["requested"]:
            error = f"all {failed} requested HTTP Archive reports failed"
        logger.info("HTTP Archive: %d records with %d values; %d reports failed.", counters["records"],
                    counters["values"], failed)
        return CollectorResult(counters=counters, complete=complete, error=error)

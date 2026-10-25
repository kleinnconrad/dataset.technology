"""
Collects daily counts of the CVE records published by the NVD and the CISA catalog of Known Exploited Vulnerabilities.

Sources:
- The CVE API 2.0 of the National Vulnerability Database (NVD), a documented
  API in the public domain. It allows 5 requests per rolling 30 seconds
  without an API key and 50 with a key (``NVD_API_KEY``); the collector sets
  the request interval of the host accordingly.
- The Known Exploited Vulnerabilities (KEV) catalog of CISA (CC0-1.0), a JSON
  file on www.cisa.gov, fetched as a website file with a conditional request
  that sends both the ETag and the Last-Modified time of the previous file
  (cisa.gov answers If-Modified-Since with HTTP 304 but ignores If-None-Match).
  If the file cannot be fetched, the copy that CISA publishes on GitHub
  (cisagov/kev-data) is read instead.

``cve_daily_counts`` stores one record per publication day (UTC) with counts
of the CVE records that the NVD published that day. Per-CVE records are not
stored, because about 135,000 CVE records are published per year. A day is
counted once, in the first run after it ended; later analysis by the NVD is
not reflected. Counting rules:
- The CVSS score of a version family (v3.x, v4.0) is the NVD's own (source
  nvd@nist.gov) where present, else the first score of type Primary, else the
  first of type Secondary. Type Primary does not mean that the NVD scored the
  record; most Primary scores come from the CNA or other sources.
- Weaknesses are the values of all sources, including NVD-CWE-noinfo and
  NVD-CWE-Other, counted once per record.
- Vendors come from the vulnerable CPE matches of the NVD configurations, or,
  for records without them, from the affected data of the record (vendor
  names of the CNA), in lower case and counted once per record.
- Rejected records count only in ``cves``, ``rejected`` and ``by_status``.
- Descriptions are not used.

Each run fetches the complete UTC days from the day after the last stored day
to yesterday, with all pages of query windows of at most 120 days (the API
limit). The first run fetches the last 7 days. A backfill run also fetches the
days from 2024-01-01 to the first stored day, newest window first, so that the
stored days stay one contiguous range (cursor ``first_day`` to ``last_day``).

``known_exploited_vulnerabilities`` stores a catalog entry when it appears,
again with the next revision when one of its CISA fields changes, and once
with ``is_listed`` false when it disappears from the catalog. Descriptions,
required actions and notes are not stored. A new entry is enriched once with
the publication time and the CVSS base score of its CVE record in the NVD
(single requests, or the NVD ``hasKev`` listing in a backfill run or when
more than 50 entries are new); later changes of the NVD data do not create
revisions. While the NVD fails, a new entry added in the last 7 days waits for
the next run; an older one is stored without these values. The last stored
values of an entry are kept in its seen entry, so that a revision and the
record of a removed entry can be written without the NVD.

The collector runs in every run. At the run deadline or at an NVD rate limit it
stops; the remaining days and catalog entries follow in the next run.
"""
import logging
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from collectors.base import Collector, CollectorResult, RunContext
from schema import CveDailyCountRecord, KnownExploitedVulnerabilityRecord
from state import SEEN_DIR, CollectorEntry, SeenStore, content_hash, is_backfilled
from web import FetchError, NotFoundError, RateLimitError, Response

logger = logging.getLogger(__name__)

COUNTS_DATASET = "cve_daily_counts"
KEV_DATASET = "known_exploited_vulnerabilities"
DATASETS = (COUNTS_DATASET, KEV_DATASET)
NVD_SOURCE_ID = "nvd_cve_api"
KEV_SOURCE_ID = "cisa_kev"
LICENSE_NVD = ("Public domain (NVD). This product uses data from the NVD API but is not endorsed or certified "
               "by the NVD.")
# KEV records keep the KEV license also when they carry values from the NVD
LICENSE_KEV = "CC0-1.0 (CISA KEV)"
NVD_API_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"
NVD_HOST = "services.nvd.nist.gov"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
KEV_MIRROR_URL = "https://raw.githubusercontent.com/cisagov/kev-data/develop/known_exploited_vulnerabilities.json"

# NVD rate limits: 5 requests per rolling 30 seconds without an API key (NVD recommends a pause of
# 6 seconds), 50 requests per rolling 30 seconds with a key
NVD_INTERVAL_S = 6.5
NVD_INTERVAL_WITH_KEY_S = 0.7
# A page of 2000 CVE records is about 8.8 MB (1.3 MB compressed) and takes a few seconds
NVD_TIMEOUT_S = 120.0
NVD_PAGE_SIZE = 2000
# Longest publication date range of one API query
MAX_WINDOW_DAYS = 120
NVD_SCORE_SOURCE = "nvd@nist.gov"
CVSS_V3_KEYS = ("cvssMetricV31", "cvssMetricV30")
CVSS_V4_KEYS = ("cvssMetricV40",)
REJECTED_STATUS = "Rejected"
EXPLOIT_TAG = "Exploit"
# Days fetched by the first run; a backfill run fetches the days from HISTORY_START
FIRST_RUN_DAYS = 7
HISTORY_START = date(2024, 1, 1)

KEV_TIMEOUT_S = 60.0
# New catalog entries enriched with single NVD requests per run; with more new entries (or in a
# backfill run) the hasKev listing of the NVD is read first
MAX_SINGLE_LOOKUPS = 50
# While the NVD fails, new entries added in this many days before the run wait for the next run instead of
# being written without NVD values
KEV_MAX_DEFER_DAYS = 7
# If more known entries than this share (and more than MIN_UNLISTED_GUARD) are missing from the catalog
# at once, the removals are not recorded, because a damaged file is more likely than a mass removal
MAX_UNLISTED_SHARE = 0.05
MIN_UNLISTED_GUARD = 10
KEV_FIELDS = ("vendor_project", "product", "vulnerability_name", "date_added", "due_date",
              "known_ransomware_use", "cwe_ids")
NVD_FIELDS = ("cve_published_at", "cvss_base_score", "cvss_version", "cvss_source")
NO_NVD_VALUES: dict[str, Any] = dict.fromkeys(NVD_FIELDS)
CVE_ID_PATTERN = re.compile(r"^CVE-(\d{4})-\d{4,}$")
# Components of a CPE 2.3 formatted string are separated by colons that are not escaped with a backslash
CPE_SEPARATOR = re.compile(r"(?<!\\):")
CPE_ESCAPE = re.compile(r"\\(.)")
CPE_ANY_VALUES = frozenset({"", "*", "-"})
RANSOMWARE_USE = {"known": True, "unknown": False}


class TdmReservedError(Exception):
    """The host of a website file declares a text-and-data-mining reservation."""


def _as_dict(value: Any) -> dict[str, Any]:
    """Returns the value if it is a dict, else an empty dict."""
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """Returns the value if it is a list, else an empty list."""
    return value if isinstance(value, list) else []


def _text(value: Any) -> Optional[str]:
    """Returns a string with collapsed whitespace, or None for empty and non-string values."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text or None


def _iso_date(value: Any) -> Optional[date]:
    """Parses a YYYY-MM-DD date, returning None for missing or invalid values."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _number(value: Any) -> Optional[float]:
    """Returns a JSON number as float, or None for other values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# -- NVD URLs ---------------------------------------------------------------


def nvd_url(params: Mapping[str, Optional[object]]) -> str:
    """Builds a URL of the NVD CVE API.

    Args:
        params: Query parameters; a parameter with the value None is sent as a flag without value (e.g. ``hasKev``).

    Returns:
        str: The URL; colons in the values are not escaped, so that dates stay readable.
    """
    parts = [key if value is None else urlencode({key: value}, safe=":") for key, value in params.items()]
    return f"{NVD_API_URL}?{'&'.join(parts)}"


def _day_start(day: date) -> str:
    """Returns the first millisecond of a UTC day in the date format of the NVD API."""
    return f"{day.isoformat()}T00:00:00.000"


def _day_end(day: date) -> str:
    """Returns the last millisecond of a UTC day in the date format of the NVD API."""
    return f"{day.isoformat()}T23:59:59.999"


def window_url(start: date, end: date, start_index: int) -> str:
    """Returns the URL of one page of the CVE records published from ``start`` to ``end`` (UTC days, inclusive).

    Args:
        start: First day.
        end: Last day; at most MAX_WINDOW_DAYS - 1 days after ``start``.
        start_index: Index of the first record of the page.

    Returns:
        str: The URL.
    """
    return nvd_url({"pubStartDate": _day_start(start), "pubEndDate": _day_end(end),
                    "resultsPerPage": NVD_PAGE_SIZE, "startIndex": start_index})


def day_url(day: date) -> str:
    """Returns the URL of the CVE records published on one day, used as ``source_url`` of a day record."""
    return nvd_url({"pubStartDate": _day_start(day), "pubEndDate": _day_end(day)})


def cve_url(cve_id: str) -> str:
    """Returns the URL of one CVE record."""
    return nvd_url({"cveId": cve_id})


def kev_listing_url(start_index: int) -> str:
    """Returns the URL of one page of the CVE records that the NVD lists as being in the KEV catalog."""
    return nvd_url({"hasKev": None, "resultsPerPage": NVD_PAGE_SIZE, "startIndex": start_index})


def day_windows(start: date, end: date, size: int = MAX_WINDOW_DAYS) -> list[tuple[date, date]]:
    """Splits a range of days into consecutive windows.

    Args:
        start: First day.
        end: Last day (inclusive).
        size: Maximum number of days per window.

    Returns:
        list: ``(first day, last day)`` per window in chronological order; empty if ``start`` is after ``end``.
    """
    windows = []
    while start <= end:
        window_end = min(start + timedelta(days=size - 1), end)
        windows.append((start, window_end))
        start = window_end + timedelta(days=1)
    return windows


# -- Parsing of CVE records -------------------------------------------------


def metric_entries(metrics: Mapping[str, Any], keys: Iterable[str]) -> list[dict[str, Any]]:
    """Returns the CVSS entries of the given metric lists in order.

    Args:
        metrics: The ``metrics`` object of a CVE record.
        keys: Names of the metric lists, e.g. ``cvssMetricV31``.

    Returns:
        list[dict]: The entries.
    """
    return [metric for key in keys for metric in _as_list(metrics.get(key)) if isinstance(metric, dict)]


def _metric_rank(metric: Mapping[str, Any]) -> int:
    """Orders CVSS entries: the NVD's own score, then Primary, then Secondary, then entries without type."""
    if metric.get("source") == NVD_SCORE_SOURCE:
        return 0
    return {"Primary": 1, "Secondary": 2}.get(str(metric.get("type")), 3)


def select_metric(metrics: Mapping[str, Any], keys: Iterable[str]) -> Optional[dict[str, Any]]:
    """Chooses the CVSS entry of a version family.

    Args:
        metrics: The ``metrics`` object of a CVE record.
        keys: Names of the metric lists of the family, in order of preference.

    Returns:
        Optional[dict]: The entry from nvd@nist.gov, else the first of type Primary,
        else the first of type Secondary; None if the family has no entry.
    """
    entries = metric_entries(metrics, keys)
    return min(entries, key=_metric_rank) if entries else None


def severity_from_score(score: float) -> str:
    """Returns the qualitative severity of a CVSS v3.x or v4.0 base score."""
    if score <= 0:
        return "NONE"
    if score < 4.0:
        return "LOW"
    if score < 7.0:
        return "MEDIUM"
    if score < 9.0:
        return "HIGH"
    return "CRITICAL"


def metric_severity(metric: Mapping[str, Any]) -> Optional[str]:
    """Returns the base severity of a CVSS entry in upper case, derived from the score if it is not stated.

    Args:
        metric: A CVSS entry with ``cvssData``.

    Returns:
        Optional[str]: E.g. HIGH; None if the entry has neither severity nor score.
    """
    cvss = _as_dict(metric.get("cvssData"))
    severity = _text(cvss.get("baseSeverity")) or _text(metric.get("baseSeverity"))
    if severity:
        return severity.upper()
    score = _number(cvss.get("baseScore"))
    return severity_from_score(score) if score is not None else None


def weakness_ids(cve: Mapping[str, Any]) -> set[str]:
    """Returns the weakness values of all sources of a CVE record, e.g. CWE-79 or NVD-CWE-noinfo."""
    values = set()
    for weakness in _as_list(cve.get("weaknesses")):
        for description in _as_list(_as_dict(weakness).get("description")):
            value = _text(_as_dict(description).get("value"))
            if value:
                values.add(value)
    return values


def cpe_vendor(criteria: Any) -> Optional[str]:
    """Returns the vendor component of a CPE 2.3 formatted string.

    Args:
        criteria: E.g. ``cpe:2.3:a:google:chrome:*:*:*:*:*:*:*:*``.

    Returns:
        Optional[str]: The vendor in lower case without escape characters; None for invalid
        strings and for the values ANY (*) and NA (-).
    """
    if not isinstance(criteria, str):
        return None
    parts = CPE_SEPARATOR.split(criteria)
    if len(parts) < 5 or parts[0] != "cpe" or parts[1] != "2.3":
        return None
    vendor = CPE_ESCAPE.sub(r"\1", parts[3]).strip().lower()
    return None if vendor in CPE_ANY_VALUES else vendor


def affected_vendors(cve: Mapping[str, Any]) -> set[str]:
    """Returns the vendors of the affected products of a CVE record.

    Args:
        cve: The ``cve`` object of the API.

    Returns:
        set[str]: Vendors of the vulnerable CPE matches of the NVD configurations;
        if there are none, the vendor names of the affected data; all in lower case.
    """
    vendors = set()
    for configuration in _as_list(cve.get("configurations")):
        for node in _as_list(_as_dict(configuration).get("nodes")):
            for match in _as_list(_as_dict(node).get("cpeMatch")):
                match = _as_dict(match)
                vendor = cpe_vendor(match.get("criteria")) if match.get("vulnerable") is True else None
                if vendor:
                    vendors.add(vendor)
    if vendors:
        return vendors
    for affected in _as_list(cve.get("affected")):
        for product in _as_list(_as_dict(affected).get("affectedData")):
            vendor = _text(_as_dict(product).get("vendor"))
            if vendor:
                vendors.add(vendor.lower())
    return vendors


def has_exploit_reference(cve: Mapping[str, Any]) -> bool:
    """Tells whether a reference of a CVE record is tagged Exploit."""
    return any(EXPLOIT_TAG in _as_list(_as_dict(reference).get("tags"))
               for reference in _as_list(cve.get("references")))


def published_day(cve: Mapping[str, Any]) -> Optional[date]:
    """Returns the UTC day on which the NVD published a CVE record."""
    return _iso_date(cve.get("published"))


def nvd_time(value: Any) -> Optional[datetime]:
    """Parses a timestamp of the NVD API (UTC without offset) to an aware datetime without fractions of a second."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return parsed.replace(microsecond=0)


@dataclass
class DayCounts:
    """Counts of the CVE records published on one day; the fields of a ``CveDailyCountRecord``."""
    cves: int = 0
    rejected: int = 0
    by_status: Counter[str] = field(default_factory=Counter)
    by_cvss_v3_severity: Counter[str] = field(default_factory=Counter)
    by_cvss_v4_severity: Counter[str] = field(default_factory=Counter)
    unscored: int = 0
    nvd_scored: int = 0
    by_cwe: Counter[str] = field(default_factory=Counter)
    by_vendor: Counter[str] = field(default_factory=Counter)
    with_exploit_reference: int = 0
    kev_listed: int = 0

    def add(self, cve: Mapping[str, Any]) -> None:
        """Counts one CVE record.

        Args:
            cve: The ``cve`` object of the API.
        """
        self.cves += 1
        status = _text(cve.get("vulnStatus")) or "Unknown"
        self.by_status[status] += 1
        if status == REJECTED_STATUS:
            self.rejected += 1
            return
        metrics = _as_dict(cve.get("metrics"))
        v3 = select_metric(metrics, CVSS_V3_KEYS)
        v4 = select_metric(metrics, CVSS_V4_KEYS)
        for metric, counter in ((v3, self.by_cvss_v3_severity), (v4, self.by_cvss_v4_severity)):
            severity = metric_severity(metric) if metric else None
            if severity:
                counter[severity] += 1
        if v3 is None and v4 is None:
            self.unscored += 1
        scores = metric_entries(metrics, CVSS_V3_KEYS + CVSS_V4_KEYS)
        if any(metric.get("source") == NVD_SCORE_SOURCE for metric in scores):
            self.nvd_scored += 1
        self.by_cwe.update(weakness_ids(cve))
        self.by_vendor.update(affected_vendors(cve))
        if has_exploit_reference(cve):
            self.with_exploit_reference += 1
        if cve.get("cisaExploitAdd"):
            self.kev_listed += 1

    def fields(self) -> dict[str, Any]:
        """Returns the counts as fields of a ``CveDailyCountRecord``, with the maps sorted by key."""
        return {
            "cves": self.cves, "rejected": self.rejected,
            "by_status": dict(sorted(self.by_status.items())),
            "by_cvss_v3_severity": dict(sorted(self.by_cvss_v3_severity.items())),
            "by_cvss_v4_severity": dict(sorted(self.by_cvss_v4_severity.items())),
            "unscored": self.unscored, "nvd_scored": self.nvd_scored,
            "by_cwe": dict(sorted(self.by_cwe.items())),
            "by_vendor": dict(sorted(self.by_vendor.items())),
            "with_exploit_reference": self.with_exploit_reference, "kev_listed": self.kev_listed,
        }


def nvd_values(cve: Mapping[str, Any]) -> dict[str, Any]:
    """Returns the NVD values of a KEV record from a CVE record.

    Args:
        cve: The ``cve`` object of the API.

    Returns:
        dict: ``cve_published_at`` (ISO string) and the CVSS base score, version and source,
        from v3.x if present, else from v4.0, chosen like the scores of the daily counts.
    """
    metrics = _as_dict(cve.get("metrics"))
    metric = select_metric(metrics, CVSS_V3_KEYS) or select_metric(metrics, CVSS_V4_KEYS)
    cvss = _as_dict(metric.get("cvssData")) if metric else {}
    published = nvd_time(cve.get("published"))
    version = cvss.get("version")
    return {
        "cve_published_at": published.isoformat() if published else None,
        "cvss_base_score": _number(cvss.get("baseScore")),
        "cvss_version": str(version) if version is not None else None,
        "cvss_source": None if metric is None else ("nvd" if metric.get("source") == NVD_SCORE_SOURCE else "other"),
    }


# -- Parsing of the KEV catalog ---------------------------------------------


def kev_fields(item: Any) -> Optional[tuple[str, dict[str, Any]]]:
    """Returns the stored fields of a catalog entry.

    Args:
        item: An entry of ``vulnerabilities`` in the KEV file.

    Returns:
        Optional[tuple[str, dict]]: The CVE id and the JSON-compatible tracked fields; None if the
        entry has no valid CVE id or date of addition. Descriptions, required actions and notes are left out.
    """
    item = _as_dict(item)
    cve_id = _text(item.get("cveID"))
    date_added = _iso_date(item.get("dateAdded"))
    if cve_id is None or not CVE_ID_PATTERN.match(cve_id) or date_added is None:
        return None
    due_date = _iso_date(item.get("dueDate"))
    ransomware = _text(item.get("knownRansomwareCampaignUse"))
    cwes = {cwe for cwe in (_text(value) for value in _as_list(item.get("cwes"))) if cwe}
    return cve_id, {
        "vendor_project": _text(item.get("vendorProject")) or "",
        "product": _text(item.get("product")) or "",
        "vulnerability_name": _text(item.get("vulnerabilityName")) or "",
        "date_added": date_added.isoformat(),
        "due_date": due_date.isoformat() if due_date else None,
        "known_ransomware_use": RANSOMWARE_USE.get(ransomware.lower()) if ransomware else None,
        "cwe_ids": sorted(cwes),
    }


def kev_hash(fields: Mapping[str, Any], listed: bool) -> str:
    """Returns the hash of the tracked fields of a catalog entry and its listing state."""
    return content_hash({**fields, "is_listed": listed})


def kev_shard(cve_id: str) -> str:
    """Returns the seen-store shard of a catalog entry: the year of its CVE id."""
    match = CVE_ID_PATTERN.match(cve_id)
    return match.group(1) if match else "other"


@dataclass(frozen=True)
class KevCatalog:
    """A fetched KEV file.

    Attributes:
        url (str): URL the file was read from.
        etag (Optional[str]): ETag of the response from cisa.gov; None for the mirror.
        last_modified (Optional[str]): Last-Modified header of the response from cisa.gov; None for the mirror.
        data (dict): The parsed JSON.
    """
    url: str
    etag: Optional[str]
    last_modified: Optional[str]
    data: dict[str, Any]


def fetch_website_file(ctx: RunContext, url: str, headers: Optional[Mapping[str, str]] = None) -> Response:
    """Fetches a website file with the robots.txt and TDMRep rules.

    Args:
        ctx: The run context.
        url: The URL.
        headers: Additional request headers, e.g. ``If-None-Match``.

    Returns:
        Response: The response; status 304 for an unchanged file.

    Raises:
        TdmReservedError: If the host reserves text and data mining for the URL.
        FetchError: If the request fails.
    """
    if ctx.web.tdm_reserved(url):
        raise TdmReservedError(url)
    response = ctx.web.get(url, headers=headers, timeout_s=KEV_TIMEOUT_S)
    if ctx.web.tdm_reserved(response.url, response):
        raise TdmReservedError(url)
    return response


def _catalog_json(response: Response) -> dict[str, Any]:
    """Parses a KEV file.

    Raises:
        ValueError: If the body is not a JSON object.
    """
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"KEV file is not a JSON object: {response.url}")
    return data


def fetch_catalog(ctx: RunContext, etag: Optional[str], last_modified: Optional[str]) -> Optional[KevCatalog]:
    """Fetches the KEV catalog from cisa.gov, or from the GitHub copy of CISA if cisa.gov fails.

    Args:
        ctx: The run context.
        etag: ETag of the file read in a previous run, sent as If-None-Match.
        last_modified: Last-Modified time of that file, sent as If-Modified-Since.

    Returns:
        Optional[KevCatalog]: The catalog; None if cisa.gov reports it as unchanged (HTTP 304).

    Raises:
        TdmReservedError: If a host reserves text and data mining.
        FetchError: If both URLs fail.
        ValueError: If the file is not valid JSON.
    """
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        response = fetch_website_file(ctx, KEV_URL, headers or None)
    except FetchError as e:
        logger.warning("KEV catalog at cisa.gov failed (%s); reading the copy of CISA on GitHub instead.", e)
        return KevCatalog(KEV_MIRROR_URL, None, None, _catalog_json(fetch_website_file(ctx, KEV_MIRROR_URL)))
    if response.status == 304:
        return None
    return KevCatalog(KEV_URL, response.headers.get("etag"), response.headers.get("last-modified"),
                      _catalog_json(response))


def stored_shards(ctx: RunContext, dataset: str) -> set[str]:
    """Returns the names of the seen-store shards of a dataset that exist on disk."""
    folder = ctx.settings.state_dir / SEEN_DIR / dataset
    return {path.stem for path in folder.glob("*.json")} if folder.is_dir() else set()


# -- Collector --------------------------------------------------------------


class NvdClient:
    """Sends the requests of a run to the NVD CVE API and remembers its first failure.

    Attributes:
        requests (int): Requests sent.
        failure (Optional[Exception]): The first failure; no further requests are sent after it.
    """

    def __init__(self, ctx: RunContext) -> None:
        """Sets the request interval of the NVD host for the API key of the run.

        Args:
            ctx: The run context.
        """
        key = ctx.settings.nvd_api_key
        ctx.web.set_host_interval(NVD_HOST, NVD_INTERVAL_WITH_KEY_S if key else NVD_INTERVAL_S)
        self._ctx = ctx
        self._headers = {"apiKey": key} if key else None
        self.requests = 0
        self.failure: Optional[Exception] = None

    @property
    def available(self) -> bool:
        """True until a request of the run failed."""
        return self.failure is None

    def get(self, url: str) -> dict[str, Any]:
        """Sends one request.

        Args:
            url: An NVD API URL.

        Returns:
            dict: The parsed response.

        Raises:
            RateLimitError: If the API reports a rate limit.
            NotFoundError: If the API rejects the parameters (HTTP 404, reason in the ``message`` header).
            FetchError: For other failures.
            ValueError: If the response is not a JSON object.
        """
        self.requests += 1
        data = self._ctx.web.get(url, headers=self._headers, respect_robots=False, timeout_s=NVD_TIMEOUT_S).json()
        if not isinstance(data, dict):
            raise ValueError(f"NVD response is not a JSON object: {url}")
        return data

    def fail(self, error: Exception) -> Optional[str]:
        """Records a failure, after which the run sends no further NVD requests.

        Args:
            error: The failure.

        Returns:
            Optional[str]: Error message for the run log; None for a rate limit, which only defers the work.
        """
        self.failure = error
        if isinstance(error, RateLimitError):
            logger.warning("NVD rate limit reached (%s); the remaining NVD requests follow in the next run.", error)
            return None
        logger.error("NVD API failed: %s", error)
        return f"NVD API failed: {error}"


@dataclass
class PartOutcome:
    """Outcome of one dataset of the collector.

    Attributes:
        complete (bool): False if work remains for the next run.
        error (Optional[str]): Error message for the run log.
    """
    complete: bool = True
    error: Optional[str] = None


class VulnerabilitiesCollector(Collector):
    """Writes the daily counts of new CVE records and the changes of the CISA KEV catalog."""
    name = "vulnerabilities"
    datasets = DATASETS
    min_days_between_runs = 0

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Processes the KEV catalog, then counts the CVE records of the days since the last stored day.

        Args:
            ctx: The run context.
            entry: Run history of this collector; the cursor holds ``first_day`` and ``last_day`` of the stored
                days and ``kev_etag`` and ``kev_last_modified`` of the last processed KEV file.

        Returns:
            CollectorResult: The new cursor and the counters.
        """
        nvd = NvdClient(ctx)
        cursor = dict(entry.cursor)
        counters: dict[str, Any] = {}
        kev = self._collect_kev(ctx, entry, nvd, cursor, counters)
        counts = self._collect_counts(ctx, entry, nvd, cursor, counters)
        counters["nvd_requests"] = nvd.requests
        errors = [part.error for part in (kev, counts) if part.error]
        return CollectorResult(cursor=cursor, counters=counters, complete=kev.complete and counts.complete,
                               error="; ".join(errors) or None)

    # -- daily counts -------------------------------------------------------

    def _collect_counts(self, ctx: RunContext, entry: CollectorEntry, nvd: NvdClient, cursor: dict[str, Any],
                        counters: dict[str, Any]) -> PartOutcome:
        """Counts the CVE records of the days to fetch, one query window after the other.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            nvd: The NVD client of the run.
            cursor: The cursor; ``first_day`` and ``last_day`` are updated after each window.
            counters: Counters of the run.

        Returns:
            PartOutcome: Whether all days were fetched.
        """
        yesterday = ctx.today - timedelta(days=1)
        first_day = _iso_date(cursor.get("first_day"))
        last_day = _iso_date(cursor.get("last_day"))
        if last_day is None:
            start = HISTORY_START if ctx.backfill else yesterday - timedelta(days=FIRST_RUN_DAYS - 1)
        else:
            start = last_day + timedelta(days=1)
        windows = [(window, True) for window in day_windows(start, yesterday)]
        if ctx.backfill and first_day is not None and first_day > HISTORY_START:
            gap = day_windows(HISTORY_START, first_day - timedelta(days=1))
            windows.extend((window, False) for window in reversed(gap))
        counters.update({"cve_days": 0, "cves": 0})
        for position, ((window_start, window_end), forward) in enumerate(windows):
            if not nvd.available:
                counters["deferred_windows"] = len(windows) - position
                return PartOutcome(complete=False)
            try:
                counts = self._count_window(ctx, nvd, window_start, window_end)
            except (FetchError, ValueError) as e:
                counters["deferred_windows"] = len(windows) - position
                return PartOutcome(complete=False, error=nvd.fail(e))
            if counts is None:
                logger.info("Run deadline reached; %d windows of CVE days follow in the next run.",
                            len(windows) - position)
                counters["deferred_windows"] = len(windows) - position
                return PartOutcome(complete=False)
            records = [self._day_record(ctx, entry, day, day_counts) for day, day_counts in sorted(counts.items())]
            ctx.emit(COUNTS_DATASET, records)
            counters["cve_days"] += len(records)
            counters["cves"] += sum(day_counts.cves for day_counts in counts.values())
            if forward:
                cursor["last_day"] = window_end.isoformat()
                cursor.setdefault("first_day", window_start.isoformat())
            else:
                cursor["first_day"] = window_start.isoformat()
        logger.info("CVE daily counts: %d days with %d CVE records.", counters["cve_days"], counters["cves"])
        return PartOutcome()

    @staticmethod
    def _count_window(ctx: RunContext, nvd: NvdClient, start: date, end: date) -> Optional[dict[date, DayCounts]]:
        """Fetches all pages of a query window and counts the CVE records per publication day.

        Args:
            ctx: The run context.
            nvd: The NVD client of the run.
            start: First day of the window.
            end: Last day of the window.

        Returns:
            Optional[dict[date, DayCounts]]: Counts for every day of the window, including days
            without records; None if the run deadline was reached before the window was complete.

        Raises:
            FetchError: If a request fails or a page is empty before the end of the results.
            ValueError: If a response has no valid ``totalResults``.
        """
        counts = {start + timedelta(days=offset): DayCounts() for offset in range((end - start).days + 1)}
        start_index = 0
        outside = 0
        while True:
            if ctx.past_deadline():
                return None
            url = window_url(start, end, start_index)
            data = nvd.get(url)
            total = data.get("totalResults")
            if isinstance(total, bool) or not isinstance(total, int):
                raise ValueError(f"NVD response without totalResults: {url}")
            page = _as_list(data.get("vulnerabilities"))
            for item in page:
                cve = _as_dict(_as_dict(item).get("cve"))
                day = published_day(cve)
                if day in counts:
                    counts[day].add(cve)
                else:
                    outside += 1
            start_index += len(page)
            if start_index >= total:
                break
            if not page:
                raise FetchError(url, "NVD returned an empty page before the end of the results")
        if outside:
            logger.warning("%d CVE records of the window %s to %s have a publication day outside the window.",
                           outside, start, end)
        return counts

    @staticmethod
    def _day_record(ctx: RunContext, entry: CollectorEntry, day: date, counts: DayCounts) -> CveDailyCountRecord:
        """Builds the record of one publication day."""
        return CveDailyCountRecord(
            **ctx.envelope(COUNTS_DATASET, f"cve-day:{day.isoformat()}", revision=0, source_id=NVD_SOURCE_ID,
                           source_url=day_url(day), license=LICENSE_NVD,
                           backfilled=is_backfilled(day, entry.first_run)),
            published_date=day,
            **counts.fields(),
        )

    # -- KEV catalog --------------------------------------------------------

    def _collect_kev(self, ctx: RunContext, entry: CollectorEntry, nvd: NvdClient, cursor: dict[str, Any],
                     counters: dict[str, Any]) -> PartOutcome:
        """Writes the new, changed and removed entries of the KEV catalog.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            nvd: The NVD client of the run, used for the values of new entries.
            cursor: The cursor; ``kev_etag`` and ``kev_last_modified`` are updated when all entries were processed.
            counters: Counters of the run.

        Returns:
            PartOutcome: Whether all entries were processed.
        """
        if ctx.past_deadline():
            return PartOutcome(complete=False)
        try:
            catalog = fetch_catalog(ctx, cursor.get("kev_etag"), cursor.get("kev_last_modified"))
        except TdmReservedError as e:
            logger.error("KEV catalog not read: text and data mining reserved for %s", e)
            return PartOutcome(complete=False, error=f"KEV catalog: text and data mining reserved for {e}")
        except (FetchError, ValueError) as e:
            logger.error("KEV catalog failed: %s", e)
            return PartOutcome(complete=False, error=f"KEV catalog failed: {e}")
        if catalog is None:
            logger.info("KEV catalog unchanged since the previous run.")
            counters["kev_source"] = "unchanged"
            return PartOutcome()
        items = _as_list(catalog.data.get("vulnerabilities"))
        entries: dict[str, dict[str, Any]] = {}
        invalid = 0
        for item in items:
            parsed = kev_fields(item)
            if parsed is None:
                invalid += 1
                continue
            entries.setdefault(parsed[0], parsed[1])
        counters.update({"kev_source": "cisa" if catalog.url == KEV_URL else "mirror", "kev_entries": len(entries),
                         "kev_invalid": invalid, "kev_new": 0, "kev_changed": 0, "kev_relisted": 0,
                         "kev_unlisted": 0, "kev_not_enriched": 0, "kev_deferred": 0})
        if not entries:
            logger.error("KEV catalog from %s has no valid entries.", catalog.url)
            return PartOutcome(complete=False, error=f"KEV catalog from {catalog.url} has no valid entries")
        outcome = PartOutcome()
        new_ids = self._write_known_entries(ctx, entry, catalog, entries, counters, outcome)
        self._write_new_entries(ctx, entry, nvd, catalog, entries, new_ids, counters, outcome)
        # The validators of an incompletely processed file are not stored, so that the next run reads it again;
        # after reading the mirror, the next request to cisa.gov is unconditional
        if outcome.complete or catalog.url != KEV_URL:
            for key, value in (("kev_etag", catalog.etag), ("kev_last_modified", catalog.last_modified)):
                if value:
                    cursor[key] = value
                else:
                    cursor.pop(key, None)
        logger.info("KEV catalog: %d entries, %d new, %d changed, %d removed.", len(entries), counters["kev_new"],
                    counters["kev_changed"] + counters["kev_relisted"], counters["kev_unlisted"])
        return outcome

    def _write_known_entries(self, ctx: RunContext, entry: CollectorEntry, catalog: KevCatalog,
                             entries: Mapping[str, dict[str, Any]], counters: dict[str, Any],
                             outcome: PartOutcome) -> list[str]:
        """Writes the changed and removed catalog entries, which need no NVD request.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            catalog: The fetched catalog.
            entries: Tracked fields by CVE id.
            counters: Counters of the run.
            outcome: Outcome of the KEV part; gets an error if the removals look implausible.

        Returns:
            list[str]: CVE ids of the new entries, in catalog order.
        """
        new_ids: list[str] = []
        records: list[KnownExploitedVulnerabilityRecord] = []
        for cve_id, fields in entries.items():
            store = ctx.seen(KEV_DATASET, kev_shard(cve_id))
            item_hash = kev_hash(fields, listed=True)
            status = store.classify(cve_id, item_hash)
            if status == "unchanged":
                continue
            previous = store.get(cve_id)
            if previous is None:
                new_ids.append(cve_id)
                continue
            counters["kev_relisted" if previous.data.get("is_listed") is False else "kev_changed"] += 1
            values = {name: previous.data.get(name) for name in NVD_FIELDS}
            records.append(self._mark_and_build(ctx, entry, store, catalog, cve_id, fields, values, listed=True))
        records.extend(self._write_removed_entries(ctx, entry, catalog, entries, counters, outcome))
        ctx.emit(KEV_DATASET, records)
        return new_ids

    @staticmethod
    def _listed_ids(catalog: KevCatalog) -> set[str]:
        """Returns the CVE ids of all catalog entries, including entries that are skipped as invalid."""
        return {cve_id for cve_id in (_text(_as_dict(item).get("cveID"))
                                      for item in _as_list(catalog.data.get("vulnerabilities"))) if cve_id}

    def _write_removed_entries(self, ctx: RunContext, entry: CollectorEntry, catalog: KevCatalog,
                               entries: Mapping[str, dict[str, Any]], counters: dict[str, Any],
                               outcome: PartOutcome) -> list[KnownExploitedVulnerabilityRecord]:
        """Builds one record with ``is_listed`` false for every listed entry that is missing from the catalog.

        An entry that is in the file but skipped as invalid does not count as missing. The removals
        are not recorded if the file declares another number of entries than it contains, or if more
        entries are missing than a plausible removal would explain.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            catalog: The fetched catalog.
            entries: Tracked fields by CVE id.
            counters: Counters of the run.
            outcome: Outcome of the KEV part; gets an error if the removals look implausible.

        Returns:
            list[KnownExploitedVulnerabilityRecord]: The records of the removed entries.
        """
        shards = stored_shards(ctx, KEV_DATASET) | {kev_shard(cve_id) for cve_id in entries}
        stores = [ctx.seen(KEV_DATASET, shard) for shard in sorted(shards)]
        listed = self._listed_ids(catalog)
        removed: list[tuple[SeenStore, str, dict[str, Any]]] = []
        for store in stores:
            for key in store.keys():
                seen = store.get(key)
                if key not in listed and seen is not None and seen.data.get("is_listed", True) is not False:
                    removed.append((store, key, seen.data))
        if not removed:
            return []
        known = sum(len(store) for store in stores)
        declared = catalog.data.get("count")
        contained = len(_as_list(catalog.data.get("vulnerabilities")))
        if isinstance(declared, int) and declared != contained:
            logger.warning("KEV catalog declares %d entries but contains %d; removals are not recorded.",
                           declared, contained)
            return []
        if len(removed) > max(MIN_UNLISTED_GUARD, MAX_UNLISTED_SHARE * known):
            logger.error("%d of %d known KEV entries are missing from the catalog; removals are not recorded.",
                         len(removed), known)
            outcome.error = f"{len(removed)} of {known} known KEV entries are missing from the catalog"
            return []
        records = []
        for store, cve_id, data in removed:
            fields = {name: data.get(name) for name in KEV_FIELDS}
            values = {name: data.get(name) for name in NVD_FIELDS}
            records.append(self._mark_and_build(ctx, entry, store, catalog, cve_id, fields, values, listed=False))
        counters["kev_unlisted"] = len(records)
        return records

    def _write_new_entries(self, ctx: RunContext, entry: CollectorEntry, nvd: NvdClient, catalog: KevCatalog,
                           entries: Mapping[str, dict[str, Any]], new_ids: list[str], counters: dict[str, Any],
                           outcome: PartOutcome) -> None:
        """Fetches the NVD values of the new catalog entries and writes them.

        The requests are sent before any entry is marked as seen, so that a failure
        cannot mark an entry whose record was not written. An entry whose request
        cannot be sent waits for the next run if the run deadline has passed, or if
        the NVD failed and the entry was added in the last KEV_MAX_DEFER_DAYS days;
        otherwise it is written without NVD values.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            nvd: The NVD client of the run.
            catalog: The fetched catalog.
            entries: Tracked fields by CVE id.
            new_ids: CVE ids of the new entries.
            counters: Counters of the run.
            outcome: Outcome of the KEV part; incomplete if entries are deferred.
        """
        if not new_ids:
            return
        values: dict[str, dict[str, Any]] = {}
        if ctx.backfill or len(new_ids) > MAX_SINGLE_LOOKUPS:
            error = self._read_kev_listing(ctx, nvd, set(new_ids), values)
            outcome.error = outcome.error or error
        recent = (ctx.today - timedelta(days=KEV_MAX_DEFER_DAYS)).isoformat()
        deferred: set[str] = set()
        lookups = 0
        for cve_id in new_ids:
            if cve_id in values or lookups >= MAX_SINGLE_LOOKUPS:
                continue
            if ctx.past_deadline():
                deferred.add(cve_id)
                continue
            if nvd.available:
                lookups += 1
                try:
                    found = self._lookup_cve(nvd, cve_id)
                except NotFoundError as e:
                    logger.warning("NVD lookup of %s rejected: %s", cve_id, e)
                    continue
                except (FetchError, ValueError) as e:
                    outcome.error = outcome.error or nvd.fail(e)
                else:
                    if found is not None:
                        values[cve_id] = found
                    continue
            # The NVD failed: a recently added entry waits for the next run, an older one is written without
            # NVD values, so that a long NVD outage does not hold back the catalog
            if entries[cve_id]["date_added"] >= recent:
                deferred.add(cve_id)
        records = []
        for cve_id in new_ids:
            if cve_id in deferred:
                continue
            if cve_id not in values:
                counters["kev_not_enriched"] += 1
            store = ctx.seen(KEV_DATASET, kev_shard(cve_id))
            records.append(self._mark_and_build(ctx, entry, store, catalog, cve_id, entries[cve_id],
                                                values.get(cve_id, NO_NVD_VALUES), listed=True))
        counters["kev_new"] = len(records)
        ctx.emit(KEV_DATASET, records)
        if deferred:
            logger.info("%d new KEV entries wait for their NVD values; they follow in the next run.", len(deferred))
            counters["kev_deferred"] = len(deferred)
            outcome.complete = False

    @staticmethod
    def _read_kev_listing(ctx: RunContext, nvd: NvdClient, wanted: set[str],
                          values: dict[str, dict[str, Any]]) -> Optional[str]:
        """Reads the NVD values of new catalog entries from the hasKev listing of the NVD.

        Args:
            ctx: The run context.
            nvd: The NVD client of the run.
            wanted: CVE ids of the new entries.
            values: Receives the NVD values by CVE id.

        Returns:
            Optional[str]: Error message if the NVD failed.
        """
        start_index = 0
        while nvd.available and not ctx.past_deadline():
            url = kev_listing_url(start_index)
            try:
                data = nvd.get(url)
            except NotFoundError as e:
                # The API rejected the parameters; the single requests still work
                logger.error("NVD hasKev listing rejected: %s", e)
                return f"NVD hasKev listing rejected: {e}"
            except (FetchError, ValueError) as e:
                return nvd.fail(e)
            page = _as_list(data.get("vulnerabilities"))
            for item in page:
                cve = _as_dict(_as_dict(item).get("cve"))
                if cve.get("id") in wanted:
                    values[cve["id"]] = nvd_values(cve)
            start_index += len(page)
            total = data.get("totalResults")
            if not page or not isinstance(total, int) or start_index >= total:
                break
        return None

    @staticmethod
    def _lookup_cve(nvd: NvdClient, cve_id: str) -> Optional[dict[str, Any]]:
        """Returns the NVD values of one CVE record, or None if the NVD does not have it."""
        for item in _as_list(nvd.get(cve_url(cve_id)).get("vulnerabilities")):
            cve = _as_dict(_as_dict(item).get("cve"))
            if cve.get("id") == cve_id:
                return nvd_values(cve)
        return None

    @staticmethod
    def _mark_and_build(ctx: RunContext, entry: CollectorEntry, store: SeenStore, catalog: KevCatalog,
                        cve_id: str, fields: Mapping[str, Any], values: Mapping[str, Any],
                        listed: bool) -> KnownExploitedVulnerabilityRecord:
        """Marks a catalog entry as seen and builds its record.

        The seen entry keeps the stored fields and NVD values, so that a later revision and
        the record of a removed entry carry the same values.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            store: The seen-store shard of the entry.
            catalog: The fetched catalog.
            cve_id: The CVE id.
            fields: Tracked fields (JSON-compatible).
            values: NVD values (JSON-compatible).
            listed: Whether the entry is in the catalog.

        Returns:
            KnownExploitedVulnerabilityRecord: The record with the revision from the seen store.
        """
        revision = store.mark(cve_id, kev_hash(fields, listed), ctx.today,
                              data={**fields, **values, "is_listed": listed})
        return KnownExploitedVulnerabilityRecord(
            **ctx.envelope(KEV_DATASET, f"kev:{cve_id}", revision=revision, source_id=KEV_SOURCE_ID,
                           source_url=catalog.url, license=LICENSE_KEV,
                           backfilled=is_backfilled(_iso_date(fields.get("date_added")), entry.first_run)),
            cve_id=cve_id,
            **fields,
            **values,
            is_listed=listed,
        )

"""Tests of the collector of the NVD daily CVE counts and the CISA KEV catalog."""
import json
from datetime import date, datetime, timezone
from typing import Any, Optional

import httpx

from collectors.vulnerabilities import (
    HISTORY_START, KEV_MIRROR_URL, KEV_URL, LICENSE_KEV, LICENSE_NVD, NVD_API_URL, NVD_HOST, VulnerabilitiesCollector,
    affected_vendors, cpe_vendor, cve_url, day_url, day_windows, kev_fields, kev_listing_url, metric_severity,
    select_metric, window_url,
)
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

NVD_FIXTURE = json.loads((FIXTURES_DIR / "vulnerabilities" / "nvd_cves.json").read_text(encoding="utf-8"))
KEV_FIXTURE = json.loads((FIXTURES_DIR / "vulnerabilities" / "known_exploited_vulnerabilities.json")
                         .read_text(encoding="utf-8"))
# A cursor whose days are complete up to the day before RUN_STARTED, so that a run sends no count requests
IDLE_COUNTS = {"first_day": "2026-10-19", "last_day": "2026-10-25"}
LATER = datetime(2026, 10, 27, 2, 17, tzinfo=timezone.utc)
EVEN_LATER = datetime(2026, 10, 28, 2, 17, tzinfo=timezone.utc)


def _page(cves: list[dict[str, Any]], total: Optional[int] = None) -> dict[str, Any]:
    """An NVD response page."""
    return {"resultsPerPage": len(cves), "startIndex": 0, "totalResults": len(cves) if total is None else total,
            "format": "NVD_CVE", "version": "2.0", "vulnerabilities": [{"cve": cve} for cve in cves]}


def _cve(cve_id: str, published: str, status: str = "Awaiting Analysis", **fields: Any) -> dict[str, Any]:
    """A minimal CVE record of the NVD API."""
    return {"id": cve_id, "published": published, "vulnStatus": status, "metrics": {}, "references": [], **fields}


def _cvss(source: str, kind: str, version: str, score: float, severity: Optional[str]) -> dict[str, Any]:
    """A CVSS entry of the NVD API."""
    data: dict[str, Any] = {"version": version, "baseScore": score}
    if severity:
        data["baseSeverity"] = severity
    return {"source": source, "type": kind, "cvssData": data}


def _catalog(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """A KEV file with the header of the fixture."""
    header = {key: value for key, value in KEV_FIXTURE.items() if key != "vulnerabilities"}
    return {**header, "count": len(entries), "vulnerabilities": entries}


def _kev_entry(cve_id: str, **changes: Any) -> dict[str, Any]:
    """A copy of a fixture entry with changes."""
    entry = next(item for item in KEV_FIXTURE["vulnerabilities"] if item["cveID"] == cve_id)
    return {**entry, **changes}


def _nvd_requests(fake_web) -> list[str]:
    return [url for url in fake_web.requested if url.startswith(NVD_API_URL)]


def _by_id(records: list[dict[str, Any]]) -> dict[tuple[str, int], dict[str, Any]]:
    return {(record["record_id"], record["revision"]): record for record in records}


def _capture_headers(fake_web, name: str) -> list[tuple[str, Optional[str]]]:
    """Records the value of a request header per URL; call before make_ctx."""
    captured: list[tuple[str, Optional[str]]] = []
    original = fake_web.handler

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((str(request.url), request.headers.get(name)))
        return original(request)

    fake_web.handler = handler
    return captured


# -- parsing ------------------------------------------------------------------


def test_metric_selection_prefers_the_nvd_then_primary_then_secondary() -> None:
    cna_primary = _cvss("cna@example.com", "Primary", "3.1", 9.1, "CRITICAL")
    adp_secondary = _cvss("adp@example.com", "Secondary", "3.1", 6.5, "MEDIUM")
    nvd = _cvss("nvd@nist.gov", "Secondary", "3.0", 7.5, "HIGH")
    keys = ("cvssMetricV31", "cvssMetricV30")

    assert select_metric({"cvssMetricV31": [adp_secondary, cna_primary], "cvssMetricV30": [nvd]}, keys) is nvd
    assert select_metric({"cvssMetricV31": [adp_secondary, cna_primary]}, keys) is cna_primary
    assert select_metric({"cvssMetricV31": [adp_secondary]}, keys) is adp_secondary
    assert select_metric({"cvssMetricV2": [cna_primary]}, keys) is None
    assert metric_severity(_cvss("cna@example.com", "Primary", "4.0", 9.3, None)) == "CRITICAL"
    assert metric_severity(_cvss("cna@example.com", "Primary", "3.1", 0.0, None)) == "NONE"


def test_vendors_come_from_vulnerable_cpe_matches_or_else_from_the_affected_data() -> None:
    assert cpe_vendor("cpe:2.3:a:google:chrome:*:*:*:*:*:*:*:*") == "google"
    assert cpe_vendor(r"cpe:2.3:a:acme\:labs:tool:1.0:*:*:*:*:*:*:*") == "acme:labs"
    assert cpe_vendor("cpe:2.3:a:*:tool:*:*:*:*:*:*:*:*") is None
    assert cpe_vendor("not a cpe") is None
    application = {"vulnerable": True, "criteria": "cpe:2.3:a:Example:app:*:*:*:*:*:*:*:*"}
    platform = {"vulnerable": False, "criteria": "cpe:2.3:o:microsoft:windows:-:*:*:*:*:*:*:*"}
    configured = _cve("CVE-2026-0001", "2026-10-20T10:00:00.000", configurations=[{"nodes": [
        {"operator": "OR", "cpeMatch": [application]}, {"operator": "OR", "cpeMatch": [platform]},
    ]}], affected=[{"source": "cna@example.com", "affectedData": [{"vendor": "Example Corp", "product": "App"}]}])
    platform_only = {**configured, "configurations": [{"nodes": [{"cpeMatch": [platform]}]}]}

    assert affected_vendors(configured) == {"example"}
    assert affected_vendors(platform_only) == {"example corp"}
    assert affected_vendors(_cve("CVE-2026-0002", "2026-10-20T10:00:00.000")) == set()


def test_kev_entries_without_valid_id_or_date_are_skipped_and_texts_are_not_kept() -> None:
    cve_id, fields = kev_fields(_kev_entry("CVE-2026-105134", cwes=["CWE-78", "CWE-77", "CWE-78"]))

    assert cve_id == "CVE-2026-105134"
    assert fields == {"vendor_project": "Ahsay", "product": "AhsayCBS",
                      "vulnerability_name": "Ahsay AhsayCBS Command Injection Vulnerability",
                      "date_added": "2026-10-14", "due_date": "2026-10-17", "known_ransomware_use": False,
                      "cwe_ids": ["CWE-77", "CWE-78"]}
    assert kev_fields(_kev_entry("CVE-2026-105134", cveID="")) is None
    assert kev_fields(_kev_entry("CVE-2026-105134", dateAdded="unknown")) is None
    unclear = kev_fields(_kev_entry("CVE-2026-105134", knownRansomwareCampaignUse="Maybe"))
    assert unclear is not None and unclear[1]["known_ransomware_use"] is None


def test_day_windows_respect_the_api_limit() -> None:
    windows = day_windows(date(2024, 1, 1), date(2024, 8, 28))

    assert windows == [(date(2024, 1, 1), date(2024, 4, 29)), (date(2024, 4, 30), date(2024, 8, 27)),
                       (date(2024, 8, 28), date(2024, 8, 28))]
    assert window_url(date(2026, 10, 19), date(2026, 10, 25), 2000) == (
        "https://services.nvd.nist.gov/rest/json/cves/2.0?pubStartDate=2026-10-19T00:00:00.000"
        "&pubEndDate=2026-10-25T23:59:59.999&resultsPerPage=2000&startIndex=2000")
    assert kev_listing_url(0).endswith("/cves/2.0?hasKev&resultsPerPage=2000&startIndex=0")


# -- daily counts ---------------------------------------------------------------


def test_daily_counts_follow_the_counting_rules(fake_web, make_ctx) -> None:
    fake_web.add(KEV_URL, b"", status=304)
    early = [item for item in NVD_FIXTURE["vulnerabilities"] if item["cve"]["published"] < "2026-10-02"]
    late = [item for item in NVD_FIXTURE["vulnerabilities"] if item["cve"]["published"] >= "2026-10-02"]
    fake_web.add(window_url(date(2026, 6, 4), date(2026, 10, 1), 0), _page([item["cve"] for item in early]))
    fake_web.add(window_url(date(2026, 10, 2), date(2026, 10, 25), 0), _page([item["cve"] for item in late]))
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today, cursor={"first_day": "2026-05-01", "last_day": "2026-06-03"})

    result = VulnerabilitiesCollector().collect(ctx, entry)

    records = {r["published_date"]: r for r in read_dataset(ctx.settings.output_dir, "cve_daily_counts")}
    assert len(records) == 144 and result.complete and result.error is None
    assert result.cursor == {"first_day": "2026-05-01", "last_day": "2026-10-25"}
    day = records["2026-10-21"]
    assert day["record_id"] == "cve-day:2026-10-21" and day["revision"] == 0
    assert day["source_url"] == day_url(date(2026, 10, 21)) and day["license"] == LICENSE_NVD
    assert day["backfilled"] is False and records["2026-10-15"]["backfilled"] is True
    assert {key: day[key] for key in ("cves", "rejected", "unscored", "nvd_scored", "with_exploit_reference",
                                      "kev_listed")} == {"cves": 10, "rejected": 1, "unscored": 1, "nvd_scored": 0,
                                                         "with_exploit_reference": 0, "kev_listed": 0}
    assert day["by_status"] == {"Awaiting Analysis": 4, "Deferred": 3, "Received": 1, "Rejected": 1,
                                "Undergoing Analysis": 1}
    assert day["by_cvss_v3_severity"] == {"CRITICAL": 1, "HIGH": 3, "MEDIUM": 3}
    assert day["by_cvss_v4_severity"] == {"HIGH": 2}
    assert day["by_cwe"] == {"CWE-119": 1, "CWE-120": 1, "CWE-287": 1, "CWE-617": 1, "CWE-78": 1, "CWE-862": 1,
                             "CWE-89": 1, "CWE-94": 1}
    assert day["by_vendor"] == {"apache software foundation": 1, "linux": 1, "mindsdb": 1, "ntpsec": 1,
                                "qingteng": 1, "red hat": 1, "splunk": 1, "tenda": 1, "unknown": 1}
    analyzed = records["2026-10-20"]
    assert analyzed["cves"] == 2 and analyzed["nvd_scored"] == 2 and analyzed["with_exploit_reference"] == 1
    assert analyzed["by_cvss_v3_severity"] == {"HIGH": 2} and analyzed["by_vendor"] == {"google": 2}
    assert analyzed["by_status"] == {"Analyzed": 1, "Modified": 1}
    assert records["2026-06-04"]["kev_listed"] == 1 and records["2026-06-04"]["by_vendor"] == {"solarwinds": 1}
    assert records["2026-09-25"]["by_cwe"] == {"NVD-CWE-noinfo": 1}
    assert records["2026-10-15"]["by_cvss_v4_severity"] == {"MEDIUM": 1}
    assert records["2026-10-22"]["cves"] == 0 and records["2026-10-22"]["by_status"] == {}
    assert len(_nvd_requests(fake_web)) == 2


def test_first_run_reads_all_pages_of_the_last_seven_days_and_the_next_run_continues(fake_web, make_ctx) -> None:
    fake_web.add(KEV_URL, b"", status=304)
    fake_web.add(window_url(date(2026, 10, 19), date(2026, 10, 25), 0), _page([
        _cve("CVE-2026-1001", "2026-10-19T08:00:00.000"), _cve("CVE-2026-1002", "2026-10-25T23:59:00.000")], total=3))
    fake_web.add(window_url(date(2026, 10, 19), date(2026, 10, 25), 2), _page([
        _cve("CVE-2026-1003", "2026-10-25T23:59:30.000", "Rejected")], total=3))
    ctx = make_ctx()

    first = VulnerabilitiesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert first.cursor == {"first_day": "2026-10-19", "last_day": "2026-10-25"}
    assert first.counters["cve_days"] == 7 and first.counters["cves"] == 3
    fake_web.add(window_url(date(2026, 10, 26), date(2026, 10, 27), 0), _page([
        _cve("CVE-2026-1004", "2026-10-27T12:00:00.000")]))
    second = VulnerabilitiesCollector().collect(make_ctx(started=EVEN_LATER),
                                                CollectorEntry(first_run=ctx.today, cursor=first.cursor))

    records = {r["published_date"]: r for r in read_dataset(ctx.settings.output_dir, "cve_daily_counts")}
    assert sorted(records) == [f"2026-10-{day}" for day in range(19, 28)]
    assert records["2026-10-25"]["cves"] == 2 and records["2026-10-25"]["rejected"] == 1
    assert records["2026-10-25"]["unscored"] == 1 and records["2026-10-19"]["unscored"] == 1
    assert records["2026-10-27"]["cves"] == 1 and records["2026-10-26"]["cves"] == 0
    assert second.cursor == {"first_day": "2026-10-19", "last_day": "2026-10-27"}
    assert all(record["revision"] == 0 for record in records.values())


def test_api_key_is_sent_and_shortens_the_request_interval(fake_web, make_ctx) -> None:
    captured = _capture_headers(fake_web, "apiKey")
    fake_web.add(KEV_URL, b"", status=304)
    fake_web.add(NVD_API_URL, _page([]))
    ctx = make_ctx(nvd_api_key="test-key")

    VulnerabilitiesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    keyless = make_ctx()
    VulnerabilitiesCollector().collect(keyless, CollectorEntry(first_run=keyless.today, cursor=IDLE_COUNTS))

    assert [key for url, key in captured if url.startswith(NVD_API_URL)] == ["test-key"]
    assert all(key is None for url, key in captured if not url.startswith(NVD_API_URL))
    assert ctx.web.host_intervals[NVD_HOST] == 0.7 and keyless.web.host_intervals[NVD_HOST] == 6.5


def test_backfill_fetches_the_days_before_the_first_stored_day_newest_window_first(fake_web, make_ctx) -> None:
    fake_web.add(KEV_URL, b"", status=304)
    fake_web.add(NVD_API_URL, _page([]))
    ctx = make_ctx(backfill=True)

    result = VulnerabilitiesCollector().collect(ctx, CollectorEntry(first_run=ctx.today, cursor=IDLE_COUNTS))

    expected = [window_url(start, end, 0) for start, end in reversed(day_windows(HISTORY_START, date(2026, 10, 18)))]
    assert _nvd_requests(fake_web) == expected
    assert result.cursor == {"first_day": "2024-01-01", "last_day": "2026-10-25"}
    records = read_dataset(ctx.settings.output_dir, "cve_daily_counts")
    assert len(records) == (date(2026, 10, 18) - HISTORY_START).days + 1
    assert all(record["backfilled"] for record in records)


def test_deadline_stops_between_windows_and_keeps_the_last_complete_window(fake_web, make_ctx) -> None:
    fake_web.add(KEV_URL, b"", status=304)
    fake_web.add(NVD_API_URL, _page([]))
    ctx = make_ctx(backfill=True)
    ctx.past_deadline = lambda: len(_nvd_requests(fake_web)) >= 2

    result = VulnerabilitiesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert not result.complete and result.error is None
    assert result.cursor == {"first_day": "2024-01-01", "last_day": "2024-08-27"}
    assert result.counters["deferred_windows"] == len(day_windows(HISTORY_START, date(2026, 10, 25))) - 2
    assert len(read_dataset(ctx.settings.output_dir, "cve_daily_counts")) == 240


def test_rate_limit_defers_the_days_and_a_rejected_query_is_an_error(fake_web, make_ctx) -> None:
    fake_web.add(KEV_URL, b"", status=304)
    url = window_url(date(2026, 10, 19), date(2026, 10, 25), 0)
    fake_web.add(url, b"", status=429)
    ctx = make_ctx()

    limited = VulnerabilitiesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    fake_web.add(url, b"", status=404, headers={"message": "Invalid pubStartDate"})
    rejected = VulnerabilitiesCollector().collect(make_ctx(), CollectorEntry(first_run=ctx.today))

    assert not limited.complete and limited.error is None and limited.cursor == {}
    assert not rejected.complete and "NVD API failed" in (rejected.error or "")
    assert read_dataset(ctx.settings.output_dir, "cve_daily_counts") == []


def test_empty_page_before_the_end_of_the_results_is_an_error(fake_web, make_ctx) -> None:
    fake_web.add(KEV_URL, b"", status=304)
    fake_web.add(window_url(date(2026, 10, 19), date(2026, 10, 25), 0), _page([], total=5))
    ctx = make_ctx()

    result = VulnerabilitiesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert "empty page" in (result.error or "") and result.cursor == {}
    assert read_dataset(ctx.settings.output_dir, "cve_daily_counts") == []


# -- KEV catalog ----------------------------------------------------------------


def _serve_kev_lookups(fake_web) -> None:
    """Serves NVD records for three catalog entries; other lookups find nothing."""
    fake_web.add(NVD_API_URL, _page([]))
    fake_web.add(cve_url("CVE-2026-114625"), _page([_cve("CVE-2026-114625", "2026-10-21T18:15:47.123", metrics={
        "cvssMetricV31": [_cvss("psirt@arista.com", "Primary", "3.1", 9.1, "CRITICAL"),
                          _cvss("nvd@nist.gov", "Secondary", "3.1", 9.8, "CRITICAL")]})]))
    fake_web.add(cve_url("CVE-2026-96655"), _page([_cve("CVE-2026-96655", "2026-10-19T09:00:00.000", metrics={
        "cvssMetricV40": [_cvss("cve@plex.example", "Secondary", "4.0", 8.7, "HIGH")]})]))
    fake_web.add(cve_url("CVE-2025-62221"), _page([_cve("CVE-2025-62221", "2025-12-09T18:15:59.000", metrics={
        "cvssMetricV40": [_cvss("secure@microsoft.com", "Secondary", "4.0", 8.1, "HIGH")],
        "cvssMetricV31": [_cvss("secure@microsoft.com", "Primary", "3.1", 7.8, "HIGH")]})]))


def test_kev_first_run_stores_every_entry_with_the_nvd_values(fake_web, make_ctx) -> None:
    fake_web.add(KEV_URL, KEV_FIXTURE, headers={"etag": '"v1"'})
    _serve_kev_lookups(fake_web)
    ctx = make_ctx()

    result = VulnerabilitiesCollector().collect(ctx, CollectorEntry(first_run=ctx.today, cursor=IDLE_COUNTS))

    records = {r["cve_id"]: r for r in read_dataset(ctx.settings.output_dir, "known_exploited_vulnerabilities")}
    assert len(records) == 10 and result.complete and result.error is None
    assert result.cursor == {**IDLE_COUNTS, "kev_etag": '"v1"'}
    assert result.counters["kev_new"] == 10 and result.counters["kev_not_enriched"] == 7
    assert result.counters["kev_source"] == "cisa"
    arista = records["CVE-2026-114625"]
    assert arista["record_id"] == "kev:CVE-2026-114625" and arista["revision"] == 0 and arista["is_listed"] is True
    assert arista["cve_published_at"] == "2026-10-21T18:15:47Z"
    assert (arista["cvss_base_score"], arista["cvss_version"], arista["cvss_source"]) == (9.8, "3.1", "nvd")
    assert arista["license"] == LICENSE_KEV and arista["source_url"] == KEV_URL and arista["backfilled"] is False
    plex = records["CVE-2026-96655"]
    assert (plex["cvss_base_score"], plex["cvss_version"], plex["cvss_source"]) == (8.7, "4.0", "other")
    windows = records["CVE-2025-62221"]
    assert (windows["cvss_base_score"], windows["cvss_version"], windows["cvss_source"]) == (7.8, "3.1", "other")
    vcenter = records["CVE-2026-59310"]
    assert vcenter["known_ransomware_use"] is True and vcenter["cwe_ids"] == ["CWE-22"]
    assert vcenter["vendor_project"] == "Broadcom" and vcenter["product"] == "VMware vCenter"
    assert vcenter["date_added"] == "2026-08-18" and vcenter["due_date"] == "2026-08-21"
    assert vcenter["cvss_base_score"] is None and vcenter["cve_published_at"] is None
    assert records["CVE-2026-53362"]["cwe_ids"] == [] and records["CVE-2021-27104"]["backfilled"] is True
    assert not {"shortDescription", "requiredAction", "notes", "short_description"} & set(arista)
    assert len([url for url in _nvd_requests(fake_web) if "cveId=" in url]) == 10


def test_kev_changes_removals_and_reappearance_create_revisions(fake_web, make_ctx) -> None:
    etags = _capture_headers(fake_web, "If-None-Match")
    dates = _capture_headers(fake_web, "If-Modified-Since")
    fake_web.add(KEV_URL, KEV_FIXTURE, headers={"etag": '"v1"', "last-modified": "Fri, 23 Oct 2026 19:30:18 GMT"})
    _serve_kev_lookups(fake_web)
    ctx = make_ctx()
    first = VulnerabilitiesCollector().collect(ctx, CollectorEntry(first_run=ctx.today, cursor=IDLE_COUNTS))

    # A tracked change, a reordered CWE list and an edited description (not tracked); CVE-2023-49897 is removed
    overrides = {"CVE-2026-96655": {"knownRansomwareCampaignUse": "Known"},
                 "CVE-2026-105134": {"cwes": ["CWE-78", "CWE-77"]},
                 "CVE-2026-53362": {"shortDescription": "Edited."}}
    changed = [_kev_entry(item["cveID"], **overrides.get(item["cveID"], {}))
               for item in KEV_FIXTURE["vulnerabilities"] if item["cveID"] != "CVE-2023-49897"]
    added = {**_kev_entry("CVE-2026-114625"), "cveID": "CVE-2026-120001", "dateAdded": "2026-10-26",
             "dueDate": "2026-11-16", "vulnerabilityName": "Example Product Path Traversal Vulnerability"}
    fake_web.add(KEV_URL, _catalog([added, *changed]), headers={"etag": '"v2"'})
    fake_web.add(cve_url("CVE-2026-120001"), _page([_cve("CVE-2026-120001", "2026-10-25T07:00:00.000", metrics={
        "cvssMetricV31": [_cvss("nvd@nist.gov", "Primary", "3.1", 7.5, "HIGH")]})]))
    requests_before = len(fake_web.requested)
    second = VulnerabilitiesCollector().collect(make_ctx(started=LATER),
                                                CollectorEntry(first_run=ctx.today, cursor=first.cursor))
    second_lookups = [url for url in fake_web.requested[requests_before:] if "cveId=" in url]

    fake_web.add(KEV_URL, _catalog([added, *changed, _kev_entry("CVE-2023-49897")]), headers={"etag": '"v3"'})
    third = VulnerabilitiesCollector().collect(make_ctx(started=EVEN_LATER),
                                               CollectorEntry(first_run=ctx.today, cursor=second.cursor))

    records = _by_id(read_dataset(ctx.settings.output_dir, "known_exploited_vulnerabilities"))
    assert len(records) == 10 + 3 + 1
    assert second.counters["kev_new"] == 1 and second.counters["kev_changed"] == 1
    assert second.counters["kev_unlisted"] == 1 and second.cursor["kev_etag"] == '"v2"'
    assert second_lookups == [cve_url("CVE-2026-120001")]
    removed = records[("kev:CVE-2023-49897", 1)]
    assert removed["is_listed"] is False and removed["vendor_project"] == "FXC"
    assert removed["product"] == "AE1021, AE1021PE" and removed["date_added"] == "2023-12-21"
    plex = records[("kev:CVE-2026-96655", 1)]
    assert plex["known_ransomware_use"] is True and plex["is_listed"] is True
    assert (plex["cvss_base_score"], plex["cvss_version"], plex["cvss_source"]) == (8.7, "4.0", "other")
    new = records[("kev:CVE-2026-120001", 0)]
    assert new["cvss_source"] == "nvd" and new["backfilled"] is False and new["date_added"] == "2026-10-26"
    relisted = records[("kev:CVE-2023-49897", 2)]
    assert relisted["is_listed"] is True and third.counters["kev_relisted"] == 1
    assert third.counters["kev_new"] == 0 and third.counters["kev_unlisted"] == 0
    assert [value for url, value in etags if url == KEV_URL] == [None, '"v1"', '"v2"']
    # The second file has no Last-Modified header, so the third request sends no If-Modified-Since
    assert [value for url, value in dates if url == KEV_URL] == [None, "Fri, 23 Oct 2026 19:30:18 GMT", None]
    assert "kev_last_modified" not in third.cursor


def test_unchanged_kev_file_is_not_processed(fake_web, make_ctx) -> None:
    fake_web.add(KEV_URL, KEV_FIXTURE, headers={"etag": '"v1"', "last-modified": "Fri, 23 Oct 2026 19:30:18 GMT"})
    fake_web.add(NVD_API_URL, _page([]))
    ctx = make_ctx()
    first = VulnerabilitiesCollector().collect(ctx, CollectorEntry(first_run=ctx.today, cursor=IDLE_COUNTS))

    fake_web.add(KEV_URL, b"", status=304)
    second = VulnerabilitiesCollector().collect(make_ctx(), CollectorEntry(first_run=ctx.today, cursor=first.cursor))

    assert second.counters["kev_source"] == "unchanged" and second.complete
    assert second.cursor == {**IDLE_COUNTS, "kev_etag": '"v1"', "kev_last_modified": "Fri, 23 Oct 2026 19:30:18 GMT"}
    assert len(read_dataset(ctx.settings.output_dir, "known_exploited_vulnerabilities")) == 10


def test_mirror_is_read_when_cisa_fails(fake_web, make_ctx) -> None:
    fake_web.fail(KEV_URL)
    fake_web.add(KEV_MIRROR_URL, _catalog([_kev_entry("CVE-2021-27104")]))
    fake_web.add(NVD_API_URL, _page([]))
    ctx = make_ctx()

    result = VulnerabilitiesCollector().collect(ctx, CollectorEntry(
        first_run=ctx.today, cursor={**IDLE_COUNTS, "kev_etag": '"v0"', "kev_last_modified": "Thu, 22 Oct 2026"}))

    records = read_dataset(ctx.settings.output_dir, "known_exploited_vulnerabilities")
    assert [record["source_url"] for record in records] == [KEV_MIRROR_URL]
    assert result.counters["kev_source"] == "mirror" and result.cursor == IDLE_COUNTS
    assert result.error is None and result.complete


def test_backfill_reads_the_nvd_values_from_the_kev_listing(fake_web, make_ctx) -> None:
    fake_web.add(KEV_URL, KEV_FIXTURE, headers={"etag": '"v1"'})
    fake_web.add(NVD_API_URL, _page([]))
    fake_web.add(kev_listing_url(0), _page([
        _cve("CVE-2021-27104", "2021-02-16T21:15:12.000", metrics={
            "cvssMetricV31": [_cvss("nvd@nist.gov", "Primary", "3.1", 9.8, "CRITICAL")]}),
        _cve("CVE-2022-42475", "2023-01-02T09:15:10.000", metrics={
            "cvssMetricV31": [_cvss("psirt@fortinet.com", "Primary", "3.1", 9.3, "CRITICAL")]}),
        _cve("CVE-2019-0001", "2019-01-01T00:00:00.000"),
    ], total=3))
    ctx = make_ctx(backfill=True)

    result = VulnerabilitiesCollector().collect(
        ctx, CollectorEntry(first_run=ctx.today, cursor={"first_day": "2024-01-01", "last_day": "2026-10-25"}))

    records = {r["cve_id"]: r for r in read_dataset(ctx.settings.output_dir, "known_exploited_vulnerabilities")}
    assert records["CVE-2021-27104"]["cvss_base_score"] == 9.8 and records["CVE-2021-27104"]["cvss_source"] == "nvd"
    assert records["CVE-2022-42475"]["cvss_source"] == "other"
    assert records["CVE-2022-42475"]["cve_published_at"] == "2023-01-02T09:15:10Z"
    lookups = [url for url in _nvd_requests(fake_web) if "cveId=" in url]
    assert kev_listing_url(0) in fake_web.requested and len(lookups) == 8
    assert result.counters["kev_not_enriched"] == 8 and result.complete


def test_deadline_defers_new_kev_entries_that_need_an_nvd_request(fake_web, make_ctx) -> None:
    fake_web.add(KEV_URL, KEV_FIXTURE, headers={"etag": '"v1"'})
    _serve_kev_lookups(fake_web)
    ctx = make_ctx()
    ctx.past_deadline = lambda: any("cveId=" in url for url in fake_web.requested)

    first = VulnerabilitiesCollector().collect(ctx, CollectorEntry(first_run=ctx.today, cursor=IDLE_COUNTS))
    second = VulnerabilitiesCollector().collect(make_ctx(), CollectorEntry(first_run=ctx.today, cursor=first.cursor))

    assert not first.complete and first.counters["kev_new"] == 1 and first.counters["kev_deferred"] == 9
    assert "kev_etag" not in first.cursor
    assert second.complete and second.counters["kev_new"] == 9 and second.cursor["kev_etag"] == '"v1"'
    records = read_dataset(ctx.settings.output_dir, "known_exploited_vulnerabilities")
    assert len(records) == 10 and {record["revision"] for record in records} == {0}


def test_nvd_failure_defers_recent_kev_entries_and_stores_older_ones_without_nvd_values(fake_web, make_ctx) -> None:
    catalog = _catalog([_kev_entry("CVE-2026-114625"), _kev_entry("CVE-2026-96655"), _kev_entry("CVE-2025-62221")])
    fake_web.add(KEV_URL, catalog, headers={"etag": '"v1"'})
    fake_web.add(cve_url("CVE-2026-114625"), b"", status=503)
    ctx = make_ctx()

    failed = VulnerabilitiesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    _serve_kev_lookups(fake_web)
    recovered = VulnerabilitiesCollector().collect(make_ctx(), CollectorEntry(first_run=ctx.today,
                                                                              cursor=failed.cursor))

    assert "NVD API failed" in (failed.error or "") and not failed.complete
    # Added on 2026-10-23 and 2026-10-20, within 7 days before the run: they wait for the NVD
    assert failed.counters["kev_deferred"] == 2 and failed.counters["kev_new"] == 1
    assert failed.counters["kev_not_enriched"] == 1 and failed.counters["deferred_windows"] == 1
    assert failed.cursor == {} and recovered.complete and recovered.counters["kev_new"] == 2
    records = {r["cve_id"]: r for r in read_dataset(ctx.settings.output_dir, "known_exploited_vulnerabilities")}
    assert records["CVE-2025-62221"]["cvss_base_score"] is None
    assert records["CVE-2026-114625"]["cvss_base_score"] == 9.8 and records["CVE-2026-96655"]["cvss_version"] == "4.0"
    assert len([url for url in fake_web.requested if url == cve_url("CVE-2026-114625")]) == 3 + 1


def test_entry_that_becomes_invalid_is_not_recorded_as_removed(fake_web, make_ctx) -> None:
    entries = [_kev_entry("CVE-2026-114625"), _kev_entry("CVE-2021-27104")]
    fake_web.add(KEV_URL, _catalog(entries))
    fake_web.add(NVD_API_URL, _page([]))
    ctx = make_ctx()
    first = VulnerabilitiesCollector().collect(ctx, CollectorEntry(first_run=ctx.today, cursor=IDLE_COUNTS))

    fake_web.add(KEV_URL, _catalog([entries[0], {**entries[1], "dateAdded": ""}]))
    second = VulnerabilitiesCollector().collect(make_ctx(), CollectorEntry(first_run=ctx.today, cursor=first.cursor))

    assert second.counters["kev_invalid"] == 1 and second.counters["kev_unlisted"] == 0
    assert len(read_dataset(ctx.settings.output_dir, "known_exploited_vulnerabilities")) == 2


def test_implausible_mass_removal_is_not_recorded(fake_web, make_ctx) -> None:
    entries = [{**_kev_entry("CVE-2026-114625"), "cveID": f"CVE-2026-{200000 + number}"} for number in range(25)]
    fake_web.add(KEV_URL, _catalog(entries), headers={"etag": '"v1"'})
    fake_web.add(NVD_API_URL, _page([]))
    ctx = make_ctx()
    first = VulnerabilitiesCollector().collect(ctx, CollectorEntry(first_run=ctx.today, cursor=IDLE_COUNTS))

    fake_web.add(KEV_URL, _catalog(entries[:10]), headers={"etag": '"v2"'})
    damaged = VulnerabilitiesCollector().collect(make_ctx(), CollectorEntry(first_run=ctx.today, cursor=first.cursor))
    miscounted = {**_catalog(entries[:24]), "count": 25}
    fake_web.add(KEV_URL, miscounted, headers={"etag": '"v3"'})
    inconsistent = VulnerabilitiesCollector().collect(make_ctx(),
                                                      CollectorEntry(first_run=ctx.today, cursor=damaged.cursor))

    assert damaged.counters["kev_unlisted"] == 0 and "missing from the catalog" in (damaged.error or "")
    assert inconsistent.counters["kev_unlisted"] == 0 and inconsistent.error is None
    records = read_dataset(ctx.settings.output_dir, "known_exploited_vulnerabilities")
    assert len(records) == 25 and all(record["is_listed"] for record in records)

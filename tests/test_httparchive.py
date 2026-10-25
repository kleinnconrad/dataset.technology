"""Tests of the HTTP Archive collector."""
import itertools
import json
from datetime import date, datetime, timezone

import pytest

from collectors.httparchive import METRICS, HttpArchiveCollector, parse_report, report_url
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

REPORTS = tuple(metric.report for metric in METRICS.values())
LATER = datetime(2026, 11, 23, 2, 17, tzinfo=timezone.utc)


def add_reports(fake_web) -> None:
    for report in REPORTS:
        fake_web.add_fixture(report_url(report), f"httparchive/{report}.json")


def test_report_parsing_skips_invalid_entries() -> None:
    data = [
        {"timestamp": "1577836800000", "date": "2020_01_01", "client": "desktop", "p50": 2000.5},
        {"timestamp": "1577836800000", "date": "2020_01_01", "client": "mobile", "p50": "1800.25"},
        {"date": "2020_02_01", "client": "desktop", "p50": None},
        {"date": "2020_02_01", "client": "tablet", "p50": 10},
        {"date": "not a date", "client": "desktop", "p50": 10},
        {"date": "2020_03_01", "client": "mobile", "p50": True},
        "not an entry",
    ]

    assert parse_report(data, "p50") == {"desktop": {date(2020, 1, 1): 2000.5}, "mobile": {date(2020, 1, 1): 1800.25}}
    with pytest.raises(ValueError):
        parse_report({"status": 404}, "p50")


def test_first_run_imports_the_whole_history_of_every_metric_and_client(fake_web, make_ctx) -> None:
    add_reports(fake_web)
    ctx = make_ctx()

    result = HttpArchiveCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    records = {r["record_id"]: r for r in read_dataset(ctx.settings.output_dir, "web_metrics")}
    assert set(records) == {
        "httparchive:pctHttps:desktop:2016-01-01:2026-10-01", "httparchive:pctHttps:mobile:2016-01-01:2026-10-01",
        "httparchive:h2:desktop:2016-01-01:2026-10-01", "httparchive:h2:mobile:2016-01-01:2026-10-01",
        "httparchive:h3:desktop:2020-01-01:2026-10-01", "httparchive:h3:mobile:2020-01-01:2026-10-01",
        "httparchive:bytesTotal_p50:desktop:2010-11-15:2026-10-01",
        "httparchive:bytesTotal_p50:mobile:2025-11-01:2026-10-01",
        "httparchive:bytesJs_p50:desktop:2010-11-15:2026-10-01",
        "httparchive:bytesJs_p50:mobile:2025-11-01:2026-10-01",
    }
    https = records["httparchive:pctHttps:desktop:2016-01-01:2026-10-01"]
    assert (https["metric"], https["client"], https["unit"], https["revision"]) == ("pctHttps", "desktop", "percent", 0)
    assert len(https["monthly_values"]) == 15
    assert https["monthly_values"]["2016-01-01"] == 24.04 and https["monthly_values"]["2026-10-01"] == 99.47
    assert https["source_url"] == "https://cdn.httparchive.org/v1/static/reports/pctHttps.json"
    assert https["license"] == "HTTP Archive (free use, attribution requested)" and https["backfilled"] is True
    weight = records["httparchive:bytesTotal_p50:desktop:2010-11-15:2026-10-01"]
    assert weight["unit"] == "KB" and weight["monthly_values"]["2010-11-15"] == 467.7
    assert len(weight["monthly_values"]) == 18
    assert records["httparchive:h2:mobile:2016-01-01:2026-10-01"]["monthly_values"]["2016-01-01"] == 0.0
    assert records["httparchive:bytesJs_p50:mobile:2025-11-01:2026-10-01"]["monthly_values"]["2026-10-01"] == 687.51
    # The crawl of 2021-01-01 has an h3 value for desktop only
    h3_desktop = records["httparchive:h3:desktop:2020-01-01:2026-10-01"]["monthly_values"]
    h3_mobile = records["httparchive:h3:mobile:2020-01-01:2026-10-01"]["monthly_values"]
    assert h3_desktop["2021-01-01"] == 2.55 and "2021-01-01" not in h3_mobile
    assert h3_mobile["2020-06-01"] == 0.74 and h3_mobile["2026-10-01"] == 47.59
    assert result.complete and result.error is None
    assert result.counters["records"] == 10
    assert result.counters["values"] == 4 * 15 + 10 + 9 + 2 * 18 + 2 * 12


def test_next_run_stores_only_the_crawls_after_the_last_stored_date(fake_web, make_ctx) -> None:
    add_reports(fake_web)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    HttpArchiveCollector().collect(ctx, entry)

    report = json.loads((FIXTURES_DIR / "httparchive" / "pctHttps.json").read_text(encoding="utf-8"))
    report.append({"timestamp": "1793491200000", "date": "2026_11_01", "client": "desktop", "percent": 99.5})
    fake_web.add(report_url("pctHttps"), report)
    later = HttpArchiveCollector().collect(make_ctx(started=LATER), entry)

    records = read_dataset(ctx.settings.output_dir, "web_metrics")
    new = [r for r in records if r["observed_at"].startswith("2026-11-23")]
    assert [(r["record_id"], r["monthly_values"], r["backfilled"]) for r in new] == [
        ("httparchive:pctHttps:desktop:2026-11-01:2026-11-01", {"2026-11-01": 99.5}, False)]
    assert all(r["revision"] == 0 for r in records)
    assert later.counters["records"] == 1 and later.counters["values"] == 1


def test_run_stops_at_the_deadline_and_the_next_run_continues(fake_web, make_ctx, monkeypatch) -> None:
    add_reports(fake_web)
    ctx = make_ctx()
    checks = itertools.count()
    monkeypatch.setattr(ctx, "past_deadline", lambda: next(checks) >= 2)
    entry = CollectorEntry(first_run=ctx.today)

    first = HttpArchiveCollector().collect(ctx, entry)
    second = HttpArchiveCollector().collect(make_ctx(), entry)

    assert not first.complete and first.counters["records"] == 4 and first.counters["deferred"] == 3
    # The reports of the first run hold no new crawls; the remaining reports are imported
    assert second.complete and second.counters["records"] == 6
    assert len(read_dataset(ctx.settings.output_dir, "web_metrics")) == 10


def test_a_failed_report_is_counted_and_an_error_is_set_only_if_all_fail(fake_web, make_ctx) -> None:
    fake_web.add_fixture(report_url("pctHttps"), "httparchive/pctHttps.json")
    fake_web.add(report_url("h2"), "not json", content_type="application/json")
    partly = HttpArchiveCollector().collect(make_ctx(), CollectorEntry(first_run=date(2026, 10, 26)))
    fake_web.routes.clear()
    failing = HttpArchiveCollector().collect(make_ctx(), CollectorEntry(first_run=date(2026, 10, 26)))

    assert partly.counters["records"] == 2 and partly.counters["failed"] == 4 and partly.error is None
    assert failing.counters["failed"] == 5 and failing.error == "all 5 requested HTTP Archive reports failed"


def test_rate_limit_and_refusal_stop_the_run(fake_web, make_ctx) -> None:
    fake_web.add(report_url("pctHttps"), "Too many requests", status=429, content_type="text/plain")
    limited = HttpArchiveCollector().collect(make_ctx(), CollectorEntry(first_run=date(2026, 10, 26)))
    fake_web.add(report_url("pctHttps"), "Forbidden", status=403, content_type="text/plain")
    fake_web.requested.clear()
    blocked = HttpArchiveCollector().collect(make_ctx(), CollectorEntry(first_run=date(2026, 10, 26)))

    assert not limited.complete and limited.error is None and limited.counters["deferred"] == 5
    assert not blocked.complete and blocked.error is not None and blocked.error.startswith("blocked")
    assert fake_web.requested == [report_url("pctHttps")]

"""Tests of the StatCounter market shares collector."""
import itertools
from datetime import date, datetime, timezone

import pytest

from collectors.statcounter import CHART_URL, STATISTICS, StatCounterCollector, chart_url, last_storable_month, parse_csv
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

FIXTURE = "statcounter/os_combined_desktop_ww_200901_202609.csv"
# Live responses of 2026-10-25; StatCounter appends the months without data as rows of zeros at the end
AI_CHATBOT_FIXTURE = "statcounter/ai_chatbot_US_202503_202608.csv"
PLATFORM_FIXTURE = "statcounter/comparison_DE_200807_202608.csv"
# The download link that was checked against the live site on 2026-10-25
VERIFIED_URL = ("https://gs.statcounter.com/chart.php?device=Desktop&device_hidden=desktop&multi-device=true"
                "&statType_hidden=os_combined&region_hidden=ww&granularity=monthly&statType=Operating%20System"
                "&region=Worldwide&fromInt=200901&toInt=202609&fromMonthYear=2009-01&toMonthYear=2026-09&csv=1")
HISTORY_END = date(2026, 8, 1)
LATER = datetime(2026, 11, 23, 2, 17, tzinfo=timezone.utc)


def chart_requests(fake_web) -> list[str]:
    return [url for url in fake_web.requested if url.startswith(CHART_URL)]


def desktop_ww(output_dir) -> list[dict]:
    return sorted((r for r in read_dataset(output_dir, "market_shares")
                   if r["statistic"] == "desktop_os" and r["region"] == "ww"), key=lambda r: r["record_id"])


def test_chart_url_matches_the_verified_download_link() -> None:
    assert chart_url(STATISTICS["desktop_os"], "ww", date(2009, 1, 1), date(2026, 9, 1)) == VERIFIED_URL
    url = chart_url(STATISTICS["ai_chatbot"], "US", date(2025, 3, 1), HISTORY_END)
    assert "device_hidden=desktop%2Bmobile%2Btablet%2Bconsole" in url
    assert "statType_hidden=ai_chatbot&region_hidden=US" in url and "region=United%20States%20of%20America" in url
    assert "fromInt=202503&toInt=202608" in url


@pytest.mark.parametrize(("today", "expected"), [
    (date(2026, 10, 26), date(2026, 8, 1)),
    (date(2026, 11, 14), date(2026, 8, 1)),
    (date(2026, 11, 15), date(2026, 9, 1)),
    (date(2027, 2, 14), date(2026, 11, 1)),
    (date(2027, 2, 15), date(2026, 12, 1)),
])
def test_only_months_that_ended_45_days_before_the_run_are_storable(today: date, expected: date) -> None:
    assert last_storable_month(today) == expected


def test_csv_keeps_category_names_and_skips_empty_cells_and_months_without_data() -> None:
    text = ('"Date","ChatGPT","Perplexity","OS X","macOS","Other"\n'
            "2025-03,80.5,,1.2,0,18.3\n"
            "2025-04,79,10.2,n/a,0.5,10.3\n"
            "2025-05,,,,,\n"
            "2026-09,70,20,0,0,10\n"
            "2025-02,0,0,0,0,0\n")

    shares = parse_csv(text, date(2025, 1, 1), HISTORY_END)

    assert shares == {
        date(2025, 3, 1): {"ChatGPT": 80.5, "OS X": 1.2, "macOS": 0.0, "Other": 18.3},
        date(2025, 4, 1): {"ChatGPT": 79.0, "Perplexity": 10.2, "macOS": 0.5, "Other": 10.3},
    }


def test_live_csv_with_the_months_before_the_start_of_a_statistic_appended_as_zeros() -> None:
    text = (FIXTURES_DIR / AI_CHATBOT_FIXTURE).read_text(encoding="utf-8")

    shares = parse_csv(text, date(2025, 3, 1), HISTORY_END)

    assert len(shares) == 17 and min(shares) == date(2025, 4, 1) and max(shares) == HISTORY_END
    assert shares[date(2025, 4, 1)] == {"ChatGPT": 88.02, "Microsoft Copilot": 0.61, "Google Gemini": 2.85,
                                        "Perplexity": 7.46, "Claude": 0.73, "Deepseek": 0.34, "Other": 0.0}


def test_a_page_that_is_not_a_csv_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_csv("<!DOCTYPE html><html><body>Error</body></html>", date(2025, 1, 1), HISTORY_END)


def test_first_run_imports_the_history_of_every_series(fake_web, make_ctx) -> None:
    # FIXTURE is served for every query of chart.php without a more specific response
    fake_web.add_fixture(CHART_URL, FIXTURE)
    fake_web.add_fixture(chart_url(STATISTICS["ai_chatbot"], "US", date(2025, 3, 1), HISTORY_END), AI_CHATBOT_FIXTURE)
    fake_web.add_fixture(chart_url(STATISTICS["platform"], "DE", date(2008, 7, 1), HISTORY_END), PLATFORM_FIXTURE)
    ctx = make_ctx()

    result = StatCounterCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    requested = chart_requests(fake_web)
    assert len(requested) == 21
    assert chart_url(STATISTICS["desktop_os"], "ww", date(2008, 7, 1), HISTORY_END) in requested
    assert chart_url(STATISTICS["ai_chatbot"], "DE", date(2025, 3, 1), HISTORY_END) in requested
    records = {r["record_id"]: r for r in read_dataset(ctx.settings.output_dir, "market_shares")}
    assert len(records) == 21
    chatbots = records["statcounter:ai_chatbot:US:2025-04:2026-08"]
    assert len(chatbots["monthly_shares"]) == 17 and chatbots["monthly_shares"]["2026-08-01"]["Claude"] == 4.37
    platform = records["statcounter:platform:DE:2009-01:2026-08"]["monthly_shares"]
    assert len(platform) == 212 and platform["2026-08-01"] == {"Desktop": 47.35, "Mobile": 50.91, "Tablet": 1.74}
    desktop = records["statcounter:desktop_os:ww:2009-01:2026-08"]
    assert (desktop["statistic"], desktop["region"], desktop["revision"]) == ("desktop_os", "ww", 0)
    assert desktop["source_url"] == chart_url(STATISTICS["desktop_os"], "ww", date(2008, 7, 1), HISTORY_END)
    assert desktop["license"] == "CC-BY-SA-3.0 (StatCounter Global Stats, https://gs.statcounter.com)"
    assert desktop["backfilled"] is True
    shares = desktop["monthly_shares"]
    assert len(shares) == 212 and "2026-09-01" not in shares
    assert shares["2009-01-01"] == {"Windows": 95.58, "OS X": 3.69, "Linux": 0.64, "Chrome OS": 0.0, "iOS": 0.0,
                                    "macOS": 0.0, "Android": 0.0, "Playstation": 0.08, "Other": 0.02}
    assert shares["2026-08-01"]["OS X"] == 11.57 and shares["2026-08-01"]["macOS"] == 15.08
    assert result.complete and result.error is None
    assert result.counters["records"] == 21 and result.counters["last_storable_month"] == "2026-08"
    assert result.counters["months"] == sum(len(r["monthly_shares"]) for r in records.values())


def test_next_runs_request_only_the_months_after_the_last_stored_month(fake_web, make_ctx) -> None:
    fake_web.add_fixture(CHART_URL, FIXTURE)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    StatCounterCollector().collect(ctx, entry)

    fake_web.requested.clear()
    early = StatCounterCollector().collect(make_ctx(started=datetime(2026, 11, 10, 2, 17, tzinfo=timezone.utc)),
                                           entry)
    assert chart_requests(fake_web) == [] and early.counters["up_to_date"] == 21

    later = StatCounterCollector().collect(make_ctx(started=LATER), entry)

    assert chart_url(STATISTICS["desktop_os"], "ww", date(2026, 9, 1), date(2026, 9, 1)) in chart_requests(fake_web)
    records = desktop_ww(ctx.settings.output_dir)
    assert [r["record_id"] for r in records] == ["statcounter:desktop_os:ww:2009-01:2026-08",
                                                 "statcounter:desktop_os:ww:2026-09:2026-09"]
    assert records[1]["monthly_shares"] == {"2026-09-01": {
        "Windows": 76.41, "OS X": 10.65, "Linux": 4.54, "Chrome OS": 2.31, "iOS": 0.0, "macOS": 6.09, "Android": 0.0,
        "Playstation": 0.0, "Other": 0.0}}
    assert all(r["revision"] == 0 for r in records)
    assert later.counters["records"] == 21 and later.counters["months"] == 21


def test_run_stops_at_the_deadline_and_the_next_run_continues(fake_web, make_ctx, monkeypatch) -> None:
    fake_web.add_fixture(CHART_URL, FIXTURE)
    ctx = make_ctx()
    checks = itertools.count()
    monkeypatch.setattr(ctx, "past_deadline", lambda: next(checks) >= 2)
    entry = CollectorEntry(first_run=ctx.today)

    first = StatCounterCollector().collect(ctx, entry)
    second = StatCounterCollector().collect(make_ctx(), entry)

    assert not first.complete and first.counters["records"] == 2 and first.counters["deferred"] == 19
    assert second.complete and second.counters["records"] == 19 and second.counters["up_to_date"] == 2
    assert len(read_dataset(ctx.settings.output_dir, "market_shares")) == 21


def test_rate_limit_stops_the_run_and_keeps_the_written_series(fake_web, make_ctx) -> None:
    fake_web.add(CHART_URL, "Too many requests", status=429, content_type="text/plain")
    fake_web.add_fixture(chart_url(STATISTICS["os"], "ww", date(2008, 7, 1), HISTORY_END), FIXTURE)
    ctx = make_ctx()

    result = StatCounterCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert not result.complete and result.error is None
    assert result.counters["records"] == 1 and result.counters["deferred"] == 20
    assert [r["record_id"] for r in read_dataset(ctx.settings.output_dir, "market_shares")] == [
        "statcounter:os:ww:2009-01:2026-08"]


def test_a_refusal_stops_all_requests_of_the_run(fake_web, make_ctx) -> None:
    fake_web.add(CHART_URL, "Forbidden", status=403, content_type="text/plain")
    ctx = make_ctx()

    result = StatCounterCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert len(chart_requests(fake_web)) == 1
    assert result.error is not None and result.error.startswith("blocked") and not result.complete


def test_a_tdm_reservation_stops_the_collector_before_the_download(fake_web, make_ctx) -> None:
    fake_web.add("https://gs.statcounter.com/.well-known/tdmrep.json", [{"location": "/*", "tdm-reservation": 1}])
    fake_web.add_fixture(CHART_URL, FIXTURE)
    ctx = make_ctx()

    result = StatCounterCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert chart_requests(fake_web) == []
    assert result.error is not None and "text and data mining reserved" in result.error
    assert read_dataset(ctx.settings.output_dir, "market_shares") == []


def test_failed_series_are_counted_and_an_error_is_set_only_if_all_fail(fake_web, make_ctx) -> None:
    fake_web.add_fixture(chart_url(STATISTICS["browser"], "DE", date(2008, 7, 1), HISTORY_END), FIXTURE)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)

    partly = StatCounterCollector().collect(ctx, entry)
    # On the later run the ranges change, so no request matches a prepared response
    failing = StatCounterCollector().collect(make_ctx(started=LATER), entry)

    assert partly.counters["records"] == 1 and partly.counters["failed"] == 20 and partly.error is None
    assert failing.counters["failed"] == 21 and failing.error == "all 21 requested StatCounter series failed"

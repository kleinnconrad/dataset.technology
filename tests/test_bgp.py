"""Tests of the collector of the BGP routing table size."""
from datetime import date, datetime, timezone
from typing import Optional

from collectors.bgp import LICENSE, TABLES, BgpCollector, last_complete_day, parse_measurements
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

V4, V6 = TABLES
FIXTURES = {
    V4.prefixes_url: "bgp/v4-bgp-active.txt", V4.ases_url: "bgp/v4-bgp-as-count.txt",
    V6.prefixes_url: "bgp/v6-bgp-active.txt", V6.ases_url: "bgp/v6-bgp-as-count.txt",
}
LATER = datetime(2026, 11, 26, 2, 17, tzinfo=timezone.utc)


def _ts(day: str, hour: int) -> int:
    """Returns the unix timestamp of an hour of a UTC day."""
    return int(datetime.fromisoformat(day).replace(hour=hour, tzinfo=timezone.utc).timestamp())


def _serve(fake_web, appended: Optional[dict[str, str]] = None) -> None:
    """Serves the fixtures, each with optional lines appended."""
    for url, fixture in FIXTURES.items():
        text = (FIXTURES_DIR / fixture).read_text(encoding="utf-8")
        fake_web.add(url, text + (appended or {}).get(url, ""), content_type="text/plain")


def _records(output_dir, ip_version: int) -> list[dict]:
    records = read_dataset(output_dir, "bgp_routing_table")
    return sorted((r for r in records if r["ip_version"] == ip_version), key=lambda r: r["record_id"])


def test_parsing_keeps_the_last_measurement_of_each_utc_day_and_skips_unreadable_lines() -> None:
    text = "\n".join([
        f"{_ts('2026-10-23', 22)} 100",
        # The last measurement of the day is listed before an earlier one
        f"{_ts('2026-10-24', 23)} 300",
        f"{_ts('2026-10-24', 1)} 200",
        "not a measurement",
        f"{_ts('2026-10-24', 5)}",
        f"{_ts('2026-10-25', 0)} 400 500",
        "",
        # A gap of three days
        f"{_ts('2026-10-28', 2)} 500",
    ])

    series = parse_measurements(text)

    assert series.values == {date(2026, 10, 23): 100, date(2026, 10, 24): 300, date(2026, 10, 28): 500}
    assert series.latest_day == date(2026, 10, 28) and series.malformed == 3


def test_a_day_is_complete_before_the_run_day_and_the_latest_measurement_of_both_files() -> None:
    today = date(2026, 10, 26)

    assert last_complete_day(today, [today, today]) == (date(2026, 10, 25), 0)
    # A file that lags by a day holds back the other file of its IP version
    assert last_complete_day(today, [date(2026, 10, 25), today]) == (date(2026, 10, 24), 0)
    # A file that is no longer updated does not
    assert last_complete_day(today, [date(2026, 10, 1), today]) == (date(2026, 10, 25), 1)
    # A measurement dated after the run day counts as the run day
    assert last_complete_day(today, [date(2027, 1, 1), today]) == (date(2026, 10, 25), 0)


def test_first_run_imports_the_whole_history_up_to_the_last_complete_day(fake_web, make_ctx) -> None:
    _serve(fake_web)
    ctx = make_ctx()

    result = BgpCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    [v4] = _records(ctx.settings.output_dir, 4)
    assert v4["record_id"] == "bgp:v4:1988-06-30:2026-10-24"
    # Monthly values from 1988, the last value of each day in October 2026
    assert v4["daily_prefixes"]["1988-06-30"] == 173 and v4["daily_prefixes"]["2026-10-24"] == 1085601
    assert min(v4["daily_ases"]) == "1996-09-30" and v4["daily_ases"]["2026-10-24"] == 79757
    # The fixtures end on 2026-10-25 08:01 UTC, so that day is not complete
    assert max(v4["daily_prefixes"]) == "2026-10-24" and max(v4["daily_ases"]) == "2026-10-24"
    assert len(v4["daily_prefixes"]) == 47 and len(v4["daily_ases"]) == 47
    assert v4["backfilled"] is True and v4["revision"] == 0
    assert v4["source_url"] == "https://bgp.potaroo.net/as2.0/bgp-active.txt" and v4["license"] == LICENSE
    [v6] = _records(ctx.settings.output_dir, 6)
    assert v6["record_id"] == "bgp:v6:2003-02-10:2026-10-24"
    assert v6["daily_prefixes"]["2003-02-10"] == 406 and v6["daily_prefixes"]["2026-10-24"] == 259597
    assert v6["daily_ases"]["2026-10-24"] == 37864 and len(v6["daily_prefixes"]) == 19
    assert result.counters["records"] == 2 and result.counters["files"] == 4
    # Days with a value in either series: 30 monthly prefix days, 30 AS count days and 17 shared days for IPv4
    assert result.counters["days"] == 77 + 19 and result.counters["malformed_lines"] == 0
    assert result.complete and result.error is None


def test_next_run_stores_only_the_complete_days_after_the_last_stored_day(fake_web, make_ctx) -> None:
    _serve(fake_web)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    BgpCollector().collect(ctx, entry)
    later_lines = {url: "\n".join([
        f"{_ts('2026-10-25', 23)} {base + 1}",
        f"{_ts('2026-11-25', 12)} {base + 2}",
        f"{_ts('2026-11-25', 23)} {base + 3}",
        f"{_ts('2026-11-26', 1)} {base + 4}",
    ]) + "\n" for url, base in ((V4.prefixes_url, 1086000), (V4.ases_url, 79800), (V6.prefixes_url, 260000),
                                (V6.ases_url, 37900))}
    _serve(fake_web, later_lines)

    result = BgpCollector().collect(make_ctx(started=LATER), entry)

    v4 = _records(ctx.settings.output_dir, 4)
    assert [r["record_id"] for r in v4] == ["bgp:v4:1988-06-30:2026-10-24", "bgp:v4:2026-10-25:2026-11-25"]
    assert v4[1]["daily_prefixes"] == {"2026-10-25": 1086001, "2026-11-25": 1086003}
    assert v4[1]["daily_ases"] == {"2026-10-25": 79801, "2026-11-25": 79803}
    assert v4[1]["backfilled"] is False and v4[1]["revision"] == 0
    v6 = _records(ctx.settings.output_dir, 6)
    assert v6[1]["record_id"] == "bgp:v6:2026-10-25:2026-11-25"
    assert result.counters["days"] == 4


def test_a_failed_file_skips_its_ip_version_until_the_next_run(fake_web, make_ctx) -> None:
    _serve(fake_web)
    fake_web.fail(V6.ases_url)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)

    first = BgpCollector().collect(ctx, entry)
    _serve(fake_web)
    fake_web.failing.clear()
    second = BgpCollector().collect(make_ctx(started=LATER), entry)

    assert first.counters["failed"] == 1 and first.counters["records"] == 1 and first.error is None
    assert [r["record_id"] for r in _records(ctx.settings.output_dir, 6)] == ["bgp:v6:2003-02-10:2026-10-24"]
    assert second.counters["records"] == 1


def test_all_files_failing_sets_the_error(fake_web, make_ctx) -> None:
    for url in FIXTURES:
        fake_web.fail(url)
    ctx = make_ctx()

    result = BgpCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    # The AS count file is not requested when the prefix file of its IP version failed
    assert result.error == "all 2 requested files failed"
    assert read_dataset(ctx.settings.output_dir, "bgp_routing_table") == []


def test_the_collector_stops_at_the_deadline_between_requests(fake_web, make_ctx, monkeypatch) -> None:
    _serve(fake_web)
    ctx = make_ctx()
    checks = iter([False, False])
    monkeypatch.setattr(ctx, "past_deadline", lambda: next(checks, True))

    result = BgpCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert not result.complete and result.counters["deferred"] == 2
    assert [r["ip_version"] for r in read_dataset(ctx.settings.output_dir, "bgp_routing_table")] == [4]
    assert V6.prefixes_url not in fake_web.requested

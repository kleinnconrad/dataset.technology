"""Tests of the collector of the APNIC Labs IPv6 measurement."""
import copy
import json
from datetime import date, datetime, timezone

import pytest

from collectors import ipv6
from collectors.ipv6 import ECONOMIES, LICENSE, AdoptionFormatError, Ipv6AdoptionCollector, economy_url, parse_adoption
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

GERMANY = json.loads((FIXTURES_DIR / "ipv6" / "v6economy_DE.json").read_text(encoding="utf-8"))
WORLD = json.loads((FIXTURES_DIR / "ipv6" / "v6region_XA.json").read_text(encoding="utf-8"))
LATER = datetime(2026, 11, 23, 2, 17, tzinfo=timezone.utc)


def _with_days(data: dict, *days: str) -> dict:
    """Returns a copy of a file with entries for more days, copied from its last entry."""
    extended = copy.deepcopy(data)
    for position, day in enumerate(days):
        entry = copy.deepcopy(data["data"][-1])
        entry["date"] = day
        entry["raw"]["seen"] += position + 1
        extended["data"].append(entry)
    return extended


def _records(output_dir, economy: str) -> list[dict]:
    records = read_dataset(output_dir, "ipv6_adoption")
    return sorted((r for r in records if r["economy"] == economy), key=lambda r: r["record_id"])


def test_parsing_reads_the_raw_window_and_skips_days_without_measurements() -> None:
    germany = parse_adoption(GERMANY)
    world = parse_adoption(WORLD)

    assert germany.empty_days == 2 and germany.malformed == 0
    assert date(2013, 10, 11) not in germany.samples and date(2013, 10, 12) not in germany.capable_pct
    assert germany.capable_pct[date(2013, 10, 8)] == 4.9222 and germany.preferred_pct[date(2013, 10, 8)] == 4.7539
    assert germany.samples[date(2013, 10, 8)] == 2377
    # The world values are weighted, so the number of measured users is rounded
    assert world.samples[date(2013, 10, 7)] == 293084 and world.capable_pct[date(2026, 10, 22)] == 44.561
    assert len(world.samples) == 13


def test_parsing_counts_unreadable_entries_and_rejects_files_without_entries() -> None:
    data = {"data": [
        {"date": "2013-13-01", "raw": {"seen": 10, "capable_pc": 1.0, "preferred_pc": 1.0}},
        {"date": "2014-01-01"},
        {"date": "2014-01-02", "raw": {"seen": 10, "capable_pc": None, "preferred_pc": 1.0}},
        {"date": "2014-01-03", "raw": {"seen": True, "capable_pc": 1.0, "preferred_pc": 1.0}},
        "2014-01-04",
        {"date": "2014-01-05", "raw": {"seen": 10, "capable_pc": 2.5, "preferred_pc": 2.0}},
    ]}

    adoption = parse_adoption(data)

    assert adoption.malformed == 5 and adoption.samples == {date(2014, 1, 5): 10}
    with pytest.raises(AdoptionFormatError):
        parse_adoption({"data": "none"})
    with pytest.raises(AdoptionFormatError):
        parse_adoption([])


def test_first_run_imports_the_whole_history_of_every_economy(fake_web, make_ctx) -> None:
    for economy in ECONOMIES:
        fake_web.add(economy_url(economy), WORLD if economy == "XA" else GERMANY)
    ctx = make_ctx()

    result = Ipv6AdoptionCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    records = {r["economy"]: r for r in read_dataset(ctx.settings.output_dir, "ipv6_adoption")}
    assert set(records) == set(ECONOMIES)
    germany = records["DE"]
    assert germany["record_id"] == "ipv6:DE:2013-10-08:2026-10-22"
    assert list(germany["daily_samples"]) == ["2013-10-08", "2013-10-09", "2013-10-10"] + [
        f"2026-10-{day}" for day in range(15, 23)]
    assert germany["daily_capable_pct"]["2026-10-22"] == 73.5147
    assert germany["daily_preferred_pct"]["2026-10-22"] == 70.7613
    assert germany["daily_samples"]["2026-10-22"] == 462743
    assert germany["backfilled"] is True and germany["revision"] == 0 and germany["license"] == LICENSE
    assert germany["source_url"] == "https://data1.labs.apnic.net/v6stats/v6economy/DE.json"
    world = records["XA"]
    assert world["record_id"] == "ipv6:XA:2013-10-07:2026-10-22"
    assert world["source_url"] == "https://data1.labs.apnic.net/v6stats/v6region/XA.json"
    assert result.counters["records"] == 12 and result.counters["empty_days"] == 2 * 11
    assert result.counters["days"] == 13 + 11 * 11
    assert result.complete and result.error is None


def test_next_run_stores_the_settled_days_after_the_last_stored_day(fake_web, make_ctx, monkeypatch) -> None:
    monkeypatch.setattr(ipv6, "ECONOMIES", ("DE",))
    # Yesterday (2026-10-25) can still change and is stored by the next run
    fake_web.add(economy_url("DE"), _with_days(GERMANY, "2026-10-23", "2026-10-24", "2026-10-25"))
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    Ipv6AdoptionCollector().collect(ctx, entry)
    requests = len(fake_web.requested)
    same_day = Ipv6AdoptionCollector().collect(make_ctx(), entry)
    same_day_requests = len(fake_web.requested) - requests
    fake_web.add(economy_url("DE"), _with_days(GERMANY, "2026-10-23", "2026-10-24", "2026-10-25", "2026-11-20",
                                               "2026-11-21", "2026-11-22"))

    Ipv6AdoptionCollector().collect(make_ctx(started=LATER), entry)

    germany = _records(ctx.settings.output_dir, "DE")
    assert [r["record_id"] for r in germany] == ["ipv6:DE:2013-10-08:2026-10-24", "ipv6:DE:2026-10-25:2026-11-21"]
    assert list(germany[1]["daily_samples"]) == ["2026-10-25", "2026-11-20", "2026-11-21"]
    assert germany[1]["daily_samples"]["2026-10-25"] == 462743 + 3 and germany[1]["backfilled"] is False
    # A run without new settled days sends no request
    assert same_day.counters["records"] == 0 and same_day_requests == 0


def test_a_failed_economy_is_skipped_and_all_failing_sets_the_error(fake_web, make_ctx, monkeypatch) -> None:
    monkeypatch.setattr(ipv6, "ECONOMIES", ("XA", "DE", "US"))
    fake_web.add(economy_url("XA"), WORLD)
    fake_web.add(economy_url("DE"), "<html>not json</html>")
    ctx = make_ctx()

    partial = Ipv6AdoptionCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    monkeypatch.setattr(ipv6, "ECONOMIES", ("DE", "US"))
    failed = Ipv6AdoptionCollector().collect(make_ctx(), CollectorEntry(first_run=ctx.today))

    # DE is not valid JSON, US is not found
    assert partial.counters["failed"] == 2 and partial.counters["records"] == 1 and partial.error is None
    assert failed.error == "all 2 requested files failed"
    assert [r["economy"] for r in read_dataset(ctx.settings.output_dir, "ipv6_adoption")] == ["XA"]


def test_the_collector_stops_at_the_deadline_between_requests(fake_web, make_ctx, monkeypatch) -> None:
    for economy in ECONOMIES:
        fake_web.add(economy_url(economy), WORLD if economy == "XA" else GERMANY)
    ctx = make_ctx()
    checks = iter([False])
    monkeypatch.setattr(ctx, "past_deadline", lambda: next(checks, True))

    result = Ipv6AdoptionCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert not result.complete and result.counters["deferred"] == len(ECONOMIES) - 1
    assert [r["economy"] for r in read_dataset(ctx.settings.output_dir, "ipv6_adoption")] == ["XA"]
    assert economy_url("US") not in fake_web.requested

"""Tests of the TOP500 collector."""
import itertools
import json
from datetime import date, datetime, timezone

import pytest
from defusedxml import ElementTree

from collectors.top500 import Top500Collector, aggregate, edition_url, expected_editions, parse_systems
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

# The first ten systems of the November 2025 list; the author names of the list are replaced by placeholders
FIXTURE = "top500/TOP500_202511_all_first10.xml"
JUNE_2026 = date(2026, 6, 1)
NOVEMBER_2026 = date(2026, 11, 1)

# Synthetic list in the format of the early editions: no system names, no power, systems not in rank order
EARLY_LIST = b"""<?xml version="1.0" encoding="UTF-8"?>
<top500:list xmlns:top500="http://www.top500.org/xml/top500/1.0" authors="Author One" date="1993/06/24">
  <top500:site>
    <top500:rank>2</top500:rank><top500:system-name></top500:system-name>
    <top500:manufacturer>Cray</top500:manufacturer><top500:computer>Y-MP C916</top500:computer>
    <top500:r-max>13.7</top500:r-max><top500:power></top500:power>
    <top500:country>United States</top500:country><top500:number-of-processors>16</top500:number-of-processors>
  </top500:site>
  <top500:site>
    <top500:rank>1</top500:rank><top500:system-name></top500:system-name>
    <top500:manufacturer>Thinking Machines</top500:manufacturer><top500:computer>CM-5/1024</top500:computer>
    <top500:r-max>59.7</top500:r-max><top500:power>0</top500:power>
    <top500:country>United States</top500:country><top500:number-of-processors>1024</top500:number-of-processors>
  </top500:site>
</top500:list>"""

# Synthetic list without namespace that states accelerators
ACCELERATOR_LIST = b"""<list>
  <site><rank>1</rank><system-name>A</system-name><manufacturer>M</manufacturer><r-max>1000</r-max>
    <country>Japan</country><number-of-processors>10</number-of-processors><accelerator>NVIDIA H100</accelerator></site>
  <site><rank>2</rank><system-name>B</system-name><manufacturer>M</manufacturer><r-max>500</r-max>
    <country>Japan</country><number-of-processors>10</number-of-processors><accelerator>None</accelerator></site>
</list>"""


def fixture_bytes() -> bytes:
    return (FIXTURES_DIR / FIXTURE).read_bytes()


def test_aggregates_of_a_list() -> None:
    fields = aggregate(parse_systems(fixture_bytes()))

    assert fields == {
        "systems": 10,
        "total_rmax_tflops": 7710910.0,
        "rank1_rmax_tflops": 1809000.0,
        "rank500_rmax_tflops": 241200.0,
        "rank1_system": "El Capitan",
        "rank1_country": "United States",
        "total_cores": 54018688,
        "total_power_kw": 168868.4,
        "systems_with_power": 9,
        "systems_by_country": {"Finland": 1, "Germany": 1, "Italy": 2, "Japan": 1, "Switzerland": 1,
                               "United States": 4},
        "rmax_by_country_tflops": {"Finland": 379700.0, "Germany": 1000000.0, "Italy": 719100.0, "Japan": 442010.0,
                                   "Switzerland": 434900.0, "United States": 4735200.0},
        "systems_by_manufacturer": {"EVIDEN": 2, "Fujitsu": 1, "HPE": 5, "Intel": 1, "Microsoft Azure": 1},
        "systems_with_accelerator": None,
    }


def test_early_lists_without_names_and_power_are_aggregated_by_rank() -> None:
    fields = aggregate(parse_systems(EARLY_LIST))

    assert fields["rank1_system"] == "CM-5/1024" and fields["rank1_rmax_tflops"] == 0.0597
    assert fields["rank500_rmax_tflops"] == 0.0137 and fields["total_rmax_tflops"] == 0.0734
    assert fields["total_power_kw"] is None and fields["systems_with_power"] == 0
    assert fields["total_cores"] == 1040 and fields["systems_with_accelerator"] is None


def test_accelerators_are_counted_if_the_list_states_them() -> None:
    assert aggregate(parse_systems(ACCELERATOR_LIST))["systems_with_accelerator"] == 1


@pytest.mark.parametrize("content", [
    b"<html><body>Not found</body></html>",
    b"<list><site><rank>1</rank><system-name>A</system-name></site></list>",
    b"<list></list>",
    b'<?xml version="1.0"?><!DOCTYPE list [<!ENTITY a "x">]><list>&a;</list>',
])
def test_files_that_are_not_complete_lists_are_rejected(content: bytes) -> None:
    with pytest.raises((ValueError, ElementTree.ParseError)):
        aggregate(parse_systems(content))


def test_expected_editions_follow_the_months_of_publication() -> None:
    editions = expected_editions(date(2026, 10, 26))

    assert len(editions) == 67 and editions[0] == date(1993, 6, 1) and editions[-1] == JUNE_2026
    assert expected_editions(date(2026, 11, 1))[-1] == NOVEMBER_2026
    assert expected_editions(date(2027, 5, 31))[-1] == NOVEMBER_2026


def test_first_run_stores_the_latest_published_edition(fake_web, make_ctx) -> None:
    fake_web.add_fixture(edition_url(JUNE_2026), FIXTURE)
    ctx = make_ctx()

    result = Top500Collector().collect(ctx, CollectorEntry(first_run=ctx.today))

    records = read_dataset(ctx.settings.output_dir, "supercomputer_lists")
    assert [r["record_id"] for r in records] == ["top500:2026-06"]
    record = records[0]
    assert record["list_edition"] == "2026-06-01" and record["revision"] == 0 and record["backfilled"] is True
    assert record["systems"] == 10 and record["rank1_system"] == "El Capitan"
    assert record["source_url"] == "https://www.top500.org/lists/top500/2026/06/download/TOP500_202606_all.xml/"
    assert record["license"] == "Aggregates derived from the TOP500 lists (TOP500.org)"
    # Neither the list authors nor system or site names other than the first system are stored
    stored = json.dumps(records)
    assert "Author" not in stored and "Frontier" not in stored and "Livermore" not in stored
    assert result.complete and result.error is None and result.counters["requested"] == 1


def test_first_run_in_a_month_of_publication_falls_back_to_the_previous_edition(fake_web, make_ctx) -> None:
    fake_web.add_fixture(edition_url(JUNE_2026), FIXTURE)
    ctx = make_ctx(started=datetime(2026, 11, 2, 2, 17, tzinfo=timezone.utc))

    result = Top500Collector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert [r["record_id"] for r in read_dataset(ctx.settings.output_dir, "supercomputer_lists")] == ["top500:2026-06"]
    assert result.counters["not_yet_published"] == "2026-11" and result.counters["failed"] == 0
    assert result.complete and result.error is None


def test_next_runs_request_only_new_editions(fake_web, make_ctx) -> None:
    fake_web.add_fixture(edition_url(JUNE_2026), FIXTURE)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    Top500Collector().collect(ctx, entry)

    early = Top500Collector().collect(make_ctx(started=datetime(2026, 11, 5, 2, 17, tzinfo=timezone.utc)), entry)
    fake_web.add_fixture(edition_url(NOVEMBER_2026), FIXTURE)
    later = Top500Collector().collect(make_ctx(started=datetime(2026, 11, 30, 2, 17, tzinfo=timezone.utc)), entry)

    assert early.counters["not_yet_published"] == "2026-11" and early.counters["records"] == 0
    assert early.error is None and early.complete
    records = sorted(read_dataset(ctx.settings.output_dir, "supercomputer_lists"), key=lambda r: r["record_id"])
    assert [r["record_id"] for r in records] == ["top500:2026-06", "top500:2026-11"]
    assert records[1]["list_edition"] == "2026-11-01" and records[1]["backfilled"] is False
    assert later.counters["records"] == 1 and later.counters["editions_stored"] == 1
    # Normal runs do not request the editions before the first stored edition
    assert edition_url(date(2025, 11, 1)) not in fake_web.requested


def test_a_missing_edition_after_its_month_of_publication_is_a_failure(fake_web, make_ctx) -> None:
    fake_web.add_fixture(edition_url(JUNE_2026), FIXTURE)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    Top500Collector().collect(ctx, entry)

    result = Top500Collector().collect(make_ctx(started=datetime(2026, 12, 3, 2, 17, tzinfo=timezone.utc)), entry)

    assert result.counters["failed"] == 1 and "not_yet_published" not in result.counters
    assert result.error == "all 1 requested published TOP500 editions failed"


def test_first_run_without_any_available_edition_is_an_error(fake_web, make_ctx) -> None:
    ctx = make_ctx(started=datetime(2026, 11, 2, 2, 17, tzinfo=timezone.utc))

    result = Top500Collector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.counters["not_yet_published"] == "2026-11" and result.counters["requested"] == 2
    assert result.error == "all 1 requested published TOP500 editions failed"


def test_backfill_imports_all_editions_and_a_normal_run_continues_after_the_deadline(fake_web, make_ctx,
                                                                                      monkeypatch) -> None:
    for edition in expected_editions(date(2026, 10, 26)):
        fake_web.add_fixture(edition_url(edition), FIXTURE)
    ctx = make_ctx(backfill=True)
    checks = itertools.count()
    monkeypatch.setattr(ctx, "past_deadline", lambda: next(checks) >= 40)
    entry = CollectorEntry(first_run=ctx.today)

    first = Top500Collector().collect(ctx, entry)
    second = Top500Collector().collect(make_ctx(), entry)

    assert not first.complete and first.counters["records"] == 40 and first.counters["deferred"] == 27
    assert second.complete and second.counters["records"] == 27 and second.counters["editions_stored"] == 40
    records = read_dataset(ctx.settings.output_dir, "supercomputer_lists")
    assert len({r["record_id"] for r in records}) == 67
    assert next(r for r in records if r["record_id"] == "top500:1993-06")["list_edition"] == "1993-06-01"


def test_rate_limit_and_refusal_stop_the_run(fake_web, make_ctx) -> None:
    fake_web.add(edition_url(JUNE_2026), "Too many requests", status=429, content_type="text/plain")
    limited = Top500Collector().collect(make_ctx(), CollectorEntry(first_run=date(2026, 10, 26)))
    fake_web.add(edition_url(JUNE_2026), "Forbidden", status=403, content_type="text/plain")
    blocked = Top500Collector().collect(make_ctx(), CollectorEntry(first_run=date(2026, 10, 26)))

    assert not limited.complete and limited.error is None and limited.counters["deferred"] == 1
    assert not blocked.complete and blocked.error is not None and blocked.error.startswith("blocked")

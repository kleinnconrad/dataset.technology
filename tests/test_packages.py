"""Tests of the package downloads collector (npm, PyPI, crates.io)."""
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Union

from collectors.packages import (
    NpmSeries,
    PackageDownloadsCollector,
    check_npm_days,
    crates_downloads_url,
    npm_range_url,
    parse_crates_downloads,
    parse_npm_range,
    pypistats_url,
    request_chunks,
    split_npm_days,
)
from conftest import FIXTURES_DIR, REGISTRY_FIXTURE, read_dataset
from registry import Package, Technology
from state import CollectorEntry, SeenStore

DATASET = "package_downloads"
REACT = """
[[technology]]
id = "react"
name = "React"
category = "web_framework"
added = 2026-10-01
packages = ["npm:react"]
"""
VUE = """
[[technology]]
id = "vue"
name = "Vue.js"
category = "web_framework"
added = 2026-10-01
packages = ["npm:vue"]
"""
ANGULAR = """
[[technology]]
id = "angular"
name = "Angular"
category = "web_framework"
added = 2026-10-01
packages = ["npm:@angular/core"]
"""
TINY = """
[[technology]]
id = "tiny"
name = "Tiny"
category = "developer_tool"
added = 2026-10-01
packages = ["npm:tiny-test-package"]
"""
NUMPY = """
[[technology]]
id = "numpy"
name = "NumPy"
category = "data_library"
added = 2026-10-01
packages = ["pypi:NumPy"]
"""
SERDE = """
[[technology]]
id = "serde"
name = "Serde"
category = "developer_tool"
added = 2026-10-01
packages = ["crates:serde"]
"""
NPM_REGISTRY = REACT + VUE + ANGULAR + TINY
PYPI_CRATES_REGISTRY = NUMPY + SERDE
# Days on which react and vue report 0 in the fixtures (2025-10-25..2026-10-24): npm outages and unprocessed days
NPM_ZERO_DAYS = {date.fromisoformat(day) for day in (
    "2026-06-03", "2026-07-12", "2026-08-14", "2026-09-03", "2026-09-07", "2026-09-08", "2026-09-15", "2026-10-06",
    "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-23", "2026-10-24")}
# Gap days of the first run (2026-10-26) that are requested again: from 60 days before the run to the last day
FIRST_RUN_RETRY_DAYS = ["2026-09-03", "2026-09-07", "2026-09-08", "2026-09-15", "2026-10-06", "2026-10-13",
                        "2026-10-14", "2026-10-15", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-23"]
SECOND_RUN = datetime(2026, 11, 2, 2, 17, tzinfo=timezone.utc)
TINY_FIRST_DAY = date(2025, 11, 3)

Value = Union[int, Callable[[date], int]]


def _registry(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "technologies.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _add_not_found(fake_web, url: str, fixture: str) -> None:
    fake_web.add(url, (FIXTURES_DIR / fixture).read_bytes(), status=404, content_type="application/json")


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _npm(package: str, start: date, end: date, value: Value, zeros: frozenset[date] = frozenset()) -> dict:
    """An npm range response with a value per day and zeros on the given days."""
    return {"start": start.isoformat(), "end": end.isoformat(), "package": package, "downloads": [
        {"downloads": 0 if day in zeros else (value(day) if callable(value) else value), "day": day.isoformat()}
        for day in _days(start, end)]}


def _tiny_first_run(day: date) -> int:
    """Downloads of the small package: none before its first day and on Sundays (genuine zeros)."""
    if day < TINY_FIRST_DAY or day in NPM_ZERO_DAYS or day.weekday() == 6:
        return 0
    return 7


def _serve_first_npm_run(fake_web) -> None:
    start, end = date(2025, 10, 24), date(2026, 10, 23)
    for package in ("react", "vue"):
        fake_web.add_fixture(npm_range_url(package, start, end), f"packages/npm_{package}_2025-10-25_2026-10-24.json")
    fake_web.add_fixture(npm_range_url("@angular/core", start, end), "packages/npm_angular_core_last_week.json")
    fake_web.add(npm_range_url("tiny-test-package", start, end), _npm("tiny-test-package", start, end, _tiny_first_run))


def _records(ctx, package: str) -> list[dict]:
    return sorted((r for r in read_dataset(ctx.settings.output_dir, DATASET) if r["package"] == package),
                  key=lambda r: r["record_id"])


def _seen_data(ctx, key: str) -> dict:
    entry = SeenStore.load(ctx.settings.state_dir, DATASET).get(key)
    assert entry is not None
    return entry.data


def test_new_npm_packages_get_365_days_without_gap_days(fake_web, make_ctx, tmp_path) -> None:
    _serve_first_npm_run(fake_web)
    ctx = make_ctx()

    result = PackageDownloadsCollector(_registry(tmp_path, NPM_REGISTRY)).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert "https://api.npmjs.org/downloads/range/2025-10-24:2026-10-23/@angular/core" in fake_web.requested
    react = _records(ctx, "react")
    assert len(react) == 1
    record = react[0]
    assert record["record_id"] == "downloads:npm:react:2025-10-25:2026-10-22"
    assert record["technology_id"] == "react" and record["ecosystem"] == "npm" and record["revision"] == 0
    assert record["source_id"] == "npm_downloads"
    assert record["license"] == "npm public registry API (npm open source terms)"
    assert record["source_url"] == "https://api.npmjs.org/downloads/range/2025-10-25:2026-10-22/react"
    assert record["backfilled"] is True
    days = record["daily_downloads"]
    assert days["2025-10-25"] == 4017426 and days["2026-10-22"] == 42444710
    # Outage days (all reference packages report 0) and days after today - 3 are not stored
    assert not set(days) & {day.isoformat() for day in NPM_ZERO_DAYS}
    assert len(days) == 364 - 15
    assert _seen_data(ctx, "npm:react") == {"last_day": "2026-10-23", "gap_days": FIRST_RUN_RETRY_DAYS}
    # A scoped package; its response covers only one week
    angular = _records(ctx, "@angular/core")[0]
    assert angular["record_id"] == "downloads:npm:@angular/core:2026-10-16:2026-10-22"
    assert angular["daily_downloads"] == {"2026-10-16": 1243906, "2026-10-17": 458939, "2026-10-18": 454606,
                                          "2026-10-22": 1475968}
    assert _seen_data(ctx, "npm:@angular/core") == {"last_day": "2026-10-22",
                                                    "gap_days": ["2026-10-19", "2026-10-20", "2026-10-21"]}
    # A small package: no leading zeros, genuine zeros on checked days are stored, zeros on gap days are not
    tiny = _records(ctx, "tiny-test-package")[0]
    assert tiny["record_id"] == "downloads:npm:tiny-test-package:2025-11-03:2026-10-22"
    assert tiny["daily_downloads"]["2025-11-03"] == 7 and tiny["daily_downloads"]["2025-11-09"] == 0
    assert "2026-10-19" not in tiny["daily_downloads"]
    assert result.counters["npm_reference_packages"] == 3
    assert result.counters["npm_gap_days"] == 15
    assert result.counters["records"] == 4 and result.counters["failed"] == 0
    assert result.cursor == {"npm_gap_days": sorted(day.isoformat() for day in NPM_ZERO_DAYS - {date(2026, 10, 24)})}
    assert result.complete and result.error is None


def test_later_run_fills_gap_days_and_adds_the_new_days(fake_web, make_ctx, tmp_path) -> None:
    registry = _registry(tmp_path, NPM_REGISTRY)
    _serve_first_npm_run(fake_web)
    first_ctx = make_ctx()
    entry = CollectorEntry(first_run=first_ctx.today)
    PackageDownloadsCollector(registry).collect(first_ctx, entry)

    # 10-13 to 10-15 are still missing and 10-28 is a new outage day; the other gap days now have values
    start, end = date(2026, 9, 3), date(2026, 10, 30)
    still_missing = frozenset({date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 28)})
    fake_web.add(npm_range_url("react", start, end), _npm("react", start, end, 30_000_000, still_missing))
    fake_web.add(npm_range_url("vue", start, end), _npm("vue", start, end, 3_000_000, still_missing))
    fake_web.add(npm_range_url("tiny-test-package", start, end),
                 _npm("tiny-test-package", start, end, 5, still_missing | {start}))
    angular_start = date(2026, 10, 19)
    fake_web.add(npm_range_url("@angular/core", angular_start, end),
                 _npm("@angular/core", angular_start, end, 400_000, still_missing))
    ctx = make_ctx(started=SECOND_RUN)

    result = PackageDownloadsCollector(registry).collect(ctx, entry)

    react = _records(ctx, "react")
    assert [r["record_id"] for r in react] == ["downloads:npm:react:2025-10-25:2026-10-22",
                                               "downloads:npm:react:2026-09-03:2026-10-30"]
    filled = ["2026-09-03", "2026-09-07", "2026-09-08", "2026-09-15", "2026-10-06", "2026-10-19", "2026-10-20",
              "2026-10-21", "2026-10-23"]
    new = ["2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27", "2026-10-29", "2026-10-30"]
    assert sorted(react[1]["daily_downloads"]) == filled + new
    assert react[1]["revision"] == 0
    assert _seen_data(ctx, "npm:react") == {
        "last_day": "2026-10-30", "gap_days": ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-28"]}
    # The small package reports 0 on a day that is no longer a gap: the 0 is stored
    tiny = _records(ctx, "tiny-test-package")[1]["daily_downloads"]
    assert tiny["2026-09-03"] == 0 and tiny["2026-09-07"] == 5 and "2026-10-13" not in tiny
    angular = _records(ctx, "@angular/core")[1]
    assert angular["record_id"] == "downloads:npm:@angular/core:2026-10-19:2026-10-30"
    assert "2026-10-28" not in angular["daily_downloads"]
    assert _seen_data(ctx, "npm:@angular/core")["gap_days"] == ["2026-10-28"]
    assert result.counters["npm_filled_days"] == 9 + 9 + 9 + 3
    assert result.counters["npm_gap_days"] == 4


def test_package_added_later_skips_the_gap_days_of_earlier_runs(fake_web, make_ctx, tmp_path) -> None:
    start, end = date(2025, 10, 24), date(2026, 10, 23)
    for package in ("react", "vue"):
        fake_web.add_fixture(npm_range_url(package, start, end), f"packages/npm_{package}_2025-10-25_2026-10-24.json")
    first_ctx = make_ctx()
    first = PackageDownloadsCollector(_registry(tmp_path, REACT + VUE)).collect(
        first_ctx, CollectorEntry(first_run=first_ctx.today))

    # The new package gets 365 days, but react and vue cover only the days since 2026-09-03 in this run
    start, end = date(2026, 9, 3), date(2026, 10, 30)
    still_missing = frozenset({date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 28)})
    fake_web.add(npm_range_url("react", start, end), _npm("react", start, end, 30_000_000, still_missing))
    fake_web.add(npm_range_url("vue", start, end), _npm("vue", start, end, 3_000_000, still_missing))
    old_outages = frozenset({date(2026, 6, 3), date(2026, 7, 12), date(2026, 8, 14)})
    new_start = date(2025, 10, 31)
    fake_web.add(npm_range_url("svelte", new_start, end),
                 _npm("svelte", new_start, end, 50_000, old_outages | still_missing))
    svelte = REACT.replace('"react"', '"svelte"').replace('"React"', '"Svelte"').replace("npm:react", "npm:svelte")
    ctx = make_ctx(started=SECOND_RUN)

    result = PackageDownloadsCollector(_registry(tmp_path, REACT + VUE + svelte)).collect(
        ctx, CollectorEntry(first_run=first_ctx.today, cursor=first.cursor or {}))

    days = _records(ctx, "svelte")[0]["daily_downloads"]
    assert len(days) == 365 - 7
    assert not set(days) & {day.isoformat() for day in old_outages | still_missing}
    assert _seen_data(ctx, "npm:svelte")["gap_days"] == ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-28"]
    assert result.cursor == {"npm_gap_days": ["2026-06-03", "2026-07-12", "2026-08-14", "2026-10-13", "2026-10-14",
                                              "2026-10-15", "2026-10-28"]}


def test_gap_days_are_not_detected_with_fewer_than_two_reference_packages(fake_web, make_ctx) -> None:
    start, end = date(2025, 10, 24), date(2026, 10, 23)
    fake_web.add_fixture(npm_range_url("react", start, end), "packages/npm_react_2025-10-25_2026-10-24.json")
    fake_web.add("https://pypistats.org/api/packages/torch/overall?mirrors=false", "404", status=404)
    ctx = make_ctx()

    result = PackageDownloadsCollector(REGISTRY_FIXTURE).collect(ctx, CollectorEntry(first_run=ctx.today))

    days = _records(ctx, "react")[0]["daily_downloads"]
    assert days["2026-10-19"] == 0 and days["2026-10-23"] == 0 and "2026-10-24" not in days
    assert _seen_data(ctx, "npm:react") == {"last_day": "2026-10-23"}
    assert result.counters["npm_reference_packages"] == 1 and result.counters["npm_gap_days"] == 0
    # pypi:torch, pypi:polars and crates:polars are not served
    assert result.counters["failed"] == 3 and result.error is None


def test_pypi_and_crates_store_the_days_up_to_two_days_before_the_run(fake_web, make_ctx, tmp_path) -> None:
    registry = _registry(tmp_path, PYPI_CRATES_REGISTRY)
    fake_web.add_fixture(pypistats_url("NumPy"), "packages/pypistats_numpy_overall.json")
    fake_web.add_fixture(crates_downloads_url("serde"), "packages/crates_serde_downloads.json")
    # The samples were fetched on 2026-10-25; a run on 2026-10-26 stores the days up to 2026-10-24
    ctx = make_ctx(started=datetime(2026, 10, 26, 9, 15, tzinfo=timezone.utc))
    entry = CollectorEntry(first_run=ctx.today)

    result = PackageDownloadsCollector(registry).collect(ctx, entry)

    assert "https://pypistats.org/api/packages/numpy/overall?mirrors=false" in fake_web.requested
    numpy = _records(ctx, "NumPy")[0]
    assert numpy["record_id"] == "downloads:pypi:NumPy:2026-04-08:2026-10-24"
    assert numpy["source_url"] == "https://pypistats.org/api/packages/numpy/overall?mirrors=false"
    assert numpy["license"] == "pypistats.org (PyPI download statistics)" and numpy["source_id"] == "pypistats"
    assert len(numpy["daily_downloads"]) == 200 and numpy["daily_downloads"]["2026-10-24"] == 25304211
    serde = _records(ctx, "serde")[0]
    assert serde["record_id"] == "downloads:crates:serde:2026-09-26:2026-10-24"
    assert serde["source_url"] == "https://crates.io/api/v1/crates/serde/downloads"
    assert serde["license"] == "crates.io API (crates.io data access policy)"
    assert serde["daily_downloads"]["2026-10-24"] == 4129178 and "2026-10-25" not in serde["daily_downloads"]
    assert result.counters["records"] == 2

    later = datetime(2026, 10, 28, 2, 17, tzinfo=timezone.utc)
    pypi = json.loads((FIXTURES_DIR / "packages" / "pypistats_numpy_overall.json").read_text(encoding="utf-8"))
    pypi["data"] += [{"category": "without_mirrors", "date": "2026-10-25", "downloads": 21000000},
                     {"category": "without_mirrors", "date": "2026-10-26", "downloads": 33000000}]
    fake_web.add(pypistats_url("NumPy"), pypi)
    fake_web.add(crates_downloads_url("serde"), {
        "version_downloads": [{"version": 1, "downloads": 100, "date": day} for day in
                              ("2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27")],
        "meta": {"extra_downloads": [{"date": "2026-10-25", "downloads": 50}, {"date": "2026-10-26", "downloads": 20}]},
    })
    PackageDownloadsCollector(registry).collect(make_ctx(started=later), entry)

    assert _records(ctx, "NumPy")[1]["daily_downloads"] == {"2026-10-25": 21000000, "2026-10-26": 33000000}
    assert _records(ctx, "serde")[1]["record_id"] == "downloads:crates:serde:2026-10-25:2026-10-26"
    assert _records(ctx, "serde")[1]["daily_downloads"] == {"2026-10-25": 150, "2026-10-26": 120}


def test_unknown_packages_are_counted_as_failed(fake_web, make_ctx, tmp_path) -> None:
    _add_not_found(fake_web, npm_range_url("react", date(2025, 10, 24), date(2026, 10, 23)), "packages/npm_not_found.json")
    # pypistats.org answers an unknown package with the text 404 as HTML
    fake_web.add(pypistats_url("NumPy"), "404", status=404)
    _add_not_found(fake_web, crates_downloads_url("serde"), "packages/crates_not_found.json")
    ctx = make_ctx()

    result = PackageDownloadsCollector(_registry(tmp_path, REACT + PYPI_CRATES_REGISTRY)).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    assert read_dataset(ctx.settings.output_dir, DATASET) == []
    assert result.counters["failed"] == 3 and result.counters["requested"] == 3
    assert result.error == "3 of 3 packages failed"


def test_rate_limit_stops_only_the_registry(fake_web, make_ctx, tmp_path) -> None:
    registry = _registry(tmp_path, NPM_REGISTRY + PYPI_CRATES_REGISTRY)
    fake_web.add(npm_range_url("react", date(2025, 10, 24), date(2026, 10, 23)), {"error": "slow down"}, status=429)
    fake_web.add_fixture(pypistats_url("NumPy"), "packages/pypistats_numpy_overall.json")
    fake_web.add_fixture(crates_downloads_url("serde"), "packages/crates_serde_downloads.json")
    ctx = make_ctx()

    result = PackageDownloadsCollector(registry).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert not any("vue" in url for url in fake_web.requested)
    assert {r["ecosystem"] for r in read_dataset(ctx.settings.output_dir, DATASET)} == {"pypi", "crates"}
    assert result.counters["deferred"] == 4
    assert not result.complete and result.error is None


def test_collector_stops_at_the_run_deadline(fake_web, make_ctx, tmp_path) -> None:
    ctx = make_ctx(run_deadline_minutes=0)

    result = PackageDownloadsCollector(_registry(tmp_path, NPM_REGISTRY + PYPI_CRATES_REGISTRY)).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    assert fake_web.requested == []
    assert result.counters["deferred"] == 6
    assert not result.complete and result.error is None


def test_npm_requests_are_split_into_chunks_of_540_days_in_a_backfill_run(fake_web, make_ctx, tmp_path) -> None:
    registry = _registry(tmp_path, REACT)
    ctx = make_ctx(backfill=True)
    chunks = list(request_chunks(date(2015, 1, 10), date(2026, 10, 23)))
    for start, end in chunks:
        fake_web.add(npm_range_url("react", start, end), _npm("react", start, end, 1000))

    PackageDownloadsCollector(registry).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert chunks[0] == (date(2015, 1, 10), date(2016, 7, 2)) and chunks[-1][1] == date(2026, 10, 23)
    assert all((end - start).days < 540 for start, end in chunks)
    assert len(fake_web.requested) == len(chunks) == 8
    assert _records(ctx, "react")[0]["record_id"] == "downloads:npm:react:2015-01-10:2026-10-23"


def test_gap_check_counts_only_reference_packages_that_report_the_day() -> None:
    day, other = date(2026, 10, 19), date(2026, 10, 18)
    big = {day: 0, other: 5000}
    small = {day: 3, other: 1}

    assert check_npm_days([big, dict(big), small]) == {day: True, other: False}
    # Half of the reference packages is not more than half
    assert check_npm_days([big, {day: 9000, other: 9000}]) == {day: False, other: False}
    assert check_npm_days([big, small]) == {}


def test_gap_days_are_given_up_after_60_days() -> None:
    technology = Technology(id="react", name="React", category="web_framework", added=date(2026, 10, 1))
    series = NpmSeries(technology, Package("npm", "react"), last_day=date(2026, 10, 20),
                       retry_days=frozenset({date(2026, 9, 1), date(2026, 10, 1)}),
                       values={date(2026, 9, 1): 0, date(2026, 10, 1): 0, date(2026, 10, 21): 10, date(2026, 10, 22): 0})
    checked = {date(2026, 9, 1): True, date(2026, 10, 1): True, date(2026, 10, 22): True}

    stored, gaps, last_day = split_npm_days(series, checked, retry_from=date(2026, 9, 15))

    assert stored == {date(2026, 10, 21): 10}
    assert gaps == {date(2026, 10, 1), date(2026, 10, 22)}
    assert last_day == date(2026, 10, 22)


def test_days_that_cannot_be_checked_use_the_gap_days_of_earlier_runs() -> None:
    technology = Technology(id="svelte", name="Svelte", category="web_framework", added=date(2026, 11, 1))
    values = {date(2026, 6, 2): 0, date(2026, 6, 3): 0, date(2026, 6, 4): 80}
    series = NpmSeries(technology, Package("npm", "svelte"), last_day=None, retry_days=frozenset(), values=values)
    known_gaps = {date(2026, 6, 3), date(2026, 6, 4)}

    stored, gaps, _ = split_npm_days(series, {}, retry_from=date(2026, 6, 1), known_gaps=known_gaps)

    # A known gap day is skipped only if the package reports 0 on it
    assert stored == {date(2026, 6, 2): 0, date(2026, 6, 4): 80}
    assert gaps == {date(2026, 6, 3)}


def test_parsers_ignore_days_outside_the_range_and_sum_crates_versions() -> None:
    data = json.loads((FIXTURES_DIR / "packages" / "npm_angular_core_last_week.json").read_text(encoding="utf-8"))
    assert list(parse_npm_range(data, date(2026, 10, 17), date(2026, 10, 18))) == [date(2026, 10, 17),
                                                                                 date(2026, 10, 18)]
    crates = json.loads((FIXTURES_DIR / "packages" / "crates_serde_downloads.json").read_text(encoding="utf-8"))
    totals = parse_crates_downloads(crates)
    assert totals[date(2026, 10, 23)] == 5979752 and len(totals) == 30

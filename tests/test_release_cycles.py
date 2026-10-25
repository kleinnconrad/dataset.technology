"""Tests of the endoflife.date release cycles collector."""
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from collectors.release_cycles import ReleaseCyclesCollector, parse_product, product_url
from conftest import FIXTURES_DIR, RUN_STARTED, read_dataset
from state import CollectorEntry, SeenStore

DATASET = "release_cycles"
REGISTRY = """
[[technology]]
id = "python"
name = "Python"
category = "programming_language"
added = 2026-10-01
endoflife = "python"

[[technology]]
id = "ubuntu"
name = "Ubuntu"
category = "operating_system"
added = 2026-10-01
endoflife = "ubuntu-linux"

[[technology]]
id = "nvidia_gpu"
name = "NVIDIA GPUs"
category = "accelerator"
added = 2026-10-01
endoflife = "nvidia-gpu"

[[technology]]
id = "unknown_product"
name = "Unknown product"
category = "developer_tool"
added = 2026-10-01
endoflife = "this-product-does-not-exist"

[[technology]]
id = "rust"
name = "Rust"
category = "programming_language"
added = 2026-10-01
"""
PYTHON_URL = "https://endoflife.date/api/v1/products/python"
UBUNTU_ALIAS_URL = "https://endoflife.date/api/v1/products/ubuntu-linux"
# Target of the redirect of the alias, as observed on 2026-10-25
UBUNTU_URL = "https://endoflife.date/api/v1/products/ubuntu/"
NVIDIA_URL = "https://endoflife.date/api/v1/products/nvidia-gpu"
UNKNOWN_URL = "https://endoflife.date/api/v1/products/this-product-does-not-exist"
NOT_FOUND_PAGE = "<!DOCTYPE html><html><head><title>Page Not Found - endoflife.date</title></head><body>404</body></html>"
# Collection started on 2026-10-01: cycles released before 2026-09-24 are backfilled
FIRST_RUN = date(2026, 10, 1)


def _registry(tmp_path: Path) -> Path:
    path = tmp_path / "technologies.toml"
    path.write_text(REGISTRY, encoding="utf-8")
    return path


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / "release_cycles" / name).read_text(encoding="utf-8"))


def _serve(fake_web, python: dict[str, Any]) -> None:
    fake_web.add(PYTHON_URL, python)
    # An alias answers with a permanent redirect to the canonical product
    fake_web.add(UBUNTU_ALIAS_URL, "", status=301, headers={"location": UBUNTU_URL})
    fake_web.add(UBUNTU_URL, _fixture("ubuntu.json"))
    fake_web.add(NVIDIA_URL, _fixture("nvidia-gpu.json"))
    fake_web.add(UNKNOWN_URL, NOT_FOUND_PAGE, status=404)


def _records(ctx) -> dict[str, list[dict[str, Any]]]:
    records: dict[str, list[dict[str, Any]]] = {}
    for record in read_dataset(ctx.settings.output_dir, DATASET):
        records.setdefault(record["record_id"], []).append(record)
    return {key: sorted(value, key=lambda r: r["revision"]) for key, value in records.items()}


def test_first_run_writes_every_cycle(fake_web, make_ctx, tmp_path) -> None:
    _serve(fake_web, _fixture("python.json"))
    ctx = make_ctx()

    result = ReleaseCyclesCollector(_registry(tmp_path)).collect(ctx, CollectorEntry(first_run=FIRST_RUN))

    records = _records(ctx)
    assert len(records) == 18 + 4 + 4
    python = records["eol:python:3.13"][0]
    assert python == {**python, "technology_id": "python", "product": "python", "cycle": "3.13", "codename": None,
                      "release_date": "2024-10-07", "is_lts": False, "lts_from": None,
                      "active_support_until": "2026-10-01", "eol_date": "2029-10-31", "is_eol": False,
                      "extended_support_until": None, "is_maintained": True, "latest_version": "3.13.17",
                      "latest_release_date": "2026-10-21", "is_listed": True, "revision": 0, "backfilled": True,
                      "source_id": "endoflife_date", "license": "MIT (endoflife.date)", "source_url": PYTHON_URL}
    assert records["eol:python:3.15"][0]["backfilled"] is False
    # Records of an alias are keyed by the slug of the registry; the source URL is the canonical product
    ubuntu = records["eol:ubuntu-linux:26.04"][0]
    assert ubuntu["product"] == "ubuntu-linux" and ubuntu["source_url"] == UBUNTU_URL
    assert ubuntu["codename"] == "Resolute Raccoon" and ubuntu["is_lts"] is True
    assert ubuntu["active_support_until"] == "2031-05-29" and ubuntu["extended_support_until"] == "2036-04-23"
    nvidia = records["eol:nvidia-gpu:consumer-blackwell"][0]
    assert nvidia["latest_version"] is None and nvidia["eol_date"] is None and nvidia["is_maintained"] is True
    assert result.counters["new"] == 26 and result.counters["moved"] == 1
    assert result.counters["failed"] == 1 and result.counters["requested"] == 4
    assert result.complete and result.error is None


def test_later_runs_write_changed_unlisted_and_relisted_cycles(fake_web, make_ctx, tmp_path) -> None:
    registry = _registry(tmp_path)
    entry = CollectorEntry(first_run=FIRST_RUN)
    python = _fixture("python.json")
    _serve(fake_web, python)
    ctx = make_ctx()
    ReleaseCyclesCollector(registry).collect(ctx, entry)

    unchanged = ReleaseCyclesCollector(registry).collect(make_ctx(started=RUN_STARTED + timedelta(days=7)), entry)

    changed_python = json.loads(json.dumps(python))
    releases = changed_python["result"]["releases"]
    releases[1]["latest"] = {"name": "3.14.10", "date": "2026-11-04", "link": None}
    removed = releases.pop()
    fake_web.add(PYTHON_URL, changed_python)
    changed = ReleaseCyclesCollector(registry).collect(make_ctx(started=RUN_STARTED + timedelta(days=14)), entry)
    still_missing = ReleaseCyclesCollector(registry).collect(make_ctx(started=RUN_STARTED + timedelta(days=21)), entry)
    releases.append(removed)
    fake_web.add(PYTHON_URL, changed_python)
    relisted = ReleaseCyclesCollector(registry).collect(make_ctx(started=RUN_STARTED + timedelta(days=28)), entry)

    assert unchanged.counters["new"] == 0 and unchanged.counters["changed"] == 0 and unchanged.counters["records"] == 0
    assert changed.counters["changed"] == 1 and changed.counters["unlisted"] == 1 and changed.counters["records"] == 2
    assert still_missing.counters["records"] == 0
    assert relisted.counters["changed"] == 1 and relisted.counters["records"] == 1
    records = _records(ctx)
    assert [r["latest_version"] for r in records["eol:python:3.14"]] == ["3.14.9", "3.14.10"]
    assert records["eol:python:3.14"][1]["revision"] == 1
    assert records["eol:python:3.14"][1]["latest_release_date"] == "2026-11-04"
    cycle_26 = records["eol:python:2.6"]
    assert [(r["revision"], r["is_listed"]) for r in cycle_26] == [(0, True), (1, False), (2, True)]
    assert cycle_26[1]["release_date"] is None and cycle_26[1]["eol_date"] is None
    assert cycle_26[1]["technology_id"] == "python" and cycle_26[1]["backfilled"] is False
    assert cycle_26[2]["eol_date"] == "2013-10-29"
    seen = SeenStore.load(ctx.settings.state_dir, DATASET)
    assert seen.get("python:2.6").data == {} and seen.get("python:2.6").revision == 2


def test_a_product_page_without_releases_does_not_unlist_its_cycles(fake_web, make_ctx, tmp_path) -> None:
    registry = _registry(tmp_path)
    entry = CollectorEntry(first_run=FIRST_RUN)
    _serve(fake_web, _fixture("python.json"))
    ctx = make_ctx()
    ReleaseCyclesCollector(registry).collect(ctx, entry)
    empty = _fixture("python.json")
    empty["result"]["releases"] = []
    fake_web.add(PYTHON_URL, empty)

    result = ReleaseCyclesCollector(registry).collect(make_ctx(started=RUN_STARTED + timedelta(days=7)), entry)

    assert result.counters["unlisted"] == 0 and result.counters["failed"] == 2
    assert all(len(revisions) == 1 for revisions in _records(ctx).values())


def test_error_when_every_product_fails(fake_web, make_ctx, tmp_path) -> None:
    fake_web.add(UNKNOWN_URL, NOT_FOUND_PAGE, status=404)
    ctx = make_ctx()

    result = ReleaseCyclesCollector(_registry(tmp_path)).collect(ctx, CollectorEntry(first_run=FIRST_RUN))

    assert result.counters["failed"] == 4
    assert result.error == "4 of 4 products failed"


def test_rate_limit_stops_the_collector(fake_web, make_ctx, tmp_path) -> None:
    _serve(fake_web, _fixture("python.json"))
    fake_web.add(UBUNTU_ALIAS_URL, "Too Many Requests", status=429)
    ctx = make_ctx()

    result = ReleaseCyclesCollector(_registry(tmp_path)).collect(ctx, CollectorEntry(first_run=FIRST_RUN))

    assert NVIDIA_URL not in fake_web.requested
    assert {r["product"] for r in read_dataset(ctx.settings.output_dir, DATASET)} == {"python"}
    assert result.counters["deferred"] == 3
    assert not result.complete and result.error is None


def test_collector_stops_at_the_run_deadline(fake_web, make_ctx, tmp_path) -> None:
    ctx = make_ctx(run_deadline_minutes=0)

    result = ReleaseCyclesCollector(_registry(tmp_path)).collect(ctx, CollectorEntry(first_run=FIRST_RUN))

    assert fake_web.requested == []
    assert result.counters["deferred"] == 4
    assert not result.complete and result.error is None


def test_parse_product_maps_optional_fields() -> None:
    canonical, cycles = parse_product({"result": {"name": "demo", "releases": [
        {"name": "1", "releaseDate": "not a date", "isLts": None, "isEol": True, "eolFrom": "2020-01-31",
         "latest": None},
        {"name": "1", "releaseDate": "2019-01-01"},
    ]}})

    assert canonical == "demo"
    assert cycles == [{"cycle": "1", "codename": None, "release_date": None, "is_lts": None, "lts_from": None,
                       "active_support_until": None, "eol_date": date(2020, 1, 31), "is_eol": True,
                       "extended_support_until": None, "is_maintained": None, "latest_version": None,
                       "latest_release_date": None}]
    assert product_url("ubuntu") == "https://endoflife.date/api/v1/products/ubuntu"

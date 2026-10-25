"""Tests of the Eurostat indicators collector."""
import copy
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from collectors.eurostat import (DATA_URL, GEOS, QUERIES, EurostatCollector, EurostatQuery, flat_index, parse_cube,
                                 parse_timestamp)
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

LATER = datetime(2026, 11, 22, 2, 17, tzinfo=timezone.utc)
AI_QUERY = next(query for query in QUERIES if query.dataset == "isoc_eb_ai")


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES_DIR / "eurostat" / name).read_text(encoding="utf-8"))


def _serve_all(fake_web) -> None:
    for query in QUERIES:
        fake_web.add_fixture(DATA_URL.format(dataset=query.dataset), f"eurostat/{query.dataset}.json")


def _records(ctx) -> dict[tuple[str, int], dict]:
    return {(r["record_id"], r["revision"]): r for r in read_dataset(ctx.settings.output_dir, "eurostat_indicators")}


def test_first_run_writes_every_series_with_values(fake_web, make_ctx) -> None:
    _serve_all(fake_web)
    ctx = make_ctx()

    result = EurostatCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    records = _records(ctx)
    # The fixtures contain the EU and Germany; series of other countries have no values and are not written
    assert len(records) == 2 * (2 + 1 + 2 + 1 + 2 + 1)
    eu_ai = records[("eurostat:isoc_eb_ai:E_AI_TANY:PC_ENT:EU27_2020", 0)]
    assert eu_ai["values"] == {"2021": 7.65, "2023": 8.06, "2024": 13.48, "2025": 19.95}
    assert eu_ai["indicator_label"].startswith("Enterprises using at least one of the AI technologies")
    assert eu_ai["dataset_code"] == "isoc_eb_ai" and eu_ai["unit"] == "PC_ENT" and eu_ai["geo"] == "EU27_2020"
    assert eu_ai["source_updated"] == "2026-06-15T09:00:00Z"
    assert eu_ai["backfilled"] is True and eu_ai["source_id"] == "eurostat"
    assert eu_ai["license"] == "Eurostat (reuse authorised with acknowledgement of the source)"
    assert eu_ai["source_url"] == (
        "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/isoc_eb_ai?format=JSON&lang=EN&freq=A"
        "&size_emp=GE10&nace_r2=C10-S951_X_K&indic_is=E_AI_TANY&unit=PC_ENT&geo=EU27_2020")
    assert records[("eurostat:isoc_eb_ai:E_AI_TNLG:PC_ENT:EU27_2020", 0)]["values"]["2025"] == 8.76
    assert records[("eurostat:isoc_cicce_use:E_CC:PC_ENT:EU27_2020", 0)]["values"]["2025"] == 52.74
    # A code that the response contains but the query does not select is ignored
    assert not any("E_AI_TPVSG" in record_id for record_id, _ in records)
    specialists = records[("eurostat:isoc_sks_itspt:TOTAL:THS_PER:DE", 0)]
    assert specialists["values"]["2025"] == 2319.1 and specialists["indicator"] == "TOTAL"
    assert specialists["indicator_label"] == "Employed information and communications technology (ICT) specialists"
    assert records[("eurostat:isoc_sks_itspt:TOTAL:PC_EMP:DE", 0)]["values"]["2025"] == 5.5
    households = records[("eurostat:isoc_ci_in_h:TOTAL:PC_HH:EU27_2020", 0)]
    assert households["indicator_label"] == "Households - level of internet access: Total"
    assert households["values"]["2025"] == 94.76
    assert records[("eurostat:isoc_e_dii:E_DI4_GELO:PC_ENT:DE", 0)]["values"] == {"2022": 78.04, "2024": 80.51}
    assert records[("eurostat:isoc_ai_iaiu:I_IUAI:PC_IND:DE", 0)]["values"] == {"2025": 32.25}
    assert result.counters["records"] == 18 and result.counters["new"] == 18 and result.complete
    assert result.error is None
    # One request per dataset, with repeated parameters for the series codes and the countries
    ai_request = next(url for url in fake_web.requested if "/isoc_eb_ai?" in url)
    params = parse_qsl(urlsplit(ai_request).query)
    assert [value for key, value in params if key == "indic_is"] == ["E_AI_TANY", "E_AI_TNLG"]
    assert [value for key, value in params if key == "geo"] == list(GEOS)
    assert ("freq", "A") in params and ("size_emp", "GE10") in params and ("format", "JSON") in params
    assert len(fake_web.requested) == len(QUERIES)


def test_unchanged_series_are_not_written_again_and_changed_series_get_a_revision(fake_web, make_ctx) -> None:
    _serve_all(fake_web)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    EurostatCollector().collect(ctx, entry)
    unchanged = EurostatCollector().collect(make_ctx(started=LATER), entry)

    changed = copy.deepcopy(_fixture("isoc_eb_ai.json"))
    # EU27 2025 of E_AI_TANY is revised; the EU loses its 2021 value of E_AI_TNLG
    changed["value"]["11"] = 20.1
    del changed["value"]["0"]
    changed["updated"] = "2026-12-01T11:00:00+0100"
    fake_web.add(DATA_URL.format(dataset="isoc_eb_ai"), changed)
    revised = EurostatCollector().collect(make_ctx(started=datetime(2026, 12, 20, 2, 17, tzinfo=timezone.utc)), entry)

    assert unchanged.counters["records"] == 0
    assert revised.counters["changed"] == 2 and revised.counters["records"] == 2
    records = _records(ctx)
    tany = records[("eurostat:isoc_eb_ai:E_AI_TANY:PC_ENT:EU27_2020", 1)]
    assert tany["values"] == {"2021": 7.65, "2023": 8.06, "2024": 13.48, "2025": 20.1}
    assert tany["backfilled"] is False and tany["source_updated"] == "2026-12-01T10:00:00Z"
    assert records[("eurostat:isoc_eb_ai:E_AI_TNLG:PC_ENT:EU27_2020", 1)]["values"] == {
        "2023": 2.1, "2024": 5.41, "2025": 8.76}
    assert ("eurostat:isoc_eb_ai:E_AI_TANY:PC_ENT:DE", 1) not in records


def test_series_whose_values_disappear_get_an_empty_revision(fake_web, make_ctx) -> None:
    _serve_all(fake_web)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    EurostatCollector().collect(ctx, entry)
    without_germany = copy.deepcopy(_fixture("isoc_ai_iaiu.json"))
    without_germany["value"] = {key: value for key, value in without_germany["value"].items() if int(key) % 2 == 0}
    fake_web.add(DATA_URL.format(dataset="isoc_ai_iaiu"), without_germany)

    EurostatCollector([q for q in QUERIES if q.dataset == "isoc_ai_iaiu"]).collect(make_ctx(started=LATER), entry)

    record = _records(ctx)[("eurostat:isoc_ai_iaiu:I_IUAI:PC_IND:DE", 1)]
    assert record["values"] == {}


def test_unknown_dataset_and_empty_response_count_as_failed(fake_web, make_ctx) -> None:
    unknown = EurostatQuery("isoc_xx_yy", (("freq", ("A",)), ("unit", ("PC_ENT",))), series_dimension=None)
    fake_web.add(DATA_URL.format(dataset="isoc_xx_yy"), _fixture("unknown_dataset_404.json"), status=404)
    empty = copy.deepcopy(_fixture("isoc_eb_ai.json"))
    empty["value"] = {}
    empty["size"][empty["id"].index("geo")] = 0
    empty["dimension"]["geo"]["category"] = {"index": {}, "label": {}}
    fake_web.add(DATA_URL.format(dataset="isoc_eb_ai"), empty)
    fake_web.add_fixture(DATA_URL.format(dataset="isoc_cicce_use"), "eurostat/isoc_cicce_use.json")
    cloud = next(query for query in QUERIES if query.dataset == "isoc_cicce_use")
    ctx = make_ctx()

    partial = EurostatCollector([unknown, AI_QUERY, cloud]).collect(ctx, CollectorEntry(first_run=ctx.today))
    failed = EurostatCollector([unknown, AI_QUERY]).collect(make_ctx(), CollectorEntry(first_run=ctx.today))

    assert partial.counters["failed"] == 2 and partial.counters["records"] == 2 and partial.error is None
    assert failed.error == "all 2 requested datasets failed"


def test_collector_stops_at_the_deadline_and_at_a_rate_limit(fake_web, make_ctx) -> None:
    _serve_all(fake_web)
    ctx = make_ctx()
    ctx.past_deadline = lambda: len(fake_web.requested) >= 2

    stopped = EurostatCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert not stopped.complete and stopped.counters["deferred"] == len(QUERIES) - 2
    assert {r["dataset_code"] for r in read_dataset(ctx.settings.output_dir, "eurostat_indicators")} == {
        QUERIES[0].dataset, QUERIES[1].dataset}

    fake_web.add(DATA_URL.format(dataset=QUERIES[0].dataset), "slow down", status=429)
    limited = EurostatCollector().collect(make_ctx(), CollectorEntry(first_run=ctx.today))
    assert not limited.complete and limited.counters["deferred"] == len(QUERIES) and limited.error is None


def test_blocked_request_stops_the_collector(fake_web, make_ctx) -> None:
    _serve_all(fake_web)
    fake_web.add(DATA_URL.format(dataset=QUERIES[0].dataset), "Forbidden", status=403)
    ctx = make_ctx()

    result = EurostatCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.error and result.error.startswith("blocked") and not result.complete
    assert len(fake_web.requested) == 1


def test_dimension_that_is_not_fixed_is_rejected() -> None:
    # The response lists all size classes, because the request did not fix size_emp
    with pytest.raises(ValueError, match="size_emp"):
        parse_cube(_fixture("isoc_eb_ai_unknown_geo.json"), AI_QUERY)


def test_flat_index_is_row_major() -> None:
    # isoc_eb_ai fixture: E_AI_TANY (position 1 of 3), EU27_2020 (0 of 2), 2025 (3 of 4)
    assert flat_index([0, 0, 0, 1, 0, 0, 3], [1, 1, 1, 3, 1, 2, 4]) == 11
    assert flat_index([0, 0, 0, 1, 0, 1, 0], [1, 1, 1, 3, 1, 2, 4]) == 12
    assert _fixture("isoc_eb_ai.json")["value"]["11"] == 19.95


def test_parse_timestamp_converts_to_utc() -> None:
    assert parse_timestamp("2026-06-15T11:00:00+0200") == datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    assert parse_timestamp("2026-02-27T11:00:00+01:00") == datetime(2026, 2, 27, 10, 0, tzinfo=timezone.utc)
    assert parse_timestamp("not a date") is None and parse_timestamp(None) is None

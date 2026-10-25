"""Tests of the World Bank indicators collector."""
import copy
import json
from datetime import datetime, timezone
from typing import Any, Optional

from collectors.world_bank import (COUNTRIES_URL, WorldBankCollector, indicator_url, parse_entities,
                                   parse_indicator_rows, year_delta)
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

INTERNET = "IT.NET.USER.ZS"
RND = "GB.XPD.RSDV.GD.ZS"
ARCHIVED = "IP.TMK.TOTL"
LATER = datetime(2026, 11, 22, 2, 17, tzinfo=timezone.utc)


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES_DIR / "world_bank" / name).read_text(encoding="utf-8"))


def _probe(data: list, last_updated: Optional[str] = None) -> list:
    """Returns the response to a request with per_page=1 for the full response ``data``."""
    meta = {**data[0], "per_page": 1, "pages": data[0]["total"]}
    if last_updated:
        meta["lastupdated"] = last_updated
    return [meta, data[1][:1]]


def _rows(data: list, iso2: str) -> list[dict]:
    return [row for row in data[1] if row["country"]["id"] == iso2]


def _serve_first_run(fake_web) -> None:
    fake_web.add_fixture(COUNTRIES_URL, "world_bank/countries.json")
    fake_web.add_fixture(indicator_url(INTERNET), f"world_bank/{INTERNET}.json")
    fake_web.add_fixture(indicator_url(RND), f"world_bank/{RND}.json")


def _records_of_day(ctx, day: str) -> dict[str, dict]:
    return {r["record_id"]: r for r in read_dataset(ctx.settings.output_dir, "world_bank_indicators")
            if r["observed_at"].startswith(day)}


def test_first_run_writes_all_years_of_every_country_with_values(fake_web, make_ctx) -> None:
    _serve_first_run(fake_web)
    ctx = make_ctx()

    result = WorldBankCollector((INTERNET, RND)).collect(ctx, CollectorEntry(first_run=ctx.today))

    records = _records_of_day(ctx, "2026-10-26")
    # The European Union has only null values in the fixture and gets no record
    assert set(records) == {f"wb:{INTERNET}:{code}" for code in ("HIC", "WLD", "CHN", "DEU", "IND", "USA")} | {
        f"wb:{RND}:{code}" for code in ("WLD", "DEU", "KOR", "NGA", "USA")}
    germany = records[f"wb:{INTERNET}:DEU"]
    # The latest year is null for Germany and is not stored
    assert germany["values"] == {"2019": 88.1345169091916, "2020": 89.81294133, "2021": 91.43060973,
                                 "2022": 91.62984383, "2023": 92.4763763, "2024": 93.49999579}
    assert germany["revision"] == 0 and germany["backfilled"] is True and germany["removed_years"] == []
    assert germany["indicator_name"] == "Individuals using the Internet (% of population)"
    assert germany["country_name"] == "Germany" and germany["is_aggregate"] is False
    assert germany["source_last_updated"] == "2026-10-08"
    assert germany["source_url"] == "https://api.worldbank.org/v2/country/DEU/indicator/IT.NET.USER.ZS?format=json"
    assert germany["license"] == "CC-BY-4.0 (World Bank)" and germany["source_id"] == "worldbank_indicators"
    # Income groups have no ISO3 code in the data rows; the code comes from the country list
    high_income = records[f"wb:{INTERNET}:HIC"]
    assert high_income["country_name"] == "High income" and high_income["is_aggregate"] is True
    assert high_income["values"]["2025"] == 94.2 and high_income["values"]["2024"] == 93.0
    assert records[f"wb:{INTERNET}:WLD"]["is_aggregate"] is True
    assert records[f"wb:{RND}:NGA"]["values"] == {"2019": 0.28444}
    assert result.cursor == {"last_updated": {INTERNET: "2026-10-08", RND: "2026-10-08"}}
    assert result.counters["downloaded"] == 2 and result.counters["records"] == 11
    assert result.complete and result.error is None
    # Without a stored date the indicator is downloaded without the metadata request
    assert indicator_url(INTERNET, per_page=1) not in fake_web.requested
    shards = ctx.settings.state_dir / "seen" / "world_bank_indicators"
    assert sorted(path.name for path in shards.glob("*.json")) == [f"{RND}.json", f"{INTERNET}.json"]


def test_indicator_with_unchanged_update_date_is_not_downloaded(fake_web, make_ctx) -> None:
    _serve_first_run(fake_web)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    first = WorldBankCollector((INTERNET, RND)).collect(ctx, entry)
    fake_web.add(indicator_url(INTERNET, per_page=1), _probe(_fixture(f"{INTERNET}.json")))
    fake_web.add(indicator_url(RND, per_page=1), _probe(_fixture(f"{RND}.json")))
    fake_web.requested.clear()

    second = WorldBankCollector((INTERNET, RND)).collect(make_ctx(started=LATER), entry.model_copy(
        update={"cursor": first.cursor}))

    assert fake_web.requested == [indicator_url(INTERNET, per_page=1), indicator_url(RND, per_page=1)]
    assert second.counters["unchanged"] == 2 and second.counters["records"] == 0
    assert second.cursor == first.cursor
    assert _records_of_day(ctx, "2026-11-22") == {}


def test_update_writes_only_added_changed_and_removed_years(fake_web, make_ctx) -> None:
    _serve_first_run(fake_web)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    first = WorldBankCollector((INTERNET, RND)).collect(ctx, entry)
    rnd_shard = ctx.settings.state_dir / "seen" / "world_bank_indicators" / f"{RND}.json"
    internet_shard = rnd_shard.with_name(f"{INTERNET}.json")
    rnd_state, internet_state = rnd_shard.read_bytes(), internet_shard.read_bytes()

    updated = copy.deepcopy(_fixture(f"{INTERNET}.json"))
    updated[0]["lastupdated"] = "2026-12-16"
    for row in _rows(updated, "DE"):
        row["value"] = {"2024": 93.6, "2025": 94.1}.get(row["date"], row["value"])
    for row in _rows(updated, "US"):
        if row["date"] == "2019":
            row["value"] = None
    for row in _rows(updated, "EU"):
        if row["date"] == "2019":
            row["value"] = 85.0
    # India disappears from the response
    updated[1] = [row for row in updated[1] if row["country"]["id"] != "IN"]
    updated[0]["total"] = len(updated[1])
    fake_web.add(indicator_url(INTERNET), updated)
    fake_web.add(indicator_url(INTERNET, per_page=1), _probe(updated))
    fake_web.add(indicator_url(RND, per_page=1), _probe(_fixture(f"{RND}.json")))

    second = WorldBankCollector((INTERNET, RND)).collect(make_ctx(started=LATER), entry.model_copy(
        update={"cursor": first.cursor}))

    records = _records_of_day(ctx, "2026-11-22")
    assert set(records) == {f"wb:{INTERNET}:{code}" for code in ("DEU", "USA", "IND", "EUU")}
    germany = records[f"wb:{INTERNET}:DEU"]
    assert germany["revision"] == 1 and germany["backfilled"] is False
    assert germany["values"] == {"2024": 93.6, "2025": 94.1} and germany["removed_years"] == []
    assert germany["source_last_updated"] == "2026-12-16"
    usa = records[f"wb:{INTERNET}:USA"]
    assert usa["revision"] == 1 and usa["values"] == {} and usa["removed_years"] == [2019]
    india = records[f"wb:{INTERNET}:IND"]
    assert india["values"] == {} and india["removed_years"] == [2019, 2020, 2021, 2022, 2023, 2024, 2025]
    assert india["country_name"] == "India" and india["is_aggregate"] is False
    eu = records[f"wb:{INTERNET}:EUU"]
    assert eu["revision"] == 0 and eu["backfilled"] is True and eu["values"] == {"2019": 85.0}
    assert eu["is_aggregate"] is True
    assert second.cursor == {"last_updated": {INTERNET: "2026-12-16", RND: "2026-10-08"}}
    assert second.counters == {**second.counters, "downloaded": 1, "unchanged": 1, "records": 4, "values": 3,
                               "removed_years": 8}
    # Only the shard of the updated indicator is rewritten
    assert rnd_shard.read_bytes() == rnd_state
    assert internet_shard.read_bytes() != internet_state

    # The stored values are the current ones: the same response yields no further record
    third = WorldBankCollector((INTERNET,)).collect(make_ctx(started=datetime(2026, 12, 20, 2, 17, tzinfo=timezone.utc)),
                                                    entry.model_copy(update={"cursor": {}}))
    assert third.counters["records"] == 0 and third.counters["downloaded"] == 1


def test_all_pages_are_fetched(fake_web, make_ctx) -> None:
    data = _fixture(f"{INTERNET}.json")
    fake_web.add_fixture(COUNTRIES_URL, "world_bank/countries.json")
    fake_web.add(indicator_url(INTERNET), [{**data[0], "pages": 2}, data[1][:30]])
    fake_web.add(f"{indicator_url(INTERNET)}&page=2", [{**data[0], "page": 2, "pages": 2}, data[1][30:]])
    ctx = make_ctx()

    result = WorldBankCollector((INTERNET,)).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.counters["records"] == 6
    assert f"{indicator_url(INTERNET)}&page=2" in fake_web.requested


def test_archived_failing_and_incomplete_indicators_are_skipped(fake_web, make_ctx) -> None:
    fake_web.add_fixture(COUNTRIES_URL, "world_bank/countries.json")
    fake_web.add_fixture(indicator_url(ARCHIVED), f"world_bank/archived_{ARCHIVED}.json")
    data = _fixture(f"{INTERNET}.json")
    fake_web.add(indicator_url(INTERNET), [{**data[0], "total": 50}, data[1]])
    fake_web.add_fixture(indicator_url(RND), f"world_bank/{RND}.json")
    ctx = make_ctx()

    result = WorldBankCollector((ARCHIVED, "XX.UNKNOWN", INTERNET, RND)).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    assert result.counters["archived"] == 1 and result.counters["failed"] == 2
    assert result.counters["downloaded"] == 1 and result.error is None
    assert result.cursor == {"last_updated": {RND: "2026-10-08"}}
    assert {r["indicator_id"] for r in read_dataset(ctx.settings.output_dir, "world_bank_indicators")} == {RND}


def test_error_when_every_indicator_fails(fake_web, make_ctx) -> None:
    fake_web.add_fixture(COUNTRIES_URL, "world_bank/countries.json")
    fake_web.add_fixture(indicator_url(ARCHIVED), f"world_bank/archived_{ARCHIVED}.json")
    ctx = make_ctx()

    result = WorldBankCollector((ARCHIVED, "XX.UNKNOWN")).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.error == "all 2 requested indicators failed or are archived"


def test_missing_country_list_stops_the_collector(fake_web, make_ctx) -> None:
    fake_web.add_fixture(indicator_url(INTERNET), f"world_bank/{INTERNET}.json")
    ctx = make_ctx()

    result = WorldBankCollector((INTERNET, RND)).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.error and "country list" in result.error and not result.complete
    assert indicator_url(INTERNET) not in fake_web.requested


def test_collector_stops_at_the_deadline_and_at_a_rate_limit(fake_web, make_ctx) -> None:
    _serve_first_run(fake_web)
    ctx = make_ctx()
    ctx.past_deadline = lambda: indicator_url(INTERNET) in fake_web.requested

    stopped = WorldBankCollector((INTERNET, RND)).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert not stopped.complete and stopped.counters["deferred"] == 1 and stopped.error is None
    assert stopped.cursor == {"last_updated": {INTERNET: "2026-10-08"}}
    assert indicator_url(RND) not in fake_web.requested

    fake_web.add(indicator_url(RND), "slow down", status=429)
    limited = WorldBankCollector((RND, INTERNET)).collect(make_ctx(), CollectorEntry(first_run=ctx.today))
    assert not limited.complete and limited.counters["deferred"] == 2 and limited.error is None


def test_stored_date_without_stored_values_downloads_the_indicator(fake_web, make_ctx) -> None:
    _serve_first_run(fake_web)
    ctx = make_ctx()

    result = WorldBankCollector((INTERNET,)).collect(ctx, CollectorEntry(
        first_run=ctx.today, cursor={"last_updated": {INTERNET: "2026-10-08"}}))

    assert indicator_url(INTERNET, per_page=1) not in fake_web.requested
    assert result.counters["downloaded"] == 1 and result.counters["records"] == 6


def test_blocked_request_stops_the_collector(fake_web, make_ctx) -> None:
    _serve_first_run(fake_web)
    fake_web.add(COUNTRIES_URL, "Forbidden", status=403)
    ctx = make_ctx()

    result = WorldBankCollector((INTERNET, RND)).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.error and result.error.startswith("blocked") and not result.complete
    assert fake_web.requested == [COUNTRIES_URL]


def test_parse_rows_maps_income_groups_and_skips_unknown_countries() -> None:
    entities = parse_entities(_fixture("countries.json")[1])
    rows = _fixture(f"{INTERNET}.json")[1][:2] + [
        {"indicator": {"id": INTERNET, "value": "x"}, "country": {"id": "ZZ", "value": "Unknown"},
         "countryiso3code": "", "date": "2020", "value": 1.0},
        {"indicator": {"id": INTERNET, "value": "x"}, "country": {"id": "XD", "value": "High income"},
         "countryiso3code": "", "date": "2020Q1", "value": 1.0},
        {"indicator": {"id": INTERNET, "value": "x"}, "country": {"id": "XD", "value": "High income"},
         "countryiso3code": "", "date": "2020", "value": 88},
    ]

    data = parse_indicator_rows(rows, entities)

    assert entities["XD"].code == "HIC" and entities["XD"].is_aggregate
    assert entities["DE"].code == "DEU" and not entities["DE"].is_aggregate
    assert data.values == {"EUU": {}, "HIC": {"2020": 88.0}}
    assert data.skipped_rows == 2


def test_year_delta() -> None:
    changed, removed = year_delta({"2019": 1.0, "2020": 2.0, "2021": 3.0}, {"2020": 2.0, "2021": 3.5, "2022": 4.0})

    assert changed == {"2021": 3.5, "2022": 4.0}
    assert removed == ["2019"]

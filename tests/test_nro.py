"""Tests of the collector of the NRO delegated statistics."""
from datetime import date, datetime, timezone

import pytest

from collectors import nro
from collectors.nro import (LATEST_URL, LICENSE, NroCollector, aggregate_resources, archive_months, archive_url,
                            iter_lines, read_version)
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

SAMPLE = (FIXTURES_DIR / "nro" / "nro-delegated-stats_sample.txt").read_text(encoding="utf-8")
SAMPLE_GROUPS = 34
# Lines of older files and lines that the current file does not contain
UNUSUAL_LINES = """\
2|nro|20191007|9|19821213|20191007|+0000
nro|*|asn|*|3|summary
nro|*|ipv4|*|3|summary
nro|*|ipv6|*|2|summary
# comment
iana|ZZ|asn|0|1|20140311|ietf|ietf|iana
arin|US|asn|1|1|00000000|assigned|e5e3b9c13678dfc483fb1f819d70883c|e-stats
ripencc|DE|asn|28|x|19930901|assigned|2849f34d-3e57-4e2a-8464-953adab9ef02|e-stats
ripencc|DE|ipv4|2.28.0.0|262144|20100921|allocated|4049ad1f-e0fa-43c6-9353-ca512a58ddfa|e-stats
ripencc|DE|ipv4|31.42.180.0|768|19700101|assigned|cb852e71-036d-429d-b681-fac69c17751c|e-stats
apnic||ipv6|2001:de3::|48|20250703|assigned|A919DB08|e-stats
apnic|ZZ|ipv6|2001:7fa::|64|20261024|reserved|apnic|e-stats
lacnic|BR|ipv5|1.2.3.4|1|20200101|assigned|1251|e-stats
broken|line
"""
LATER = datetime(2026, 11, 23, 2, 17, tzinfo=timezone.utc)


def _file_of(day: str) -> str:
    """Returns the sample with the version line of another file date (YYYYMMDD)."""
    return f"2|nro|{day}|151|19821213|{day}|+0000" + SAMPLE[SAMPLE.index("\n"):]


def _serve_latest(fake_web, etag: str = '"6adc90ea-3665b9b"') -> None:
    fake_web.add(LATEST_URL, SAMPLE, content_type="text/plain", headers={"etag": etag})


def test_lines_are_summed_per_registry_country_and_status_with_unusual_lines_counted() -> None:
    lines = iter_lines(UNUSUAL_LINES.encode("utf-8"))

    file_date, declared = read_version(lines)
    groups, counters = aggregate_resources(lines)

    assert file_date == date(2019, 10, 7) and declared == 9
    assert counters == {"lines": 9, "unknown_status": 1, "malformed": 2, "unknown_type": 1, "empty_country": 1}
    assert set(groups) == {("arin", "US", "assigned"), ("ripencc", "DE", "assigned"), ("apnic", "ZZ", "assigned"),
                           ("apnic", "ZZ", "reserved")}
    # allocated counts as assigned; a range of 768 addresses is not a CIDR block and counts as it is
    germany = groups[("ripencc", "DE", "assigned")]
    assert (germany.ipv4_addresses, germany.ipv4_ranges) == (262144 + 768, 2)
    # A line without a country counts under ZZ; prefixes longer than /32 are fractions of a /32
    assert groups[("apnic", "ZZ", "assigned")].ipv6_slash32_equivalents == 2 ** -16
    assert groups[("apnic", "ZZ", "reserved")].ipv6_slash32_equivalents == 2 ** -32
    assert (groups[("arin", "US", "assigned")].asns, groups[("arin", "US", "assigned")].asn_ranges) == (1, 1)


def test_first_run_writes_one_snapshot_record_per_registry_country_and_status(fake_web, make_ctx) -> None:
    _serve_latest(fake_web)
    ctx = make_ctx()

    result = NroCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    records = {r["record_id"]: r for r in read_dataset(ctx.settings.output_dir, "ip_address_delegations")}
    assert len(records) == SAMPLE_GROUPS
    arin = records["nro:2026-10-24:arin:US:assigned"]
    assert (arin["ipv4_addresses"], arin["ipv4_ranges"], arin["asns"], arin["asn_ranges"]) == (3584, 3, 69, 4)
    assert (arin["ipv6_slash32_equivalents"], arin["ipv6_prefixes"]) == (2.0, 2)
    assert arin["registry"] == "arin" and arin["country"] == "US" and arin["status"] == "assigned"
    assert arin["file_date"] == "2026-10-24" and arin["revision"] == 0 and arin["backfilled"] is False
    # The records name the archive URL of the file date, which stays valid
    assert arin["source_url"] == "https://ftp.ripe.net/pub/stats/ripencc/nro-stats/20261024/nro-delegated-stats"
    assert arin["license"] == LICENSE
    iana = records["nro:2026-10-24:iana:ZZ:available"]
    assert (iana["asns"], iana["ipv4_addresses"], iana["ipv6_slash32_equivalents"]) == (135053, 768, 2 ** 9 * 2 + 2 ** 13)
    apnic_pool = records["nro:2026-10-24:apnic:ZZ:reserved"]
    assert apnic_pool["ipv6_slash32_equivalents"] == 2 ** -3 + 1 + 2 ** -32 and apnic_pool["ipv6_prefixes"] == 3
    assert (apnic_pool["asns"], apnic_pool["asn_ranges"], apnic_pool["ipv4_addresses"]) == (5, 3, 2304)
    assert records["nro:2026-10-24:apnic:ZZ:assigned"]["ipv6_slash32_equivalents"] == 2 ** -16
    # Totals of the sample, computed independently with awk
    assert sum(r["ipv4_addresses"] for r in records.values()) == 42467072
    assert sum(r["ipv4_ranges"] for r in records.values()) == 47
    assert sum(r["asns"] for r in records.values()) == 136416
    assert sum(r["ipv6_prefixes"] for r in records.values()) == 53
    assert sum(r["ipv6_slash32_equivalents"] for r in records.values()) == pytest.approx(67118184.6291351318)
    assert result.counters["lines"] == 151 and result.counters["empty_country"] == 1
    assert "count_mismatch" not in result.counters
    assert result.cursor == {"etag": '"6adc90ea-3665b9b"', "file_date": "2026-10-24"}
    assert result.complete and result.error is None


def test_next_run_sends_the_etag_and_skips_an_unchanged_file(fake_web, make_ctx) -> None:
    conditions: list[tuple[str, str]] = []
    serve = fake_web.handler

    def recording_handler(request):
        conditions.append((str(request.url), request.headers.get("if-none-match", "")))
        return serve(request)

    fake_web.handler = recording_handler
    _serve_latest(fake_web, etag='"v1"')
    ctx = make_ctx()
    first = NroCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    fake_web.add(LATEST_URL, b"", status=304, content_type="text/plain")

    second = NroCollector().collect(make_ctx(started=LATER), CollectorEntry(first_run=ctx.today, cursor=first.cursor))

    assert conditions[-1] == (LATEST_URL, '"v1"')
    assert second.counters["not_modified"] == 1 and second.counters["records"] == 0
    assert second.cursor == first.cursor and second.complete and second.error is None
    assert len(read_dataset(ctx.settings.output_dir, "ip_address_delegations")) == SAMPLE_GROUPS


def test_a_changed_file_with_a_stored_date_is_not_written_again(fake_web, make_ctx) -> None:
    _serve_latest(fake_web, etag='"v1"')
    ctx = make_ctx()
    first = NroCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    _serve_latest(fake_web, etag='"v2"')

    second = NroCollector().collect(make_ctx(started=LATER), CollectorEntry(first_run=ctx.today, cursor=first.cursor))

    assert second.counters["already_stored"] == 1 and second.counters["records"] == 0
    assert second.cursor["etag"] == '"v2"'
    assert len(read_dataset(ctx.settings.output_dir, "ip_address_delegations")) == SAMPLE_GROUPS


def test_archive_quarters_start_in_october_2021_and_exclude_the_run_day() -> None:
    assert archive_months(date(2022, 1, 1)) == [date(2021, 10, 1)]
    assert archive_months(date(2022, 1, 2))[-1] == date(2022, 1, 1)
    assert len(archive_months(date(2026, 10, 26))) == 21
    assert archive_url(date(2021, 10, 1)) == "https://ftp.ripe.net/pub/stats/ripencc/nro-stats/20211001/nro-delegated-stats"


def test_backfill_imports_monthly_archive_files_and_later_runs_continue_after_the_deadline(
        fake_web, make_ctx, monkeypatch) -> None:
    monkeypatch.setattr(nro, "ARCHIVE_START", date(2026, 8, 1))
    monkeypatch.setattr(nro, "ARCHIVE_STEP_MONTHS", 1)
    monkeypatch.setattr(nro, "ARCHIVE_FILES_PER_REGULAR_RUN", 1)
    _serve_latest(fake_web)
    for day in ("20260801", "20260901", "20261001"):
        fake_web.add(f"https://ftp.ripe.net/pub/stats/ripencc/nro-stats/{day}/nro-delegated-stats", _file_of(day),
                     content_type="text/plain")
    ctx = make_ctx(backfill=True)
    checks = iter([False, False])
    monkeypatch.setattr(ctx, "past_deadline", lambda: next(checks, True))

    first = NroCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    entry = CollectorEntry(first_run=ctx.today, cursor=first.cursor)
    second = NroCollector().collect(make_ctx(started=datetime(2026, 10, 27, 2, 17, tzinfo=timezone.utc)), entry)
    entry = CollectorEntry(first_run=ctx.today, cursor=second.cursor)
    third = NroCollector().collect(make_ctx(started=datetime(2026, 10, 29, 2, 17, tzinfo=timezone.utc)), entry)

    # The backfill run stops at the deadline after the current file and one archive file
    assert not first.complete and first.counters["deferred"] == 2 and first.cursor["archive"] == "pending"
    # Regular runs continue with at most ARCHIVE_FILES_PER_REGULAR_RUN archive files
    assert not second.complete and second.counters["deferred"] == 1 and second.counters["already_stored"] == 1
    assert third.complete and third.cursor["archive"] == "done"
    records = read_dataset(ctx.settings.output_dir, "ip_address_delegations")
    assert sorted({r["file_date"] for r in records}) == ["2026-08-01", "2026-09-01", "2026-10-01", "2026-10-24"]
    assert len(records) == 4 * SAMPLE_GROUPS
    archived = next(r for r in records if r["record_id"] == "nro:2026-08-01:arin:US:assigned")
    assert archived["backfilled"] is True
    assert archived["source_url"] == "https://ftp.ripe.net/pub/stats/ripencc/nro-stats/20260801/nro-delegated-stats"


def test_a_missing_archive_month_is_not_requested_again(fake_web, make_ctx, monkeypatch) -> None:
    monkeypatch.setattr(nro, "ARCHIVE_START", date(2026, 9, 1))
    _serve_latest(fake_web)
    fake_web.add("https://ftp.ripe.net/pub/stats/ripencc/nro-stats/20261001/nro-delegated-stats", _file_of("20261001"),
                 content_type="text/plain")
    ctx = make_ctx(backfill=True)
    first = NroCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    requests = len(fake_web.requested)

    second = NroCollector().collect(make_ctx(backfill=True), CollectorEntry(first_run=ctx.today, cursor=first.cursor))

    assert first.counters["archive_missing"] == 1 and first.cursor["archive_missing"] == ["2026-09-01"]
    assert first.cursor["archive"] == "done" and first.complete and first.error is None
    assert not any("20260901" in url for url in fake_web.requested[requests:])
    assert second.cursor["archive"] == "done" and second.counters["records"] == 0


def test_a_failed_download_sets_the_error_and_keeps_the_cursor(fake_web, make_ctx) -> None:
    fake_web.fail(LATEST_URL)
    ctx = make_ctx()

    result = NroCollector().collect(ctx, CollectorEntry(first_run=ctx.today, cursor={"etag": '"v1"'}))

    assert result.error == "all 1 requested files failed" and result.counters["failed"] == 1
    assert result.cursor == {"etag": '"v1"'}
    assert read_dataset(ctx.settings.output_dir, "ip_address_delegations") == []


def test_a_rate_limit_stops_the_run_without_an_error(fake_web, make_ctx) -> None:
    fake_web.add(LATEST_URL, "Too many requests", status=429, content_type="text/plain")
    ctx = make_ctx()

    result = NroCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert not result.complete and result.error is None and result.counters["records"] == 0


def test_a_run_after_the_deadline_sends_no_request(fake_web, make_ctx) -> None:
    _serve_latest(fake_web)
    ctx = make_ctx()
    ctx.deadline = ctx.started

    result = NroCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert not result.complete and fake_web.requested == []

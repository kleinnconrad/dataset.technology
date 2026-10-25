"""Tests of the Indeed job postings collector."""
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from collectors.job_postings import (AGGREGATE_URL, AI_URL, SECTOR_URL, JobPostingsCollector, parse_sectors,
                                     slugify, source_files)
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

LATER = datetime(2026, 11, 2, 2, 17, tzinfo=timezone.utc)
FILES = source_files(aggregate_countries=("US", "EA"), sector_countries=("US", "DE"))
US_AGGREGATE = AGGREGATE_URL.format(country="US")
EA_AGGREGATE = AGGREGATE_URL.format(country="EA")
US_SECTORS = SECTOR_URL.format(country="US")
DE_SECTORS = SECTOR_URL.format(country="DE")
URLS = (US_AGGREGATE, EA_AGGREGATE, AI_URL, US_SECTORS, DE_SECTORS)


def _text(url: str, crlf: bool = False) -> str:
    """Returns the fixture of a file URL with LF or CRLF line endings."""
    path = FIXTURES_DIR / "job_postings" / url.rsplit("/", 1)[-1]
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return text.replace("\n", "\r\n") if crlf else text


def _serve(fake_web, url: str, text: str, etag: Optional[str] = None) -> None:
    fake_web.add(url, text.encode("utf-8"), content_type="text/plain; charset=utf-8",
                 headers={"etag": etag or f'W/"v1-{url.rsplit("/", 1)[-1]}"'})


def _serve_all(fake_web) -> None:
    # Two files with CRLF line endings, the others with LF; the csv module reads both
    for url in URLS:
        _serve(fake_web, url, _text(url, crlf=url in (US_AGGREGATE, US_SECTORS)))


def _record_headers(fake_web, monkeypatch) -> list[tuple[str, Optional[str]]]:
    """Records the If-None-Match header of every request."""
    sent: list[tuple[str, Optional[str]]] = []
    handler = fake_web.handler

    def recording_handler(request: httpx.Request) -> httpx.Response:
        sent.append((str(request.url), request.headers.get("if-none-match")))
        return handler(request)

    monkeypatch.setattr(fake_web, "handler", recording_handler)
    return sent


def _by_id(ctx, dataset: str) -> dict[str, dict]:
    return {r["record_id"]: r for r in read_dataset(ctx.settings.output_dir, dataset)}


def test_first_run_stores_the_whole_history_of_every_series(fake_web, make_ctx) -> None:
    _serve_all(fake_web)
    ctx = make_ctx()

    result = JobPostingsCollector(FILES).collect(ctx, CollectorEntry(first_run=ctx.today))

    jobs = _by_id(ctx, "job_postings")
    us_sectors = ("banking-finance", "data-analytics", "it-infrastructure-operations-support", "it-systems-solutions",
                  "software-development")
    de_sectors = ("mechanical-engineering", "it-systems-solutions", "software-development")
    assert set(jobs) == {
        "jobs:US:total-postings:2026-10-02:2026-10-16", "jobs:US:new-postings:2026-10-02:2026-10-16",
        "jobs:EA:new-postings:2026-10-07:2026-10-16",
        *(f"jobs:US:{slug}:2026-10-14:2026-10-16" for slug in us_sectors),
        *(f"jobs:DE:{slug}:2026-10-12:2026-10-16" for slug in de_sectors),
    }
    total = jobs["jobs:US:total-postings:2026-10-02:2026-10-16"]
    # The seasonally adjusted index is stored, not the unadjusted one (108.46)
    assert total["daily_index"]["2026-10-02"] == 103.84 and len(total["daily_index"]) == 15
    assert total["category"] == "total postings" and total["country"] == "US"
    assert total["seasonally_adjusted"] is True and total["revision"] == 0 and total["backfilled"] is True
    assert total["source_url"] == US_AGGREGATE and total["source_id"] == "indeed_job_postings"
    assert total["license"] == "CC-BY-4.0 (Indeed Hiring Lab)"
    assert jobs["jobs:US:new-postings:2026-10-02:2026-10-16"]["daily_index"]["2026-10-16"] == 99.19
    # Quoted category with a comma; only the index of total postings is stored
    infrastructure = jobs["jobs:US:it-infrastructure-operations-support:2026-10-14:2026-10-16"]
    assert infrastructure["category"] == "IT Infrastructure, Operations & Support"
    assert infrastructure["daily_index"] == {"2026-10-14": 68.46, "2026-10-15": 68.49, "2026-10-16": 68.48}
    assert infrastructure["seasonally_adjusted"] is True and infrastructure["source_url"] == US_SECTORS
    ai = _by_id(ctx, "ai_job_postings")
    assert set(ai) == {f"ai-jobs:{country}:2026-09-27:2026-09-30"
                       for country in ("AU", "CA", "DE", "FR", "GB", "IE", "IT", "NL", "US")}
    us_ai = ai["ai-jobs:US:2026-09-27:2026-09-30"]
    assert us_ai["daily_share_pct"] == {"2026-09-27": 7.30375656977849, "2026-09-28": 7.32025176043087,
                                        "2026-09-29": 7.32689967995836, "2026-09-30": 7.33818434887748}
    assert us_ai["country"] == "US" and us_ai["source_id"] == "indeed_ai_tracker" and us_ai["backfilled"] is True
    assert result.cursor == {"etags": {url.rsplit("/", 1)[-1]: f'W/"v1-{url.rsplit("/", 1)[-1]}"' for url in URLS}}
    assert result.counters["downloaded"] == 5 and result.counters["records"] == 20 and result.complete


def test_next_run_stores_only_the_days_after_the_last_stored_day(fake_web, make_ctx) -> None:
    _serve_all(fake_web)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    first = JobPostingsCollector(FILES).collect(ctx, entry)

    # Hiring Lab revises the history at every update; the revised days are not stored again
    lines = _text(US_AGGREGATE).splitlines()
    revised = [lines[0]] + [line.replace(",US,", ",US,1") for line in lines[1:]]
    for offset in range(7):
        day = (date(2026, 10, 17) + timedelta(days=offset)).isoformat()
        revised += [f"{day},US,{105 + offset},109,total postings", f"{day},US,{100 + offset},104,new postings"]
    _serve(fake_web, US_AGGREGATE, "\r\n".join(revised) + "\r\n", etag='W/"v2"')

    second = JobPostingsCollector(FILES).collect(make_ctx(started=LATER), entry.model_copy(
        update={"cursor": first.cursor}))

    jobs = sorted((r for r in read_dataset(ctx.settings.output_dir, "job_postings")
                   if r["country"] == "US" and r["category"] == "total postings"), key=lambda r: r["record_id"])
    assert [r["record_id"] for r in jobs] == ["jobs:US:total-postings:2026-10-02:2026-10-16",
                                              "jobs:US:total-postings:2026-10-17:2026-10-23"]
    assert jobs[0]["daily_index"]["2026-10-02"] == 103.84
    assert jobs[1]["daily_index"] == {f"2026-10-{17 + offset}": 105.0 + offset for offset in range(7)}
    assert jobs[1]["backfilled"] is False and jobs[1]["revision"] == 0
    assert second.counters["records"] == 2 and second.counters["days"] == 14
    assert second.cursor["etags"]["aggregate_job_postings_US.csv"] == 'W/"v2"'


def test_unchanged_files_are_skipped_with_conditional_requests(fake_web, make_ctx, monkeypatch) -> None:
    _serve_all(fake_web)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    first = JobPostingsCollector(FILES).collect(ctx, entry)
    sent = _record_headers(fake_web, monkeypatch)
    for url in URLS:
        fake_web.add(url, b"", status=304)

    second = JobPostingsCollector(FILES).collect(make_ctx(started=LATER), entry.model_copy(
        update={"cursor": first.cursor}))

    assert sent == [(url, f'W/"v1-{url.rsplit("/", 1)[-1]}"') for url in URLS]
    assert second.counters["not_modified"] == 5 and second.counters["records"] == 0
    assert second.cursor == first.cursor and second.complete and second.error is None
    assert len(read_dataset(ctx.settings.output_dir, "job_postings")) == 11


def test_file_without_stored_series_is_downloaded_unconditionally(fake_web, make_ctx, monkeypatch) -> None:
    _serve_all(fake_web)
    sent = _record_headers(fake_web, monkeypatch)
    ctx = make_ctx()
    cursor = {"etags": {"aggregate_job_postings_US.csv": 'W/"old"', "AI_posting.csv": 'W/"old"'}}

    result = JobPostingsCollector(FILES).collect(ctx, CollectorEntry(first_run=ctx.today, cursor=cursor))

    assert all(header is None for _, header in sent)
    assert result.counters["downloaded"] == 5


def test_failed_and_malformed_files_are_counted(fake_web, make_ctx) -> None:
    _serve_all(fake_web)
    del fake_web.routes[EA_AGGREGATE]
    _serve(fake_web, DE_SECTORS, "date,jobcountry,indeed_job_postings_index,variable\n2026-10-12,DE,1,total postings\n")
    ctx = make_ctx()

    partial = JobPostingsCollector(FILES).collect(ctx, CollectorEntry(first_run=ctx.today))
    failed = JobPostingsCollector(source_files(("EA",), (), ai_tracker=False)).collect(
        make_ctx(), CollectorEntry(first_run=ctx.today))

    assert partial.counters["failed"] == 2 and partial.counters["downloaded"] == 3 and partial.error is None
    assert "job_postings_by_sector_DE.csv" not in partial.cursor["etags"]
    assert failed.error == "all 1 requested files failed"


def test_series_that_disappears_from_a_file_is_reported(fake_web, make_ctx) -> None:
    _serve_all(fake_web)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    first = JobPostingsCollector(FILES).collect(ctx, entry)
    without = "\n".join(line for line in _text(US_SECTORS).splitlines() if "Data & Analytics" not in line)
    _serve(fake_web, US_SECTORS, without, etag='W/"v2"')

    second = JobPostingsCollector(FILES).collect(make_ctx(started=LATER), entry.model_copy(
        update={"cursor": first.cursor}))

    assert second.counters["missing_series"] == 1 and second.counters["records"] == 0


def test_collector_stops_at_the_deadline_and_at_a_rate_limit(fake_web, make_ctx) -> None:
    _serve_all(fake_web)
    ctx = make_ctx()
    ctx.past_deadline = lambda: len(fake_web.requested) >= 2

    stopped = JobPostingsCollector(FILES).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert not stopped.complete and stopped.counters["deferred"] == 3 and stopped.error is None
    assert set(stopped.cursor["etags"]) == {"aggregate_job_postings_US.csv", "aggregate_job_postings_EA.csv"}

    fake_web.add(US_AGGREGATE, "slow down", status=429)
    limited = JobPostingsCollector(FILES).collect(make_ctx(), CollectorEntry(first_run=ctx.today))
    assert not limited.complete and limited.counters["deferred"] == 5 and limited.error is None


def test_blocked_request_stops_the_collector(fake_web, make_ctx) -> None:
    _serve_all(fake_web)
    fake_web.add(US_AGGREGATE, "Forbidden", status=403)
    ctx = make_ctx()

    result = JobPostingsCollector(FILES).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.error and result.error.startswith("blocked") and not result.complete
    assert fake_web.requested == [US_AGGREGATE]


def test_parse_sectors_keeps_total_postings_of_selected_categories() -> None:
    series = parse_sectors(_text(DE_SECTORS, crlf=True), categories=("Software Development", "Accounting"))

    assert series == {"Software Development": {date(2026, 10, 12): 47.32, date(2026, 10, 13): 47.22,
                                               date(2026, 10, 14): 47.22, date(2026, 10, 15): 47.42,
                                               date(2026, 10, 16): 47.69}}


def test_slugify() -> None:
    assert slugify("IT Infrastructure, Operations & Support") == "it-infrastructure-operations-support"
    assert slugify("total postings") == "total-postings"

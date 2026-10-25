"""Tests of the collector of the weekly Internet-Draft counts of the IETF Datatracker."""
from datetime import date, datetime, timedelta, timezone

from collectors.ietf_drafts import (
    EVENTS_URL, LICENSE, SCOPES, IetfDraftsCollector, count_url, last_complete_week, mondays, scope_url,
)
from conftest import read_dataset
from state import CollectorEntry

NEXT_WEEK = datetime(2026, 11, 2, 2, 17, tzinfo=timezone.utc)
SAME_WEEK = datetime(2026, 10, 28, 2, 17, tzinfo=timezone.utc)
LAST_WEEK = date(2026, 10, 19)
FIRST_WEEK = date(2026, 8, 31)


def _count(total: int) -> dict:
    """A Datatracker response with the given total count."""
    return {"meta": {"limit": 1, "next": None, "offset": 0, "previous": None, "total_count": total}, "objects": []}


def _event_requests(fake_web) -> list[str]:
    return [url for url in fake_web.requested if url.startswith(EVENTS_URL)]


def _records(ctx) -> dict[str, dict]:
    return {record["record_id"]: record for record in read_dataset(ctx.settings.output_dir, "ietf_draft_activity")}


def test_weeks_are_iso_weeks_that_ended_before_the_run_day() -> None:
    assert last_complete_week(date(2026, 10, 26)) == LAST_WEEK
    assert last_complete_week(date(2026, 11, 1)) == LAST_WEEK
    assert last_complete_week(date(2026, 10, 27)) == LAST_WEEK
    assert mondays(date(2026, 10, 5), LAST_WEEK) == [date(2026, 10, 5), date(2026, 10, 12), LAST_WEEK]
    assert count_url("all", LAST_WEEK, new_only=False) == (
        f"{EVENTS_URL}?time__gte=2026-10-19T00:00:00&time__lt=2026-10-26T00:00:00&doc__type=draft&limit=1&format=json")
    assert count_url("area:wit", LAST_WEEK, new_only=True).endswith(
        "&doc__type=draft&doc__group__parent__acronym=wit&doc__group__type=wg&rev=00&limit=1&format=json")
    assert scope_url("group_type:individ") == f"{EVENTS_URL}?doc__type=draft&doc__group__type=individ"


def test_new_scopes_get_the_last_eight_weeks(fake_web, make_ctx) -> None:
    fake_web.add(EVENTS_URL, _count(5))
    fake_web.add_fixture(count_url("all", LAST_WEEK, new_only=False),
                         "ietf_drafts/newrevisiondocevent_draft_week_count.json")
    fake_web.add(count_url("all", LAST_WEEK, new_only=True), _count(80))
    fake_web.add(count_url("group_type:wg", date(2026, 10, 12), new_only=False), _count(116))
    ctx = make_ctx()

    result = IetfDraftsCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    records = _records(ctx)
    assert sorted(records) == sorted(f"ietf-drafts:{scope}:2026-08-31:2026-10-19" for scope in SCOPES)
    everything = records["ietf-drafts:all:2026-08-31:2026-10-19"]
    assert everything["scope"] == "all" and everything["revision"] == 0 and everything["license"] == LICENSE
    assert everything["weekly_revisions"]["2026-10-19"] == 323 and everything["weekly_new_drafts"]["2026-10-19"] == 80
    assert list(everything["weekly_revisions"]) == [week.isoformat() for week in mondays(FIRST_WEEK, LAST_WEEK)]
    assert everything["weekly_revisions"]["2026-08-31"] == 5 and everything["backfilled"] is True
    assert everything["source_url"] == scope_url("all")
    assert records["ietf-drafts:group_type:wg:2026-08-31:2026-10-19"]["weekly_revisions"]["2026-10-12"] == 116
    assert result.complete and result.counters["records"] == 11 and result.counters["weeks"] == 88
    assert len(_event_requests(fake_web)) == 11 * 8 * 2


def test_next_runs_fetch_only_the_weeks_that_ended_since_the_last_stored_week(fake_web, make_ctx) -> None:
    fake_web.add(EVENTS_URL, _count(5))
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    IetfDraftsCollector().collect(ctx, entry)
    first_requests = len(_event_requests(fake_web))

    same_week = IetfDraftsCollector().collect(make_ctx(started=SAME_WEEK), entry)
    next_week = IetfDraftsCollector().collect(make_ctx(started=NEXT_WEEK), entry)

    assert same_week.counters["records"] == 0 and same_week.complete
    assert len(_event_requests(fake_web)) - first_requests == 11 * 2
    assert next_week.counters["records"] == 11 and next_week.counters["weeks"] == 11
    records = _records(ctx)
    assert "ietf-drafts:area:sec:2026-10-26:2026-10-26" in records
    assert records["ietf-drafts:area:sec:2026-10-26:2026-10-26"]["backfilled"] is False
    assert all(record["revision"] == 0 for record in records.values())


def test_backfill_fetches_the_older_weeks_newest_first(fake_web, make_ctx) -> None:
    fake_web.add(EVENTS_URL, _count(5))
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    IetfDraftsCollector().collect(ctx, entry)
    first_requests = len(_event_requests(fake_web))

    result = IetfDraftsCollector().collect(make_ctx(backfill=True), entry)

    history_start = LAST_WEEK - timedelta(weeks=103)
    gap = mondays(history_start, FIRST_WEEK - timedelta(weeks=1))
    backfill_requests = _event_requests(fake_web)[first_requests:]
    newest_first = [count_url("all", week, new_only=new_only)
                    for week in (gap[-1], gap[-2]) for new_only in (False, True)]
    assert backfill_requests[:4] == newest_first
    assert len(backfill_requests) == 11 * len(gap) * 2 and result.complete
    record = _records(ctx)[f"ietf-drafts:all:{history_start.isoformat()}:{gap[-1].isoformat()}"]
    assert len(record["weekly_revisions"]) == 96 and record["backfilled"] is True
    stored = make_ctx().seen("ietf_draft_activity").get("all")
    assert stored is not None and stored.data == {"first_week": history_start.isoformat(), "last_week": "2026-10-19"}
    rerun = IetfDraftsCollector().collect(make_ctx(backfill=True), entry)
    assert rerun.counters["records"] == 0


def test_deadline_stores_the_complete_weeks_and_the_next_run_continues(fake_web, make_ctx) -> None:
    fake_web.add(EVENTS_URL, _count(5))
    ctx = make_ctx()
    ctx.past_deadline = lambda: len(_event_requests(fake_web)) >= 20
    entry = CollectorEntry(first_run=ctx.today)

    stopped = IetfDraftsCollector().collect(ctx, entry)
    resumed = IetfDraftsCollector().collect(make_ctx(), entry)

    assert not stopped.complete and stopped.counters["deferred"] == 10 and stopped.error is None
    records = _records(ctx)
    assert "ietf-drafts:group_type:wg:2026-08-31:2026-09-07" in records
    assert "ietf-drafts:group_type:wg:2026-09-14:2026-10-19" in records
    assert resumed.complete and resumed.counters["records"] == 10
    assert len(records) == 12


def test_a_failing_scope_is_skipped_and_a_rate_limit_stops_the_run(fake_web, make_ctx) -> None:
    fake_web.add(EVENTS_URL, _count(5))
    fake_web.add(count_url("group_type:rg", FIRST_WEEK, new_only=False), b"", status=500)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)

    one_failure = IetfDraftsCollector().collect(ctx, entry)
    fake_web.add(count_url("group_type:rg", FIRST_WEEK, new_only=False), b"", status=429)
    limited = IetfDraftsCollector().collect(make_ctx(), entry)

    assert not one_failure.complete and one_failure.error is None and one_failure.counters["failed"] == 1
    assert one_failure.counters["records"] == 10
    # The rate-limited scope (position 3) and the seven scopes after it follow in the next run
    assert not limited.complete and limited.error is None and limited.counters["deferred"] == len(SCOPES) - 3
    assert limited.counters["records"] == 0


def test_consecutive_failing_scopes_stop_the_run_with_an_error(fake_web, make_ctx) -> None:
    fake_web.add(EVENTS_URL, _count(5))
    fake_web.add(count_url("group_type:wg", FIRST_WEEK, new_only=True), b"", status=500)
    fake_web.add(count_url("group_type:individ", FIRST_WEEK, new_only=False), b"", status=503)
    ctx = make_ctx()

    result = IetfDraftsCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert "Datatracker failed" in (result.error or "") and result.counters["failed"] == 2
    assert result.counters["deferred"] == len(SCOPES) - 3 and result.counters["records"] == 1
    assert list(_records(ctx)) == ["ietf-drafts:all:2026-08-31:2026-10-19"]

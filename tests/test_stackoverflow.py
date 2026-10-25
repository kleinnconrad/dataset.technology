"""Tests of the Stack Overflow question count collector."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from collectors.stackoverflow import (LICENSE, QUESTIONS_URL, StackOverflowCollector, last_complete_week,
                                      questions_params, tags_info_url, week_bounds)
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

DATASET = "stackoverflow_questions"
# The weeks of a new tag in a normal run that starts on Monday 2026-10-26
WEEKS = [date(2026, 9, 28), date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 19)]
NEXT_RUN = datetime(2026, 11, 2, 2, 17, tzinfo=timezone.utc)


def _registry(tmp_path: Path, **tags: str) -> Path:
    """Writes a registry with one technology per tag, keyed by the technology id."""
    entries = [f'[[technology]]\nid = "{technology_id}"\nname = "{technology_id}"\ncategory = "programming_language"\n'
               f'added = 2026-10-01\nstackoverflow_tags = ["{tag}"]\n' for technology_id, tag in tags.items()]
    path = tmp_path / "technologies.toml"
    path.write_text("\n".join(entries), encoding="utf-8")
    return path


def _week_url(tag: str, monday: date) -> str:
    """The URL that the collector requests for the questions of a tag in one week (without a key)."""
    return str(httpx.URL(QUESTIONS_URL, params=questions_params(tag, monday)))


def _total(total: int, quota_remaining: int = 290, backoff: Optional[int] = None) -> dict[str, Any]:
    """A response of the total filter."""
    return {"total": total, "quota_max": 300, "quota_remaining": quota_remaining,
            **({"backoff": backoff} if backoff is not None else {})}


def _tags_info(*items: tuple[str, int]) -> dict[str, Any]:
    return {"items": [{"name": name, "count": count} for name, count in items], "has_more": False,
            "quota_max": 300, "quota_remaining": 295}


def _records(ctx) -> dict[str, list[dict[str, Any]]]:
    """Records by tag, ordered by record id."""
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for record in sorted(read_dataset(ctx.settings.output_dir, DATASET), key=lambda r: r["record_id"]):
        by_tag.setdefault(record["tag"], []).append(record)
    return by_tag


def test_weeks_end_on_sunday_before_the_run_day() -> None:
    # Bounds of the verified request for the week from 2026-10-12 to 2026-10-18
    assert week_bounds(date(2026, 10, 12)) == (1791763200, 1792367999)
    assert last_complete_week(date(2026, 10, 26)) == date(2026, 10, 19)
    assert last_complete_week(date(2026, 10, 25)) == date(2026, 10, 12)


def test_new_tags_get_four_weeks_and_the_tag_totals(fake_web, make_ctx, tmp_path) -> None:
    registry = _registry(tmp_path, python="python", rust="rust", go="golang", csharp="c#", cpp="c++")
    info_url = ("https://api.stackexchange.com/2.3/tags/python;rust;golang;c%23;c%2B%2B/info"
                "?site=stackoverflow&pagesize=100")
    fake_web.add(info_url, json.loads((FIXTURES_DIR / "stackoverflow" / "tags_info_python_rust_golang_csharp_cpp.json")
                                      .read_text(encoding="utf-8")))
    fake_web.add(QUESTIONS_URL, _total(3))
    # Verified counts of the week from 2026-10-12 to 2026-10-18; the rust response is the one of the total filter
    fake_web.add(_week_url("python", date(2026, 10, 12)), _total(28))
    fake_web.add_fixture(_week_url("rust", date(2026, 10, 12)), "stackoverflow/questions_rust_2026-10-12.json")
    ctx = make_ctx()

    result = StackOverflowCollector(registry).collect(ctx, CollectorEntry(first_run=ctx.today))

    records = _records(ctx)
    assert set(records) == {"python", "rust", "golang", "c#", "c++"}
    python = records["python"][0]
    assert python["record_id"] == "stackoverflow:python:2026-09-28:2026-10-19"
    assert python["weekly_questions"] == {"2026-09-28": 3, "2026-10-05": 3, "2026-10-12": 28, "2026-10-19": 3}
    assert python["technology_id"] == "python" and python["total_questions"] == 2204253
    assert python["revision"] == 0 and python["backfilled"] is True and python["license"] == LICENSE
    assert python["source_url"] == ("https://api.stackexchange.com/2.3/questions?tagged=python&fromdate=1790553600"
                                    "&todate=1792972799&site=stackoverflow&filter=%219n30I5fDgPX4")
    assert records["rust"][0]["weekly_questions"]["2026-10-12"] == 4
    assert {tag: records[tag][0]["total_questions"] for tag in ("rust", "c#", "c++")} == {
        "rust": 43978, "c#": 1621624, "c++": 813955}
    # golang is a synonym of go: the API returns it under the name of the master tag
    assert records["golang"][0]["total_questions"] is None
    assert info_url in fake_web.requested
    assert _week_url("c#", date(2026, 10, 19)).startswith(f"{QUESTIONS_URL}?tagged=c%23&")
    assert _week_url("c#", date(2026, 10, 19)) in fake_web.requested
    assert result.counters == {"tags": 5, "records": 5, "weeks": 20, "failed": 0, "missing_totals": 1,
                               "quota_remaining": 290}
    assert result.complete and result.error is None
    assert ctx.seen(DATASET).get("python").data == {"last_week": "2026-10-19"}


def test_next_run_fetches_only_the_weeks_after_the_last_stored_week(fake_web, make_ctx, tmp_path) -> None:
    registry = _registry(tmp_path, python="python")
    fake_web.add(tags_info_url(["python"]), _tags_info(("python", 2204253)))
    fake_web.add(QUESTIONS_URL, _total(3))
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    StackOverflowCollector(registry).collect(ctx, entry)

    fake_web.requested.clear()
    fake_web.add(_week_url("python", date(2026, 10, 26)), _total(27))
    second = StackOverflowCollector(registry).collect(make_ctx(started=NEXT_RUN), entry)
    second_requests = list(fake_web.requested)

    fake_web.requested.clear()
    third = StackOverflowCollector(registry).collect(make_ctx(started=NEXT_RUN + timedelta(days=3)), entry)

    python = _records(ctx)["python"]
    assert [r["record_id"] for r in python] == ["stackoverflow:python:2026-09-28:2026-10-19",
                                                "stackoverflow:python:2026-10-26:2026-10-26"]
    assert python[1]["weekly_questions"] == {"2026-10-26": 27} and python[1]["backfilled"] is False
    question_requests = [url for url in second_requests if url.startswith(QUESTIONS_URL)]
    assert question_requests == [_week_url("python", date(2026, 10, 26))]
    assert second.counters["records"] == 1 and second.complete
    # No week ended since the second run: no requests and no record
    assert fake_web.requested == [] and third.counters["records"] == 0 and third.complete


def test_backfill_spreads_over_runs_when_the_quota_ends(fake_web, make_ctx, tmp_path) -> None:
    registry = _registry(tmp_path, python="python")
    fake_web.add(tags_info_url(["python"]), _tags_info(("python", 2204253)))
    fake_web.add(QUESTIONS_URL, _total(5, quota_remaining=9000))
    first_week = date(2024, 10, 28)
    # The response of the 51st week leaves less than 10 requests of the daily quota
    fake_web.add(_week_url("python", first_week + timedelta(weeks=50)), _total(6, quota_remaining=9))
    ctx = make_ctx(backfill=True)
    entry = CollectorEntry(first_run=ctx.today)

    stopped = StackOverflowCollector(registry).collect(ctx, entry)
    continued = StackOverflowCollector(registry).collect(make_ctx(), entry)

    first, second = _records(ctx)["python"]
    assert first["record_id"] == "stackoverflow:python:2024-10-28:2025-10-13" and len(first["weekly_questions"]) == 51
    assert first["weekly_questions"]["2025-10-13"] == 6 and first["backfilled"] is True
    assert not stopped.complete and stopped.error is None
    assert stopped.counters["stopped"] == "quota" and stopped.counters["quota_remaining"] == 9
    # The next run, a normal one, continues after the last fetched week instead of starting a new history
    assert second["record_id"] == "stackoverflow:python:2025-10-20:2026-10-19" and len(second["weekly_questions"]) == 53
    assert continued.complete


def test_throttle_error_stops_gracefully_and_keeps_the_fetched_weeks(fake_web, make_ctx, tmp_path) -> None:
    registry = _registry(tmp_path, python="python", rust="rust")
    fake_web.add(tags_info_url(["python", "rust"]), _tags_info(("python", 2204253), ("rust", 43978)))
    fake_web.add(QUESTIONS_URL, _total(3))
    # Documented error response of the API for too many requests
    fake_web.add(_week_url("python", WEEKS[1]), {"error_id": 502, "error_name": "throttle_violation",
                                                 "error_message": "too many requests from this IP"}, status=400)
    ctx = make_ctx()

    result = StackOverflowCollector(registry).collect(ctx, CollectorEntry(first_run=ctx.today))

    records = _records(ctx)
    assert list(records) == ["python"]
    assert records["python"][0]["weekly_questions"] == {"2026-09-28": 3}
    assert ctx.seen(DATASET).get("python").data == {"last_week": "2026-09-28"}
    assert not any("tagged=rust" in url for url in fake_web.requested)
    assert not result.complete and result.error is None
    assert result.counters["stopped"] == "api_error" and result.counters["deferred"] == 2


def test_short_backoff_is_waited_for_and_a_long_one_stops(fake_web, make_ctx, tmp_path) -> None:
    registry = _registry(tmp_path, python="python")
    fake_web.add(tags_info_url(["python"]), _tags_info(("python", 2204253)))
    fake_web.add(QUESTIONS_URL, _total(3))
    fake_web.add(_week_url("python", WEEKS[0]), _total(3, backoff=10))
    ctx = make_ctx()
    waits: list[float] = []
    ctx.web.min_interval_s = 0.0
    ctx.web.sleep = waits.append

    waited = StackOverflowCollector(registry).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert waits == [10] and waited.complete
    fake_web.add(_week_url("python", date(2026, 10, 26)), _total(3, backoff=60))
    later = make_ctx(started=NEXT_RUN + timedelta(weeks=2))
    later.web.min_interval_s = 0.0
    later.web.sleep = waits.append

    stopped = StackOverflowCollector(registry).collect(later, CollectorEntry(first_run=ctx.today))

    assert waits == [10]
    assert _week_url("python", date(2026, 11, 2)) not in fake_web.requested
    assert _records(ctx)["python"][-1]["weekly_questions"] == {"2026-10-26": 3}
    assert not stopped.complete and stopped.error is None and stopped.counters["stopped"] == "backoff"


def test_failed_week_ends_the_tag_and_the_next_tag_continues(fake_web, make_ctx, tmp_path) -> None:
    registry = _registry(tmp_path, python="python", rust="rust")
    fake_web.add(tags_info_url(["python", "rust"]), _tags_info(("python", 2204253), ("rust", 43978)))
    fake_web.add(QUESTIONS_URL, _total(3))
    fake_web.fail(_week_url("python", WEEKS[2]))
    ctx = make_ctx()

    result = StackOverflowCollector(registry).collect(ctx, CollectorEntry(first_run=ctx.today))

    records = _records(ctx)
    # The weeks after the failed one are fetched in the next run, so that the series has no gap
    assert list(records["python"][0]["weekly_questions"]) == ["2026-09-28", "2026-10-05"]
    assert _week_url("python", WEEKS[3]) not in fake_web.requested
    assert len(records["rust"][0]["weekly_questions"]) == 4
    assert result.counters["failed"] == 1 and result.complete and result.error is None


def test_failed_tag_totals_leave_the_totals_empty(fake_web, make_ctx, tmp_path) -> None:
    registry = _registry(tmp_path, python="python")
    fake_web.add(tags_info_url(["python"]), "error", status=502)
    fake_web.add(QUESTIONS_URL, _total(3))
    ctx = make_ctx()

    result = StackOverflowCollector(registry).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert _records(ctx)["python"][0]["total_questions"] is None
    assert result.counters["missing_totals"] == 0 and result.complete


def test_key_is_sent_to_the_api_but_not_stored(fake_web, make_ctx, tmp_path) -> None:
    registry = _registry(tmp_path, csharp="c#")
    fake_web.add(tags_info_url(["c#"]), _tags_info(("c#", 1621624)))
    fake_web.add(QUESTIONS_URL, _total(3))
    ctx = make_ctx(stackexchange_key="test-key")

    StackOverflowCollector(registry).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert len(fake_web.requested) == 5
    assert all(url.endswith("&key=test-key") for url in fake_web.requested)
    [record] = read_dataset(ctx.settings.output_dir, DATASET)
    assert "test-key" not in json.dumps(record) and "tagged=c%23" in record["source_url"]


def test_tag_totals_are_requested_in_batches_of_100(fake_web, make_ctx, tmp_path) -> None:
    tags = [f"tag-{number}" for number in range(101)]
    registry = _registry(tmp_path, **{f"technology_{number}": tag for number, tag in enumerate(tags)})
    fake_web.add(tags_info_url(tags[:100]), _tags_info(*((tag, 1) for tag in tags[:100])))
    fake_web.add(tags_info_url(tags[100:]), _tags_info((tags[100], 1)))
    fake_web.add(QUESTIONS_URL, _total(3))
    ctx = make_ctx()

    result = StackOverflowCollector(registry).collect(ctx, CollectorEntry(first_run=ctx.today))

    info_requests = [url for url in fake_web.requested if "/tags/" in url]
    assert len(info_requests) == 2 and info_requests[0].startswith(tags_info_url(tags[:100]) + "?")
    assert result.counters["records"] == 101 and result.counters["missing_totals"] == 0

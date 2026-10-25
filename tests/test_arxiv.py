"""Tests of the arXiv collector."""
import re
from datetime import date, timedelta

import pytest

from collectors.arxiv import API_URL, CATEGORIES, DATASET, ArxivCollector, parse_total_results, query_url
from conftest import FIXTURES_DIR, RUN_STARTED, read_dataset
from state import CollectorEntry
from web import FetchError

FEED = (FIXTURES_DIR / "arxiv" / "query_cat_week.xml").read_text(encoding="utf-8")
ERROR_FEED = (FIXTURES_DIR / "arxiv" / "error_feed.xml").read_text(encoding="utf-8")
ATOM = "application/atom+xml; charset=utf-8"
TEST_CATEGORIES = {"cs.LG": "Machine Learning", "quant-ph": "Quantum Physics"}
# RUN_STARTED is Monday 2026-10-26. The week of 2026-10-19 ended one day before and is not settled;
# the eight weeks of a new category end with the week of 2026-10-12.
WEEKS = [date(2026, 10, 12) - timedelta(weeks=n) for n in range(7, -1, -1)]


def feed(total: int) -> str:
    """The fixture feed with another total; a feed with a total of 0 has no entry."""
    body = FEED.replace("<opensearch:totalResults>334<", f"<opensearch:totalResults>{total}<")
    return re.sub(r"\s*<entry>.*</entry>", "", body, flags=re.DOTALL) if total == 0 else body


def week_url(category: str, monday: date) -> str:
    """The query URL of a category for one week."""
    return query_url(category, monday, monday + timedelta(days=6))


def test_parse_total_results_reads_the_total_and_rejects_errors() -> None:
    assert parse_total_results(FEED.encode(), "url") == 334
    assert parse_total_results(feed(0).encode(), "url") == 0
    invalid = [ERROR_FEED, "<html>Service unavailable", FEED.replace(">334<", ">0<"), feed(0).replace(">0<", ">5<"),
               FEED.replace("<opensearch:totalResults>334</opensearch:totalResults>", "")]
    for body in invalid:
        with pytest.raises(FetchError):
            parse_total_results(body.encode(), "url")


def test_query_covers_monday_0000_to_sunday_2359_and_the_basket_is_complete() -> None:
    assert query_url("cs.LG", date(2026, 10, 12), date(2026, 10, 18)) == (
        "https://export.arxiv.org/api/query?search_query=cat%3Acs.LG+AND+submittedDate%3A"
        "%5B202610120000+TO+202610182359%5D&max_results=1")
    assert len(CATEGORIES) == 20 and {"cs.LG", "quant-ph", "stat.ML", "eess.SY"} <= set(CATEGORIES)


def test_new_categories_get_eight_settled_weeks(fake_web, make_ctx) -> None:
    for n, monday in enumerate(WEEKS):
        fake_web.add(week_url("cs.LG", monday), feed(1000 + n), content_type=ATOM)
        fake_web.add(week_url("quant-ph", monday), feed(200 + n), content_type=ATOM)
    ctx = make_ctx()

    result = ArxivCollector(TEST_CATEGORIES).collect(ctx, CollectorEntry(first_run=date(2026, 9, 1)))

    records = {r["category"]: r for r in read_dataset(ctx.settings.output_dir, DATASET)}
    machine_learning = records["cs.LG"]
    assert machine_learning["record_id"] == "arxiv:cs.LG:2026-08-24:2026-10-12"
    assert machine_learning["revision"] == 0 and machine_learning["category_name"] == "Machine Learning"
    assert machine_learning["weekly_submissions"] == {monday.isoformat(): 1000 + n for n, monday in enumerate(WEEKS)}
    assert machine_learning["source_url"] == query_url("cs.LG", date(2026, 8, 24), date(2026, 10, 18))
    assert machine_learning["license"] == "CC0-1.0 (arXiv metadata)" and machine_learning["backfilled"] is True
    assert records["quant-ph"]["weekly_submissions"]["2026-10-12"] == 207
    assert result.complete and result.error is None
    assert result.counters["requests"] == 16 and result.counters["weeks"] == 16


def test_next_run_fetches_only_the_newly_settled_week(fake_web, make_ctx) -> None:
    fake_web.add(API_URL, feed(50), content_type=ATOM)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    ArxivCollector(TEST_CATEGORIES).collect(ctx, entry)

    fake_web.requested.clear()
    fake_web.add(week_url("cs.LG", date(2026, 10, 19)), feed(1500), content_type=ATOM)
    result = ArxivCollector(TEST_CATEGORIES).collect(make_ctx(started=RUN_STARTED + timedelta(days=7)), entry)

    assert fake_web.requested == [week_url("cs.LG", date(2026, 10, 19)), week_url("quant-ph", date(2026, 10, 19))]
    machine_learning = sorted((r for r in read_dataset(ctx.settings.output_dir, DATASET) if r["category"] == "cs.LG"),
                              key=lambda r: r["record_id"])
    assert [r["record_id"] for r in machine_learning] == ["arxiv:cs.LG:2026-08-24:2026-10-12",
                                                          "arxiv:cs.LG:2026-10-19:2026-10-19"]
    assert machine_learning[1]["weekly_submissions"] == {"2026-10-19": 1500}
    assert result.complete


def test_deadline_stops_the_run_and_the_next_run_continues(fake_web, make_ctx) -> None:
    fake_web.add(API_URL, feed(50), content_type=ATOM)
    ctx = make_ctx()
    ctx.past_deadline = lambda: len(fake_web.requested) >= 3
    entry = CollectorEntry(first_run=ctx.today)

    result = ArxivCollector(TEST_CATEGORIES).collect(ctx, entry)

    assert not result.complete and result.error is None and result.counters["deferred"] == 1
    assert [r["record_id"] for r in read_dataset(ctx.settings.output_dir, DATASET)] == [
        "arxiv:cs.LG:2026-08-24:2026-09-07"]
    fake_web.requested.clear()
    ArxivCollector(TEST_CATEGORIES).collect(make_ctx(), entry)
    assert fake_web.requested[0] == week_url("cs.LG", date(2026, 9, 14))


def test_failed_week_stops_its_category_and_marks_the_run_incomplete(fake_web, make_ctx) -> None:
    fake_web.add(API_URL, feed(50), content_type=ATOM)
    fake_web.add(week_url("cs.LG", WEEKS[3]), ERROR_FEED, status=500, content_type=ATOM)
    # An error feed with HTTP 200 counts as a failure, too
    fake_web.add(week_url("quant-ph", WEEKS[5]), ERROR_FEED, content_type=ATOM)
    ctx = make_ctx()

    result = ArxivCollector(TEST_CATEGORIES).collect(ctx, CollectorEntry(first_run=ctx.today))

    records = {r["category"]: r["record_id"] for r in read_dataset(ctx.settings.output_dir, DATASET)}
    assert records == {"cs.LG": "arxiv:cs.LG:2026-08-24:2026-09-07",
                       "quant-ph": "arxiv:quant-ph:2026-08-24:2026-09-21"}
    assert not result.complete and result.error is None and result.counters["failed"] == 2


def test_unavailable_api_stops_after_three_failed_requests(fake_web, make_ctx) -> None:
    fake_web.fail(API_URL)
    categories = {"cs.AI": "Artificial Intelligence", "cs.LG": "Machine Learning", "cs.CL": "Computation and Language",
                  "cs.CV": "Computer Vision and Pattern Recognition"}
    ctx = make_ctx()

    result = ArxivCollector(categories).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.counters["failed"] == 3 and result.counters["deferred"] == 1
    assert result.error is not None and not result.complete
    assert read_dataset(ctx.settings.output_dir, DATASET) == []


def test_rate_limit_stops_the_run_without_error(fake_web, make_ctx) -> None:
    fake_web.add(API_URL, feed(50), content_type=ATOM)
    fake_web.add(week_url("cs.LG", WEEKS[2]), "Rate exceeded.", status=429, content_type="text/plain")
    ctx = make_ctx()

    result = ArxivCollector(TEST_CATEGORIES).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert [r["record_id"] for r in read_dataset(ctx.settings.output_dir, DATASET)] == [
        "arxiv:cs.LG:2026-08-24:2026-08-31"]
    assert not result.complete and result.error is None and result.counters["failed"] == 0


def test_backfill_gives_new_categories_104_weeks(fake_web, make_ctx) -> None:
    fake_web.add(API_URL, feed(50), content_type=ATOM)
    ctx = make_ctx(backfill=True)

    result = ArxivCollector({"cs.LG": "Machine Learning"}).collect(ctx, CollectorEntry(first_run=ctx.today))

    [record] = read_dataset(ctx.settings.output_dir, DATASET)
    assert len(record["weekly_submissions"]) == 104
    assert record["record_id"] == f"arxiv:cs.LG:{date(2026, 10, 12) - timedelta(weeks=103)}:2026-10-12"
    assert result.complete and result.counters["requests"] == 104

"""Tests of the Wikipedia pageviews collector."""
from datetime import date, datetime, timezone

from collectors.pageviews import WIKIDATA_API_URL, PageviewsCollector, article_url
from conftest import REGISTRY_FIXTURE, read_dataset
from state import CollectorEntry

ENTITIES = {"entities": {
    "Q28865": {"sitelinks": {"enwiki": {"title": "Python (programming language)"}}},
    "Q575650": {"sitelinks": {"enwiki": {"title": "Rust (programming language)"}}},
    "Q19399674": {"sitelinks": {"enwiki": {"title": "React (software)"}}},
    "Q47509047": {"sitelinks": {"enwiki": {"title": "PyTorch"}}},
    "Q115305900": {"sitelinks": {"enwiki": {"title": "Large language model"}}},
    "Q192490": {"sitelinks": {}},
}}


def _views(*days: tuple[str, int]) -> dict:
    return {"items": [{"timestamp": f"{day.replace('-', '')}00", "views": views} for day, views in days]}


def test_new_articles_get_the_history_and_articles_without_data_are_skipped(fake_web, make_ctx) -> None:
    fake_web.add(WIKIDATA_API_URL, ENTITIES)
    fake_web.add(article_url("Python (programming language)", date(2015, 7, 1), date(2026, 10, 25)),
                 _views(("2015-07-01", 5000), ("2026-10-25", 7000)))
    fake_web.add(article_url("Rust (programming language)", date(2015, 7, 1), date(2026, 10, 25)),
                 _views(("2026-10-24", 3000), ("2026-10-25", 3100)))
    ctx = make_ctx()

    result = PageviewsCollector(REGISTRY_FIXTURE).collect(ctx, CollectorEntry(first_run=ctx.today))

    records = {r["technology_id"]: r for r in read_dataset(ctx.settings.output_dir, "wikipedia_pageviews")}
    assert set(records) == {"python", "rust"}
    python = records["python"]
    assert python["record_id"] == "pageviews:python:2015-07-01:2026-10-25"
    assert python["daily_views"] == {"2015-07-01": 5000, "2026-10-25": 7000}
    assert python["backfilled"] is True and python["wikipedia_project"] == "en.wikipedia"
    assert records["rust"]["backfilled"] is False
    assert result.counters["without_article"] == 1
    assert result.complete


def test_next_run_fetches_only_the_days_after_the_last_stored_day(fake_web, make_ctx) -> None:
    fake_web.add(WIKIDATA_API_URL, ENTITIES)
    fake_web.add(article_url("Python (programming language)", date(2015, 7, 1), date(2026, 10, 25)),
                 _views(("2026-10-25", 7000)))
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    PageviewsCollector(REGISTRY_FIXTURE).collect(ctx, entry)

    later = datetime(2026, 11, 2, 2, 17, tzinfo=timezone.utc)
    fake_web.add(article_url("Python (programming language)", date(2026, 10, 26), date(2026, 11, 1)),
                 _views(("2026-10-26", 7100), ("2026-11-01", 6900)))
    PageviewsCollector(REGISTRY_FIXTURE).collect(make_ctx(started=later), entry)

    python = sorted((r for r in read_dataset(ctx.settings.output_dir, "wikipedia_pageviews")
                     if r["technology_id"] == "python"), key=lambda r: r["record_id"])
    assert [r["record_id"] for r in python] == ["pageviews:python:2026-10-25:2026-10-25",
                                                "pageviews:python:2026-10-26:2026-11-01"]
    assert all(r["revision"] == 0 for r in python)

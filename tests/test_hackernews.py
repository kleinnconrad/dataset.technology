"""Tests of the Hacker News collector."""
import json
from datetime import date, timedelta
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import pytest

import collectors.hackernews as hackernews
from collectors.hackernews import (
    DATASET_MENTIONS,
    DATASET_STORIES,
    SEARCH_BY_DATE_URL,
    SEARCH_URL,
    HackerNewsCollector,
    count_url,
    is_countable,
    mention_key,
    parse_count,
    search_query,
    stories_url,
    term_pattern,
    term_slug,
    unix_time,
)
from conftest import FIXTURES_DIR, RUN_STARTED, read_dataset
from state import CollectorEntry
from web import FetchError

REGISTRY = FIXTURES_DIR / "hackernews" / "technologies.toml"
MENTIONS_REGISTRY = FIXTURES_DIR / "hackernews" / "technologies_mentions.toml"
STORIES = json.loads((FIXTURES_DIR / "hackernews" / "search_by_date_stories.json").read_text(encoding="utf-8"))
COUNT = json.loads((FIXTURES_DIR / "hackernews" / "search_count_exhaustive.json").read_text(encoding="utf-8"))
INEXACT_COUNT = json.loads((FIXTURES_DIR / "hackernews" / "search_count_nonexhaustive.json").read_text(encoding="utf-8"))
# Stories window of the first run: the 7 days that end 48 hours before the start of the run
WINDOW_END = int(RUN_STARTED.timestamp()) - 48 * 3600
WINDOW_START = WINDOW_END - 7 * 86400
# RUN_STARTED is Monday 2026-10-26: a new term gets the four weeks up to the week of 2026-10-19
WEEKS = [date(2026, 9, 28), date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 19)]
# Counted terms of the mentions registry in the order of a first run; .net is not counted
COUNTABLE_TERMS = ["c programming", "dotnet", "fsharp", "large language model", "llm", "node.js", "rust"]


def count_body(hits: int, exhaustive: bool = True) -> dict:
    """A count response of the API with another number of hits."""
    return {**(COUNT if exhaustive else INEXACT_COUNT), "nbHits": hits}


def stories_body(hits: list, nb_hits: Optional[int] = None) -> dict:
    """A stories response of the API with other hits."""
    return {**STORIES, "hits": hits, "nbHits": len(hits) if nb_hits is None else nb_hits}


def week_url(term: str, kind: str, monday: date) -> str:
    """The count URL of a term for one week."""
    return count_url(term, kind, unix_time(monday), unix_time(monday + timedelta(weeks=1)))


def queried_terms(urls: list[str]) -> list[str]:
    """The search queries of the count requests, in the order of their first request."""
    queries = [parse_qs(urlsplit(url).query)["query"][0] for url in urls if url.startswith(SEARCH_URL + "?")]
    return list(dict.fromkeys(queries))


@pytest.fixture
def quiet_web(fake_web):
    """A fake web that answers every request without its own route with no stories and zero mentions."""
    fake_web.add(SEARCH_BY_DATE_URL, stories_body([]))
    fake_web.add(SEARCH_URL, count_body(0))
    return fake_web


@pytest.mark.parametrize(("term", "title", "found"), [
    ("c++", "C++ 26 released", True),
    ("c++", "C++26 is done: ISO C++ standards meeting Trip Report", True),
    ("c++", "Learning C the hard way", False),
    ("c programming", "Beej's Guide to C Programming", True),
    ("c programming", "Modern C++ programming", False),
    ("c#", "Use C# unions and closed hierarchies in ASP.NET Core", True),
    ("c#", "I built a Game Boy emulator in F#", False),
    ("f#", "I built a Game Boy emulator in F#", True),
    (".net", "Performance Improvements in .NET 11", True),
    (".net", "Building an in-memory background job queue in ASP.NET Core", False),
    (".net", "Dropping Cloudflare for Bunny.net", False),
    ("rust", "11 year old unsoundness bugs found in Rust borrow checker", True),
    ("rust", "Rusty tools to build trust", False),
    ("open source", "Penguin Mail – open-source Rust email client for Linux with AI", True),
    ("node.js", "Why we migrated from Python to Node.js", True),
    ("node.js", "Node js tips", False),
    ("large language model", "It would be neat to do a large language model trained only on Mark Twain", True),
    ("large language model", "Large language models are here", False),
    ("llm", "If you don't run your own LLM proxy, how do you know whats going on?", True),
    ("llm", "LLMs explained", False),
    ("http/3", "Quad9 Enables DNS over HTTP/3 and DNS over QUIC", True),
    ("wi-fi 6", "Wi-Fi 6 routers compared", True),
])
def test_term_pattern_finds_whole_words_and_terms_with_symbols(term: str, title: str, found: bool) -> None:
    assert bool(term_pattern(term).search(title)) is found


def test_terms_with_a_leading_dot_are_not_counted_and_other_terms_are_quoted_phrases(quiet_web, make_ctx) -> None:
    assert [is_countable(term) for term in ("c++", "c#", "f#", ".net")] == [True, True, True, False]
    assert all(is_countable(term) for term in ("node.js", "asp.net core", "http/3", "objective-c", "rust"))
    assert search_query("rust") == "rust"
    assert search_query("large language model") == '"large language model"'
    assert search_query("node.js") == '"node.js"'
    ctx = make_ctx()

    result = HackerNewsCollector(REGISTRY).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.counters["terms_skipped"] == 1 and result.counters["terms"] == 11
    expected = COUNTABLE_TERMS + ["c++", "c#", "f#"]
    assert sorted(queried_terms(quiet_web.requested)) == sorted(search_query(term) for term in expected)


def test_term_slugs_are_distinct_for_distinct_terms() -> None:
    terms = ["fine tuning", "fine-tuning", "x86_64", "x86 64", "http/3", "c++", "node.js", "large language model"]
    slugs = [term_slug(term) for term in terms]
    assert slugs == ["fine_tuning", "fine-tuning", "x86%5F64", "x86_64", "http%2F3", "c%2B%2B", "node.js",
                     "large_language_model"]
    assert len(set(slugs)) == len(terms)


def test_parse_count_reads_both_exhaustive_flags() -> None:
    assert parse_count(COUNT, "url") == (7, True)
    assert parse_count(INEXACT_COUNT, "url") == (149800, False)
    assert parse_count({"nbHits": 5, "exhaustiveNbHits": False}, "url") == (5, False)
    with pytest.raises(FetchError):
        parse_count({"hits": []}, "url")


def test_first_run_stores_settled_stories_with_type_domain_and_technologies(quiet_web, make_ctx) -> None:
    # Titles from live searches; ids, points and URLs are made up
    extra = [{"objectID": str(50200000 + n), "title": title, "url": f"https://example.org/{n}", "points": 150,
              "num_comments": 40, "created_at_i": WINDOW_START + 3600, "_tags": ["story"]}
             for n, title in enumerate(["C++26 is done: ISO C++ standards meeting Trip Report",
                                        "Use C# unions and closed hierarchies in ASP.NET Core",
                                        "Performance Improvements in .NET 11", "Beej's Guide to C Programming"])]
    quiet_web.add(stories_url(WINDOW_START, WINDOW_END), stories_body(STORIES["hits"] + extra, nb_hits=327))
    ctx = make_ctx()

    result = HackerNewsCollector(REGISTRY).collect(ctx, CollectorEntry(first_run=date(2026, 10, 15)))

    records = {r["story_id"]: r for r in read_dataset(ctx.settings.output_dir, DATASET_STORIES)}
    assert len(records) == 14 and result.counters["stories"] == 14
    show = records["50145715"]
    assert show["record_id"] == "hn-story:50145715" and show["revision"] == 0
    assert show["source_url"] == "https://news.ycombinator.com/item?id=50145715"
    assert show["title"] == "Show HN: WhatsApp MCP running free on Cloudflare"
    assert show["story_type"] == "show_hn" and show["domain"] == "github.com"
    assert show["points"] == 157 and show["comments"] == 85
    assert show["created_at"] == "2026-10-18T21:36:10Z" and show["backfilled"] is False
    assert show["technology_ids"] == [] and "author" not in show
    ask = records["50089577"]
    assert ask["story_type"] == "ask_hn" and ask["url"] is None and ask["domain"] is None
    assert records["50146624"]["domain"] == "csis.org" and records["50146624"]["story_type"] == "story"
    assert records["50145507"]["technology_ids"] == ["large_language_model"]
    assert records["49963385"]["technology_ids"] == ["rust"] and records["49963385"]["backfilled"] is True
    assert [records[str(50200000 + n)]["technology_ids"] for n in range(4)] == [["cpp"], ["csharp"], ["dotnet"], ["c"]]
    assert result.cursor == {"stories_until": WINDOW_END} and result.complete and result.error is None


def test_window_with_more_than_1000_hits_is_split_in_halves(quiet_web, make_ctx) -> None:
    middle = (WINDOW_START + WINDOW_END) // 2
    quiet_web.add_fixture(stories_url(WINDOW_START, WINDOW_END), "hackernews/search_by_date_capped.json")
    quiet_web.add(stories_url(WINDOW_START, middle), stories_body(STORIES["hits"][:3]))
    quiet_web.add(stories_url(middle, WINDOW_END), stories_body(STORIES["hits"][3:5]))
    ctx = make_ctx()

    result = HackerNewsCollector(REGISTRY).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert [url for url in quiet_web.requested if url.startswith(SEARCH_BY_DATE_URL)] == [
        stories_url(WINDOW_START, WINDOW_END), stories_url(WINDOW_START, middle), stories_url(middle, WINDOW_END)]
    stored = {r["story_id"] for r in read_dataset(ctx.settings.output_dir, DATASET_STORIES)}
    assert stored == {hit["objectID"] for hit in STORIES["hits"][:5]}
    assert result.counters["story_windows_split"] == 1 and result.cursor == {"stories_until": WINDOW_END}


def test_failed_window_stays_after_the_cursor(quiet_web, make_ctx) -> None:
    middle = (WINDOW_START + WINDOW_END) // 2
    quiet_web.add_fixture(stories_url(WINDOW_START, WINDOW_END), "hackernews/search_by_date_capped.json")
    quiet_web.add(stories_url(WINDOW_START, middle), stories_body(STORIES["hits"][:2]))
    quiet_web.add(stories_url(middle, WINDOW_END), "Service Unavailable", status=503, content_type="text/plain")
    ctx = make_ctx()

    result = HackerNewsCollector(REGISTRY).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.cursor == {"stories_until": middle} and result.counters["failed"] == 1
    assert len(read_dataset(ctx.settings.output_dir, DATASET_STORIES)) == 2
    assert result.complete and result.error is None


def test_next_run_starts_at_the_cursor_and_skips_stored_stories(quiet_web, make_ctx) -> None:
    quiet_web.add(stories_url(WINDOW_START, WINDOW_END), stories_body(STORIES["hits"][:2]))
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    first = HackerNewsCollector(REGISTRY).collect(ctx, entry)

    later = RUN_STARTED + timedelta(days=3)
    later_end = int(later.timestamp()) - 48 * 3600
    # The second window repeats a story of the first one
    quiet_web.add(stories_url(WINDOW_END, later_end), stories_body(STORIES["hits"][1:4]))
    second = HackerNewsCollector(REGISTRY).collect(make_ctx(started=later),
                                                   entry.model_copy(update={"cursor": first.cursor}))

    assert first.cursor == {"stories_until": WINDOW_END} and second.cursor == {"stories_until": later_end}
    assert second.counters["stories"] == 2 and second.counters["stories_seen"] == 1
    stored = sorted(r["story_id"] for r in read_dataset(ctx.settings.output_dir, DATASET_STORIES))
    assert stored == sorted(hit["objectID"] for hit in STORIES["hits"][:4])
    # The weeks of the first run are complete; the run on Thursday counts no mentions
    assert second.counters["count_requests"] == 0


def test_new_terms_get_four_weeks_and_inexact_counts_are_split(quiet_web, make_ctx) -> None:
    quiet_web.add(week_url("rust", "stories", WEEKS[0]), count_body(12))
    last = WEEKS[-1]
    quiet_web.add(week_url("rust", "comments", last), count_body(149800, exhaustive=False))
    # Six days are exhaustive; Wednesday is not and is split into 6-hour windows, one of which stays inexact
    for day in range(7):
        day_start = unix_time(last + timedelta(days=day))
        if day != 2:
            quiet_web.add(count_url("rust", "comments", day_start, day_start + 86400), count_body(100))
            continue
        quiet_web.add(count_url("rust", "comments", day_start, day_start + 86400), count_body(50000, exhaustive=False))
        for part in range(4):
            part_start = day_start + part * 21600
            quiet_web.add(count_url("rust", "comments", part_start, part_start + 21600),
                          count_body(40 + part, exhaustive=part != 3))
    ctx = make_ctx()

    result = HackerNewsCollector(MENTIONS_REGISTRY).collect(ctx, CollectorEntry(first_run=ctx.today))

    records = {r["term"]: r for r in read_dataset(ctx.settings.output_dir, DATASET_MENTIONS)}
    assert sorted(records) == sorted(COUNTABLE_TERMS)
    rust = records["rust"]
    assert rust["record_id"] == "hn-mentions:rust:rust:2026-09-28:2026-10-19" and rust["revision"] == 0
    assert rust["technology_id"] == "rust" and rust["backfilled"] is True
    assert rust["weekly_stories"] == {"2026-09-28": 12, "2026-10-05": 0, "2026-10-12": 0, "2026-10-19": 0}
    assert rust["weekly_comments"]["2026-10-19"] == 6 * 100 + 40 + 41 + 42 + 43
    assert rust["approximate_weeks"] == ["2026-10-19"]
    llm = records["large language model"]
    assert llm["record_id"] == "hn-mentions:large_language_model:large_language_model:2026-09-28:2026-10-19"
    assert llm["approximate_weeks"] == []
    assert records["node.js"]["record_id"] == "hn-mentions:nodejs:node.js:2026-09-28:2026-10-19"
    # 7 terms x 4 weeks x 2 counts, plus 7 days and 4 windows of the split
    assert result.counters["count_requests"] == 7 * 4 * 2 + 7 + 4
    assert result.counters["approximate_weeks"] == 1 and result.counters["terms_deferred"] == 0


def test_next_run_counts_only_the_weeks_after_the_last_stored_week(quiet_web, make_ctx) -> None:
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    HackerNewsCollector(MENTIONS_REGISTRY).collect(ctx, entry)

    quiet_web.add(week_url("rust", "stories", date(2026, 10, 26)), count_body(9))
    result = HackerNewsCollector(MENTIONS_REGISTRY).collect(make_ctx(started=RUN_STARTED + timedelta(days=7)), entry)

    rust = sorted((r for r in read_dataset(ctx.settings.output_dir, DATASET_MENTIONS) if r["term"] == "rust"),
                  key=lambda r: r["record_id"])
    assert [r["record_id"] for r in rust] == ["hn-mentions:rust:rust:2026-09-28:2026-10-19",
                                              "hn-mentions:rust:rust:2026-10-26:2026-10-26"]
    assert rust[1]["weekly_stories"] == {"2026-10-26": 9}
    assert result.counters["count_requests"] == 7 * 2


def test_budget_visits_new_terms_first_then_the_longest_waiting_terms(quiet_web, make_ctx, monkeypatch) -> None:
    ctx = make_ctx()
    seen = ctx.seen(DATASET_MENTIONS)
    # node.js waits for three weeks, dotnet and rust for one; the other terms except llm are up to date.
    # llm was never counted and gets the four weeks of a new term.
    last_weeks = {("nodejs", "node.js"): date(2026, 9, 28), ("dotnet", "dotnet"): date(2026, 10, 12),
                  ("rust", "rust"): date(2026, 10, 12), ("c", "c programming"): WEEKS[-1],
                  ("fsharp", "fsharp"): WEEKS[-1], ("large_language_model", "large language model"): WEEKS[-1]}
    for (technology_id, term), last in last_weeks.items():
        seen.mark(mention_key(technology_id, term), "hash", ctx.today, data={"last_week": last.isoformat()})
    monkeypatch.setattr(hackernews, "MAX_MENTION_REQUESTS_PER_RUN", 16)

    result = HackerNewsCollector(MENTIONS_REGISTRY).collect(ctx, CollectorEntry(first_run=ctx.today))

    # llm: 4 weeks (8 requests), node.js: 3 weeks (6), dotnet: 1 week (2); rust would exceed the budget
    assert queried_terms(quiet_web.requested) == ["llm", '"node.js"', "dotnet"]
    assert result.counters["count_requests"] == 16
    assert result.counters["terms_pending"] == 4 and result.counters["terms_deferred"] == 1
    assert result.complete and result.error is None
    records = {r["term"]: r for r in read_dataset(ctx.settings.output_dir, DATASET_MENTIONS)}
    assert records["node.js"]["record_id"] == "hn-mentions:nodejs:node.js:2026-10-05:2026-10-19"


def test_backfill_run_has_no_budget_and_new_terms_get_52_weeks(quiet_web, make_ctx, monkeypatch) -> None:
    monkeypatch.setattr(hackernews, "MAX_MENTION_REQUESTS_PER_RUN", 10)
    ctx = make_ctx(backfill=True)

    result = HackerNewsCollector(MENTIONS_REGISTRY).collect(ctx, CollectorEntry(first_run=ctx.today))

    records = read_dataset(ctx.settings.output_dir, DATASET_MENTIONS)
    assert len(records) == 7 and all(len(r["weekly_stories"]) == 52 for r in records)
    assert min(records[0]["weekly_comments"]) == (WEEKS[-1] - timedelta(weeks=51)).isoformat()
    assert result.counters["count_requests"] == 7 * 52 * 2 and result.counters["terms_deferred"] == 0
    # The first stories window of a backfill run covers 365 days
    assert stories_url(WINDOW_END - 365 * 86400, WINDOW_END) in quiet_web.requested


def test_deadline_stops_the_run_and_keeps_the_weeks_already_counted(quiet_web, make_ctx) -> None:
    ctx = make_ctx()
    # The deadline passes after the stories request and two weeks of the first term
    ctx.past_deadline = lambda: len(quiet_web.requested) >= 5

    result = HackerNewsCollector(MENTIONS_REGISTRY).collect(ctx, CollectorEntry(first_run=ctx.today))

    records = read_dataset(ctx.settings.output_dir, DATASET_MENTIONS)
    assert [r["record_id"] for r in records] == ["hn-mentions:c:c_programming:2026-09-28:2026-10-05"]
    assert not result.complete and result.error is None and result.counters["terms_deferred"] == 6


def test_rate_limit_stops_the_run_without_error(quiet_web, make_ctx) -> None:
    quiet_web.add(week_url("c programming", "comments", WEEKS[1]), {"message": "rate limited"}, status=429)
    ctx = make_ctx()

    result = HackerNewsCollector(REGISTRY).collect(ctx, CollectorEntry(first_run=ctx.today))

    records = read_dataset(ctx.settings.output_dir, DATASET_MENTIONS)
    assert [r["record_id"] for r in records] == ["hn-mentions:c:c_programming:2026-09-28:2026-09-28"]
    assert not result.complete and result.error is None and result.counters["failed"] == 0


def test_failed_term_is_counted_and_a_refusal_stops_the_run_with_an_error(quiet_web, make_ctx) -> None:
    quiet_web.add(week_url("dotnet", "stories", WEEKS[0]), "Internal Server Error", status=500,
                  content_type="text/plain")
    quiet_web.add(week_url("rust", "stories", WEEKS[0]), "Forbidden", status=403, content_type="text/plain")
    ctx = make_ctx()

    result = HackerNewsCollector(MENTIONS_REGISTRY).collect(ctx, CollectorEntry(first_run=ctx.today))

    terms = {r["term"] for r in read_dataset(ctx.settings.output_dir, DATASET_MENTIONS)}
    # dotnet failed in its first week; the terms after it were counted until the request for rust was refused
    assert terms == {"c programming", "fsharp", "large language model", "llm", "node.js"}
    assert result.counters["failed"] == 1
    assert result.error is not None and "refused" in result.error

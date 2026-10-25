"""Tests of the run orchestration with fake collectors."""
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from collectors.base import Collector, CollectorResult, CollectorSkipped, RunContext
from conftest import RUN_STARTED, read_dataset
from pipeline import COLLECTOR_ORDER, HOST_INTERVALS, build_collectors, run_pipeline
from schema import DATASET_MODELS, PageviewRecord
from settings import Settings
from state import CollectorEntry, CollectorStates, SeenStore


class WritingCollector(Collector):
    """Writes one pageview record."""
    name = "writer"
    datasets = ("wikipedia_pageviews",)

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        ctx.emit("wikipedia_pageviews", [PageviewRecord(
            **ctx.envelope("wikipedia_pageviews", "pageviews:python:2026-10-25:2026-10-25", revision=0,
                           source_id="wikimedia_pageviews", source_url=None, license="CC0-1.0", backfilled=False),
            technology_id="python", wikipedia_project="en.wikipedia", wikipedia_title="Python (programming language)",
            wikidata_qid="Q28865", daily_views={date(2026, 10, 25): 100},
        )])
        return CollectorResult(cursor={"position": 1}, counters={"written": 1})


class FailingCollector(Collector):
    """Raises an unexpected error."""
    name = "failing"
    datasets = ()

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        raise RuntimeError("unexpected")


class SkippedCollector(Collector):
    """Misses a prerequisite."""
    name = "skipped"
    datasets = ()

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        raise CollectorSkipped("configuration missing")


class WeeklyCollector(WritingCollector):
    """Writes one record and runs at most once a week."""
    name = "weekly"
    min_days_between_runs = 6


class PartialCollector(Collector):
    """Stops at the run deadline in every run."""
    name = "partial"
    datasets = ()
    min_days_between_runs = 6

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        return CollectorResult(complete=False)


class SlowCollector(Collector):
    """Advances a mutable clock by an hour."""
    name = "slow"
    datasets = ()

    def __init__(self, now: list[datetime]) -> None:
        self._now = now

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        self._now[0] += timedelta(hours=1)
        return CollectorResult()


def settings_for(tmp_path, **overrides) -> Settings:
    return replace(Settings(), output_dir=tmp_path / "data", **overrides)


def test_failing_collector_does_not_stop_the_others(tmp_path, fake_web) -> None:
    settings = settings_for(tmp_path)

    summary = run_pipeline(settings, [FailingCollector(), SkippedCollector(), WritingCollector()],
                           web=fake_web.client(), clock=lambda: RUN_STARTED)

    assert summary.collectors["failing"].status == "failed"
    assert summary.collectors["skipped"].status == "skipped"
    assert summary.collectors["writer"].status == "ok"
    assert summary.collectors["writer"].records == {"wikipedia_pageviews": 1}
    assert sorted(summary.failed) == ["failing", "skipped"]
    assert len(read_dataset(settings.output_dir, "wikipedia_pageviews")) == 1

    states = CollectorStates.load(settings.state_dir)
    assert states.entry("failing", RUN_STARTED.date()).consecutive_failures == 1
    assert states.entry("writer", RUN_STARTED.date()).cursor == {"position": 1}
    log = [json.loads(line) for line in (settings.state_dir / "run_log.jsonl").read_text().splitlines()]
    assert log[-1]["collectors"]["failing"]["error"] == "RuntimeError: unexpected"


class MarkThenFailCollector(Collector):
    """Marks an item in a seen store and fails before it emits the record."""
    name = "mark_then_fail"
    datasets = ()

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        ctx.seen("release_cycles").mark("python:3.14", "hash", ctx.today)
        raise RuntimeError("failed before emitting")


def test_marks_of_a_failed_collector_are_not_saved(tmp_path, fake_web) -> None:
    settings = settings_for(tmp_path)

    summary = run_pipeline(settings, [MarkThenFailCollector()], web=fake_web.client(), clock=lambda: RUN_STARTED)

    assert summary.collectors["mark_then_fail"].status == "failed"
    assert "python:3.14" not in SeenStore.load(settings.state_dir, "release_cycles")


def test_collector_selection_and_dry_run(tmp_path, fake_web) -> None:
    settings = settings_for(tmp_path, collectors=("writer",))

    summary = run_pipeline(settings, [FailingCollector(), WritingCollector()], dry_run=True,
                           web=fake_web.client(), clock=lambda: datetime(2026, 10, 26, tzinfo=timezone.utc))

    assert list(summary.collectors) == ["writer"]
    assert summary.mode == "dry-run"
    assert not settings.output_dir.exists()


def test_weekly_collector_runs_six_days_after_its_last_complete_run(tmp_path, fake_web) -> None:
    settings = settings_for(tmp_path)

    def run(days: int, **options) -> str:
        summary = run_pipeline(options.pop("settings", settings), [WeeklyCollector()], web=fake_web.client(),
                               clock=lambda: RUN_STARTED + timedelta(days=days), **options)
        return summary.collectors["weekly"].status

    assert run(0) == "ok"
    assert run(3) == "not_due"
    assert run(7) == "ok"
    # Explicitly selected collectors and backfill runs ignore the interval
    assert run(8, settings=replace(settings, collectors=("weekly",))) == "ok"
    assert run(9, backfill=True) == "ok"
    log = [json.loads(line) for line in (settings.state_dir / "run_log.jsonl").read_text().splitlines()]
    assert log[1]["collectors"]["weekly"] == {"status": "not_due"}
    assert log[-1]["mode"] == "backfill"


def test_collector_stopped_early_is_partial_and_stays_due(tmp_path, fake_web) -> None:
    settings = settings_for(tmp_path)

    first = run_pipeline(settings, [PartialCollector()], web=fake_web.client(), clock=lambda: RUN_STARTED)
    second = run_pipeline(settings, [PartialCollector()], web=fake_web.client(),
                          clock=lambda: RUN_STARTED + timedelta(days=3))

    assert first.collectors["partial"].status == "partial" and first.failed == []
    assert second.collectors["partial"].status == "partial"
    entry = CollectorStates.load(settings.state_dir).entry("partial", RUN_STARTED.date())
    assert entry.last_success is not None and entry.last_complete is None


def test_collectors_after_the_run_deadline_are_skipped(tmp_path, fake_web) -> None:
    settings = settings_for(tmp_path)
    now = [RUN_STARTED]

    summary = run_pipeline(settings, [SlowCollector(now), WritingCollector()], web=fake_web.client(),
                           clock=lambda: now[0])

    assert summary.collectors["slow"].status == "ok"
    assert summary.collectors["writer"].status == "skipped"
    assert "run deadline" in (summary.collectors["writer"].error or "")
    assert summary.failed == ["writer"]
    assert not (settings.output_dir / "wikipedia_pageviews").exists()


def test_build_collectors_follows_the_order() -> None:
    assert [collector.name for collector in build_collectors()] == list(COLLECTOR_ORDER)


def test_every_dataset_is_written_by_exactly_one_collector() -> None:
    writers = [dataset for collector in build_collectors() for dataset in collector.datasets]
    assert sorted(writers) == sorted(DATASET_MODELS)


def test_host_intervals_respect_the_documented_limits() -> None:
    # arXiv: one request every three seconds; crates.io: one request per second
    assert HOST_INTERVALS["export.arxiv.org"] >= 3.0
    assert HOST_INTERVALS.get("crates.io", 1.0) >= 1.0

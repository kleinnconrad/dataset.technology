"""Tests of the partitioned storage and the state files."""
import json
from datetime import date, datetime, timezone

from schema import PageviewRecord
from state import CollectorStates, SeenStore, content_hash, is_backfilled, seen_path
from storage import dumps_records, iter_records, partition_file, write_records

DAY = date(2026, 10, 26)


def _record(record_id: str, revision: int = 0, views: int = 10) -> PageviewRecord:
    return PageviewRecord(
        record_id=record_id, revision=revision, dataset="wikipedia_pageviews",
        observed_at=datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc), source_id="wikimedia_pageviews",
        source_url=None, license="CC0-1.0", backfilled=False, technology_id="python",
        wikipedia_project="en.wikipedia", wikipedia_title="Python (programming language)", wikidata_qid="Q28865",
        daily_views={date(2026, 10, 25): views},
    )


def test_partition_file_uses_hive_style_folders(tmp_path) -> None:
    path = partition_file(tmp_path, "wikipedia_pageviews", DAY)
    assert path == tmp_path / "wikipedia_pageviews" / "year=2026" / "month=10" / "wikipedia_pageviews_2026-10-26.json"


def test_dumps_records_writes_one_record_per_line() -> None:
    text = dumps_records([{"a": 1}, {"b": "ä"}])
    assert text == '[\n{"a":1},\n{"b":"ä"}\n]\n'
    assert json.loads(text) == [{"a": 1}, {"b": "ä"}]


def test_write_records_merges_by_record_id_and_revision(tmp_path) -> None:
    write_records(tmp_path, "wikipedia_pageviews", DAY, [_record("a"), _record("b")])
    write_records(tmp_path, "wikipedia_pageviews", DAY, [_record("a", views=11), _record("a", revision=1, views=12)])

    records = list(iter_records(tmp_path, "wikipedia_pageviews"))
    keys = sorted((r["record_id"], r["revision"], r["daily_views"]["2026-10-25"]) for r in records)
    assert keys == [("a", 0, 11), ("a", 1, 12), ("b", 0, 10)]


def test_write_records_without_records_writes_nothing(tmp_path) -> None:
    assert write_records(tmp_path, "wikipedia_pageviews", DAY, []) is None
    assert not (tmp_path / "wikipedia_pageviews").exists()


def test_seen_store_classifies_and_counts_revisions(tmp_path) -> None:
    store = SeenStore.load(tmp_path, "release_cycles")
    assert store.classify("1", "a") == "new"
    assert store.mark("1", "a", DAY) == 0
    assert store.classify("1", "a") == "unchanged"
    assert store.mark("1", "a", DAY) == 0
    assert store.classify("1", "b") == "changed"
    assert store.mark("1", "b", DAY, data={"status": "x"}) == 1
    store.save()

    reloaded = SeenStore.load(tmp_path, "release_cycles")
    entry = reloaded.get("1")
    assert entry is not None and entry.revision == 1 and entry.data == {"status": "x"}


def test_seen_store_is_written_only_after_changes(tmp_path) -> None:
    store = SeenStore.load(tmp_path, "release_cycles")
    store.save()
    assert not seen_path(tmp_path, "release_cycles").exists()

    store.mark("1", "a", DAY)
    store.save()
    path = seen_path(tmp_path, "release_cycles")
    written = path.stat().st_mtime_ns
    SeenStore.load(tmp_path, "release_cycles").save()
    assert path.stat().st_mtime_ns == written


def test_sharded_seen_stores_use_one_file_per_shard(tmp_path) -> None:
    store = SeenStore.load(tmp_path, "cves", shard="2026")
    store.mark("CVE-2026-0001", "h", DAY)
    store.save()

    assert seen_path(tmp_path, "cves", "2026") == tmp_path / "seen" / "cves" / "2026.json"
    assert "CVE-2026-0001" in SeenStore.load(tmp_path, "cves", shard="2026")
    assert len(SeenStore.load(tmp_path, "cves", shard="2025")) == 0


def test_seen_store_prunes_old_entries(tmp_path) -> None:
    store = SeenStore.load(tmp_path, "hackernews_stories")
    store.mark("old", "h", date(2026, 8, 1))
    store.mark("new", "h", date(2026, 10, 19))

    assert store.prune(DAY, retention_days=30) == 1
    assert "old" not in store and "new" in store


def test_collector_states_track_failures_and_cursor(tmp_path) -> None:
    states = CollectorStates.load(tmp_path)
    started = datetime(2026, 10, 26, 2, 17, tzinfo=timezone.utc)
    assert states.entry("cves", DAY).first_run == DAY
    states.record_failure("cves", started, "boom")
    states.record_failure("cves", started, "boom")
    assert states.entry("cves", DAY).consecutive_failures == 2
    states.record_success("cves", started, {"last_modified": "x"})
    states.save()

    entry = CollectorStates.load(tmp_path).entry("cves", date(2027, 1, 1))
    assert entry.consecutive_failures == 0
    assert entry.cursor == {"last_modified": "x"}
    assert entry.first_run == DAY


def test_is_backfilled_uses_a_grace_period() -> None:
    assert is_backfilled(date(2026, 10, 1), first_run=DAY)
    assert not is_backfilled(date(2026, 10, 20), first_run=DAY)
    assert not is_backfilled(None, first_run=DAY)


def test_content_hash_ignores_key_order() -> None:
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})

"""Tests of the collector that writes the technology registry."""
from collectors.technologies import TechnologiesCollector
from conftest import REGISTRY_FIXTURE, read_dataset
from state import CollectorEntry


def test_first_run_writes_every_entry_and_later_runs_only_changes(make_ctx, tmp_path) -> None:
    registry = tmp_path / "technologies.toml"
    registry.write_text(REGISTRY_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)

    first = TechnologiesCollector(registry).collect(ctx, entry)
    unchanged = TechnologiesCollector(registry).collect(make_ctx(), entry)
    registry.write_text(registry.read_text(encoding="utf-8").replace(
        'stackoverflow_tags = ["rust"]', 'stackoverflow_tags = ["rust", "rust-cargo"]'), encoding="utf-8")
    changed = TechnologiesCollector(registry).collect(make_ctx(), entry)

    assert first.counters["new"] == 8 and first.counters["active"] == 7
    assert unchanged.counters["new"] == 0 and unchanged.counters["changed"] == 0
    assert changed.counters["changed"] == 1
    records = read_dataset(ctx.settings.output_dir, "technologies")
    rust = sorted((r for r in records if r["technology_id"] == "rust"), key=lambda r: r["revision"])
    assert [r["revision"] for r in rust] == [0, 1]
    assert rust[1]["stackoverflow_tags"] == ["rust", "rust-cargo"]
    gopher = next(r for r in records if r["technology_id"] == "gopher")
    assert gopher["removed"] == "2026-10-15" and gopher["domain"] == "networking"
    polars = next(r for r in records if r["technology_id"] == "polars")
    assert polars["packages"] == ["pypi:polars", "crates:polars"] and polars["wikidata_qid"] is None

"""Tests of the README overview generator."""
from datetime import datetime, timezone

import pytest

from conftest import REGISTRY_FIXTURE
from generate_dataset_overview import END_MARKER, START_MARKER, build_overview, update_readme
from state import append_run_log
from storage import dumps_records, partition_file

NOW = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)


def write(data_dir, dataset: str, records: list[dict]) -> None:
    path = partition_file(data_dir, dataset, NOW.date())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_records(records), encoding="utf-8")


def test_build_overview_counts_records_runs_and_registry(tmp_path) -> None:
    data_dir = tmp_path / "data"
    write(data_dir, "technologies", [
        {"record_id": "technology:python", "observed_at": "2026-10-26T02:20:00Z", "technology_id": "python",
         "stackoverflow_tags": ["python"]},
        {"record_id": "technology:python", "revision": 1, "observed_at": "2026-10-26T02:20:00Z",
         "technology_id": "python", "stackoverflow_tags": []},
    ])
    append_run_log(data_dir / "state", {"started": "2026-10-26T02:17:00+00:00", "mode": "normal",
                                        "collectors": {"technologies": {"status": "ok",
                                                                        "records": {"technologies": 2}}}})

    overview = build_overview(data_dir, REGISTRY_FIXTURE, now=NOW)

    assert "| `technologies` | 2 | 1 | 1 | 2026-10-26 to 2026-10-26 |" in overview
    assert "`technologies`: ok (technologies 2)" in overview
    assert "- **Active technologies:** 7 in 3 domains" in overview
    assert "| Wikipedia article | 85.7% (6) |" in overview
    assert ("| `stackoverflow_tags` | Stack Overflow tags counted for the technology. | 50.0% (1) | 1 |"
            in overview)
    assert "<summary><code>wikipedia_pageviews</code>" not in overview


def test_update_readme_replaces_only_the_marked_section(tmp_path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(f"# Title\n{START_MARKER}\nold\n{END_MARKER}\nrest\n", encoding="utf-8")

    assert update_readme(readme, "new") is True
    assert readme.read_text(encoding="utf-8") == f"# Title\n{START_MARKER}\nnew\n{END_MARKER}\nrest\n"
    assert update_readme(readme, "new") is False


def test_update_readme_requires_markers(tmp_path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# Title\n", encoding="utf-8")
    with pytest.raises(ValueError):
        update_readme(readme, "x")


def test_overview_without_data(tmp_path) -> None:
    overview = build_overview(tmp_path / "data", REGISTRY_FIXTURE, now=NOW)
    assert "- No run yet." in overview
    assert "| `wikipedia_pageviews` | 0 | 0 | 0 | none |" in overview

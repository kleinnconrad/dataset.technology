"""
Writes the dataset overview into the README.

The overview replaces the text between the markers DATASET_OVERVIEW_START and
DATASET_OVERVIEW_END: record counts per dataset, the outcome of the latest
run, the coverage of the technology registry per domain and source, and the
fields of every dataset with their descriptions from ``schema.py`` and their
fill rates. Long tables are placed in collapsible sections.

Usage:
    uv run python src/generate_dataset_overview.py [--data-dir data] [--readme README.md]
"""
import argparse
import json
import logging
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from registry import CONFIG_PATH, RegistryError, load_registry
from schema import DATASET_MODELS
from state import RUN_LOG_FILE
from storage import iter_dataset_files, load_records

logger = logging.getLogger(__name__)

START_MARKER = "<!-- DATASET_OVERVIEW_START -->"
END_MARKER = "<!-- DATASET_OVERVIEW_END -->"


@dataclass
class DatasetData:
    """The stored records of one dataset.

    Attributes:
        files (int): Number of partition files.
        records (list[dict]): All records in chronological order of the files.
    """
    files: int
    records: list[dict[str, Any]]


def load_datasets(data_dir: Path) -> dict[str, DatasetData]:
    """Reads all records of all datasets.

    Args:
        data_dir: Root folder of the datasets.

    Returns:
        dict[str, DatasetData]: Records per dataset, in the order of ``DATASET_MODELS``.
    """
    datasets = {}
    for dataset in DATASET_MODELS:
        files = list(iter_dataset_files(data_dir, dataset))
        records: list[dict[str, Any]] = []
        for path in files:
            records.extend(load_records(path))
        datasets[dataset] = DatasetData(files=len(files), records=records)
    return datasets


def _description(dataset: str) -> str:
    """Returns the first line of the docstring of a dataset's record model."""
    doc = DATASET_MODELS[dataset].__doc__ or ""
    return doc.strip().splitlines()[0] if doc.strip() else ""


def _cell(text: Optional[str]) -> str:
    """Escapes text for a Markdown table cell."""
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _share(part: int, total: int) -> str:
    """Formats a share with its count, e.g. ``95.0% (190)``."""
    return f"{part / total:.1%} ({part:,})" if total else "0.0% (0)"


def dataset_lines(datasets: dict[str, DatasetData]) -> list[str]:
    """Builds the table of the datasets.

    Args:
        datasets: Records per dataset.

    Returns:
        list[str]: Markdown lines.
    """
    lines = ["| Dataset | Records | Items | Files | Observed | Content |", "|---|---|---|---|---|---|"]
    for dataset, data in datasets.items():
        observed = sorted(str(r.get("observed_at"))[:10] for r in data.records if r.get("observed_at"))
        items = len({record.get("record_id") for record in data.records})
        span = f"{observed[0]} to {observed[-1]}" if observed else "none"
        lines.append(f"| `{dataset}` | {len(data.records):,} | {items:,} | {data.files} | {span} | "
                     f"{_cell(_description(dataset))} |")
    return lines


def _is_filled(value: Any) -> bool:
    """Tells whether a field has a value: not null and not an empty text, list or object."""
    return value is not None and value != "" and value != [] and value != {}


def _distinct(values: list[Any]) -> int:
    """Counts the distinct values of a field: list elements and object keys are counted individually."""
    distinct: set[str] = set()
    for value in values:
        if isinstance(value, list):
            distinct.update(json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value)
        elif isinstance(value, dict):
            distinct.update(str(key) for key in value)
        elif value is not None:
            distinct.add(json.dumps(value, ensure_ascii=False))
    return len(distinct)


def field_lines(datasets: dict[str, DatasetData]) -> list[str]:
    """Builds one collapsible field table per dataset with records.

    Args:
        datasets: Records per dataset.

    Returns:
        list[str]: Markdown lines; empty if no dataset has records.
    """
    lines: list[str] = []
    for dataset, data in datasets.items():
        if not data.records:
            continue
        total = len(data.records)
        lines.extend([
            "<details>",
            f"<summary><code>{dataset}</code>: {len(DATASET_MODELS[dataset].model_fields)} fields</summary>",
            "",
            "| Field | Description | Filled | Distinct values |",
            "|---|---|---|---|",
        ])
        for name, field in DATASET_MODELS[dataset].model_fields.items():
            values = [record.get(name) for record in data.records]
            filled = sum(1 for value in values if _is_filled(value))
            lines.append(f"| `{name}` | {_cell(field.description)} | {_share(filled, total)} | {_distinct(values):,} |")
        lines.extend(["", "</details>", ""])
    return lines


def registry_lines(registry_path: Path) -> list[str]:
    """Summarizes the technology registry: entries per domain and identifiers per source.

    Args:
        registry_path: The registry file.

    Returns:
        list[str]: Markdown lines.
    """
    try:
        technologies = [technology for technology in load_registry(registry_path) if technology.active]
    except (FileNotFoundError, RegistryError) as e:
        return [f"- Registry not readable: {_cell(str(e))[:200]}"]
    if not technologies:
        return ["- No technologies registered."]
    domains = Counter(technology.domain for technology in technologies)
    coverage = {
        "Wikipedia article": sum(1 for t in technologies if t.wikidata),
        "Stack Overflow tags": sum(1 for t in technologies if t.stackoverflow_tags),
        "GitHub repositories": sum(1 for t in technologies if t.github_repos),
        "Packages or images": sum(1 for t in technologies if t.packages),
        "endoflife.date product": sum(1 for t in technologies if t.endoflife),
        "Hacker News terms": sum(1 for t in technologies if t.hn_terms),
    }
    lines = [f"- **Active technologies:** {len(technologies):,} in {len(domains)} domains", "",
             "| Domain | Technologies |", "|---|---|"]
    lines.extend(f"| {domain.replace('_', ' ')} | {count:,} |" for domain, count in sorted(domains.items()))
    lines.extend(["", "| Identifier | Technologies with it |", "|---|---|"])
    lines.extend(f"| {label} | {_share(count, len(technologies))} |" for label, count in coverage.items())
    return lines


def last_run_lines(state_dir: Path) -> list[str]:
    """Summarizes the latest run from the run log.

    Args:
        state_dir: Folder of the state files.

    Returns:
        list[str]: Markdown lines.
    """
    path = state_dir / RUN_LOG_FILE
    if not path.exists():
        return ["- No run yet."]
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return ["- No run yet."]
    entry = json.loads(lines[-1])
    rows = [f"- **Latest run:** {entry.get('started', '?')[:16].replace('T', ' ')} UTC ({entry.get('mode')})"]
    for name, outcome in entry.get("collectors", {}).items():
        records = ", ".join(f"{dataset} {count:,}" for dataset, count in outcome.get("records", {}).items())
        error = f"; {outcome['error']}" if outcome.get("error") else ""
        rows.append(f"  - `{name}`: {outcome.get('status')}{' (' + records + ')' if records else ''}{error}")
    return rows


def build_overview(data_dir: Path, registry_path: Path = CONFIG_PATH, now: Optional[datetime] = None) -> str:
    """Builds the Markdown of the overview.

    Args:
        data_dir: Root folder of the datasets.
        registry_path: The registry file.
        now: Current UTC time; defaults to now.

    Returns:
        str: The overview without the markers.
    """
    now = now or datetime.now(timezone.utc)
    datasets = load_datasets(data_dir)
    fields = field_lines(datasets)
    parts = [
        f"**Last updated:** {now:%Y-%m-%d %H:%M} UTC",
        "",
        *dataset_lines(datasets),
        "",
        "**Runs**",
        "",
        *last_run_lines(data_dir / "state"),
        "",
        "**Technology registry**",
        "",
        *registry_lines(registry_path),
    ]
    if fields:
        parts.extend(["", "**Fields** (Filled: records with a value that is not null and not empty)", "", *fields])
    return "\n".join(parts).rstrip()


def update_readme(readme: Path, overview: str) -> bool:
    """Replaces the text between the markers of the README.

    Args:
        readme: The README file.
        overview: The new overview.

    Returns:
        bool: True if the README changed.

    Raises:
        ValueError: If the markers are missing.
    """
    text = readme.read_text(encoding="utf-8")
    start = text.find(START_MARKER)
    end = text.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"{readme} has no overview markers")
    updated = text[: start + len(START_MARKER)] + "\n" + overview + "\n" + text[end:]
    if updated == text:
        return False
    readme.write_text(updated, encoding="utf-8", newline="\n")
    return True


def main(argv: Optional[list[str]] = None) -> int:
    """Writes the overview into the README.

    Args:
        argv: Arguments without the program name; None reads ``sys.argv``.

    Returns:
        int: 0 on success, 1 if the README has no markers.
    """
    parser = argparse.ArgumentParser(description="Writes the dataset overview into the README.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        changed = update_readme(args.readme, build_overview(args.data_dir))
    except ValueError as e:
        logger.error("%s", e)
        return 1
    logger.info("README overview %s.", "updated" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())

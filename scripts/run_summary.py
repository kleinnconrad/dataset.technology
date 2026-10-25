"""
Writes a Markdown summary of the latest pipeline run for the notification comment.

The summary is built from the last line of ``data/state/run_log.jsonl``: the
status and records of each collector and the remaining API quotas that the
collectors report. The notification workflow posts it as a commit comment
that mentions the repository owner, so that GitHub sends an email.

Usage:
    python scripts/run_summary.py --run-log data/state/run_log.jsonl --output summary.md
        [--conclusion success] [--run-url URL] [--mention USER] [--not-before TIMESTAMP]
"""
import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Counters of the collectors that report the remaining quota of an API
QUOTA_COUNTERS = ("quota_remaining", "rate_limit_remaining")


def last_entry(run_log: Path, not_before: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Returns the last entry of the run log.

    Args:
        run_log: Path of ``run_log.jsonl``.
        not_before: If given, an entry that started earlier belongs to a previous run and is ignored.

    Returns:
        Optional[dict[str, Any]]: The entry, or None if the file is missing or has no entry of this run.
    """
    if not run_log.exists():
        return None
    lines = [line for line in run_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return None
    entry = json.loads(lines[-1])
    if not_before is not None and datetime.fromisoformat(entry["started"]) < not_before:
        return None
    return entry


def _duration_minutes(entry: dict[str, Any]) -> Optional[float]:
    """Returns the duration of a run in minutes, or None if it has no end time."""
    if not entry.get("finished"):
        return None
    started = datetime.fromisoformat(entry["started"])
    finished = datetime.fromisoformat(entry["finished"])
    return (finished - started).total_seconds() / 60


def build_summary(entry: Optional[dict[str, Any]], conclusion: str, run_url: Optional[str],
                  mention: Optional[str]) -> str:
    """Builds the Markdown of the notification.

    Args:
        entry: The run log entry of the run; None if the run wrote none.
        conclusion: Conclusion of the workflow run (success, failure, ...).
        run_url: URL of the workflow run.
        mention: GitHub user to mention, so that GitHub sends an email.

    Returns:
        str: The Markdown text.
    """
    greeting = f"@{mention} " if mention else ""
    lines = [f"{greeting}Tech dataset pipeline: **{conclusion}**"]
    if run_url:
        lines.append(f"Workflow run: {run_url}")
    lines.append("")
    if entry is None:
        lines.append("The run wrote no run log entry; see the workflow log.")
        return "\n".join(lines) + "\n"

    duration = _duration_minutes(entry)
    lines.append(f"- **Mode:** {entry.get('mode')}")
    lines.append(f"- **Started:** {entry['started'][:16].replace('T', ' ')} UTC"
                 + (f", duration {duration:.0f} min" if duration is not None else ""))
    lines.append(f"- **HTTP requests:** {entry.get('requests', 0):,}")
    lines.extend(["", "| Collector | Status | Records |", "|---|---|---|"])
    collectors = entry.get("collectors", {})
    quotas = []
    for name, outcome in collectors.items():
        records = ", ".join(f"{dataset} {count:,}" for dataset, count in outcome.get("records", {}).items())
        error = f" ({outcome['error']})" if outcome.get("error") else ""
        lines.append(f"| `{name}` | {outcome.get('status')}{error} | {records} |")
        counters = outcome.get("counters", {})
        quotas.extend(f"`{name}` {counter.replace('_', ' ')} {counters[counter]:,}"
                      for counter in QUOTA_COUNTERS if isinstance(counters.get(counter), int))
    if quotas:
        lines.extend(["", "**API quotas after the run:** " + "; ".join(quotas) + "."])
    return "\n".join(lines) + "\n"


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    """Parses the command-line arguments.

    Args:
        argv: Arguments without the program name; None reads ``sys.argv``.

    Returns:
        argparse.Namespace: The parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Writes a Markdown summary of the latest pipeline run.")
    parser.add_argument("--run-log", type=Path, required=True, help="path of run_log.jsonl")
    parser.add_argument("--output", type=Path, required=True, help="path of the Markdown file to write")
    parser.add_argument("--conclusion", default="completed", help="conclusion of the workflow run")
    parser.add_argument("--run-url", help="URL of the workflow run")
    parser.add_argument("--mention", help="GitHub user to mention")
    parser.add_argument("--not-before", type=datetime.fromisoformat,
                        help="start of the workflow run (ISO 8601); older run log entries are ignored")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Writes the summary file.

    Args:
        argv: Arguments without the program name; None reads ``sys.argv``.

    Returns:
        int: 0 on success, 1 if the run log cannot be read.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    args = parse_args(argv)
    try:
        entry = last_entry(args.run_log, args.not_before)
    except (ValueError, KeyError) as e:
        logger.error("Run log %s cannot be read: %s", args.run_log, e)
        return 1
    args.output.write_text(build_summary(entry, args.conclusion, args.run_url, args.mention), encoding="utf-8")
    logger.info("Summary written to %s.", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())

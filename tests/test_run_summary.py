"""Tests of the run summary of the notification workflow."""
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_summary.py"
_spec = importlib.util.spec_from_file_location("run_summary", SCRIPT)
assert _spec is not None and _spec.loader is not None
run_summary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_summary)

ENTRY = {
    "started": "2026-10-26T02:17:00+00:00", "finished": "2026-10-26T02:41:00+00:00", "mode": "normal",
    "requests": 2140,
    "collectors": {
        "technologies": {"status": "ok", "records": {"technologies": 3}},
        "wikipedia_pageviews": {"status": "not_due"},
        "stackoverflow_questions": {"status": "ok", "records": {"stackoverflow_questions": 160},
                                    "counters": {"quota_remaining": 9650}},
        "cves": {"status": "failed", "error": "FetchError: HTTP 503"},
    },
}


def write_log(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "run_log.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    return path


def test_summary_lists_collectors_and_quotas() -> None:
    text = run_summary.build_summary(ENTRY, "failure", "https://github.com/run/1", "owner")

    assert text.startswith("@owner Tech dataset pipeline: **failure**")
    assert "duration 24 min" in text
    assert "| `wikipedia_pageviews` | not_due |  |" in text
    assert "| `technologies` | ok | technologies 3 |" in text
    assert "| `cves` | failed (FetchError: HTTP 503) |  |" in text
    assert "`stackoverflow_questions` quota remaining 9,650" in text


def test_entry_of_an_earlier_run_is_ignored(tmp_path: Path) -> None:
    log = write_log(tmp_path, ENTRY)

    assert run_summary.last_entry(log) == ENTRY
    assert run_summary.last_entry(log, datetime(2026, 10, 27, tzinfo=timezone.utc)) is None
    assert run_summary.last_entry(tmp_path / "missing.jsonl") is None
    assert "wrote no run log entry" in run_summary.build_summary(None, "failure", None, None)


def test_main_writes_the_summary_file(tmp_path: Path) -> None:
    log = write_log(tmp_path, {"started": "2026-10-22T02:17:00+00:00", "mode": "normal", "collectors": {}}, ENTRY)
    output = tmp_path / "summary.md"

    assert run_summary.main(["--run-log", str(log), "--output", str(output), "--conclusion", "success",
                             "--not-before", "2026-10-26T02:16:30Z"]) == 0
    assert "Tech dataset pipeline: **success**" in output.read_text(encoding="utf-8")

"""Tests of the monthly review of the technology registry."""
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from conftest import REGISTRY_FIXTURE
from registry import load_registry
from settings import Settings
from source_review import (ENDOFLIFE_URL, GITHUB_REPO_URL, WIKIDATA_API_URL, IdentifierChecker, ReviewResult,
                           build_report, check_collector_health, check_identifiers, insert_entry, main,
                           parse_candidates, propose_additions, replace_in_entry)
from state import CollectorStates

TODAY = date(2026, 11, 1)
TAGS_URL = "https://api.stackexchange.com/2.3/tags/large-language-model;postgresql;python;pytorch;reactjs;rust/info"


def _serve_sources(fake_web) -> None:
    fake_web.add(WIKIDATA_API_URL, {"entities": {
        "Q28865": {"sitelinks": {"enwiki": {"title": "Python (programming language)"}}},
        "Q999": {"redirects": {"from": "Q575650", "to": "Q999"}, "sitelinks": {"enwiki": {"title": "Rust"}}},
        "Q19399674": {"sitelinks": {"enwiki": {"title": "React (software)"}}},
        "Q47509047": {"id": "Q47509047", "missing": ""},
        "Q115305900": {"sitelinks": {"enwiki": {"title": "Large language model"}}},
        "Q192490": {"sitelinks": {}},
    }})
    fake_web.add(TAGS_URL, {"items": [{"name": tag} for tag in
                                      ("large-language-model", "postgresql", "python", "reactjs", "rust")]})
    fake_web.add(GITHUB_REPO_URL.format(repo="python/cpython"), {"full_name": "python/cpython", "archived": False})
    fake_web.add(GITHUB_REPO_URL.format(repo="rust-lang/rust"), {"full_name": "rust-lang/rust", "archived": False})
    fake_web.add(GITHUB_REPO_URL.format(repo="facebook/react"), {"full_name": "react/react", "archived": False})
    fake_web.add(GITHUB_REPO_URL.format(repo="pytorch/pytorch"), {"full_name": "pytorch/pytorch", "archived": True})
    for url in ("https://hub.docker.com/v2/namespaces/library/repositories/python",
                "https://hub.docker.com/v2/namespaces/library/repositories/postgres",
                "https://api.npmjs.org/downloads/point/last-week/react", "https://pypi.org/pypi/polars/json",
                "https://crates.io/api/v1/crates/polars"):
        fake_web.add(url, {"ok": True})
    fake_web.add(ENDOFLIFE_URL.format(slug="python"), {"result": {"name": "python"}})
    fake_web.add(ENDOFLIFE_URL.format(slug="react"), {"result": {"name": "react-js"}})
    fake_web.add(ENDOFLIFE_URL.format(slug="postgresql"), {"result": {"name": "postgresql"}})


def test_identifiers_are_checked_renames_fixed_and_missing_ones_reported(fake_web) -> None:
    _serve_sources(fake_web)
    technologies = load_registry(REGISTRY_FIXTURE)
    result = ReviewResult(text=REGISTRY_FIXTURE.read_text(encoding="utf-8"))

    check_identifiers(IdentifierChecker(fake_web.client(), Settings()), technologies, result)

    assert any("Q575650 was merged into Q999" in fix for fix in result.fixes)
    assert any("`facebook/react` moved to `react/react`" in fix for fix in result.fixes)
    assert any("`react` is now `react-js`" in fix for fix in result.fixes)
    assert '"Q999"' in result.text and '"react/react"' in result.text and 'endoflife = "react-js"' in result.text
    issues = "\n".join(result.issues)
    assert "Q47509047 is missing" in issues and "Q192490 is no enwiki" in issues
    assert "tag `pytorch`" in issues and "`pytorch/pytorch` is archived" in issues
    assert "package `pypi:torch` does not exist" in issues
    # The removed entry gopher is not checked
    assert "gopher" not in issues and "Q271373" not in issues
    assert result.checked["github_repos"] == 4
    assert 'id = "react"' in result.text


class StubChecker:
    """Answers the identifier checks of the proposals without network access."""

    def wikidata_for_title(self, title: str) -> Optional[str]:
        return {"Vision-language model": "Q125000001", "PyTorch": "Q47509047"}.get(title)

    def existing_tags(self, tags):
        return {"vision-language-model"}

    def repository(self, repo: str):
        return ("ok", repo) if repo == "example/vlm" else ("missing", None)

    def package(self, value: str) -> str:
        return "ok" if value == "pypi:vlm" else "missing"

    def endoflife(self, slug: str):
        return ("missing", None)


class FakeModel:
    """Returns prepared answers per domain and counts the requests."""

    def __init__(self) -> None:
        self.calls = 0

    def ask(self, prompt: str) -> str:
        self.calls += 1
        if "Domain: ai_ml." not in prompt:
            return "[]"
        return """```json
[{"name": "Vision-language model", "category": "ai_concept", "wikipedia_title": "Vision-language model",
  "stackoverflow_tags": ["vision-language-model", "unknown-tag"], "github_repos": ["example/vlm", "gone/repo"],
  "packages": ["pypi:vlm", "pypi:missing", "maven:x"], "endoflife": null,
  "hn_terms": ["vision language model", "vlm", "ai"], "reason": "Rising model type."},
 {"name": "PyTorch 2", "category": "ai_framework", "wikipedia_title": "PyTorch", "reason": "duplicate"},
 {"name": "Router", "category": "network_protocol", "wikipedia_title": "Router", "reason": "wrong domain"}]
```"""


def test_verified_proposals_are_added_to_their_category_and_others_rejected() -> None:
    technologies = load_registry(REGISTRY_FIXTURE)
    result = ReviewResult(text=REGISTRY_FIXTURE.read_text(encoding="utf-8"))
    model = FakeModel()

    propose_additions(model, StubChecker(), list(technologies), TODAY, result)

    assert len(result.additions) == 1 and "`vision_language_model` (ai_concept)" in result.additions[0]
    assert any("Wikidata item Q47509047 is already in the registry" in reason for reason in result.rejected)
    assert any("does not belong to ai_ml" in reason for reason in result.rejected)
    assert result.search_calls == 8
    block = result.text.split('id = "vision_language_model"', 1)[1].split("\n\n", 1)[0]
    assert 'wikidata = "Q125000001"' in block and 'stackoverflow_tags = ["vision-language-model"]' in block
    assert 'github_repos = ["example/vlm"]' in block and 'packages = ["pypi:vlm"]' in block
    assert 'hn_terms = ["vision language model", "vlm"]' in block and "added = 2026-11-01" in block
    # Inserted after the last ai_concept entry, before the next entry of another category
    assert result.text.index('id = "vision_language_model"') < result.text.index('id = "postgresql"')


def test_proposed_registry_stays_valid(tmp_path: Path) -> None:
    technologies = load_registry(REGISTRY_FIXTURE)
    result = ReviewResult(text=REGISTRY_FIXTURE.read_text(encoding="utf-8"))
    propose_additions(FakeModel(), StubChecker(), list(technologies), TODAY, result)
    path = tmp_path / "technologies.toml"
    path.write_text(result.text, encoding="utf-8")

    reviewed = load_registry(path)

    assert len(reviewed) == len(technologies) + 1


def test_text_helpers_change_only_the_given_entry() -> None:
    text = REGISTRY_FIXTURE.read_text(encoding="utf-8")
    changed = replace_in_entry(text, "rust", "github_repos", "rust-lang/rust", "rust-lang/rust2")
    assert changed.count("rust-lang/rust2") == 1
    added = insert_entry(text, {"id": "new_db", "name": "New DB", "category": "database", "added": TODAY},
                         "New DB", load_registry(REGISTRY_FIXTURE))
    assert "# New DB\n[[technology]]\nid = \"new_db\"" in added


def test_candidates_are_parsed_from_answers_with_text_around_the_array() -> None:
    candidates = parse_candidates('Here: [{"name": "X", "category": "database", "wikipedia_title": "X"}, {"bad": 1}]')
    assert [candidate.name for candidate in candidates] == ["X"]


def test_collector_health_lists_failures_overdue_and_missing_runs(tmp_path: Path) -> None:
    states = CollectorStates.load(tmp_path)
    started = datetime(2026, 10, 27, 2, 7, tzinfo=timezone.utc)
    states.entry("failing", date(2026, 9, 1))
    states.record_failure("failing", started, "FetchError: HTTP 500")
    states.record_failure("failing", started, "FetchError: HTTP 500")
    states.entry("overdue", date(2026, 8, 1))
    states.record_success("overdue", datetime(2026, 8, 1, tzinfo=timezone.utc))
    states.entry("fine", date(2026, 10, 1))
    states.record_success("fine", started)
    states.save()
    result = ReviewResult(text="")

    check_collector_health(tmp_path, {"failing": 6, "overdue": 6, "fine": 6, "new": 27}, TODAY, result)

    health = "\n".join(result.health)
    assert "`failing`: failed in 2 consecutive runs" in health
    assert "`overdue`: last complete run 2026-08-01" in health
    assert "`new`: has never run" in health
    assert "`fine`" not in health


def test_report_lists_every_section() -> None:
    result = ReviewResult(text="", fixes=["fix"], issues=["issue"], notes=["note"], checked={"wikidata": 3})
    report = build_report(result, TODAY)
    assert report.startswith("## Registry review 2026-11")
    assert "- fix" in report and "- issue" in report and "### Added technologies\n\n- none" in report


def test_main_without_search_writes_the_report_and_keeps_an_unchanged_registry(tmp_path: Path, monkeypatch) -> None:
    registry = tmp_path / "technologies.toml"
    registry.write_text(REGISTRY_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    report = tmp_path / "review.md"

    def offline_review(web, settings, registry_path, state_dir, intervals, model, today):
        assert model is None
        return ReviewResult(text=registry_path.read_text(encoding="utf-8"))

    monkeypatch.setattr("source_review.run_review", offline_review)
    assert main(["--report", str(report), "--registry", str(registry), "--state-dir", str(tmp_path), "--no-search"]) == 0
    assert "## Registry review" in report.read_text(encoding="utf-8")
    assert registry.read_text(encoding="utf-8") == REGISTRY_FIXTURE.read_text(encoding="utf-8")

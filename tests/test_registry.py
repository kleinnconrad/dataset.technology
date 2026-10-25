"""Tests of the technology registry: the parser and the committed registry file."""
from datetime import date
from typing import get_args

import pytest

from conftest import REGISTRY_FIXTURE
from registry import CONFIG_PATH, RegistryError, active_technologies, load_registry, parse_package, parse_technology
from schema import CATEGORY_DOMAINS, TechnologyCategory, TechnologyDomain


def _entry(**changes) -> dict:
    entry = {"id": "rust", "name": "Rust", "category": "programming_language", "added": date(2026, 10, 1)}
    entry.update(changes)
    return entry


def test_every_category_has_a_domain() -> None:
    assert set(CATEGORY_DOMAINS) == set(get_args(TechnologyCategory))
    assert set(CATEGORY_DOMAINS.values()) == set(get_args(TechnologyDomain))


def test_fixture_registry_parses_and_excludes_removed_entries() -> None:
    technologies = load_registry(REGISTRY_FIXTURE)
    active = active_technologies(REGISTRY_FIXTURE)

    assert len(technologies) == len(active) + 1
    assert "gopher" not in {t.id for t in active}
    react = next(t for t in technologies if t.id == "react")
    assert react.domain == "software_development"
    assert [package.key for package in react.packages] == ["npm:react"]


@pytest.mark.parametrize("changes, message", [
    ({"id": "Rust"}, "snake_case"),
    ({"category": "language"}, "unknown category"),
    ({"wikidata": "575650"}, "Wikidata id"),
    ({"stackoverflow_tags": ["Rust Lang"]}, "Stack Overflow tags"),
    ({"github_repos": ["rust-lang"]}, "owner/name"),
    ({"packages": ["cargo:serde"]}, "ecosystem"),
    ({"hn_terms": ["Rust"]}, "lower case"),
    ({"removed_reason": "gone"}, "together"),
    ({"tags": ["rust"]}, "unknown keys"),
])
def test_invalid_entries_are_rejected(changes: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_technology(_entry(**changes))


def test_duplicate_identifiers_are_rejected(tmp_path) -> None:
    path = tmp_path / "technologies.toml"
    path.write_text(
        '[[technology]]\nid = "a"\nname = "A"\ncategory = "database"\nadded = 2026-10-01\nstackoverflow_tags = ["x"]\n'
        '[[technology]]\nid = "b"\nname = "B"\ncategory = "database"\nadded = 2026-10-01\nstackoverflow_tags = ["x"]\n',
        encoding="utf-8")

    with pytest.raises(RegistryError, match="duplicate stackoverflow_tags: x"):
        load_registry(path)


def test_scoped_npm_and_docker_packages_are_parsed() -> None:
    assert parse_package("npm:@angular/core").name == "@angular/core"
    assert parse_package("docker:library/postgres").name == "library/postgres"
    with pytest.raises(ValueError):
        parse_package("docker:postgres")


def test_committed_registry_is_valid() -> None:
    technologies = load_registry(CONFIG_PATH)

    assert len(technologies) >= 100
    domains = {technology.domain for technology in technologies}
    assert domains == set(get_args(TechnologyDomain))
    assert all(technology.wikidata or technology.stackoverflow_tags or technology.packages
               for technology in technologies)

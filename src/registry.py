"""
The registry of tracked technologies (``config/technologies.toml``).

Every technology has a stable ``id`` that links the datasets: pageviews,
Stack Overflow tags, GitHub repositories, package downloads, release cycles
and Hacker News mentions all carry the ``technology_id`` of the entry whose
identifier they were collected for. An entry lists the identifiers of the
technology at each source; a collector reads only the identifiers it needs.

Every entry has the date on which it was ``added``. An entry that is no
longer collected keeps its place with the date on which it was ``removed``
and a ``removed_reason``, so that the coverage period of every technology
stays known; ids are never reused. Each identifier belongs to one entry only,
so that no value is counted for two technologies.
"""
import re
import tomllib
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from schema import CATEGORY_DOMAINS

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "technologies.toml"

PACKAGE_ECOSYSTEMS = ("npm", "pypi", "crates", "docker")
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
WIKIDATA_PATTERN = re.compile(r"^Q[1-9][0-9]*$")
# Stack Overflow tags consist of lower-case letters, digits and the characters + # - .
TAG_PATTERN = re.compile(r"^[a-z0-9+#.-]+$")
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
KNOWN_KEYS = frozenset({
    "id", "name", "category", "added", "removed", "removed_reason", "wikidata", "stackoverflow_tags",
    "github_repos", "packages", "endoflife", "hn_terms", "github_language",
})


class RegistryError(ValueError):
    """The registry file contains invalid or duplicate entries."""


@dataclass(frozen=True)
class Package:
    """A package or container image that serves as an adoption proxy of a technology.

    Attributes:
        ecosystem (str): npm, pypi, crates or docker.
        name (str): Package name; for docker the repository with namespace, e.g. ``library/postgres``.
    """
    ecosystem: str
    name: str

    @property
    def key(self) -> str:
        """The package as written in the registry, e.g. ``pypi:torch``."""
        return f"{self.ecosystem}:{self.name}"


@dataclass(frozen=True)
class Technology:
    """A tracked technology.

    Attributes:
        id (str): Stable id, lower-case snake_case; never reused.
        name (str): Display name.
        category (str): Category (see ``schema.TechnologyCategory``).
        added (date): Day on which the entry was added; start of coverage.
        removed (Optional[date]): Day on which the entry was removed; it is no longer collected.
        removed_reason (Optional[str]): Why the entry was removed.
        wikidata (Optional[str]): Wikidata id of the technology; its English Wikipedia
            article is resolved at run time.
        stackoverflow_tags (tuple[str, ...]): Stack Overflow tags.
        github_repos (tuple[str, ...]): Main GitHub repositories as ``owner/name``.
        packages (tuple[Package, ...]): Packages and container images.
        endoflife (Optional[str]): Product slug at endoflife.date.
        hn_terms (tuple[str, ...]): Lower-case terms counted in Hacker News stories and comments.
        github_language (Optional[str]): Language name in GitHub search (Linguist).
    """
    id: str
    name: str
    category: str
    added: date
    removed: Optional[date] = None
    removed_reason: Optional[str] = None
    wikidata: Optional[str] = None
    stackoverflow_tags: tuple[str, ...] = ()
    github_repos: tuple[str, ...] = ()
    packages: tuple[Package, ...] = ()
    endoflife: Optional[str] = None
    hn_terms: tuple[str, ...] = ()
    github_language: Optional[str] = None

    @property
    def domain(self) -> str:
        """The domain of the category, e.g. ``ai_ml``."""
        return CATEGORY_DOMAINS[self.category]

    @property
    def active(self) -> bool:
        """True if the technology is collected, i.e. it was not removed."""
        return self.removed is None


def _str_list(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    """Reads an optional list of non-empty strings."""
    values = raw.get(key, [])
    if not isinstance(values, list) or not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return tuple(value.strip() for value in values)


def _optional_str(raw: dict[str, Any], key: str) -> Optional[str]:
    """Reads an optional non-empty string."""
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_date(raw: dict[str, Any], key: str) -> Optional[date]:
    """Reads an optional TOML date."""
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{key} must be a date (YYYY-MM-DD)")
    return value


def parse_package(value: str) -> Package:
    """Parses a package reference such as ``npm:@angular/core`` or ``docker:library/postgres``.

    Args:
        value: The reference.

    Returns:
        Package: The parsed package.

    Raises:
        ValueError: If the ecosystem is unknown or the name is empty.
    """
    ecosystem, _, name = value.partition(":")
    if ecosystem not in PACKAGE_ECOSYSTEMS or not name:
        raise ValueError(f"package {value!r} must be <ecosystem>:<name> with ecosystem one of "
                         f"{', '.join(PACKAGE_ECOSYSTEMS)}")
    if ecosystem == "docker" and not REPO_PATTERN.match(name):
        raise ValueError(f"docker image {value!r} must be docker:<namespace>/<repository>")
    return Package(ecosystem, name)


def parse_technology(raw: dict[str, Any]) -> Technology:
    """Validates one registry entry.

    Args:
        raw: The entry as read from TOML.

    Returns:
        Technology: The parsed entry.

    Raises:
        ValueError: If a field is missing, unknown or invalid.
    """
    unknown = sorted(set(raw) - KNOWN_KEYS)
    if unknown:
        raise ValueError(f"unknown keys: {', '.join(unknown)}")
    technology_id = _optional_str(raw, "id")
    if technology_id is None or not ID_PATTERN.match(technology_id):
        raise ValueError("id must be lower-case snake_case")
    name = _optional_str(raw, "name")
    if name is None:
        raise ValueError("name is missing")
    category = raw.get("category")
    if category not in CATEGORY_DOMAINS:
        raise ValueError(f"unknown category {category!r}")
    added = _optional_date(raw, "added")
    if added is None:
        raise ValueError("added is missing")
    removed = _optional_date(raw, "removed")
    removed_reason = _optional_str(raw, "removed_reason")
    if (removed is None) != (removed_reason is None):
        raise ValueError("removed and removed_reason must be given together")
    if removed is not None and removed < added:
        raise ValueError("removed is earlier than added")
    wikidata = _optional_str(raw, "wikidata")
    if wikidata is not None and not WIKIDATA_PATTERN.match(wikidata):
        raise ValueError(f"wikidata {wikidata!r} is not a Wikidata id")
    tags = _str_list(raw, "stackoverflow_tags")
    bad_tags = [tag for tag in tags if not TAG_PATTERN.match(tag)]
    if bad_tags:
        raise ValueError(f"invalid Stack Overflow tags: {', '.join(bad_tags)}")
    repos = _str_list(raw, "github_repos")
    bad_repos = [repo for repo in repos if not REPO_PATTERN.match(repo)]
    if bad_repos:
        raise ValueError(f"GitHub repositories must be owner/name: {', '.join(bad_repos)}")
    terms = _str_list(raw, "hn_terms")
    if any(term != term.lower() for term in terms):
        raise ValueError("hn_terms must be lower case")
    return Technology(
        id=technology_id,
        name=name,
        category=category,
        added=added,
        removed=removed,
        removed_reason=removed_reason,
        wikidata=wikidata,
        stackoverflow_tags=tags,
        github_repos=repos,
        packages=tuple(parse_package(value) for value in _str_list(raw, "packages")),
        endoflife=_optional_str(raw, "endoflife"),
        hn_terms=terms,
        github_language=_optional_str(raw, "github_language"),
    )


def _duplicate_problems(technologies: Iterable[Technology]) -> list[str]:
    """Returns a message for every id or identifier that occurs in more than one entry."""
    technologies = list(technologies)
    identifiers: dict[str, list[str]] = {
        "id": [t.id for t in technologies],
        "wikidata": [t.wikidata for t in technologies if t.wikidata],
        "stackoverflow_tags": [tag for t in technologies for tag in t.stackoverflow_tags],
        "github_repos": [repo.lower() for t in technologies for repo in t.github_repos],
        "packages": [package.key for t in technologies for package in t.packages],
        "endoflife": [t.endoflife for t in technologies if t.endoflife],
        "hn_terms": [term for t in technologies for term in t.hn_terms],
        "github_language": [t.github_language for t in technologies if t.github_language],
    }
    problems = []
    for key, values in identifiers.items():
        duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
        if duplicates:
            problems.append(f"duplicate {key}: {', '.join(duplicates)}")
    return problems


def load_registry(path: Path = CONFIG_PATH) -> list[Technology]:
    """Reads and validates the registry.

    Args:
        path: The registry file.

    Returns:
        list[Technology]: All entries in file order, including removed ones.

    Raises:
        RegistryError: If an entry is invalid or an id or identifier is duplicated.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    entries = data.get("technology", [])
    if not isinstance(entries, list):
        raise RegistryError(f"{path} must contain [[technology]] entries")
    technologies: list[Technology] = []
    problems: list[str] = []
    for position, raw in enumerate(entries, start=1):
        try:
            technologies.append(parse_technology(raw))
        except ValueError as e:
            problems.append(f"entry {position} ({raw.get('id', '?')}): {e}")
    problems.extend(_duplicate_problems(technologies))
    if problems:
        raise RegistryError(f"{path}: " + "; ".join(problems))
    return technologies


def active_technologies(path: Path = CONFIG_PATH) -> list[Technology]:
    """Returns the entries that are collected, i.e. not removed.

    Args:
        path: The registry file.

    Returns:
        list[Technology]: The active entries in file order.
    """
    return [technology for technology in load_registry(path) if technology.active]

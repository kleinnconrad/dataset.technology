"""
Monthly review of the technology registry and of the health of the collectors.

The review proposes changes of ``config/technologies.toml``, which the
workflow ``source_review.yml`` opens as a pull request; they take effect when
the pull request is merged. It has three parts:

- **Identifiers:** every identifier of the active technologies is checked
  against its live source. Renames are fixed in the registry (a GitHub
  repository that moved, an endoflife.date slug that became an alias, a
  Wikidata item that was merged into another). Identifiers that no longer
  exist (deleted Stack Overflow tags, packages, repositories, products) are
  listed for a decision by hand; ids and entries are never removed
  automatically, because ids are permanent (CLAUDE.md, section 9).
- **Collector health:** collectors that failed in two or more consecutive runs
  or whose last complete run is overdue are listed, from
  ``data/state/collectors.json``.
- **Additions:** Gemini with Google Search proposes, per domain, technologies
  that are missing in the registry. A proposal is admitted only if its English
  Wikipedia article resolves to a Wikidata item that is not yet in the registry;
  its other identifiers are kept only if they exist and belong to no other
  entry. At most MAX_ADDITIONS technologies are added per review.

Usage:
    uv run python src/source_review.py --report review.md [--no-search]
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from collectors.base import batches
from registry import CONFIG_PATH, RegistryError, Technology, load_registry, parse_package
from schema import CATEGORY_DOMAINS
from settings import Settings
from state import CollectorStates
from web import BlockedError, FetchError, NotFoundError, RateLimitError, WebClient

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
MAX_ADDITIONS = 5
CANDIDATES_PER_DOMAIN = 3
MAX_SEARCH_CALLS = 8
MIN_SEARCH_INTERVAL_S = 2.0
SEARCH_TIMEOUT_MS = 120_000
# Collector health: failures in a row, and the multiple of the interval after which a complete run is overdue
FAILURES_TO_REPORT = 2
OVERDUE_FACTOR = 3
MIN_OVERDUE_DAYS = 14

WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"
STACKEXCHANGE_TAGS_URL = "https://api.stackexchange.com/2.3/tags/{tags}/info"
GITHUB_REPO_URL = "https://api.github.com/repos/{repo}"
ENDOFLIFE_URL = "https://endoflife.date/api/v1/products/{slug}"
PACKAGE_URLS = {
    "npm": "https://api.npmjs.org/downloads/point/last-week/{name}",
    "pypi": "https://pypi.org/pypi/{name}/json",
    "crates": "https://crates.io/api/v1/crates/{name}?include=",
    "docker": "https://hub.docker.com/v2/namespaces/{namespace}/repositories/{repository}",
}
# Request intervals of the hosts that are not in the pipeline's HOST_INTERVALS
REVIEW_HOST_INTERVALS = {
    "api.stackexchange.com": 0.2, "api.github.com": 0.25, "api.npmjs.org": 0.5, "endoflife.date": 0.5,
    "crates.io": 1.0, "hub.docker.com": 1.0, "pypi.org": 0.5,
}
ENTRY_PATTERN = re.compile(r"(?:# [^\n]*\n)?\[\[technology\]\]\n(?:[^\n]+\n)*")
KEY_ORDER = ("id", "name", "category", "added", "removed", "removed_reason", "wikidata", "stackoverflow_tags",
             "github_repos", "packages", "endoflife", "hn_terms", "github_language")


class SearchError(Exception):
    """The search model could not answer."""


class Candidate(BaseModel):
    """A technology proposed by the search model."""
    name: str = Field(description="Display name of the technology.")
    category: str = Field(description="Category of the registry.")
    wikipedia_title: str = Field(description="Exact title of the English Wikipedia article about the technology.")
    stackoverflow_tags: list[str] = Field(default_factory=list, description="Stack Overflow master tags.")
    github_repos: list[str] = Field(default_factory=list, description="Main GitHub repositories as owner/name.")
    packages: list[str] = Field(default_factory=list, description="npm:, pypi:, crates: packages or docker: images.")
    endoflife: Optional[str] = Field(default=None, description="Product slug at endoflife.date.")
    hn_terms: list[str] = Field(default_factory=list, description="Unambiguous lower-case terms.")
    reason: str = Field(default="", description="Why the technology belongs in the registry.")


@dataclass
class ReviewResult:
    """Outcome of a review.

    Attributes:
        text (str): The registry text after the review.
        fixes (list[str]): Automatic changes of identifiers.
        additions (list[str]): Added technologies with their identifiers.
        issues (list[str]): Findings that need a decision by hand.
        health (list[str]): Findings about the collectors.
        rejected (list[str]): Proposals that were not admitted, with the reason.
        notes (list[str]): Information about the review itself.
        checked (dict[str, int]): Number of checked identifiers per kind.
        search_calls (int): Requests sent to the search model.
    """
    text: str
    fixes: list[str] = field(default_factory=list)
    additions: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    health: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    checked: dict[str, int] = field(default_factory=dict)
    search_calls: int = 0

    @property
    def changed(self) -> bool:
        """True if the registry text was changed."""
        return bool(self.fixes or self.additions)


# ---------------------------------------------------------------------------
# Identifier checks
# ---------------------------------------------------------------------------

class IdentifierChecker:
    """Checks identifiers against their live sources and caches the answers of the review."""

    def __init__(self, web: WebClient, settings: Settings) -> None:
        """Initializes the checker.

        Args:
            web: The HTTP client.
            settings: The settings with the optional GitHub token and Stack Exchange key.
        """
        self._web = web
        self._settings = settings
        self._github_stopped = False
        self._repos: dict[str, tuple[str, Optional[str]]] = {}

    def wikidata(self, qids: Sequence[str]) -> dict[str, tuple[str, Optional[str]]]:
        """Checks Wikidata items.

        Args:
            qids: Wikidata ids.

        Returns:
            dict: Per id ``(status, value)``: ("ok", enwiki title), ("redirect", new id),
            ("no_enwiki", None) or ("missing", None).
        """
        results: dict[str, tuple[str, Optional[str]]] = {}
        for batch in batches(sorted(set(qids)), 50):
            data = self._web.get(WIKIDATA_API_URL, respect_robots=False, params={
                "action": "wbgetentities", "ids": "|".join(batch), "props": "sitelinks", "sitefilter": "enwiki",
                "format": "json"}).json()
            for key, item in (data.get("entities") or {}).items():
                redirect = item.get("redirects") or {}
                if redirect.get("from") in batch:
                    results[redirect["from"]] = ("redirect", redirect.get("to"))
                    continue
                if "missing" in item:
                    results[key] = ("missing", None)
                    continue
                title = ((item.get("sitelinks") or {}).get("enwiki") or {}).get("title")
                results[key] = ("ok", title) if title else ("no_enwiki", None)
        return results

    def wikidata_for_title(self, title: str) -> Optional[str]:
        """Returns the Wikidata id of an English Wikipedia article, following redirects.

        Args:
            title: Article title.

        Returns:
            Optional[str]: The Wikidata id, or None if the article does not exist.
        """
        data = self._web.get(WIKIPEDIA_API_URL, respect_robots=False, params={
            "action": "query", "prop": "pageprops", "ppprop": "wikibase_item", "redirects": "1", "titles": title,
            "format": "json", "formatversion": "2"}).json()
        pages = (data.get("query") or {}).get("pages") or []
        if not pages or pages[0].get("missing"):
            return None
        return (pages[0].get("pageprops") or {}).get("wikibase_item")

    def existing_tags(self, tags: Sequence[str]) -> Optional[set[str]]:
        """Returns the tags that exist as master tags on Stack Overflow.

        Args:
            tags: Tags to check.

        Returns:
            Optional[set[str]]: The existing tags; None if the API could not be asked (quota).
        """
        found: set[str] = set()
        for batch in batches(sorted(set(tags)), 100):
            params: dict[str, Any] = {"site": "stackoverflow", "pagesize": 100}
            if self._settings.stackexchange_key:
                params["key"] = self._settings.stackexchange_key
            url = STACKEXCHANGE_TAGS_URL.format(tags=";".join(quote(tag, safe="") for tag in batch))
            try:
                data = self._web.get(url, params=params, respect_robots=False).json()
            except (RateLimitError, FetchError) as e:
                logger.warning("Stack Exchange tags not checked: %s", e)
                return None
            found.update(item.get("name") for item in data.get("items", []) if item.get("name"))
        return found

    def repository(self, repo: str) -> tuple[str, Optional[str]]:
        """Checks a GitHub repository.

        Args:
            repo: Repository as owner/name.

        Returns:
            tuple: ("ok", full name), ("archived", full name), ("missing", None) or ("unchecked", None)
            after the rate limit was reached.
        """
        if repo in self._repos:
            return self._repos[repo]
        if self._github_stopped:
            return ("unchecked", None)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._settings.github_token:
            headers["Authorization"] = f"Bearer {self._settings.github_token}"
        try:
            data = self._web.get(GITHUB_REPO_URL.format(repo=repo), headers=headers, respect_robots=False).json()
            full_name = str(data.get("full_name") or repo)
            result = ("archived" if data.get("archived") else "ok", full_name)
        except NotFoundError:
            result = ("missing", None)
        except (RateLimitError, BlockedError) as e:
            logger.warning("GitHub repositories not checked any more in this review: %s", e)
            self._github_stopped = True
            return ("unchecked", None)
        self._repos[repo] = result
        return result

    def package(self, value: str) -> str:
        """Checks a package or container image.

        Args:
            value: Package as written in the registry, e.g. ``pypi:torch``.

        Returns:
            str: ok, missing or unchecked.
        """
        package = parse_package(value)
        if package.ecosystem == "docker":
            namespace, repository = package.name.split("/", 1)
            url = PACKAGE_URLS["docker"].format(namespace=namespace, repository=repository)
        else:
            url = PACKAGE_URLS[package.ecosystem].format(name=package.name.lower() if package.ecosystem == "pypi"
                                                         else package.name)
        try:
            self._web.get(url, respect_robots=False)
            return "ok"
        except NotFoundError:
            return "missing"
        except FetchError as e:
            logger.warning("Package %s not checked: %s", value, e)
            return "unchecked"

    def endoflife(self, slug: str) -> tuple[str, Optional[str]]:
        """Checks an endoflife.date product.

        Args:
            slug: Product slug.

        Returns:
            tuple: ("ok", canonical slug), ("missing", None) or ("unchecked", None).
        """
        try:
            data = self._web.get(ENDOFLIFE_URL.format(slug=slug), respect_robots=False).json()
        except NotFoundError:
            return ("missing", None)
        except FetchError as e:
            logger.warning("endoflife.date product %s not checked: %s", slug, e)
            return ("unchecked", None)
        return ("ok", str((data.get("result") or {}).get("name") or slug))


# ---------------------------------------------------------------------------
# Registry text
# ---------------------------------------------------------------------------

def _entry_spans(text: str) -> dict[str, tuple[int, int]]:
    """Returns the character span of every entry block by technology id."""
    spans: dict[str, tuple[int, int]] = {}
    for match in ENTRY_PATTERN.finditer(text):
        found = re.search(r'^id = "([^"]+)"$', match.group(0), flags=re.MULTILINE)
        if found:
            spans[found.group(1)] = match.span()
    return spans


def replace_in_entry(text: str, technology_id: str, key: str, old: str, new: str) -> str:
    """Replaces a quoted value of one key within the entry of one technology.

    Args:
        text: The registry text.
        technology_id: Id of the entry.
        key: The key whose line holds the value, e.g. ``github_repos``.
        old: The current value (without quotes).
        new: The new value.

    Returns:
        str: The changed text.

    Raises:
        KeyError: If the entry, the key or the value is not found.
    """
    start, end = _entry_spans(text)[technology_id]
    lines = text[start:end].split("\n")
    quoted_old, quoted_new = json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False)
    for position, line in enumerate(lines):
        if line.startswith(f"{key} = ") and quoted_old in line:
            lines[position] = line.replace(quoted_old, quoted_new, 1)
            return text[:start] + "\n".join(lines) + text[end:]
    raise KeyError(f"{key} {old} not in the entry of {technology_id}")


def render_entry(entry: dict[str, Any], title: Optional[str]) -> str:
    """Renders a registry entry in the format of the registry file.

    Args:
        entry: Keys and values of the entry.
        title: English Wikipedia title for the comment line.

    Returns:
        str: The entry block with a trailing newline.
    """
    lines = [f"# {title}"] if title else []
    lines.append("[[technology]]")
    for key in KEY_ORDER:
        value = entry.get(key)
        if value in (None, [], ""):
            continue
        lines.append(f"{key} = {json.dumps(value, ensure_ascii=False) if isinstance(value, (str, list)) else value}")
    return "\n".join(lines) + "\n"


def insert_entry(text: str, entry: dict[str, Any], title: Optional[str], technologies: Sequence[Technology]) -> str:
    """Inserts an entry after the last entry of its category, or at the end of the file.

    Args:
        text: The registry text.
        entry: The new entry.
        title: English Wikipedia title for the comment line.
        technologies: The current entries, to find the category.

    Returns:
        str: The changed text.
    """
    block = render_entry(entry, title)
    spans = _entry_spans(text)
    same_category = [spans[t.id] for t in technologies if t.category == entry["category"] and t.id in spans]
    if not same_category:
        return text.rstrip("\n") + "\n\n" + block
    end = max(span[1] for span in same_category)
    return text[:end] + "\n" + block + text[end:]


# ---------------------------------------------------------------------------
# Review steps
# ---------------------------------------------------------------------------

def check_identifiers(checker: IdentifierChecker, technologies: Sequence[Technology], result: ReviewResult) -> None:
    """Checks the identifiers of the active technologies, fixes renames and records issues.

    Args:
        checker: The identifier checker.
        technologies: All registry entries.
        result: The review result; its text is changed for fixes.
    """
    active = [t for t in technologies if t.active]
    used_repos = {repo.lower() for t in technologies for repo in t.github_repos}
    used_slugs = {t.endoflife for t in technologies if t.endoflife}
    used_qids = {t.wikidata for t in technologies if t.wikidata}

    wikidata = checker.wikidata([t.wikidata for t in active if t.wikidata])
    result.checked["wikidata"] = len(wikidata)
    for technology in (t for t in active if t.wikidata):
        status, value = wikidata.get(technology.wikidata or "", ("missing", None))
        if status == "redirect" and value and value not in used_qids:
            result.text = replace_in_entry(result.text, technology.id, "wikidata", technology.wikidata or "", value)
            result.fixes.append(f"`{technology.id}`: Wikidata item {technology.wikidata} was merged into {value}")
        elif status in ("missing", "no_enwiki", "redirect"):
            result.issues.append(f"`{technology.id}`: Wikidata item {technology.wikidata} is {status.replace('_', ' ')}"
                                 + (f" ({value})" if value else ""))

    tags = [tag for t in active for tag in t.stackoverflow_tags]
    existing = checker.existing_tags(tags)
    if existing is None:
        result.notes.append("Stack Overflow tags were not checked (quota or API error).")
    else:
        result.checked["stackoverflow_tags"] = len(set(tags))
        for technology in active:
            for tag in technology.stackoverflow_tags:
                if tag not in existing:
                    result.issues.append(f"`{technology.id}`: Stack Overflow tag `{tag}` is no master tag any more "
                                         "(deleted or made a synonym)")

    unchecked_repos = 0
    for technology in active:
        for repo in technology.github_repos:
            status, full_name = checker.repository(repo)
            result.checked["github_repos"] = result.checked.get("github_repos", 0) + (status != "unchecked")
            if status == "unchecked":
                unchecked_repos += 1
            elif status == "missing":
                result.issues.append(f"`{technology.id}`: GitHub repository `{repo}` does not exist any more")
            else:
                if full_name and full_name.lower() != repo.lower() and full_name.lower() not in used_repos:
                    result.text = replace_in_entry(result.text, technology.id, "github_repos", repo, full_name)
                    used_repos.add(full_name.lower())
                    result.fixes.append(f"`{technology.id}`: GitHub repository `{repo}` moved to `{full_name}`")
                if status == "archived":
                    result.issues.append(f"`{technology.id}`: GitHub repository `{full_name}` is archived")
    if unchecked_repos:
        result.notes.append(f"{unchecked_repos} GitHub repositories were not checked (rate limit).")

    for technology in active:
        for package in technology.packages:
            status = checker.package(package.key)
            result.checked["packages"] = result.checked.get("packages", 0) + (status != "unchecked")
            if status == "missing":
                result.issues.append(f"`{technology.id}`: package `{package.key}` does not exist any more")

    for technology in (t for t in active if t.endoflife):
        status, canonical = checker.endoflife(technology.endoflife or "")
        result.checked["endoflife"] = result.checked.get("endoflife", 0) + (status != "unchecked")
        if status == "missing":
            result.issues.append(f"`{technology.id}`: endoflife.date product `{technology.endoflife}` does not exist "
                                 "any more")
        elif status == "ok" and canonical and canonical != technology.endoflife and canonical not in used_slugs:
            result.text = replace_in_entry(result.text, technology.id, "endoflife", technology.endoflife or "", canonical)
            used_slugs.add(canonical)
            result.fixes.append(f"`{technology.id}`: endoflife.date product `{technology.endoflife}` is now "
                                f"`{canonical}`")


def check_collector_health(state_dir: Path, intervals: dict[str, int], today: date, result: ReviewResult) -> None:
    """Lists collectors that failed repeatedly or whose last complete run is overdue.

    Args:
        state_dir: Folder of the pipeline state.
        intervals: ``min_days_between_runs`` per collector name.
        today: Day of the review.
        result: The review result.
    """
    if not (state_dir / "collectors.json").exists():
        result.notes.append("No collector state found; the collector health was not checked.")
        return
    states = CollectorStates.load(state_dir)
    for name, interval in intervals.items():
        entry = states.entry(name, today)
        if entry.last_run is None:
            result.health.append(f"`{name}`: has never run")
            continue
        if entry.consecutive_failures >= FAILURES_TO_REPORT:
            result.health.append(f"`{name}`: failed in {entry.consecutive_failures} consecutive runs; last error: "
                                 f"{entry.last_error}")
        overdue_days = max(OVERDUE_FACTOR * max(interval, 3), MIN_OVERDUE_DAYS)
        if entry.last_complete is None or (today - entry.last_complete.date()).days > overdue_days:
            last = entry.last_complete.date().isoformat() if entry.last_complete else "never"
            result.health.append(f"`{name}`: last complete run {last} (expected at least every {overdue_days} days)")


def _slug(name: str) -> str:
    """Returns a registry id for a display name."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug if slug and slug[0].isalpha() else f"t_{slug}"


def search_prompt(domain: str, categories: Sequence[str], technologies: Sequence[Technology]) -> str:
    """Builds the search prompt for one domain.

    Args:
        domain: The domain, e.g. ai_ml.
        categories: The categories of the domain.
        technologies: The current entries.

    Returns:
        str: The prompt.
    """
    names = ", ".join(sorted(t.name for t in technologies if t.domain == domain))
    return (
        "You maintain the registry of a long-term dataset that tracks technologies for 15 to 20 years "
        "(Wikipedia pageviews, Stack Overflow questions, GitHub activity, package downloads, Hacker News mentions).\n"
        f"Domain: {domain}. Categories: {', '.join(categories)}.\n"
        f"Technologies already in the registry for this domain: {names}.\n"
        f"Use Google Search and propose up to {CANDIDATES_PER_DOMAIN} technologies of this domain that are missing "
        "and that are widely used or rising now. Do not propose technologies that are already listed under another "
        "name. Answer only with a JSON array of objects with the keys name, category (one of the categories above), "
        "wikipedia_title (exact title of the English Wikipedia article about the technology itself), "
        "stackoverflow_tags, github_repos (owner/name of the main repository), packages (npm:name, pypi:name, "
        "crates:name or docker:namespace/repository of the core package or official image), endoflife (product slug "
        "at endoflife.date or null), hn_terms (lower-case terms that refer to the technology without ambiguity) and "
        "reason (one sentence). Use empty lists or null for anything you are not sure about."
    )


def parse_candidates(text: str) -> list[Candidate]:
    """Extracts the candidates from a model answer.

    Args:
        text: The answer; it may contain code fences or other text around the JSON array.

    Returns:
        list[Candidate]: The valid candidates; invalid objects are skipped.

    Raises:
        ValueError: If the answer contains no JSON array.
    """
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end < start:
        raise ValueError("the answer contains no JSON array")
    data = json.loads(text[start:end + 1])
    candidates = []
    for item in data if isinstance(data, list) else []:
        try:
            candidates.append(Candidate.model_validate(item))
        except ValidationError:
            continue
    return candidates


def verify_candidate(candidate: Candidate, domain: str, checker: IdentifierChecker,
                     technologies: Sequence[Technology], today: date) -> tuple[Optional[dict[str, Any]], str]:
    """Verifies a proposed technology and builds its registry entry.

    Args:
        candidate: The proposal.
        domain: The domain the proposal was made for.
        checker: The identifier checker.
        technologies: The current entries, including entries added in this review.
        today: Day of the review, used as ``added``.

    Returns:
        tuple: The entry (or None) and a reason for the decision.
    """
    if CATEGORY_DOMAINS.get(candidate.category) != domain:
        return None, f"category {candidate.category} does not belong to {domain}"
    technology_id = _slug(candidate.name)
    if technology_id in {t.id for t in technologies}:
        return None, f"id {technology_id} exists"
    qid = checker.wikidata_for_title(candidate.wikipedia_title)
    if qid is None:
        return None, f"no English Wikipedia article or Wikidata item for '{candidate.wikipedia_title}'"
    if qid in {t.wikidata for t in technologies}:
        return None, f"Wikidata item {qid} is already in the registry"
    used_tags = {tag for t in technologies for tag in t.stackoverflow_tags}
    used_repos = {repo.lower() for t in technologies for repo in t.github_repos}
    used_packages = {package.key for t in technologies for package in t.packages}
    used_slugs = {t.endoflife for t in technologies if t.endoflife}
    used_terms = {term for t in technologies for term in t.hn_terms}

    tags = [tag.strip().lower() for tag in candidate.stackoverflow_tags if tag.strip().lower() not in used_tags]
    existing_tags = checker.existing_tags(tags) if tags else set()
    repos = []
    for repo in candidate.github_repos:
        status, full_name = checker.repository(repo.strip())
        if status in ("ok",) and full_name and full_name.lower() not in used_repos:
            repos.append(full_name)
    packages = []
    for value in candidate.packages:
        try:
            parse_package(value.strip())
        except ValueError:
            continue
        if value.strip() not in used_packages and checker.package(value.strip()) == "ok":
            packages.append(value.strip())
    endoflife = None
    if candidate.endoflife and candidate.endoflife not in used_slugs:
        status, canonical = checker.endoflife(candidate.endoflife.strip())
        endoflife = canonical if status == "ok" and canonical not in used_slugs else None
    terms = []
    for term in candidate.hn_terms:
        term = term.strip().lower()
        if len(term) >= 3 and term not in used_terms and term not in terms:
            terms.append(term)
    entry = {
        "id": technology_id, "name": candidate.name.strip(), "category": candidate.category, "added": today,
        "wikidata": qid, "stackoverflow_tags": [tag for tag in tags if existing_tags and tag in existing_tags],
        "github_repos": repos, "packages": packages, "endoflife": endoflife, "hn_terms": terms,
    }
    return entry, candidate.reason.strip()


def propose_additions(model: "SearchModel", checker: IdentifierChecker, technologies: list[Technology],
                      today: date, result: ReviewResult) -> None:
    """Asks the search model per domain for missing technologies and adds the verified ones.

    Args:
        model: The search model.
        checker: The identifier checker.
        technologies: The current entries; added entries are appended.
        today: Day of the review.
        result: The review result; its text is changed for additions.
    """
    domains: dict[str, list[str]] = {}
    for category, domain in CATEGORY_DOMAINS.items():
        domains.setdefault(domain, []).append(category)
    for domain, categories in domains.items():
        if len(result.additions) >= MAX_ADDITIONS:
            break
        try:
            candidates = parse_candidates(model.ask(search_prompt(domain, categories, technologies)))
        except (SearchError, ValueError) as e:
            result.notes.append(f"Search for {domain} failed: {e}")
            continue
        for candidate in candidates:
            if len(result.additions) >= MAX_ADDITIONS:
                break
            entry, reason = verify_candidate(candidate, domain, checker, technologies, today)
            if entry is None:
                result.rejected.append(f"{candidate.name} ({domain}): {reason}")
                continue
            result.text = insert_entry(result.text, entry, candidate.wikipedia_title, technologies)
            technologies.append(Technology(
                id=entry["id"], name=entry["name"], category=entry["category"], added=today, wikidata=entry["wikidata"],
                stackoverflow_tags=tuple(entry["stackoverflow_tags"]), github_repos=tuple(entry["github_repos"]),
                packages=tuple(parse_package(value) for value in entry["packages"]), endoflife=entry["endoflife"],
                hn_terms=tuple(entry["hn_terms"])))
            identifiers = ", ".join(f"{key} {entry[key]}" for key in ("wikidata", "stackoverflow_tags", "github_repos",
                                                                       "packages", "endoflife", "hn_terms")
                                    if entry.get(key))
            result.additions.append(f"`{entry['id']}` ({entry['category']}): {entry['name']}; {identifiers}. "
                                    f"Reason: {reason}")
    result.search_calls = model.calls


class SearchModel:
    """Gemini with Google Search grounding, for short JSON answers.

    Attributes:
        calls (int): Requests sent in this review.
    """

    def __init__(self, api_key: str, model: str, client: Optional[Any] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        """Initializes the model.

        Args:
            api_key: Gemini API key.
            model: Gemini model id.
            client: A ``genai.Client`` or a compatible test double; created from the key if omitted.
            sleep: Function used to wait between requests; replaceable in tests.
        """
        if client is None:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=SEARCH_TIMEOUT_MS))
        self._client = client
        self._model = model
        self._sleep = sleep
        self.calls = 0

    def ask(self, prompt: str) -> str:
        """Sends a prompt with Google Search enabled.

        Args:
            prompt: The prompt.

        Returns:
            str: The answer text; empty if the model returned none.

        Raises:
            SearchError: If MAX_SEARCH_CALLS requests were sent or the request failed.
        """
        from google.genai import errors, types
        if self.calls >= MAX_SEARCH_CALLS:
            raise SearchError(f"MAX_SEARCH_CALLS ({MAX_SEARCH_CALLS}) reached")
        if self.calls:
            self._sleep(MIN_SEARCH_INTERVAL_S)
        self.calls += 1
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())], temperature=0.2,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        try:
            response = self._client.models.generate_content(model=self._model, contents=prompt, config=config)
        except errors.APIError as e:
            raise SearchError(f"Gemini request failed: {e}") from e
        except httpx.TransportError as e:
            raise SearchError(f"network error: {e!r}") from e
        return getattr(response, "text", None) or ""


def run_review(web: WebClient, settings: Settings, registry_path: Path, state_dir: Path,
               intervals: dict[str, int], model: Optional[SearchModel], today: date) -> ReviewResult:
    """Runs all review steps.

    Args:
        web: The HTTP client.
        settings: Settings with the optional tokens.
        registry_path: The registry file.
        state_dir: Folder of the pipeline state.
        intervals: ``min_days_between_runs`` per collector name.
        model: The search model; None skips the additions.
        today: Day of the review.

    Returns:
        ReviewResult: The proposed registry text and the findings.

    Raises:
        RegistryError: If the registry is invalid before the review.
    """
    technologies = load_registry(registry_path)
    result = ReviewResult(text=registry_path.read_text(encoding="utf-8"))
    checker = IdentifierChecker(web, settings)
    check_identifiers(checker, technologies, result)
    check_collector_health(state_dir, intervals, today, result)
    if model is None:
        result.notes.append("The search for new technologies was skipped.")
    else:
        propose_additions(model, checker, list(technologies), today, result)
    return result


def _section(title: str, lines: Iterable[str], empty: str) -> list[str]:
    """Builds a Markdown section with a bullet list."""
    items = list(lines)
    return [f"### {title}", "", *([f"- {line}" for line in items] or [f"- {empty}"]), ""]


def build_report(result: ReviewResult, today: date) -> str:
    """Builds the Markdown report of a review, used as the pull request description.

    Args:
        result: The review result.
        today: Day of the review.

    Returns:
        str: The report.
    """
    checked = ", ".join(f"{kind.replace('_', ' ')} {count}" for kind, count in result.checked.items()) or "none"
    lines = [f"## Registry review {today:%Y-%m}", "",
             f"- **Checked identifiers:** {checked}",
             f"- **Automatic fixes:** {len(result.fixes)}; **additions:** {len(result.additions)}; "
             f"**issues to decide by hand:** {len(result.issues)}; **collector findings:** {len(result.health)}",
             f"- **Search requests:** {result.search_calls}", ""]
    lines += _section("Automatic fixes", result.fixes, "none")
    lines += _section("Added technologies", result.additions, "none")
    lines += _section("Issues to decide by hand", result.issues, "none")
    lines += _section("Collector health", result.health, "no findings")
    lines += _section("Proposals not admitted", result.rejected, "none")
    lines += _section("Notes", result.notes, "none")
    lines.append("New identifiers were verified against their live sources on the day of the review. Review the "
                 "hn_terms of added technologies for ambiguity before merging.")
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    """Runs the review, writes the proposed registry and the report.

    Args:
        argv: Arguments without the program name; None reads ``sys.argv``.

    Returns:
        int: 0 on success, 1 if the registry is or would become invalid.
    """
    from pipeline import HOST_INTERVALS, build_collectors

    parser = argparse.ArgumentParser(description="Reviews the technology registry and the collector health.")
    parser.add_argument("--report", type=Path, required=True, help="Markdown file for the report")
    parser.add_argument("--registry", type=Path, default=CONFIG_PATH)
    parser.add_argument("--state-dir", type=Path, default=Path("data") / "state")
    parser.add_argument("--no-search", action="store_true", help="skip the search for new technologies")
    args = parser.parse_args(argv)
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = None
    if not args.no_search and api_key:
        model = SearchModel(api_key, os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL)
    today = datetime.now(timezone.utc).date()
    intervals = {collector.name: collector.min_days_between_runs for collector in build_collectors()}
    with WebClient(host_intervals={**HOST_INTERVALS, **REVIEW_HOST_INTERVALS}) as web:
        try:
            result = run_review(web, settings, args.registry, args.state_dir, intervals, model, today)
        except RegistryError as e:
            logger.error("The registry is invalid: %s", e)
            return 1
    if not args.no_search and not api_key:
        result.notes.append("GEMINI_API_KEY is not set; the search for new technologies was skipped.")
    if result.changed:
        candidate = args.registry.with_suffix(".review.tmp")
        candidate.write_text(result.text, encoding="utf-8", newline="\n")
        try:
            load_registry(candidate)
        except RegistryError as e:
            candidate.unlink()
            logger.error("The proposed registry is invalid, nothing is changed: %s", e)
            return 1
        candidate.replace(args.registry)
    args.report.write_text(build_report(result, today), encoding="utf-8")
    # In GitHub Actions the workflow opens an issue for findings that do not change the registry
    github_output = os.getenv("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"findings={len(result.issues) + len(result.health)}\nchanged={str(result.changed).lower()}\n")
    logger.info("Review: %d fixes, %d additions, %d issues, %d collector findings.", len(result.fixes),
                len(result.additions), len(result.issues), len(result.health))
    return 0


if __name__ == "__main__":
    sys.exit(main())

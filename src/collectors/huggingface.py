"""
Collects the top model and dataset repositories of organizations on the Hugging Face Hub and the size of the Hub.

Source: the Hugging Face Hub API (``https://huggingface.co/api/``), without a key. The
anonymous API allows 500 requests per 5 minutes per IP address. The Hub publishes no
explicit license for its metadata; the records carry the terms of service as license.
The total numbers of repositories come from the list pages of the Hub website, which
robots.txt allows.

Fetch strategy:
- one request each lists the 500 models and the 200 datasets with the most downloads
  in the last 30 days (``/api/models`` and ``/api/datasets``, ``sort=downloads``), with
  the needed ``expand[]`` fields; ``cardData`` is not requested because of its size;
- repository ids include user accounts. The owner of every listed repository, and of
  every base model of an organization repository, is checked once with
  ``/api/organizations/<name>/overview`` (HTTP 200 for an organization, 404 otherwise).
  The result is cached without expiry in the seen store ``huggingface_authors``
  (``OwnerClassifier``, also used by the Epoch AI collector); a user account is cached
  under a hash of its name, so that no user name is stored;
- the pages ``/models``, ``/datasets`` and ``/spaces`` contain the total number of
  public repositories (``numTotalItems``).

Records: only repositories of organizations are written. Repositories of users are
counted in the totals record; base models owned by users are left out of
``base_models``.
- ``huggingface_repo_stats``: downloads, likes and rank per organization repository,
  one snapshot per run (record_id ``hf-stats:<type>:<repo id>:<day>``); the rank counts
  all repositories of the list, including those of users, so ranks have gaps. Low
  trending scores are stored as the Hub reports them (low scores are fractions);
- ``huggingface_repos``: the attributes of an organization repository, written when it
  enters the top lists and again when an attribute changes (record_id
  ``hf-repo:<type>:<repo id>``); dropping out of the top lists is not a removal. The
  license comes from the ``license:<id>`` tags, comma-separated if there are several;
  the number of parameters from the safetensors files (missing for many repositories);
- ``huggingface_hub_totals``: one snapshot per run (record_id ``hf-totals:<day>``) with
  the totals of the Hub and the number of user repositories in the top lists.

History: the API reports only current values (downloads of the last 30 days and since
creation); the series start with the first run.

Schedule: weekly. At HTTP 429 or at the run deadline the owner checks stop; the
repositories whose owner is not yet known follow in the next run.
"""
import hashlib
import html
import logging
import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

from collectors.base import Collector, CollectorResult, RunContext, batches
from schema import HuggingFaceHubTotalsRecord, HuggingFaceRepoRecord, HuggingFaceRepoStatsRecord
from state import CollectorEntry, content_hash, is_backfilled
from web import BlockedError, FetchError, NotFoundError, RateLimitError

logger = logging.getLogger(__name__)

DATASET_REPOS = "huggingface_repos"
DATASET_STATS = "huggingface_repo_stats"
DATASET_TOTALS = "huggingface_hub_totals"
DATASETS = (DATASET_REPOS, DATASET_STATS, DATASET_TOTALS)
SOURCE_ID = "huggingface_hub"
LICENSE = "Hugging Face Hub metadata (Hugging Face terms of service)"
HUB_URL = "https://huggingface.co"
MODELS_API_URL = "https://huggingface.co/api/models"
DATASETS_API_URL = "https://huggingface.co/api/datasets"
ORGANIZATION_API_URL = "https://huggingface.co/api/organizations/{name}/overview"
REPO_PAGE_URLS = {"model": "https://huggingface.co/{repo_id}", "dataset": "https://huggingface.co/datasets/{repo_id}"}
# Website list pages with the total number of public repositories of each type
TOTALS_PAGE_URLS = {"models": "https://huggingface.co/models", "datasets": "https://huggingface.co/datasets",
                    "spaces": "https://huggingface.co/spaces"}
# Size of the top lists; the API returns at most 1,000 repositories per request
TOP_MODELS = 500
TOP_DATASETS = 200
MODEL_FIELDS = ("author", "downloads", "downloadsAllTime", "likes", "trendingScore", "pipeline_tag", "library_name",
                "createdAt", "safetensors", "gated", "tags", "baseModels")
DATASET_FIELDS = ("author", "downloads", "downloadsAllTime", "likes", "trendingScore", "createdAt", "gated", "tags")
# Seen store of the checked repository owners; not a dataset
AUTHORS_STORE = "huggingface_authors"
ORGANIZATION = "organization"
USER = "user"
# A list of 500 repositories with expanded fields takes several seconds to build
LIST_TIMEOUT_S = 90.0
EMIT_BATCH_SIZE = 250
# The total in the data-props JSON of the list (HTML-escaped in the attribute) or in a deferred script value
TOTAL_ITEMS_PATTERNS = (
    re.compile(r'"numTotalItems"\s*:\s*(\d+)'),
    re.compile(r'__hf_deferred\[\s*["\']numTotalItems["\']\s*\]\s*=\s*(\d+)'),
)


@dataclass(frozen=True)
class HubRepo:
    """A repository of a top list of the Hub API.

    Attributes:
        repo_type (str): model or dataset.
        repo_id (str): Repository id (owner/name).
        author (Optional[str]): Owner of the repository (organization or user).
        rank (int): 1-based position in the list.
        downloads_30d (Optional[int]): Downloads in the last 30 days.
        downloads_all_time (Optional[int]): Downloads since the creation of the repository.
        likes (Optional[int]): Number of likes.
        trending_score (Optional[float]): Trending score of the Hub.
        created_at (Optional[datetime]): Creation time (UTC).
        pipeline_tag (Optional[str]): Task of a model.
        library_name (Optional[str]): Library of a model.
        repo_license (Optional[str]): License tags, comma-separated if there are several.
        base_models (tuple[str, ...]): Ids of the base models, including those of users.
        base_model_relation (Optional[str]): Relation to the base models, e.g. finetune.
        parameters (Optional[int]): Number of parameters from the safetensors files.
        gated (Optional[str]): Access gating: false, auto or manual.
    """
    repo_type: str
    repo_id: str
    author: Optional[str]
    rank: int
    downloads_30d: Optional[int]
    downloads_all_time: Optional[int]
    likes: Optional[int]
    trending_score: Optional[float]
    created_at: Optional[datetime]
    pipeline_tag: Optional[str]
    library_name: Optional[str]
    repo_license: Optional[str]
    base_models: tuple[str, ...]
    base_model_relation: Optional[str]
    parameters: Optional[int]
    gated: Optional[str]


def owner_of(repo_id: str) -> Optional[str]:
    """Returns the owner part of a repository id.

    Args:
        repo_id: Repository id, e.g. ``Qwen/Qwen3-0.6B``.

    Returns:
        Optional[str]: The owner, or None for an id without owner (e.g. legacy ids such as ``gpt2``).
    """
    owner, separator, _name = repo_id.partition("/")
    return owner if separator and owner else None


def _int_or_none(value: Any) -> Optional[int]:
    """Returns an integer value, also from a float without fraction; None for other values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _number_or_none(value: Any) -> Optional[float]:
    """Returns a finite number as float; None for missing, boolean and non-finite values.

    The Hub reports low trending scores as fractions (e.g. 0.34).
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _text_or_none(value: Any) -> Optional[str]:
    """Returns a non-empty string without surrounding whitespace, or None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_time(value: Any) -> Optional[datetime]:
    """Parses an ISO 8601 timestamp of the API as UTC time; None if it is missing or invalid."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _gated(value: Any) -> Optional[str]:
    """Returns the gating as a string: false, auto or manual."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return _text_or_none(value)


def _license(tags: Any) -> Optional[str]:
    """Returns the license tags (``license:<id>``) of a repository, comma-separated if there are several."""
    if not isinstance(tags, list):
        return None
    licenses = [tag.split(":", 1)[1].strip() for tag in tags if isinstance(tag, str) and tag.startswith("license:")]
    return ",".join(dict.fromkeys(name for name in licenses if name)) or None


def _base_models(value: Any) -> tuple[tuple[str, ...], Optional[str]]:
    """Returns the base model ids and the relation from the ``baseModels`` field."""
    if not isinstance(value, dict):
        return (), None
    models = value.get("models")
    ids: list[str] = []
    if isinstance(models, list):
        ids = [model["id"].strip() for model in models
               if isinstance(model, dict) and isinstance(model.get("id"), str) and model["id"].strip()]
    return tuple(dict.fromkeys(ids)), _text_or_none(value.get("relation"))


def parse_repos(items: Any, repo_type: str) -> list[HubRepo]:
    """Parses a top list of the Hub API.

    Args:
        items: The decoded JSON response.
        repo_type: model or dataset.

    Returns:
        list[HubRepo]: The repositories in list order; a repeated id keeps its first position.

    Raises:
        ValueError: If the response is not a list.
    """
    if not isinstance(items, list):
        raise ValueError(f"The Hub API returned no list of {repo_type} repositories")
    repos: list[HubRepo] = []
    listed: set[str] = set()
    for rank, item in enumerate(items, start=1):
        repo_id = _text_or_none(item.get("id")) if isinstance(item, dict) else None
        if repo_id is None or repo_id in listed:
            continue
        listed.add(repo_id)
        safetensors = item.get("safetensors")
        base_models, relation = _base_models(item.get("baseModels"))
        repos.append(HubRepo(
            repo_type=repo_type,
            repo_id=repo_id,
            author=_text_or_none(item.get("author")) or owner_of(repo_id),
            rank=rank,
            downloads_30d=_int_or_none(item.get("downloads")),
            downloads_all_time=_int_or_none(item.get("downloadsAllTime")),
            likes=_int_or_none(item.get("likes")),
            trending_score=_number_or_none(item.get("trendingScore")),
            created_at=_parse_time(item.get("createdAt")),
            pipeline_tag=_text_or_none(item.get("pipeline_tag")),
            library_name=_text_or_none(item.get("library_name")),
            repo_license=_license(item.get("tags")),
            base_models=base_models,
            base_model_relation=relation,
            parameters=_int_or_none(safetensors.get("total")) if isinstance(safetensors, dict) else None,
            gated=_gated(item.get("gated")),
        ))
    return repos


def fetch_top_repos(ctx: RunContext, url: str, repo_type: str, limit: int, fields: Sequence[str]) -> list[HubRepo]:
    """Fetches a top list sorted by downloads in the last 30 days.

    Args:
        ctx: The run context.
        url: List endpoint of the API.
        repo_type: model or dataset.
        limit: Number of repositories (at most 1,000).
        fields: Values of the repeated ``expand[]`` parameter.

    Returns:
        list[HubRepo]: The repositories in list order.

    Raises:
        RateLimitError: If the API rate limit is reached.
        FetchError: If the request fails.
        ValueError: If the response is not a list.
    """
    params = {"sort": "downloads", "direction": "-1", "limit": str(limit), "expand[]": list(fields)}
    response = ctx.web.get(url, params=params, respect_robots=False, timeout_s=LIST_TIMEOUT_S)
    return parse_repos(response.json(), repo_type)


def parse_total_items(page: str) -> Optional[int]:
    """Reads the total number of repositories from a list page of the Hub website.

    The models and datasets pages contain ``"numTotalItems":<N>`` in the HTML-escaped
    ``data-props`` attribute; the Spaces page sets ``numTotalItems`` to null there and
    assigns the number in a script (``window.__hf_deferred["numTotalItems"] = <N>``).

    Args:
        page: HTML of the page.

    Returns:
        Optional[int]: The total, or None if the page contains none.
    """
    text = html.unescape(page)
    for pattern in TOTAL_ITEMS_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def fetch_hub_totals(ctx: RunContext) -> dict[str, Optional[int]]:
    """Fetches the total numbers of models, datasets and Spaces from the list pages of the Hub website.

    A page that is not available, reserves text and data mining or contains no total
    gives None; a rate limit stops the remaining page requests.

    Args:
        ctx: The run context.

    Returns:
        dict[str, Optional[int]]: Total per kind (models, datasets, spaces).
    """
    totals: dict[str, Optional[int]] = {kind: None for kind in TOTALS_PAGE_URLS}
    for kind, url in TOTALS_PAGE_URLS.items():
        if ctx.past_deadline():
            logger.warning("Run deadline reached; the remaining Hub totals are not fetched.")
            break
        try:
            response = ctx.web.get(url)
        except RateLimitError as e:
            logger.warning("Hub website rate limit reached; the remaining totals are not fetched: %s", e)
            break
        except FetchError as e:
            logger.warning("Hub list page %s is not available: %s", url, e)
            continue
        if ctx.web.tdm_reserved(url, response):
            logger.warning("Text and data mining is reserved for %s; the total is not stored.", url)
            continue
        totals[kind] = parse_total_items(response.text)
        if totals[kind] is None:
            logger.warning("No total number of items found in %s.", url)
    return totals


def _user_key(name: str) -> str:
    """Returns the cache key of a user account: a hash, so that the user name is not stored."""
    return "user:" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


class OwnerClassifier:
    """Tells whether Hub accounts are organizations or users, with a cache in the seen store ``huggingface_authors``.

    An organization is cached under its name, a user account under a hash of its name;
    the hash of the entry is ``organization`` or ``user``. Entries do not expire.

    Attributes:
        requests (int): Overview requests sent in this run.
        failed (int): Checks that failed with an error other than a rate limit or a refusal.
        stopped (Optional[str]): Why no more requests are sent in this run: rate_limit,
            deadline or blocked; None while checks can continue.
    """

    def __init__(self, ctx: RunContext) -> None:
        """Initializes the classifier.

        Args:
            ctx: The run context.
        """
        self._ctx = ctx
        self._store = ctx.seen(AUTHORS_STORE)
        self._failed_names: set[str] = set()
        self.requests = 0
        self.failed = 0
        self.stopped: Optional[str] = None

    def cached(self, name: str) -> Optional[str]:
        """Returns the cached kind of an account without sending a request.

        Args:
            name: Account name.

        Returns:
            Optional[str]: organization, user, or None if the account was not checked yet.
        """
        entry = self._store.get(name) or self._store.get(_user_key(name))
        return entry.hash if entry else None

    def classify(self, name: str) -> Optional[str]:
        """Returns the kind of an account, checking it with the API if it is not cached.

        Args:
            name: Account name.

        Returns:
            Optional[str]: organization or user; None if the check failed or no more
            requests are sent in this run.
        """
        kind = self.cached(name)
        if kind is not None or self.stopped or name in self._failed_names:
            return kind
        if self._ctx.past_deadline():
            self.stopped = "deadline"
            return None
        self.requests += 1
        try:
            self._ctx.web.get(ORGANIZATION_API_URL.format(name=quote(name, safe="")), respect_robots=False)
            kind = ORGANIZATION
        except NotFoundError:
            kind = USER
        except RateLimitError as e:
            logger.warning("Hugging Face API rate limit reached after %d owner checks (%s); the remaining owners "
                           "follow in the next run.", self.requests, e.reason)
            self.stopped = "rate_limit"
            return None
        except BlockedError as e:
            logger.error("Hugging Face API refused an owner check (%s); no further checks in this run.", e.reason)
            self.stopped = "blocked"
            return None
        except FetchError as e:
            # The URL is not logged, because it may contain the name of a user
            logger.warning("Owner check failed (%s).", e.reason)
            self.failed += 1
            self._failed_names.add(name)
            return None
        self._store.mark(name if kind == ORGANIZATION else _user_key(name), kind, self._ctx.today)
        return kind

    def save(self) -> None:
        """Saves the cache now (not in dry-run mode), so that an interrupted run keeps the checked owners."""
        if not self._ctx.dry_run:
            self._store.save()


def _repo_url(repo: HubRepo) -> str:
    """Returns the URL of the repository page."""
    return REPO_PAGE_URLS[repo.repo_type].format(repo_id=repo.repo_id)


def _base_model_owners(repo: HubRepo) -> list[str]:
    """Returns the owners of the base models of a repository."""
    return [owner for owner in (owner_of(model) for model in repo.base_models) if owner]


def _organization_base_models(repo: HubRepo, kinds: dict[str, Optional[str]]) -> Optional[list[str]]:
    """Returns the base models of a repository without those owned by users.

    Args:
        repo: The repository.
        kinds: Kind of each checked owner (organization, user, or None if unknown).

    Returns:
        Optional[list[str]]: The base models of organizations and base models without owner
        (legacy ids); None if the owner of a base model is unknown.
    """
    base_models: list[str] = []
    for model in repo.base_models:
        owner = owner_of(model)
        kind = kinds.get(owner) if owner else ORGANIZATION
        if kind is None:
            return None
        if kind == ORGANIZATION:
            base_models.append(model)
    return base_models


def _user_repo_count(repos: Iterable[HubRepo], kinds: dict[str, Optional[str]]) -> Optional[int]:
    """Returns the number of repositories of users; None if the owner of a repository is unknown."""
    owner_kinds = [kinds.get(repo.author) for repo in repos if repo.author]
    if any(kind is None for kind in owner_kinds):
        return None
    return sum(kind == USER for kind in owner_kinds)


class HuggingFaceCollector(Collector):
    """Writes the top repositories of organizations on the Hugging Face Hub and the totals of the Hub."""
    name = "huggingface"
    datasets = DATASETS
    min_days_between_runs = 6

    def __init__(self, top_models: int = TOP_MODELS, top_datasets: int = TOP_DATASETS) -> None:
        """Initializes the collector.

        Args:
            top_models: Number of models in the top list.
            top_datasets: Number of datasets in the top list.
        """
        self._top_models = top_models
        self._top_datasets = top_datasets

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Reads the top lists, checks the owners and emits the stats, the changed attributes and the totals.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; incomplete if the owner checks stopped at a rate limit or the deadline.
        """
        try:
            lists = {
                "model": fetch_top_repos(ctx, MODELS_API_URL, "model", self._top_models, MODEL_FIELDS),
                "dataset": fetch_top_repos(ctx, DATASETS_API_URL, "dataset", self._top_datasets, DATASET_FIELDS),
            }
        except RateLimitError as e:
            logger.warning("Hugging Face API rate limit reached before the top lists were read: %s", e)
            return CollectorResult(counters={"rate_limited": 1}, complete=False)
        repos = lists["model"] + lists["dataset"]
        classifier = OwnerClassifier(ctx)
        kinds: dict[str, Optional[str]] = {}
        for repo in repos:
            if repo.author and repo.author not in kinds:
                kinds[repo.author] = classifier.classify(repo.author)
        org_repos = [repo for repo in repos if repo.author and kinds[repo.author] == ORGANIZATION]
        for repo in org_repos:
            for owner in _base_model_owners(repo):
                if owner not in kinds:
                    kinds[owner] = classifier.classify(owner)
        classifier.save()

        owner_counts = Counter(kinds.get(repo.author) for repo in repos if repo.author)
        counters: dict[str, Any] = {
            "top_models": len(lists["model"]), "top_datasets": len(lists["dataset"]),
            "organization_repos": owner_counts[ORGANIZATION], "user_repos": owner_counts[USER],
            "unknown_owner_repos": owner_counts[None], "owner_checks": classifier.requests,
            "owner_checks_failed": classifier.failed,
        }
        counters["stats_records"] = self._emit_stats(ctx, org_repos)
        counters.update(self._emit_repos(ctx, entry, org_repos, kinds))
        totals = fetch_hub_totals(ctx)
        counters["totals_found"] = sum(value is not None for value in totals.values())
        values = {
            "models": totals["models"], "datasets": totals["datasets"], "spaces": totals["spaces"],
            "user_repos_in_top_models": _user_repo_count(lists["model"], kinds),
            "user_repos_in_top_datasets": _user_repo_count(lists["dataset"], kinds),
        }
        # A record without any known value adds nothing to the series
        if any(value is not None for value in values.values()):
            ctx.emit(DATASET_TOTALS, [HuggingFaceHubTotalsRecord(
                **ctx.envelope(DATASET_TOTALS, f"hf-totals:{ctx.today.isoformat()}", revision=0, source_id=SOURCE_ID,
                               source_url=HUB_URL, license=LICENSE, backfilled=False),
                **values,
            )])
        error = None
        if classifier.stopped:
            counters["stopped"] = classifier.stopped
            if classifier.stopped == "blocked":
                error = "blocked: the Hugging Face API refused the owner checks"
        logger.info("Hugging Face: %d stats records, %d new and %d changed repositories, %d owner checks.",
                    counters["stats_records"], counters["repos_new"], counters["repos_changed"], classifier.requests)
        return CollectorResult(counters=counters, complete=classifier.stopped is None, error=error)

    @staticmethod
    def _emit_stats(ctx: RunContext, repos: Sequence[HubRepo]) -> int:
        """Emits one snapshot of the counts per organization repository.

        Args:
            ctx: The run context.
            repos: The organization repositories of the top lists.

        Returns:
            int: Number of emitted records.
        """
        records = [
            HuggingFaceRepoStatsRecord(
                **ctx.envelope(DATASET_STATS, f"hf-stats:{repo.repo_type}:{repo.repo_id}:{ctx.today.isoformat()}",
                               revision=0, source_id=SOURCE_ID, source_url=_repo_url(repo), license=LICENSE,
                               backfilled=False),
                repo_type=repo.repo_type,
                repo_id=repo.repo_id,
                rank=repo.rank,
                downloads_30d=repo.downloads_30d,
                downloads_all_time=repo.downloads_all_time,
                likes=repo.likes,
                trending_score=repo.trending_score,
            )
            for repo in repos if repo.downloads_30d is not None
        ]
        for batch in batches(records, EMIT_BATCH_SIZE):
            ctx.emit(DATASET_STATS, batch)
        return len(records)

    @staticmethod
    def _emit_repos(ctx: RunContext, entry: CollectorEntry, repos: Sequence[HubRepo],
                    kinds: dict[str, Optional[str]]) -> dict[str, int]:
        """Emits the attributes of the organization repositories that are new or changed.

        A repository whose base model owners are not all known is deferred to the next
        run; base models owned by users are left out.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            repos: The organization repositories of the top lists.
            kinds: Kind of each checked owner (organization, user, or None if unknown).

        Returns:
            dict[str, int]: Counters.
        """
        seen = ctx.seen(DATASET_REPOS)
        counters = {"repos_new": 0, "repos_changed": 0, "repos_deferred": 0, "user_base_models_left_out": 0}
        records: list[HuggingFaceRepoRecord] = []
        for repo in repos:
            base_models = _organization_base_models(repo, kinds)
            if base_models is None:
                counters["repos_deferred"] += 1
                continue
            counters["user_base_models_left_out"] += len(repo.base_models) - len(base_models)
            fields = {
                "organization": repo.author, "created_at": repo.created_at, "pipeline_tag": repo.pipeline_tag,
                "library_name": repo.library_name, "repo_license": repo.repo_license, "base_models": base_models,
                "base_model_relation": repo.base_model_relation, "parameters": repo.parameters, "gated": repo.gated,
            }
            key = f"{repo.repo_type}:{repo.repo_id}"
            item_hash = content_hash(fields)
            status = seen.classify(key, item_hash)
            if status == "unchanged":
                continue
            counters[f"repos_{status}"] += 1
            revision = seen.mark(key, item_hash, ctx.today)
            created = repo.created_at.date() if repo.created_at else None
            records.append(HuggingFaceRepoRecord(
                **ctx.envelope(DATASET_REPOS, f"hf-repo:{key}", revision=revision, source_id=SOURCE_ID,
                               source_url=_repo_url(repo), license=LICENSE,
                               backfilled=is_backfilled(created, entry.first_run)),
                repo_type=repo.repo_type,
                repo_id=repo.repo_id,
                **fields,
            ))
            if len(records) >= EMIT_BATCH_SIZE:
                ctx.emit(DATASET_REPOS, records)
                records = []
        ctx.emit(DATASET_REPOS, records)
        return counters

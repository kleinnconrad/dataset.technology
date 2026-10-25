"""
Collects the cumulative pulls and stars of the container images of the tracked technologies from Docker Hub.

Source: the Docker Hub API v2, endpoint
``/v2/namespaces/<namespace>/repositories/<repository>``, called with
``respect_robots=False``. The Docker terms allow automated access through the
documented APIs within the published rate limits (about 180 requests per
minute). The response contains the pulls since the image was created, the
stars and the time of the latest push. Response headers are not stored
(``x-ratelimit-ip`` contains the IP address of the caller).

Fetch strategy: Docker Hub keeps no history of the counts, so the dataset is a
snapshot dataset: every run writes one record per image with the current
values. The pulls of a period follow from the difference between two
snapshots.

History: none; the series starts with the first run.

Schedule: once a week (``min_days_between_runs = 6``). At the run deadline or
a rate limit the collector stops; the remaining images follow in the next run.
"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from collectors.base import Collector, CollectorResult, RunContext
from registry import CONFIG_PATH, active_technologies
from schema import DockerImageRecord
from state import CollectorEntry, is_backfilled
from web import BlockedError, FetchError, NotFoundError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "docker_image_pulls"
SOURCE_ID = "docker_hub"
LICENSE = "Docker Hub API (Docker terms of service)"
REPOSITORY_URL = "https://hub.docker.com/v2/namespaces/{namespace}/repositories/{repository}"
# Records are written in batches, so that an interrupted run keeps its progress
EMIT_BATCH_SIZE = 100


def repository_url(image: str) -> str:
    """Builds the API URL of an image.

    Args:
        image: Image as ``namespace/repository``, e.g. ``library/postgres``.

    Returns:
        str: The URL.
    """
    namespace, _, repository = image.partition("/")
    return REPOSITORY_URL.format(namespace=quote(namespace, safe=""), repository=quote(repository, safe=""))


def _parse_time(value: Any) -> Optional[datetime]:
    """Parses an ISO 8601 time as UTC; returns None for missing or invalid values."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Invalid time %r in a Docker Hub response.", value)
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def parse_repository(data: Any) -> dict[str, Any]:
    """Extracts the counts of an image from a repository response.

    Args:
        data: The decoded JSON response.

    Returns:
        dict: ``pull_count``, ``star_count`` and ``last_updated`` as fields of a ``DockerImageRecord``.

    Raises:
        ValueError: If the response has no pull count.
    """
    if not isinstance(data, dict):
        raise ValueError("Docker Hub response is not an object")
    pulls = data.get("pull_count")
    if not isinstance(pulls, int) or isinstance(pulls, bool) or pulls < 0:
        raise ValueError(f"Docker Hub response without a valid pull_count: {pulls!r}")
    stars = data.get("star_count")
    return {
        "pull_count": pulls,
        "star_count": stars if isinstance(stars, int) and not isinstance(stars, bool) else None,
        "last_updated": _parse_time(data.get("last_updated")),
    }


def fetch_repository(ctx: RunContext, image: str) -> dict[str, Any]:
    """Fetches the counts of an image.

    Args:
        ctx: The run context.
        image: Image as ``namespace/repository``.

    Returns:
        dict: ``pull_count``, ``star_count`` and ``last_updated``.

    Raises:
        NotFoundError: If the repository does not exist.
        FetchError: If the request fails.
        ValueError: If the response cannot be parsed.
    """
    return parse_repository(ctx.web.get(repository_url(image), respect_robots=False).json())


class DockerHubCollector(Collector):
    """Writes a snapshot of the pulls and stars of the container images of the tracked technologies."""
    name = "docker_hub"
    datasets = (DATASET,)
    min_days_between_runs = 6

    def __init__(self, registry_path: Path = CONFIG_PATH) -> None:
        """Initializes the collector.

        Args:
            registry_path: The registry file.
        """
        self._registry_path = registry_path

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Fetches the current counts of every image and emits one record per image.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters.
        """
        images = [(technology, package) for technology in active_technologies(self._registry_path)
                  for package in technology.packages if package.ecosystem == "docker"]
        records: list[DockerImageRecord] = []
        counters: dict[str, Any] = {"images": len(images), "requested": 0, "records": 0, "failed": 0, "deferred": 0}
        complete = True
        error: Optional[str] = None
        for position, (technology, package) in enumerate(images):
            if ctx.past_deadline():
                logger.warning("Docker Hub: run deadline reached; the remaining images follow in the next run.")
                counters["deferred"] = len(images) - position
                complete = False
                break
            try:
                values = fetch_repository(ctx, package.name)
            except RateLimitError as e:
                logger.warning("Docker Hub is rate limited (%s); the remaining images follow in the next run.", e)
                counters["deferred"] = len(images) - position
                complete = False
                break
            except BlockedError as e:
                logger.error("Docker Hub refuses automated requests (%s); it is not requested again in this run.", e)
                counters["requested"] += 1
                counters["failed"] += 1
                counters["deferred"] = len(images) - position - 1
                error = f"Docker Hub refuses automated requests: {e}"
                break
            except NotFoundError as e:
                logger.warning("Docker image %s of %s not found: %s", package.name, technology.id, e)
                counters["requested"] += 1
                counters["failed"] += 1
                continue
            except (FetchError, ValueError) as e:
                logger.warning("Docker image %s failed: %s", package.name, e)
                counters["requested"] += 1
                counters["failed"] += 1
                continue
            counters["requested"] += 1
            records.append(DockerImageRecord(
                **ctx.envelope(
                    DATASET, f"docker:{package.name}:{ctx.today.isoformat()}", revision=0, source_id=SOURCE_ID,
                    source_url=repository_url(package.name), license=LICENSE,
                    backfilled=is_backfilled(ctx.today, entry.first_run),
                ),
                technology_id=technology.id,
                image=package.name,
                **values,
            ))
            if len(records) >= EMIT_BATCH_SIZE:
                ctx.emit(DATASET, records)
                counters["records"] += len(records)
                records = []
        ctx.emit(DATASET, records)
        counters["records"] += len(records)
        logger.info("Docker Hub: %d records of %d images, %d failed.", counters["records"], len(images),
                    counters["failed"])
        failed = counters["failed"]
        if error is None and failed and failed == counters["requested"]:
            error = f"{failed} of {counters['requested']} images failed"
        return CollectorResult(counters=counters, complete=complete, error=error)

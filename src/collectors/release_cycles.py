"""
Collects the release cycles of the tracked technologies from endoflife.date.

Source: the endoflife.date API v1, endpoint ``/api/v1/products/<slug>``, called
with ``respect_robots=False``; the MIT license of the data is stated in the API
specification. A product page lists all release cycles of the product with
their release date, long-term support, end of active support, end of life,
end of extended support, maintenance status and latest release. Aliases of a
product answer with a permanent redirect to the canonical slug, which the
client follows; unknown products return a 404 page.

Fetch strategy: the dataset is a change dataset. Every run fetches the product
page of each technology with an endoflife.date slug. A cycle is written when it
appears and again, with the next revision, when one of its fields changes; the
status fields (``is_eol``, ``is_maintained``) change when their dates pass. A
cycle that disappears from the list of its product is written once with
``is_listed`` false and null values in the other fields (its last values are in
the previous revision); if it reappears, it is written again with ``is_listed``
true. Records are keyed by the product slug of the registry.

History: the first run writes every cycle that the source lists, including
cycles that ended before the start of collection (marked as backfilled by their
release date).

Schedule: once a week (``min_days_between_runs = 6``). At the run deadline or a
rate limit the collector stops; the remaining products follow in the next run.
"""
import logging
from collections import defaultdict
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from collectors.base import Collector, CollectorResult, RunContext
from registry import CONFIG_PATH, Technology, active_technologies
from schema import ReleaseCycleRecord
from state import CollectorEntry, SeenStore, content_hash, is_backfilled
from web import BlockedError, FetchError, NotFoundError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "release_cycles"
SOURCE_ID = "endoflife_date"
LICENSE = "MIT (endoflife.date)"
PRODUCT_URL = "https://endoflife.date/api/v1/products/{product}"
# Fields of a ReleaseCycleRecord that are null in the record of a cycle that is no longer listed
CYCLE_VALUE_FIELDS = ("codename", "release_date", "is_lts", "lts_from", "active_support_until", "eol_date", "is_eol",
                      "extended_support_until", "is_maintained", "latest_version", "latest_release_date")
# Records are written in batches, so that an interrupted run keeps its progress
EMIT_BATCH_SIZE = 100


def product_url(product: str) -> str:
    """Builds the API URL of a product.

    Args:
        product: Product slug at endoflife.date.

    Returns:
        str: The URL.
    """
    return PRODUCT_URL.format(product=quote(product, safe=""))


def _date(value: Any) -> Optional[date]:
    """Parses a ``YYYY-MM-DD`` string; returns None for null and logs invalid values."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    logger.warning("Invalid date %r in an endoflife.date response.", value)
    return None


def _bool(value: Any) -> Optional[bool]:
    """Returns a boolean value; None for null and other types."""
    return value if isinstance(value, bool) else None


def _text(value: Any) -> Optional[str]:
    """Returns a non-empty string; None for null, empty strings and other types."""
    return value if isinstance(value, str) and value.strip() else None


def parse_release(release: Mapping[str, Any]) -> dict[str, Any]:
    """Maps a release of the API to the cycle fields of a ``ReleaseCycleRecord``.

    Args:
        release: One element of ``result.releases``.

    Returns:
        dict: ``cycle`` and the fields of CYCLE_VALUE_FIELDS.

    Raises:
        ValueError: If the release has no name.
    """
    name = release.get("name")
    if isinstance(name, int) and not isinstance(name, bool):
        name = str(name)
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"release without a name: {name!r}")
    latest = release.get("latest")
    latest = latest if isinstance(latest, dict) else {}
    return {
        "cycle": name,
        "codename": _text(release.get("codename")),
        "release_date": _date(release.get("releaseDate")),
        "is_lts": _bool(release.get("isLts")),
        "lts_from": _date(release.get("ltsFrom")),
        "active_support_until": _date(release.get("eoasFrom")),
        "eol_date": _date(release.get("eolFrom")),
        "is_eol": _bool(release.get("isEol")),
        "extended_support_until": _date(release.get("eoesFrom")),
        "is_maintained": _bool(release.get("isMaintained")),
        "latest_version": _text(latest.get("name")),
        "latest_release_date": _date(latest.get("date")),
    }


def parse_product(data: Any) -> tuple[Optional[str], list[dict[str, Any]]]:
    """Parses a product page.

    Args:
        data: The decoded JSON response.

    Returns:
        tuple: The canonical product slug (``result.name``) and the cycle fields of
        every release, in the order of the source; a repeated cycle name is kept once.

    Raises:
        ValueError: If the response has no releases, so that it cannot serve as the
            complete list of the cycles of the product, or a release has no name.
    """
    result = data.get("result") if isinstance(data, dict) else None
    releases = result.get("releases") if isinstance(result, dict) else None
    if not isinstance(releases, list) or not releases:
        raise ValueError("endoflife.date response without releases")
    cycles: dict[str, dict[str, Any]] = {}
    for release in releases:
        if not isinstance(release, dict):
            raise ValueError(f"release is not an object: {release!r}")
        fields = parse_release(release)
        if fields["cycle"] in cycles:
            logger.warning("Cycle %s is listed twice; the first entry is kept.", fields["cycle"])
            continue
        cycles[fields["cycle"]] = fields
    return _text(result.get("name")), list(cycles.values())


def unlisted_fields(cycle: str) -> dict[str, Any]:
    """Returns the cycle fields of the record of a cycle that is no longer listed.

    Args:
        cycle: Name of the cycle.

    Returns:
        dict: ``cycle`` and null values for the fields of CYCLE_VALUE_FIELDS.
    """
    return {"cycle": cycle, **{name: None for name in CYCLE_VALUE_FIELDS}}


class ReleaseCyclesCollector(Collector):
    """Writes new and changed release cycles of the products of the tracked technologies."""
    name = "release_cycles"
    datasets = (DATASET,)
    min_days_between_runs = 6

    def __init__(self, registry_path: Path = CONFIG_PATH) -> None:
        """Initializes the collector.

        Args:
            registry_path: The registry file.
        """
        self._registry_path = registry_path

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Fetches the product page of every technology and emits the new, changed and unlisted cycles.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters.
        """
        technologies = [technology for technology in active_technologies(self._registry_path) if technology.endoflife]
        seen = ctx.seen(DATASET)
        known: dict[str, set[str]] = defaultdict(set)
        for key in seen.keys():
            product, _, cycle = key.partition(":")
            known[product].add(cycle)
        records: list[ReleaseCycleRecord] = []
        counters: dict[str, Any] = {"products": len(technologies), "requested": 0, "cycles": 0, "new": 0,
                                    "changed": 0, "unlisted": 0, "records": 0, "failed": 0, "moved": 0,
                                    "deferred": 0}
        complete = True
        error: Optional[str] = None
        for position, technology in enumerate(technologies):
            product = technology.endoflife or ""
            if ctx.past_deadline():
                logger.warning("Release cycles: run deadline reached; the remaining products follow in the next run.")
                counters["deferred"] = len(technologies) - position
                complete = False
                break
            try:
                response = ctx.web.get(product_url(product), respect_robots=False)
                canonical, cycles = parse_product(response.json())
            except RateLimitError as e:
                logger.warning("endoflife.date is rate limited (%s); the remaining products follow in the next run.",
                               e)
                counters["deferred"] = len(technologies) - position
                complete = False
                break
            except BlockedError as e:
                logger.error("endoflife.date refuses automated requests (%s); it is not requested again in this run.",
                             e)
                counters["requested"] += 1
                counters["failed"] += 1
                counters["deferred"] = len(technologies) - position - 1
                error = f"endoflife.date refuses automated requests: {e}"
                break
            except NotFoundError as e:
                logger.warning("Product %s of %s not found at endoflife.date: %s", product, technology.id, e)
                counters["requested"] += 1
                counters["failed"] += 1
                continue
            except (FetchError, ValueError) as e:
                logger.warning("Product %s of %s failed: %s", product, technology.id, e)
                counters["requested"] += 1
                counters["failed"] += 1
                continue
            counters["requested"] += 1
            if canonical and canonical != product:
                logger.warning("endoflife.date product %s of %s is now %s; the registry should be updated.",
                               product, technology.id, canonical)
                counters["moved"] += 1
            counters["cycles"] += len(cycles)
            listed = {fields["cycle"] for fields in cycles}
            observed = [(fields, True) for fields in cycles]
            # The product page lists all cycles of the product, so a known cycle that is missing was removed
            observed += [(unlisted_fields(cycle), False) for cycle in sorted(known[product] - listed)]
            for cycle_fields, is_listed in observed:
                record = self._observe(ctx, entry, seen, technology, response.url, cycle_fields, is_listed, counters)
                if record is not None:
                    records.append(record)
            if len(records) >= EMIT_BATCH_SIZE:
                ctx.emit(DATASET, records)
                counters["records"] += len(records)
                records = []
        ctx.emit(DATASET, records)
        counters["records"] += len(records)
        logger.info("Release cycles: %d new, %d changed, %d unlisted cycles of %d products; %d products failed.",
                    counters["new"], counters["changed"], counters["unlisted"], counters["requested"],
                    counters["failed"])
        failed = counters["failed"]
        if error is None and failed and failed == counters["requested"]:
            error = f"{failed} of {counters['requested']} products failed"
        return CollectorResult(counters=counters, complete=complete, error=error)

    @staticmethod
    def _observe(ctx: RunContext, entry: CollectorEntry, seen: SeenStore, technology: Technology, source_url: str,
                 cycle_fields: dict[str, Any], is_listed: bool, counters: dict[str, Any]) -> Optional[ReleaseCycleRecord]:
        """Compares a cycle with the seen store and builds its record if it is new or changed.

        Args:
            ctx: The run context.
            entry: Run history of the collector.
            seen: The seen store of the dataset.
            technology: Technology of the product.
            source_url: URL of the product page.
            cycle_fields: ``cycle`` and the fields of CYCLE_VALUE_FIELDS.
            is_listed: False for a cycle that is no longer listed on the product page.
            counters: Counters of the run; new, changed and unlisted are updated.

        Returns:
            Optional[ReleaseCycleRecord]: The record, or None if the cycle is unchanged.
        """
        product = technology.endoflife or ""
        fields = {"technology_id": technology.id, "product": product, **cycle_fields, "is_listed": is_listed}
        key = f"{product}:{cycle_fields['cycle']}"
        item_hash = content_hash(fields)
        status = seen.classify(key, item_hash)
        if status == "unchanged":
            return None
        counters[status if is_listed else "unlisted"] += 1
        revision = seen.mark(key, item_hash, ctx.today, data={} if is_listed else {"is_listed": False})
        return ReleaseCycleRecord(
            **ctx.envelope(
                DATASET, f"eol:{key}", revision=revision, source_id=SOURCE_ID, source_url=source_url,
                license=LICENSE, backfilled=is_backfilled(fields["release_date"], entry.first_run),
            ),
            **fields,
        )

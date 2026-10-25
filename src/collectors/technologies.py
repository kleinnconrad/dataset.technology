"""
Writes the entries of the technology registry to the ``technologies`` dataset.

The registry (``config/technologies.toml``) is the slowly changing dimension
of the dataset: every entry is written when it appears and again, with the
next revision, when one of its fields changes, so that the identifiers that
were collected for a technology at time t can be reconstructed from the data
alone. The collector sends no requests.
"""
import logging
from pathlib import Path
from typing import Any

from collectors.base import Collector, CollectorResult, RunContext
from registry import CONFIG_PATH, Technology, load_registry
from schema import TechnologyRecord
from state import CollectorEntry, content_hash

logger = logging.getLogger(__name__)

DATASET = "technologies"
SOURCE_ID = "registry"
SOURCE_URL = "https://github.com/kleinnconrad/dataset.technology/blob/main/config/technologies.toml"
LICENSE = "ODbL-1.0"


def technology_fields(technology: Technology) -> dict[str, Any]:
    """Returns the dataset fields of a registry entry.

    Args:
        technology: The registry entry.

    Returns:
        dict: The fields of a ``TechnologyRecord`` without the common fields.
    """
    return {
        "technology_id": technology.id,
        "name": technology.name,
        "domain": technology.domain,
        "category": technology.category,
        "added": technology.added,
        "removed": technology.removed,
        "removed_reason": technology.removed_reason,
        "wikidata_qid": technology.wikidata,
        "stackoverflow_tags": list(technology.stackoverflow_tags),
        "github_repos": list(technology.github_repos),
        "packages": [package.key for package in technology.packages],
        "endoflife_product": technology.endoflife,
        "hn_terms": list(technology.hn_terms),
        "github_language": technology.github_language,
    }


class TechnologiesCollector(Collector):
    """Writes new and changed entries of the technology registry."""
    name = "technologies"
    datasets = (DATASET,)

    def __init__(self, registry_path: Path = CONFIG_PATH) -> None:
        """Initializes the collector.

        Args:
            registry_path: The registry file.
        """
        self._registry_path = registry_path

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Emits a record for every registry entry that is new or changed since the previous run.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters.
        """
        technologies = load_registry(self._registry_path)
        seen = ctx.seen(DATASET)
        records: list[TechnologyRecord] = []
        counters = {"technologies": len(technologies), "active": sum(t.active for t in technologies),
                    "new": 0, "changed": 0}
        for technology in technologies:
            fields = technology_fields(technology)
            item_hash = content_hash(fields)
            status = seen.classify(technology.id, item_hash)
            if status == "unchanged":
                continue
            counters[status] += 1
            revision = seen.mark(technology.id, item_hash, ctx.today)
            records.append(TechnologyRecord(
                **ctx.envelope(DATASET, f"technology:{technology.id}", revision=revision, source_id=SOURCE_ID,
                               source_url=SOURCE_URL, license=LICENSE, backfilled=False),
                **fields,
            ))
        ctx.emit(DATASET, records)
        logger.info("Technologies: %d new, %d changed of %d entries.", counters["new"], counters["changed"],
                    len(technologies))
        return CollectorResult(counters=counters)

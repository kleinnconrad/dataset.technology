"""
Collects aggregates of the editions of the TOP500 list of supercomputers.

Source: TOP500.org publishes two editions of the list per year, in June and in November, each as an
XML file (``TOP500_YYYYMM_all.xml``) that can be downloaded without login. The site states only a
copyright notice and no terms of reuse, so the collector stores aggregates per edition and no
per-system data.

Fetch strategy: one request per edition. The download is a website request: robots.txt
(www.top500.org has none) and text-and-data-mining reservations are checked. Each ``site`` element of
the XML describes one system, with Rmax in GFlop/s, power in kW (empty for many systems), cores
(``number-of-processors``), manufacturer and country (a name, not an ISO code). The record of an
edition holds the total, first-rank and entry-level Rmax (converted to TFlop/s), the cores, the power
of the systems that report it, systems and Rmax per country and systems per manufacturer. The name and
country of the first system are the only per-system values; system and site names and the names of
the list authors are not stored. The XML has no accelerator field, so ``systems_with_accelerator`` is
null unless an edition states accelerators in an element named accelerator or co-processor.

History: the first run stores the latest published edition; a backfill run imports all editions since
June 1993 and continues in the next run if it reaches the run deadline. Later runs request the
editions missing between the earliest stored edition and the latest edition that should exist (June
editions from June on, November editions from November on). A 404 for the newest edition in its month
of publication means that it is not published yet; after that month it counts as a failure.

Schedule: monthly check. At the run deadline or a rate limit the collector stops; the missing editions
follow in the next run. A refusal (HTTP 401/403, challenge page) or a TDM reservation stops all
requests of the run and marks the run as failed.
"""
import logging
import math
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from defusedxml import ElementTree

from collectors.base import Collector, CollectorResult, RunContext
from schema import SupercomputerListRecord
from state import CollectorEntry, content_hash, is_backfilled
from web import BlockedError, FetchError, NotFoundError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "supercomputer_lists"
SOURCE_ID = "top500"
LICENSE = "Aggregates derived from the TOP500 lists (TOP500.org)"
# The trailing slash is part of the download URL
LIST_URL = "https://www.top500.org/lists/top500/{year:04d}/{month:02d}/download/TOP500_{year:04d}{month:02d}_all.xml/"
FIRST_EDITION = date(1993, 6, 1)
EDITION_MONTHS = (6, 11)
# A list file has about 650 KB
XML_TIMEOUT_S = 60.0
GFLOPS_PER_TFLOPS = 1000.0
# Elements that state the accelerators or co-processors of a system, matched against the local name
ACCELERATOR_ELEMENT = re.compile(r"accelerator|co-?processor")
# Values of an accelerator element that state that a system has none
NO_ACCELERATOR_VALUES = frozenset({"", "none", "n/a", "na", "0", "-"})
# Key of systems without a country or manufacturer in the counts
UNKNOWN = "unknown"


@dataclass(frozen=True)
class ListedSystem:
    """The values of one system of a list that enter the aggregates.

    Attributes:
        rank (int): Rank in the list.
        name (str): System name; the computer description if the list gives no name.
        country (str): Country name as written in the list.
        manufacturer (str): Manufacturer as written in the list.
        rmax_gflops (float): Measured LINPACK performance in GFlop/s.
        cores (Optional[int]): Number of cores.
        power_kw (Optional[float]): Power in kW; None if not reported.
        accelerator (Optional[bool]): Whether the system has accelerators; None if the list does not state it.
    """
    rank: int
    name: str
    country: str
    manufacturer: str
    rmax_gflops: float
    cores: Optional[int]
    power_kw: Optional[float]
    accelerator: Optional[bool]


def edition_key(edition: date) -> str:
    """Returns the key of an edition (YYYY-MM), used in the record id and the seen store."""
    return f"{edition:%Y-%m}"


def edition_url(edition: date) -> str:
    """Returns the download URL of the XML file of an edition.

    Args:
        edition: First day of the month of the edition.

    Returns:
        str: The URL.
    """
    return LIST_URL.format(year=edition.year, month=edition.month)


def expected_editions(today: date) -> list[date]:
    """Returns the editions from June 1993 up to the latest edition that should be published by ``today``.

    Args:
        today: The run day.

    Returns:
        list[date]: First days of the months of the editions, in chronological order.
    """
    return [date(year, month, 1) for year in range(FIRST_EDITION.year, today.year + 1) for month in EDITION_MONTHS
            if FIRST_EDITION <= date(year, month, 1) <= today]


def _local_name(tag: str) -> str:
    """Returns the tag name without its XML namespace."""
    return tag.rsplit("}", 1)[-1]


def _number(text: Optional[str]) -> Optional[float]:
    """Parses a finite number; None for empty or invalid text."""
    try:
        value = float((text or "").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _accelerator(fields: dict[str, str]) -> Optional[bool]:
    """Tells whether the fields of a system state an accelerator or co-processor.

    Args:
        fields: Text per element name of the system.

    Returns:
        Optional[bool]: None if the system has no accelerator element; otherwise whether one of the
        elements names an accelerator or a positive number of accelerator cores.
    """
    names = [name for name in fields if ACCELERATOR_ELEMENT.search(name)]
    if not names:
        return None
    for name in names:
        value = fields[name].strip()
        if "cores" in name:
            if (_number(value) or 0) > 0:
                return True
        elif value.lower() not in NO_ACCELERATOR_VALUES:
            return True
    return False


def parse_systems(content: bytes) -> list[ListedSystem]:
    """Reads the systems of a TOP500 XML file.

    Args:
        content: The XML file.

    Returns:
        list[ListedSystem]: The systems in document order.

    Raises:
        ValueError: If the file is not a TOP500 list or a system has no Rmax; also for
            forbidden XML constructs (defusedxml).
        ElementTree.ParseError: If the file is not well-formed XML.
    """
    root = ElementTree.fromstring(content)
    if _local_name(root.tag) != "list":
        raise ValueError(f"not a TOP500 list (root element {_local_name(root.tag)!r})")
    systems: list[ListedSystem] = []
    for position, site in enumerate(child for child in root if _local_name(child.tag) == "site"):
        fields: dict[str, str] = {}
        for element in site.iter():
            if element is not site:
                fields.setdefault(_local_name(element.tag), (element.text or "").strip())
        rmax = _number(fields.get("r-max"))
        if rmax is None:
            raise ValueError(f"system at position {position + 1} has no Rmax")
        rank = _number(fields.get("rank"))
        cores = _number(fields.get("number-of-processors"))
        power = _number(fields.get("power"))
        systems.append(ListedSystem(
            rank=int(rank) if rank else position + 1,
            name=fields.get("system-name") or fields.get("computer") or "",
            country=fields.get("country") or UNKNOWN,
            manufacturer=fields.get("manufacturer") or UNKNOWN,
            rmax_gflops=rmax,
            cores=int(cores) if cores is not None else None,
            power_kw=power if power is not None and power > 0 else None,
            accelerator=_accelerator(fields),
        ))
    return systems


def _tflops(gflops: float) -> float:
    """Converts GFlop/s to TFlop/s, rounded to 1 MFlop/s."""
    return round(gflops / GFLOPS_PER_TFLOPS, 6)


def aggregate(systems: Sequence[ListedSystem]) -> dict[str, Any]:
    """Computes the aggregates of a list.

    Args:
        systems: The systems of the list.

    Returns:
        dict: The fields of a ``SupercomputerListRecord`` without the common fields and the edition.

    Raises:
        ValueError: If the list has no systems.
    """
    if not systems:
        raise ValueError("the list has no systems")
    ranked = sorted(systems, key=lambda system: system.rank)
    first, last = ranked[0], ranked[-1]
    powers = [system.power_kw for system in systems if system.power_kw is not None]
    rmax_by_country: defaultdict[str, float] = defaultdict(float)
    for system in systems:
        rmax_by_country[system.country] += system.rmax_gflops
    flags = [system.accelerator for system in systems if system.accelerator is not None]
    return {
        "systems": len(systems),
        "total_rmax_tflops": _tflops(sum(system.rmax_gflops for system in systems)),
        "rank1_rmax_tflops": _tflops(first.rmax_gflops),
        "rank500_rmax_tflops": _tflops(last.rmax_gflops),
        "rank1_system": first.name,
        "rank1_country": first.country,
        "total_cores": sum(system.cores or 0 for system in systems),
        "total_power_kw": round(sum(powers), 3) if powers else None,
        "systems_with_power": len(powers),
        "systems_by_country": dict(sorted(Counter(system.country for system in systems).items())),
        "rmax_by_country_tflops": {country: _tflops(value) for country, value in sorted(rmax_by_country.items())},
        "systems_by_manufacturer": dict(sorted(Counter(system.manufacturer for system in systems).items())),
        "systems_with_accelerator": sum(flags) if flags else None,
    }


def fetch_list(ctx: RunContext, url: str) -> bytes:
    """Downloads the XML file of an edition, respecting the text-and-data-mining reservations of the host.

    Args:
        ctx: The run context.
        url: The download URL.

    Returns:
        bytes: The XML file.

    Raises:
        BlockedError: If the host refuses automated requests or reserves text and data mining.
        RateLimitError: If the host reports a rate limit.
        NotFoundError: If the edition does not exist (yet).
        FetchError: For other failures.
    """
    if ctx.web.tdm_reserved(url):
        raise BlockedError(url, "text and data mining reserved (TDMRep)")
    response = ctx.web.get(url, timeout_s=XML_TIMEOUT_S)
    if ctx.web.tdm_reserved(response.url, response):
        raise BlockedError(url, "text and data mining reserved (TDMRep)")
    return response.content


class Top500Collector(Collector):
    """Writes one record with the aggregates of every new edition of the TOP500 list."""
    name = "top500"
    datasets = (DATASET,)
    min_days_between_runs = 27

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Requests the missing editions and emits one record per edition.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters; the stored editions are kept in the seen store.
        """
        seen = ctx.seen(DATASET)
        expected = expected_editions(ctx.today)
        newest = expected[-1]
        stored = [edition for edition in expected if edition_key(edition) in seen]
        if ctx.backfill:
            pending = [edition for edition in expected if edition_key(edition) not in seen]
        elif stored:
            pending = [edition for edition in expected if edition > stored[0] and edition_key(edition) not in seen]
        else:
            # First run: the latest published edition only
            pending = [newest]
        first_run = not stored and not ctx.backfill
        counters: dict[str, Any] = {"editions_expected": len(expected), "editions_stored": len(stored),
                                    "requested": 0, "records": 0, "failed": 0}
        complete = True
        error: Optional[str] = None
        index = 0
        while index < len(pending):
            edition = pending[index]
            key = edition_key(edition)
            if ctx.past_deadline():
                counters["deferred"] = len(pending) - index
                complete = False
                break
            index += 1
            url = edition_url(edition)
            counters["requested"] += 1
            try:
                systems = parse_systems(fetch_list(ctx, url))
                fields = aggregate(systems)
            except RateLimitError as e:
                logger.warning("TOP500 rate limit at edition %s; the missing editions follow in the next run: %s",
                               key, e)
                counters["deferred"] = len(pending) - index + 1
                complete = False
                break
            except BlockedError as e:
                logger.error("TOP500.org refused the request; no further requests in this run: %s", e)
                counters["deferred"] = len(pending) - index + 1
                error = f"blocked: {e}"
                complete = False
                break
            except NotFoundError as e:
                if edition == newest and (edition.year, edition.month) == (ctx.today.year, ctx.today.month):
                    logger.info("TOP500 edition %s is not published yet.", key)
                    counters["not_yet_published"] = key
                    if first_run and len(expected) > 1:
                        pending.append(expected[-2])
                    continue
                logger.warning("TOP500 edition %s not found after its month of publication: %s", key, e)
                counters["failed"] += 1
                continue
            except (FetchError, ElementTree.ParseError, ValueError) as e:
                logger.warning("TOP500 edition %s failed: %s", key, e)
                counters["failed"] += 1
                continue
            record = SupercomputerListRecord(
                **ctx.envelope(DATASET, f"top500:{key}", revision=0, source_id=SOURCE_ID, source_url=url,
                               license=LICENSE, backfilled=is_backfilled(edition, entry.first_run)),
                list_edition=edition,
                **fields,
            )
            # Only missing editions are requested, so every record is new and has revision 0
            seen.mark(key, content_hash(fields), ctx.today)
            ctx.emit(DATASET, [record])
            counters["records"] += 1
        failed = counters["failed"]
        # An edition that is not published yet is not a failure
        published = counters["requested"] - ("not_yet_published" in counters)
        if error is None and failed and failed == published:
            error = f"all {failed} requested published TOP500 editions failed"
        logger.info("TOP500: %d new editions stored, %d failed, %d of %d expected editions stored before.",
                    counters["records"], failed, len(stored), len(expected))
        return CollectorResult(counters=counters, complete=complete, error=error)

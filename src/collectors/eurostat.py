"""
Collects indicators of the digital economy and society from Eurostat for the EU and its member states.

Source: Eurostat dissemination API, statistics endpoint 1.0 (JSON-stat 2.0), without a key and
without a documented rate limit. Eurostat authorises the reuse of its statistical data for
commercial or non-commercial purposes provided the source is acknowledged. The series
(QUERIES) cover the use of AI and of paid cloud computing services by enterprises, the
digital intensity of enterprises, employed ICT specialists, the internet access of households
and the use of generative AI by individuals, for the EU (EU27_2020) and the 27 member states
(GEOS).

Fetch strategy: one request per dataset. Every dimension except the country (geo) and the
year (time) is fixed by query parameters; series codes, units and countries are repeated
parameters. The response is a JSON-stat cube: ``value`` maps the flat index of a cell to its
value (cells without data are missing), and the flat index is the row-major position of the
category codes, with the last dimension varying fastest. A dataset that Eurostat no longer
disseminates returns HTTP 404; it is logged and skipped.

Records: change records per dataset, series, unit and country (record_id
``eurostat:<dataset>:<indicator>:<unit>:<geo>``) with the values of all years. A series is
written when it appears and again, with the next revision, when a value is added, changed or
removed. The indicator is the code of the series dimension: ``indic_is``, or ``hhtyp`` for
isoc_ci_in_h; isoc_sks_itspt has one series per unit and the indicator ``TOTAL``. The status
flags of the values (e.g. ``b`` for a break in the series, ``e`` for estimated) are not stored.

History: all years that Eurostat publishes, on the first run; the first record of a series is
marked as backfilled.

Schedule: every 27 days; the underlying surveys are annual. At the run deadline the collector
stops; the remaining datasets follow in the next run.
"""
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import product
from typing import Any, Optional
from urllib.parse import urlencode

from collectors.base import Collector, CollectorResult, RunContext
from schema import EurostatIndicatorRecord
from state import CollectorEntry, SeenStore, content_hash
from web import BlockedError, FetchError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "eurostat_indicators"
SOURCE_ID = "eurostat"
LICENSE = "Eurostat (reuse authorised with acknowledgement of the source)"
DATA_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}"
BASE_PARAMS: tuple[tuple[str, str], ...] = (("format", "JSON"), ("lang", "EN"))
GEO_DIMENSION = "geo"
TIME_DIMENSION = "time"
UNIT_DIMENSION = "unit"
# Indicator of a dataset without a series dimension (one series per unit and country)
SINGLE_SERIES_INDICATOR = "TOTAL"
# The EU (current composition) and its 27 member states; Eurostat uses EL for Greece
GEOS: tuple[str, ...] = (
    "EU27_2020", "BE", "BG", "CZ", "DK", "DE", "EE", "IE", "EL", "ES", "FR", "HR", "IT", "CY", "LV", "LT", "LU",
    "HU", "MT", "NL", "AT", "PL", "PT", "RO", "SI", "SK", "FI", "SE",
)
# Enterprises with 10 or more persons employed, all activities without the financial sector
ENTERPRISE_FILTERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("freq", ("A",)), ("size_emp", ("GE10",)), ("nace_r2", ("C10-S951_X_K",)),
)


@dataclass(frozen=True)
class EurostatQuery:
    """The fixed selection of one Eurostat dataset.

    Attributes:
        dataset (str): Dataset code, e.g. isoc_eb_ai.
        filters (tuple): Codes per dimension, for every dimension except geo and time. The
            series dimension and the unit may have several codes; each code is a separate series.
        series_dimension (Optional[str]): Dimension whose code is the ``indicator`` of the
            records; None for a dataset with one series per unit (indicator ``TOTAL``).
    """
    dataset: str
    filters: tuple[tuple[str, tuple[str, ...]], ...]
    series_dimension: Optional[str] = "indic_is"

    def codes(self, dimension: str) -> tuple[str, ...]:
        """Returns the requested codes of a dimension.

        Args:
            dimension: Name of the dimension.

        Returns:
            tuple[str, ...]: The codes; empty if the dimension is not filtered.
        """
        return next((codes for name, codes in self.filters if name == dimension), ())

    @property
    def indicators(self) -> tuple[str, ...]:
        """The indicators of the series of the dataset."""
        return self.codes(self.series_dimension) if self.series_dimension else (SINGLE_SERIES_INDICATOR,)

    def params(self, geos: Sequence[str] = GEOS) -> list[tuple[str, str]]:
        """Returns the query parameters of the request, with repeated parameters for several codes.

        Args:
            geos: Country codes.

        Returns:
            list[tuple[str, str]]: The parameters.
        """
        return [*BASE_PARAMS, *((name, code) for name, codes in self.filters for code in codes),
                *((GEO_DIMENSION, geo) for geo in geos)]

    def series_url(self, indicator: str, unit: str, geo: str) -> str:
        """Builds the API URL of a single series and country.

        Args:
            indicator: Indicator of the series.
            unit: Unit code.
            geo: Country code.

        Returns:
            str: The URL.
        """
        params: list[tuple[str, str]] = list(BASE_PARAMS)
        for name, codes in self.filters:
            if name == self.series_dimension:
                params.append((name, indicator))
            elif name == UNIT_DIMENSION:
                params.append((name, unit))
            else:
                params.extend((name, code) for code in codes)
        params.append((GEO_DIMENSION, geo))
        return f"{DATA_URL.format(dataset=self.dataset)}?{urlencode(params)}"


QUERIES: tuple[EurostatQuery, ...] = (
    # Enterprises using at least one AI technology; using AI for text, speech or code generation
    EurostatQuery("isoc_eb_ai", (*ENTERPRISE_FILTERS, ("indic_is", ("E_AI_TANY", "E_AI_TNLG")),
                                 ("unit", ("PC_ENT",)))),
    # Enterprises using paid cloud computing services
    EurostatQuery("isoc_cicce_use", (*ENTERPRISE_FILTERS, ("indic_is", ("E_CC",)), ("unit", ("PC_ENT",)))),
    # Employed ICT specialists: share of total employment and thousand persons
    EurostatQuery("isoc_sks_itspt", (("freq", ("A",)), ("unit", ("PC_EMP", "THS_PER"))), series_dimension=None),
    # Households with internet access, all household types
    EurostatQuery("isoc_ci_in_h", (("freq", ("A",)), ("unit", ("PC_HH",)), ("hhtyp", ("TOTAL",))),
                  series_dimension="hhtyp"),
    # Enterprises with at least a basic level of digital intensity; versions 3 and 4 are not comparable
    EurostatQuery("isoc_e_dii", (*ENTERPRISE_FILTERS, ("indic_is", ("E_DI3_GELO", "E_DI4_GELO")),
                                 ("unit", ("PC_ENT",)))),
    # Individuals who used generative AI tools in the last 3 months
    EurostatQuery("isoc_ai_iaiu", (("freq", ("A",)), ("ind_type", ("IND_TOTAL",)), ("indic_is", ("I_IUAI",)),
                                   ("unit", ("PC_IND",)))),
)


@dataclass
class Cube:
    """The values of a JSON-stat response, grouped by series and country.

    Attributes:
        label (str): Label of the dataset.
        updated (Optional[datetime]): Last data update of the dataset (UTC).
        series_labels (dict): Labels of the codes of the series dimension.
        values (dict): Values by (indicator, unit, geo) and year.
    """
    label: str
    updated: Optional[datetime]
    series_labels: dict[str, str] = field(default_factory=dict)
    values: dict[tuple[str, str, str], dict[int, float]] = field(default_factory=dict)


def flat_index(positions: Sequence[int], sizes: Sequence[int]) -> int:
    """Returns the position of a cell in the flat value array of a JSON-stat cube.

    The index is the sum of ``position_i * size_(i+1) * ... * size_(n-1)`` (row-major order,
    the last dimension varies fastest).

    Args:
        positions: Position of the category code in each dimension.
        sizes: Number of codes of each dimension.

    Returns:
        int: The flat index.
    """
    index = 0
    for position, size in zip(positions, sizes):
        index = index * size + position
    return index


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parses a timestamp such as 2026-06-15T11:00:00+0200 and converts it to UTC.

    Args:
        value: The timestamp.

    Returns:
        Optional[datetime]: The UTC time, or None for a missing or invalid value.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _category_index(category: Mapping[str, Any]) -> dict[str, int]:
    """Returns the position of each code of a dimension; JSON-stat allows an object or a list."""
    index = category.get("index", {})
    if isinstance(index, list):
        return {str(code): position for position, code in enumerate(index)}
    return {str(code): int(position) for code, position in index.items()}


def parse_cube(data: Any, query: EurostatQuery, geos: Sequence[str] = GEOS) -> Cube:
    """Reads the values of the requested series from a JSON-stat response.

    Args:
        data: The parsed JSON response.
        query: The selection of the request.
        geos: The requested country codes.

    Returns:
        Cube: The values of the requested series codes, units and countries.

    Raises:
        ValueError: If the response is an error object or has an unexpected structure, or if
            a dimension that should be fixed has several codes.
        KeyError: If a dimension listed in ``id`` is missing.
    """
    if not isinstance(data, dict) or "error" in data or not isinstance(data.get("id"), list):
        raise ValueError(f"unexpected response: {str(data)[:200]}")
    dimensions = [str(name) for name in data["id"]]
    sizes = [int(size) for size in data["size"]]
    if len(dimensions) != len(sizes):
        raise ValueError("dimensions and sizes differ in length")
    variable = {GEO_DIMENSION, TIME_DIMENSION, UNIT_DIMENSION, query.series_dimension}
    for name, size in zip(dimensions, sizes):
        if name not in variable and size > 1:
            raise ValueError(f"dimension {name} of {query.dataset} has {size} codes; it must be fixed to one")
    for name in (GEO_DIMENSION, TIME_DIMENSION, UNIT_DIMENSION):
        if name not in dimensions:
            raise ValueError(f"dataset {query.dataset} has no dimension {name}")
    if query.series_dimension and query.series_dimension not in dimensions:
        raise ValueError(f"dataset {query.dataset} has no dimension {query.series_dimension}")
    categories = {name: _category_index(data["dimension"][name]["category"]) for name in dimensions}
    raw_values = data.get("value") or {}
    if isinstance(raw_values, list):
        raw_values = {str(position): value for position, value in enumerate(raw_values)}
    cube = Cube(label=str(data.get("label") or query.dataset), updated=parse_timestamp(data.get("updated")))
    if query.series_dimension:
        labels = data["dimension"][query.series_dimension]["category"].get("label") or {}
        cube.series_labels = {str(code): str(label) for code, label in labels.items()}
    indicators, units, wanted_geos = set(query.indicators), set(query.codes(UNIT_DIMENSION)), set(geos)
    for cell in product(*(categories[name].items() for name in dimensions)):
        value = raw_values.get(str(flat_index([position for _, position in cell], sizes)))
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            continue
        codes = {name: code for name, (code, _) in zip(dimensions, cell)}
        indicator = codes[query.series_dimension] if query.series_dimension else SINGLE_SERIES_INDICATOR
        unit, geo, period = codes[UNIT_DIMENSION], codes[GEO_DIMENSION], codes[TIME_DIMENSION]
        if indicator not in indicators or unit not in units or geo not in wanted_geos or not period.isdigit():
            continue
        cube.values.setdefault((indicator, unit, geo), {})[int(period)] = float(value)
    return cube


def indicator_label(query: EurostatQuery, cube: Cube, indicator: str) -> str:
    """Returns the label of a series.

    Args:
        query: The selection of the dataset.
        cube: The parsed response.
        indicator: Indicator of the series.

    Returns:
        str: The label of the indic_is code; for other series dimensions the dataset label
        with the label of the code; for a dataset with one series the dataset label.
    """
    if query.series_dimension is None:
        return cube.label
    code_label = cube.series_labels.get(indicator, indicator)
    return code_label if query.series_dimension == "indic_is" else f"{cube.label}: {code_label}"


class EurostatCollector(Collector):
    """Writes new and changed series of Eurostat indicators on the digital economy and society."""
    name = "eurostat"
    datasets = (DATASET,)
    min_days_between_runs = 27

    def __init__(self, queries: Sequence[EurostatQuery] = QUERIES, geos: Sequence[str] = GEOS) -> None:
        """Initializes the collector.

        Args:
            queries: Selections of the datasets.
            geos: Country codes.
        """
        self._queries = tuple(queries)
        self._geos = tuple(geos)

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Downloads every dataset and emits the series whose values are new or changed.

        Args:
            ctx: The run context.
            entry: Run history of this collector.

        Returns:
            CollectorResult: Counters.
        """
        seen = ctx.seen(DATASET)
        counters: dict[str, Any] = {"datasets": len(self._queries), "failed": 0, "series": 0, "new": 0,
                                    "changed": 0, "records": 0}
        complete = True
        error: Optional[str] = None
        attempted = 0
        for position, query in enumerate(self._queries):
            if ctx.past_deadline():
                counters["deferred"] = len(self._queries) - position
                complete = False
                break
            attempted += 1
            try:
                response = ctx.web.get(DATA_URL.format(dataset=query.dataset), params=query.params(self._geos),
                                       respect_robots=False)
                cube = parse_cube(response.json(), query, self._geos)
            except RateLimitError as e:
                logger.warning("Eurostat API rate limit at %s; the remaining datasets follow in the next run: %s",
                               query.dataset, e)
                counters["deferred"] = len(self._queries) - position
                complete = False
                break
            except BlockedError as e:
                logger.error("Eurostat API refused the request; no further requests in this run: %s", e)
                error = f"blocked: {e}"
                complete = False
                break
            except (FetchError, ValueError, KeyError, TypeError) as e:
                logger.warning("Eurostat dataset %s failed: %s", query.dataset, e)
                counters["failed"] += 1
                continue
            if not cube.values:
                # An empty response would otherwise remove all values of the stored series
                logger.warning("Eurostat dataset %s returned no values for the requested series.", query.dataset)
                counters["failed"] += 1
                continue
            records = self._records(ctx, query, cube, seen, counters)
            ctx.emit(DATASET, records)
            counters["records"] += len(records)
        if error is None and counters["failed"] and counters["failed"] == attempted:
            error = f"all {attempted} requested datasets failed"
        logger.info("Eurostat: %d records (%d new, %d changed series) of %d series.", counters["records"],
                    counters["new"], counters["changed"], counters["series"])
        return CollectorResult(counters=counters, complete=complete, error=error)

    def _records(self, ctx: RunContext, query: EurostatQuery, cube: Cube, seen: SeenStore,
                 counters: dict[str, Any]) -> list[EurostatIndicatorRecord]:
        """Builds the records of the new and changed series of one dataset.

        Args:
            ctx: The run context.
            query: The selection of the dataset.
            cube: The parsed response.
            seen: The seen store of the dataset.
            counters: Counters of the run; updated in place.

        Returns:
            list[EurostatIndicatorRecord]: One record per new or changed series and country.
        """
        records: list[EurostatIndicatorRecord] = []
        for indicator, unit, geo in product(query.indicators, query.codes(UNIT_DIMENSION), self._geos):
            values = dict(sorted(cube.values.get((indicator, unit, geo), {}).items()))
            key = f"{query.dataset}:{indicator}:{unit}:{geo}"
            if values:
                counters["series"] += 1
            item_hash = content_hash(values)
            status = seen.classify(key, item_hash)
            # A series without values is written only if it had values before
            if status == "unchanged" or (status == "new" and not values):
                continue
            counters[status] += 1
            revision = seen.mark(key, item_hash, ctx.today)
            records.append(EurostatIndicatorRecord(
                **ctx.envelope(DATASET, f"eurostat:{key}", revision=revision, source_id=SOURCE_ID,
                               source_url=query.series_url(indicator, unit, geo), license=LICENSE,
                               backfilled=revision == 0),
                dataset_code=query.dataset,
                indicator=indicator,
                indicator_label=indicator_label(query, cube, indicator),
                unit=unit,
                geo=geo,
                values=values,
                source_updated=cube.updated,
            ))
        return records

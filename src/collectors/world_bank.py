"""
Collects technology-related development indicators of the World Bank for all countries and aggregates.

Source: World Bank Indicators API v2 (``https://api.worldbank.org/v2/``), without a key and
without a documented rate limit. The World Bank licenses its datasets under CC BY 4.0, which
requires attribution and an indication of changes. The indicators are a fixed list
(INDICATORS) from the World Development Indicators: internet use, broadband and mobile
subscriptions, secure servers, high-technology and ICT trade, ICT service exports, R&D
expenditure and researchers, patent applications, scientific articles, electric power
consumption and charges for the use of intellectual property.

Fetch strategy:
- one request returns all countries, aggregates and years of an indicator
  (``country/all/indicator/<id>?per_page=20000``, about 17,500 rows); further pages are
  followed if the API splits the response;
- before the full download, a request with ``per_page=1`` reads the date of the last update
  of the indicator (``lastupdated``); an indicator whose date equals the one in the cursor is
  skipped;
- the country list (``country?per_page=400``) provides the 3-letter codes of the five income
  groups, whose data rows carry no ISO3 code, and tells aggregates (region ``NA``) from
  countries; it is loaded only when an indicator is downloaded;
- an archived indicator returns a message instead of data; it is logged and skipped.

Records: the World Bank revises past values with its updates, so the dataset stores
revisions per indicator and country or aggregate (record_id ``wb:<indicator>:<country code>``):
revision 0 holds all years with a value; a later revision holds only the years whose value
was added or changed, and lists the years whose value disappeared in ``removed_years``.
Countries whose values did not change get no record. The current values are kept in the
seen store, one shard per indicator, so that only updated indicators rewrite their state file.

History: all available years (from 1960) on the first run; revision 0 records are marked as
backfilled.

Schedule: every 27 days; the World Bank updates the World Development Indicators a few times
a year. At the run deadline the collector stops; the remaining indicators follow in the next run.
"""
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from collectors.base import Collector, CollectorResult, RunContext
from schema import WorldBankIndicatorRecord
from state import CollectorEntry, SeenStore, content_hash
from web import BlockedError, FetchError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "world_bank_indicators"
SOURCE_ID = "worldbank_indicators"
LICENSE = "CC-BY-4.0 (World Bank)"
API_URL = "https://api.worldbank.org/v2"
COUNTRIES_URL = f"{API_URL}/country?format=json&per_page=400"
INDICATOR_URL = API_URL + "/country/all/indicator/{indicator}?format=json&per_page={per_page}"
ENTITY_URL = API_URL + "/country/{country}/indicator/{indicator}?format=json"
INDICATORS: tuple[str, ...] = (
    "IT.NET.USER.ZS",     # Individuals using the Internet (% of population)
    "IT.NET.BBND.P2",     # Fixed broadband subscriptions (per 100 people)
    "IT.CEL.SETS.P2",     # Mobile cellular subscriptions (per 100 people)
    "IT.NET.SECR.P6",     # Secure Internet servers (per 1 million people)
    "TX.VAL.TECH.CD",     # High-technology exports (current US$)
    "TX.VAL.TECH.MF.ZS",  # High-technology exports (% of manufactured exports)
    "BX.GSR.CCIS.CD",     # ICT service exports (BoP, current US$)
    "BX.GSR.CCIS.ZS",     # ICT service exports (% of service exports, BoP)
    "TX.VAL.ICTG.ZS.UN",  # ICT goods exports (% of total goods exports)
    "TM.VAL.ICTG.ZS.UN",  # ICT goods imports (% of total goods imports)
    "GB.XPD.RSDV.GD.ZS",  # Research and development expenditure (% of GDP)
    "SP.POP.SCIE.RD.P6",  # Researchers in R&D (per million people)
    "IP.PAT.RESD",        # Patent applications, residents
    "IP.PAT.NRES",        # Patent applications, nonresidents
    "IP.JRN.ARTC.SC",     # Scientific and technical journal articles
    "EG.USE.ELEC.KH.PC",  # Electric power consumption (kWh per capita)
    "BX.GSR.ROYL.CD",     # Charges for the use of intellectual property, receipts (BoP, current US$)
    "BM.GSR.ROYL.CD",     # Charges for the use of intellectual property, payments (BoP, current US$)
    "BX.GSR.CMCP.ZS",     # Communications, computer, etc. (% of service exports, BoP)
)
# Rows per page; one page holds all countries and years of an indicator (about 17,500 rows)
PER_PAGE = 20000
# The first request of an indicator that the API had not cached took 87 s in a test
TIMEOUT_S = 180.0
# Region id of the aggregates (regions, income groups, the world) in the country list
AGGREGATE_REGION_ID = "NA"


class ApiMessageError(Exception):
    """The API answered with a message instead of data, e.g. for an archived indicator."""


class CountryListError(Exception):
    """The country list could not be loaded; no indicator can be processed without it."""


@dataclass(frozen=True)
class Entity:
    """A country or aggregate of the country list.

    Attributes:
        code (str): 3-letter code (ISO 3166-1 alpha-3 for countries, World Bank code for aggregates).
        name (str): Name of the country or aggregate.
        is_aggregate (bool): True for regions, income groups and other aggregates.
    """
    code: str
    name: str
    is_aggregate: bool


@dataclass
class IndicatorData:
    """The values of one indicator for all countries and aggregates.

    Attributes:
        name (Optional[str]): Name of the indicator from the data rows.
        last_updated (Optional[str]): Date of the last update at the World Bank (``lastupdated``).
        values (dict): Values by country code and year (4-digit string); a country whose rows
            are all null has an empty map.
        names (dict): Country names by code, as given in the data rows.
        skipped_rows (int): Rows without a known country code or without an annual date.
    """
    name: Optional[str] = None
    last_updated: Optional[str] = None
    values: dict[str, dict[str, float]] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    skipped_rows: int = 0


def indicator_url(indicator: str, per_page: int = PER_PAGE) -> str:
    """Builds the URL of all countries and years of an indicator.

    Args:
        indicator: Indicator code, e.g. IT.NET.USER.ZS.
        per_page: Rows per page; 1 reads only the metadata.

    Returns:
        str: The URL of the first page.
    """
    return INDICATOR_URL.format(indicator=indicator, per_page=per_page)


def split_page(data: Any) -> tuple[dict[str, Any], list[Any]]:
    """Splits a response page of the API into its metadata and its rows.

    Args:
        data: The parsed JSON response.

    Returns:
        tuple: The metadata (page, pages, total, lastupdated) and the rows; an empty list if
        the page has no rows.

    Raises:
        ApiMessageError: If the API returned a message instead of data.
        ValueError: If the response has an unexpected structure.
    """
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError("unexpected response structure")
    if "message" in data[0]:
        messages = data[0]["message"]
        items = messages if isinstance(messages, list) else [messages]
        raise ApiMessageError("; ".join(
            str(item.get("value") or item.get("key")) if isinstance(item, dict) else str(item) for item in items))
    rows = data[1] if len(data) > 1 and isinstance(data[1], list) else []
    return data[0], list(rows)


def fetch_all_rows(ctx: RunContext, url: str) -> tuple[dict[str, Any], list[Any]]:
    """Fetches all pages of a list resource.

    Args:
        ctx: The run context.
        url: URL of the first page, with a query string.

    Returns:
        tuple: The metadata of the first page and the rows of all pages.

    Raises:
        ApiMessageError: If the API returned a message instead of data.
        ValueError: If the response is not valid JSON, has an unexpected structure, or
            contains fewer rows than its metadata announces.
        FetchError: If a request fails.
    """
    meta, rows = split_page(ctx.web.get(url, respect_robots=False, timeout_s=TIMEOUT_S).json())
    for page in range(2, int(meta.get("pages") or 1) + 1):
        _, more = split_page(ctx.web.get(f"{url}&page={page}", respect_robots=False, timeout_s=TIMEOUT_S).json())
        rows.extend(more)
    total = int(meta.get("total") or 0)
    if len(rows) != total:
        raise ValueError(f"received {len(rows)} of {total} rows from {url}")
    return meta, rows


def fetch_last_updated(ctx: RunContext, indicator: str) -> Optional[str]:
    """Reads the date of the last update of an indicator with a request for one row.

    Args:
        ctx: The run context.
        indicator: Indicator code.

    Returns:
        Optional[str]: The ``lastupdated`` date, e.g. 2026-10-08, or None if the API omits it.

    Raises:
        ApiMessageError: If the indicator is archived or unknown.
        ValueError: If the response is not valid JSON or has an unexpected structure.
        FetchError: If the request fails.
    """
    meta, _ = split_page(ctx.web.get(indicator_url(indicator, per_page=1), respect_robots=False,
                                     timeout_s=TIMEOUT_S).json())
    value = meta.get("lastupdated")
    return str(value) if value else None


def parse_entities(rows: Sequence[Any]) -> dict[str, Entity]:
    """Reads the countries and aggregates of the country list.

    Args:
        rows: Rows of the country list.

    Returns:
        dict[str, Entity]: Entities by their 2-letter code (``iso2Code``), the join key of the data rows.
    """
    entities: dict[str, Entity] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id") or not row.get("iso2Code"):
            continue
        region = row.get("region") if isinstance(row.get("region"), dict) else {}
        entities[str(row["iso2Code"])] = Entity(
            code=str(row["id"]), name=str(row.get("name") or row["id"]).strip(),
            is_aggregate=region.get("id") == AGGREGATE_REGION_ID,
        )
    return entities


def parse_indicator_rows(rows: Sequence[Any], entities: Mapping[str, Entity]) -> IndicatorData:
    """Groups the data rows of an indicator by country and year.

    The country code is ``countryiso3code``; for the five income groups, whose rows carry
    no ISO3 code, it is the 3-letter id from the country list, matched by the 2-letter code.

    Args:
        rows: Data rows of the indicator.
        entities: Entities of the country list by 2-letter code.

    Returns:
        IndicatorData: Name, non-null values by country and year, and country names.
    """
    data = IndicatorData()
    for row in rows:
        country = row.get("country") if isinstance(row, dict) else None
        year = row.get("date") if isinstance(row, dict) else None
        if not isinstance(country, dict) or not isinstance(year, str) or len(year) != 4 or not year.isdigit():
            data.skipped_rows += 1
            continue
        entity = entities.get(str(country.get("id") or ""))
        code = str(row.get("countryiso3code") or "") or (entity.code if entity else "")
        if not code:
            data.skipped_rows += 1
            continue
        indicator = row.get("indicator")
        if data.name is None and isinstance(indicator, dict) and indicator.get("value"):
            data.name = str(indicator["value"]).strip()
        data.names.setdefault(code, str(country.get("value") or code).strip())
        series = data.values.setdefault(code, {})
        value = row.get("value")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            series[year] = float(value)
    data.values = {code: dict(sorted(series.items())) for code, series in data.values.items()}
    return data


def year_delta(previous: Mapping[str, float], current: Mapping[str, float]) -> tuple[dict[str, float], list[str]]:
    """Compares the stored values of a country with the current ones.

    Args:
        previous: Stored values by year.
        current: Current values by year.

    Returns:
        tuple: The added or changed values by year, and the years whose value disappeared.
    """
    changed = {year: value for year, value in current.items() if previous.get(year) != value}
    removed = sorted(year for year in previous if year not in current)
    return changed, removed


def _parse_date(value: Optional[str]) -> Optional[date]:
    """Parses an ISO date, returning None for a missing or invalid value."""
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


class CountryList:
    """The countries and aggregates of the API, loaded on first use."""

    def __init__(self, ctx: RunContext) -> None:
        """Initializes the list without loading it.

        Args:
            ctx: The run context.
        """
        self._ctx = ctx
        self._by_iso2: Optional[dict[str, Entity]] = None
        self._by_code: dict[str, Entity] = {}

    def by_iso2(self) -> dict[str, Entity]:
        """Returns the entities by 2-letter code, loading the list on the first call.

        Returns:
            dict[str, Entity]: The entities.

        Raises:
            RateLimitError: If the API reports a rate limit.
            BlockedError: If the API refuses the request.
            CountryListError: If the list cannot be loaded for another reason.
        """
        if self._by_iso2 is None:
            try:
                _, rows = fetch_all_rows(self._ctx, COUNTRIES_URL)
            except (RateLimitError, BlockedError):
                raise
            except (ApiMessageError, FetchError, ValueError) as e:
                raise CountryListError(f"country list failed: {e}") from e
            entities = parse_entities(rows)
            if not entities:
                raise CountryListError("country list is empty")
            self._by_iso2 = entities
            self._by_code = {entity.code: entity for entity in entities.values()}
        return self._by_iso2

    def by_code(self, code: str) -> Optional[Entity]:
        """Returns the entity with a 3-letter code, if the list is loaded and contains it.

        Args:
            code: 3-letter code.

        Returns:
            Optional[Entity]: The entity.
        """
        return self._by_code.get(code)


class WorldBankCollector(Collector):
    """Writes the added, changed and removed values of technology-related World Bank indicators."""
    name = "world_bank"
    datasets = (DATASET,)
    min_days_between_runs = 27

    def __init__(self, indicators: Sequence[str] = INDICATORS) -> None:
        """Initializes the collector.

        Args:
            indicators: Indicator codes to collect.
        """
        self._indicators = tuple(indicators)

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Downloads every indicator that the World Bank updated since the previous run and emits the changes.

        Args:
            ctx: The run context.
            entry: Run history of this collector; the cursor holds the ``lastupdated`` date per indicator.

        Returns:
            CollectorResult: The updated cursor and counters.
        """
        last_updated: dict[str, str] = dict(entry.cursor.get("last_updated") or {})
        countries = CountryList(ctx)
        counters: dict[str, Any] = {"indicators": len(self._indicators), "unchanged": 0, "downloaded": 0,
                                    "archived": 0, "failed": 0, "records": 0, "values": 0, "removed_years": 0,
                                    "skipped_rows": 0}
        complete = True
        error: Optional[str] = None
        for position, indicator in enumerate(self._indicators):
            if ctx.past_deadline():
                counters["deferred"] = len(self._indicators) - position
                complete = False
                break
            seen = ctx.seen(DATASET, shard=indicator)
            try:
                data = self._download(ctx, indicator, last_updated.get(indicator), seen, countries)
            except RateLimitError as e:
                logger.warning("World Bank API rate limit at %s; the remaining indicators follow in the next run: %s",
                               indicator, e)
                counters["deferred"] = len(self._indicators) - position
                complete = False
                break
            except BlockedError as e:
                logger.error("World Bank API refused the request; no further requests in this run: %s", e)
                error = f"blocked: {e}"
                complete = False
                break
            except CountryListError as e:
                logger.error("World Bank collector stopped: %s", e)
                error = str(e)
                complete = False
                break
            except ApiMessageError as e:
                logger.warning("World Bank indicator %s is not available (archived or removed): %s", indicator, e)
                counters["archived"] += 1
                continue
            except (FetchError, ValueError, KeyError, TypeError) as e:
                logger.warning("World Bank indicator %s failed: %s", indicator, e)
                counters["failed"] += 1
                continue
            if data is None:
                counters["unchanged"] += 1
                continue
            records = self._records(ctx, indicator, data, countries, seen, counters)
            # All records of an indicator (at most one per country) are emitted together with its seen shard
            ctx.emit(DATASET, records)
            counters["downloaded"] += 1
            counters["records"] += len(records)
            counters["skipped_rows"] += data.skipped_rows
            if data.last_updated:
                last_updated[indicator] = data.last_updated
            logger.info("World Bank indicator %s (updated %s): %d changed countries.", indicator, data.last_updated,
                        len(records))
        unavailable = counters["failed"] + counters["archived"]
        if error is None and unavailable and not counters["downloaded"] and not counters["unchanged"]:
            error = f"all {unavailable} requested indicators failed or are archived"
        logger.info("World Bank: %d records for %d downloaded indicators, %d unchanged, %d archived, %d failed.",
                    counters["records"], counters["downloaded"], counters["unchanged"], counters["archived"],
                    counters["failed"])
        return CollectorResult(cursor={**entry.cursor, "last_updated": last_updated}, counters=counters,
                               complete=complete, error=error)

    @staticmethod
    def _download(ctx: RunContext, indicator: str, stored_last_updated: Optional[str], seen: SeenStore,
                  countries: CountryList) -> Optional[IndicatorData]:
        """Downloads all values of an indicator, unless its update date equals the stored one.

        Args:
            ctx: The run context.
            indicator: Indicator code.
            stored_last_updated: ``lastupdated`` date of the previous download, from the cursor.
            seen: Seen shard of the indicator.
            countries: The country list.

        Returns:
            Optional[IndicatorData]: The values, or None if the indicator was not updated.

        Raises:
            ApiMessageError: If the indicator is archived or unknown.
            CountryListError: If the country list cannot be loaded.
            ValueError: If a response is invalid or incomplete.
            FetchError: If a request fails.
        """
        # Without a stored date or stored values (first run, lost state) the indicator is downloaded in full
        if stored_last_updated and len(seen) and fetch_last_updated(ctx, indicator) == stored_last_updated:
            return None
        entities = countries.by_iso2()
        meta, rows = fetch_all_rows(ctx, indicator_url(indicator))
        if not rows:
            raise ValueError("the response contains no rows")
        data = parse_indicator_rows(rows, entities)
        data.last_updated = str(meta["lastupdated"]) if meta.get("lastupdated") else None
        return data

    @staticmethod
    def _records(ctx: RunContext, indicator: str, data: IndicatorData, countries: CountryList, seen: SeenStore,
                 counters: dict[str, Any]) -> list[WorldBankIndicatorRecord]:
        """Builds the records of the countries whose values changed and stores their current values.

        Args:
            ctx: The run context.
            indicator: Indicator code.
            data: The downloaded values.
            countries: The loaded country list.
            seen: Seen shard of the indicator.
            counters: Counters of the run; updated in place.

        Returns:
            list[WorldBankIndicatorRecord]: One record per new or changed country.
        """
        records: list[WorldBankIndicatorRecord] = []
        # Countries that disappeared from the response are compared with empty values
        for code in sorted(set(data.values) | set(seen.keys())):
            values = data.values.get(code, {})
            item_hash = content_hash(values)
            if seen.classify(code, item_hash) == "unchanged":
                continue
            previous = seen.get(code)
            if previous is None and not values:
                continue
            stored_values = (previous.data.get("values") or {}) if previous else {}
            changed, removed = year_delta(stored_values, values)
            revision = seen.mark(code, item_hash, ctx.today, data={"values": values})
            entity = countries.by_code(code)
            records.append(WorldBankIndicatorRecord(
                **ctx.envelope(DATASET, f"wb:{indicator}:{code}", revision=revision, source_id=SOURCE_ID,
                               source_url=ENTITY_URL.format(country=code, indicator=indicator), license=LICENSE,
                               backfilled=revision == 0),
                indicator_id=indicator,
                indicator_name=data.name or indicator,
                country_code=code,
                country_name=data.names.get(code) or (entity.name if entity else code),
                is_aggregate=entity.is_aggregate if entity else False,
                values={int(year): value for year, value in changed.items()},
                removed_years=[int(year) for year in removed],
                source_last_updated=_parse_date(data.last_updated),
            ))
            counters["values"] += len(changed)
            counters["removed_years"] += len(removed)
        return records

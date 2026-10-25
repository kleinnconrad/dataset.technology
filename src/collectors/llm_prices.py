"""
Collects the list prices and limits of LLM offerings from the LiteLLM model price list.

Source: ``model_prices_and_context_window.json`` in the GitHub repository BerriAI/litellm,
read from ``raw.githubusercontent.com``, which serves repository files and has no
robots.txt. The file maps about 4,300 model keys (a model at a provider or platform, e.g.
``gpt-4o`` or ``bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0``) to prices per token,
token limits and feature flags. The repository content outside ``enterprise/`` is licensed
under MIT.

Fetch strategy: one conditional request per run with the ETag of the previous download
(collector cursor ``etag``); HTTP 304 ends the run without records. The file has about 3 MB.

Records: change records per model key (record_id ``llm-price:<model key>``) for the
offerings with the mode chat, completion, responses or embedding; the documentation entry
``sample_spec`` is skipped. Prices per token are converted to prices per million tokens with
decimal arithmetic, so that no binary rounding noise is stored; non-numeric and negative
prices are stored as null. ``max_output_tokens`` is not filled from the legacy field
``max_tokens``, and ``pricing_page`` is the ``source`` URL of the entry. An offering is written
when it appears and again, with the next revision, when a stored field changes. An offering
that disappears from the list, or changes to another mode, is written once with
``is_listed`` false and its last known values, which the seen store keeps. A list that would
remove more than half of the listed offerings is treated as defective.

History: the file states only current prices; the series starts with the first run. Records
are not marked as backfilled.

Schedule: every run; an unchanged file costs one conditional request.
"""
import logging
import math
import re
from collections import Counter
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from collectors.base import Collector, CollectorResult, RunContext
from schema import LlmPriceRecord
from state import CollectorEntry, content_hash
from web import RateLimitError

logger = logging.getLogger(__name__)

DATASET = "llm_prices"
SOURCE_ID = "litellm_model_prices"
LICENSE = "MIT (LiteLLM model price list)"
PRICES_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
KEPT_MODES = frozenset({"chat", "completion", "responses", "embedding"})
# Entries of the file that describe the format instead of an offering
SKIPPED_KEYS = frozenset({"sample_spec"})
TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)
FETCH_TIMEOUT_S = 60.0
EMIT_BATCH_SIZE = 500
# A changed list that would remove more than this share of the listed offerings is treated as defective
MAX_REMOVED_SHARE = 0.5
MIN_LISTED_FOR_REMOVAL_CHECK = 20
# Record field and source field of the prices (USD per token in the source)
PRICE_FIELDS = {
    "input_price_usd_per_mtok": "input_cost_per_token",
    "output_price_usd_per_mtok": "output_cost_per_token",
    "cached_input_price_usd_per_mtok": "cache_read_input_token_cost",
    "batch_input_price_usd_per_mtok": "input_cost_per_token_batches",
    "batch_output_price_usd_per_mtok": "output_cost_per_token_batches",
}
LIMIT_FIELDS = ("max_input_tokens", "max_output_tokens")
FLAG_FIELDS = ("supports_function_calling", "supports_vision", "supports_reasoning", "supports_prompt_caching",
               "supports_web_search")
OPTIONAL_FIELDS = (*PRICE_FIELDS, *LIMIT_FIELDS, *FLAG_FIELDS, "deprecation_date", "pricing_page")
ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


def price_per_million_tokens(value: Any) -> Optional[float]:
    """Converts a price per token to a price per million tokens.

    Args:
        value: Price per token in USD from the list, e.g. ``2.5e-06``.

    Returns:
        Optional[float]: Price per million tokens (e.g. 2.5), computed with decimal
        arithmetic; None for non-numeric, non-finite and negative values.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(Decimal(str(value)) * TOKENS_PER_PRICE_UNIT) if value else 0.0


def token_limit(value: Any) -> Optional[int]:
    """Returns a token limit as an integer; None for missing, non-integer and negative values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value if isinstance(value, int) and value >= 0 else None


def flag(value: Any) -> Optional[bool]:
    """Returns a boolean feature flag; None if it is missing or not a boolean."""
    return value if isinstance(value, bool) else None


def deprecation_date(value: Any) -> Optional[date]:
    """Parses a deprecation date in the form YYYY-MM-DD; None if it is missing or invalid."""
    if not isinstance(value, str) or not ISO_DATE_PATTERN.fullmatch(value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _text(value: Any) -> Optional[str]:
    """Returns a non-empty string without surrounding whitespace, or None."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_price_list(data: Any) -> tuple[dict[str, dict[str, Any]], int]:
    """Parses the price list.

    Args:
        data: The decoded JSON file.

    Returns:
        tuple: Fields of a ``LlmPriceRecord`` (without the common fields and ``is_listed``)
        per model key, for the kept modes; and the number of entries skipped because
        they name no provider.

    Raises:
        ValueError: If the file is not a JSON object.
    """
    if not isinstance(data, dict):
        raise ValueError("The LiteLLM price list is not a JSON object")
    offerings: dict[str, dict[str, Any]] = {}
    invalid = 0
    for key, entry in data.items():
        if key in SKIPPED_KEYS or not isinstance(entry, dict):
            continue
        mode = entry.get("mode")
        if not isinstance(mode, str) or mode not in KEPT_MODES:
            continue
        provider = _text(entry.get("litellm_provider"))
        if provider is None:
            invalid += 1
            continue
        offerings[key] = {
            "model_key": key,
            "provider": provider,
            "mode": mode,
            **{name: price_per_million_tokens(entry.get(source)) for name, source in PRICE_FIELDS.items()},
            **{name: token_limit(entry.get(name)) for name in LIMIT_FIELDS},
            **{name: flag(entry.get(name)) for name in FLAG_FIELDS},
            "deprecation_date": deprecation_date(entry.get("deprecation_date")),
            "pricing_page": _text(entry.get("source")),
        }
    return offerings, invalid


def _stored(fields: dict[str, Any]) -> dict[str, Any]:
    """Returns the fields in JSON-compatible form, with dates as ISO strings."""
    return {name: value.isoformat() if isinstance(value, date) else value for name, value in fields.items()}


def _item_hash(stored: dict[str, Any], is_listed: bool) -> str:
    """Returns the hash of the stored fields of an offering and its listing state."""
    return content_hash({**stored, "is_listed": is_listed})


class LlmPricesCollector(Collector):
    """Writes the new, changed and removed offerings of the LiteLLM model price list."""
    name = "llm_prices"
    datasets = (DATASET,)
    min_days_between_runs = 0

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Downloads the price list if it changed and emits the changed offerings.

        Args:
            ctx: The run context.
            entry: Run history of this collector; the cursor holds the ETag of the last download.

        Returns:
            CollectorResult: The new ETag and counters.

        Raises:
            FetchError: If the download fails.
            ValueError: If the file is not a valid price list.
        """
        etag = entry.cursor.get("etag")
        headers = {"If-None-Match": etag} if etag else None
        try:
            response = ctx.web.get(PRICES_URL, headers=headers, respect_robots=False, timeout_s=FETCH_TIMEOUT_S)
        except RateLimitError as e:
            logger.warning("GitHub rate limit for the LiteLLM price list; it follows in the next run: %s", e)
            return CollectorResult(counters={"rate_limited": 1}, complete=False)
        if response.status == 304:
            logger.info("LiteLLM price list unchanged since the previous download.")
            return CollectorResult(counters={"not_modified": 1})
        offerings, invalid = parse_price_list(response.json())
        counters = self._write(ctx, offerings)
        counters["entries_without_provider"] = invalid
        logger.info("LiteLLM prices: %d offerings, %d new, %d changed, %d delisted.", counters["offerings"],
                    counters["new"], counters["changed"], counters["delisted"])
        cursor = {name: value for name, value in entry.cursor.items() if name != "etag"}
        if response.headers.get("etag"):
            cursor["etag"] = response.headers["etag"]
        return CollectorResult(cursor=cursor, counters=counters)

    @staticmethod
    def _write(ctx: RunContext, offerings: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Emits the new and changed offerings and the offerings that disappeared from the list.

        Args:
            ctx: The run context.
            offerings: Fields per model key of the current list.

        Returns:
            dict: Counters.

        Raises:
            ValueError: If the list would remove more than MAX_REMOVED_SHARE of the listed offerings.
        """
        seen = ctx.seen(DATASET)
        listed_before = [key for key in seen.keys() if not seen.get(key).data.get("delisted")]
        removed = [key for key in listed_before if key not in offerings]
        if len(listed_before) >= MIN_LISTED_FOR_REMOVAL_CHECK and len(removed) > MAX_REMOVED_SHARE * len(listed_before):
            raise ValueError(f"{len(removed)} of {len(listed_before)} listed offerings are missing from the LiteLLM "
                             "price list; the file is treated as defective")
        counts: Counter[str] = Counter()
        records: list[LlmPriceRecord] = []
        for key, fields in offerings.items():
            stored = _stored(fields)
            item_hash = _item_hash(stored, is_listed=True)
            status = seen.classify(key, item_hash)
            if status == "unchanged":
                continue
            counts[status] += 1
            revision = seen.mark(key, item_hash, ctx.today, data={
                "fields": {name: value for name, value in stored.items() if value is not None},
            })
            records.append(_record(ctx, key, fields, revision, is_listed=True))
            if len(records) >= EMIT_BATCH_SIZE:
                ctx.emit(DATASET, records)
                records = []
        for key in removed:
            previous = seen.get(key)
            previous_data = previous.data if previous else {}
            stored = {**dict.fromkeys(OPTIONAL_FIELDS), **(previous_data.get("fields") or {}), "model_key": key}
            revision = seen.mark(key, _item_hash(stored, is_listed=False), ctx.today,
                                 data={**previous_data, "delisted": True})
            counts["delisted"] += 1
            records.append(_record(ctx, key, stored, revision, is_listed=False))
        ctx.emit(DATASET, records)
        return {"offerings": len(offerings), "new": counts["new"], "changed": counts["changed"],
                "delisted": counts["delisted"]}


def _record(ctx: RunContext, key: str, fields: dict[str, Any], revision: int, is_listed: bool) -> LlmPriceRecord:
    """Builds the record of an offering.

    Args:
        ctx: The run context.
        key: Model key.
        fields: Stored fields, as parsed or as kept in the seen store.
        revision: Revision from the seen store.
        is_listed: False for the record of an offering that disappeared.

    Returns:
        LlmPriceRecord: The record.
    """
    return LlmPriceRecord(
        **ctx.envelope(DATASET, f"llm-price:{key}", revision=revision, source_id=SOURCE_ID, source_url=PRICES_URL,
                       license=LICENSE, backfilled=False),
        **fields,
        is_listed=is_listed,
    )

"""
Collects the Epoch AI databases of AI models and machine learning hardware and the Epoch Capabilities Index.

Source: data files of Epoch AI at ``https://epoch.ai/data/``, which robots.txt allows. Epoch
AI publishes its data under CC BY 4.0 ("free to use, distribute, and reproduce provided the
source and authors are credited"). The files are:
- ``all_ai_models.csv``: all models of the database, a superset of the notable, frontier
  and large-scale models (column ``Notability criteria``);
- ``ml_hardware.csv``: machine learning accelerators;
- ``benchmark_data.zip``: benchmark results; the collector reads the entry
  ``epoch_capabilities_index/eci_scores.csv`` with the Epoch Capabilities Index (ECI) per model.

Fetch strategy: one conditional request per file. The ETags are kept in the collector cursor
(``etags``); a file that answers HTTP 304 is skipped. A changed file is read in full. A file
that cannot be read (e.g. a missing key column) is reported as an error, and the other files
are still processed; its ETag is not stored, so that it is read again in the next run.

Records: change records per row. The files have no stable id column, so the key of a row is
its model or hardware name without surrounding spaces; the record_id is
``<epoch-model|epoch-hardware|epoch-eci>:<slug of the name>:<first 8 hex digits of the SHA-256
of the name>``. A row is written when it appears and again, with the next revision, when a
stored field other than ``Last modified`` changes, so that edits of notes create no revision.
A row that disappears is written once with ``is_listed`` false and its last known values,
which the seen store keeps. A file that would remove more than half of the listed rows is
treated as defective. Author names, abstracts, notes and links are not stored. The
``Hugging Face developer id`` of a model is kept only if it is an organization on the Hugging
Face Hub (``OwnerClassifier`` of the Hugging Face collector, with its cache); user accounts
are not stored. A model whose id cannot be checked in this run follows in the next run.

Conversions: numbers are float strings (e.g. ``9.4e+26``); empty, non-finite (``Infinity``,
``NaN``) and non-numeric values are stored as null, and a cell with several comma-separated
values counts only if all values are equal. List columns are split at commas, keeping order
and repetitions (e.g. one organization category per organization).

History: the files contain the complete databases; the first run imports all rows, marked as
backfilled by their publication, release or model date.

Schedule: weekly.
"""
import csv
import hashlib
import io
import logging
import math
import re
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

from collectors.base import Collector, CollectorResult, RunContext
from collectors.huggingface import USER, OwnerClassifier
from schema import AiCapabilityScoreRecord, AiModelRecord, MlHardwareRecord, RecordBase
from state import CollectorEntry, content_hash, is_backfilled
from web import BlockedError, FetchError, RateLimitError

logger = logging.getLogger(__name__)

DATASET_MODELS = "ai_models"
DATASET_HARDWARE = "ml_hardware"
DATASET_SCORES = "ai_capability_scores"
DATASETS = (DATASET_MODELS, DATASET_HARDWARE, DATASET_SCORES)
SOURCE_ID = "epoch_ai"
LICENSE = "CC-BY-4.0 (Epoch AI)"
MODELS_URL = "https://epoch.ai/data/all_ai_models.csv"
HARDWARE_URL = "https://epoch.ai/data/ml_hardware.csv"
BENCHMARKS_URL = "https://epoch.ai/data/benchmark_data.zip"
ECI_ENTRY = "epoch_capabilities_index/eci_scores.csv"
# The model file has about 7 MB
FETCH_TIMEOUT_S = 120.0
EMIT_BATCH_SIZE = 500
# A changed file that would remove more than this share of the listed rows is treated as defective
MAX_REMOVED_SHARE = 0.5
MIN_ROWS_FOR_REMOVAL_CHECK = 20
SLUG_MAX_LENGTH = 60
TRUE_VALUES = frozenset({"true", "yes", "checked", "1", "x"})
FALSE_VALUES = frozenset({"false", "no", "unchecked", "0"})
# Fields that are stored but do not create a new revision when only they change
UNTRACKED_FIELDS = frozenset({"epoch_last_modified"})

Converter = Callable[[Optional[str]], Any]


def text_value(raw: Optional[str]) -> Optional[str]:
    """Returns a cell without surrounding whitespace; None if it is empty."""
    value = (raw or "").strip()
    return value or None


def list_value(raw: Optional[str]) -> list[str]:
    """Splits a cell at commas, keeping order and repeated values; empty parts are dropped."""
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def number_value(raw: Optional[str]) -> Optional[float]:
    """Parses a numeric cell.

    Args:
        raw: The cell, e.g. ``9.4e+26``.

    Returns:
        Optional[float]: The value; None for empty, non-finite (Infinity, NaN) and
        non-numeric cells. A cell with several comma-separated values (as in some lookup
        columns, e.g. ``929000,929000``) gives the value only if all values are equal.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        values = {float(part) for part in text.split(",")}
    except ValueError:
        return None
    if len(values) != 1:
        return None
    value = values.pop()
    return value if math.isfinite(value) else None


def date_value(raw: Optional[str]) -> Optional[date]:
    """Parses a date cell (``YYYY-MM-DD``, optionally followed by a time); None if it is empty or invalid."""
    match = re.match(r"\d{4}-\d{2}-\d{2}", (raw or "").strip())
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(0))
    except ValueError:
        return None


def datetime_value(raw: Optional[str]) -> Optional[datetime]:
    """Parses a timestamp cell such as ``2026-10-23 21:54:37+00:00`` as UTC time; None if it is empty or invalid."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def bool_value(raw: Optional[str]) -> Optional[bool]:
    """Parses a yes/no or checkbox cell (Yes, No, True, checked); None if it is empty or unknown."""
    text = (raw or "").strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


ColumnSpec = tuple[str, str, Converter]

MODEL_COLUMNS: tuple[ColumnSpec, ...] = (
    ("organization", "Organization", text_value),
    ("organization_categories", "Organization categorization", list_value),
    ("countries", "Country (of organization)", list_value),
    ("publication_date", "Publication date", date_value),
    ("domains", "Domain", list_value),
    ("tasks", "Task", list_value),
    ("notability_criteria", "Notability criteria", list_value),
    ("frontier_model", "Frontier model", bool_value),
    ("parameters", "Parameters", number_value),
    ("training_compute_flop", "Training compute (FLOP)", number_value),
    ("training_dataset_size", "Training dataset size (total)", number_value),
    ("training_time_hours", "Training time (hours)", number_value),
    ("training_hardware", "Training hardware", list_value),
    ("hardware_quantity", "Hardware quantity", number_value),
    ("training_compute_cost_usd_2023", "Training compute cost (2023 USD)", number_value),
    ("training_power_draw_watts", "Training power draw (W)", number_value),
    ("finetune_compute_flop", "Finetune compute (FLOP)", number_value),
    ("post_training_compute_flop", "Post-training compute (FLOP)", number_value),
    ("compute_estimation_method", "Training compute estimation method", text_value),
    ("confidence", "Confidence", text_value),
    ("model_accessibility", "Model accessibility", text_value),
    ("open_weights", "Open model weights?", bool_value),
    ("training_code_accessibility", "Training code accessibility", text_value),
    ("base_model", "Base model", text_value),
    ("hugging_face_developer_id", "Hugging Face developer id", text_value),
    ("epoch_last_modified", "Last modified", datetime_value),
)
HARDWARE_COLUMNS: tuple[ColumnSpec, ...] = (
    ("manufacturer", "Manufacturer", text_value),
    ("hardware_type", "Type", text_value),
    ("release_date", "Release date", date_value),
    ("release_price_usd", "Release price (USD)", number_value),
    ("fp64_flops", "FP64 (double precision) performance (FLOP/s)", number_value),
    ("fp32_flops", "FP32 (single precision) performance (FLOP/s)", number_value),
    ("tf32_flops", "TF32 (TensorFloat-32) performance (FLOP/s)", number_value),
    ("fp16_flops", "FP16 (half precision) performance (FLOP/s)", number_value),
    ("tensor_fp16_bf16_flops", "Tensor-FP16/BF16 performance (FLOP/s)", number_value),
    ("fp8_flops", "FP8 performance (FLOP/s)", number_value),
    ("fp4_flops", "FP4 performance (FLOP/s)", number_value),
    ("int8_ops", "INT8 performance (OP/s)", number_value),
    ("int4_ops", "INT4 performance (OP/s)", number_value),
    ("memory_bytes", "Memory (bytes)", number_value),
    ("memory_bandwidth_bytes_per_s", "Memory bandwidth (byte/s)", number_value),
    ("intranode_bandwidth_bytes_per_s", "Intranode bandwidth (byte/s)", number_value),
    ("internode_bandwidth_bits_per_s", "Internode bandwidth (bit/s)", number_value),
    ("tdp_watts", "TDP (W)", number_value),
    ("foundry", "Foundry", text_value),
    ("process_size_nm", "Process size (nm)", number_value),
    ("transistors_millions", "Transistors (millions)", number_value),
    ("die_size_mm2", "Die Size (mm^2)", number_value),
    ("ml_models", "ML models", list_value),
    ("epoch_last_modified", "Last modified", datetime_value),
)
SCORE_COLUMNS: tuple[ColumnSpec, ...] = (
    ("display_name", "Display name", text_value),
    ("eci", "eci", number_value),
    ("eci_ci_low", "eci_ci_low", number_value),
    ("eci_ci_high", "eci_ci_high", number_value),
    ("model_date", "date", date_value),
    ("organization", "Organization", text_value),
    ("country", "Country (of organization)", text_value),
    ("model_accessibility", "Model accessibility", text_value),
    ("accessibility_group", "Accessibility group", text_value),
)


@dataclass(frozen=True)
class FileSpec:
    """A data file of Epoch AI and its mapping to a dataset.

    Attributes:
        dataset (str): Name of the dataset.
        url (str): URL of the file.
        record_prefix (str): First part of the record_id.
        model (type[RecordBase]): Record model of the dataset.
        key_column (str): Column with the name of the row; the file fails without it.
        key_field (str): Record field of the name.
        columns (tuple[ColumnSpec, ...]): ``(field, column, converter)`` per stored column;
            a missing column gives empty values.
        date_field (str): Field whose date decides whether a row is backfilled.
        zip_entry (Optional[str]): For a zip archive, the path suffix of the CSV entry.
        required (tuple[str, ...]): Fields without which a row is skipped.
    """
    dataset: str
    url: str
    record_prefix: str
    model: type[RecordBase]
    key_column: str
    key_field: str
    columns: tuple[ColumnSpec, ...]
    date_field: str
    zip_entry: Optional[str] = None
    required: tuple[str, ...] = ()

    def empty_fields(self) -> dict[str, Any]:
        """Returns the stored fields with empty values (None or an empty list)."""
        return {name: [] if convert is list_value else None for name, _column, convert in self.columns}


FILES: tuple[FileSpec, ...] = (
    FileSpec(dataset=DATASET_MODELS, url=MODELS_URL, record_prefix="epoch-model", model=AiModelRecord,
             key_column="Model", key_field="model_name", columns=MODEL_COLUMNS, date_field="publication_date"),
    FileSpec(dataset=DATASET_HARDWARE, url=HARDWARE_URL, record_prefix="epoch-hardware", model=MlHardwareRecord,
             key_column="Hardware name", key_field="hardware_name", columns=HARDWARE_COLUMNS,
             date_field="release_date"),
    FileSpec(dataset=DATASET_SCORES, url=BENCHMARKS_URL, record_prefix="epoch-eci", model=AiCapabilityScoreRecord,
             key_column="Model", key_field="model_name", columns=SCORE_COLUMNS, date_field="model_date",
             zip_entry=ECI_ENTRY, required=("eci",)),
)


@dataclass
class ParsedFile:
    """The rows of a file.

    Attributes:
        rows (dict[str, dict[str, Any]]): Stored fields per row, keyed by the name.
        duplicates (int): Rows whose name occurred before; the first row is kept.
        skipped (int): Rows without a name or without a required value.
    """
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    duplicates: int = 0
    skipped: int = 0


def slug(name: str) -> str:
    """Returns a readable ASCII form of a name for record ids.

    Args:
        name: Model or hardware name.

    Returns:
        str: Lower-case letters, digits and hyphens, at most SLUG_MAX_LENGTH characters.
    """
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return value[:SLUG_MAX_LENGTH].rstrip("-") or "item"


def record_id(prefix: str, name: str) -> str:
    """Builds the record_id of a row from its name.

    Args:
        prefix: First part, e.g. ``epoch-model``.
        name: Model or hardware name (the key of the row).

    Returns:
        str: ``<prefix>:<slug>:<first 8 hex digits of the SHA-256 of the name>``.
    """
    return f"{prefix}:{slug(name)}:{hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]}"


def read_zip_entry(content: bytes, suffix: str) -> bytes:
    """Returns the content of the entry of a zip archive whose path ends with ``suffix``.

    Args:
        content: The archive.
        suffix: Path or path suffix of the entry.

    Returns:
        bytes: Content of the entry; the shortest matching path if several match.

    Raises:
        ValueError: If the archive is invalid or has no such entry.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = [name for name in archive.namelist() if name == suffix or name.endswith("/" + suffix)]
            if not names:
                raise ValueError(f"the archive has no entry {suffix}")
            return archive.read(min(names, key=len))
    except zipfile.BadZipFile as e:
        raise ValueError(f"invalid zip archive: {e}") from e


def parse_rows(text: str, spec: FileSpec) -> ParsedFile:
    """Parses a CSV file of Epoch AI.

    Args:
        text: The CSV content.
        spec: Mapping of the file.

    Returns:
        ParsedFile: The stored fields per row.

    Raises:
        ValueError: If the file has no header or no key column.
    """
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise ValueError("the file is empty")
    reader.fieldnames = [name.strip() for name in reader.fieldnames]
    if spec.key_column not in reader.fieldnames:
        raise ValueError(f"the key column {spec.key_column!r} is missing")
    missing = [column for _name, column, _convert in spec.columns if column not in reader.fieldnames]
    if missing:
        logger.warning("Epoch AI %s: columns not found, stored as empty: %s", spec.dataset, ", ".join(missing))
    parsed = ParsedFile()
    for row in reader:
        key = (row.get(spec.key_column) or "").strip()
        fields = {spec.key_field: key, **{name: convert(row.get(column)) for name, column, convert in spec.columns}}
        if not key or any(fields[name] is None for name in spec.required):
            parsed.skipped += 1
            continue
        if key in parsed.rows:
            parsed.duplicates += 1
            continue
        parsed.rows[key] = fields
    if parsed.duplicates:
        logger.warning("Epoch AI %s: %d rows repeat a name; the first row of each name is kept.", spec.dataset,
                       parsed.duplicates)
    return parsed


def parse_file(content: bytes, spec: FileSpec) -> ParsedFile:
    """Decodes a downloaded file (from the zip archive if needed) and parses its rows.

    Args:
        content: The downloaded bytes.
        spec: Mapping of the file.

    Returns:
        ParsedFile: The stored fields per row.

    Raises:
        ValueError: If the archive, the header or the key column is missing or invalid.
    """
    if spec.zip_entry:
        content = read_zip_entry(content, spec.zip_entry)
    return parse_rows(content.decode("utf-8-sig", errors="replace"), spec)


def _stored(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Returns the fields in JSON-compatible form, with dates and times as ISO strings."""
    return {name: value.isoformat() if isinstance(value, date) else value for name, value in fields.items()}


def _item_hash(stored: Mapping[str, Any], is_listed: bool) -> str:
    """Returns the hash of the tracked fields of a row and its listing state."""
    tracked = {name: value for name, value in stored.items() if name not in UNTRACKED_FIELDS}
    return content_hash({**tracked, "is_listed": is_listed})


def _as_date(value: Any) -> Optional[date]:
    """Returns a date from a date object or an ISO string (as kept in the seen store)."""
    if isinstance(value, date):
        return value
    return date_value(value) if isinstance(value, str) else None


class EpochAiCollector(Collector):
    """Writes the new, changed and removed rows of the Epoch AI model, hardware and ECI files."""
    name = "epoch_ai"
    datasets = DATASETS
    min_days_between_runs = 6

    def __init__(self, files: Sequence[FileSpec] = FILES) -> None:
        """Initializes the collector.

        Args:
            files: The files to read, in this order.
        """
        self._files = tuple(files)

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Downloads every file that changed since the previous run and emits the changed rows.

        Args:
            ctx: The run context.
            entry: Run history of this collector; the cursor holds the ETag per file URL.

        Returns:
            CollectorResult: The updated ETags and counters; an error names the files that failed.
        """
        etags: dict[str, str] = dict(entry.cursor.get("etags") or {})
        counters: dict[str, Any] = {}
        errors: list[str] = []
        complete = True
        for position, spec in enumerate(self._files):
            if ctx.past_deadline():
                counters["deferred_files"] = len(self._files) - position
                complete = False
                break
            headers = {"If-None-Match": etags[spec.url]} if spec.url in etags else None
            try:
                response = ctx.web.get(spec.url, headers=headers, timeout_s=FETCH_TIMEOUT_S)
            except RateLimitError as e:
                logger.warning("Epoch AI rate limit; the remaining files follow in the next run: %s", e)
                counters["deferred_files"] = len(self._files) - position
                complete = False
                break
            except BlockedError as e:
                logger.error("Epoch AI refused the request; no further requests in this run: %s", e)
                errors.append(f"blocked: {e}")
                complete = False
                break
            except FetchError as e:
                logger.error("Epoch AI file %s failed: %s", spec.url, e)
                errors.append(f"{spec.dataset}: {e}")
                complete = False
                continue
            if response.status == 304:
                counters[f"{spec.dataset}_not_modified"] = 1
                continue
            if ctx.web.tdm_reserved(spec.url, response):
                logger.error("Text and data mining is reserved for %s; the file is not used.", spec.url)
                errors.append(f"{spec.dataset}: text and data mining reserved")
                continue
            try:
                parsed = parse_file(response.content, spec)
                finished = self._write(ctx, entry, spec, parsed, counters)
            except (ValueError, csv.Error) as e:
                logger.error("Epoch AI file %s is not usable: %s", spec.url, e)
                errors.append(f"{spec.dataset}: {e}")
                complete = False
                continue
            if not finished:
                complete = False
            elif response.headers.get("etag"):
                etags[spec.url] = response.headers["etag"]
        logger.info("Epoch AI: %s", ", ".join(f"{key}={value}" for key, value in sorted(counters.items())) or
                    "no changed files")
        return CollectorResult(cursor={**entry.cursor, "etags": etags}, counters=counters, complete=complete,
                               error="; ".join(errors) or None)

    def _write(self, ctx: RunContext, entry: CollectorEntry, spec: FileSpec, parsed: ParsedFile,
               counters: dict[str, Any]) -> bool:
        """Emits the new and changed rows of a file and the rows that disappeared from it.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            spec: Mapping of the file.
            parsed: The rows of the file.
            counters: Counters of the run; updated in place.

        Returns:
            bool: True if all rows were processed; False if rows were deferred to the next run.

        Raises:
            ValueError: If the file would remove more than MAX_REMOVED_SHARE of the listed rows.
        """
        rows = parsed.rows
        seen = ctx.seen(spec.dataset)
        listed_before = [key for key in seen.keys() if not seen.get(key).data.get("delisted")]
        removed = [key for key in listed_before if key not in rows]
        if len(listed_before) >= MIN_ROWS_FOR_REMOVAL_CHECK and len(removed) > MAX_REMOVED_SHARE * len(listed_before):
            raise ValueError(f"{len(removed)} of {len(listed_before)} listed rows are missing; "
                             "the file is treated as defective")
        deferred = self._check_developer_ids(ctx, rows, counters) if spec.dataset == DATASET_MODELS else set()
        counts: Counter[str] = Counter()
        records: list[RecordBase] = []
        for key, fields in rows.items():
            if key in deferred:
                continue
            stored = _stored(fields)
            item_hash = _item_hash(stored, is_listed=True)
            status = seen.classify(key, item_hash)
            if status == "unchanged":
                continue
            counts[status] += 1
            revision = seen.mark(key, item_hash, ctx.today, data={
                "fields": {name: value for name, value in stored.items() if value is not None and value != []},
            })
            records.append(self._record(ctx, entry, spec, key, fields, revision, is_listed=True))
            if len(records) >= EMIT_BATCH_SIZE:
                ctx.emit(spec.dataset, records)
                records = []
        for key in removed:
            previous = seen.get(key)
            previous_data = previous.data if previous else {}
            stored = {**spec.empty_fields(), **(previous_data.get("fields") or {}), spec.key_field: key}
            revision = seen.mark(key, _item_hash(stored, is_listed=False), ctx.today,
                                 data={**previous_data, "delisted": True})
            counts["delisted"] += 1
            records.append(self._record(ctx, entry, spec, key, stored, revision, is_listed=False))
        ctx.emit(spec.dataset, records)
        counters.update({f"{spec.dataset}_rows": len(rows), f"{spec.dataset}_new": counts["new"],
                         f"{spec.dataset}_changed": counts["changed"], f"{spec.dataset}_delisted": counts["delisted"]})
        if parsed.duplicates or parsed.skipped:
            counters[f"{spec.dataset}_duplicates"] = parsed.duplicates
            counters[f"{spec.dataset}_skipped_rows"] = parsed.skipped
        return not deferred

    @staticmethod
    def _record(ctx: RunContext, entry: CollectorEntry, spec: FileSpec, key: str, fields: Mapping[str, Any],
                revision: int, is_listed: bool) -> RecordBase:
        """Builds the record of a row.

        Args:
            ctx: The run context.
            entry: Run history of this collector.
            spec: Mapping of the file.
            key: Name of the row.
            fields: Stored fields, as parsed or as kept in the seen store.
            revision: Revision from the seen store.
            is_listed: False for the record of a row that disappeared.

        Returns:
            RecordBase: The record.
        """
        return spec.model(
            **ctx.envelope(spec.dataset, record_id(spec.record_prefix, key), revision=revision, source_id=SOURCE_ID,
                           source_url=spec.url, license=LICENSE,
                           backfilled=is_backfilled(_as_date(fields.get(spec.date_field)), entry.first_run)),
            **fields,
            is_listed=is_listed,
        )

    @staticmethod
    def _check_developer_ids(ctx: RunContext, rows: dict[str, dict[str, Any]], counters: dict[str, Any]) -> set[str]:
        """Removes Hugging Face developer ids that are user accounts.

        Args:
            ctx: The run context.
            rows: Stored fields per model; changed in place.
            counters: Counters of the run; updated in place.

        Returns:
            set[str]: Names of the models whose developer id could not be checked in this run.
        """
        classifier = OwnerClassifier(ctx)
        deferred: set[str] = set()
        users = 0
        for key, fields in rows.items():
            developer = fields.get("hugging_face_developer_id")
            if developer is None:
                continue
            kind = classifier.classify(developer)
            if kind == USER:
                fields["hugging_face_developer_id"] = None
                users += 1
            elif kind is None:
                deferred.add(key)
        classifier.save()
        counters.update({"hf_owner_checks": classifier.requests, "hf_user_ids_left_out": users,
                         f"{DATASET_MODELS}_deferred": len(deferred)})
        if classifier.stopped:
            counters["hf_owner_checks_stopped"] = classifier.stopped
        return deferred

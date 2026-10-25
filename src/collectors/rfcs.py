"""
Collects the metadata of all RFCs from the index of the RFC Editor.

Source: ``rfc-index.xml`` of the RFC Editor, the complete index of the RFC
series (about 14 MB, 2.3 MB compressed). It is a website file: the collector
respects robots.txt and TDMRep, and it sends a conditional request with the
ETag of the previous run (cursor ``etag``), so that an unchanged index
(HTTP 304) is not downloaded again. The index is parsed with defusedxml;
elements are matched by their local name, so that a change of the XML
namespace does not break the parser.

The ``rfcs`` dataset stores every RFC when it appears in the index and again,
with the next revision, when one of its fields changes, e.g. when a later RFC
obsoletes or updates it or when its status changes. Author names and
abstracts are not stored; the number of authors is. The area is the area of
the working group as stated in the index, which is the current area of the
group. Entries of the BCP, FYI and STD subseries and numbers that were not
issued are skipped; series memberships are in ``is_also``. RFCs are not
removed from the index, so the records have no listing flag. The first run
stores the complete series (about 9,850 RFCs). The seen store is split into
shards of 1000 RFC numbers.

The collector runs once a week.
"""
import logging
from collections.abc import Iterator
from datetime import MAXYEAR, MINYEAR, date
from typing import Any, Optional
from xml.etree.ElementTree import Element, ParseError

import defusedxml.ElementTree as SafeET

from collectors.base import Collector, CollectorResult, RunContext
from schema import RfcRecord
from state import CollectorEntry, content_hash, is_backfilled
from web import FetchError, RateLimitError

logger = logging.getLogger(__name__)

DATASET = "rfcs"
SOURCE_ID = "rfc_editor_index"
# rfc-editor.org states no license for the index; the IETF Trust licenses the material it holds
LICENSE = "IETF Trust (RFC Editor index metadata)"
INDEX_URL = "https://www.rfc-editor.org/rfc-index.xml"
INFO_URL = "https://www.rfc-editor.org/info/rfc{number}"
TIMEOUT_S = 120.0
RFC_PREFIX = "RFC"
NO_WORKING_GROUP = "NON WORKING GROUP"
MONTHS = {name: number for number, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"), start=1)}
# Relation elements of an rfc-entry and the record fields they fill
RELATIONS = {"obsoletes": "obsoletes", "obsoleted-by": "obsoleted_by", "updates": "updates",
             "updated-by": "updated_by", "is-also": "is_also"}
SHARD_SIZE = 1000


def local_name(tag: Any) -> str:
    """Returns the tag of an element without its namespace."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(element: Element, name: str) -> Iterator[Element]:
    """Yields the direct children of an element with a local name."""
    return (child for child in element if local_name(child.tag) == name)


def _child(element: Element, name: str) -> Optional[Element]:
    """Returns the first direct child of an element with a local name."""
    return next(_children(element, name), None)


def _text(element: Optional[Element]) -> Optional[str]:
    """Returns the text of an element with collapsed whitespace, or None if it is missing or empty."""
    if element is None or element.text is None:
        return None
    text = " ".join(element.text.split())
    return text or None


def _child_text(element: Element, name: str) -> Optional[str]:
    """Returns the text of the first direct child with a local name."""
    return _text(_child(element, name))


def _doc_ids(element: Optional[Element]) -> list[str]:
    """Returns the ``doc-id`` texts of a relation element, e.g. ``["RFC2068"]``."""
    if element is None:
        return []
    return [text for text in (_text(child) for child in _children(element, "doc-id")) if text]


def rfc_fields(element: Element) -> Optional[dict[str, Any]]:
    """Returns the stored fields of an ``rfc-entry``.

    Args:
        element: The ``rfc-entry`` element.

    Returns:
        Optional[dict]: The fields of an ``RfcRecord`` without the common fields;
        None if the entry has no valid number, title or publication month.
    """
    doc_id = _child_text(element, "doc-id") or ""
    number_text = doc_id[len(RFC_PREFIX):]
    date_element = _child(element, "date")
    month = MONTHS.get(_child_text(date_element, "month") or "") if date_element is not None else None
    year_text = _child_text(date_element, "year") if date_element is not None else None
    title = _child_text(element, "title")
    # Any year that is a valid date; the index has RFCs dated before RFC 1 (e.g. RFC 31, February 1968)
    year = int(year_text) if year_text and year_text.isdigit() else 0
    if not doc_id.startswith(RFC_PREFIX) or not number_text.isdigit() or month is None or title is None \
            or not MINYEAR <= year <= MAXYEAR:
        return None
    page_count = _child_text(element, "page-count")
    keywords_element = _child(element, "keywords")
    keywords = [] if keywords_element is None else [
        text for text in (_text(child) for child in _children(keywords_element, "kw")) if text]
    working_group = _child_text(element, "wg_acronym")
    fields: dict[str, Any] = {
        "rfc_number": int(number_text),
        "title": title,
        "publication_year": year,
        "publication_month": month,
        "page_count": int(page_count) if page_count and page_count.isdigit() else None,
        "author_count": sum(1 for _ in _children(element, "author")),
        "current_status": _child_text(element, "current-status") or "UNKNOWN",
        "publication_status": _child_text(element, "publication-status") or "UNKNOWN",
        "stream": _child_text(element, "stream"),
        "area": _child_text(element, "area"),
        "working_group": None if working_group == NO_WORKING_GROUP else working_group,
        "keywords": keywords,
    }
    for element_name, field_name in RELATIONS.items():
        fields[field_name] = _doc_ids(_child(element, element_name))
    fields.update({
        "draft": _child_text(element, "draft"),
        "has_errata": _child(element, "errata-url") is not None,
        "doi": _child_text(element, "doi"),
    })
    return fields


def parse_index(content: bytes) -> tuple[list[dict[str, Any]], int]:
    """Parses the RFC index.

    Args:
        content: The XML document.

    Returns:
        tuple[list[dict], int]: The fields of every valid ``rfc-entry`` in document order, and
        the number of ``rfc-entry`` elements that were skipped as invalid.

    Raises:
        ParseError: If the document is not well-formed XML.
        ValueError: If defusedxml rejects the document (DTDs, entities or external references).
    """
    root = SafeET.fromstring(content)
    rfcs: list[dict[str, Any]] = []
    invalid = 0
    for element in _children(root, "rfc-entry"):
        fields = rfc_fields(element)
        if fields is None:
            invalid += 1
            logger.warning("Skipped an invalid rfc-entry: %s", _child_text(element, "doc-id"))
            continue
        rfcs.append(fields)
    return rfcs, invalid


def shard_of(number: int) -> str:
    """Returns the seen-store shard of an RFC, e.g. ``09000`` for RFC 9000 to 9999."""
    return f"{number // SHARD_SIZE * SHARD_SIZE:05d}"


class RfcsCollector(Collector):
    """Writes new and changed RFCs from the index of the RFC Editor."""
    name = "rfcs"
    datasets = (DATASET,)
    min_days_between_runs = 6

    def collect(self, ctx: RunContext, entry: CollectorEntry) -> CollectorResult:
        """Fetches the index if it changed and emits a record for every new or changed RFC.

        Args:
            ctx: The run context.
            entry: Run history of this collector; the cursor holds the ETag of the last processed index.

        Returns:
            CollectorResult: The new cursor and the counters.
        """
        counters: dict[str, Any] = {"index": "unchanged", "rfcs": 0, "invalid": 0, "new": 0, "changed": 0}
        etag = entry.cursor.get("etag")
        if ctx.web.tdm_reserved(INDEX_URL):
            logger.error("RFC index not read: text and data mining reserved for %s", INDEX_URL)
            return CollectorResult(counters=counters, error=f"text and data mining reserved for {INDEX_URL}")
        try:
            response = ctx.web.get(INDEX_URL, headers={"If-None-Match": etag} if etag else None, timeout_s=TIMEOUT_S)
        except RateLimitError as e:
            logger.warning("RFC index rate limited (%s); it is fetched in the next run.", e)
            return CollectorResult(counters=counters, complete=False)
        except FetchError as e:
            logger.error("RFC index failed: %s", e)
            return CollectorResult(counters=counters, complete=False, error=f"RFC index failed: {e}")
        if response.status == 304:
            logger.info("RFC index unchanged since the previous run.")
            return CollectorResult(counters=counters)
        if ctx.web.tdm_reserved(response.url, response):
            logger.error("RFC index not processed: text and data mining reserved for %s", INDEX_URL)
            return CollectorResult(counters=counters, error=f"text and data mining reserved for {INDEX_URL}")
        try:
            rfcs, invalid = parse_index(response.content)
        except (ParseError, ValueError) as e:
            logger.error("RFC index is not valid XML: %s", e)
            return CollectorResult(counters=counters, complete=False, error=f"RFC index is not valid XML: {e}")
        counters.update({"index": "changed", "rfcs": len(rfcs), "invalid": invalid})
        if not rfcs:
            logger.error("RFC index contains no valid rfc-entry.")
            return CollectorResult(counters=counters, complete=False, error="RFC index contains no valid rfc-entry")
        records: list[RfcRecord] = []
        for fields in rfcs:
            number = fields["rfc_number"]
            store = ctx.seen(DATASET, shard_of(number))
            item_hash = content_hash(fields)
            status = store.classify(str(number), item_hash)
            if status == "unchanged":
                continue
            counters[status] += 1
            revision = store.mark(str(number), item_hash, ctx.today)
            published = date(fields["publication_year"], fields["publication_month"], 1)
            records.append(RfcRecord(
                **ctx.envelope(DATASET, f"rfc:{number}", revision=revision, source_id=SOURCE_ID,
                               source_url=INFO_URL.format(number=number), license=LICENSE,
                               backfilled=is_backfilled(published, entry.first_run)),
                **fields,
            ))
        ctx.emit(DATASET, records)
        cursor = {key: value for key, value in entry.cursor.items() if key != "etag"}
        new_etag = response.headers.get("etag")
        if new_etag:
            cursor["etag"] = new_etag
        logger.info("RFC index: %d RFCs, %d new, %d changed.", len(rfcs), counters["new"], counters["changed"])
        return CollectorResult(cursor=cursor, counters=counters)

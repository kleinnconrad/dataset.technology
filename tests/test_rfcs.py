"""Tests of the collector of the RFC Editor index."""
from datetime import datetime, timezone
from typing import Optional

import httpx

from collectors.rfcs import INDEX_URL, LICENSE, RfcsCollector, parse_index
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

INDEX = (FIXTURES_DIR / "rfcs" / "rfc-index.xml").read_bytes()
LATER = datetime(2026, 11, 9, 2, 17, tzinfo=timezone.utc)
NEW_RFC = """  <rfc-entry>
    <doc-id>RFC10099</doc-id>
    <title>QUIC Version 3</title>
    <author>
      <name>A. Example</name>
    </author>
    <date>
      <month>November</month>
      <year>2026</year>
    </date>
    <format>
      <file-format>HTML</file-format>
    </format>
    <page-count>40</page-count>
    <obsoletes>
      <doc-id>RFC9000</doc-id>
    </obsoletes>
    <current-status>PROPOSED STANDARD</current-status>
    <publication-status>PROPOSED STANDARD</publication-status>
    <stream>IETF</stream>
    <area>wit</area>
    <wg_acronym>quic</wg_acronym>
    <doi>10.17487/RFC10099</doi>
  </rfc-entry>
  <std-entry>"""


def _capture_if_none_match(fake_web) -> list[Optional[str]]:
    """Records the If-None-Match header of the index requests; call before make_ctx."""
    captured: list[Optional[str]] = []
    original = fake_web.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == INDEX_URL:
            captured.append(request.headers.get("if-none-match"))
        return original(request)

    fake_web.handler = handler
    return captured


def test_first_run_stores_every_rfc_and_skips_the_subseries(fake_web, make_ctx) -> None:
    fake_web.add(INDEX_URL, INDEX, content_type="application/xml", headers={"etag": '"index-1"'})
    ctx = make_ctx()

    result = RfcsCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    records = {r["rfc_number"]: r for r in read_dataset(ctx.settings.output_dir, "rfcs")}
    assert sorted(records) == [31, 1149, 2616, 8164, 9000, 9920, 9948, 9998, 10023, 10032, 10065]
    assert result.cursor == {"etag": '"index-1"'} and result.counters["new"] == 11 and result.complete
    http = records[2616]
    assert http["record_id"] == "rfc:2616" and http["revision"] == 0 and http["license"] == LICENSE
    assert http["source_url"] == "https://www.rfc-editor.org/info/rfc2616" and http["backfilled"] is True
    assert {key: http[key] for key in ("title", "publication_year", "publication_month", "page_count",
                                       "author_count", "current_status", "publication_status", "stream", "area",
                                       "working_group", "draft", "has_errata", "doi")} == {
        "title": "Hypertext Transfer Protocol -- HTTP/1.1", "publication_year": 1999, "publication_month": 6,
        "page_count": 176, "author_count": 7, "current_status": "DRAFT STANDARD",
        "publication_status": "DRAFT STANDARD", "stream": "IETF", "area": "app", "working_group": "http",
        "draft": "draft-ietf-http-v11-spec-rev-06", "has_errata": True, "doi": "10.17487/RFC2616"}
    assert http["keywords"] == ["HTTP", "Hypertext Transfer Protocol", "World Wide Web", "WWW", "hypermedia"]
    assert http["obsoletes"] == ["RFC2068"] and http["updates"] == [] and http["is_also"] == []
    assert http["obsoleted_by"] == ["RFC7230", "RFC7231", "RFC7232", "RFC7233", "RFC7234", "RFC7235"]
    assert http["updated_by"] == ["RFC2817", "RFC5785", "RFC6266", "RFC6585"]
    quic = records[9000]
    assert quic["title"] == "QUIC: A UDP-Based Multiplexed and Secure Transport" and quic["author_count"] == 2
    avian = records[1149]
    assert avian["publication_month"] == 4 and avian["area"] is None and avian["working_group"] is None
    assert avian["has_errata"] is False and avian["stream"] == "INDEPENDENT" and avian["draft"] is None
    assert avian["updated_by"] == ["RFC2549", "RFC6214"]
    assert records[8164]["current_status"] == "HISTORIC" and records[8164]["publication_status"] == "EXPERIMENTAL"
    assert records[9920]["stream"] == "Editorial" and len(records[9920]["updates"]) == 11
    assert records[9948]["keywords"] == [] and records[9998]["stream"] == "IAB"
    assert records[10023]["working_group"] is None and records[10023]["stream"] == "IETF"
    assert records[10032]["stream"] == "IRTF" and records[10065]["author_count"] == 4
    early = records[31]
    assert (early["publication_year"], early["publication_month"], early["stream"]) == (1968, 2, "Legacy")
    assert early["current_status"] == "UNKNOWN" and early["keywords"] == [] and early["has_errata"] is True
    assert not {"abstract", "authors", "author_names"} & set(http)
    shards = sorted(path.stem for path in (ctx.settings.state_dir / "seen" / "rfcs").glob("*.json"))
    assert shards == ["00000", "01000", "02000", "08000", "09000", "10000"]


def test_unchanged_index_is_not_processed_and_changes_create_revisions(fake_web, make_ctx) -> None:
    captured = _capture_if_none_match(fake_web)
    fake_web.add(INDEX_URL, INDEX, content_type="application/xml", headers={"etag": '"index-1"'})
    ctx = make_ctx()
    first = RfcsCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    entry = CollectorEntry(first_run=ctx.today, cursor=first.cursor)

    fake_web.add(INDEX_URL, b"", status=304)
    unchanged = RfcsCollector().collect(make_ctx(), entry)
    changed_index = INDEX.decode("utf-8").replace(
        "<draft>draft-ietf-quic-transport-34</draft>",
        "<draft>draft-ietf-quic-transport-34</draft>\n    <obsoleted-by>\n      <doc-id>RFC10099</doc-id>\n"
        "    </obsoleted-by>").replace("  <std-entry>", NEW_RFC)
    fake_web.add(INDEX_URL, changed_index, content_type="application/xml", headers={"etag": '"index-2"'})
    changed = RfcsCollector().collect(make_ctx(started=LATER), entry)

    assert unchanged.cursor is None and unchanged.counters["index"] == "unchanged"
    assert changed.counters["new"] == 1 and changed.counters["changed"] == 1
    assert changed.cursor == {"etag": '"index-2"'}
    assert captured == [None, '"index-1"', '"index-1"']
    records = read_dataset(ctx.settings.output_dir, "rfcs")
    assert len(records) == 13
    quic = next(r for r in records if r["record_id"] == "rfc:9000" and r["revision"] == 1)
    assert quic["obsoleted_by"] == ["RFC10099"]
    new = next(r for r in records if r["record_id"] == "rfc:10099")
    assert new["revision"] == 0 and new["backfilled"] is False and new["obsoletes"] == ["RFC9000"]
    assert new["keywords"] == [] and new["has_errata"] is False


def test_invalid_entries_are_skipped_and_the_namespace_does_not_matter() -> None:
    text = INDEX.decode("utf-8")
    damaged = text.replace("<month>April</month>\n      <day>1</day>\n      <year>1990</year>",
                           "<month>Avril</month>\n      <year>1990</year>").replace(
        "<doc-id>RFC9948</doc-id>", "<doc-id>RFC-NINE</doc-id>")
    old_namespace = text.replace('xmlns="https://www.rfc-editor.org/rfc-index"',
                                 'xmlns="http://www.rfc-editor.org/rfc-index"')

    rfcs, invalid = parse_index(damaged.encode("utf-8"))
    same, none_invalid = parse_index(old_namespace.encode("utf-8"))

    assert invalid == 2 and len(rfcs) == 9
    assert none_invalid == 0 and same == parse_index(INDEX)[0]


def test_failures_are_errors_and_keep_the_cursor(fake_web, make_ctx) -> None:
    fake_web.add(INDEX_URL, b"", status=500)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today, cursor={"etag": '"index-1"'})

    failed = RfcsCollector().collect(ctx, entry)
    fake_web.add(INDEX_URL, b"<rfc-index><rfc-entry>", content_type="application/xml")
    malformed = RfcsCollector().collect(make_ctx(), entry)
    fake_web.add(INDEX_URL, b"<!DOCTYPE x [<!ENTITY a 'b'>]><rfc-index>&a;</rfc-index>", content_type="application/xml")
    entities = RfcsCollector().collect(make_ctx(), entry)

    for result in (failed, malformed, entities):
        assert result.cursor is None and not result.complete and result.error
    assert "HTTP 500" in (failed.error or "") and "not valid XML" in (malformed.error or "")
    assert read_dataset(ctx.settings.output_dir, "rfcs") == []


def test_tdm_reservation_prevents_the_download(fake_web, make_ctx) -> None:
    fake_web.add("https://www.rfc-editor.org/.well-known/tdmrep.json",
                 [{"location": "/rfc-index*", "tdm-reservation": 1}])
    fake_web.add(INDEX_URL, INDEX, content_type="application/xml")
    ctx = make_ctx()

    result = RfcsCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert "text and data mining reserved" in (result.error or "")
    assert INDEX_URL not in fake_web.requested

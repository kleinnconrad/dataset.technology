"""Tests of the LiteLLM price list collector."""
import json
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import pytest

from collectors.llm_prices import LICENSE, PRICES_URL, LlmPricesCollector, price_per_million_tokens
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

LATER = datetime(2026, 10, 27, 2, 17, tzinfo=timezone.utc)
THIRD = datetime(2026, 10, 28, 2, 17, tzinfo=timezone.utc)
FOURTH = datetime(2026, 10, 29, 2, 17, tzinfo=timezone.utc)


class EtagServer:
    """Serves the price list with an ETag and answers a matching conditional request with HTTP 304.

    Must be created before the run context, which captures the request handler.
    """

    def __init__(self, fake_web) -> None:
        self.fake_web = fake_web
        self.etag: Optional[str] = None
        self.sent_etag: Optional[str] = None
        handler = fake_web.handler

        def conditional_handler(request: httpx.Request) -> httpx.Response:
            self.sent_etag = request.headers.get("if-none-match")
            if self.sent_etag is not None and self.sent_etag == self.etag:
                return httpx.Response(304, headers={"etag": self.etag})
            return handler(request)

        fake_web.handler = conditional_handler

    def serve(self, data: dict[str, Any], etag: str) -> None:
        self.etag = etag
        self.fake_web.add(PRICES_URL, json.dumps(data).encode("utf-8"), content_type="text/plain; charset=utf-8",
                          headers={"etag": etag})


def _prices() -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / "llm_prices" / "model_prices.json").read_text(encoding="utf-8"))


def _records_of_day(output, day: str) -> list[dict]:
    return [record for record in read_dataset(output, "llm_prices") if record["observed_at"].startswith(day)]


def test_first_run_writes_the_offerings_of_the_kept_modes(fake_web, make_ctx) -> None:
    server = EtagServer(fake_web)
    server.serve(_prices(), '"v1"')
    ctx = make_ctx()

    result = LlmPricesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    records = {r["model_key"]: r for r in read_dataset(ctx.settings.output_dir, "llm_prices")}
    # sample_spec, the image generation entry and the entry without provider are skipped
    assert set(records) == {"gpt-4o", "claude-haiku-4-5", "text-embedding-3-small", "gpt-3.5-turbo-instruct", "o1-pro",
                            "bedrock/ap-northeast-1/1-month-commitment/anthropic.claude-v2:1",
                            "example-provider/invalid-values"}
    gpt4o = records["gpt-4o"]
    assert gpt4o["record_id"] == "llm-price:gpt-4o" and gpt4o["revision"] == 0 and gpt4o["is_listed"] is True
    assert gpt4o["provider"] == "openai" and gpt4o["mode"] == "chat" and gpt4o["license"] == LICENSE
    assert gpt4o["input_price_usd_per_mtok"] == 2.5 and gpt4o["output_price_usd_per_mtok"] == 10.0
    assert gpt4o["cached_input_price_usd_per_mtok"] == 1.25
    assert gpt4o["batch_input_price_usd_per_mtok"] == 1.25 and gpt4o["batch_output_price_usd_per_mtok"] == 5.0
    assert gpt4o["max_input_tokens"] == 128000 and gpt4o["max_output_tokens"] == 16384
    assert gpt4o["supports_function_calling"] is True and gpt4o["supports_vision"] is True
    assert gpt4o["supports_prompt_caching"] is True and gpt4o["supports_reasoning"] is None
    assert gpt4o["supports_web_search"] is None and gpt4o["deprecation_date"] is None
    assert gpt4o["pricing_page"] == "https://developers.openai.com/api/docs/pricing"
    assert gpt4o["source_url"] == PRICES_URL and gpt4o["backfilled"] is False
    claude = records["claude-haiku-4-5"]
    # 1e-07 per token is 0.1 per million tokens, without binary rounding noise
    assert claude["cached_input_price_usd_per_mtok"] == 0.1 and claude["supports_reasoning"] is True
    embedding = records["text-embedding-3-small"]
    assert embedding["mode"] == "embedding" and embedding["input_price_usd_per_mtok"] == 0.02
    # No fallback to the legacy max_tokens
    assert embedding["output_price_usd_per_mtok"] == 0.0 and embedding["max_output_tokens"] is None
    instruct = records["gpt-3.5-turbo-instruct"]
    assert instruct["mode"] == "completion" and instruct["deprecation_date"] == "2026-09-28"
    assert instruct["provider"] == "text-completion-openai" and instruct["input_price_usd_per_mtok"] == 1.5
    o1 = records["o1-pro"]
    assert o1["mode"] == "responses" and o1["input_price_usd_per_mtok"] == 150.0
    assert o1["output_price_usd_per_mtok"] == 600.0 and o1["batch_output_price_usd_per_mtok"] == 300.0
    bedrock = records["bedrock/ap-northeast-1/1-month-commitment/anthropic.claude-v2:1"]
    assert bedrock["provider"] == "bedrock" and bedrock["input_price_usd_per_mtok"] is None
    invalid = records["example-provider/invalid-values"]
    assert [invalid[name] for name in ("input_price_usd_per_mtok", "output_price_usd_per_mtok",
                                       "cached_input_price_usd_per_mtok", "max_input_tokens", "max_output_tokens",
                                       "supports_vision", "deprecation_date", "pricing_page")] == [None] * 8
    assert result.cursor == {"etag": '"v1"'} and result.complete
    assert result.counters["new"] == 7 and result.counters["entries_without_provider"] == 1


def test_unchanged_list_answers_304_and_writes_nothing(fake_web, make_ctx) -> None:
    server = EtagServer(fake_web)
    server.serve(_prices(), '"v1"')
    ctx = make_ctx()
    first = LlmPricesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    second = LlmPricesCollector().collect(make_ctx(started=LATER),
                                          CollectorEntry(first_run=ctx.today, cursor=first.cursor))

    assert server.sent_etag == '"v1"'
    assert second.counters == {"not_modified": 1} and second.cursor is None and second.complete
    assert _records_of_day(ctx.settings.output_dir, "2026-10-27") == []


def test_later_runs_write_changed_removed_and_reappearing_offerings_only(fake_web, make_ctx) -> None:
    server = EtagServer(fake_web)
    server.serve(_prices(), '"v1"')
    ctx = make_ctx()
    first = LlmPricesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    changed = _prices()
    changed["gpt-4o"]["input_cost_per_token"] = 2e-06
    # A change of a field that is not stored creates no revision
    changed["claude-haiku-4-5"]["supports_assistant_prefill"] = False
    del changed["o1-pro"]
    server.serve(changed, '"v2"')

    second = LlmPricesCollector().collect(make_ctx(started=LATER),
                                          CollectorEntry(first_run=ctx.today, cursor=first.cursor))

    output = ctx.settings.output_dir
    later = {r["model_key"]: r for r in _records_of_day(output, "2026-10-27")}
    assert {key: (r["revision"], r["is_listed"]) for key, r in later.items()} == {
        "gpt-4o": (1, True), "o1-pro": (1, False)}
    assert later["gpt-4o"]["input_price_usd_per_mtok"] == 2.0
    removed = later["o1-pro"]
    assert removed["input_price_usd_per_mtok"] == 150.0 and removed["mode"] == "responses"
    assert removed["deprecation_date"] == "2026-10-23" and removed["provider"] == "openai"
    assert removed["supports_reasoning"] is True and removed["supports_web_search"] is None
    assert second.cursor == {"etag": '"v2"'} and second.counters["delisted"] == 1

    # The removed offering is written only once
    changed["gpt-4o"]["supports_pdf_input"] = False
    server.serve(changed, '"v3"')
    third = LlmPricesCollector().collect(make_ctx(started=THIRD),
                                         CollectorEntry(first_run=ctx.today, cursor=second.cursor))
    assert _records_of_day(output, "2026-10-28") == [] and third.counters["delisted"] == 0

    # A reappearing offering is written again with the next revision
    changed["o1-pro"] = _prices()["o1-pro"]
    server.serve(changed, '"v4"')
    LlmPricesCollector().collect(make_ctx(started=FOURTH), CollectorEntry(first_run=ctx.today, cursor=third.cursor))
    fourth = _records_of_day(output, "2026-10-29")
    assert [(r["model_key"], r["revision"], r["is_listed"]) for r in fourth] == [("o1-pro", 2, True)]


def test_rate_limit_ends_the_run_as_incomplete(fake_web, make_ctx) -> None:
    fake_web.add(PRICES_URL, "rate limited", status=429, content_type="text/plain")
    ctx = make_ctx()

    result = LlmPricesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.complete is False and result.error is None and result.counters == {"rate_limited": 1}
    assert read_dataset(ctx.settings.output_dir, "llm_prices") == []


def test_list_that_removes_most_offerings_is_treated_as_defective(fake_web, make_ctx) -> None:
    server = EtagServer(fake_web)
    offerings = {f"model-{number}": {"litellm_provider": "example", "mode": "chat", "input_cost_per_token": 1e-06}
                 for number in range(24)}
    server.serve(offerings, '"v1"')
    ctx = make_ctx()
    first = LlmPricesCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    server.serve({key: offerings[key] for key in list(offerings)[:5]}, '"v2"')

    with pytest.raises(ValueError, match="treated as defective"):
        LlmPricesCollector().collect(make_ctx(started=LATER), CollectorEntry(first_run=ctx.today, cursor=first.cursor))
    assert len(read_dataset(ctx.settings.output_dir, "llm_prices")) == 24


def test_price_per_million_tokens() -> None:
    assert price_per_million_tokens(1e-07) == 0.1 and price_per_million_tokens(1.5e-07) == 0.15
    assert price_per_million_tokens(2.5e-06) == 2.5 and price_per_million_tokens(0) == 0.0
    assert price_per_million_tokens(-1e-06) is None and price_per_million_tokens("1e-06") is None
    assert price_per_million_tokens(True) is None and price_per_million_tokens(float("inf")) is None
    assert price_per_million_tokens(None) is None

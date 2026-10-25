"""Tests of the Hugging Face Hub collector."""
import json
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from collectors.huggingface import (
    DATASETS_API_URL,
    LICENSE,
    MODELS_API_URL,
    ORGANIZATION_API_URL,
    TOTALS_PAGE_URLS,
    HuggingFaceCollector,
    parse_repos,
    parse_total_items,
)
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

HF_DIR = FIXTURES_DIR / "huggingface"
ORGANIZATIONS = ("sentence-transformers", "cross-encoder", "pyannote", "Qwen", "google", "huggingface", "Salesforce",
                 "openai")
LATER = datetime(2026, 11, 2, 2, 17, tzinfo=timezone.utc)


def _load(name: str) -> Any:
    return json.loads((HF_DIR / name).read_text(encoding="utf-8"))


def _serve_hub(fake_web, models: Optional[list] = None) -> None:
    """Serves the top lists, the organization overviews and the list pages; user accounts answer 404."""
    fake_web.add(MODELS_API_URL, models if models is not None else _load("models.json"))
    fake_web.add(DATASETS_API_URL, _load("datasets.json"))
    for name in ORGANIZATIONS:
        fake_web.add(ORGANIZATION_API_URL.format(name=name), {"name": name, "fullname": name})
    for kind, url in TOTALS_PAGE_URLS.items():
        fake_web.add_fixture(url, f"huggingface/page_{kind}.html")


def _owner_checks(fake_web) -> int:
    return sum("/api/organizations/" in url for url in fake_web.requested)


def test_first_run_writes_organization_repos_stats_and_totals_without_user_names(fake_web, make_ctx) -> None:
    _serve_hub(fake_web)
    ctx = make_ctx()

    result = HuggingFaceCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    output = ctx.settings.output_dir
    stats = {(r["repo_type"], r["repo_id"]): r for r in read_dataset(output, "huggingface_repo_stats")}
    # Ranks count the repositories of users as well
    assert {key: r["rank"] for key, r in stats.items()} == {
        ("model", "sentence-transformers/all-MiniLM-L6-v2"): 1, ("model", "cross-encoder/ms-marco-MiniLM-L6-v2"): 3,
        ("model", "pyannote/speaker-diarization-3.1"): 4, ("model", "Qwen/Qwen3-0.6B"): 5,
        ("model", "google/electra-base-discriminator"): 7, ("dataset", "huggingface/documentation-images"): 1,
        ("dataset", "Salesforce/wikitext"): 2, ("dataset", "openai/gsm8k"): 4,
    }
    minilm_stats = stats[("model", "sentence-transformers/all-MiniLM-L6-v2")]
    assert minilm_stats["record_id"] == "hf-stats:model:sentence-transformers/all-MiniLM-L6-v2:2026-10-26"
    assert minilm_stats["downloads_30d"] == 191688814 and minilm_stats["downloads_all_time"] == 4148027675
    assert minilm_stats["likes"] == 6013 and minilm_stats["trending_score"] == 21
    assert minilm_stats["backfilled"] is False and minilm_stats["revision"] == 0
    assert minilm_stats["source_url"] == "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2"

    repos = {r["repo_id"]: r for r in read_dataset(output, "huggingface_repos")}
    assert len(repos) == 8
    minilm = repos["sentence-transformers/all-MiniLM-L6-v2"]
    assert minilm["record_id"] == "hf-repo:model:sentence-transformers/all-MiniLM-L6-v2" and minilm["revision"] == 0
    assert minilm["organization"] == "sentence-transformers" and minilm["license"] == LICENSE
    assert minilm["repo_license"] == "apache-2.0" and minilm["gated"] == "false"
    assert minilm["pipeline_tag"] == "sentence-similarity" and minilm["library_name"] == "sentence-transformers"
    assert minilm["parameters"] == 22713728 and minilm["created_at"] == "2022-03-02T23:29:05Z"
    # The base model of a user account is left out; the relation stays
    assert minilm["base_models"] == [] and minilm["base_model_relation"] == "quantized"
    assert minilm["backfilled"] is True
    qwen = repos["Qwen/Qwen3-0.6B"]
    assert qwen["base_models"] == ["Qwen/Qwen3-0.6B-Base"] and qwen["base_model_relation"] == "finetune"
    assert qwen["parameters"] == 751632384
    assert repos["cross-encoder/ms-marco-MiniLM-L6-v2"]["base_models"] == ["cross-encoder/ms-marco-MiniLM-L12-v2"]
    pyannote = repos["pyannote/speaker-diarization-3.1"]
    assert pyannote["gated"] == "auto" and pyannote["repo_license"] == "mit" and pyannote["parameters"] is None
    assert repos["google/electra-base-discriminator"]["pipeline_tag"] is None
    wikitext = repos["Salesforce/wikitext"]
    assert wikitext["repo_type"] == "dataset" and wikitext["repo_license"] == "cc-by-sa-3.0,gfdl"
    assert wikitext["pipeline_tag"] is None and wikitext["library_name"] is None and wikitext["base_models"] == []
    assert wikitext["source_url"] == "https://huggingface.co/datasets/Salesforce/wikitext"
    assert repos["openai/gsm8k"]["gated"] == "manual"

    totals = read_dataset(output, "huggingface_hub_totals")
    assert len(totals) == 1
    assert totals[0]["record_id"] == "hf-totals:2026-10-26"
    assert (totals[0]["models"], totals[0]["datasets"], totals[0]["spaces"]) == (3185420, 1104787, 1508971)
    assert totals[0]["user_repos_in_top_models"] == 2 and totals[0]["user_repos_in_top_datasets"] == 1

    # 11 repository owners and the user account that owns one base model
    assert result.counters["owner_checks"] == 12 and _owner_checks(fake_web) == 12
    assert result.counters["user_repos"] == 3 and result.counters["user_base_models_left_out"] == 1
    assert result.complete and result.error is None
    models_request = next(url for url in fake_web.requested if url.startswith(MODELS_API_URL + "?"))
    assert "sort=downloads" in models_request and "direction=-1" in models_request and "limit=500" in models_request
    assert "expand%5B%5D=baseModels" in models_request and "cardData" not in models_request
    # No user name in any record or state file, including the owner cache
    for path in output.rglob("*.json"):
        assert "example-user" not in path.read_text(encoding="utf-8"), path


def test_next_run_writes_only_changed_attributes_and_uses_the_owner_cache(fake_web, make_ctx) -> None:
    _serve_hub(fake_web)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)
    HuggingFaceCollector().collect(ctx, entry)
    checks_after_first_run = _owner_checks(fake_web)

    models = _load("models.json")
    qwen = next(model for model in models if model["id"] == "Qwen/Qwen3-0.6B")
    qwen["tags"] = [tag for tag in qwen["tags"] if not tag.startswith("license:")] + ["license:mit"]
    qwen["downloads"] += 1000
    _serve_hub(fake_web, models)
    result = HuggingFaceCollector().collect(make_ctx(started=LATER), entry)

    assert _owner_checks(fake_web) == checks_after_first_run and result.counters["owner_checks"] == 0
    output = ctx.settings.output_dir
    repos = read_dataset(output, "huggingface_repos")
    assert len(repos) == 9
    qwen_records = sorted((r for r in repos if r["repo_id"] == "Qwen/Qwen3-0.6B"), key=lambda r: r["revision"])
    assert [(r["revision"], r["repo_license"]) for r in qwen_records] == [(0, "apache-2.0"), (1, "mit")]
    stats = read_dataset(output, "huggingface_repo_stats")
    assert len(stats) == 16
    later_qwen = next(r for r in stats if r["record_id"] == "hf-stats:model:Qwen/Qwen3-0.6B:2026-11-02")
    assert later_qwen["downloads_30d"] == 23834726
    assert {r["record_id"] for r in read_dataset(output, "huggingface_hub_totals")} == {
        "hf-totals:2026-10-26", "hf-totals:2026-11-02"}


def test_rate_limit_stops_the_owner_checks_and_defers_unknown_owners(fake_web, make_ctx) -> None:
    _serve_hub(fake_web)
    fake_web.add(ORGANIZATION_API_URL.format(name="pyannote"), {"error": "rate limited"}, status=429)
    fake_web.fail(TOTALS_PAGE_URLS["spaces"])
    ctx = make_ctx()

    result = HuggingFaceCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.complete is False and result.error is None and result.counters["stopped"] == "rate_limit"
    output = ctx.settings.output_dir
    # Owners checked before the rate limit: sentence-transformers, example-user-b, cross-encoder
    stats = {r["repo_id"] for r in read_dataset(output, "huggingface_repo_stats")}
    assert stats == {"sentence-transformers/all-MiniLM-L6-v2", "cross-encoder/ms-marco-MiniLM-L6-v2"}
    # The owner of the base model of all-MiniLM-L6-v2 is unknown, so its attributes follow in the next run
    repos = {r["repo_id"] for r in read_dataset(output, "huggingface_repos")}
    assert repos == {"cross-encoder/ms-marco-MiniLM-L6-v2"} and result.counters["repos_deferred"] == 1
    totals = read_dataset(output, "huggingface_hub_totals")[0]
    assert totals["user_repos_in_top_models"] is None and totals["user_repos_in_top_datasets"] is None
    assert totals["models"] == 3185420 and totals["spaces"] is None


def test_run_deadline_stops_the_owner_checks(fake_web, make_ctx) -> None:
    _serve_hub(fake_web)
    ctx = make_ctx(run_deadline_minutes=0)

    result = HuggingFaceCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    assert result.complete is False and result.counters["stopped"] == "deadline"
    assert _owner_checks(fake_web) == 0
    output = ctx.settings.output_dir
    assert read_dataset(output, "huggingface_repo_stats") == [] and read_dataset(output, "huggingface_repos") == []
    # Without any known total no totals record is written
    assert read_dataset(output, "huggingface_hub_totals") == [] and result.counters["totals_found"] == 0


def test_parse_repos_keeps_list_positions_and_normalizes_values() -> None:
    items = [
        {"id": "example-org/model-a", "downloads": 100, "trendingScore": 0.9, "gated": False,
         "createdAt": "2025-01-02T03:04:05.000Z", "tags": ["license:mit", "license:apache-2.0", "license:mit"]},
        {"downloads": 90},
        {"id": "example-org/model-a", "downloads": 80},
        {"id": "gpt2", "author": "openai-community", "downloads": 70, "trendingScore": 12, "gated": "manual",
         "safetensors": {"total": 137022720.0}, "baseModels": {"relation": "adapter", "models": [{"id": "x/y"}, {}]}},
    ]

    repos = parse_repos(items, "model")

    assert [(repo.repo_id, repo.rank, repo.author) for repo in repos] == [
        ("example-org/model-a", 1, "example-org"), ("gpt2", 4, "openai-community")]
    first, second = repos
    assert first.trending_score == 0.9 and first.gated == "false" and first.repo_license == "mit,apache-2.0"
    assert first.created_at == datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc) and first.parameters is None
    assert second.trending_score == 12 and second.gated == "manual" and second.parameters == 137022720
    assert second.base_models == ("x/y",) and second.base_model_relation == "adapter"
    with pytest.raises(ValueError):
        parse_repos({"error": "Invalid sort direction"}, "model")


def test_parse_total_items_reads_escaped_plain_and_deferred_forms() -> None:
    assert parse_total_items((HF_DIR / "page_models.html").read_text(encoding="utf-8")) == 3185420
    assert parse_total_items((HF_DIR / "page_datasets.html").read_text(encoding="utf-8")) == 1104787
    assert parse_total_items((HF_DIR / "page_spaces.html").read_text(encoding="utf-8")) == 1508971
    assert parse_total_items('<script>const props = {"numTotalItems": 42};</script>') == 42
    assert parse_total_items("<div data-props=\"{&quot;numTotalItems&quot;:null}\"></div>") is None
    assert parse_total_items("<html><body>Models</body></html>") is None

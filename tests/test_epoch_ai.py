"""Tests of the Epoch AI collector."""
import csv
import io
import zipfile
from datetime import datetime, timezone
from typing import Optional

import httpx

from collectors.epoch_ai import (
    BENCHMARKS_URL,
    FILES,
    HARDWARE_URL,
    LICENSE,
    MODELS_URL,
    EpochAiCollector,
    number_value,
    record_id,
)
from collectors.huggingface import ORGANIZATION_API_URL
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

EPOCH_DIR = FIXTURES_DIR / "epoch_ai"
HF_ORGANIZATIONS = ("deepseek-ai", "zai-org", "LGAI-EXAONE")
LATER = datetime(2026, 11, 2, 2, 17, tzinfo=timezone.utc)
THIRD = datetime(2026, 11, 9, 2, 17, tzinfo=timezone.utc)
FOURTH = datetime(2026, 11, 16, 2, 17, tzinfo=timezone.utc)
SCORES_HEADER = ("Model,Display name,eci,eci_ci_low,eci_ci_high,date,Organization,Country (of organization),"
                 "Model accessibility,Accessibility group,model_versions\n")


class EtagWeb:
    """Serves files with ETags through the fake web and answers matching conditional requests with HTTP 304.

    Must be created before the run context, which captures the request handler.
    """

    def __init__(self, fake_web) -> None:
        self.fake_web = fake_web
        self.etags: dict[str, str] = {}
        self.sent_etags: dict[str, Optional[str]] = {}
        handler = fake_web.handler

        def conditional_handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            sent = request.headers.get("if-none-match")
            self.sent_etags[url] = sent
            if sent is not None and sent == self.etags.get(url):
                fake_web.requested.append(url)
                return httpx.Response(304, headers={"etag": sent})
            return handler(request)

        fake_web.handler = conditional_handler

    def serve(self, url: str, content: bytes, etag: str, content_type: str = "text/csv") -> None:
        self.etags[url] = etag
        self.fake_web.add(url, content, content_type=content_type, headers={"etag": etag})


def _fixture(name: str) -> str:
    return (EPOCH_DIR / name).read_text(encoding="utf-8")


def _zip(scores_csv: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("README.md", "Benchmark data")
        archive.writestr("gpqa_diamond.csv", "Model,mean_score\n")
        archive.writestr("epoch_capabilities_index/eci_scores.csv", scores_csv)
    return buffer.getvalue()


def _edit_csv(text: str, key: str, changes: Optional[dict[str, dict[str, str]]] = None,
              remove: tuple[str, ...] = ()) -> str:
    """Changes cells of rows (by the value of the key column) and removes rows."""
    reader = csv.DictReader(io.StringIO(text, newline=""))
    rows = [row for row in reader if row[key].strip() not in remove]
    for row in rows:
        row.update((changes or {}).get(row[key].strip(), {}))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _serve_files(web: EtagWeb, models: Optional[str] = None, models_etag: str = 'W/"models-1"') -> None:
    web.serve(MODELS_URL, (models if models is not None else _fixture("all_ai_models.csv")).encode(), models_etag)
    web.serve(HARDWARE_URL, _fixture("ml_hardware.csv").encode(), 'W/"hardware-1"')
    web.serve(BENCHMARKS_URL, _zip(_fixture("eci_scores.csv")), '"zip-1"', content_type="application/zip")
    for name in HF_ORGANIZATIONS:
        web.fake_web.add(ORGANIZATION_API_URL.format(name=name), {"name": name})


def _records_of_day(output, dataset: str, day: str) -> list[dict]:
    return [record for record in read_dataset(output, dataset) if record["observed_at"].startswith(day)]


def test_first_run_writes_all_rows_with_converted_values(fake_web, make_ctx) -> None:
    web = EtagWeb(fake_web)
    _serve_files(web)
    ctx = make_ctx()

    result = EpochAiCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    output = ctx.settings.output_dir
    models = {r["model_name"]: r for r in read_dataset(output, "ai_models")}
    # Surrounding spaces of names are removed
    assert len(models) == 8 and "K-EXAONE 2.0" in models
    deepseek = models["DeepSeek V4.1 Flash"]
    assert deepseek["record_id"] == record_id("epoch-model", "DeepSeek V4.1 Flash")
    assert deepseek["record_id"].startswith("epoch-model:deepseek-v4-1-flash:")
    assert deepseek["revision"] == 0 and deepseek["is_listed"] is True and deepseek["license"] == LICENSE
    assert deepseek["parameters"] == 748000000000.0 and deepseek["training_compute_flop"] == 4.32e24
    assert deepseek["training_dataset_size"] == 45000000000000.0
    assert deepseek["frontier_model"] is True and deepseek["open_weights"] is True
    assert deepseek["domains"] == ["Language", "Vision", "Multimodal"]
    assert deepseek["notability_criteria"] == ["Discretionary"]
    assert deepseek["hugging_face_developer_id"] == "deepseek-ai"
    assert deepseek["epoch_last_modified"] == "2026-10-14T05:28:00Z"
    assert deepseek["publication_date"] == "2026-09-09" and deepseek["backfilled"] is True
    kimi = models["Kimi K3.1 Pro"]
    assert kimi["finetune_compute_flop"] is None and kimi["frontier_model"] is None
    assert kimi["open_weights"] is False and kimi["notability_criteria"] == []
    # Published within the grace period before the first run
    assert kimi["backfilled"] is False
    assert models["GLM-5.3-Flash"]["hardware_quantity"] is None
    assert models["DeepSeek V4 Flash 0731"]["training_dataset_size"] == 929000.0
    motif = models["Motif-3"]
    assert motif["hugging_face_developer_id"] is None
    assert motif["training_hardware"] == ["NVIDIA B200"] and motif["training_power_draw_watts"] == 1489975.7958831799
    assert motif["training_compute_cost_usd_2023"] is None and motif["training_time_hours"] == 2400.0
    assert motif["compute_estimation_method"] == "Reported" and motif["hardware_quantity"] == 768.0
    assert models["K-EXAONE 2.0"]["base_model"] == "K-EXAONE"
    assert models["K-EXAONE 2.0"]["training_code_accessibility"] == "Unreleased"
    transformer = models["Transformer"]
    assert transformer["organization"] == "Google Research,Google Brain"
    assert transformer["organization_categories"] == ["Industry", "Industry"]
    assert transformer["countries"] == ["United States of America", "United States of America"]
    assert transformer["notability_criteria"] == ["Highly cited", "Historical significance"]
    assert transformer["compute_estimation_method"] == "Hardware,Third-party estimation"
    assert transformer["training_time_hours"] == 84.0 and transformer["training_power_draw_watts"] == 4180.032869488383
    alexnet = models["AlexNet"]
    assert alexnet["frontier_model"] is True and alexnet["open_weights"] is None and alexnet["model_accessibility"] is None

    hardware = {r["hardware_name"]: r for r in read_dataset(output, "ml_hardware")}
    assert len(hardware) == 3
    gb300 = hardware["NVIDIA GB300 (Blackwell Ultra)"]
    assert gb300["record_id"].startswith("epoch-hardware:nvidia-gb300-blackwell-ultra:")
    assert gb300["manufacturer"] == "NVIDIA" and gb300["hardware_type"] == "GPU" and gb300["foundry"] == "TSMC"
    assert gb300["release_date"] == "2025-08-22" and gb300["release_price_usd"] == 43000.0
    assert gb300["tensor_fp16_bf16_flops"] == 2.5e15 and gb300["fp8_flops"] == 5e15 and gb300["fp4_flops"] == 1.5e16
    assert gb300["fp64_flops"] == 1.3e12 and gb300["fp32_flops"] == 8e13 and gb300["tf32_flops"] == 1.25e15
    assert gb300["int8_ops"] == 1.65e14 and gb300["int4_ops"] == 1.5e16 and gb300["fp16_flops"] is None
    assert gb300["memory_bytes"] == 2.88e11 and gb300["memory_bandwidth_bytes_per_s"] == 8e12
    assert gb300["intranode_bandwidth_bytes_per_s"] == 1.8e12 and gb300["internode_bandwidth_bits_per_s"] is None
    assert gb300["tdp_watts"] == 1400.0 and gb300["process_size_nm"] == 4.0 and gb300["die_size_mm2"] is None
    assert gb300["ml_models"] == ["GPT-5.5", "Inkling", "Inkling-Small"] and gb300["backfilled"] is True
    assert gb300["epoch_last_modified"] == "2026-10-21T00:04:39Z"
    assert hardware["AMD Instinct MI355X"]["ml_models"] == []

    scores = {r["model_name"]: r for r in read_dataset(output, "ai_capability_scores")}
    # The row without a score is skipped
    assert set(scores) == {"Claude Opus 5.5", "GPT-6 Astra", "GPT-6.1 Sol", "Claude Fable 5.1"}
    opus = scores["Claude Opus 5.5"]
    assert opus["record_id"].startswith("epoch-eci:claude-opus-5-5:") and opus["source_url"] == BENCHMARKS_URL
    assert (opus["eci"], opus["eci_ci_low"], opus["eci_ci_high"]) == (168.83, 165.25, 173.95)
    assert opus["model_date"] == "2026-09-22" and opus["organization"] == "Anthropic"
    assert opus["country"] == "United States of America" and opus["model_accessibility"] == "API access"
    assert opus["accessibility_group"] == "Closed weights" and opus["display_name"] == "Claude Opus 5.5"

    assert result.complete and result.error is None
    assert result.cursor == {"etags": {MODELS_URL: 'W/"models-1"', HARDWARE_URL: 'W/"hardware-1"',
                                       BENCHMARKS_URL: '"zip-1"'}}
    assert result.counters["hf_user_ids_left_out"] == 1 and result.counters["ai_capability_scores_skipped_rows"] == 1
    # Authors, abstracts, notes and user accounts are not stored anywhere
    for path in output.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "Author One" not in text and "not stored" not in text and "example-user" not in text, path


def test_later_runs_write_changed_removed_and_reappearing_rows_only(fake_web, make_ctx) -> None:
    web = EtagWeb(fake_web)
    _serve_files(web)
    ctx = make_ctx()
    first = EpochAiCollector().collect(ctx, CollectorEntry(first_run=ctx.today))
    entry = CollectorEntry(first_run=ctx.today, cursor=first.cursor)
    original = _fixture("all_ai_models.csv")
    # A tracked change, an edit of only notes and Last modified, and a removed row
    changed = _edit_csv(original, "Model", changes={
        "Motif-3": {"Parameters": "315000000000.0"},
        "GLM-5.3-Flash": {"Parameters notes": "Edited note.", "Last modified": "2026-10-30 10:00:00+00:00"},
    }, remove=("AlexNet",))
    web.serve(MODELS_URL, changed.encode(), 'W/"models-2"')

    second = EpochAiCollector().collect(make_ctx(started=LATER), entry)

    output = ctx.settings.output_dir
    assert web.sent_etags[HARDWARE_URL] == 'W/"hardware-1"' and web.sent_etags[MODELS_URL] == 'W/"models-1"'
    assert second.counters["ml_hardware_not_modified"] == 1 and second.counters["ai_capability_scores_not_modified"] == 1
    later = {r["model_name"]: r for r in _records_of_day(output, "ai_models", "2026-11-02")}
    assert {name: (r["revision"], r["is_listed"]) for name, r in later.items()} == {
        "Motif-3": (1, True), "AlexNet": (1, False)}
    assert later["Motif-3"]["parameters"] == 315000000000.0
    removed = later["AlexNet"]
    assert removed["record_id"] == record_id("epoch-model", "AlexNet")
    assert removed["frontier_model"] is True and removed["training_compute_flop"] == 4.7e17
    assert removed["notability_criteria"] == ["Highly cited", "Historical significance"]
    assert removed["publication_date"] == "2012-09-30" and removed["backfilled"] is True
    assert removed["epoch_last_modified"] == "2026-01-01T14:02:54Z" and removed["open_weights"] is None
    assert _records_of_day(output, "ml_hardware", "2026-11-02") == []
    assert second.cursor["etags"][MODELS_URL] == 'W/"models-2"' and second.complete

    # The removed row is written only once
    still_removed = _edit_csv(changed, "Model", changes={"Kimi K3.1 Pro": {"Last modified": "2026-11-05 08:00:00+00:00"}})
    web.serve(MODELS_URL, still_removed.encode(), 'W/"models-3"')
    third = EpochAiCollector().collect(make_ctx(started=THIRD), CollectorEntry(first_run=ctx.today,
                                                                               cursor=second.cursor))
    assert _records_of_day(output, "ai_models", "2026-11-09") == [] and third.counters["ai_models_delisted"] == 0

    # A reappearing row is written again with the next revision
    web.serve(MODELS_URL, _edit_csv(original, "Model", changes={"Motif-3": {"Parameters": "315000000000.0"}}).encode(),
              'W/"models-4"')
    EpochAiCollector().collect(make_ctx(started=FOURTH), CollectorEntry(first_run=ctx.today, cursor=third.cursor))
    fourth = _records_of_day(output, "ai_models", "2026-11-16")
    assert [(r["model_name"], r["revision"], r["is_listed"]) for r in fourth] == [("AlexNet", 2, True)]


def test_file_without_key_column_is_reported_and_the_other_files_are_written(fake_web, make_ctx) -> None:
    web = EtagWeb(fake_web)
    _serve_files(web)
    web.serve(HARDWARE_URL, _fixture("ml_hardware.csv").replace("Hardware name,", "Name,", 1).encode(),
              'W/"hardware-2"')
    ctx = make_ctx()

    result = EpochAiCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    output = ctx.settings.output_dir
    assert result.error is not None and "ml_hardware" in result.error and "Hardware name" in result.error
    assert result.complete is False
    assert read_dataset(output, "ml_hardware") == []
    assert len(read_dataset(output, "ai_models")) == 8 and len(read_dataset(output, "ai_capability_scores")) == 4
    assert HARDWARE_URL not in result.cursor["etags"] and MODELS_URL in result.cursor["etags"]


def test_models_with_unchecked_developer_ids_follow_in_the_next_run(fake_web, make_ctx) -> None:
    web = EtagWeb(fake_web)
    _serve_files(web)
    fake_web.add(ORGANIZATION_API_URL.format(name="zai-org"), {"error": "rate limited"}, status=429)
    ctx = make_ctx()

    first = EpochAiCollector().collect(ctx, CollectorEntry(first_run=ctx.today))

    output = ctx.settings.output_dir
    assert first.complete is False and first.error is None and first.counters["hf_owner_checks_stopped"] == "rate_limit"
    assert {r["model_name"] for r in read_dataset(output, "ai_models")} == {
        "Kimi K3.1 Pro", "DeepSeek V4.1 Flash", "DeepSeek V4 Flash 0731", "Transformer", "AlexNet"}
    # The ETag of the model file is not stored, so that the file is read again
    assert MODELS_URL not in first.cursor["etags"] and HARDWARE_URL in first.cursor["etags"]

    fake_web.add(ORGANIZATION_API_URL.format(name="zai-org"), {"name": "zai-org"})
    second = EpochAiCollector().collect(make_ctx(started=LATER), CollectorEntry(first_run=ctx.today,
                                                                               cursor=first.cursor))

    later = _records_of_day(output, "ai_models", "2026-11-02")
    assert {(r["model_name"], r["revision"], r["hugging_face_developer_id"]) for r in later} == {
        ("GLM-5.3-Flash", 0, "zai-org"), ("Motif-3", 0, None), ("K-EXAONE 2.0", 0, "LGAI-EXAONE")}
    assert second.complete and second.cursor["etags"][MODELS_URL] == 'W/"models-1"'


def test_file_that_removes_most_rows_is_treated_as_defective(fake_web, make_ctx) -> None:
    web = EtagWeb(fake_web)
    rows = [f"Model {number},Model {number},{100 + number},,,2026-09-01,Lab,,,,\n" for number in range(24)]
    web.serve(BENCHMARKS_URL, _zip(SCORES_HEADER + "".join(rows)), '"zip-1"', content_type="application/zip")
    collector = EpochAiCollector(files=[spec for spec in FILES if spec.url == BENCHMARKS_URL])
    ctx = make_ctx()
    first = collector.collect(ctx, CollectorEntry(first_run=ctx.today))
    web.serve(BENCHMARKS_URL, _zip(SCORES_HEADER + "".join(rows[:5])), '"zip-2"', content_type="application/zip")

    second = collector.collect(make_ctx(started=LATER), CollectorEntry(first_run=ctx.today, cursor=first.cursor))

    assert second.error is not None and "treated as defective" in second.error
    assert len(read_dataset(ctx.settings.output_dir, "ai_capability_scores")) == 24
    assert second.cursor["etags"][BENCHMARKS_URL] == '"zip-1"'


def test_number_value_and_record_id() -> None:
    assert number_value("9.4e+26") == 9.4e26 and number_value(" 1.5 ") == 1.5
    assert number_value("Infinity") is None and number_value("NaN") is None and number_value("-inf") is None
    assert number_value("") is None and number_value(None) is None and number_value("n/a") is None
    assert number_value("929000,929000") == 929000.0 and number_value("327978752,192851506176") is None
    unicode_id = record_id("epoch-model", "Zhiyun Culture LLM (智云文化大模型)")
    assert unicode_id.startswith("epoch-model:zhiyun-culture-llm:") and len(unicode_id.rsplit(":", 1)[1]) == 8
    assert record_id("epoch-eci", "GPT-4") != record_id("epoch-eci", "GPT 4")

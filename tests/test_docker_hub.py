"""Tests of the Docker Hub collector."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collectors.base import RunContext
from collectors.docker_hub import DockerHubCollector, parse_repository, repository_url
from conftest import FIXTURES_DIR, RUN_STARTED, read_dataset
from settings import Settings
from state import CollectorEntry

DATASET = "docker_image_pulls"
REGISTRY = """
[[technology]]
id = "nginx"
name = "nginx"
category = "web_server"
added = 2026-10-01
packages = ["docker:library/nginx"]

[[technology]]
id = "ollama"
name = "Ollama"
category = "ai_application"
added = 2026-10-01
packages = ["docker:ollama/ollama"]

[[technology]]
id = "unknown_image"
name = "Unknown image"
category = "developer_tool"
added = 2026-10-01
packages = ["docker:library/this-repo-does-not-exist-xyz-123"]
"""
NGINX_URL = "https://hub.docker.com/v2/namespaces/library/repositories/nginx"
OLLAMA_URL = "https://hub.docker.com/v2/namespaces/ollama/repositories/ollama"
UNKNOWN_URL = "https://hub.docker.com/v2/namespaces/library/repositories/this-repo-does-not-exist-xyz-123"
# Docker Hub echoes the IP address of the caller in a response header
CALLER_IP = "203.0.113.7"


def _registry(tmp_path: Path) -> Path:
    path = tmp_path / "technologies.toml"
    path.write_text(REGISTRY, encoding="utf-8")
    return path


def _serve(fake_web) -> None:
    for url, fixture in ((NGINX_URL, "library_nginx.json"), (OLLAMA_URL, "ollama_ollama.json")):
        fake_web.add(url, (FIXTURES_DIR / "docker_hub" / fixture).read_bytes(), content_type="application/json",
                     headers={"x-ratelimit-ip": CALLER_IP, "x-ratelimit-remaining": "163"})
    fake_web.add(UNKNOWN_URL, (FIXTURES_DIR / "docker_hub" / "not_found.json").read_bytes(), status=404,
                 content_type="application/json")


def test_every_run_writes_a_snapshot_per_image(fake_web, make_ctx, tmp_path) -> None:
    _serve(fake_web)
    ctx = make_ctx()
    entry = CollectorEntry(first_run=ctx.today)

    result = DockerHubCollector(_registry(tmp_path)).collect(ctx, entry)

    records = {r["image"]: r for r in read_dataset(ctx.settings.output_dir, DATASET)}
    assert set(records) == {"library/nginx", "ollama/ollama"}
    nginx = records["library/nginx"]
    assert nginx["record_id"] == "docker:library/nginx:2026-10-26"
    assert nginx["technology_id"] == "nginx" and nginx["revision"] == 0 and nginx["backfilled"] is False
    assert nginx["pull_count"] == 13509187123 and nginx["star_count"] == 21410
    assert nginx["last_updated"] == "2026-10-24T19:52:24.781981Z"
    assert nginx["source_url"] == NGINX_URL and nginx["source_id"] == "docker_hub"
    assert nginx["license"] == "Docker Hub API (Docker terms of service)"
    assert records["ollama/ollama"]["pull_count"] == 191631304
    # Response headers are not stored
    stored_text = "".join(path.read_text(encoding="utf-8") for path in (ctx.settings.output_dir / DATASET).rglob("*.json"))
    assert CALLER_IP not in stored_text
    assert result.counters == {"images": 3, "requested": 3, "records": 2, "failed": 1, "deferred": 0}
    assert result.complete and result.error is None

    later = RUN_STARTED + timedelta(days=7)
    DockerHubCollector(_registry(tmp_path)).collect(make_ctx(started=later), entry)

    ids = sorted(r["record_id"] for r in read_dataset(ctx.settings.output_dir, DATASET) if r["image"] == "library/nginx")
    assert ids == ["docker:library/nginx:2026-10-26", "docker:library/nginx:2026-11-02"]


def test_error_when_every_image_fails(fake_web, make_ctx, tmp_path) -> None:
    fake_web.add(UNKNOWN_URL, (FIXTURES_DIR / "docker_hub" / "not_found.json").read_bytes(), status=404,
                 content_type="application/json")
    fake_web.add(NGINX_URL, "<html>maintenance</html>", status=500)
    fake_web.add(OLLAMA_URL, {"name": "ollama"})
    ctx = make_ctx()

    result = DockerHubCollector(_registry(tmp_path)).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert read_dataset(ctx.settings.output_dir, DATASET) == []
    assert result.error == "3 of 3 images failed"


def test_rate_limit_and_block_stop_the_collector(fake_web, make_ctx, tmp_path) -> None:
    _serve(fake_web)
    fake_web.add(OLLAMA_URL, {"message": "too many requests"}, status=429, headers={"retry-after": "1"})
    ctx = make_ctx()

    limited = DockerHubCollector(_registry(tmp_path)).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert UNKNOWN_URL not in fake_web.requested
    assert [r["image"] for r in read_dataset(ctx.settings.output_dir, DATASET)] == ["library/nginx"]
    assert limited.counters["deferred"] == 2
    assert not limited.complete and limited.error is None

    fake_web.add(NGINX_URL, "Forbidden", status=403)
    fake_web.requested.clear()
    blocked = DockerHubCollector(_registry(tmp_path)).collect(make_ctx(), CollectorEntry(first_run=ctx.today))

    assert fake_web.requested == [NGINX_URL]
    assert blocked.error is not None and "refuses automated requests" in blocked.error


def test_collector_stops_at_the_run_deadline(fake_web, tmp_path) -> None:
    _serve(fake_web)
    settings = Settings(output_dir=tmp_path / "data")
    # The deadline passes after the first request
    ctx = RunContext(settings, fake_web.client(), RUN_STARTED,
                     clock=lambda: RUN_STARTED + (timedelta(hours=1) if fake_web.requested else timedelta(0)))

    result = DockerHubCollector(_registry(tmp_path)).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert fake_web.requested == [NGINX_URL]
    assert [r["image"] for r in read_dataset(settings.output_dir, DATASET)] == ["library/nginx"]
    assert result.counters["deferred"] == 2
    assert not result.complete and result.error is None


def test_parse_repository_requires_a_pull_count() -> None:
    assert parse_repository({"pull_count": 5, "star_count": None, "last_updated": None}) == {
        "pull_count": 5, "star_count": None, "last_updated": None}
    assert parse_repository({"pull_count": 5, "last_updated": "2026-10-24T19:52:24Z"})["last_updated"] == datetime(
        2026, 10, 24, 19, 52, 24, tzinfo=timezone.utc)
    assert repository_url("library/postgres") == "https://hub.docker.com/v2/namespaces/library/repositories/postgres"
    for invalid in ({}, {"pull_count": "13509187123"}, {"pull_count": True}, []):
        try:
            parse_repository(invalid)
        except ValueError:
            continue
        raise AssertionError(f"accepted {invalid!r}")

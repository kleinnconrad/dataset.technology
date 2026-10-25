"""Tests of the GitHub repository collector."""
import json
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from collectors.github import (CONTRIBUTORS_URL, LATEST_RELEASE_URL, LICENSE, PARTICIPATION_URL, REPO_URL,
                               GitHubCollector, last_page_number, parse_repository)
from conftest import FIXTURES_DIR, read_dataset
from state import CollectorEntry

DATASET = "github_repositories"
# Link header of the verified contributors response of python/cpython (one contributor per page)
CPYTHON_LINK = (
    '<https://api.github.com/repositories/81598961/contributors?per_page=1&anon=true&page=2>; rel="next", '
    '<https://api.github.com/repositories/81598961/contributors?per_page=1&anon=true&page=4114>; rel="last"'
)


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES_DIR / "github" / name).read_text(encoding="utf-8"))


def _registry(tmp_path: Path, **repos: str) -> Path:
    """Writes a registry with one technology per repository, keyed by the technology id."""
    entries = [f'[[technology]]\nid = "{technology_id}"\nname = "{technology_id}"\ncategory = "developer_tool"\n'
               f'added = 2026-10-01\ngithub_repos = ["{repo}"]\n' for technology_id, repo in repos.items()]
    path = tmp_path / "technologies.toml"
    path.write_text("\n".join(entries), encoding="utf-8")
    return path


def _contributors_url(repo: str) -> str:
    return CONTRIBUTORS_URL.format(repo=repo) + "?per_page=1&anon=true"


def _remaining(value: str) -> dict[str, str]:
    return {"x-ratelimit-remaining": value, "x-ratelimit-reset": "1792922900"}


def _serve_cpython(fake_web, remaining: Optional[list[str]] = None) -> None:
    """Serves the verified responses of python/cpython, with the rate-limit header of each response."""
    remaining = remaining or ["59", "58", "57"]
    fake_web.add(REPO_URL.format(repo="python/cpython"), _fixture("repos_python_cpython.json"),
                 headers=_remaining(remaining[0]))
    fake_web.add(PARTICIPATION_URL.format(repo="python/cpython"), _fixture("stats_participation_python_cpython.json"),
                 headers=_remaining(remaining[1]))
    fake_web.add(_contributors_url("python/cpython"), _fixture("contributors_python_cpython_per_page1_anon.json"),
                 headers={"link": CPYTHON_LINK, **_remaining(remaining[2])})
    fake_web.add(LATEST_RELEASE_URL.format(repo="python/cpython"), _fixture("releases_latest_python_cpython_404.json"),
                 status=404)


def _serve_kubernetes(fake_web) -> None:
    """Serves kubernetes/kubernetes with a release; the statistics are still being computed (HTTP 202)."""
    fake_web.add(REPO_URL.format(repo="kubernetes/kubernetes"), _fixture("repos_kubernetes_kubernetes.json"))
    fake_web.add(PARTICIPATION_URL.format(repo="kubernetes/kubernetes"), {}, status=202)
    # The answer that GitHub gives for repositories whose history is too large for the API (verified for torvalds/linux)
    fake_web.add(_contributors_url("kubernetes/kubernetes"), _fixture("contributors_torvalds_linux_403_too_large.json"),
                 status=403)
    fake_web.add(LATEST_RELEASE_URL.format(repo="kubernetes/kubernetes"),
                 _fixture("releases_latest_kubernetes_kubernetes.json"))


def test_snapshot_record_holds_the_repository_values(fake_web, make_ctx, tmp_path) -> None:
    _serve_cpython(fake_web)
    ctx = make_ctx()

    result = GitHubCollector(_registry(tmp_path, python="python/cpython")).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    [record] = read_dataset(ctx.settings.output_dir, DATASET)
    weekly = _fixture("stats_participation_python_cpython.json")["all"]
    assert record["record_id"] == "github:python/cpython:2026-10-26"
    assert record["revision"] == 0 and record["backfilled"] is False
    assert record["license"] == LICENSE and record["source_id"] == "github_rest_api"
    assert record["source_url"] == "https://api.github.com/repos/python/cpython"
    assert {key: record[key] for key in ("technology_id", "repo", "repo_id", "full_name", "stars", "forks",
                                         "open_issues", "watchers", "size_kb", "primary_language", "repo_license",
                                         "topics", "archived")} == {
        "technology_id": "python", "repo": "python/cpython", "repo_id": 81598961, "full_name": "python/cpython",
        "stars": 77762, "forks": 38005, "open_issues": 9307, "watchers": 1650, "size_kb": 899484,
        "primary_language": "Python", "repo_license": "NOASSERTION", "topics": [], "archived": False,
    }
    assert record["created_at"] == "2017-02-10T19:23:51Z" and record["pushed_at"] == "2026-10-25T07:43:36Z"
    assert record["contributors"] == 4114
    assert record["commits_last_4_weeks"] == 109 + 93 + 83 + 326 == sum(weekly[-4:])
    assert record["commits_last_52_weeks"] == sum(weekly)
    assert record["latest_release_tag"] is None and record["latest_release_at"] is None
    assert result.complete and result.error is None
    assert result.counters == {"repositories": 1, "records": 1, "failed": 0, "renamed": 0,
                               "without_contributors": 0, "statistics_pending": 0, "rate_limit_remaining": 57}


def test_release_large_history_and_pending_statistics(fake_web, make_ctx, tmp_path) -> None:
    _serve_kubernetes(fake_web)
    ctx = make_ctx()

    result = GitHubCollector(_registry(tmp_path, kubernetes="kubernetes/kubernetes")).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    [record] = read_dataset(ctx.settings.output_dir, DATASET)
    assert record["latest_release_tag"] == "v1.37.1"
    assert record["latest_release_at"] == "2026-09-23T19:13:17Z"
    assert record["repo_license"] == "Apache-2.0" and record["topics"] == ["cncf", "containers", "go", "kubernetes"]
    assert record["contributors"] is None
    # Requested once more at the end of the run; still HTTP 202, so the commit counts stay empty
    assert fake_web.requested.count(PARTICIPATION_URL.format(repo="kubernetes/kubernetes")) == 2
    assert record["commits_last_4_weeks"] is None and record["commits_last_52_weeks"] is None
    assert result.counters["statistics_pending"] == 1 and result.counters["without_contributors"] == 1
    assert result.complete and result.error is None


def test_statistics_computed_during_the_run_are_stored(fake_web, make_ctx, tmp_path) -> None:
    _serve_cpython(fake_web)
    participation = PARTICIPATION_URL.format(repo="python/cpython")
    fake_web.add(participation, {}, status=202)
    serve = fake_web.handler

    def handler(request: httpx.Request) -> httpx.Response:
        response = serve(request)
        if str(request.url) == participation:
            # GitHub has computed the statistics after the first request
            fake_web.add(participation, _fixture("stats_participation_python_cpython.json"))
        return response

    fake_web.handler = handler
    ctx = make_ctx()

    result = GitHubCollector(_registry(tmp_path, python="python/cpython")).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    [record] = read_dataset(ctx.settings.output_dir, DATASET)
    assert fake_web.requested.count(participation) == 2
    assert record["commits_last_52_weeks"] == sum(_fixture("stats_participation_python_cpython.json")["all"])
    assert record["contributors"] == 4114
    assert result.counters["statistics_pending"] == 0


def test_renamed_repository_keeps_the_registry_name_and_uses_the_current_name(fake_web, make_ctx, tmp_path) -> None:
    fake_web.add(REPO_URL.format(repo="mitsuhiko/flask"), _fixture("repos_mitsuhiko_flask_301_moved.json"), status=301,
                 headers={"location": "https://api.github.com/repositories/596892"})
    fake_web.add("https://api.github.com/repositories/596892", _fixture("repositories_596892_pallets_flask.json"))
    fake_web.add(PARTICIPATION_URL.format(repo="pallets/flask"), {"all": [0] * 50 + [2, 1], "owner": [0] * 52})
    # Without a Link header the number of listed contributors is the count
    fake_web.add(_contributors_url("pallets/flask"), [{"type": "User", "contributions": 10}])
    fake_web.add(LATEST_RELEASE_URL.format(repo="pallets/flask"), _fixture("releases_latest_python_cpython_404.json"),
                 status=404)
    ctx = make_ctx()

    result = GitHubCollector(_registry(tmp_path, flask="mitsuhiko/flask")).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    [record] = read_dataset(ctx.settings.output_dir, DATASET)
    assert record["record_id"] == "github:mitsuhiko/flask:2026-10-26"
    assert (record["repo"], record["full_name"], record["repo_id"]) == ("mitsuhiko/flask", "pallets/flask", 596892)
    assert record["source_url"] == "https://api.github.com/repos/pallets/flask"
    assert record["repo_license"] == "BSD-3-Clause" and record["watchers"] == 2129
    assert record["contributors"] == 1
    assert (record["commits_last_4_weeks"], record["commits_last_52_weeks"]) == (3, 3)
    assert result.counters["renamed"] == 1
    assert not any("mitsuhiko/flask/" in url for url in fake_web.requested)


def test_empty_repository_has_no_contributors_and_no_statistics(fake_web, make_ctx, tmp_path) -> None:
    _serve_cpython(fake_web)
    fake_web.add(_contributors_url("python/cpython"), b"", status=204)
    fake_web.add(PARTICIPATION_URL.format(repo="python/cpython"), {})
    ctx = make_ctx()

    result = GitHubCollector(_registry(tmp_path, python="python/cpython")).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    [record] = read_dataset(ctx.settings.output_dir, DATASET)
    assert record["contributors"] == 0
    assert record["commits_last_4_weeks"] is None and record["commits_last_52_weeks"] is None
    assert result.counters["failed"] == 0 and result.counters["statistics_pending"] == 0


def test_low_rate_limit_stops_before_the_next_repository(fake_web, make_ctx, tmp_path) -> None:
    _serve_cpython(fake_web, remaining=["21", "20", "19"])
    _serve_kubernetes(fake_web)
    ctx = make_ctx()

    result = GitHubCollector(_registry(tmp_path, python="python/cpython", kubernetes="kubernetes/kubernetes")).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    assert [r["repo"] for r in read_dataset(ctx.settings.output_dir, DATASET)] == ["python/cpython"]
    assert not any("kubernetes" in url for url in fake_web.requested)
    assert not result.complete and result.error is None
    assert result.counters["rate_limit_remaining"] == 19
    assert result.counters["stopped"] == "rate_limit" and result.counters["deferred"] == 1


def test_exhausted_rate_limit_stops_and_keeps_the_collected_records(fake_web, make_ctx, tmp_path) -> None:
    _serve_cpython(fake_web)
    fake_web.add(REPO_URL.format(repo="kubernetes/kubernetes"), {"message": "API rate limit exceeded"}, status=403,
                 headers=_remaining("0"))
    ctx = make_ctx()

    result = GitHubCollector(_registry(tmp_path, python="python/cpython", kubernetes="kubernetes/kubernetes")).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    assert [r["repo"] for r in read_dataset(ctx.settings.output_dir, DATASET)] == ["python/cpython"]
    assert fake_web.requested.count(REPO_URL.format(repo="kubernetes/kubernetes")) == 1
    assert not result.complete and result.error is None
    assert result.counters["stopped"] == "rate_limit" and result.counters["deferred"] == 1


def test_refused_request_stops_the_collector_with_an_error(fake_web, make_ctx, tmp_path) -> None:
    fake_web.add(REPO_URL.format(repo="python/cpython"), {"message": "Bad credentials"}, status=401)
    _serve_kubernetes(fake_web)
    ctx = make_ctx()

    result = GitHubCollector(_registry(tmp_path, python="python/cpython", kubernetes="kubernetes/kubernetes")).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    assert read_dataset(ctx.settings.output_dir, DATASET) == []
    assert not any("kubernetes" in url for url in fake_web.requested)
    assert not result.complete and "refused" in (result.error or "")
    assert result.counters["stopped"] == "blocked"


def test_missing_repository_is_counted_as_failed(fake_web, make_ctx, tmp_path) -> None:
    _serve_cpython(fake_web)
    ctx = make_ctx()

    result = GitHubCollector(_registry(tmp_path, removed="example-org/removed-repo", python="python/cpython")).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    assert [r["repo"] for r in read_dataset(ctx.settings.output_dir, DATASET)] == ["python/cpython"]
    assert result.counters["failed"] == 1
    assert result.complete and result.error is None


def test_error_when_every_repository_fails(fake_web, make_ctx, tmp_path) -> None:
    ctx = make_ctx()

    result = GitHubCollector(_registry(tmp_path, removed="example-org/removed-repo")).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    assert result.error == "1 of 1 repositories failed"


def test_server_errors_in_a_row_stop_the_collector(fake_web, make_ctx, tmp_path) -> None:
    repos = {f"technology_{number}": f"example-org/repo-{number}" for number in range(4)}
    for repo in repos.values():
        fake_web.add(REPO_URL.format(repo=repo), "error", status=502)
    ctx = make_ctx()

    result = GitHubCollector(_registry(tmp_path, **repos)).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert REPO_URL.format(repo="example-org/repo-3") not in fake_web.requested
    assert result.counters["failed"] == 3 and result.counters["deferred"] == 1
    assert not result.complete and "in a row" in (result.error or "")


def test_run_deadline_stops_before_the_first_request(fake_web, make_ctx, tmp_path) -> None:
    ctx = make_ctx()
    ctx.deadline = ctx.started

    result = GitHubCollector(_registry(tmp_path, python="python/cpython")).collect(
        ctx, CollectorEntry(first_run=ctx.today))

    assert fake_web.requested == []
    assert not result.complete and result.counters["deferred"] == 1 and result.counters["stopped"] == "deadline"


@pytest.mark.parametrize("token", ["test-token", None])
def test_api_headers_and_token(fake_web, make_ctx, tmp_path, token: Optional[str]) -> None:
    _serve_cpython(fake_web)
    headers: list[httpx.Headers] = []
    serve = fake_web.handler

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers)
        return serve(request)

    fake_web.handler = handler
    ctx = make_ctx(github_token=token)

    GitHubCollector(_registry(tmp_path, python="python/cpython")).collect(ctx, CollectorEntry(first_run=ctx.today))

    assert len(headers) == 4
    for request_headers in headers:
        assert request_headers["accept"] == "application/vnd.github+json"
        assert request_headers["x-github-api-version"] == "2022-11-28"
        assert request_headers.get("authorization") == (f"Bearer {token}" if token else None)


def test_last_page_number_of_link_headers() -> None:
    assert last_page_number(CPYTHON_LINK) == 4114
    assert last_page_number('<https://api.github.com/x?page=2>; rel="next"') is None
    assert last_page_number("") is None


def test_parse_repository_without_license_and_with_missing_counts() -> None:
    data = _fixture("repos_python_cpython.json")
    assert parse_repository({**data, "license": None}).license_spdx is None
    with pytest.raises(ValueError, match="subscribers_count"):
        parse_repository({key: value for key, value in data.items() if key != "subscribers_count"})

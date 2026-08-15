"""Mattermost fetcher: incremental append across runs, with HTTP mocked at http_get."""

import json
from datetime import datetime
from pathlib import Path

import pytest

import sediment.sources.mattermost as mm

SINCE = datetime(2026, 7, 1)
UNTIL = datetime(2026, 8, 1)
CHANNEL_ID = "chanidinfra"
ROOT = "rootpostid00000000000000"


def at(hour: int, minute: int, second: int) -> int:
    return int(datetime(2026, 7, 17, hour, minute, second).timestamp() * 1000)


def post(post_id: str, ts: int, user: str, message: str) -> dict:
    return {"id": post_id, "create_at": ts, "user_id": user, "message": message, "root_id": ROOT}


@pytest.fixture
def profile(tmp_path):
    return {
        "vault_path": str(tmp_path / "vault"),
        "mattermost": {
            "url": "https://mm.example.com",
            "token_env": "MM_TEST_TOKEN",
            "team": "acme",
            "channels": [{"id": CHANNEL_ID, "name": "infra"}],
        },
    }


def install_http(monkeypatch, posts: list[dict]) -> None:
    """Serve one page of users and one page of posts; later pages come back empty."""
    users = [{"id": "u1", "first_name": "Alice", "last_name": "Doe", "username": "alice"}]

    def fake_get(url, headers=None, timeout=None):
        if "/users?" in url:
            return json.dumps(users if "page=0" in url else [])
        if "/posts?" in url:
            if "page=0" not in url:
                return json.dumps({"posts": {}})
            return json.dumps({"posts": {p["id"]: p for p in posts}})
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(mm, "http_get", fake_get)
    monkeypatch.setenv("MM_TEST_TOKEN", "token")


def thread_text(tmp_path) -> str:
    files = sorted((tmp_path / "vault" / "raw" / "mattermost" / "infra").glob("*.md"))
    return "\n".join(f.read_text() for f in files)


def test_thread_is_written_once(tmp_path, profile, monkeypatch):
    install_http(monkeypatch, [post("p1", at(10, 0, 5), "u1", "The gateway is dropping tunnels again.")])

    mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

    text = thread_text(tmp_path)
    assert "**Alice Doe** [2026-07-17 10:00]: The gateway is dropping tunnels again." in text


def test_reply_in_the_boundary_minute_is_neither_duplicated_nor_lost(tmp_path, profile, monkeypatch):
    """Raw files hold minute precision, so replies sharing the last recorded minute
    are the case that a naive "strictly newer than the max ts" filter gets wrong."""
    first = "The gateway is dropping tunnels again."
    second = "Conntrack expires the flow early."
    third = "Raising the keepalive interval fixed it."
    posts = [post("p1", at(10, 0, 5), "u1", first), post("p2", at(10, 0, 30), "u1", second)]
    install_http(monkeypatch, posts)
    mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

    posts.append(post("p3", at(10, 0, 52), "u1", third))
    install_http(monkeypatch, posts)
    mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

    text = thread_text(tmp_path)
    assert text.count(first) == 1
    assert text.count(second) == 1
    assert text.count(third) == 1


def test_second_run_without_new_posts_adds_no_file(tmp_path, profile, monkeypatch):
    posts = [post("p1", at(10, 0, 5), "u1", "The gateway is dropping tunnels again.")]
    install_http(monkeypatch, posts)
    mm.fetch_mattermost_posts(profile, SINCE, UNTIL)
    before = sorted(p.name for p in Path(tmp_path / "vault" / "raw" / "mattermost" / "infra").glob("*.md"))

    mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

    after = sorted(p.name for p in Path(tmp_path / "vault" / "raw" / "mattermost" / "infra").glob("*.md"))
    assert after == before

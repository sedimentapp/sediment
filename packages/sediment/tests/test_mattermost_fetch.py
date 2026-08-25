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


DISCOVERED_ID = "pgsr7cnjtjno7qcyt13aiwstjy"
DM_ID = "16f8bhajtfghik9f8rwwtkpger"
ME_ID = "ty3dhpyyypgrip5cyijgwfrf8w"
TEAM_ID = "dy1soyciybf3xemzzn15ycmdpa"
PEER_ID = "pux9o5g7upb85k7crwfapwkx5h"


def channel(ch_id: str, ch_type: str, **overrides) -> dict:
    base = {
        "id": ch_id,
        "type": ch_type,
        "display_name": "",
        "name": ch_id,
        "team_id": TEAM_ID if ch_type in ("O", "P") else "",
        "total_msg_count": 12,
        "last_post_at": int(datetime(2026, 7, 20).timestamp() * 1000),
        "delete_at": 0,
    }
    return {**base, **overrides}


def install_discovery_http(monkeypatch, joined: list[dict], posts_by_channel: dict[str, list[dict]]) -> None:
    """Serve the discovery endpoints on top of the posts/users ones."""
    users = [
        {"id": "u1", "first_name": "Alice", "last_name": "Doe", "username": "alice"},
        {"id": PEER_ID, "first_name": "Bob", "last_name": "Roe", "username": "bob"},
    ]

    def fake_get(url, headers=None, timeout=None):
        if "/users/me/channels?" in url:
            return json.dumps(joined if "page=0" in url else [])
        if url.endswith("/users/me"):
            return json.dumps({"id": ME_ID})
        if "/teams/name/" in url:
            return json.dumps({"id": TEAM_ID})
        if "/users?" in url:
            return json.dumps(users if "page=0" in url else [])
        if "/posts?" in url:
            ch_id = url.split("/channels/")[1].split("/posts")[0]
            if "page=0" not in url:
                return json.dumps({"posts": {}})
            return json.dumps({"posts": {p["id"]: p for p in posts_by_channel.get(ch_id, [])}})
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(mm, "http_get", fake_get)
    monkeypatch.setenv("MM_TEST_TOKEN", "token")


def dirs(tmp_path) -> list[str]:
    root = tmp_path / "vault" / "raw" / "mattermost"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


class TestDiscovery:
    def test_public_channel_lands_in_an_id_suffixed_dir(self, tmp_path, profile, monkeypatch):
        profile["mattermost"]["discover"] = {"types": ["O"]}
        install_discovery_http(
            monkeypatch,
            [channel(DISCOVERED_ID, "O", display_name="host-alerts")],
            {DISCOVERED_ID: [post("p1", at(10, 0, 5), "u1", "Disk is filling up.")]},
        )

        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

        assert dirs(tmp_path) == [f"host-alerts__{DISCOVERED_ID}"]

    def test_direct_message_is_named_after_the_counterpart(self, tmp_path, profile, monkeypatch):
        profile["mattermost"]["discover"] = {"types": ["D"]}
        install_discovery_http(
            monkeypatch,
            [channel(DM_ID, "D", name=f"{PEER_ID}__{ME_ID}")],
            {DM_ID: [post("p1", at(10, 0, 5), PEER_ID, "Ping.")]},
        )

        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

        assert dirs(tmp_path) == [f"Bob Roe__{DM_ID}"]

    def test_pinned_channel_is_not_fetched_twice(self, tmp_path, profile, monkeypatch):
        profile["mattermost"]["discover"] = {"types": ["O"]}
        install_discovery_http(
            monkeypatch,
            [channel(CHANNEL_ID, "O", display_name="infra")],
            {CHANNEL_ID: [post("p1", at(10, 0, 5), "u1", "The gateway is dropping tunnels again.")]},
        )

        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

        assert dirs(tmp_path) == ["infra"]

    def test_excluded_id_is_never_fetched(self, tmp_path, profile, monkeypatch):
        profile["mattermost"]["discover"] = {"types": ["O"]}
        profile["mattermost"]["exclude"] = {"ids": [DISCOVERED_ID]}
        install_discovery_http(
            monkeypatch,
            [channel(DISCOVERED_ID, "O", display_name="host-alerts")],
            {DISCOVERED_ID: [post("p1", at(10, 0, 5), "u1", "Disk is filling up.")]},
        )

        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

        assert dirs(tmp_path) == []

    def test_exclude_by_name_also_bans_a_pinned_channel(self, tmp_path, profile, monkeypatch):
        """The ban is absolute: being listed by hand does not survive it."""
        profile["mattermost"]["exclude"] = {"names": ["infra"]}
        install_http(monkeypatch, [post("p1", at(10, 0, 5), "u1", "The gateway is dropping tunnels.")])

        with pytest.raises(ValueError, match="No channels configured"):
            mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

    def test_channel_without_recent_traffic_is_skipped(self, tmp_path, profile, monkeypatch):
        profile["mattermost"]["discover"] = {"types": ["O"], "active_within_days": 30}
        stale = int(datetime(2026, 1, 1).timestamp() * 1000)
        install_discovery_http(
            monkeypatch,
            [channel(DISCOVERED_ID, "O", display_name="host-alerts", last_post_at=stale)],
            {DISCOVERED_ID: [post("p1", at(10, 0, 5), "u1", "Disk is filling up.")]},
        )

        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

        assert dirs(tmp_path) == []

    def test_channel_of_another_team_is_skipped(self, tmp_path, profile, monkeypatch):
        profile["mattermost"]["discover"] = {"types": ["O"]}
        install_discovery_http(
            monkeypatch,
            [channel(DISCOVERED_ID, "O", display_name="host-alerts", team_id="othersteamidxxxxxxxxxxxxxx")],
            {DISCOVERED_ID: [post("p1", at(10, 0, 5), "u1", "Disk is filling up.")]},
        )

        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

        assert dirs(tmp_path) == []

    def test_channel_with_nothing_in_the_window_creates_no_directory(self, tmp_path, profile, monkeypatch):
        profile["mattermost"]["discover"] = {"types": ["O"]}
        install_discovery_http(
            monkeypatch, [channel(DISCOVERED_ID, "O", display_name="host-alerts")], {}
        )

        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

        assert dirs(tmp_path) == []

    def test_unknown_channel_type_fails_loudly(self, tmp_path, profile, monkeypatch):
        profile["mattermost"]["discover"] = {"types": ["O", "X"]}
        install_discovery_http(monkeypatch, [], {})

        with pytest.raises(ValueError, match="Unknown mattermost channel types: X"):
            mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

    def test_new_channels_are_reported_with_their_space(self, tmp_path, profile, monkeypatch, capsys):
        profile["mattermost"]["discover"] = {"types": ["O"]}
        install_discovery_http(
            monkeypatch,
            [channel(DISCOVERED_ID, "O", display_name="host-alerts")],
            {DISCOVERED_ID: [post("p1", at(10, 0, 5), "u1", "Disk is filling up.")]},
        )

        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

        out = capsys.readouterr().out
        assert f"mm:{DISCOVERED_ID}" in out
        assert "need an ACL grant" in out

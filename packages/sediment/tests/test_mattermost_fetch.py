"""Mattermost fetcher: incremental append across runs, with HTTP mocked at http_get."""

import json
from datetime import datetime
from pathlib import Path

import pytest

import sediment.sources.mattermost as mm
from sediment.sources import read_space_kinds

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


def test_historical_backfill_preserves_existing_and_repeats_cleanly(tmp_path, profile, monkeypatch):
    messages = [
        post("early", at(9, 0, 5), "u1", "Earlier history."),
        post("same", at(10, 0, 5), "u1", "Earlier in the same minute."),
        post("recent", at(10, 0, 30), "u1", "Already saved."),
    ]
    install_http(monkeypatch, messages[2:])
    mm.fetch_mattermost_posts(profile, SINCE, UNTIL)
    raw = tmp_path / "vault" / "raw" / "mattermost" / "infra"
    before = {p: p.read_bytes() for p in raw.glob("*.md")}
    install_http(monkeypatch, messages)
    mm.fetch_mattermost_posts(profile, SINCE, UNTIL)
    for message in messages:
        assert thread_text(tmp_path).count(message["message"]) == 1
    assert all(p.read_bytes() == content for p, content in before.items())
    snapshot = {p: p.read_bytes() for p in raw.glob("*.md")}
    mm.fetch_mattermost_posts(profile, SINCE, UNTIL)
    assert {p: p.read_bytes() for p in raw.glob("*.md")} == snapshot


def test_multiple_appends_in_same_minute_do_not_overwrite(tmp_path, profile, monkeypatch):
    messages = [post(str(i), at(10, 0, i), "u1", f"Message number {i}.") for i in range(1, 5)]
    for count in range(1, 5):
        install_http(monkeypatch, messages[:count])
        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)
    for message in messages:
        assert thread_text(tmp_path).count(message["message"]) == 1


def test_multiline_redacted_posts_and_identical_messages_repeat_cleanly(tmp_path, profile, monkeypatch):
    messages = [
        post("p1", at(10, 0, 1), "u1", "First line.\n\nSecond line. token=private-value"),
        post("p2", at(10, 0, 2), "u1", "Same reply."),
        post("p3", at(10, 0, 3), "u1", "Same reply."),
    ]
    install_http(monkeypatch, messages)
    mm.fetch_mattermost_posts(profile, SINCE, UNTIL)
    raw = tmp_path / "vault" / "raw" / "mattermost" / "infra"
    snapshot = {p: p.read_bytes() for p in raw.glob("*.md")}
    mm.fetch_mattermost_posts(profile, SINCE, UNTIL)
    assert {p: p.read_bytes() for p in raw.glob("*.md")} == snapshot
    assert thread_text(tmp_path).count("Same reply.") == 2
    assert "private-value" not in thread_text(tmp_path)


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
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []


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


class TestDirectoryFollowsTheId:
    """A channel keeps its directory when its name changes — under it or in the config."""

    def test_renamed_channel_keeps_writing_to_its_directory(self, tmp_path, profile, monkeypatch):
        profile["mattermost"]["discover"] = {"types": ["O"]}
        install_discovery_http(
            monkeypatch,
            [channel(DISCOVERED_ID, "O", display_name="host-alerts")],
            {DISCOVERED_ID: [post("p1", at(10, 0, 5), "u1", "Disk is filling up.")]},
        )
        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)
        assert dirs(tmp_path) == [f"host-alerts__{DISCOVERED_ID}"]

        install_discovery_http(
            monkeypatch,
            [channel(DISCOVERED_ID, "O", display_name="infra-alerts")],
            {DISCOVERED_ID: [post("p2", at(11, 0, 5), "u1", "Rotated the logs.")]},
        )
        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

        assert dirs(tmp_path) == [f"host-alerts__{DISCOVERED_ID}"]
        written = (tmp_path / "vault" / "raw" / "mattermost" / f"host-alerts__{DISCOVERED_ID}")
        assert "Rotated the logs." in "\n".join(f.read_text() for f in written.glob("*.md"))

    def test_hand_named_directory_is_adopted_once_it_carries_the_id(self, tmp_path, profile, monkeypatch):
        """The migration path: a pinned dir renamed to <old name>__<id> must not fork."""
        raw = tmp_path / "vault" / "raw" / "mattermost" / f"DM Some Person__{DISCOVERED_ID}"
        raw.mkdir(parents=True)
        (raw / "old.md").write_text("# earlier\n")
        profile["mattermost"]["discover"] = {"types": ["O"]}
        install_discovery_http(
            monkeypatch,
            [channel(DISCOVERED_ID, "O", display_name="Some Person")],
            {DISCOVERED_ID: [post("p1", at(10, 0, 5), "u1", "A later message.")]},
        )

        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

        assert dirs(tmp_path) == [f"DM Some Person__{DISCOVERED_ID}"]
        assert sorted(p.name for p in raw.glob("*.md")) == ["old.md", f"{ROOT}.md"]


class TestSpaceKindSidecar:
    def test_discovery_records_the_channel_type(self, tmp_path, profile, monkeypatch):
        profile["mattermost"]["discover"] = {"types": ["O", "D"]}
        install_discovery_http(
            monkeypatch,
            [
                channel(DISCOVERED_ID, "O", display_name="host-alerts"),
                channel(DM_ID, "D", name=f"{PEER_ID}__{ME_ID}"),
            ],
            {DISCOVERED_ID: [post("p1", at(10, 0, 5), "u1", "Disk is filling up.")]},
        )

        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

        assert read_space_kinds(tmp_path / "vault" / "raw" / "mattermost") == {
            f"mm:{DISCOVERED_ID}": "public",
            f"mm:{DM_ID}": "dm",
        }

    def test_pinned_channel_gets_a_kind_although_discovery_skips_it(self, tmp_path, profile, monkeypatch):
        """Its type is only ever in the listing, and the listing is walked once."""
        profile["mattermost"]["discover"] = {"types": ["O"]}
        install_discovery_http(
            monkeypatch,
            [channel(CHANNEL_ID, "P", display_name="infra")],
            {CHANNEL_ID: [post("p1", at(10, 0, 5), "u1", "Disk is filling up.")]},
        )

        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

        assert read_space_kinds(tmp_path / "vault" / "raw" / "mattermost") == {f"mm:{CHANNEL_ID}": "private"}

    def test_a_narrower_second_run_keeps_the_kinds_of_the_first(self, tmp_path, profile, monkeypatch):
        """A fetch window only lists what was active in it; the rest must not lose its kind."""
        profile["mattermost"]["discover"] = {"types": ["O", "D"]}
        install_discovery_http(
            monkeypatch,
            [
                channel(DISCOVERED_ID, "O", display_name="host-alerts"),
                channel(DM_ID, "D", name=f"{PEER_ID}__{ME_ID}"),
            ],
            {DISCOVERED_ID: [post("p1", at(10, 0, 5), "u1", "Disk is filling up.")]},
        )
        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

        install_discovery_http(
            monkeypatch,
            [channel(DM_ID, "D", name=f"{PEER_ID}__{ME_ID}")],
            {},
        )
        mm.fetch_mattermost_posts(profile, SINCE, UNTIL)

        kinds = read_space_kinds(tmp_path / "vault" / "raw" / "mattermost")
        assert kinds[f"mm:{DISCOVERED_ID}"] == "public"

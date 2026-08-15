"""Mattermost channel posts fetcher: groups posts into threads by root_id."""

import glob
import json
import os
import urllib.error
import urllib.parse
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from knowledge_schema import make_space

from sediment._common import (
    HttpError,
    http_get,
    is_already_recorded,
    last_post_ts_ms,
    recorded_post_counts,
    safe_path_component,
    sanitize,
)
from sediment.sources import FetchWindow, Source, SpaceDerivationError, add_dir_entry


def fetch_mattermost_posts(profile: dict[str, Any], since_dt: datetime, until_dt: datetime):
    mm = profile["mattermost"]
    base_url = mm["url"].rstrip("/")
    token_env = mm["token_env"]
    token = os.environ.get(token_env, "")
    if not token:
        raise RuntimeError(f"Missing env var {token_env}")

    raw_dir = Path(profile["vault_path"]).expanduser() / "raw" / "mattermost"
    raw_dir.mkdir(parents=True, exist_ok=True)

    channels = mm.get("channels", [])
    if not channels:
        raise ValueError("No channels configured in _profile.yaml")

    team_name = mm.get("team", "")
    cutoff_ms = int(since_dt.timestamp() * 1000)

    # Preload users — paged: a bare ?per_page=200 silently caps at the first
    # page, leaving posts of everyone else with raw user ids instead of names
    user_cache = {}
    users_page = 0
    while True:
        params = urllib.parse.urlencode({"page": users_page, "per_page": 200})
        users = json.loads(http_get(
            f"{base_url}/api/v4/users?{params}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        ))
        for u in users:
            name = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or u.get("username", u["id"])
            user_cache[u["id"]] = name
        if len(users) < 200:
            break
        users_page += 1
    print(f"  Loaded {len(user_cache)} users")

    total_new = 0
    total_appended = 0

    for ch in channels:
        ch_id = ch["id"] if isinstance(ch, dict) else ch
        ch_name = safe_path_component(
            ch.get("name", ch_id) if isinstance(ch, dict) else ch_id,
            "Mattermost channel name",
        )
        ch_dir = raw_dir / ch_name
        ch_dir.mkdir(exist_ok=True)

        all_posts: dict[str, dict] = {}
        until_ms = int(until_dt.timestamp() * 1000)
        # Mattermost `?since=X` caps the response at ~1000 posts with no pagination,
        # so deep backfills silently lose history. Page-walk newest-first instead,
        # stopping once the batch is fully older than the cutoff.
        page = 0
        per_page = 200
        channel_failed = False
        while True:
            params = urllib.parse.urlencode({"page": page, "per_page": per_page})
            try:
                data = json.loads(http_get(
                    f"{base_url}/api/v4/channels/{ch_id}/posts?{params}",
                    headers={"Authorization": f"Bearer {token}"},
                ))
            except HttpError as e:
                if e.code in (401, 403):  # bad token — fail fast, don't waste other channels
                    raise
                print(f"  Warning: {ch_name}: HTTP {e.code}")
                channel_failed = True
                break
            except (urllib.error.URLError, TimeoutError) as e:
                print(f"  Warning: {ch_name}: {type(e).__name__}: {e}")
                channel_failed = True
                break

            batch = data.get("posts", {})
            if not batch:
                break
            oldest_in_batch = min(p.get("create_at", 0) for p in batch.values())
            for post_id, post in batch.items():
                if post.get("type"):
                    continue
                if not post.get("message", "").strip():
                    continue
                ts = post.get("create_at", 0)
                if ts < cutoff_ms or ts > until_ms:
                    continue
                all_posts[post_id] = post
            if oldest_in_batch < cutoff_ms or len(batch) < per_page:
                break
            page += 1

        if channel_failed:
            continue

        thread_roots: dict[str, list[dict]] = {}
        for post_id, post in all_posts.items():
            root_id = post.get("root_id") or post_id
            thread_roots.setdefault(root_id, []).append(post)

        ch_new = 0
        ch_appended = 0
        for root_id, posts in thread_roots.items():
            root_id = safe_path_component(root_id, "Mattermost thread id")
            existing = sorted(ch_dir.glob(f"{glob.escape(root_id)}.md")) + sorted(ch_dir.glob(f"{glob.escape(root_id)}.*.md"))

            if existing:
                last_known_ms = last_post_ts_ms(existing)
                recorded = recorded_post_counts(existing)
                posts.sort(key=lambda p: p.get("create_at", 0))
                posts = [
                    p
                    for p in posts
                    if p.get("create_at", 0) >= last_known_ms
                    and not is_already_recorded(
                        recorded,
                        datetime.fromtimestamp(p.get("create_at", 0) / 1000).strftime("%Y-%m-%d %H:%M"),
                    )
                ]
                if not posts:
                    continue

            posts.sort(key=lambda p: p.get("create_at", 0))
            first_ts = posts[0].get("create_at", 0)
            date_str = datetime.fromtimestamp(first_ts / 1000).strftime("%Y-%m-%d") if first_ts else ""
            permalink = f"{base_url}/{team_name}/pl/{root_id}"

            if existing:
                stamp = datetime.fromtimestamp(first_ts / 1000).strftime("%Y-%m-%dT%H-%M")
                raw_file = ch_dir / f"{root_id}.{stamp}.md"
                ch_appended += 1
            else:
                raw_file = ch_dir / f"{root_id}.md"
                ch_new += 1

            lines = [
                f"# {ch_name} | {date_str}",
                f"[Open in Mattermost]({permalink})",
                "",
            ]

            for post in posts:
                user_name = user_cache.get(post.get("user_id", ""), post.get("user_id", ""))
                ts = post.get("create_at", 0)
                ts_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M") if ts else ""
                lines.append(f"**{user_name}** [{ts_str}]: {post['message']}")
                lines.append("")

            raw_file.write_text(sanitize("\n".join(lines)))

        if ch_new or ch_appended:
            print(f"  {ch_name}: {ch_new} new, {ch_appended} updated")
        total_new += ch_new
        total_appended += ch_appended

    print(f"  Total threads: {total_new} new, {total_appended} updated")


def _fetch(profile: dict[str, Any], window: FetchWindow, options: Mapping[str, Any]) -> None:
    fetch_mattermost_posts(profile, since_dt=window.since_dt, until_dt=window.until_dt)


def _space_context(profile: dict[str, Any]) -> dict[str, tuple[str, str]] | None:
    """dir-name -> (channel_id, channel_name); None when the profile has no mattermost section.

    Directories carry the human channel name, so the stable id has to come from
    the same config the fetcher wrote them with.
    """
    mm_cfg = profile.get("mattermost")
    if mm_cfg is None:
        return None
    by_dir: dict[str, tuple[str, str]] = {}
    for ch in mm_cfg.get("channels", []):
        # mirror the fetch loop above: entries are {id, name} dicts or bare id strings
        ch_id = ch["id"] if isinstance(ch, dict) else ch
        ch_name = ch.get("name", ch_id) if isinstance(ch, dict) else ch_id
        dir_key = safe_path_component(ch_name, "Mattermost channel name")
        add_dir_entry(by_dir, "mattermost", dir_key, (ch_id, ch_name))
    return by_dir


def _derive_space(rel_path: str, context: dict[str, tuple[str, str]] | None) -> tuple[str, str]:
    """mattermost/<channel_name>/<root_id>[.stamp].md — ownership is the directory."""
    if context is None:
        raise RuntimeError(
            "Loading mattermost requires --config-dir with a profile containing "
            "the mattermost section (name->id map for space derivation)"
        )
    parts = rel_path.split("/")
    if len(parts) < 3:
        raise SpaceDerivationError(rel_path, "expected mattermost/<dir>/<file>.md layout")
    dir_name = parts[1]
    entry = context.get(dir_name)
    if entry is None:
        raise SpaceDerivationError(
            rel_path, f"mattermost directory {dir_name!r} not in config (renamed or removed channel?)"
        )
    channel_id, channel_name = entry
    return make_space("mattermost", channel_id), channel_name


SOURCE = Source(
    name="mattermost",
    fetch=_fetch,
    derive_space=_derive_space,
    space_context=_space_context,
)

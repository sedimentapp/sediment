"""Mattermost channel posts fetcher: groups posts into threads by root_id."""

import glob
import json
import os
import re
import urllib.error
import urllib.parse
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
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
from sediment.sources import (
    FetchWindow,
    Source,
    SpaceDerivationError,
    SpaceExcluded,
    add_dir_entry,
)

# Mattermost channel types, as the `type` field spells them.
CHANNEL_TYPES = {"O": "public", "P": "private", "G": "group", "D": "direct"}

_MAX_DISCOVERED_NAME_CHARS = 120
_MM_ID_RE = re.compile(r"[a-z0-9]{26}")
# Discovered channels carry their id in the directory name, so a raw file stays
# self-describing: `derive_space` reads ownership straight off the path instead
# of needing the channel to be listed in the profile. Pinned channels keep the
# bare-name layout they were fetched with.
_DISCOVERED_DIR_RE = re.compile(rf"^(?P<name>.+)__(?P<id>{_MM_ID_RE.pattern})$")


@dataclass(frozen=True)
class Channel:
    """One channel to fetch: stable id, display name, and the directory it owns."""

    id: str
    name: str
    dir_name: str


@dataclass(frozen=True)
class Exclusions:
    """Channels the config bans outright — from fetching and from indexing alike."""

    ids: frozenset[str] = frozenset()
    names: frozenset[str] = frozenset()

    def blocks(self, channel_id: str, channel_name: str) -> bool:
        return channel_id in self.ids or channel_name in self.names


@dataclass(frozen=True)
class SpaceContext:
    """What `derive_space` needs: the pinned dir->channel map plus the ban list."""

    by_dir: dict[str, tuple[str, str]] = field(default_factory=dict)
    exclusions: Exclusions = Exclusions()


def _api_get(base_url: str, token: str, path: str, timeout: int = 30) -> Any:
    return json.loads(
        http_get(f"{base_url}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    )


def _exclusions(mm_cfg: Mapping[str, Any]) -> Exclusions:
    exclude = mm_cfg.get("exclude") or {}
    unknown = set(exclude) - {"ids", "names"}
    if unknown:
        raise ValueError(f"Unknown keys in mattermost.exclude: {', '.join(sorted(unknown))}")
    return Exclusions(
        ids=frozenset(str(i) for i in exclude.get("ids", [])),
        names=frozenset(str(n) for n in exclude.get("names", [])),
    )


def _pinned_channels(mm_cfg: Mapping[str, Any], exclusions: Exclusions) -> list[Channel]:
    """Channels listed by hand in the profile, minus the ones `exclude` bans."""
    pinned = []
    for ch in mm_cfg.get("channels", []):
        ch_id = str(ch["id"] if isinstance(ch, dict) else ch)
        ch_name = str(ch.get("name", ch_id) if isinstance(ch, dict) else ch_id)
        if exclusions.blocks(ch_id, ch_name):
            print(f"  Excluded (configured but banned): {ch_name}")
            continue
        pinned.append(
            Channel(ch_id, ch_name, safe_path_component(ch_name, "Mattermost channel name"))
        )
    return pinned


def _discovered_name(ch: Mapping[str, Any], user_cache: Mapping[str, str], me_id: str) -> str:
    """A human name for a channel the profile never named.

    Direct messages have no display_name at all — their `name` is the two member
    ids joined by `__`, so the counterpart's name has to come from the user cache.
    """
    display = str(ch.get("display_name") or "").strip()
    if display:
        return display
    raw_name = str(ch.get("name", ""))
    if ch.get("type") == "D":
        members = raw_name.split("__")
        # A self-DM has both halves equal, so "the other one" is just the other half.
        other = next((m for m in members if m != me_id), members[-1] if members else "")
        return user_cache.get(other, other) or raw_name
    return raw_name


def _discover_channels(
    base_url: str,
    token: str,
    mm_cfg: Mapping[str, Any],
    pinned: list[Channel],
    exclusions: Exclusions,
    user_cache: Mapping[str, str],
    until_ms: int,
) -> list[Channel]:
    """Channels the account is a member of that the profile does not list.

    Returns [] unless the profile opts in with a `discover` section; the opt-in
    names the channel types to pick up, so "public channels only" and "every
    conversation with recent traffic" are the same mechanism at different settings.
    """
    discover = mm_cfg.get("discover")
    if not discover:
        return []
    unknown = set(discover) - {"types", "active_within_days"}
    if unknown:
        raise ValueError(f"Unknown keys in mattermost.discover: {', '.join(sorted(unknown))}")

    types = discover.get("types")
    if not isinstance(types, list) or not types:
        raise ValueError(
            "mattermost.discover.types must be a non-empty list of channel types "
            f"({', '.join(f'{k} = {v}' for k, v in CHANNEL_TYPES.items())})"
        )
    unknown_types = set(types) - set(CHANNEL_TYPES)
    if unknown_types:
        raise ValueError(f"Unknown mattermost channel types: {', '.join(sorted(unknown_types))}")

    active_days = discover.get("active_within_days")
    # Anchored to the fetch window's end, not to wall clock: a backfill run asks
    # "which channels were alive back then", and repeat runs stay deterministic.
    cutoff_ms = until_ms - int(active_days) * 86_400_000 if active_days else None

    team_id = ""
    if {"O", "P"} & set(types):
        team_name = mm_cfg.get("team", "")
        if not team_name:
            raise ValueError("mattermost.discover of public/private channels requires mattermost.team")
        team_id = _api_get(base_url, token, f"/api/v4/teams/name/{urllib.parse.quote(team_name)}")["id"]

    me_id = _api_get(base_url, token, "/api/v4/users/me")["id"] if "D" in types else ""

    candidates: list[dict] = []
    page = 0
    per_page = 200
    while True:
        params = urllib.parse.urlencode({"page": page, "per_page": per_page})
        batch = _api_get(base_url, token, f"/api/v4/users/me/channels?{params}")
        candidates.extend(batch)
        if len(batch) < per_page:
            break
        page += 1

    pinned_ids = {c.id for c in pinned}
    taken_dirs = {c.dir_name for c in pinned}
    discovered: list[Channel] = []
    per_type: Counter[str] = Counter()
    skipped_excluded = 0
    for ch in candidates:
        ch_id = ch["id"]
        if ch_id in pinned_ids or ch.get("delete_at"):
            continue
        ch_type = str(ch.get("type", ""))
        if ch_type not in types:
            continue
        if ch_type in ("O", "P") and ch.get("team_id") != team_id:
            continue
        if not ch.get("total_msg_count"):
            continue
        if cutoff_ms is not None and int(ch.get("last_post_at") or 0) < cutoff_ms:
            continue
        ch_name = _discovered_name(ch, user_cache, me_id)
        if exclusions.blocks(ch_id, ch_name):
            skipped_excluded += 1
            continue
        stem = safe_path_component(
            ch_name.replace("/", "-")[:_MAX_DISCOVERED_NAME_CHARS].strip() or ch_id,
            "Mattermost channel name",
        )
        dir_name = f"{stem}__{ch_id}"
        if dir_name in taken_dirs:  # same channel twice in one listing — Mattermost shouldn't, but
            continue
        taken_dirs.add(dir_name)
        per_type[ch_type] += 1
        discovered.append(Channel(ch_id, ch_name, dir_name))

    by_type = ", ".join(f"{CHANNEL_TYPES[t]} {per_type[t]}" for t in types if per_type[t])
    print(
        f"  Discovery: {len(candidates)} joined, {len(pinned)} pinned, "
        f"{len(discovered)} auto ({by_type or 'none'}), {skipped_excluded} excluded"
    )
    return discovered


def _existing_dirs_by_id(raw_dir: Path) -> dict[str, Path]:
    """channel id -> the directory already holding it, for ids encoded in a name.

    A channel's visible name moves — someone renames it in Mattermost, or the
    profile named it by hand and the config is being retired — while its id does
    not. Without this lookup the fetcher would start a second directory under the
    new name and re-fetch the same threads into it.
    """
    by_id: dict[str, Path] = {}
    if not raw_dir.exists():
        return by_id
    for entry in sorted(raw_dir.iterdir()):
        if not entry.is_dir():
            continue
        match = _DISCOVERED_DIR_RE.match(entry.name)
        if match:
            by_id.setdefault(match["id"], entry)
    return by_id


def fetch_mattermost_posts(profile: dict[str, Any], since_dt: datetime, until_dt: datetime):
    mm = profile["mattermost"]
    base_url = mm["url"].rstrip("/")
    token_env = mm["token_env"]
    token = os.environ.get(token_env, "")
    if not token:
        raise RuntimeError(f"Missing env var {token_env}")

    raw_dir = Path(profile["vault_path"]).expanduser() / "raw" / "mattermost"
    raw_dir.mkdir(parents=True, exist_ok=True)

    exclusions = _exclusions(mm)
    pinned = _pinned_channels(mm, exclusions)
    if not pinned and not mm.get("discover"):
        raise ValueError("No channels configured in _profile.yaml and discovery is off")

    team_name = mm.get("team", "")
    cutoff_ms = int(since_dt.timestamp() * 1000)
    until_ms = int(until_dt.timestamp() * 1000)

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

    discovered = _discover_channels(base_url, token, mm, pinned, exclusions, user_cache, until_ms)
    channels = pinned + discovered
    existing_by_id = _existing_dirs_by_id(raw_dir)
    first_seen = [c for c in discovered if c.id not in existing_by_id]

    total_new = 0
    total_appended = 0

    for channel in channels:
        ch_id = channel.id
        ch_name = channel.name
        # Reuse the directory that already holds this channel's id, whatever it
        # is called; only a channel with no directory yet gets a fresh name.
        # Created lazily: discovery walks every joined channel, and most of them
        # have nothing inside the fetch window.
        ch_dir = existing_by_id.get(channel.id, raw_dir / channel.dir_name)

        all_posts: dict[str, dict] = {}
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

            ch_dir.mkdir(parents=True, exist_ok=True)
            raw_file.write_text(sanitize("\n".join(lines)))

        if ch_new or ch_appended:
            print(f"  {ch_name}: {ch_new} new, {ch_appended} updated")
        total_new += ch_new
        total_appended += ch_appended

    print(f"  Total threads: {total_new} new, {total_appended} updated")

    # Auto-added channels land in spaces nobody holds a grant for yet, and ACL
    # matches spaces exactly — without this list their content is simply invisible.
    fetched_first_time = [
        c for c in first_seen if any(existing_by_id.get(c.id, raw_dir / c.dir_name).glob("*.md"))
    ]
    if fetched_first_time:
        print(f"  New channels fetched for the first time ({len(fetched_first_time)}) — need an ACL grant:")
        for channel in fetched_first_time:
            print(f"    {make_space('mattermost', channel.id)}  {channel.name}")


def _fetch(profile: dict[str, Any], window: FetchWindow, options: Mapping[str, Any]) -> None:
    fetch_mattermost_posts(profile, since_dt=window.since_dt, until_dt=window.until_dt)


def _space_context(profile: dict[str, Any]) -> SpaceContext | None:
    """Pinned dir-name -> (channel_id, channel_name) plus exclusions; None without the section.

    Pinned directories carry only the human channel name, so their stable id has
    to come from the same config the fetcher wrote them with. Auto-discovered
    ones need no config at all — see `_derive_space`.
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
    return SpaceContext(by_dir=by_dir, exclusions=_exclusions(mm_cfg))


def _derive_space(rel_path: str, context: SpaceContext | None) -> tuple[str, str]:
    """mattermost/<channel_name>[__<channel_id>]/<root_id>[.stamp].md — the directory owns the file.

    Pinned channels resolve through the profile; discovered ones carry the id in
    the directory name, so their files survive a config the fetch host never had.
    """
    parts = rel_path.split("/")
    if len(parts) < 3:
        raise SpaceDerivationError(rel_path, "expected mattermost/<dir>/<file>.md layout")
    dir_name = parts[1]

    entry = context.by_dir.get(dir_name) if context is not None else None
    if entry is not None:
        channel_id, channel_name = entry
    elif match := _DISCOVERED_DIR_RE.match(dir_name):
        channel_id, channel_name = match["id"], match["name"]
    elif context is None:
        raise RuntimeError(
            "Loading mattermost requires --config-dir with a profile containing "
            "the mattermost section (name->id map for space derivation)"
        )
    else:
        raise SpaceDerivationError(
            rel_path, f"mattermost directory {dir_name!r} not in config (renamed or removed channel?)"
        )

    if context is not None and context.exclusions.blocks(channel_id, channel_name):
        raise SpaceExcluded(
            rel_path, f"channel {channel_name!r} is banned by mattermost.exclude — not indexed"
        )
    return make_space("mattermost", channel_id), channel_name


SOURCE = Source(
    name="mattermost",
    fetch=_fetch,
    derive_space=_derive_space,
    space_context=_space_context,
)

"""Durable per-source progress. A failed fetch rolls back its day's checkpoint."""

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from sediment.sources import FetchWindow, Source

logger = logging.getLogger(__name__)


def fetch_window(since: date, until: date) -> FetchWindow:
    if since > until:
        raise ValueError(f"Start date {since} is after end date {until}")
    return FetchWindow(
        since_date=since.isoformat(), until_date=until.isoformat(),
        since_dt=datetime.combine(since, datetime.min.time()),
        until_dt=datetime.combine(until + timedelta(days=1), datetime.min.time()),
    )


def fetch_incremental(
    profile_name: str, profile: dict[str, Any], source: Source,
    options: Mapping[str, Any], today: date, initial_since: date | None,
) -> None:
    vault = Path(profile["vault_path"]).expanduser()
    vault.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(vault / ".fetch-state.sqlite3", timeout=0)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS progress ("
            "profile TEXT NOT NULL, source TEXT NOT NULL, resume_date TEXT NOT NULL, "
            "last_success TEXT NOT NULL, raw_files INTEGER NOT NULL, "
            "PRIMARY KEY (profile, source))"
        )
        with conn:
            # Serialize fetch and checkpoint together; a concurrent writer fails visibly.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT resume_date FROM progress WHERE profile = ? AND source = ?",
                (profile_name, source.name),
            ).fetchone()
            if row is None:
                if initial_since is None:
                    raise ValueError(
                        f"No fetch checkpoint for {profile_name}/{source.name}; "
                        "specify --initial-since YYYY-MM-DD to choose the start of automatic collection"
                    )
                since = initial_since
            else:
                if initial_since is not None:
                    raise ValueError(f"Checkpoint already exists for {profile_name}/{source.name}; use --since for a manual backfill")
                saved = date.fromisoformat(row[0])
                if saved > today:
                    raise ValueError(f"Fetch checkpoint {saved} is in the future for {profile_name}/{source.name}")
                # Replay the boundary and retain the previous two-day overlap.
                since = min(saved, today - timedelta(days=2))
            if since > today:
                raise ValueError(f"Initial date {since} is in the future")
            print(json.dumps({
                "event": "fetch_started", "profile": profile_name, "source": source.name,
                "since": since.isoformat(), "until": today.isoformat(),
                "catchup_days": (today - since).days,
            }))
        day = since
        while day <= today:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT resume_date FROM progress WHERE profile = ? AND source = ?",
                    (profile_name, source.name),
                ).fetchone()
                # Another process must not move progress between our day transactions.
                if current != row:
                    raise RuntimeError(f"Fetch progress changed concurrently for {profile_name}/{source.name}")
                source.fetch(profile, fetch_window(day, day), options)
                count = sum(1 for _ in (vault / "raw" / source.name).rglob("*.md"))
                succeeded = datetime.now(timezone.utc).isoformat()
                resume = max(day, date.fromisoformat(row[0])) if row is not None else day
                conn.execute(
                    "INSERT INTO progress VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(profile, source) DO UPDATE SET "
                    "resume_date=excluded.resume_date, last_success=excluded.last_success, raw_files=excluded.raw_files",
                    (profile_name, source.name, resume.isoformat(), succeeded, count),
                )
                row = (resume.isoformat(),)
            print(json.dumps({
                "event": "fetch_succeeded", "profile": profile_name, "source": source.name,
                "day": day.isoformat(), "raw_files": count, "last_success": succeeded,
            }))
            if count == 0:
                logger.warning(
                    "Source fetch returned successfully but its raw inventory is empty",
                    extra={"operation": "fetch_inventory", "profile": profile_name, "source": source.name, "day": day.isoformat()},
                )
            day += timedelta(days=1)
    finally:
        conn.close()

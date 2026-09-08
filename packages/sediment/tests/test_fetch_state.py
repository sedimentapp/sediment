import sqlite3
from datetime import date

import pytest

from sediment.fetch_state import fetch_incremental
from sediment.sources import Source
from sediment._common import HttpError
from sediment.sources import youtrack


def checkpoint(vault, profile="acme", source="youtrack"):
    with sqlite3.connect(vault / ".fetch-state.sqlite3") as conn:
        return conn.execute("SELECT resume_date FROM progress WHERE profile=? AND source=?", (profile, source)).fetchone()


def source(fetch, name="youtrack"):
    return Source(name=name, fetch=fetch, derive_space=lambda path, ctx: ("yt:ACME", "ACME"))


def test_first_run_requires_explicit_start(tmp_path):
    calls = []
    with pytest.raises(ValueError, match="--initial-since"):
        fetch_incremental("acme", {"vault_path": str(tmp_path)}, source(lambda *args: calls.append(args)), {}, date(2026, 9, 8), None)
    assert calls == []
    assert checkpoint(tmp_path) is None


def test_outage_catches_up_every_day(tmp_path):
    windows = []
    src = source(lambda profile, window, options: windows.append(window))
    cfg = {"vault_path": str(tmp_path)}
    fetch_incremental("acme", cfg, src, {}, date(2026, 9, 1), date(2026, 9, 1))
    windows.clear()
    fetch_incremental("acme", cfg, src, {}, date(2026, 9, 8), None)
    assert [w.since_date for w in windows] == [f"2026-09-{d:02}" for d in range(1, 9)]
    assert all(w.since_date == w.until_date for w in windows)
    assert checkpoint(tmp_path) == ("2026-09-08",)


def test_failure_keeps_last_success_and_replays_boundary(tmp_path):
    cfg = {"vault_path": str(tmp_path)}

    def fetch(profile, window, options):
        if window.since_date == "2026-09-03":
            raise TimeoutError("source unavailable")

    with pytest.raises(TimeoutError):
        fetch_incremental("acme", cfg, source(fetch), {}, date(2026, 9, 8), date(2026, 9, 1))
    assert checkpoint(tmp_path) == ("2026-09-02",)
    windows = []
    fetch_incremental("acme", cfg, source(lambda p, w, o: windows.append(w.since_date)), {}, date(2026, 9, 8), None)
    assert windows == [f"2026-09-{d:02}" for d in range(2, 9)]


def test_progress_is_isolated_by_profile_and_source(tmp_path):
    cfg = {"vault_path": str(tmp_path)}
    fetch_incremental("acme", cfg, source(lambda *args: None), {}, date(2026, 9, 1), date(2026, 9, 1))
    assert checkpoint(tmp_path, "globex") is None
    assert checkpoint(tmp_path, source="mattermost") is None


def test_success_logs_inventory_and_warns_when_empty(tmp_path, capsys, caplog):
    fetch_incremental("acme", {"vault_path": str(tmp_path)}, source(lambda *args: None), {}, date(2026, 9, 1), date(2026, 9, 1))
    assert '"raw_files": 0' in capsys.readouterr().out
    assert "raw inventory is empty" in caplog.text


def test_future_checkpoint_is_not_silently_reset(tmp_path):
    cfg = {"vault_path": str(tmp_path)}
    src = source(lambda *args: None)
    fetch_incremental("acme", cfg, src, {}, date(2026, 9, 8), date(2026, 9, 8))
    with pytest.raises(ValueError, match="future"):
        fetch_incremental("acme", cfg, src, {}, date(2026, 9, 7), None)
    assert checkpoint(tmp_path) == ("2026-09-08",)


def test_rejected_youtrack_project_does_not_advance_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_TEST_TOKEN", "test")
    calls = []
    def reject(url, headers):
        calls.append(url)
        raise HttpError(400, url, b'The value "MISSING" isn\'t used for the project field')
    monkeypatch.setattr(youtrack, "http_get", reject)
    cfg = {"vault_path": str(tmp_path), "youtrack": {
        "url": "https://youtrack.invalid", "token_env": "YT_TEST_TOKEN",
        "projects": [{"short": "MISSING", "enabled": True}],
    }}
    with pytest.raises(HttpError):
        fetch_incremental("acme", cfg, youtrack.SOURCE, {"articles_only": False, "projects": None}, date(2026, 9, 8), date(2026, 9, 8))
    assert len(calls) == 1
    assert checkpoint(tmp_path) is None

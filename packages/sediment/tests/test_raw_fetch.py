from datetime import date
import sqlite3

import pytest

from sediment import raw_fetch
from sediment.sources import Source


@pytest.fixture
def cli(tmp_path, monkeypatch):
    calls = []
    def arguments(parser):
        parser.add_argument("--articles-only", action="store_true")
    src = Source(name="youtrack", fetch=lambda p, w, o: calls.append(w),
                 derive_space=lambda p, c: ("yt:ACME", "ACME"), add_arguments=arguments)
    monkeypatch.setattr(raw_fetch, "available_sources", lambda: {"youtrack": src})
    monkeypatch.setattr(raw_fetch, "load_profile", lambda path: {
        "profiles": {"acme": {"vault_path": str(tmp_path), "youtrack": {}}},
    })
    return calls


def test_manual_fetch_does_not_create_checkpoint(cli, tmp_path):
    raw_fetch.main(["--since", "2026-01-01", "--until", "2026-01-02"])
    assert cli[0].since_date == "2026-01-01"
    assert not (tmp_path / ".fetch-state.sqlite3").exists()


@pytest.mark.parametrize("args", [
    ["--until", "2026-01-01"], ["--articles-only"],
    ["--since", "2026-01-02", "--until", "2026-01-01"],
    ["--since", "2026-01-01", "--initial-since", "2026-01-01"],
])
def test_invalid_cli_fails_before_fetch(cli, args):
    with pytest.raises(SystemExit):
        raw_fetch.main(args)
    assert cli == []


def test_manual_subset_does_not_advance_existing_checkpoint(cli, tmp_path):
    raw_fetch.main(["--initial-since", date.today().isoformat()])
    with sqlite3.connect(tmp_path / ".fetch-state.sqlite3") as conn:
        before = conn.execute("SELECT * FROM progress").fetchall()
    raw_fetch.main(["--since", "2026-01-01", "--until", "2026-01-02", "--articles-only"])
    with sqlite3.connect(tmp_path / ".fetch-state.sqlite3") as conn:
        assert conn.execute("SELECT * FROM progress").fetchall() == before

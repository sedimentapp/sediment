"""What a data source is, and how one is plugged in from outside this package.

A source contributes two halves of a single convention: a fetcher that writes
raw files into the vault, and the rule that reads ownership back out of the
paths it wrote (`space`, see knowledge_schema.make_space). They ship together
because a fetcher that changes its file layout silently breaks derivation.

Sources that can only run on a workstation — a Telegram session file, local
Claude Code transcripts — live outside this distribution and register through
the "sediment.sources" entry-point group (see sediment.registry).

Source *names* and space prefixes stay in knowledge_schema, not here: they are
the vocabulary of what is already stored in Qdrant, which the reader has to
understand whether or not the importer that produced it is installed.
"""

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sediment._common import (
    HttpError,
    http_get,
    is_already_recorded,
    last_post_ts_ms,
    recorded_post_counts,
    safe_path_component,
    sanitize,
)

__all__ = [
    "FetchWindow",
    "HttpError",
    "Source",
    "SpaceDerivationError",
    "add_dir_entry",
    "http_get",
    "is_already_recorded",
    "last_post_ts_ms",
    "recorded_post_counts",
    "safe_path_component",
    "sanitize",
]


class SpaceDerivationError(Exception):
    """A file's space cannot be derived; the file must be reported, never silently indexed."""

    def __init__(self, rel_path: str, reason: str) -> None:
        self.rel_path = rel_path
        self.reason = reason
        super().__init__(f"{rel_path}: {reason}")


@dataclass(frozen=True)
class FetchWindow:
    """The run's time range, in both forms the source APIs ask for."""

    since_date: str  # YYYY-MM-DD, inclusive
    until_date: str  # YYYY-MM-DD, inclusive
    since_dt: datetime
    until_dt: datetime  # exclusive: midnight after until_date


def _no_context(profile: dict[str, Any]) -> Any:
    return None


def _no_arguments(parser: argparse.ArgumentParser) -> None:
    return None


@dataclass(frozen=True)
class Source:
    """One importable source. Instances are what an entry point must resolve to."""

    name: str
    # Writes raw files for one profile; `options` is the parsed raw-fetch
    # command line, so a source reads back whatever add_arguments registered.
    fetch: Callable[[dict[str, Any], FetchWindow, Mapping[str, Any]], None]
    # (rel_path, context) -> (space, space_name); rel_path is relative to the
    # vault's raw dir and still starts with the source name.
    derive_space: Callable[[str, Any], tuple[str, str]]
    # Whatever derive_space needs out of the profile (name->id maps and such),
    # built once per collection. None when the rule needs no config.
    space_context: Callable[[dict[str, Any]], Any] = _no_context
    # Source-specific raw-fetch flags. Names must stay unique across sources —
    # they share one parser.
    add_arguments: Callable[[argparse.ArgumentParser], None] = _no_arguments


def add_dir_entry(
    mapping: dict[str, tuple[str, str]],
    source: str,
    key: str,
    value: tuple[str, str],
) -> None:
    """Record dir-name -> (stable_id, display_name), refusing an ambiguous config.

    Two channels whose names collapse to the same directory would make space
    derivation pick one of them at random, i.e. hand one channel's documents the
    other's ACL.
    """
    existing = mapping.get(key)
    if existing is not None and existing[0] != value[0]:
        raise RuntimeError(
            f"Ambiguous {source} config: directory {key!r} maps to both "
            f"id {existing[0]!r} and id {value[0]!r}; space derivation would be wrong"
        )
    mapping[key] = value

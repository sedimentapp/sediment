"""Descriptive labels for a raw file: what kind of container it sits in
(`space_kind`) and what kind of document it is (`doc_kind`).

Unlike `space`, these are filters and nothing else — no access decision reads
them — so a source that cannot say leaves the label off. That is why nothing
here has a fallback: a guessed kind would quietly drop documents out of a
filtered search, which is worse than a point that simply has no kind.

Where a kind comes from depends on whether the path already carries it. A
YouTrack space is always a project and an article's id says it is an article,
so those rules live in the source module next to the layout they read. A
Mattermost channel's type exists only in the API, so the fetcher writes it to a
sidecar beside the raw files and this module reads it back.
"""

from pathlib import Path

from knowledge_schema import DOC_KINDS, SPACE_KINDS

from sediment.registry import available_sources
from sediment.sources import SPACE_KINDS_FILE, read_space_kinds


class KindResolver:
    """Kind lookups for one vault's raw directory."""

    def __init__(self, sidecars: dict[str, dict[str, str]]) -> None:
        self._sidecars = sidecars

    @classmethod
    def from_raw_dir(cls, raw_dir: Path) -> "KindResolver":
        """Load the sidecar of every installed source that publishes one."""
        sidecars = {}
        for name, source in available_sources().items():
            if source.space_kind is not None:
                continue
            kinds = read_space_kinds(raw_dir / name)
            unknown = sorted(set(kinds.values()) - set(SPACE_KINDS))
            if unknown:
                raise RuntimeError(
                    f"{raw_dir / name / SPACE_KINDS_FILE}: unknown space kinds "
                    f"{', '.join(unknown)}"
                )
            sidecars[name] = kinds
        return cls(sidecars)

    def space_kind(self, source: str, space: str) -> str | None:
        plugin = available_sources()[source]
        if plugin.space_kind is not None:
            return plugin.space_kind
        return self._sidecars[source].get(space)

    def doc_kind(self, source: str, rel_path: str) -> str | None:
        kind = available_sources()[source].doc_kind(rel_path)
        if kind is not None and kind not in DOC_KINDS:
            raise RuntimeError(f"Source {source!r} returned unknown doc kind {kind!r} for {rel_path}")
        return kind


__all__ = ["KindResolver"]

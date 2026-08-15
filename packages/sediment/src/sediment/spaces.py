"""Route a raw file to its source's `space` derivation rule.

Ownership is encoded into the path by whichever fetcher wrote it, so the rule
that decodes it lives next to that fetcher (sediment/sources/*.py for the
built-ins, the plugin package for the rest). This module only routes, and holds
the per-profile context those rules need: Mattermost and Telegram directories
are named after the human channel name, so mapping one back to a stable id
takes the same profile config the fetcher used.

A file whose source has no importer installed here raises SpaceDerivationError
rather than being indexed spaceless — under ACL a point without a space is
invisible, and the content_hash increment would never revisit it.
"""

from typing import Any

from sediment.registry import available_sources
from sediment.sources import SpaceDerivationError


class SpaceResolver:
    """Derivation contexts for one profile, one per installed source."""

    def __init__(self, contexts: dict[str, Any]) -> None:
        self._contexts = contexts

    @classmethod
    def from_profile(cls, profile: dict[str, Any]) -> "SpaceResolver":
        """Build every installed source's context from one profile entry."""
        return cls({
            name: source.space_context(profile)
            for name, source in available_sources().items()
        })

    def derive(self, source: str, rel_path: str) -> tuple[str, str]:
        """(space, space_name) for a raw file path relative to raw_dir.

        Raises SpaceDerivationError when this particular file cannot be mapped
        (skip + report), and RuntimeError when the run is misconfigured (a
        source whose rule needs profile config that is absent).
        """
        plugin = available_sources().get(source)
        if plugin is None:
            raise SpaceDerivationError(
                rel_path,
                f"no importer installed for source {source!r} — its space rule ships "
                "with its fetcher",
            )
        return plugin.derive_space(rel_path, self._contexts[source])


__all__ = ["SpaceDerivationError", "SpaceResolver"]

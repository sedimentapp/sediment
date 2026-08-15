"""Source plugin discovery: what a bad registration does, and that a good one reaches derivation."""

import pytest

from knowledge_schema import SOURCES
from sediment import registry
from sediment.spaces import SpaceDerivationError, SpaceResolver


class FakeEntryPoint:
    def __init__(self, name: str, obj: object, value: str = "pkg.mod:SOURCE") -> None:
        self.name = name
        self.value = value
        self._obj = obj

    def load(self) -> object:
        return self._obj


def wiki_source(name: str = "wiki") -> registry.Source:
    return registry.Source(
        name=name,
        fetch=lambda profile, window, options: None,
        derive_space=lambda rel_path, context: ("yt:WIKI", context or "wiki"),
        space_context=lambda profile: profile.get("wiki", {}).get("label"),
    )


@pytest.fixture(autouse=True)
def clear_plugin_cache():
    registry._plugin_sources.cache_clear()
    yield
    registry._plugin_sources.cache_clear()


@pytest.fixture
def plugins(monkeypatch):
    def install(*entry_points: FakeEntryPoint):
        monkeypatch.setattr(
            registry,
            "entry_points",
            lambda group: list(entry_points) if group == registry.ENTRY_POINT_GROUP else [],
        )

    return install


def test_builtins_only_without_plugins(plugins):
    plugins()
    assert list(registry.available_sources()) == ["youtrack", "mattermost"]


def test_plugin_extends_the_pipeline(plugins):
    plugins(FakeEntryPoint("wiki", wiki_source()))
    assert list(registry.available_sources()) == ["youtrack", "mattermost", "wiki"]

    resolver = SpaceResolver.from_profile({"wiki": {"label": "Wiki"}})
    assert resolver.derive("wiki", "wiki/page.md") == ("yt:WIKI", "Wiki")


def test_name_mismatch_is_fatal(plugins):
    plugins(FakeEntryPoint("wiki", wiki_source("notwiki")))
    with pytest.raises(RuntimeError, match="entry-point name must match"):
        registry.available_sources()


def test_shadowing_a_builtin_is_fatal(plugins):
    plugins(FakeEntryPoint("youtrack", wiki_source("youtrack")))
    with pytest.raises(RuntimeError, match="registered twice"):
        registry.available_sources()


def test_wrong_object_is_fatal(plugins):
    plugins(FakeEntryPoint("wiki", lambda: None))
    with pytest.raises(RuntimeError, match="expected a sediment.sources.Source"):
        registry.available_sources()


def test_uninstalled_source_stays_a_valid_payload_value(plugins):
    """The stored vocabulary is wider than what this host can import.

    Points written elsewhere (telegram, claude) must still be declarable in a
    collection and filterable by the reader, so the contract keeps their names
    while derivation refuses to guess a space for them.
    """
    plugins()
    assert set(SOURCES) - set(registry.available_sources()) == {"claude", "telegram"}
    with pytest.raises(SpaceDerivationError, match="no importer installed"):
        SpaceResolver.from_profile({}).derive("telegram", "telegram/chat/1.md")

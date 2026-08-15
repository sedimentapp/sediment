"""Which sources this installation can import from.

Built-in sources are the ones that run unattended in a container: they need
nothing but a URL and a token. Everything else — sources bound to a particular
workstation's files or interactive login — ships as a separate distribution and
registers a `Source` instance under the "sediment.sources" entry-point group:

    [project.entry-points."sediment.sources"]
    telegram = "my_package.telegram:SOURCE"

A plugin that fails to import, or that collides with an already registered
name, is a fatal error rather than a skipped source: the pipeline reports what
it did not find as "nothing new", so a silently disabled importer looks exactly
like an idle one.

This is not the list of sources that may appear in stored payloads — that is
knowledge_schema.SOURCES, and it stays complete on hosts that import nothing.
"""

from functools import cache
from importlib.metadata import EntryPoint, entry_points

from sediment.sources import Source
from sediment.sources.mattermost import SOURCE as MATTERMOST
from sediment.sources.youtrack import SOURCE as YOUTRACK

ENTRY_POINT_GROUP = "sediment.sources"

BUILTIN_SOURCES: tuple[Source, ...] = (YOUTRACK, MATTERMOST)


def _load(entry_point: EntryPoint) -> Source:
    source = entry_point.load()
    if not isinstance(source, Source):
        raise RuntimeError(
            f"Source plugin {entry_point.name!r} ({entry_point.value}) resolved to "
            f"{type(source).__name__}, expected a sediment.sources.Source instance"
        )
    if source.name != entry_point.name:
        raise RuntimeError(
            f"Source plugin {entry_point.name!r} ({entry_point.value}) declares "
            f"name {source.name!r}; the entry-point name must match"
        )
    return source


@cache
def _plugin_sources() -> tuple[Source, ...]:
    builtin = {source.name for source in BUILTIN_SOURCES}
    loaded: dict[str, Source] = {}
    for entry_point in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda ep: ep.name):
        source = _load(entry_point)
        if source.name in builtin or source.name in loaded:
            raise RuntimeError(
                f"Source {source.name!r} is registered twice "
                f"(second registration: {entry_point.value})"
            )
        loaded[source.name] = source
    return tuple(loaded.values())


def available_sources() -> dict[str, Source]:
    """Importable sources by name, built-ins first."""
    return {source.name: source for source in (*BUILTIN_SOURCES, *_plugin_sources())}

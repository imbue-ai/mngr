"""The runtime registry of help topic pages.

Built-in topics are registered from markdown files in the docs tree (see
``builtin_help_topics.py``) and plugins contribute their own via the
``register_help_topics`` hook; both flow through the same hook and land in this
module-level registry. The :class:`TopicHelpPage` model and the
``build_topics_from_directory`` helper live in ``imbue.mngr.interfaces.help_topic``
so the plugin hookspec can reference the model without importing the CLI.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from imbue.mngr.interfaces.help_topic import TopicHelpPage

_topic_registry: dict[str, TopicHelpPage] = {}
_topic_alias_to_canonical: dict[str, str] = {}


def get_topic(name: str) -> TopicHelpPage | None:
    """Look up a topic by name or alias."""
    canonical = _topic_alias_to_canonical.get(name, name)
    return _topic_registry.get(canonical)


def get_all_topics() -> dict[str, TopicHelpPage]:
    """Return a copy of the topic registry."""
    return dict(_topic_registry)


@contextmanager
def preserve_topic_registry() -> Iterator[None]:
    """Snapshot the topic registry on entry and restore it on exit.

    The topic registry is populated once at import time (built-in topics plus
    any installed plugins' topics) and is not rebuilt per-test, so tests that
    register temporary topics use this to undo their additions without
    disturbing the topics other tests rely on.
    """
    saved_topics = dict(_topic_registry)
    saved_aliases = dict(_topic_alias_to_canonical)
    try:
        yield
    finally:
        _topic_registry.clear()
        _topic_registry.update(saved_topics)
        _topic_alias_to_canonical.clear()
        _topic_alias_to_canonical.update(saved_aliases)


def register_topic(topic: TopicHelpPage) -> bool:
    """Register a topic page unless any of its names is already taken.

    Returns True if the topic was registered, or False if its key or any of its
    aliases collides with an already-registered key or alias (in which case
    nothing changes -- registration is all-or-nothing, so no aliases are added
    on a collision). This gives the first-registered topic precedence on
    collisions -- mngr registers its built-in topics first so plugins cannot
    override them, including by shadowing a built-in key or alias with one of
    their own aliases.
    """
    taken_names = set(_topic_registry) | set(_topic_alias_to_canonical)
    if topic.key in taken_names or any(alias in taken_names for alias in topic.aliases):
        return False
    _topic_registry[topic.key] = topic
    for alias in topic.aliases:
        _topic_alias_to_canonical[alias] = topic.key
    return True

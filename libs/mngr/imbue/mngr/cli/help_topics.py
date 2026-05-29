"""The runtime registry of help topic pages.

Built-in topics are declared explicitly in ``builtin_help_topics.py`` (the
``_DOC_TOPICS`` registry, each backed by a markdown doc file, plus the inline
address topic) and plugins contribute their own via the ``register_help_topics``
hook; both flow through the same hook and land in this module-level registry. The
:class:`TopicHelpPage` model lives in ``imbue.mngr.interfaces.help_topic`` so the
plugin hookspec can reference it without importing the CLI.
"""

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

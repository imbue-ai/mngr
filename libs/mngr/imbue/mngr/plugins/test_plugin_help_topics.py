"""Tests for the plugin help-topics hook (register_help_topics)."""

from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pluggy
import pytest
from click.testing import CliRunner

import imbue.mngr.main
from imbue.mngr import hookimpl
from imbue.mngr.cli.help import load_help_topics_from_plugins
from imbue.mngr.cli.help_topics import _topic_alias_to_canonical
from imbue.mngr.cli.help_topics import _topic_registry
from imbue.mngr.cli.help_topics import get_topic
from imbue.mngr.interfaces.help_topic import DocFile
from imbue.mngr.interfaces.help_topic import InlineContent
from imbue.mngr.interfaces.help_topic import TopicHelpPage
from imbue.mngr.main import cli
from imbue.mngr.main import reset_plugin_manager
from imbue.mngr.plugins import hookspecs


@contextmanager
def preserve_topic_registry() -> Generator[None, None, None]:
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


class _PluginWithTopic:
    """A test plugin that contributes a single topic page."""

    @hookimpl
    def register_help_topics(self) -> Sequence[TopicHelpPage] | None:
        return [
            TopicHelpPage(
                key="plugin_topic_xyz",
                aliases=("ptx",),
                one_line_description="A topic from a test plugin",
                body=InlineContent(markdown="This topic is contributed by a plugin."),
            )
        ]


class _PluginWithNoTopics:
    """A test plugin that returns None (no topics)."""

    @hookimpl
    def register_help_topics(self) -> Sequence[TopicHelpPage] | None:
        return None


class _PluginOverridingBuiltinTopic:
    """A test plugin that tries to override the built-in 'address' topic."""

    @hookimpl
    def register_help_topics(self) -> Sequence[TopicHelpPage] | None:
        return [
            TopicHelpPage(
                key="address",
                one_line_description="HIJACKED by a plugin",
                body=InlineContent(markdown="This should never replace the built-in topic."),
            )
        ]


class _PluginShadowingBuiltinViaAlias:
    """A test plugin that tries to shadow the built-in 'address' topic via an alias."""

    @hookimpl
    def register_help_topics(self) -> Sequence[TopicHelpPage] | None:
        return [
            TopicHelpPage(
                key="plugin_unique_key_qrs",
                aliases=("address",),
                one_line_description="HIJACKED via alias",
                body=InlineContent(markdown="This should never shadow the built-in address topic."),
            )
        ]


class _PluginWithDocBackedTopic:
    """A test plugin whose topic body comes from a markdown file (DocFile)."""

    def __init__(self, body_path: Path) -> None:
        self._body_path = body_path

    @hookimpl
    def register_help_topics(self) -> Sequence[TopicHelpPage] | None:
        return [
            TopicHelpPage(
                key="from_dir_topic",
                one_line_description="Directory Topic",
                body=DocFile(path=self._body_path),
            )
        ]


@contextmanager
def _registered_plugin_topics(plugin: Any) -> Generator[pluggy.PluginManager, None, None]:
    """Register a plugin's help topics, restoring the global registry on exit."""
    reset_plugin_manager()
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    pm.register(plugin)

    old_pm = imbue.mngr.main._plugin_manager_container["pm"]
    imbue.mngr.main._plugin_manager_container["pm"] = pm

    try:
        with preserve_topic_registry():
            load_help_topics_from_plugins(pm)
            yield pm
    finally:
        imbue.mngr.main._plugin_manager_container["pm"] = old_pm


def test_plugin_topic_is_registered() -> None:
    """A plugin topic becomes resolvable via get_topic once registered."""
    with _registered_plugin_topics(_PluginWithTopic()):
        topic = get_topic("plugin_topic_xyz")
        assert topic is not None
        assert topic.one_line_description == "A topic from a test plugin"


def test_plugin_topic_alias_resolves() -> None:
    """A plugin topic's alias resolves to its canonical key."""
    with _registered_plugin_topics(_PluginWithTopic()):
        topic = get_topic("ptx")
        assert topic is not None
        assert topic.key == "plugin_topic_xyz"


def test_plugin_topic_appears_in_help_overview() -> None:
    """A plugin topic shows up in the 'mngr help' overview."""
    with _registered_plugin_topics(_PluginWithTopic()) as pm:
        result = CliRunner().invoke(cli, ["help"], obj=pm, catch_exceptions=False)
        assert result.exit_code == 0
        assert "plugin_topic_xyz" in result.output


def test_plugin_topic_page_is_viewable() -> None:
    """'mngr help <plugin topic>' renders the plugin topic page."""
    with _registered_plugin_topics(_PluginWithTopic()) as pm:
        result = CliRunner().invoke(cli, ["help", "plugin_topic_xyz"], obj=pm, catch_exceptions=False)
        assert result.exit_code == 0
        assert "This topic is contributed by a plugin." in result.output


def test_plugin_returning_no_topics_is_harmless() -> None:
    """A plugin returning None contributes no topics and does not error."""
    with _registered_plugin_topics(_PluginWithNoTopics()) as pm:
        result = CliRunner().invoke(cli, ["help"], obj=pm, catch_exceptions=False)
        assert result.exit_code == 0


@pytest.mark.allow_warnings
def test_plugin_cannot_override_builtin_topic() -> None:
    """A plugin topic whose key collides with a built-in is skipped (and logs a warning)."""
    with _registered_plugin_topics(_PluginOverridingBuiltinTopic()):
        topic = get_topic("address")
        assert topic is not None
        assert "HIJACKED" not in topic.one_line_description


@pytest.mark.allow_warnings
def test_plugin_cannot_shadow_builtin_topic_via_alias() -> None:
    """A plugin topic whose alias collides with a built-in topic's key is skipped.

    The plugin uses a unique key but an alias of 'address'; the built-in
    'address' topic must remain reachable and unchanged, and the plugin's own
    topic must not be registered (registration is all-or-nothing on collision).
    """
    with _registered_plugin_topics(_PluginShadowingBuiltinViaAlias()):
        address_topic = get_topic("address")
        assert address_topic is not None
        assert address_topic.key == "address"
        assert "HIJACKED" not in address_topic.one_line_description
        assert get_topic("plugin_unique_key_qrs") is None


def test_plugin_doc_backed_topic_is_registered(tmp_path: Path) -> None:
    """A plugin can contribute a topic whose body is a markdown file (DocFile)."""
    body = tmp_path / "from_dir_topic.md"
    body.write_text("# Directory Topic\n\nFrom a file.")
    with _registered_plugin_topics(_PluginWithDocBackedTopic(body)):
        topic = get_topic("from_dir_topic")
        assert topic is not None
        assert isinstance(topic.body, DocFile)
        assert "From a file." in topic.load_body()

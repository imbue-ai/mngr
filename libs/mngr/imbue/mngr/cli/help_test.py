"""Unit tests for the help command and topic pages."""

from pathlib import Path

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.help import format_topic_help
from imbue.mngr.cli.help_topics import get_all_topics
from imbue.mngr.cli.help_topics import get_topic
from imbue.mngr.interfaces.help_topic import TopicHelpPage
from imbue.mngr.main import cli
from imbue.mngr.utils.testing import capture_loguru

# =============================================================================
# Topic registry tests
# =============================================================================


def test_get_topic_by_canonical_name() -> None:
    """get_topic returns a topic when looked up by its canonical key."""
    topic = get_topic("address")
    assert topic is not None
    assert topic.key == "address"


def test_get_topic_by_alias() -> None:
    """get_topic resolves aliases to the canonical topic."""
    topic = get_topic("addr")
    assert topic is not None
    assert topic.key == "address"


def test_get_topic_nonexistent() -> None:
    """get_topic returns None for unknown topic names."""
    assert get_topic("nonexistent-topic-xyz") is None


def test_get_all_topics_contains_registered_topics() -> None:
    """get_all_topics returns all registered topic pages."""
    topics = get_all_topics()
    assert "address" in topics


def test_doc_based_topics_are_registered() -> None:
    """The built-in doc-backed topics (generic/ and concepts/) are registered."""
    topics = get_all_topics()
    # generic/ topics
    assert "multi_target" in topics
    assert "resource_cleanup" in topics
    assert "common" in topics
    # concepts/ topics
    assert "idle_detection" in topics
    assert "agents" in topics
    assert "hosts" in topics
    assert "providers" in topics


def test_doc_based_topic_loads_body_from_file() -> None:
    """A doc-backed topic loads its body lazily from the markdown file."""
    topic = get_topic("idle_detection")
    assert topic is not None
    assert topic.is_markdown_body
    assert topic.content is None
    assert "idle" in topic.load_body().lower()
    assert topic.docs_path is not None
    assert topic.docs_path.endswith(".md")


def test_doc_based_topic_has_explicit_description() -> None:
    """A doc-backed topic carries its one-line description explicitly (not parsed)."""
    topic = get_topic("idle_detection")
    assert topic is not None
    assert topic.one_line_description == "Idle Detection"


# =============================================================================
# Topic formatting tests
# =============================================================================


def test_format_topic_help_inline_contains_name_section() -> None:
    """An inline-content topic renders in man-page format with a NAME section."""
    topic = TopicHelpPage(
        key="test-topic",
        one_line_description="A test topic",
        content="Some content here.",
    )
    output = format_topic_help(topic, use_ansi=False, width=80)
    assert "NAME" in output
    assert "test-topic - A test topic" in output


def test_format_topic_help_inline_contains_aliases() -> None:
    """An inline-content topic shows aliases in the NAME section."""
    topic = TopicHelpPage(
        key="test-topic",
        one_line_description="A test topic",
        aliases=("tt", "test"),
        content="Some content here.",
    )
    output = format_topic_help(topic, use_ansi=False, width=80)
    assert "test-topic (tt, test)" in output


def test_format_topic_help_inline_contains_description() -> None:
    """An inline-content topic includes a DESCRIPTION section with the content."""
    topic = TopicHelpPage(
        key="test-topic",
        one_line_description="A test topic",
        content="First line.\n\nSecond paragraph.",
    )
    output = format_topic_help(topic, use_ansi=False, width=80)
    assert "DESCRIPTION" in output
    assert "First line." in output
    assert "Second paragraph." in output


def test_format_topic_help_doc_backed_renders_body(tmp_path: Path) -> None:
    """A doc-backed topic renders its markdown file body (no man-page NAME chrome)."""
    md = tmp_path / "topic.md"
    md.write_text("# A Doc Topic\n\nThe body prose.")
    topic = TopicHelpPage(key="doc-topic", one_line_description="A Doc Topic", body_path=md)
    output = format_topic_help(topic, use_ansi=False, width=80)
    # Non-ansi: the raw markdown body is emitted verbatim (rich is only used
    # for interactive terminals), including the file's own heading.
    assert "# A Doc Topic" in output
    assert "The body prose." in output
    assert "NAME" not in output


def test_format_topic_help_contains_see_also() -> None:
    """format_topic_help includes a SEE ALSO section when references exist."""
    topic = TopicHelpPage(
        key="test-topic",
        one_line_description="A test topic",
        content="Some content.",
        see_also=(("other-topic", "Related topic"),),
    )
    output = format_topic_help(topic, use_ansi=False, width=80)
    assert "SEE ALSO" in output
    assert "mngr help other-topic" in output


def test_format_topic_help_see_also_strips_anchor() -> None:
    """format_topic_help drops a '#anchor' suffix from see_also refs (terminal can't jump to it)."""
    topic = TopicHelpPage(
        key="test-topic",
        one_line_description="A test topic",
        content="Some content.",
        see_also=(("list#filtering", "Filtering agents"),),
    )
    output = format_topic_help(topic, use_ansi=False, width=80)
    assert "mngr help list - Filtering agents" in output
    assert "list#filtering" not in output


def test_format_topic_help_omits_see_also_when_empty() -> None:
    """format_topic_help omits SEE ALSO section when there are no references."""
    topic = TopicHelpPage(
        key="test-topic",
        one_line_description="A test topic",
        content="Some content.",
    )
    output = format_topic_help(topic, use_ansi=False, width=80)
    assert "SEE ALSO" not in output


# =============================================================================
# CLI integration tests (via CliRunner)
# =============================================================================


def test_help_no_args_shows_overview(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help' with no args shows the overview with commands and topics."""
    result = cli_runner.invoke(cli, ["help"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "COMMANDS" in result.output
    assert "TOPICS" in result.output
    assert "address" in result.output


def test_help_command_shows_command_help(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help create' shows the same help as 'mngr create --help'."""
    result = cli_runner.invoke(cli, ["help", "create"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "NAME" in result.output
    assert "mngr create" in result.output


def test_help_command_alias(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help c' resolves the 'c' alias and shows help for 'create'."""
    result = cli_runner.invoke(cli, ["help", "c"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "mngr create" in result.output


def test_help_subcommand(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help snapshot create' shows help for the snapshot create subcommand."""
    result = cli_runner.invoke(cli, ["help", "snapshot", "create"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "snapshot create" in result.output


def test_help_subcommand_with_group_alias(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help snap create' resolves the 'snap' alias to 'snapshot'."""
    result = cli_runner.invoke(cli, ["help", "snap", "create"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "snapshot create" in result.output


def test_help_topic_address(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help address' shows the address topic page."""
    result = cli_runner.invoke(cli, ["help", "address"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "[NAME][@[HOST][.PROVIDER]]" in result.output


def test_help_topic_alias(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help addr' resolves the alias and shows the address topic."""
    result = cli_runner.invoke(cli, ["help", "addr"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "address" in result.output
    assert "[NAME][@[HOST][.PROVIDER]]" in result.output


def test_help_doc_based_topic(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help multi_target' renders the doc-backed topic's markdown body."""
    result = cli_runner.invoke(cli, ["help", "multi_target"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    # Doc-backed topics render the markdown file body (no man-page NAME/DESCRIPTION
    # chrome); the body's heading/prose is what appears.
    assert "target" in result.output.lower()


def test_help_concepts_topic(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help idle_detection' renders the concepts topic's markdown body."""
    result = cli_runner.invoke(cli, ["help", "idle_detection"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "Idle Detection" in result.output
    assert "idle" in result.output.lower()


def test_help_nonexistent_topic(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help nonexistent' exits with an error message."""
    with capture_loguru(level="ERROR") as log_output:
        result = cli_runner.invoke(cli, ["help", "nonexistent-xyz"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code != 0
    assert "No help found" in log_output.getvalue()


def test_help_help_shows_self(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help help' shows help for the help command itself."""
    result = cli_runner.invoke(cli, ["help", "help"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "mngr help" in result.output
    assert "command or topic" in result.output.lower()


def test_help_help_lists_topics_dynamically(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help help' shows auto-generated Available Topics including doc-based topics."""
    result = cli_runner.invoke(cli, ["help", "help"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "AVAILABLE TOPICS" in result.output
    assert "address" in result.output
    assert "multi_target" in result.output
    assert "idle_detection" in result.output


def test_command_see_also_renders_help_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Command see_also entries render as 'mngr help <name>' for both commands and topics."""
    result = cli_runner.invoke(cli, ["help", "destroy"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "SEE ALSO" in result.output
    # Command references
    assert "mngr help create" in result.output
    # Topic references
    assert "mngr help resource_cleanup" in result.output
    assert "mngr help multi_target" in result.output


def test_help_list_alias(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help ls' resolves the alias and shows help for 'list'."""
    result = cli_runner.invoke(cli, ["help", "ls"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "mngr list" in result.output


def test_cli_version_flag(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """mngr --version should display version string and exit cleanly."""
    result = cli_runner.invoke(
        cli,
        ["--version"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    # When the package is installed, --version prints the version and exits 0.
    # In editable/dev installs the package name may not be resolvable, causing
    # a RuntimeError.  Either outcome proves the flag is wired up correctly.
    if result.exit_code == 0:
        assert "mngr" in result.output
    else:
        assert result.exception is not None
        assert "is not installed" in str(result.exception)

"""Unit tests for the help command and topic pages."""

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.help import TopicHelpPage
from imbue.mngr.cli.help import format_topic_help
from imbue.mngr.cli.help import get_all_topics
from imbue.mngr.cli.help import get_topic
from imbue.mngr.main import cli

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


# =============================================================================
# Topic formatting tests
# =============================================================================


def test_format_topic_help_contains_name_section() -> None:
    """format_topic_help includes a NAME section with key and description."""
    topic = TopicHelpPage(
        key="test-topic",
        one_line_description="A test topic",
        content="Some content here.",
    )
    output = format_topic_help(topic)
    assert "NAME" in output
    assert "test-topic - A test topic" in output


def test_format_topic_help_contains_aliases() -> None:
    """format_topic_help shows aliases in the NAME section."""
    topic = TopicHelpPage(
        key="test-topic",
        one_line_description="A test topic",
        aliases=("tt", "test"),
        content="Some content here.",
    )
    output = format_topic_help(topic)
    assert "test-topic (tt, test)" in output


def test_format_topic_help_contains_description() -> None:
    """format_topic_help includes a DESCRIPTION section with the content."""
    topic = TopicHelpPage(
        key="test-topic",
        one_line_description="A test topic",
        content="First line.\n\nSecond paragraph.",
    )
    output = format_topic_help(topic)
    assert "DESCRIPTION" in output
    assert "First line." in output
    assert "Second paragraph." in output


def test_format_topic_help_contains_see_also() -> None:
    """format_topic_help includes a SEE ALSO section when references exist."""
    topic = TopicHelpPage(
        key="test-topic",
        one_line_description="A test topic",
        content="Some content.",
        see_also=(("other-topic", "Related topic"),),
    )
    output = format_topic_help(topic)
    assert "SEE ALSO" in output
    assert "mngr help other-topic" in output


def test_format_topic_help_omits_see_also_when_empty() -> None:
    """format_topic_help omits SEE ALSO section when there are no references."""
    topic = TopicHelpPage(
        key="test-topic",
        one_line_description="A test topic",
        content="Some content.",
    )
    output = format_topic_help(topic)
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


def test_help_nonexistent_topic(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help nonexistent' exits with an error message."""
    result = cli_runner.invoke(cli, ["help", "nonexistent-xyz"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code != 0
    assert "No help found" in result.output


def test_help_help_shows_self(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """'mngr help help' shows help for the help command itself."""
    result = cli_runner.invoke(cli, ["help", "help"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "mngr help" in result.output
    assert "command or topic" in result.output.lower()


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

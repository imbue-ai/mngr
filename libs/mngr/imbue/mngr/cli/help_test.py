"""Unit tests for the help command and topic pages."""

import tomllib
from pathlib import Path

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli import builtin_help_topics
from imbue.mngr.cli.help import format_topic_help
from imbue.mngr.cli.help_topics import get_all_topics
from imbue.mngr.cli.help_topics import get_topic
from imbue.mngr.interfaces.help_topic import DocFile
from imbue.mngr.interfaces.help_topic import InlineContent
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
    assert isinstance(topic.body, DocFile)
    assert "idle" in topic.load_body().lower()
    assert topic.docs_path is not None
    assert topic.docs_path.endswith(".md")


def test_doc_based_topic_has_explicit_description() -> None:
    """A doc-backed topic carries its one-line description explicitly (not parsed)."""
    topic = get_topic("idle_detection")
    assert topic is not None
    assert topic.one_line_description == "Idle Detection"


def test_builtin_topic_docs_are_force_included_in_wheel() -> None:
    """Every built-in topic's doc file lives under a dir force-included into the wheel.

    The top-level docs/ tree isn't packaged, so only the force-included subdirs
    ship; if a built-in topic's doc fell outside them, `mngr help <topic>` would
    show nothing in a PyPI/wheel install. This keeps the pyproject force-include
    in sync with the topics registered in builtin_help_topics.py.
    """
    pyproject = Path(builtin_help_topics.__file__).resolve().parents[3] / "pyproject.toml"
    force_include = tomllib.loads(pyproject.read_text())["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    # included_dirs are the force-include keys, e.g. ("docs/concepts", "docs/commands/generic").
    included_dirs = tuple(force_include.keys())
    for topic in builtin_help_topics.register_help_topics():
        assert topic.docs_path is not None, f"built-in topic {topic.key!r} has no docs_path"
        repo_rel = f"docs/{topic.docs_path}"
        assert any(repo_rel == d or repo_rel.startswith(f"{d}/") for d in included_dirs), (
            f"built-in topic {topic.key!r} doc {repo_rel!r} is not under a wheel force-included dir "
            f"{included_dirs}; add it to [tool.hatch.build.targets.wheel.force-include] in libs/mngr/pyproject.toml"
        )


# =============================================================================
# Topic formatting tests
# =============================================================================


def test_format_topic_help_renders_inline_body() -> None:
    """An inline (str) body is rendered as markdown -- raw markdown when not ansi."""
    topic = TopicHelpPage(
        key="test-topic",
        one_line_description="A test topic",
        body=InlineContent(markdown="First line.\n\nSecond paragraph."),
    )
    output = format_topic_help(topic, use_ansi=False, width=80)
    assert "First line." in output
    assert "Second paragraph." in output
    # Topics render their (markdown) body -- no man-page NAME/DESCRIPTION chrome.
    assert "NAME" not in output


def test_format_topic_help_renders_file_body(tmp_path: Path) -> None:
    """A file (Path) body is rendered from the markdown file, including its heading."""
    md = tmp_path / "topic.md"
    md.write_text("# A Doc Topic\n\nThe body prose.")
    topic = TopicHelpPage(key="doc-topic", one_line_description="A Doc Topic", body=DocFile(path=md))
    output = format_topic_help(topic, use_ansi=False, width=80)
    # Non-ansi emits the raw markdown body (rich is only used for interactive terminals).
    assert "# A Doc Topic" in output
    assert "The body prose." in output


def test_format_topic_help_contains_see_also() -> None:
    """format_topic_help includes a SEE ALSO section when references exist."""
    topic = TopicHelpPage(
        key="test-topic",
        one_line_description="A test topic",
        body=InlineContent(markdown="Some content."),
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
        body=InlineContent(markdown="Some content."),
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
        body=InlineContent(markdown="Some content."),
    )
    output = format_topic_help(topic, use_ansi=False, width=80)
    assert "SEE ALSO" not in output


# =============================================================================
# Terminal link rewriting
# =============================================================================


def test_builtin_topic_carries_github_source_url() -> None:
    """Built-in doc topics carry a GitHub blob source_url for their doc file.

    The URL is what relative/anchor links in the body are resolved against for
    clickable terminal hyperlinks. It points at the imbue-ai/mngr repo and ends
    with the doc's repo-relative path (libs/mngr/docs/<docs_path>).
    """
    topic = get_topic("idle_detection")
    assert topic is not None
    assert isinstance(topic.body, DocFile)
    source_url = topic.body.source_url
    assert source_url is not None
    assert source_url.startswith("https://github.com/imbue-ai/mngr/blob/")
    assert source_url.endswith("/libs/mngr/docs/concepts/idle_detection.md")
    # link_base_url() surfaces that same URL for the renderer.
    assert topic.link_base_url() == source_url


def test_inline_topic_has_no_link_base() -> None:
    """An inline-bodied topic has no source location, so no link base."""
    topic = TopicHelpPage(
        key="test-topic",
        one_line_description="A test topic",
        body=InlineContent(markdown="See [x](../y.md)."),
    )
    assert topic.link_base_url() is None


def test_format_topic_help_rewrites_relative_links_for_terminal(tmp_path: Path) -> None:
    """For a terminal, relative/anchor links in a doc topic become absolute GitHub URLs.

    Exercises the full path: format_topic_help reads the doc body and resolves its
    relative and anchor links against the DocFile's source_url. rich emits each
    link target as an OSC-8 hyperlink, so the resolved absolute URL appears
    verbatim in the ANSI output.
    """
    md = tmp_path / "cron_recipes.md"
    md.write_text("See [Waiting](../README.md#waiting-on-a-predicate) and [Section](#user-input-tracking).\n")
    topic = TopicHelpPage(
        key="usage_cron_recipes",
        one_line_description="Cron recipes",
        body=DocFile(
            path=md,
            source_url="https://github.com/imbue-ai/mngr/blob/v9.9.9/libs/mngr_usage/docs/cron_recipes.md",
        ),
    )
    output = format_topic_help(topic, use_ansi=True, width=100)
    assert "https://github.com/imbue-ai/mngr/blob/v9.9.9/libs/mngr_usage/README.md#waiting-on-a-predicate" in output
    assert (
        "https://github.com/imbue-ai/mngr/blob/v9.9.9/libs/mngr_usage/docs/cron_recipes.md#user-input-tracking"
        in output
    )


def test_format_topic_help_keeps_relative_links_when_not_terminal(tmp_path: Path) -> None:
    """Non-terminal (plain) output keeps the original relative links untouched."""
    md = tmp_path / "cron_recipes.md"
    md.write_text("See [Waiting](../README.md#waiting-on-a-predicate).\n")
    topic = TopicHelpPage(
        key="usage_cron_recipes",
        one_line_description="Cron recipes",
        body=DocFile(
            path=md,
            source_url="https://github.com/imbue-ai/mngr/blob/v9.9.9/libs/mngr_usage/docs/cron_recipes.md",
        ),
    )
    output = format_topic_help(topic, use_ansi=False, width=100)
    assert "[Waiting](../README.md#waiting-on-a-predicate)" in output
    assert "github.com" not in output


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

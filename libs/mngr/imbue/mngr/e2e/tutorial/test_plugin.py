"""Tests for the plugin management commands from the tutorial.

The tests are intentionally kept as separate functions (not parametrized) so that
each one has a 1:1 correspondence with a tutorial script block.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _list_plugins(e2e: E2eSession, command: str) -> list[dict[str, str]]:
    """Run a `mngr plugin list ... --format json` command and return its rows.

    Each row is a dict of stringified field values (e.g. ``enabled`` is the
    string ``"true"`` / ``"false"``), matching the CLI's JSON serialization.
    """
    result = e2e.run(command)
    expect(result).to_succeed()
    return json.loads(result.stdout)["plugins"]


@pytest.mark.release
# Every mngr invocation pays a ~10s cold-start cost (importing and registering
# all plugins), which alone exceeds the 10s default per-test timeout, so this
# e2e test needs a higher budget like the other subprocess-driven e2e tests.
@pytest.mark.timeout(60)
def test_plugin_list_shows_installed(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all available plugins
        mngr plugin list
    """)
    result = e2e.run("mngr plugin list", comment="List all installed plugins")
    expect(result).to_succeed()
    # The listing renders a table whose columns come from the default fields
    # (name, version, description, enabled) -- these headers are produced by the
    # CLI itself, independent of which plugins happen to be installed.
    for header in ("NAME", "VERSION", "DESCRIPTION", "ENABLED"):
        expect(result.stdout).to_contain(header)
    # Core plugins that always ship inside the `imbue` package must be listed.
    expect(result.stdout).to_contain("claude")
    expect(result.stdout).to_contain("modal")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_list_active(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list only active plugins
        mngr plugin list --active
    """)
    # The bare command from the tutorial block must succeed.
    expect(e2e.run("mngr plugin list --active", comment="list only active plugins")).to_succeed()

    # Verify the documented behavior: --active lists ONLY enabled plugins.
    # Parse the JSON form so assertions key off structured fields rather than
    # scraping the human-formatted table.
    active = _list_plugins(e2e, "mngr plugin list --active --format json")
    assert active, "expected --active to list at least one plugin"
    disabled_in_active = [p["name"] for p in active if p["enabled"] != "true"]
    assert not disabled_in_active, f"--active listed disabled plugins: {disabled_in_active}"

    # Core invariant: --active is *exactly* the enabled subset of the unfiltered
    # list -- nothing more, nothing less. Capturing both before any mutation
    # means they describe the same state and must agree.
    all_plugins = _list_plugins(e2e, "mngr plugin list --format json")
    enabled_in_full = {p["name"] for p in all_plugins if p["enabled"] == "true"}
    active_names_before = {p["name"] for p in active}
    assert active_names_before == enabled_in_full, (
        f"--active should equal the enabled subset of the full list; "
        f"only in --active: {active_names_before - enabled_in_full}, "
        f"only enabled in full: {enabled_in_full - active_names_before}"
    )
    # The full list must also surface plugins that --active hides (the unfiltered
    # view is a strict superset), otherwise the filter isn't doing anything.
    disabled_in_full = {p["name"] for p in all_plugins if p["enabled"] != "true"}
    assert disabled_in_full, "expected the unfiltered list to include some disabled plugins"

    # `tutor` ships enabled by default, so it must start out active; otherwise
    # the disable below would be a no-op and prove nothing about the filter.
    assert "tutor" in active_names_before, "expected the tutor plugin to be active before disabling it"

    # Disabling a real plugin must drop it -- and only it -- from --active, while
    # the unfiltered list still reports it (as disabled). This is precisely what
    # --active does.
    expect(e2e.run("mngr plugin disable tutor --scope user", comment="disable a plugin")).to_succeed()

    active_names_after = {p["name"] for p in _list_plugins(e2e, "mngr plugin list --active --format json")}
    assert "tutor" not in active_names_after, "disabled plugin should not appear under --active"
    assert active_names_before - active_names_after == {"tutor"}, (
        "disabling tutor should remove exactly that plugin from --active; "
        f"removed: {active_names_before - active_names_after}"
    )

    all_by_name = {p["name"]: p for p in _list_plugins(e2e, "mngr plugin list --format json")}
    assert "tutor" in all_by_name, "unfiltered list should still show the disabled plugin"
    assert all_by_name["tutor"]["enabled"] == "false", "disabled plugin should report enabled=false"


@pytest.mark.release
# Every mngr invocation pays a ~10s cold-start cost (importing and registering
# all plugins), which alone exceeds the 10s default per-test timeout, so this
# e2e test needs a higher budget like the other subprocess-driven e2e tests.
@pytest.mark.timeout(60)
def test_plugin_add_by_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # add a plugin by name (from the registry)
        mngr plugin add my-plugin
    """)
    # `mngr plugin add` mutates the uv tool installation, but the test env runs
    # mngr from a project venv (not a `uv tool install`), so the command exits
    # cleanly with a controlled AbortError -- which click renders as exit code 1
    # with an "Aborted" message and no Python traceback -- rather than crashing.
    result = e2e.run("mngr plugin add my-plugin", comment="add a plugin by name")
    # Exit code 1 (not just any non-zero): this rules out a click usage error
    # (exit code 2), which would mean `my-plugin` was not accepted as the source
    # argument, and confirms the command reached the intentional abort path.
    expect(result).to_have_exit_code(1)
    combined_output = result.stdout + result.stderr
    expect(combined_output).to_contain("Aborted")
    expect(combined_output).not_to_contain("Traceback")
    # `my-plugin` must be parsed as the source argument, not rejected as an
    # unknown option.
    expect(combined_output).not_to_contain("No such option")
    # Verify the *reason* for the abort is the documented one: mngr is not
    # installed via `uv tool install`, so plugin management is unavailable. This
    # ties the test to the specific behavior rather than to any abort.
    expect(combined_output).to_contain("uv tool install")


@pytest.mark.release
# Like `test_plugin_add_by_name`, every mngr invocation pays a ~10s cold-start
# cost (importing and registering all plugins), which alone exceeds the 10s
# default per-test timeout, so this e2e test needs a higher budget.
@pytest.mark.timeout(60)
def test_plugin_add_by_path(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # add a plugin from a local path
        mngr plugin add --path /path/to/my-plugin
    """)
    result = e2e.run("mngr plugin add --path /path/to/my-plugin", comment="add a plugin from a local path")
    # `--path` is a recognized option, so the command must fail with a clean
    # abort (exit code 1) -- not a click usage error (exit code 2, which would
    # indicate the flag was unparsed) and not an uncaught crash. The install
    # cannot succeed regardless: either the receipt check fails (mngr is not
    # managed by `uv tool` in the test env) or the local path does not exist.
    # Both paths raise AbortError, which click renders as exit code 1.
    expect(result).to_have_exit_code(1)
    combined_output = result.stdout + result.stderr
    expect(combined_output).to_contain("Aborted")
    expect(combined_output).not_to_contain("Traceback")
    # An unknown option would make click emit "No such option" with exit code 2.
    expect(combined_output).not_to_contain("No such option")


@pytest.mark.release
# Every mngr invocation pays a ~10s cold-start cost (importing and registering
# all plugins), which alone exceeds the 10s default per-test timeout, so this
# e2e test needs a higher budget like the other subprocess-driven e2e tests.
@pytest.mark.timeout(60)
def test_plugin_add_by_git(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # add a plugin from a git repository
        mngr plugin add --git https://github.com/user/mngr-plugin.git
    """)
    # The git URL points at a non-existent repo, so the command always fails --
    # either at install time ("Failed to install plugin packages: ...") or, in an
    # environment where mngr is not managed by `uv tool`, at the receipt check.
    # Both paths raise AbortError, which click renders as exit code 1 with an
    # "Aborted:" message. Asserting on this (rather than just `exit_code != 0`)
    # confirms `--git` is accepted as a source specifier and that the command
    # reaches a clean, intentional error path -- not a click usage error (exit
    # code 2, which `!= 0` would also accept) or an uncaught traceback.
    result = e2e.run(
        "mngr plugin add --git https://github.com/user/mngr-plugin.git",
        comment="add a plugin from a git repository",
    )
    expect(result).to_have_exit_code(1)
    combined_output = result.stdout + result.stderr
    expect(combined_output).to_contain("Aborted")
    # `--git` must be recognized: an unknown option would make click emit "No such option".
    expect(combined_output).not_to_contain("No such option")
    # The abort must be a clean, user-facing error -- never an uncaught crash.
    expect(combined_output).not_to_contain("Traceback")


@pytest.mark.release
# Every mngr invocation pays a ~10s cold-start cost (importing and registering
# all plugins), which alone exceeds the 10s default per-test timeout, so this
# e2e test needs a higher budget like the other subprocess-driven e2e tests.
@pytest.mark.timeout(60)
def test_plugin_remove(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # remove a plugin
        mngr plugin remove my-plugin
    """)
    # `my-plugin` is not an installed plugin, so removal must fail. Whatever the
    # precise reason (mngr not installed via uv tool, or the package not being a
    # declared plugin), the command must fail *cleanly* -- a user-facing
    # "Aborted" error, never a bare Python traceback.
    result = e2e.run("mngr plugin remove my-plugin", comment="remove a plugin")
    expect(result).to_fail()
    combined_output = result.stdout + result.stderr
    expect(combined_output).to_contain("Aborted")
    expect(combined_output).not_to_contain("Traceback")


@pytest.mark.release
# Every mngr invocation pays a ~10s cold-start cost (importing and registering
# all plugins), which alone exceeds the 10s default per-test timeout, so this
# e2e test needs a higher budget like the other subprocess-driven e2e tests.
@pytest.mark.timeout(60)
def test_plugin_remove_rejects_invalid_name(e2e: E2eSession) -> None:
    # Unhappy path for the `mngr plugin remove` block: an unparseable package
    # name is rejected up front with a clear argument-validation error, before
    # any installation state is consulted (so this holds regardless of how mngr
    # itself was installed).
    e2e.write_tutorial_block("""
        # remove a plugin
        mngr plugin remove my-plugin
    """)
    result = e2e.run(
        "mngr plugin remove 'not a valid name'",
        comment="remove a plugin with an invalid package name",
    )
    # The name is rejected by argument validation, which raises AbortError ->
    # exit code 1. This distinguishes a clean, intentional rejection from a click
    # usage error (exit code 2, which `to_fail()` alone would also accept).
    expect(result).to_have_exit_code(1)
    combined_output = result.stdout + result.stderr
    # The error names the offending input so the user knows exactly what was
    # rejected, and arrives as a user-facing abort rather than a Python crash.
    expect(combined_output).to_contain("Invalid package name 'not a valid name'")
    expect(combined_output).to_contain("Aborted")
    expect(combined_output).not_to_contain("Traceback")


@pytest.mark.release
def test_plugin_enable_project_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # enable a plugin at the project scope
        mngr plugin enable my-plugin --scope project
    """)
    result = e2e.run(
        "mngr plugin enable my-plugin --scope project",
        comment="enable a plugin at the project scope",
    )
    # Enabling is a soft pre-configuration: it succeeds even for a plugin that
    # is not installed yet, recording the setting and warning that it will only
    # take effect once the plugin is installed.
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Enabled plugin 'my-plugin' in project")
    expect(result.stderr).to_contain("not currently registered")

    # Verify the concrete effect: the project settings.toml referenced in the
    # output now marks the plugin enabled. The path is printed in parentheses.
    path_match = re.search(r"\((?P<path>/\S+settings\.toml)\)", result.stdout)
    assert path_match is not None, f"could not find settings path in output: {result.stdout!r}"
    settings_path = Path(path_match.group("path"))
    # --scope project must route to the project-scope file (``settings.toml``),
    # not the local-scope override (``settings.local.toml``). The two live side
    # by side, so the basename is what distinguishes the scopes.
    assert settings_path.name == "settings.toml", settings_path
    settings_text = settings_path.read_text()
    parsed = tomllib.loads(settings_text)
    assert parsed["plugins"]["my-plugin"]["enabled"] is True, settings_text


@pytest.mark.release
def test_plugin_enable_rejects_invalid_scope(e2e: E2eSession) -> None:
    # Unhappy path for the `mngr plugin enable --scope` block: `--scope` is a
    # constrained choice (user/project/local), so an unrecognized value is
    # rejected up front by argument parsing -- a click usage error (exit code 2)
    # rather than a soft success that silently persists an unknown scope.
    e2e.write_tutorial_block("""
        # enable a plugin at the project scope
        mngr plugin enable my-plugin --scope project
    """)
    result = e2e.run(
        "mngr plugin enable my-plugin --scope bogus",
        comment="enable a plugin with an invalid scope",
    )
    expect(result).to_have_exit_code(2)
    combined_output = result.stdout + result.stderr
    expect(combined_output).to_contain("Invalid value")
    expect(combined_output).not_to_contain("Traceback")
    # The constrained scope must not have been persisted as a side effect.
    expect(combined_output).not_to_contain("Enabled plugin")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_disable_user_scope(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # disable a plugin at the user scope
        mngr plugin disable my-plugin --scope user
    """)
    result = e2e.run(
        "mngr plugin disable my-plugin --scope user",
        comment="disable a plugin at the user scope",
    )
    # Disabling is a soft operation: it persists the setting even for a plugin
    # that is not yet installed, so the user can pre-configure it. The command
    # therefore succeeds and warns that the plugin is not currently registered.
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Disabled plugin 'my-plugin' in user")
    expect(result.stderr).to_contain("not currently registered")
    # Verify the actual effect: the disabled state is persisted in the
    # user-scope config and reads back as false.
    read_back = e2e.run(
        "mngr config get plugins.my-plugin.enabled --scope user",
        comment="read back the persisted user-scope setting",
    )
    expect(read_back).to_succeed()
    expect(read_back.stdout.strip().lower()).to_contain("false")
    # The setting must land ONLY in the user scope: reading the same key from the
    # project scope finds nothing (the user-scope disable did not leak there),
    # confirming that --scope user was honored exactly rather than writing some
    # other scope that happens to share the merged value.
    project_read = e2e.run(
        "mngr config get plugins.my-plugin.enabled --scope project",
        comment="confirm the project scope was not affected",
    )
    expect(project_read).to_fail()
    expect(project_read.stdout + project_read.stderr).to_contain("Key not found")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_list_fields(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list plugins with specific fields
        mngr plugin list --fields "name,version,enabled"
    """)
    result = e2e.run(
        'mngr plugin list --fields "name,version,enabled"',
        comment="list plugins with specific fields",
    )
    expect(result).to_succeed()
    # Only the requested columns are shown, in the requested order.
    expect(result.stdout).to_match(r"NAME\s+VERSION\s+ENABLED")
    # Unrequested fields are omitted.
    expect(result.stdout).not_to_contain("DESCRIPTION")
    # The built-in `claude` plugin is present and the `enabled` column renders a
    # real boolean (not the `-` placeholder produced by an unknown field name).
    expect(result.stdout).to_contain("claude")
    expect(result.stdout).to_match(r"\btrue\b")
    # Cross-check against the structured JSON form: every row must carry exactly
    # the requested fields (and nothing else, e.g. no `description`), confirming
    # `--fields` filters the underlying data and not merely the table columns.
    rows = _list_plugins(e2e, 'mngr plugin list --fields "name,version,enabled" --format json')
    assert rows, "expected at least one plugin row"
    for row in rows:
        assert set(row) == {"name", "version", "enabled"}, row
    # The built-in `claude` plugin reports a real version and is enabled.
    claude_row = next(row for row in rows if row["name"] == "claude")
    assert claude_row["enabled"] == "true"
    assert claude_row["version"] not in ("", "-")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_list_unknown_field_renders_placeholder(e2e: E2eSession) -> None:
    # Shares the `--fields` tutorial block, but exercises the unhappy path: an
    # unrecognized field name is handled gracefully rather than erroring -- the
    # column simply renders the `-` placeholder for every plugin.
    e2e.write_tutorial_block("""
        # list plugins with specific fields
        mngr plugin list --fields "name,version,enabled"
    """)
    result = e2e.run(
        'mngr plugin list --fields "name,bogus"',
        comment="list plugins requesting an unknown field",
    )
    # An unknown field must not crash the command.
    expect(result).to_succeed()
    # The requested (unknown) column header still appears, in the requested order.
    expect(result.stdout).to_match(r"NAME\s+BOGUS")
    # Cross-check the JSON form so assertions key off structured data: every row
    # carries exactly the requested fields, and the unknown column is all `-`.
    rows = _list_plugins(e2e, 'mngr plugin list --fields "name,bogus" --format json')
    assert rows, "expected at least one plugin row"
    for row in rows:
        assert set(row) == {"name", "bogus"}, row
        assert row["bogus"] == "-"
    # The known `name` field is unaffected and still resolves real values.
    assert any(row["name"] == "claude" for row in rows)

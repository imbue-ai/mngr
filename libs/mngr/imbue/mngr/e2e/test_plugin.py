"""Tests for plugin system behavior via the real CLI."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.timeout(120)
def test_plugin_list_shows_installed(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # list all available plugins
        mngr plugin list
    """)
    result = e2e.run("mngr plugin list", comment="List all available plugins")
    expect(result).to_succeed()

    # The human output should render as a table with the default columns.
    for header in ("NAME", "VERSION", "DESCRIPTION", "ENABLED"):
        expect(result.stdout).to_contain(header)

    # The dev environment always has the claude plugin registered, and with no
    # disable applied it must show up as enabled (the ENABLED column reads
    # "true"). Locate the claude row precisely so we don't match plugins like
    # claude_usage that merely start with "claude".
    claude_rows = [line for line in result.stdout.splitlines() if line.split() and line.split()[0] == "claude"]
    assert len(claude_rows) == 1, f"expected exactly one claude row, got: {claude_rows}"
    assert claude_rows[0].split()[-1] == "true", f"claude should be enabled, row was: {claude_rows[0]!r}"


@pytest.mark.release
@pytest.mark.timeout(180)
def test_plugin_disable_enable_roundtrip(e2e: E2eSession) -> None:
    # Disable a plugin at the user scope. The user-scope config file (the
    # profile's settings.toml) is the one the e2e fixture has already opted
    # into pytest runs; writing the plugin toggle there preserves that opt-in,
    # whereas a fresh project-scope settings.toml would not carry it and would
    # make every subsequent mngr invocation refuse to run under pytest.
    disable_result = e2e.run(
        "mngr plugin disable claude --scope user",
        comment="Disable the claude plugin",
    )
    expect(disable_result).to_succeed()

    # Verify it shows as disabled in list
    list_after_disable = e2e.run(
        "mngr plugin list --format json",
        comment="Verify claude plugin is disabled",
    )
    expect(list_after_disable).to_succeed()
    plugins = json.loads(list_after_disable.stdout)["plugins"]
    claude_plugins = [p for p in plugins if p["name"] == "claude"]
    assert len(claude_plugins) == 1
    assert claude_plugins[0]["enabled"] == "false"
    # Disabling does not just flip a flag: it blocks the plugin's entry point
    # from loading, so its version/description metadata become unavailable
    # (rendered as "-"). Observing this confirms the real load-time effect.
    assert claude_plugins[0]["version"] == "-"

    # Re-enable it at the same (user) scope so the toggle reverses cleanly.
    enable_result = e2e.run(
        "mngr plugin enable claude --scope user",
        comment="Re-enable the claude plugin",
    )
    expect(enable_result).to_succeed()

    # Verify it shows as enabled again
    list_after_enable = e2e.run(
        "mngr plugin list --format json",
        comment="Verify claude plugin is enabled again",
    )
    expect(list_after_enable).to_succeed()
    plugins = json.loads(list_after_enable.stdout)["plugins"]
    claude_plugins = [p for p in plugins if p["name"] == "claude"]
    assert len(claude_plugins) == 1
    assert claude_plugins[0]["enabled"] == "true"
    # Re-enabling lets the entry point load again, so real metadata returns
    # (the inverse of the unavailable "-" seen while disabled).
    assert claude_plugins[0]["version"] != "-"


@pytest.mark.release
@pytest.mark.timeout(120)
def test_plugin_disable_affects_create(e2e: E2eSession) -> None:
    # Disable the claude plugin so its agent type should be unavailable
    expect(e2e.run("mngr plugin disable claude", comment="Disable claude plugin")).to_succeed()

    # Attempting to create a claude agent should fail
    create_result = e2e.run(
        "mngr create my-task claude --no-connect --no-ensure-clean",
        comment="Attempt to create claude agent with plugin disabled",
    )
    expect(create_result).to_fail()
    # It must fail *because* the plugin is disabled, not for some unrelated
    # reason. Check the combined output (the error is emitted on stderr) so the
    # assertion stays meaningful even if the message channel changes.
    combined_output = (create_result.stdout + "\n" + create_result.stderr).lower()
    expect(combined_output).to_contain("claude")
    expect(combined_output).to_contain("disabled")

    # Re-enable so teardown can clean up normally; this must now succeed.
    expect(e2e.run("mngr plugin enable claude", comment="Re-enable claude for cleanup")).to_succeed()

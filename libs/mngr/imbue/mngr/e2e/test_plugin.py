"""Tests for plugin system behavior via the real CLI."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
def test_plugin_list_shows_installed(e2e: E2eSession) -> None:
    result = e2e.run("mngr plugin list", comment="List all installed plugins")
    expect(result).to_succeed()
    # The dev environment always has the claude plugin registered
    expect(result.stdout).to_contain("claude")


@pytest.mark.release
def test_plugin_disable_enable_roundtrip(e2e: E2eSession) -> None:
    # Disable a plugin
    disable_result = e2e.run(
        "mngr plugin disable claude",
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

    # Re-enable it
    enable_result = e2e.run(
        "mngr plugin enable claude",
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


@pytest.mark.release
def test_plugin_disable_affects_create(e2e: E2eSession) -> None:
    # Disable the claude plugin so its agent type should be unavailable
    expect(e2e.run("mngr plugin disable claude", comment="Disable claude plugin")).to_succeed()

    # Attempting to create a claude agent should fail
    create_result = e2e.run(
        "mngr create my-task claude --no-connect --no-ensure-clean",
        comment="Attempt to create claude agent with plugin disabled",
    )
    expect(create_result).to_fail()

    # Re-enable so teardown can clean up normally
    e2e.run("mngr plugin enable claude", comment="Re-enable claude for cleanup")

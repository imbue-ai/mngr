"""Tests for plugin system behavior via the real CLI."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.timeout(120)
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

    # The user-facing consequence of disabling is that claude drops out of the
    # active set: `--active` only lists enabled plugins.
    active_after_disable = e2e.run(
        "mngr plugin list --active --format json",
        comment="Verify claude is no longer active",
    )
    expect(active_after_disable).to_succeed()
    active_names = {p["name"] for p in json.loads(active_after_disable.stdout)["plugins"]}
    assert "claude" not in active_names

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

    # Re-enabling restores claude to the active set.
    active_after_enable = e2e.run(
        "mngr plugin list --active --format json",
        comment="Verify claude is active again",
    )
    expect(active_after_enable).to_succeed()
    active_names = {p["name"] for p in json.loads(active_after_enable.stdout)["plugins"]}
    assert "claude" in active_names


@pytest.mark.release
@pytest.mark.timeout(120)
def test_plugin_disable_affects_create(e2e: E2eSession) -> None:
    # Disable the claude plugin so its agent type should be unavailable.
    # Use the local scope so the change lands in settings.local.toml, which the
    # e2e fixture has already opted into pytest (is_allowed_in_pytest = true).
    # Disabling at the default project scope would instead create a fresh
    # settings.toml lacking that opt-in, and every subsequent mngr command would
    # then fail the pytest config guard -- masking the disabled-plugin behavior
    # this test is meant to verify.
    expect(
        e2e.run("mngr plugin disable claude --scope local", comment="Disable claude plugin")
    ).to_succeed()

    # The disable must actually take effect: claude shows as disabled in the
    # listing (and the listing succeeding proves config still loads cleanly).
    list_result = e2e.run("mngr plugin list --format json", comment="Verify claude plugin is disabled")
    expect(list_result).to_succeed()
    claude_plugins = [p for p in json.loads(list_result.stdout)["plugins"] if p["name"] == "claude"]
    assert len(claude_plugins) == 1
    assert claude_plugins[0]["enabled"] == "false"

    # Attempting to create a claude agent should fail because its agent type is
    # no longer available -- not because of any unrelated config-guard error.
    create_result = e2e.run(
        "mngr create my-task claude --no-connect --no-ensure-clean",
        comment="Attempt to create claude agent with plugin disabled",
    )
    expect(create_result).to_fail()
    combined_output = create_result.stdout + create_result.stderr
    assert "is_allowed_in_pytest" not in combined_output, (
        "create failed on the pytest config guard rather than the disabled plugin: " + combined_output
    )
    assert "claude" in combined_output.lower()
    assert "disabled" in combined_output.lower(), (
        "create should fail specifically because the claude plugin is disabled: " + combined_output
    )

    # Re-enable so teardown can clean up normally. This must genuinely succeed
    # now that the toggle lives in an opted-in config file.
    expect(
        e2e.run("mngr plugin enable claude --scope local", comment="Re-enable claude for cleanup")
    ).to_succeed()

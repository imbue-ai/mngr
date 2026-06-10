"""Tests for plugin system behavior via the real CLI."""

import json
from pathlib import Path

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _opt_project_config_into_pytest(project_config_dir: Path) -> None:
    """Seed the project-scope ``settings.toml`` with the pytest opt-in.

    ``mngr plugin disable``/``enable`` default to ``--scope project``, which
    writes ``.<root_name>/settings.toml``. The e2e fixture deliberately leaves
    that file unseeded (so config-creation tutorial tests see genuine first-use
    behavior), so without this the subprocess mngr would create a project
    settings.toml lacking ``is_allowed_in_pytest = true`` and every subsequent
    ``mngr`` invocation in the same test would abort on the pytest config guard.
    The toggle commands preserve this key when they append the plugin entry.
    """
    (project_config_dir / "settings.toml").write_text("is_allowed_in_pytest = true\n")


@pytest.mark.release
@pytest.mark.timeout(300)
def test_plugin_disable_enable_roundtrip(e2e: E2eSession, project_config_dir: Path) -> None:
    _opt_project_config_into_pytest(project_config_dir)
    # Capture the initial plugin list so we can assert the roundtrip restores it
    list_before = e2e.run(
        "mngr plugin list --format json",
        comment="Capture initial plugin list",
    )
    expect(list_before).to_succeed()
    plugins_before = json.loads(list_before.stdout)["plugins"]
    # claude must start out enabled, otherwise the roundtrip below is vacuous
    assert [p for p in plugins_before if p["name"] == "claude"][0]["enabled"] == "true"

    # Disable a plugin. Use --scope user so the toggle modifies the profile's
    # settings.toml (seeded with is_allowed_in_pytest = true) rather than
    # creating a fresh project-scope settings.toml without that opt-in, which
    # would make every subsequent mngr command fail the pytest config guard.
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

    # Re-enable it at the same scope it was disabled at.
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

    # The roundtrip must restore the full plugin list exactly: re-enabling
    # brings back not just the enabled flag but also the version/description
    # metadata (which is unavailable while the plugin is disabled and unloaded).
    assert plugins == plugins_before


@pytest.mark.release
@pytest.mark.timeout(60)
def test_plugin_disable_affects_create(e2e: E2eSession) -> None:
    # Disable the claude plugin so its agent type should be unavailable.
    # Use --scope user so the toggle lands in the profile's settings.toml
    # (which the e2e fixture seeds with is_allowed_in_pytest = true). Writing a
    # fresh project-scope settings.toml would lack that opt-in, and the next
    # mngr command would then fail the pytest config guard for an unrelated
    # reason instead of the disabled-plugin error this test asserts on.
    expect(e2e.run("mngr plugin disable claude --scope user", comment="Disable claude plugin")).to_succeed()

    # Attempting to create a claude agent should fail
    create_result = e2e.run(
        "mngr create my-task claude --no-connect --no-ensure-clean",
        comment="Attempt to create claude agent with plugin disabled",
    )
    expect(create_result).to_fail()

    # The failure must be *because* the plugin is disabled, not for some
    # unrelated reason (e.g. a config-loading guard tripping on a freshly
    # written settings.toml). Assert on the actual error so a regression that
    # makes create fail for the wrong reason cannot masquerade as a pass.
    create_output = create_result.stdout + create_result.stderr
    expect(create_output).to_contain("plugin 'claude' is disabled")
    expect(create_output).to_contain("mngr plugin enable claude")
    # Guard specifically against the pytest opt-in guard masking the real
    # behavior (it previously made this test pass for the wrong reason).
    expect(create_output).not_to_contain("is_allowed_in_pytest")

    # While disabled, claude must be absent from the available agent types --
    # this is exactly the precondition `mngr create` consults, so it confirms
    # the create failure above was the agent type genuinely being unavailable.
    list_while_disabled = e2e.run(
        "mngr plugin list --kind agent-type --active --format json",
        comment="List active agent types while claude is disabled",
    )
    expect(list_while_disabled).to_succeed()
    disabled_agent_types = [p["name"] for p in json.loads(list_while_disabled.stdout)["plugins"]]
    assert "claude" not in disabled_agent_types, disabled_agent_types

    # Re-enabling the plugin must succeed (also lets teardown clean up normally).
    # Enable at the same scope it was disabled at, otherwise the user-scope
    # disable would still win and claude would remain unavailable.
    expect(e2e.run("mngr plugin enable claude --scope user", comment="Re-enable claude for cleanup")).to_succeed()

    # After re-enabling, the claude agent type is available again, so a fresh
    # create would no longer be gated.
    list_after_enable = e2e.run(
        "mngr plugin list --kind agent-type --active --format json",
        comment="List active agent types after re-enabling claude",
    )
    expect(list_after_enable).to_succeed()
    enabled_agent_types = [p["name"] for p in json.loads(list_after_enable.stdout)["plugins"]]
    assert "claude" in enabled_agent_types, enabled_agent_types

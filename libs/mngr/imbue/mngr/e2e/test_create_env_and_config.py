"""Tests for environment variables, config, and templates from the tutorial."""

import json
import uuid

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.mngr.utils.polling import wait_for
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can set environment variables for the agent:
    mngr create my-task --env DEBUG=true
    # (--env-file loads from a file, --pass-env forwards a variable from your current shell)
    """)
    # Use a unique value so we can verify it appears in the tmux pane
    env_value = uuid.uuid4().hex
    expect(
        e2e.run(
            f"mngr create my-task --env MNGR_TEST_VAR={env_value}"
            " --command 'echo MNGR_TEST_VAR=$MNGR_TEST_VAR && sleep 99999'"
            " --no-ensure-clean",
            comment="you can set environment variables for the agent",
        )
    ).to_succeed()

    # Verify the env var is visible in the agent's tmux pane.
    # The command prints MNGR_TEST_VAR=<value> before sleeping, so it
    # should appear in the captured pane content. The session name is
    # {MNGR_PREFIX}{agent_name}, and tmux commands use the e2e fixture's
    # TMUX_TMPDIR to find the right server.
    def _env_var_visible() -> bool:
        capture = e2e.run(
            "tmux capture-pane -t $(tmux list-sessions -F '#{session_name}' | grep my-task) -p",
            comment="Capture tmux pane to verify env var",
        )
        return env_value in capture.stdout

    wait_for(_env_var_visible, timeout=10.0, error_message=f"Expected {env_value} in tmux pane")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_with_pass_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # it is *strongly encouraged* to use either use --env-file or --pass-env, especially for any sensitive environment variables (like API keys) rather than --env, because that way they won't end up in your shell history or in your config files by accident. For example:
    export API_KEY=abc123
    mngr create my-task --pass-env API_KEY
    # that command passes the API_KEY environment variable from your current shell into the agent's environment, without you having to specify the value on the command line.
    """)
    expect(
        e2e.run(
            "API_KEY=abc123 mngr create my-task --pass-env API_KEY --command 'sleep 99999' --no-ensure-clean",
            comment="it is *strongly encouraged* to use either use --env-file or --pass-env",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent created with --pass-env")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
def test_create_with_template_modal_disabled(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use templates to quickly apply a set of preconfigured options:
    echo '[create_templates.my_modal_template]' >> .mngr/settings.local.toml
    echo 'provider = "modal"' >> .mngr/settings.local.toml
    echo 'build_args = "cpu=4"' >> .mngr/settings.local.toml
    mngr create my-task --template my_modal_template
    # templates are defined in your config (see the CONFIGURATION section for more) and can be stacked: --template modal --template codex
    # templates take exactly the same parameters as the create command
    # -t is short for --template. Many commands have a short form (see the "--help")
    """)
    # Append template config to the existing settings.local.toml.
    # The e2e env uses .$MNGR_ROOT_NAME/ as the config directory (not .mngr/).
    cfg = ".$MNGR_ROOT_NAME/settings.local.toml"
    expect(
        e2e.run(
            f"echo '' >> {cfg}"
            f" && echo '[create_templates.my_modal_template]' >> {cfg}"
            f" && echo 'provider = \"modal\"' >> {cfg}"
            f" && echo 'build_args = \"cpu=4\"' >> {cfg}",
            comment="you can use templates to quickly apply a set of preconfigured options",
        )
    ).to_succeed()

    # The template sets provider=modal which is disabled, so create should fail
    result = e2e.run(
        "mngr create my-task --template my_modal_template --command 'sleep 99999' --no-ensure-clean",
        comment="templates are defined in your config",
    )
    # Expect failure because the modal provider is disabled in the test environment
    expect(result).to_fail()
    # The error should reference the modal provider being disabled, not an unknown template
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)modal|provider|disabled")


@pytest.mark.release
def test_create_with_plugin_flags(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can enable or disable specific plugins:
    mngr create my-task --plugin my-plugin --disable-plugin other-plugin
    """)
    result = e2e.run(
        "mngr create my-task --plugin my-plugin --disable-plugin other-plugin --command 'sleep 99999' --no-ensure-clean",
        comment="you can enable or disable specific plugins",
    )
    # The plugin flags should be accepted by the CLI (no "No such option" error).
    # The command may fail because my-plugin doesn't exist, which is expected.
    combined = result.stdout + result.stderr
    expect(combined).not_to_contain("No such option")
    expect(combined).not_to_contain("no such option")
    # Verify the error (if any) is about the plugin, not a crash
    if result.exit_code != 0:
        expect(combined).to_match(r"(?i)plugin|not found|unknown")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_in_place_alias_target(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you should probably use aliases for making little shortcuts for yourself, because many of the commands can get a bit long:
    echo "alias mc='mngr create --transfer=none'" >> ~/.bashrc && source ~/.bashrc
    # or use a more sophisticated tool, like Espanso
    """)
    expect(
        e2e.run(
            "mngr create my-task --transfer=none --command 'sleep 99999' --no-ensure-clean",
            comment="you should probably use aliases for making little shortcuts for yourself",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent created with --transfer=none")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
def test_config_set_headless(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # or you can set that option in your config so that it always applies:
    mngr config set headless true
    """)
    result = e2e.run(
        "mngr config set headless true",
        comment="or you can set that option in your config so that it always applies",
    )
    expect(result).to_succeed()

    # Verify the value was persisted (read from project scope where it was written)
    get_result = e2e.run("mngr config get headless --scope project", comment="Verify headless config was set")
    expect(get_result).to_succeed()
    expect(get_result.stdout).to_contain("true")


@pytest.mark.release
@pytest.mark.modal
def test_env_var_mngr_headless(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # or you can set it as an environment variable:
    export MNGR_HEADLESS=true
    """)
    result = e2e.run(
        "MNGR_HEADLESS=true mngr list",
        comment="or you can set it as an environment variable",
    )
    expect(result).to_succeed()


@pytest.mark.release
def test_config_set_default_provider(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # *all* mngr options work like that. For example, if you want to always run agents in Modal by default, you can set that in your config:
    mngr config set commands.create.provider modal
    # for more on configuration, see the CONFIGURATION section below
    """)
    result = e2e.run(
        "mngr config set commands.create.provider modal",
        comment="*all* mngr options work like that",
    )
    expect(result).to_succeed()

    # Verify the value was persisted (read from project scope where it was written)
    get_result = e2e.run(
        "mngr config get commands.create.provider --scope project",
        comment="for more on configuration, see the CONFIGURATION section below",
    )
    expect(get_result).to_succeed()
    expect(get_result.stdout).to_contain("modal")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_with_label(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can add labels to organize your agents and tags for host metadata:
    mngr create my-task --label team=backend --host-label env=staging
    """)
    expect(
        e2e.run(
            "mngr create my-task --command 'sleep 99999' --no-ensure-clean --label team=backend --host-label env=staging",
            comment="you can add labels to organize your agents and tags for host metadata",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list --format json", comment="Verify label appears in JSON output")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching_agents = [a for a in agents if a["name"] == "my-task"]
    assert len(matching_agents) == 1
    assert matching_agents[0]["labels"]["team"] == "backend"

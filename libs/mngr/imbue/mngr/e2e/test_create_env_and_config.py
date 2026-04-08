"""Tests for environment variables, config, and templates from the tutorial."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can set environment variables for the agent:
    mngr create my-task --env DEBUG=true
    # (--env-file loads from a file, --pass-env forwards a variable from your current shell)
    """)
    expect(
        e2e.run(
            "mngr create my-task --env DEBUG=true"
            " --command 'printenv DEBUG > .env_check && sleep 99999'"
            " --no-ensure-clean",
            comment="you can set environment variables for the agent",
        )
    ).to_succeed()

    # The agent command writes the env var to .env_check in its work_dir.
    # Poll for the file since the agent shell may need a moment to process the command.
    env_result = e2e.run(
        "mngr exec my-task"
        " 'for i in 1 2 3 4 5; do [ -f .env_check ] && cat .env_check && exit 0; sleep 1; done; exit 1'",
        comment="Verify DEBUG env var is set inside the agent",
    )
    expect(env_result).to_succeed()
    expect(env_result.stdout).to_contain("true")


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_pass_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # it is *strongly encouraged* to either use --env-file or --pass-env, especially for any sensitive environment variables (like API keys) rather than --env, because that way they won't end up in your shell history or in your config files by accident. For example:
    export API_KEY=abc123
    mngr create my-task --pass-env API_KEY
    # that command passes the API_KEY environment variable from your current shell into the agent's environment, without you having to specify the value on the command line.
    """)
    expect(
        e2e.run(
            "API_KEY=abc123 mngr create my-task --pass-env API_KEY --command 'sleep 99999' --no-ensure-clean",
            comment="pass API_KEY from current shell into the agent's environment",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent was created")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # Verify the env var was actually stored in the agent's env file on disk
    env_file_result = e2e.run(
        "cat $MNGR_HOST_DIR/agents/*/env",
        comment="Verify API_KEY was forwarded into agent environment",
    )
    expect(env_file_result).to_succeed()
    expect(env_file_result.stdout).to_contain("API_KEY=abc123")


@pytest.mark.release
def test_create_with_template_modal_disabled(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use templates to quickly apply a set of preconfigured options:
    echo '[create_templates.my_modal_template]' >> .mngr/settings.local.toml
    echo 'provider = "modal"' >> .mngr/settings.local.toml
    echo 'build_arg = ["cpu=4"]' >> .mngr/settings.local.toml
    mngr create my-task --template my_modal_template
    # templates are defined in your config (see the CONFIGURATION section for more) and can be stacked: --template modal --template codex
    # templates take exactly the same parameters as the create command
    # -t is short for --template. Many commands have a short form (see the "--help")
    """)
    # Append template config and disable the modal plugin in settings.local.toml.
    # The e2e env uses .$MNGR_ROOT_NAME/ as the config directory (not .mngr/).
    cfg = ".$MNGR_ROOT_NAME/settings.local.toml"
    expect(
        e2e.run(
            f"echo '' >> {cfg}"
            f" && echo '[create_templates.my_modal_template]' >> {cfg}"
            f" && echo 'provider = \"modal\"' >> {cfg}"
            f" && echo 'build_arg = [\"cpu=4\"]' >> {cfg}"
            f" && echo '' >> {cfg}"
            f" && echo '[plugins.modal]' >> {cfg}"
            f" && echo 'enabled = false' >> {cfg}",
            comment="you can use templates to quickly apply a set of preconfigured options",
        )
    ).to_succeed()

    # The template sets provider=modal, but the modal plugin is disabled
    result = e2e.run(
        "mngr create my-task --template my_modal_template --command 'sleep 99999' --no-ensure-clean",
        comment="templates are defined in your config",
    )
    # Expect failure because the modal provider is disabled
    expect(result).to_fail()
    # The error should reference the modal provider being unavailable
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)modal|provider")


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
    # The command fails because the plugins don't exist, which is expected.
    combined = result.stdout + result.stderr
    expect(combined).not_to_contain("No such option")
    expect(combined).not_to_contain("no such option")
    expect(combined).not_to_contain("Traceback")
    expect(result).to_fail()
    expect(combined).to_match(r"(?i)plugin.*not registered")


@pytest.mark.release
@pytest.mark.tmux
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

    # Verify the agent runs in-place (same directory), not in a worktree
    pwd_result = e2e.run("mngr exec my-task pwd", comment="Verify agent runs in the original directory (in-place)")
    expect(pwd_result).to_succeed()
    cwd_result = e2e.run("pwd", comment="Get the current working directory for comparison")
    expect(cwd_result).to_succeed()
    expect(pwd_result.stdout).to_contain(cwd_result.stdout.strip())


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
    expect(result.stdout).to_contain("Set headless")

    # Verify the value was persisted via the merged config view (default scope)
    get_result = e2e.run("mngr config get headless", comment="Verify headless config is visible in merged view")
    expect(get_result).to_succeed()
    expect(get_result.stdout).to_contain("true")


@pytest.mark.release
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

    # Verify the env var is picked up by the config system (merged config reflects it)
    get_result = e2e.run(
        "MNGR_HEADLESS=true mngr config get headless",
        comment="Verify MNGR_HEADLESS env var is reflected in resolved config",
    )
    expect(get_result).to_succeed()
    expect(get_result.stdout).to_contain("true")

    # Verify headless is not set when the env var is absent
    get_without = e2e.run(
        "mngr config get headless",
        comment="Without MNGR_HEADLESS, headless should be false",
    )
    expect(get_without).to_succeed()
    expect(get_without.stdout).to_contain("false")


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
    expect(result.stdout).to_contain("commands.create.provider")
    expect(result.stdout).to_contain("modal")

    # Verify the value was persisted (read from project scope where it was written)
    get_result = e2e.run(
        "mngr config get commands.create.provider --scope project",
        comment="Verify the default provider config was persisted",
    )
    expect(get_result).to_succeed()
    expect(get_result.stdout).to_contain("modal")


@pytest.mark.release
@pytest.mark.tmux
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

    list_result = e2e.run("mngr list --format json", comment="Verify labels appear in JSON output")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching_agents = [a for a in agents if a["name"] == "my-task"]
    assert len(matching_agents) == 1
    assert matching_agents[0]["labels"]["team"] == "backend"
    assert matching_agents[0]["host"]["tags"]["env"] == "staging"

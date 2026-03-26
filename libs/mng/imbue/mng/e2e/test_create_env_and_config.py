"""Tests for environment variables, config, and templates from the tutorial."""

import pytest

from imbue.mng.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can set environment variables for the agent:
    mng create my-task --env DEBUG=true
    # (--env-file loads from a file, --pass-env forwards a variable from your current shell)
    """)
    expect(
        e2e.run(
            "mng create my-task --env DEBUG=true --command 'sleep 99999' --no-ensure-clean",
            comment="you can set environment variables for the agent",
        )
    ).to_succeed()

    env_result = e2e.run(
        "mng exec my-task 'printenv DEBUG'",
        comment="Verify DEBUG env var is set inside the agent",
    )
    expect(env_result).to_succeed()
    expect(env_result.stdout).to_contain("true")


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_pass_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # it is *strongly encouraged* to use either use --env-file or --pass-env, especially for any sensitive environment variables (like API keys) rather than --env, because that way they won't end up in your shell history or in your config files by accident. For example:
    export API_KEY=abc123
    mng create my-task --pass-env API_KEY
    # that command passes the API_KEY environment variable from your current shell into the agent's environment, without you having to specify the value on the command line.
    """)
    expect(
        e2e.run(
            "API_KEY=abc123 mng create my-task --pass-env API_KEY --command 'sleep 99999' --no-ensure-clean",
            comment="it is *strongly encouraged* to use either use --env-file or --pass-env",
        )
    ).to_succeed()

    list_result = e2e.run("mng list", comment="Verify agent created with --pass-env")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
def test_create_with_template_modal_disabled(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use templates to quickly apply a set of preconfigured options:
    echo '[create_templates.my_modal_template]' >> .mng/settings.local.toml
    echo 'provider = "modal"' >> .mng/settings.local.toml
    echo 'build_args = "cpu=4"' >> .mng/settings.local.toml
    mng create my-task --template my_modal_template
    # templates are defined in your config (see the CONFIGURATION section for more) and can be stacked: --template modal --template codex
    # templates take exactly the same parameters as the create command
    # -t is short for --template. Many commands have a short form (see the "--help")
    """)
    # Append template config to the existing settings.local.toml.
    # The e2e env uses .$MNG_ROOT_NAME/ as the config directory (not .mng/).
    cfg = ".$MNG_ROOT_NAME/settings.local.toml"
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
        "mng create my-task --template my_modal_template --command 'sleep 99999' --no-ensure-clean",
        comment="templates are defined in your config",
    )
    # Expect failure because the modal provider is disabled in the test environment
    expect(result).to_fail()
    # The error should reference the modal provider being disabled, not an unknown template
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)modal|provider|disabled")


@pytest.mark.release
@pytest.mark.tmux
def test_create_with_plugin_flags(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can enable or disable specific plugins:
    mng create my-task --plugin my-plugin --disable-plugin other-plugin
    """)
    result = e2e.run(
        "mng create my-task --plugin my-plugin --disable-plugin other-plugin --command 'sleep 99999' --no-ensure-clean",
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
def test_create_in_place_alias_target(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you should probably use aliases for making little shortcuts for yourself, because many of the commands can get a bit long:
    echo "alias mc='mng create --transfer=none'" >> ~/.bashrc && source ~/.bashrc
    # or use a more sophisticated tool, like Espanso
    """)
    expect(
        e2e.run(
            "mng create my-task --transfer=none --command 'sleep 99999' --no-ensure-clean",
            comment="you should probably use aliases for making little shortcuts for yourself",
        )
    ).to_succeed()

    list_result = e2e.run("mng list", comment="Verify agent created with --transfer=none")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")


@pytest.mark.release
def test_config_set_headless(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # or you can set that option in your config so that it always applies:
    mng config set headless true
    """)
    result = e2e.run(
        "mng config set headless true",
        comment="or you can set that option in your config so that it always applies",
    )
    expect(result).to_succeed()

    # Verify the value was persisted (read from project scope where it was written)
    get_result = e2e.run("mng config get headless --scope project", comment="Verify headless config was set")
    expect(get_result).to_succeed()
    expect(get_result.stdout).to_contain("true")


@pytest.mark.release
def test_env_var_mng_headless(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # or you can set it as an environment variable:
    export MNG_HEADLESS=true
    """)
    result = e2e.run(
        "MNG_HEADLESS=true mng list",
        comment="or you can set it as an environment variable",
    )
    expect(result).to_succeed()


@pytest.mark.release
def test_config_set_default_provider(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # *all* mng options work like that. For example, if you want to always run agents in Modal by default, you can set that in your config:
    mng config set commands.create.provider modal
    # for more on configuration, see the CONFIGURATION section below
    """)
    result = e2e.run(
        "mng config set commands.create.provider modal",
        comment="*all* mng options work like that",
    )
    expect(result).to_succeed()

    # Verify the value was persisted (read from project scope where it was written)
    get_result = e2e.run(
        "mng config get commands.create.provider --scope project",
        comment="for more on configuration, see the CONFIGURATION section below",
    )
    expect(get_result).to_succeed()
    expect(get_result.stdout).to_contain("modal")

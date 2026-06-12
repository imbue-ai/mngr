"""Tests for environment variables, config, and templates from the tutorial."""

import json
import uuid

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.mngr.utils.polling import wait_for
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can set environment variables for the agent:
    mngr create my-task --env DEBUG=true
    # (--env-file loads from a file, --pass-env forwards a variable from your current shell)
    """)
    # Use a unique value so we can verify it appears in the tmux pane
    env_value = uuid.uuid4().hex
    # Run the agent body via ``bash -c`` so the env-var expansion and ``&&``
    # happen inside the agent's own shell. The command agent shell-quotes each
    # ``agent_arg`` individually before joining (see ``quote_agent_args``), so a
    # whole compound command wrapped in one set of quotes would collapse into a
    # single (non-existent) command word. Passing ``bash``, ``-c`` and the
    # script as three separate args keeps the script intact: the outer e2e shell
    # strips the single quotes, and the command agent re-quotes the script as one
    # argument to ``bash -c``. ``$MNGR_TEST_VAR`` is then expanded by that bash.
    expect(
        e2e.run(
            f"mngr create my-task --env MNGR_TEST_VAR={env_value} --type command --no-ensure-clean"
            " -- bash -c 'echo MNGR_TEST_VAR=$MNGR_TEST_VAR && sleep 100116'",
            comment="you can set environment variables for the agent",
        )
    ).to_succeed()

    # Verify the env var was persisted into the agent's on-disk env file.
    # This is a deterministic check (independent of tmux pane timing) that
    # --env actually recorded the variable in the agent's environment.
    env_file_result = e2e.run(
        "cat $MNGR_HOST_DIR/agents/*/env",
        comment="Verify --env recorded the variable in the agent environment",
    )
    expect(env_file_result).to_succeed()
    expect(env_file_result.stdout).to_contain(f"MNGR_TEST_VAR={env_value}")

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


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_pass_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # it is *strongly encouraged* to either use --env-file or --pass-env, especially for any sensitive environment variables (like API keys) rather than --env, because that way they won't end up in your shell history or in your config files by accident. For example:
    export API_KEY=abc123
    mngr create my-task --pass-env API_KEY
    # that command passes the API_KEY environment variable from your current shell into the agent's environment, without you having to specify the value on the command line.
    """)
    expect(
        e2e.run(
            "API_KEY=abc123 mngr create my-task --pass-env API_KEY --type command --no-ensure-clean -- sleep 100093",
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

    # Verify the forwarded variable actually reaches the running agent's
    # environment (not just the on-disk env file): exec into the agent and
    # print API_KEY. This is the behavior a user ultimately depends on.
    # ``mngr exec`` takes ``[AGENTS...] COMMAND``, so the command must be a
    # single argument; quote it so ``printenv API_KEY`` is not parsed as extra
    # agent names.
    exec_result = e2e.run(
        "mngr exec my-task 'printenv API_KEY'",
        comment="Verify API_KEY is visible inside the running agent",
    )
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("abc123")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_pass_env_unset(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: --pass-env forwards a variable
    # from the current shell, but when that variable is *not* set in the shell
    # it is silently skipped rather than causing an error. The agent is still
    # created; the variable simply does not appear in its environment.
    e2e.write_tutorial_block("""
    # it is *strongly encouraged* to either use --env-file or --pass-env, especially for any sensitive environment variables (like API keys) rather than --env, because that way they won't end up in your shell history or in your config files by accident. For example:
    export API_KEY=abc123
    mngr create my-task --pass-env API_KEY
    # that command passes the API_KEY environment variable from your current shell into the agent's environment, without you having to specify the value on the command line.
    """)
    # Deliberately do NOT set API_KEY in the shell before creating the agent.
    expect(
        e2e.run(
            "mngr create my-task --pass-env API_KEY --type command --no-ensure-clean -- sleep 100098",
            comment="pass-env for a variable that is unset in the shell is skipped, not an error",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent was still created")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # The unset variable must not be forwarded into the agent's env file.
    env_file_result = e2e.run(
        "cat $MNGR_HOST_DIR/agents/*/env",
        comment="Verify the unset API_KEY was not forwarded into agent environment",
    )
    expect(env_file_result).to_succeed()
    expect(env_file_result.stdout).not_to_contain("API_KEY")


@pytest.mark.release
@pytest.mark.timeout(120)
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
        "mngr create my-task --template my_modal_template --type command --no-ensure-clean -- sleep 100094",
        comment="templates are defined in your config",
    )
    # Expect failure because the modal provider is disabled
    expect(result).to_fail()
    # The error should reference the modal provider being unavailable
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)modal|provider")

    # The failed create must not leave a partial agent behind: the disabled
    # provider should abort before anything is registered.
    list_result = e2e.run("mngr list", comment="Verify the failed create left no agent behind")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.release
def test_create_with_plugin_flags(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can enable or disable specific plugins:
    mngr create my-task --plugin my-plugin --disable-plugin other-plugin
    """)
    result = e2e.run(
        "mngr create my-task --plugin my-plugin --disable-plugin other-plugin --type command --no-ensure-clean -- sleep 100095",
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

    # The rejected plugin flag must abort before anything is registered, so the
    # failed create leaves no partial agent behind.
    list_result = e2e.run("mngr list", comment="Verify the failed create left no agent behind")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_real_plugin_flags(e2e: E2eSession) -> None:
    # Happy-path counterpart to test_create_with_plugin_flags: the unhappy-path
    # test above uses non-existent plugin names, so the strict --disable-plugin
    # check fails before --plugin is ever exercised. Here we pass *real*
    # registered plugin names so both flags take effect and the agent is
    # actually created. We disable the external "modal" provider plugin (safe
    # for a local command agent) and enable the always-present "usage" plugin.
    e2e.write_tutorial_block("""
    # you can enable or disable specific plugins:
    mngr create my-task --plugin my-plugin --disable-plugin other-plugin
    """)
    result = e2e.run(
        "mngr create my-task --plugin usage --disable-plugin modal --type command --no-ensure-clean -- sleep 100099",
        comment="you can enable or disable specific plugins",
    )
    expect(result).to_succeed()
    # Real plugin names must not trigger the "not registered" rejection.
    combined = result.stdout + result.stderr
    expect(combined).not_to_match(r"(?i)not registered")

    list_result = e2e.run("mngr list", comment="Verify agent created with real plugin flags")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # Verify the agent is actually running (the flags did not break creation).
    exec_result = e2e.run("mngr exec my-task pwd", comment="Verify the agent is running")
    expect(exec_result).to_succeed()


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_in_place_alias_target(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you should probably use aliases for making little shortcuts for yourself, because many of the commands can get a bit long:
    echo "alias mc='mngr create --transfer=none'" >> ~/.bashrc && source ~/.bashrc
    # or use a more sophisticated tool, like Espanso
    """)
    expect(
        e2e.run(
            "mngr create my-task --transfer=none --type command --no-ensure-clean -- sleep 100096",
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
@pytest.mark.timeout(60)
def test_config_set_headless(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # or you can set that option in your config so that it always applies:
    mngr config set headless true
    """)
    # ``config set`` writes to the project settings.toml (the default scope).
    # The e2e fixture already seeds that file with ``is_allowed_in_pytest = true``
    # so it passes the enforce_pytest_config_opt_in guard, and ``config set``
    # preserves that key when it re-saves the file with the new value.
    result = e2e.run(
        "mngr config set headless true",
        comment="or you can set that option in your config so that it always applies",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Set headless")
    # The default scope is the project config, so the command must report that.
    expect(result.stdout).to_contain("project")

    # Verify the value was persisted via the merged config view (default scope)
    get_result = e2e.run("mngr config get headless", comment="Verify headless config is visible in merged view")
    expect(get_result).to_succeed()
    expect(get_result.stdout).to_contain("true")

    # Observe the concrete on-disk effect (as a human debugging would): the value
    # was actually written into the project-scope settings.toml file, not just
    # surfaced in the merged view.
    file_result = e2e.run(
        "cat .$MNGR_ROOT_NAME/settings.toml",
        comment="Verify config set persisted headless to the project settings.toml on disk",
    )
    expect(file_result).to_succeed()
    expect(file_result.stdout).to_contain("headless = true")


@pytest.mark.release
@pytest.mark.timeout(60)
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
@pytest.mark.timeout(60)
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


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_label(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can add labels to organize your agents and tags for host metadata:
    mngr create my-task --label team=backend --host-label env=staging
    """)
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean --label team=backend --host-label env=staging -- sleep 100097",
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

    # Labels exist to organize/filter agents, so verify they actually drive
    # filtering: the agent shows up when filtering on its label and host label,
    # and is excluded when filtering on a non-matching label value.
    filtered = e2e.run(
        "mngr list --label team=backend --host-label env=staging --format json",
        comment="filter agents by label and host label",
    )
    expect(filtered).to_succeed()
    filtered_names = [a["name"] for a in json.loads(filtered.stdout)["agents"]]
    assert "my-task" in filtered_names

    excluded = e2e.run(
        "mngr list --label team=frontend --format json",
        comment="a non-matching label value excludes the agent",
    )
    expect(excluded).to_succeed()
    excluded_names = [a["name"] for a in json.loads(excluded.stdout)["agents"]]
    assert "my-task" not in excluded_names


@pytest.mark.release
@pytest.mark.timeout(60)
def test_create_with_invalid_label_format(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: labels must be KEY=VALUE, so a
    # value without "=" is rejected and no agent is created.
    e2e.write_tutorial_block("""
    # you can add labels to organize your agents and tags for host metadata:
    mngr create my-task --label team=backend --host-label env=staging
    """)
    result = e2e.run(
        "mngr create my-task --type command --no-ensure-clean --label notvalid -- sleep 100098",
        comment="labels must be in KEY=VALUE format",
    )
    expect(result).to_fail()
    expect(result.stderr + result.stdout).to_contain("KEY=VALUE")

    # The agent must not have been created when label parsing fails.
    list_result = e2e.run("mngr list --format json", comment="Verify no agent was created")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert [a for a in parsed["agents"] if a["name"] == "my-task"] == []

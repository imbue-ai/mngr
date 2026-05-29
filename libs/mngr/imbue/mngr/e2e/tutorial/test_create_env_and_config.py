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
def test_create_with_env(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can set environment variables for the agent:
    mngr create my-task --env DEBUG=true
    # (--env-file loads from a file, --pass-env forwards a variable from your current shell)
    """)
    # Use a unique value so we can verify it appears in the tmux pane
    env_value = uuid.uuid4().hex
    # Pass the compound command as a single argument after ``--`` so that
    # ``&&`` reaches the agent's shell instead of being interpreted by the
    # outer e2e runner shell. The command agent joins agent_args with spaces,
    # so a single quoted arg is preserved verbatim.
    expect(
        e2e.run(
            f"mngr create my-task --env MNGR_TEST_VAR={env_value} --type command --no-ensure-clean"
            " -- 'echo MNGR_TEST_VAR=$MNGR_TEST_VAR && sleep 100116'",
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


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_create_with_env_file(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can set environment variables for the agent:
    mngr create my-task --env DEBUG=true
    # (--env-file loads from a file, --pass-env forwards a variable from your current shell)
    """)
    # Use a unique value so we can verify it appears in the tmux pane
    env_value = uuid.uuid4().hex
    # Write an env file and load it via --env-file. Earlier files are overridden
    # by later ones, but here a single file is enough to exercise the flag.
    expect(
        e2e.run(
            f"echo 'MNGR_TEST_VAR={env_value}' > my-task.env",
            comment="--env-file loads from a file",
        )
    ).to_succeed()
    # Pass the compound command as a single argument after ``--`` so that
    # ``&&`` reaches the agent's shell instead of being interpreted by the
    # outer e2e runner shell.
    expect(
        e2e.run(
            "mngr create my-task --env-file my-task.env --type command --no-ensure-clean"
            " -- 'echo MNGR_TEST_VAR=$MNGR_TEST_VAR && sleep 100117'",
            comment="--env-file loads from a file",
        )
    ).to_succeed()

    # Verify the env var loaded from the file is visible in the agent's tmux pane.
    def _env_var_visible() -> bool:
        capture = e2e.run(
            "tmux capture-pane -t $(tmux list-sessions -F '#{session_name}' | grep my-task) -p",
            comment="Capture tmux pane to verify env var from file",
        )
        return env_value in capture.stdout

    wait_for(_env_var_visible, timeout=10.0, error_message=f"Expected {env_value} in tmux pane")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
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


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_create_with_pass_env_unset(e2e: E2eSession) -> None:
    """Unhappy path: --pass-env for a variable that is not set in the shell.

    resolve_env_vars silently skips a passed variable that is absent from the
    environment, so create should still succeed and the variable must not
    appear in the agent's env file.
    """
    e2e.write_tutorial_block("""
    # it is *strongly encouraged* to either use --env-file or --pass-env, especially for any sensitive environment variables (like API keys) rather than --env, because that way they won't end up in your shell history or in your config files by accident. For example:
    export API_KEY=abc123
    mngr create my-task --pass-env API_KEY
    # that command passes the API_KEY environment variable from your current shell into the agent's environment, without you having to specify the value on the command line.
    """)
    # Use a variable name that is guaranteed not to exist in the shell so we
    # exercise the "forward a variable that isn't set" path.
    unset_var = f"MNGR_UNSET_PASS_ENV_{uuid.uuid4().hex}"
    expect(
        e2e.run(
            f"mngr create my-task --pass-env {unset_var} --type command --no-ensure-clean -- sleep 100098",
            comment="pass an unset variable from the current shell into the agent's environment",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify agent was created despite the unset --pass-env variable")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # The unset variable should be silently omitted from the agent's env file.
    env_file_result = e2e.run(
        "cat $MNGR_HOST_DIR/agents/*/env",
        comment="Verify the unset variable was not forwarded into the agent environment",
    )
    expect(env_file_result).to_succeed()
    expect(env_file_result.stdout).not_to_contain(unset_var)


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
        "mngr create my-task --template my_modal_template --type command --no-ensure-clean -- sleep 100094",
        comment="templates are defined in your config",
    )
    # Expect failure because the modal provider is disabled
    expect(result).to_fail()
    # The error should specifically reference the modal provider backend being unavailable
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)modal")
    expect(combined).to_match(r"(?i)provider|backend|plugin")

    # Verify the concrete effect: the failed create must not leave a dangling
    # agent behind. The unknown-provider error is raised during config
    # resolution, before any agent is persisted, so no agent directory should
    # exist under $MNGR_HOST_DIR/agents. Use a plain shell check rather than a
    # second `mngr` invocation, whose cold start would blow the per-test time
    # budget.
    agents_listing = e2e.run(
        "ls -A $MNGR_HOST_DIR/agents 2>/dev/null || true",
        comment="Verify the failed create did not register an agent",
    )
    expect(agents_listing).to_succeed()
    expect(agents_listing.stdout.strip()).to_be_empty()


@pytest.mark.release
@pytest.mark.timeout(120)
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

    # The create failed during plugin validation, so it must not have left a
    # dangling agent behind.
    list_result = e2e.run("mngr list --format json", comment="Verify the failed create left no dangling agent")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    assert not any(a["name"] == "my-task" for a in agents), f"unexpected agent created: {agents}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_valid_plugin_flags(e2e: E2eSession) -> None:
    # Happy-path counterpart to ``test_create_with_plugin_flags``: the same
    # tutorial block, but with plugin names that are actually registered, so
    # the create succeeds instead of erroring on an unknown plugin.
    e2e.write_tutorial_block("""
    # you can enable or disable specific plugins:
    mngr create my-task --plugin my-plugin --disable-plugin other-plugin
    """)
    # ``claude`` and ``notifications`` are both registered plugins, so enabling
    # one and disabling the other is accepted and the agent is created.
    expect(
        e2e.run(
            "mngr create my-task --plugin claude --disable-plugin notifications"
            " --type command --no-ensure-clean -- sleep 100098",
            comment="you can enable or disable specific plugins",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list --format json", comment="Verify agent created with plugin flags")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    assert any(a["name"] == "my-task" for a in agents), f"my-task not found in {agents}"


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
@pytest.mark.timeout(120)
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

    # Verify the value was persisted by reading the project-scope settings file
    # that ``set`` wrote (default scope for ``set`` is project). We read the file
    # directly rather than via ``mngr config get`` because ``set`` creates a fresh
    # project settings.toml that lacks ``is_allowed_in_pytest = true``; any
    # subsequent mngr invocation builds the merged config, loads that file, and is
    # refused by the pytest config guard. Reading the file confirms the concrete
    # effect of the command.
    config_file_result = e2e.run(
        "cat .$MNGR_ROOT_NAME/settings.toml",
        comment="Verify headless was persisted to the project config file",
    )
    expect(config_file_result).to_succeed()
    expect(config_file_result.stdout).to_contain("headless = true")


@pytest.mark.release
@pytest.mark.timeout(120)
def test_config_set_unknown_key_rejected(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: ``mngr config set`` validates the
    # key against the config schema and refuses to persist an unknown field.
    e2e.write_tutorial_block("""
    # or you can set that option in your config so that it always applies:
    mngr config set headless true
    """)
    result = e2e.run(
        "mngr config set definitely_not_a_real_key true",
        comment="setting an unknown config key should be rejected",
    )
    expect(result).to_fail()
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)unknown configuration field")
    expect(combined).not_to_contain("Traceback")

    # The rejected write must not create the project settings file on disk.
    no_file_result = e2e.run(
        "test ! -f .$MNGR_ROOT_NAME/settings.toml && echo NO_PROJECT_SETTINGS",
        comment="a rejected set must not persist anything to the project config",
    )
    expect(no_file_result).to_succeed()
    expect(no_file_result.stdout).to_contain("NO_PROJECT_SETTINGS")


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
    # config set defaults to project scope, so the value lands in the project
    # settings.toml; the human-readable output also names that scope and path.
    expect(result.stdout).to_contain("project")

    # Verify the value was actually persisted to the project config file on disk.
    # We read the file directly (rather than via another `mngr config get`) because
    # the test environment only opts the local-scope settings.local.toml into the
    # pytest guard (is_allowed_in_pytest); the freshly written project settings.toml
    # has no such flag, so a follow-up mngr invocation would be refused by the guard.
    get_result = e2e.run(
        "cat .$MNGR_ROOT_NAME/settings.toml",
        comment="Verify the default provider config was persisted to the project config file",
    )
    expect(get_result).to_succeed()
    expect(get_result.stdout).to_contain("[commands.create]")
    expect(get_result.stdout).to_match(r'provider\s*=\s*"modal"')


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
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


@pytest.mark.release
def test_create_with_malformed_label(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: labels must be KEY=VALUE.
    e2e.write_tutorial_block("""
    # you can add labels to organize your agents and tags for host metadata:
    mngr create my-task --label team=backend --host-label env=staging
    """)
    # A label without "=" should be rejected with a clear validation error
    # before the agent is created.
    result = e2e.run(
        "mngr create my-task --type command --no-ensure-clean --label team -- sleep 100098",
        comment="a label without KEY=VALUE format should be rejected",
    )
    expect(result).to_fail()
    combined = result.stdout + result.stderr
    expect(combined).to_match(r"(?i)label must be in KEY=VALUE format")
    expect(combined).not_to_contain("Traceback")

    # The failed create should not leave a lingering agent behind.
    list_result = e2e.run("mngr list", comment="Verify no agent was created after the validation error")
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")

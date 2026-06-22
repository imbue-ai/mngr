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
    # Dirty the working tree so the create (with --no-ensure-clean) has an
    # uncommitted file to carry into the agent's worktree via rsync. Without a
    # file to transfer, worktree creation skips rsync entirely and the
    # @pytest.mark.rsync above would be flagged as never invoked.
    e2e.run("touch untracked-file.txt && git add untracked-file.txt", comment="Dirty the working tree")
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

    # Scope the listing to the local provider (where this command agent runs).
    # A bare ``mngr list`` queries every enabled provider and exits non-zero if
    # any is unreachable; the e2e environment may have external providers (e.g.
    # AWS) enabled without credentials, which is an unrelated failure here.
    list_result = e2e.run("mngr list --provider local", comment="Verify agent was created")
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
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
    # Each ``mngr`` invocation pays a sizable interpreter-startup cost (the CLI
    # eagerly imports the full plugin/provider SDK tree), which on a slow
    # (e.g. network-backed) test filesystem can approach the default 30s
    # per-command timeout on its own. Give the ``mngr`` commands generous
    # timeouts so the test exercises the actual behavior rather than racing
    # subprocess startup; this matches the idiom used by the other tutorial e2e
    # tests.
    expect(
        e2e.run(
            "mngr create my-task --pass-env API_KEY --type command --no-ensure-clean -- sleep 100098",
            comment="pass-env for a variable that is unset in the shell is skipped, not an error",
            timeout=120.0,
        )
    ).to_succeed()

    # Scope the listing to the local provider. The agent is a local command
    # agent, and a bare ``mngr list`` runs reconciliation across every enabled
    # provider -- which exits non-zero whenever any remote provider (AWS, Azure,
    # GCP, Docker, ...) is unreachable in the test environment, even though the
    # local agent is listed correctly. ``--provider local`` restricts both the
    # reconciliation and the output to the local provider, which is the only
    # provider relevant to this agent.
    list_result = e2e.run("mngr list --provider local", comment="Verify agent was still created", timeout=120.0)
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # The unset variable must not be forwarded into the agent's env file.
    env_file_result = e2e.run(
        "cat $MNGR_HOST_DIR/agents/*/env",
        comment="Verify the unset API_KEY was not forwarded into agent environment",
    )
    expect(env_file_result).to_succeed()
    expect(env_file_result.stdout).not_to_contain("API_KEY")

    # Verify the variable is genuinely absent from the *running* agent's
    # environment (not just the on-disk env file): exec into the agent and dump
    # its environment. This is the mirror image of the happy-path counterpart
    # (test_create_with_pass_env), which asserts the forwarded value IS present
    # inside the running agent.
    exec_result = e2e.run(
        "mngr exec my-task printenv",
        comment="Verify API_KEY is absent from the running agent's environment",
        timeout=120.0,
    )
    expect(exec_result).to_succeed()
    # Match the exact variable name (the part before "=" on each line) rather
    # than a substring: inherited variables whose names merely *contain*
    # "API_KEY" (e.g. ANTHROPIC_API_KEY, which the test runner exports) must not
    # trigger a false positive.
    agent_env_var_names = {line.split("=", 1)[0] for line in exec_result.stdout.splitlines() if "=" in line}
    assert "API_KEY" not in agent_env_var_names, (
        f"API_KEY should not be present in the agent environment, but found it among: {sorted(agent_env_var_names)}"
    )


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
    # provider should abort before anything is registered. Scope the listing to
    # the local provider (where a command agent would land) so the check does
    # not query unreachable cloud providers (e.g. aws without credentials),
    # which would make `mngr list` exit non-zero for reasons unrelated to this
    # assertion.
    list_result = e2e.run(
        "mngr list --provider local", comment="Verify the failed create left no agent behind"
    )
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


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

    # The rejected plugin flag must abort before anything is registered, so the
    # failed create leaves no partial agent behind. A ``--type command`` agent
    # would be registered on the default ``local`` provider, so scope discovery
    # to ``--provider local``: this verifies the intent precisely without
    # coupling the assertion to whether unrelated cloud providers (aws, gcp,
    # ...) happen to be enabled-but-unconfigured in the test environment (an
    # enumerate-all ``mngr list`` aborts loudly on any unreachable provider).
    list_result = e2e.run(
        "mngr list --provider local", comment="Verify the failed create left no agent behind"
    )
    expect(list_result).to_succeed()
    expect(list_result.stdout).not_to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_real_plugin_flags(e2e: E2eSession) -> None:
    # NOTE: no @pytest.mark.rsync -- this creates a *local* command agent, whose
    # worktree is populated by ``git worktree`` alone. rsync is only invoked for
    # remote provider build contexts (docker/modal), so the rsync resource guard
    # would flag the mark as superfluous (never invoked) on a passing run.
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

    # The agent is a local command agent, so scope the verification to the local
    # provider. A plain ``mngr list`` fans out to every enabled provider backend;
    # in this dev monorepo all provider plugins are installed, so discovery would
    # also query credential-requiring cloud backends (e.g. AWS) and any remote
    # backend whose daemon is not running (e.g. Docker), making ``mngr list``
    # abort non-zero for reasons unrelated to the plugin flags under test.
    # ``--provider local`` restricts discovery to the provider that actually
    # hosts this agent, which is exactly what we want to verify here.
    list_result = e2e.run("mngr list --provider local", comment="Verify agent created with real plugin flags")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_contain("my-task")

    # Verify the agent is actually running (the flags did not break creation).
    # Pin the address to the local provider (NAME@HOST.PROVIDER) so agent
    # resolution does not fan out to unreachable remote providers either.
    exec_result = e2e.run("mngr exec my-task@localhost.local pwd", comment="Verify the agent is running")
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

    # Scope discovery to the local provider so the assertion does not depend on
    # (or contact) remote cloud providers. The agent runs in-place on the local
    # provider, and `mngr list` exits non-zero when any enabled-but-unreachable
    # provider (e.g. AWS without credentials in CI) cannot be queried.
    list_result = e2e.run("mngr list --provider local", comment="Verify agent created with --transfer=none")
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
    # The e2e fixture deliberately does NOT seed the project settings.toml (see
    # the note in e2e/conftest.py): this exercises the genuine first-use
    # behavior where ``config set`` creates a fresh project config file. Because
    # that fresh file does not carry ``is_allowed_in_pytest = true``, any
    # follow-up ``mngr`` command would be rejected by the enforce_pytest_config
    # opt-in guard, so we verify persistence by reading the file directly with
    # ``cat`` rather than a follow-up ``mngr config get``.
    result = e2e.run(
        "mngr config set headless true",
        comment="or you can set that option in your config so that it always applies",
    )
    expect(result).to_succeed()
    expect(result.stdout).to_contain("Set headless")
    # The default scope is the project config, so the command must report that.
    expect(result.stdout).to_contain("project")

    # Observe the concrete on-disk effect (as a human debugging would): the value
    # was actually written into the project-scope settings.toml file.
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
    # Scope the listing to the local provider so it never queries unconfigured
    # cloud providers (e.g. AWS without credentials), which would surface a
    # ProviderErrorInfo and make `mngr list` exit non-zero. The env var being
    # accepted is what we are demonstrating here, not provider discovery.
    result = e2e.run(
        "MNGR_HEADLESS=true mngr list --provider local",
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
def test_env_var_mngr_headless_explicit_false(e2e: E2eSession) -> None:
    # Edge case for the same tutorial block: MNGR_HEADLESS is parsed as a
    # boolean (1/true/yes are truthy, everything else falsey), so the variable's
    # *value* is honored -- it is not merely a presence flag. Setting it to
    # "false" must resolve headless to false, guarding against a regression where
    # the env var is treated as "set means enabled".
    e2e.write_tutorial_block("""
    # or you can set it as an environment variable:
    export MNGR_HEADLESS=true
    """)
    get_result = e2e.run(
        "MNGR_HEADLESS=false mngr config get headless",
        comment="MNGR_HEADLESS=false resolves headless to false (the value is parsed, not just presence-checked)",
    )
    expect(get_result).to_succeed()
    expect(get_result.stdout).to_contain("false")


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
    # ``config set`` defaults to the project scope, so it reports writing there.
    expect(result.stdout).to_contain("project")

    # Observe the concrete on-disk effect (as a human debugging would): the value
    # was written into the project-scope settings.toml as the nested
    # ``[commands.create]`` ``provider`` key. We read it back with ``cat`` rather
    # than a follow-up ``mngr config get``: ``config set`` creates a fresh
    # project settings.toml that does not carry ``is_allowed_in_pytest = true``,
    # so any ``mngr`` command that loaded it would trip the pytest opt-in guard
    # (this is purely a test-environment artifact; real users have no such guard).
    file_result = e2e.run(
        "cat .$MNGR_ROOT_NAME/settings.toml",
        comment="Verify the default provider config was persisted to the project settings.toml on disk",
    )
    expect(file_result).to_succeed()
    expect(file_result.stdout).to_contain("[commands.create]")
    expect(file_result.stdout).to_contain('provider = "modal"')


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

    # Scope discovery to the local provider: this is a local command agent
    # (provider=local), and a plain ``mngr list`` would also probe external
    # cloud backends (AWS, Modal, etc.) which raise ``ProviderUnavailableError``
    # when their credentials are not configured, making ``mngr list`` exit
    # non-zero. ``--provider local`` matches the sibling e2e tests and keeps the
    # listing deterministic regardless of ambient cloud credentials.
    list_result = e2e.run("mngr list --provider local --format json", comment="Verify labels appear in JSON output")
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
        "mngr list --provider local --label team=backend --host-label env=staging --format json",
        comment="filter agents by label and host label",
    )
    expect(filtered).to_succeed()
    filtered_names = [a["name"] for a in json.loads(filtered.stdout)["agents"]]
    assert "my-task" in filtered_names

    excluded = e2e.run(
        "mngr list --provider local --label team=frontend --format json",
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

    # The agent must not have been created when label parsing fails. The agent
    # would be a local command agent, so scope discovery to the local provider
    # (--provider local restricts which providers are queried, unlike the
    # --local result filter which still fans out to remote providers). This keeps
    # the check deterministic and avoids aborting on unrelated remote-provider
    # discovery failures (e.g. AWS credentials not configured on this host).
    list_result = e2e.run("mngr list --provider local --format json", comment="Verify no agent was created")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    assert [a for a in parsed["agents"] if a["name"] == "my-task"] == []

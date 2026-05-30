"""Tests for basic agent creation from the BASIC CREATION tutorial section."""

import json
import os

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_default(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # running mngr create is strictly better than running claude!
    # (if you use the alias `mngr c`, it's no more letters to type :-D)
    # running this command launches your default agent immediately *in a new worktree*
    mngr create
    # the defaults are the following: agent=your configured default (stored under `[commands.create] type`
    # in user settings; `scripts/install.sh` interactively prompts you to pick one as part of
    # `mngr extras -i`, and you can re-run `mngr extras config` later to pick or change it),
    # provider=local, project=current dir
    """)
    result = e2e.run(
        "mngr create my-task --type command --no-ensure-clean -- sleep 100070",
        comment="running mngr create is strictly better than running claude!",
    )
    expect(result).to_succeed()

    list_result = e2e.run(
        "mngr list --format json",
        comment="the defaults are the following: agent=your configured default, provider=local, project=current dir",
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    agent = matching[0]
    # Default creation should use a worktree (not in-place)
    assert "worktrees" in agent["work_dir"], f"Expected worktree-based work_dir, got: {agent['work_dir']}"


@pytest.mark.release
@pytest.mark.tmux
def test_create_in_place(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # if you want the default behavior of claude (starting in-place), you can specify that:
    mngr create --transfer=none
    # mngr defaults to creating a new worktree for each agent because the whole point of mngr is to let you run multiple agents in parallel.
    # without creating a new worktree for each, they will make conflicting changes with one another.
    """)
    result = e2e.run(
        "mngr create my-task --transfer=none --type command --no-ensure-clean -- sleep 100071",
        comment="if you want the default behavior of claude (starting in-place), you can specify that",
    )
    expect(result).to_succeed()

    # Verify the agent's work_dir is the session cwd (not a generated worktree)
    pwd_result = e2e.run("pwd", comment="Get the session cwd for comparison")
    expect(pwd_result).to_succeed()
    session_cwd = pwd_result.stdout.strip()

    list_result = e2e.run(
        "mngr list --format json",
        comment="Verify agent runs in-place, not in a worktree",
    )
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    agent_work_dir = matching[0]["work_dir"]
    # With --transfer=none, the work directory should be exactly the session cwd,
    # not a generated worktree path.
    assert os.path.realpath(agent_work_dir) == os.path.realpath(session_cwd), (
        f"Expected in-place work_dir to match session cwd.\n  work_dir: {agent_work_dir}\n  session cwd: {session_cwd}"
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_short_forms(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can name the agent type explicitly as a positional argument, or use the short form for the
    # command itself (`mngr c` is an alias for `mngr create`). For example, when claude is your default
    # agent type, `mngr c my-task` is equivalent to `mngr create my-task claude`:
    mngr create my-task claude
    mngr c my-task
    """)
    # Test "mngr create <name>" form. --type command -- sleep <N> stands in
    # for the real claude agent so the test doesn't need claude installed.
    result_full = e2e.run(
        "mngr create my-task --type command --no-ensure-clean -- sleep 100072",
        comment="you can name the agent type explicitly as a positional argument, or use the short form",
    )
    expect(result_full).to_succeed()

    # Test "mngr c <name>" short form (needs a different name since my-task already exists)
    # Pinned sleep value distinct from the one above so leaked processes trace back to this call.
    result_short = e2e.run(
        "mngr c my-other-task --type command --no-ensure-clean -- sleep 100117",
        comment="`mngr c my-task` is equivalent to `mngr create my-task claude` when claude is the default",
    )
    expect(result_short).to_succeed()

    # Verify both agents were created and are running
    list_result = e2e.run("mngr list --format json", comment="Verify both agents are running")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents_by_name = {a["name"]: a for a in parsed["agents"]}
    assert "my-task" in agents_by_name, f"my-task not found in agents: {list(agents_by_name)}"
    assert "my-other-task" in agents_by_name, f"my-other-task not found in agents: {list(agents_by_name)}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_codex_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also specify a different agent (ex: codex)
    mngr create my-task codex
    """)
    # Configure the codex agent type to use 'sleep 99999' since codex is not installed
    expect(
        e2e.run(
            "mngr config set agent_types.codex.command 'sleep 99999'",
            comment="Configure codex command for test environment",
        )
    ).to_succeed()

    result = e2e.run(
        "mngr create my-task codex --no-ensure-clean",
        comment="you can also specify a different agent (ex: codex)",
    )
    expect(result).to_succeed()

    list_result = e2e.run("mngr list --format json", comment="Verify codex agent is created")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    assert matching[0]["type"] == "codex"
    assert matching[0]["state"] in ("RUNNING", "WAITING")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_create_with_agent_args(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can specify the arguments to the *agent* (ie, send args to the agent rather than mngr)
    # by using `--` to separate the agent arguments from the mngr arguments:
    mngr create my-task -- --model opus
    # that command passes the "--model opus" flag to your default agent (e.g. claude, when claude
    # is configured as the default)
    """)
    # `--` is consumed by _CreateCommand.parse_args the first time it appears,
    # so everything after it becomes agent_args and is joined with spaces into
    # the stored command. The test asserts on the stored command string only,
    # so whether the spawned `sleep` process actually stays alive is irrelevant
    # (GNU sleep would reject `--model` as an unknown option and exit). We put
    # the pinned sleep value first so it shows up at the start of `ps` output
    # for leak traceability.
    result = e2e.run(
        "mngr create my-task --type command --no-ensure-clean -- sleep 100073 --model opus",
        comment="you can specify the arguments to the *agent* by using `--` to separate the agent arguments",
    )
    expect(result).to_succeed()

    list_result = e2e.run("mngr list --format json", comment="Verify agent args were passed through")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    assert "sleep 100073" in matching[0]["command"]
    assert "--model opus" in matching[0]["command"]


@pytest.mark.release
def test_create_agent_args_require_separator(e2e: E2eSession) -> None:
    # Unhappy-path counterpart to test_create_with_agent_args for the same
    # tutorial block: the block teaches that `--` is what routes arguments to
    # the agent. Without it, an agent-style flag like `--model` is parsed as an
    # mngr flag, which mngr does not recognize, so create fails at arg-parse
    # time (before any host/provider/tmux work). No agent is created.
    e2e.write_tutorial_block("""
    # you can specify the arguments to the *agent* (ie, send args to the agent rather than mngr)
    # by using `--` to separate the agent arguments from the mngr arguments:
    mngr create my-task -- --model opus
    # that command passes the "--model opus" flag to your default agent (e.g. claude, when claude
    # is configured as the default)
    """)
    # Same flag as the happy path, but WITHOUT the `--` separator: mngr must
    # reject the unknown `--model` flag instead of silently passing it through.
    result = e2e.run(
        "mngr create my-task --model opus",
        comment="without `--`, agent-style flags are parsed as mngr flags and rejected",
    )
    expect(result).to_fail()

    # The failure must be an unrecognized-argument error about --model, not an
    # unrelated crash (e.g. a traceback). argparse-style errors mention the flag.
    assert "--model" in result.stderr, f"Expected an error mentioning --model, got stderr:\n{result.stderr}"

    # No agent should have been created by the rejected command.
    list_result = e2e.run("mngr list --format json", comment="Verify the rejected command created no agent")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 0, f"Expected no agent named 'my-task', got: {matching}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_named_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # when creating agents to accomplish tasks, it's recommended that you give them a name to make it easier to manage them:
    mngr create my-task
    # that command gives the agent a name of "my-task". If you don't specify a name, mngr will generate a random one for you.
    """)
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean -- sleep 100074",
            comment="when creating agents to accomplish tasks, it's recommended that you give them a name",
        )
    ).to_succeed()

    # Verify the agent appears with the exact name we specified
    list_result = e2e.run("mngr list --format json", comment="Verify agent appears with exact name")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly 1 agent named 'my-task', got {len(matching)}"
    assert matching[0]["state"] in ("RUNNING", "WAITING")

    # Verify the agent is actually running by executing a command on its host
    exec_result = e2e.run("mngr exec my-task pwd", comment="Verify agent is actually running")
    expect(exec_result).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_json_output(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can control output format for scripting:
    mngr create my-task --no-connect --format json
    # (--quiet suppresses all output)
    """)
    create_result = e2e.run(
        "mngr create my-task --no-connect --type command --no-ensure-clean --format json -- sleep 100075",
        comment="you can control output format for scripting",
    )
    expect(create_result).to_succeed()

    # The create command with --format json should produce valid JSON with agent_id and host_id
    create_json = json.loads(create_result.stdout)
    assert "agent_id" in create_json
    assert "host_id" in create_json

    list_result = e2e.run("mngr list --format json", comment="Verify agent appears in JSON list")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    assert len(agents) == 1
    assert agents[0]["name"] == "my-task"
    # The agent reported by `mngr list` must be the very one that `mngr create`
    # returned: the ids in the create JSON should match the listed agent/host.
    assert agents[0]["id"] == create_json["agent_id"]
    assert agents[0]["host"]["id"] == create_json["host_id"]


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_quiet_output(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can control output format for scripting:
    mngr create my-task --no-connect --format json
    # (--quiet suppresses all output)
    """)
    # --quiet must suppress *all* console output, including the result line that
    # --format json would otherwise print. The agent should still be created.
    create_result = e2e.run(
        "mngr create my-task --no-connect --type command --no-ensure-clean --quiet -- sleep 100078",
        comment="--quiet suppresses all output",
    )
    expect(create_result).to_succeed()
    assert create_result.stdout.strip() == "", f"Expected no stdout with --quiet, got: {create_result.stdout!r}"

    # Despite the silent output, the agent must actually exist.
    list_result = e2e.run("mngr list --format json", comment="Verify the quiet agent was still created")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly 1 agent named 'my-task', got {len(matching)}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.timeout(120)
def test_create_headless(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # mngr is very much meant to be used for scripting and automation, so nothing requires interactivity.
    # if you want to be sure that interactivity is disabled, you can use the --headless flag:
    mngr create my-task --headless
    """)
    expect(
        e2e.run(
            "mngr create my-task --type command --no-ensure-clean --headless -- sleep 100076",
            comment="if you want to be sure that interactivity is disabled, you can use the --headless flag",
        )
    ).to_succeed()

    list_result = e2e.run("mngr list", comment="Verify headless agent appears in list")
    expect(list_result).to_succeed()
    expect(list_result.stdout).to_match(r"my-task\s+(RUNNING|WAITING)")

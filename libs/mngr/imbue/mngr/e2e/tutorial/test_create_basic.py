"""Tests for basic agent creation from the BASIC CREATION tutorial section."""

import json
import os
import shlex

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# This test runs three sequential mngr operations (create, list, exec), each of
# which performs full provider discovery, so it needs more than the default 10s.
@pytest.mark.timeout(120)
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
    # The agent should be running, not in an error/exited state.
    assert agent["state"] in ("RUNNING", "WAITING"), f"Expected agent to be running, got state: {agent['state']}"

    # Verify the agent is actually running *in* that worktree (not merely that
    # mngr reports a worktree path): exec `pwd` inside the agent and confirm it
    # resolves to the same directory as the reported work_dir. `mngr exec` prints
    # the command's own output first, then a trailing "Command succeeded ..."
    # status line, so the first non-empty line is the pwd we want.
    pwd_result = e2e.run("mngr exec my-task pwd", comment="Verify the agent runs inside its worktree")
    expect(pwd_result).to_succeed()
    exec_output_lines = [line.strip() for line in pwd_result.stdout.splitlines() if line.strip()]
    assert exec_output_lines, f"Expected `mngr exec ... pwd` to print output, got: {pwd_result.stdout!r}"
    agent_cwd = exec_output_lines[0]
    assert os.path.realpath(agent_cwd) == os.path.realpath(agent["work_dir"]), (
        f"Expected agent cwd to match its worktree work_dir.\n"
        f"  exec pwd:  {agent_cwd}\n"
        f"  work_dir:  {agent['work_dir']}"
    )


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_in_place(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can run the agent in-place (directly in your source directory) without any transfer:
        mngr create my-task --transfer=none
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

    # Confirm the running agent is actually in-place by executing pwd on it,
    # rather than trusting the list metadata alone. For an in-place agent the
    # process must be running directly in the source directory.
    exec_result = e2e.run(
        "mngr exec my-task pwd",
        comment="Confirm the agent process actually runs in the source directory",
    )
    expect(exec_result).to_succeed()
    # mngr exec forwards the raw command output first, then appends a HUMAN-format
    # status line ("Command succeeded on agent ..."), so the pwd output is the
    # first line of stdout.
    exec_pwd = exec_result.stdout.splitlines()[0].strip()
    assert os.path.realpath(exec_pwd) == os.path.realpath(session_cwd), (
        f"Expected the in-place agent to run in the session cwd.\n  exec pwd: {exec_pwd}\n  session cwd: {session_cwd}"
    )


# This test runs two `mngr create` commands (most sibling tests run one), so the
# function body exceeds the global 10s pytest-timeout default. Bump it explicitly.
#
# No @pytest.mark.modal here: this test only creates local (`--type command`)
# agents and runs `mngr list`. `mngr list` reaches Modal solely through the
# in-process gRPC SDK inside the spawned `mngr` subprocess, which the resource
# guard cannot track (the SDK monkeypatch lives in the pytest process, and the
# `modal` CLI binary -- the only cross-process-tracked path -- is never invoked
# for local agents). With the mark, the guard's NEVER_INVOKED check fails the
# test; without it there is no tracked Modal usage, so no BLOCKED violation.
@pytest.mark.timeout(120)
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
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
def test_create_codex_agent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can also specify a different agent (ex: codex)
    mngr create my-task codex
    """)
    # codex is a real agent-type plugin (imbue-mngr-codex), not a command-driven
    # stub, so it cannot be faked with a `command` override. This Modal host
    # has no codex binary or auth, so the agent is created *without launching it*
    # (--no-auto-start), auto-approving the workspace-trust prompt (-y). That keeps
    # the tutorial command (`mngr create my-task codex`) honest while verifying the
    # positional `codex` resolves to the codex agent type. The real codex run is
    # covered by the plugin's own release test (libs/mngr_codex/.../test_codex_agent_e2e.py).
    result = e2e.run(
        "mngr create my-task codex -y --no-auto-start --no-ensure-clean",
        comment="you can also specify a different agent (ex: codex)",
    )
    expect(result).to_succeed()

    list_result = e2e.run("mngr list --format json", comment="Verify codex agent is created")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents = parsed["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    # The positional `codex` must resolve to the codex agent type (not silently
    # fall back to a default), confirming the type is registered and creatable.
    assert matching[0]["type"] == "codex", f"expected codex type, got: {matching[0]}"


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
def test_create_agent_args_require_dash_separator(e2e: E2eSession) -> None:
    """Unhappy path for the same tutorial block: without the `--` separator, an
    agent-targeted flag like `--model opus` is parsed as an (unknown) mngr
    option and rejected, rather than being forwarded to the agent. This is the
    failure mode that motivates the `--` separator the tutorial teaches."""
    e2e.write_tutorial_block("""
    # you can specify the arguments to the *agent* (ie, send args to the agent rather than mngr)
    # by using `--` to separate the agent arguments from the mngr arguments:
    mngr create my-task -- --model opus
    # that command passes the "--model opus" flag to your default agent (e.g. claude, when claude
    # is configured as the default)
    """)
    # Omitting the `--` separator: mngr's own parser sees `--model`, which is not
    # a recognized create option, so the command must fail before any agent is
    # created.
    result = e2e.run(
        "mngr create my-task --type command --no-ensure-clean --model opus",
        comment="without `--`, an agent flag like --model is rejected as an unknown mngr option",
    )
    expect(result).to_fail()
    combined_output = result.stdout + result.stderr
    assert "--model" in combined_output, f"Expected the error to mention the rejected flag, got:\n{combined_output}"

    # The failed create must not have left an agent behind.
    list_result = e2e.run("mngr list --format json", comment="Verify the rejected create produced no agent")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert matching == [], f"Expected no 'my-task' agent after the rejected create, got: {matching}"


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
    work_dir = matching[0]["work_dir"]

    # Verify the agent is actually running by executing a command on its host,
    # and that it is rooted in its own dedicated worktree (the unique
    # "my-task-<hash>" directory) rather than merely showing up in `mngr list`.
    exec_result = e2e.run("mngr exec my-task pwd", comment="Verify agent is actually running in its worktree")
    expect(exec_result).to_succeed()
    assert os.path.basename(work_dir) in exec_result.stdout, (
        f"Expected `pwd` output to reference the agent worktree {work_dir!r}, got: {exec_result.stdout!r}"
    )


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


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_quiet_output(e2e: E2eSession) -> None:
    # Shares the BASIC CREATION output-format tutorial block, but exercises the
    # "--quiet suppresses all output" line that test_create_with_json_output
    # only documents without verifying.
    e2e.write_tutorial_block("""
    # you can control output format for scripting:
    mngr create my-task --no-connect --format json
    # (--quiet suppresses all output)
    """)
    create_result = e2e.run(
        "mngr create my-task --quiet --no-connect --type command --no-ensure-clean -- sleep 100078",
        comment="--quiet suppresses all output",
    )
    expect(create_result).to_succeed()

    # The whole point of --quiet for scripting: nothing on stdout to parse.
    # _output_result() returns early and the console log level is NONE, so
    # neither the result line nor the status lines are emitted.
    assert create_result.stdout.strip() == "", f"Expected no stdout under --quiet, got: {create_result.stdout!r}"

    # The agent must still have been created despite the silenced output.
    list_result = e2e.run("mngr list --format json", comment="Verify the agent was created despite --quiet")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly 1 agent named 'my-task', got {len(matching)}"
    assert matching[0]["state"] in ("RUNNING", "WAITING")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.timeout(60)
def test_create_copy(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can create a full copy (an independent git mirror) instead of a worktree:
        mngr create my-task --transfer=git-mirror
        # (a plain rsync copy is used by default if you're not in a git repo)
    """)
    expect(
        e2e.run(
            "mngr create my-task --transfer=git-mirror --type command --no-ensure-clean --no-connect -- sleep 100900",
            comment="you can create a full copy (an independent git mirror) instead of a worktree",
        )
    ).to_succeed()

    # Verify the agent was created and resolve its work_dir.
    list_result = e2e.run("mngr list --format json", comment="Verify the agent appears in the list")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {matching}"
    work_dir = matching[0]["work_dir"]

    # A git-mirror copy is an independent repository: its work_dir holds a real
    # `.git` *directory*. A worktree, by contrast, would have a `.git` *file*
    # (a gitlink pointing back at the source repo). Asserting on this confirms a
    # copy was made rather than a worktree.
    git_kind_result = e2e.run(
        "mngr exec my-task 'test -d .git && echo DIRECTORY || echo FILE'",
        comment="A git-mirror copy has its own independent .git directory, not a worktree gitlink",
    )
    expect(git_kind_result).to_succeed()
    expect(git_kind_result.stdout).to_contain("DIRECTORY")

    # The copy lives in its own directory, separate from the source repo.
    pwd_result = e2e.run("pwd", comment="Get the source directory for comparison")
    expect(pwd_result).to_succeed()
    assert os.path.realpath(work_dir) != os.path.realpath(pwd_result.stdout.strip()), (
        f"Expected the copy's work_dir to differ from the source dir, but both were {work_dir}"
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_clone(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can create a full git "clone" instead of a worktree or copy: this transfers the repo via git, giving the agent its own independent copy with a separate working directory and git history (this is also the default when the source and target are on different hosts):
        mngr create my-task --transfer=git-mirror
    """)
    pwd_result = e2e.run("pwd", comment="Get the source dir for comparison")
    expect(pwd_result).to_succeed()
    source_dir = pwd_result.stdout.strip()

    expect(
        e2e.run(
            "mngr create my-task --transfer=git-mirror --type command --no-ensure-clean --no-connect -- sleep 100901",
            comment="you can create a full git clone instead of a worktree or copy",
        )
    ).to_succeed()

    # Verify the agent was created in its own separate working directory.
    list_result = e2e.run("mngr list --format json", comment="Verify the clone agent was created")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly 1 agent named 'my-task', got {len(matching)}"
    work_dir = matching[0]["work_dir"]
    assert os.path.realpath(work_dir) != os.path.realpath(source_dir), (
        f"Clone should use a separate working directory, but work_dir matched the source.\n  work_dir: {work_dir}"
    )

    # A git-mirror clone is a full, independent repo: its .git is a real
    # directory. A worktree, by contrast, has a .git *file* pointing back at the
    # source repo. Checking for a .git directory confirms this is a clone, not a
    # worktree. The local agent's work_dir is on the local filesystem.
    git_dir_check = e2e.run(
        f"test -d {shlex.quote(work_dir)}/.git",
        comment="Verify the clone has its own .git directory (not a worktree's .git file)",
    )
    expect(git_dir_check).to_succeed()


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(120)
def test_create_with_snapshot_fictional(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can use an existing snapshot instead of building a new host from scratch:
        mngr create my-task --provider modal --snapshot snap-123abc
    """)
    # The fictional snapshot id won't exist in any modal environment, so mngr
    # reaches the Modal provider (initializing the environment and attempting to
    # load the snapshot image) and then fails. We verify it exits with an error
    # rather than crashing. `--type command -- sleep ...` stands in for the real
    # default agent so the test doesn't depend on a configured default; the agent
    # never actually launches because host creation fails on the bad snapshot
    # first.
    result = e2e.run(
        "mngr create my-task --provider modal --snapshot snap-123abc --type command --no-connect --no-ensure-clean -- sleep 100078",
        comment="use an existing snapshot instead of building a new host",
    )
    assert result.exit_code != 0
    # The command must get far enough to actually hand the snapshot to Modal and
    # be rejected for it -- i.e. the failure references the bad snapshot id, not a
    # generic earlier error (missing agent type, provider not configured, etc.).
    # And it must fail gracefully: a clean single-line mngr error, never a raw
    # Python traceback. Surface the combined output so any regression is easy to
    # diagnose.
    combined_output = result.stdout + result.stderr
    assert "snap-123abc" in combined_output, (
        f"Expected the error to reference the invalid snapshot id, got:\n{combined_output}"
    )
    assert "Traceback (most recent call last)" not in combined_output, (
        f"Expected a clean error, but got an unhandled traceback:\n{combined_output}"
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
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

    # Appearing in `mngr list` only proves the record exists; confirm the
    # headless agent is actually running by exec-ing a command on its host.
    exec_result = e2e.run("mngr exec my-task pwd", comment="Verify headless agent is actually running")
    expect(exec_result).to_succeed()

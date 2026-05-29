"""Tests for basic agent creation from the BASIC CREATION tutorial section."""

import json
import os

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
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
    # The tutorial documents provider=local as the default, so the agent's host
    # should be the local provider rather than a remote one.
    assert agent["host"]["provider_name"] == "local", (
        f"Expected local provider by default, got: {agent['host']['provider_name']}"
    )

    # Verify the agent is actually running *inside* its worktree (not merely
    # listed with that work_dir) by asking it for its working directory.
    # `mngr exec` appends a status line (e.g. "Command succeeded on agent ...")
    # to stdout, so match the worktree against any output line rather than the
    # whole buffer.
    pwd_result = e2e.run("mngr exec my-task pwd", comment="verify the agent runs inside its worktree")
    expect(pwd_result).to_succeed()
    work_dir_real = os.path.realpath(agent["work_dir"])
    pwd_lines = [line.strip() for line in pwd_result.stdout.splitlines() if line.strip()]
    assert any(os.path.realpath(line) == work_dir_real for line in pwd_lines), (
        f"Agent should run inside its worktree.\n  pwd output lines: {pwd_lines}\n  work_dir: {agent['work_dir']}"
    )


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
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
    # With --transfer=none there is no per-agent branch (the agent operates on
    # the source repo's current checkout directly).
    assert matching[0]["initial_branch"] is None, (
        f"Expected no per-agent branch for in-place creation, got: {matching[0]['initial_branch']}"
    )

    # Observe the actual runtime behavior, not just the list metadata: `mngr exec`
    # runs in the agent's work_dir by default, so running `pwd` on the agent must
    # report the same in-place source directory. This confirms the agent process
    # actually operates in-place rather than in a generated worktree. The command
    # output is the pwd line; mngr appends a trailing "Command succeeded" status
    # line, so we look for the source directory among the emitted lines.
    exec_pwd_result = e2e.run("mngr exec my-task pwd", comment="Verify the agent actually runs in the source directory")
    expect(exec_pwd_result).to_succeed()
    exec_pwd_paths = {os.path.realpath(line.strip()) for line in exec_pwd_result.stdout.splitlines() if line.strip()}
    assert os.path.realpath(session_cwd) in exec_pwd_paths, (
        f"Expected agent runtime cwd to match session cwd.\n"
        f"  exec output: {exec_pwd_result.stdout.strip()!r}\n  session cwd: {session_cwd}"
    )


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

    # Verify both agents were created and are running. The point of this tutorial
    # block is that `mngr c <name>` is equivalent to `mngr create <name>`, so the
    # short form should produce an agent indistinguishable from the full form:
    # same running state and the same default worktree-based isolation.
    list_result = e2e.run("mngr list --format json", comment="Verify both agents are running")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    agents_by_name = {a["name"]: a for a in parsed["agents"]}
    assert "my-task" in agents_by_name, f"my-task not found in agents: {list(agents_by_name)}"
    assert "my-other-task" in agents_by_name, f"my-other-task not found in agents: {list(agents_by_name)}"
    for name in ("my-task", "my-other-task"):
        agent = agents_by_name[name]
        # Both forms should leave a live agent (the `sleep` stand-in keeps running).
        assert agent["state"] in ("RUNNING", "WAITING"), f"{name} unexpectedly in state {agent['state']}"
        # Neither command opted out of the default worktree, so both must run in one.
        assert "worktrees" in agent["work_dir"], f"Expected worktree-based work_dir for {name}, got: {agent['work_dir']}"

    # Confirm the short-form agent is genuinely reachable, not just registered.
    exec_result = e2e.run("mngr exec my-other-task 'echo alive'", comment="Verify the short-form agent is running")
    expect(exec_result).to_succeed()
    expect(exec_result.stdout).to_contain("alive")


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# This test runs three full mngr invocations (config set, a connect-enabled create,
# and a cross-provider list), which does not fit in the default 10s function timeout.
@pytest.mark.timeout(120)
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
    agent = matching[0]
    assert agent["type"] == "codex"
    assert agent["state"] in ("RUNNING", "WAITING")
    # The codex agent type resolved to the command we configured above, confirming
    # the positional `codex` selected the codex agent type (not the default).
    assert agent["command"] == "sleep 99999", f"Expected configured codex command, got: {agent['command']}"
    # Like the default `mngr create`, a codex agent gets its own worktree.
    assert "worktrees" in agent["work_dir"], f"Expected worktree-based work_dir, got: {agent['work_dir']}"


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
    # Everything after `--` becomes the agent args, joined with spaces into the
    # stored command verbatim and in order. Asserting on the exact string (rather
    # than just substring membership) confirms the `--model opus` flag was passed
    # through after the base command with nothing injected or reordered.
    assert matching[0]["command"] == "sleep 100073 --model opus"


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
    # and that it runs in its own worktree (not the session cwd). The pwd output
    # should be the work_dir reported by `mngr list`.
    exec_result = e2e.run("mngr exec my-task pwd", comment="Verify agent is actually running")
    expect(exec_result).to_succeed()
    pwd_lines = [line.strip() for line in exec_result.stdout.splitlines() if line.strip().startswith("/")]
    assert pwd_lines, f"Expected an absolute path in exec output, got: {exec_result.stdout!r}"
    assert os.path.realpath(pwd_lines[0]) == os.path.realpath(work_dir), (
        f"Expected agent to run in its worktree.\n  pwd: {pwd_lines[0]}\n  work_dir: {work_dir}"
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_generates_random_name_when_unnamed(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # when creating agents to accomplish tasks, it's recommended that you give them a name to make it easier to manage them:
    mngr create my-task
    # that command gives the agent a name of "my-task". If you don't specify a name, mngr will generate a random one for you.
    """)
    # The tutorial block notes that omitting the name makes mngr generate a
    # random one. Create two unnamed agents and verify each gets a distinct,
    # non-empty auto-generated name (the default style is a multi-word
    # "coolname" slug).
    first = e2e.run(
        "mngr create --type command --no-ensure-clean -- sleep 100078",
        comment="if you don't specify a name, mngr will generate a random one for you",
    )
    expect(first).to_succeed()
    second = e2e.run(
        "mngr create --type command --no-ensure-clean -- sleep 100079",
        comment="creating another unnamed agent yields a different generated name",
    )
    expect(second).to_succeed()

    list_result = e2e.run("mngr list --format json", comment="Verify both agents have distinct generated names")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    names = [a["name"] for a in parsed["agents"]]
    assert len(names) == 2, f"Expected exactly 2 agents, got: {names}"
    # Generated names are non-empty multi-word slugs, distinct from each other
    # and from the hardcoded "my-task" used elsewhere in this section.
    for name in names:
        assert name, "Generated agent name should be non-empty"
        assert name != "my-task", f"Unnamed create should not produce 'my-task': {name}"
        assert "-" in name, f"Expected a multi-word generated slug, got: {name!r}"
    assert names[0] != names[1], f"Expected distinct generated names, got: {names}"


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
    e2e.write_tutorial_block("""
    # you can control output format for scripting:
    mngr create my-task --no-connect --format json
    # (--quiet suppresses all output)
    """)
    # Covers the second annotation of the same tutorial block: --quiet suppresses
    # all output. As elsewhere, a command-type sleep agent stands in for the real
    # default agent so the test doesn't need claude installed.
    quiet_result = e2e.run(
        "mngr create my-task --no-connect --quiet --type command --no-ensure-clean -- sleep 100078",
        comment="--quiet suppresses all output",
    )
    expect(quiet_result).to_succeed()
    # The defining behavior of --quiet: no output on either stream, so it is safe
    # to use in scripts that only care about the exit code.
    assert quiet_result.stdout.strip() == "", f"Expected no stdout with --quiet, got: {quiet_result.stdout!r}"
    assert quiet_result.stderr.strip() == "", f"Expected no stderr with --quiet, got: {quiet_result.stderr!r}"

    # Suppressed output must not mean a no-op: the agent must still have been created.
    list_result = e2e.run("mngr list --format json", comment="Verify the agent was created despite suppressed output")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly 1 agent named 'my-task', got {len(matching)}"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.timeout(60)
def test_create_copy(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can create an independent copy (its own clone of the repo) instead of a shared worktree:
        mngr create my-task --transfer=git-mirror
        # (for non-git projects, mngr makes an rsync copy by default instead)
    """)
    # The tutorial's old --copy/--clone flags were folded into --transfer; git-mirror
    # is the git-repo way to get an independent copy (its own .git) rather than a
    # worktree that shares the source's .git. rsync is rejected for git repos and is
    # only the default for non-git projects, so it cannot be exercised in this git
    # fixture. --type command -- sleep <N> stands in for the real agent so the test
    # doesn't need claude installed.
    expect(
        e2e.run(
            "mngr create my-task --transfer=git-mirror --type command --no-ensure-clean --no-connect -- sleep 100900",
            comment="create an independent copy of the repo instead of a worktree",
        )
    ).to_succeed()

    # The source repo is the session cwd; the copy must live somewhere else.
    pwd_result = e2e.run("pwd", comment="Get the source repo path (session cwd) for comparison")
    expect(pwd_result).to_succeed()
    source_cwd = pwd_result.stdout.strip()

    list_result = e2e.run("mngr list --format json", comment="Verify the agent was created")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly 1 agent named 'my-task', got {len(matching)}"
    work_dir = matching[0]["work_dir"]
    # An independent copy must not run in the source directory itself.
    assert os.path.realpath(work_dir) != os.path.realpath(source_cwd), (
        f"Expected an independent copy, but work_dir matches the source repo: {work_dir}"
    )

    # The copy must be a standalone git repo (its own .git directory), unlike a
    # worktree whose .git is a file pointing back at the source repo.
    git_dir_check = e2e.run(
        "mngr exec my-task 'test -d .git && echo standalone || echo worktree'",
        comment="Verify the copy is a standalone repo, not a shared worktree",
    )
    expect(git_dir_check).to_succeed()
    assert "standalone" in git_dir_check.stdout and "worktree" not in git_dir_check.stdout, (
        f"Expected a standalone .git directory in the copy, got: {git_dir_check.stdout!r}"
    )

    # The committed source content must have been mirrored into the copy.
    readme_check = e2e.run("mngr exec my-task 'cat README.md'", comment="Verify the repo content was copied")
    expect(readme_check).to_succeed()
    assert "Initial content" in readme_check.stdout, (
        f"Expected the source README.md content in the copy, got: {readme_check.stdout!r}"
    )


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_create_clone(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can transfer the project as a standalone git clone instead of a worktree or copy, which pushes all local branches and tags into a fresh repository with its own separate working directory:
        mngr create my-task --transfer=git-mirror
    """)
    # The tutorial uses the conceptual `--clone`; the supported flag is
    # `--transfer=git-mirror`, which produces a standalone clone (a fresh repo
    # with its own object store and working directory, not a linked worktree).
    expect(
        e2e.run(
            "mngr create my-task --transfer=git-mirror --type command --no-ensure-clean --no-connect -- sleep 100901",
            comment="you can transfer the project as a standalone git clone instead of a worktree or copy",
        )
    ).to_succeed()

    # The clone must live in its own working directory, separate from the source
    # and distinct from a worktree (worktrees go under <host_dir>/worktrees).
    pwd_result = e2e.run("pwd", comment="Get the session cwd (the source repo) for comparison")
    expect(pwd_result).to_succeed()
    session_cwd = pwd_result.stdout.strip()

    list_result = e2e.run("mngr list --format json", comment="Verify the clone agent and its work_dir")
    expect(list_result).to_succeed()
    parsed = json.loads(list_result.stdout)
    matching = [a for a in parsed["agents"] if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly 1 agent named 'my-task', got {len(matching)}"
    agent = matching[0]
    assert agent["state"] in ("RUNNING", "WAITING")
    work_dir = agent["work_dir"]
    assert os.path.realpath(work_dir) != os.path.realpath(session_cwd), (
        f"Clone work_dir should differ from the source dir, got: {work_dir}"
    )
    assert "worktrees" not in work_dir, f"Clone should not be a worktree, got work_dir: {work_dir}"

    # A standalone clone owns its own git object store: its git directory lives
    # inside the work_dir, unlike a worktree whose git dir points back to the
    # source repo. Verify the clone is a real repo that carries over history.
    # `mngr exec` prints the command's output followed by a status footer line,
    # so we read the first line for the actual git output.
    git_dir_result = e2e.run(
        "mngr exec my-task 'git rev-parse --absolute-git-dir'",
        comment="Verify the clone has its own git directory (not shared with the source)",
    )
    expect(git_dir_result).to_succeed()
    git_dir = git_dir_result.stdout.strip().splitlines()[0].strip()
    assert os.path.realpath(git_dir).startswith(os.path.realpath(work_dir)), (
        f"Clone's git dir should live inside its own work_dir.\n  git dir: {git_dir}\n  work_dir: {work_dir}"
    )
    commit_count_result = e2e.run(
        "mngr exec my-task 'git rev-list --count HEAD'",
        comment="Verify the clone carried over the source repo's history",
    )
    expect(commit_count_result).to_succeed()
    commit_count = int(commit_count_result.stdout.strip().splitlines()[0].strip())
    assert commit_count >= 1, f"Clone should carry over the source history, got {commit_count} commits"


@pytest.mark.release
@pytest.mark.modal
def test_create_with_snapshot_fictional(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can use an existing snapshot instead of building a new host from scratch:
        mngr create my-task --provider modal --snapshot snap-123abc
    """)
    # The fictional snapshot id won't exist in any modal environment; we just
    # verify mngr parses --snapshot and exits with an error rather than
    # crashing. --type command (with a stand-in `sleep`) gets past agent-type
    # resolution and preflight so that mngr actually reaches the modal provider
    # and attempts to restore from the snapshot -- otherwise it would exit early
    # on "No agent type provided" without ever invoking modal (which the
    # @pytest.mark.modal resource guard requires).
    result = e2e.run(
        "mngr create my-task --provider modal --snapshot snap-123abc --no-connect --no-ensure-clean"
        " --type command -- sleep 100078",
        comment="use an existing snapshot instead of building a new host",
    )
    assert result.exit_code != 0
    # The failure must be about the missing snapshot (modal was actually
    # contacted), not an early bail-out on configuration. We assert the error
    # references the snapshot id so a future regression that exits before
    # reaching modal would be caught here rather than only by the resource guard.
    combined_output = result.stdout + result.stderr
    assert "snap-123abc" in combined_output, (
        f"Expected the error to reference the fictional snapshot id.\n  stdout: {result.stdout}\n  stderr: {result.stderr}"
    )


# No @pytest.mark.modal: this test only creates a local agent and runs `mngr
# list`. Neither path invokes the `modal` CLI binary (the sole modal usage the
# resource guard can track across the mngr subprocess boundary -- the SDK
# monkeypatch only lives in the pytest process). `mngr list` discovery is
# deliberately read-only (is_environment_creation_allowed=False), so it never
# shells out to `modal environment create`. Adding the mark would trip the
# guard's superfluous-mark check ("marked with @pytest.mark.modal but never
# invoked modal"). Docker is handled the same way (no @pytest.mark.docker here).
@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# Worktree create (with a ttyd-install attempt) + list + an `mngr exec`
# reachability probe does not fit the default 10s per-test budget; give it
# headroom so the extra verification does not cause timeout flakiness.
@pytest.mark.timeout(30)
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

    # Appearing in the list only proves a record exists. Confirm the headless
    # agent is genuinely running and reachable by executing a command on its
    # host (the non-interactive --headless create must still produce a live,
    # usable agent).
    exec_result = e2e.run("mngr exec my-task pwd", comment="Verify the headless agent is actually running")
    expect(exec_result).to_succeed()

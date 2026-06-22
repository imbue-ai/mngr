"""Tests for the STARTING AND STOPPING AGENTS tutorial section.

Each test corresponds 1:1 to a tutorial script block.
"""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _create_my_task(e2e: E2eSession, sleep_value: int) -> None:
    expect(
        e2e.run(
            f"mngr create my-task --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
            comment=f"create my-task (sleep {sleep_value})",
        )
    ).to_succeed()


def _create_named_agents(e2e: E2eSession, names_and_sleeps: list[tuple[str, int]]) -> None:
    for name, sleep_value in names_and_sleeps:
        expect(
            e2e.run(
                f"mngr create {name} --type command --no-ensure-clean --no-connect -- sleep {sleep_value}",
                comment=f"create {name}",
            )
        ).to_succeed()


# No @pytest.mark.rsync: this test starts an *already-running* agent, which is
# an idempotent no-op, and the agent itself is a local command agent in a git
# repo (GIT_WORKTREE transfer mode). Neither create nor the redundant start
# invokes rsync, so marking the test with rsync would trip the resource guard's
# "marked but never invoked" check.
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_start_idempotent(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # start a stopped agent. Is idempotent, so is safe to call even if already running.
        mngr start my-task
    """)
    _create_my_task(e2e, 100500)
    # Capture the running agent's worktree before the redundant start so we can
    # prove the start did not tear it down and recreate it (the worktree path
    # carries a random suffix, so a recreate would change it).
    before = e2e.run("mngr exec my-task pwd", comment="record the agent's worktree before the redundant start")
    expect(before).to_succeed()
    expect(before.stdout).to_contain("worktrees/my-task")
    # Starting an already-running agent is idempotent: it succeeds rather than erroring.
    expect(e2e.run("mngr start my-task", comment="start a stopped agent (idempotent)")).to_succeed()
    # The redundant start must not have torn the agent down: it is still reachable,
    # exec lands in the agent's own worktree, and that worktree is the very same
    # one as before -- confirming the agent instance was preserved, not recreated.
    after = e2e.run("mngr exec my-task pwd", comment="verify the agent is still reachable")
    expect(after).to_succeed()
    expect(after.stdout).to_contain("worktrees/my-task")
    expect(after.stdout.strip()).to_equal(before.stdout.strip())


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_start_stopped_agent(e2e: E2eSession) -> None:
    # Happy path counterpart to test_start_idempotent: the tutorial block's primary
    # case is starting an agent that is actually stopped, so stop a running agent and
    # bring it back up.
    e2e.write_tutorial_block("""
        # start a stopped agent. Is idempotent, so is safe to call even if already running.
        mngr start my-task
    """)
    _create_my_task(e2e, 100513)
    expect(e2e.run("mngr stop my-task", comment="stop the running agent").stdout).to_contain("Stopped agent: my-task")
    # Precondition for the tutorial block: the agent is genuinely stopped, so the
    # start below does real work rather than being a no-op (cf. test_start_idempotent).
    # List queries are scoped to the local provider to avoid enumerating remote
    # providers (which the test never uses and which may be unconfigured).
    stopped = e2e.run("mngr list --provider local --stopped", comment="confirm my-task is stopped before starting")
    expect(stopped).to_succeed()
    expect(stopped.stdout).to_contain("my-task")
    started = e2e.run("mngr start my-task", comment="start the now-stopped agent")
    expect(started).to_succeed()
    expect(started.stdout).to_contain("Started agent: my-task")
    # The start took effect: the agent is no longer reported as stopped.
    after = e2e.run("mngr list --provider local --stopped", comment="verify my-task is no longer stopped after starting")
    expect(after).to_succeed()
    expect(after.stdout).not_to_contain("my-task")
    # The restarted agent is reachable again, and exec lands in its own worktree.
    reachable = e2e.run("mngr exec my-task pwd", comment="verify the restarted agent is reachable")
    expect(reachable).to_succeed()
    expect(reachable.stdout).to_contain("worktrees/my-task")


# Local command agents create + start via tmux; starting a named agent resolves
# it locally and never enumerates Modal, so this test does not carry
# @pytest.mark.modal. Unlike its sibling start tests, this one verifies --connect
# purely through the connect command's pidfile side effect (no `mngr exec`/`mngr
# stop`), so it never shells out to rsync and is intentionally not marked
# @pytest.mark.rsync. The default 10s pytest timeout is too tight for the full
# create + start round-trip (~15s), so bump it.
#
# @pytest.mark.flaky: `mngr start` drives a single large tmux command (new
# session + windows + send-keys + the background activity tracker). Under heavy
# offload load that tmux round-trip can occasionally stall past the 30s
# per-command timeout, even though it normally completes in a couple of seconds.
# That is an infra-level fluke rather than a product defect, so offload retries
# it; the per-command timeout below is also widened to absorb milder slowdowns.
@pytest.mark.flaky
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_start_connect(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # start a stopped agent and immediately connect to it
        mngr start my-task --connect
    """)
    _create_my_task(e2e, 100501)
    expect(
        e2e.run("mngr start my-task --connect", comment="start and immediately connect", timeout=90.0)
    ).to_succeed()
    # --connect runs the configured connect_command. The e2e harness's
    # connect_command (mngr-e2e-connect) records the session and writes a
    # "<agent>.pid" file into MNGR_TEST_ASCIINEMA_DIR (== e2e.output_dir). The
    # connect step only runs when start actually started a stopped agent, so the
    # file's presence verifies the whole start-then-connect path -- the behavior
    # that distinguishes --connect from a plain start. This is a local,
    # filesystem-only check that (unlike `mngr list`/`mngr exec`) does not
    # enumerate remote providers, keeping the test free of Modal usage.
    assert (e2e.output_dir / "my-task.pid").exists(), (
        f"Expected --connect to invoke the connect command and write my-task.pid in {e2e.output_dir}, "
        f"but it is missing. Directory contents: {sorted(p.name for p in e2e.output_dir.iterdir())}"
    )


# This test creates three agents, starts all three in a single invocation, and
# then execs into each to confirm it is reachable. A single create + start
# round-trip is already ~15s (see test_start_connect), and the per-agent exec
# checks add more, so the default 10s pytest timeout is far too tight; bump it
# generously. No @pytest.mark.rsync: create/start/exec of local command agents
# run on the local host and never sync files over rsync (which is only used to
# reach remote hosts).
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(240)
def test_start_multiple_agents(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # start multiple agents at once
        mngr start agent-1 agent-2 agent-3
    """)
    _create_named_agents(e2e, [("agent-1", 100502), ("agent-2", 100503), ("agent-3", 100504)])
    result = e2e.run("mngr start agent-1 agent-2 agent-3", comment="start multiple agents at once")
    expect(result).to_succeed()
    # The point of "start multiple agents at once" is that a single invocation
    # addresses every named agent, so assert all three appear in the output
    # rather than only checking the exit code.
    for name in ("agent-1", "agent-2", "agent-3"):
        expect(result.stdout).to_contain(name)
    # The summary line confirms the single invocation acted on all three agents
    # (not, say, just the first one), which is the whole point of the bulk start.
    expect(result.stdout).to_contain("Successfully started 3 agent(s)")
    # Verify the concrete effect, like a human would: each named agent is actually
    # started and reachable, landing exec in its own worktree -- not merely named
    # in the start output. This resolves each agent locally without enumerating
    # remote providers, keeping the test Modal-free.
    for name in ("agent-1", "agent-2", "agent-3"):
        reachable = e2e.run(f"mngr exec {name} pwd", comment=f"verify {name} is started and reachable")
        expect(reachable).to_succeed()
        expect(reachable.stdout).to_contain(f"worktrees/{name}")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_start_all_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # start all stopped agents by simply passing their ids from "mngr list" and reading the ids from stdin (that's what the "-" means)
        mngr list --ids | mngr start -
    """)
    _create_my_task(e2e, 100505)
    # Stop the agent first so the stdin-driven start does real work (starting a
    # stopped agent), matching the tutorial's "start all stopped agents" intent.
    expect(e2e.run("mngr stop my-task", comment="stop my-task so it is actually stopped")).to_succeed()
    stopped = e2e.run("mngr list --stopped", comment="confirm my-task is stopped")
    expect(stopped).to_succeed()
    expect(stopped.stdout).to_contain("my-task")
    # start all stopped agents by piping their ids from "mngr list" into stdin.
    result = e2e.run("mngr list --ids | mngr start -", comment="start all via stdin")
    expect(result).to_succeed()
    expect(result.stdout).to_contain("my-task")
    # Verify the start took effect: the agent is no longer in the stopped set.
    after = e2e.run("mngr list --stopped", comment="verify my-task is no longer stopped after stdin-driven start")
    expect(after).to_succeed()
    expect(after.stdout).not_to_contain("my-task")


@pytest.mark.timeout(180)
@pytest.mark.release
@pytest.mark.tmux
def test_start_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # dry-run to see what would happen without actually starting anything
        mngr list --ids | mngr start - --dry-run
    """)
    _create_my_task(e2e, 100506)
    # The tutorial block previews "start all stopped agents", so the agent must
    # actually be stopped for the dry-run to report a non-empty plan (start only
    # targets STOPPED agents; a freshly created agent is still WAITING). Stop it
    # via a local-provider-scoped address (`name@host.local`, produced by
    # `mngr list --provider local --ids`) so the setup never enumerates remote
    # providers -- a bare `mngr stop my-task` would fan out to every provider.
    expect(
        e2e.run(
            "mngr list --provider local --ids | mngr stop -",
            comment="stop my-task so the dry-run has a stopped agent to plan",
            timeout=60.0,
        )
    ).to_succeed()

    # Capture every agent's lifecycle state before the dry-run so we can prove
    # the dry-run leaves all of them untouched. Scope to the local provider to
    # avoid enumerating remote providers (which this test never uses and which
    # can be slow enough to time the command out).
    state_before = e2e.run(
        "mngr list --provider local --format '{name}={state}'",
        comment="capture agent state before the dry-run",
        timeout=60.0,
    )
    expect(state_before).to_succeed()

    # `mngr start - --dry-run` is the command under test. The piped `mngr list`
    # is scoped to the local provider (the test's agents are all local) so the
    # pipe never enumerates remote providers -- otherwise an unscoped
    # `mngr list --ids` fans out to every configured backend (AWS, Modal,
    # Vultr, ...), which is slow and emits unrelated provider warnings/errors.
    dry_run = e2e.run(
        "mngr list --provider local --ids | mngr start - --dry-run",
        comment="dry-run to see what would happen",
        timeout=60.0,
    )
    expect(dry_run).to_succeed()
    # The dry-run reports the plan (which agents would be started) without acting.
    expect(dry_run.stdout).to_contain("Would be started")
    expect(dry_run.stdout).to_contain("my-task")

    # A dry-run must be a no-op: every agent's state is identical afterwards, so
    # nothing was actually started.
    state_after = e2e.run(
        "mngr list --provider local --format '{name}={state}'",
        comment="confirm the dry-run did not change any agent state",
        timeout=60.0,
    )
    expect(state_after).to_succeed()
    expect(state_after.stdout).to_equal(state_before.stdout)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_stop_basic(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop a running agent
        mngr stop my-task
    """)
    _create_my_task(e2e, 100507)
    expect(e2e.run("mngr stop my-task", comment="stop a running agent")).to_succeed()
    # Verify the stop actually took effect: my-task should now be reported as
    # stopped and should no longer appear among the running agents. List queries
    # are scoped to the local provider to avoid enumerating remote providers
    # (which the test never uses and which may be unavailable in CI).
    stopped = e2e.run("mngr list --provider local --stopped", comment="verify my-task is now stopped")
    expect(stopped).to_succeed()
    assert "my-task" in stopped.stdout, f"expected my-task in stopped list, got: {stopped.stdout!r}"
    running = e2e.run("mngr list --provider local --running", comment="verify my-task is no longer running")
    expect(running).to_succeed()
    assert "my-task" not in running.stdout, f"expected my-task to not be running, got: {running.stdout!r}"


# No @pytest.mark.rsync: this local command agent is created with --no-connect
# and only stopped/archived and listed, so no remote source transfer and no
# rsync-backed connect/attach ever happens -- the rsync binary is never invoked.
# (create/stop still run the agent under tmux, hence @pytest.mark.tmux.)
#
# @pytest.mark.flaky: the local tmux-backed stop occasionally exceeds the e2e
# per-command default (30s) on a slow sandbox, so the stop call below is given
# headroom and offload still retries the whole test if it slips further. Matches
# the precedent on test_destroy_single_agent.
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.flaky
@pytest.mark.timeout(120)
def test_stop_archive(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop and archive the agent (marks it archived so it can be filtered out of listings; its state is preserved).
        mngr stop my-task --archive
    """)
    _create_my_task(e2e, 100508)
    # Bump the per-command timeout above the 30s default: the local tmux-backed
    # stop is the real workload here and can run long when the sandbox is slow.
    stop_result = e2e.run("mngr stop my-task --archive", comment="stop and archive the agent", timeout=60.0)
    expect(stop_result).to_succeed()
    # --archive both stops the agent and sets the 'archived_at' label.
    expect(stop_result.stdout).to_contain("Stopped agent: my-task")

    # The agent is now archived: it carries the 'archived_at' label and so
    # shows up under --archived. List queries are scoped to the local provider
    # to avoid enumerating remote providers (which the test never uses).
    archived_result = e2e.run("mngr list --provider local --archived", comment="verify my-task is now archived")
    expect(archived_result).to_succeed()
    expect(archived_result.stdout).to_contain("my-task")

    # Archived agents are excluded from --active, confirming the archive label
    # filters the agent out of normal listings without destroying it.
    active_result = e2e.run(
        "mngr list --provider local --active", comment="verify my-task is excluded from active agents"
    )
    expect(active_result).to_succeed()
    expect(active_result.stdout).not_to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_archive_command(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can also archive an agent via the "archive" command, which is basically just a shortcut for "stop --archive"
        mngr archive my-task
    """)
    _create_my_task(e2e, 100509)
    # The archive command only archives non-running agents; in the tutorial flow
    # my-task has already been stopped (see "mngr stop my-task --archive" just
    # above this block), so stop it first to mirror that state.
    expect(e2e.run("mngr stop my-task", comment="stop my-task before archiving")).to_succeed()
    expect(e2e.run("mngr archive my-task", comment="archive shortcut for stop --archive")).to_succeed()
    # Archiving sets an "archived_at" label; verify the agent is actually
    # archived rather than just trusting the command's exit code. List queries
    # are scoped to the local provider to avoid enumerating remote providers
    # (which the test never uses; my-task is a local command agent).
    list_result = e2e.run(
        "mngr list --provider local --archived --format json", comment="verify my-task is archived"
    )
    expect(list_result).to_succeed()
    archived_agents = [a for a in json.loads(list_result.stdout)["agents"] if a["name"] == "my-task"]
    assert len(archived_agents) == 1, f"expected my-task in archived list, got {list_result.stdout}"
    assert "archived_at" in archived_agents[0]["labels"], archived_agents[0]["labels"]
    # Archiving preserves the agent's state rather than destroying it: the
    # already-stopped agent stays STOPPED (archive is a label, not a teardown).
    assert archived_agents[0]["state"] == "STOPPED", archived_agents[0]


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_archive_running_agent_is_skipped(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # you can also archive an agent via the "archive" command, which is basically just a shortcut for "stop --archive"
        mngr archive my-task
    """)
    _create_my_task(e2e, 100513)
    # Unhappy path: without --force, archiving a *running* agent is a no-op. The
    # agent is skipped with a warning and the archived_at label is NOT applied.
    result = e2e.run("mngr archive my-task", comment="archive a running agent (skipped without --force)")
    expect(result).to_succeed()
    expect(result.stdout + result.stderr).to_contain("Skipping running agent")
    # Confirm nothing was archived.
    list_result = e2e.run(
        "mngr list --provider local --archived --format json", comment="verify my-task was not archived"
    )
    expect(list_result).to_succeed()
    assert not [a for a in json.loads(list_result.stdout)["agents"] if a["name"] == "my-task"], list_result.stdout
    # Skipping means the agent is left untouched, not torn down -- this is the
    # whole point of refusing to archive without --force. It must therefore still
    # show up as an active (non-archived, live-host) agent.
    active_result = e2e.run("mngr list --provider local --active", comment="verify my-task is still active")
    expect(active_result).to_succeed()
    expect(active_result.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
# This test only creates a local git-repo agent (which populates its worktree via
# git-worktree, not rsync) and then stops it (stop never rsyncs), so it does not
# exercise rsync and must not carry @pytest.mark.rsync (the resource guard rejects
# a superfluous mark). The stdin-piped `mngr list --ids | mngr stop -` runs two
# mngr processes back to back, which exceeds the default 10s pytest timeout (its
# start counterpart, test_start_all_via_stdin, uses the same 120s bump).
@pytest.mark.timeout(120)
def test_stop_all_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop all running agents
        mngr list --ids | mngr stop -
    """)
    _create_my_task(e2e, 100510)
    stop_result = e2e.run("mngr list --ids | mngr stop -", comment="stop all running agents")
    expect(stop_result).to_succeed()
    # The command reports which agents it stopped.
    expect(stop_result.stdout).to_contain("my-task")
    # Verify the concrete effect: the agent is no longer running, but still
    # exists in a stopped state (stop is not destroy/archive). List queries are
    # scoped to the local provider to avoid enumerating remote providers (which
    # the test never uses and which may be unconfigured, causing a non-zero exit).
    running_after = e2e.run("mngr list --provider local --running", comment="verify nothing is left running")
    expect(running_after).to_succeed()
    expect(running_after.stdout).not_to_contain("my-task")
    stopped_after = e2e.run("mngr list --provider local --stopped", comment="verify the agent is now stopped")
    expect(stopped_after).to_succeed()
    expect(stopped_after.stdout).to_contain("my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(180)
def test_archive_stopped_via_stdin(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # archive all stopped agents (handy for cleaning up "mngr list" after a batch of finished work).
        mngr list --stopped --ids | mngr archive -
    """)
    _create_my_task(e2e, 100511)
    expect(e2e.run("mngr stop my-task", comment="stop my-task before archive")).to_succeed()

    # The precondition/effect checks below scope discovery to the local provider
    # (`--provider local`). The agent under test runs on the local provider, so
    # this verifies exactly the state we care about while keeping the assertions
    # independent of whichever remote backends (docker, the cloud providers, ...)
    # happen to be installed and enabled in the workspace. Those backends are
    # unconfigured/unreachable in the isolated e2e environment and would
    # otherwise make a bare `mngr list` exit non-zero (e.g. aws/azure/gcp
    # deliberately raise ProviderUnavailableError when credentials are absent, and
    # docker errors when no daemon is running). The tutorial command itself is
    # left exactly as written.
    #
    # Precondition: my-task is stopped and not yet archived (no archived_at label).
    stopped_before = e2e.run(
        "mngr list --stopped --provider local", comment="confirm my-task is stopped before archiving"
    )
    expect(stopped_before).to_succeed()
    expect(stopped_before.stdout).to_match(r"my-task\s+STOPPED")
    archived_before = e2e.run("mngr list --archived --provider local", comment="confirm my-task is not yet archived")
    expect(archived_before).to_succeed()
    expect(archived_before.stdout).not_to_contain("my-task")

    expect(
        e2e.run(
            "mngr list --stopped --ids | mngr archive -",
            comment="archive all stopped agents",
        )
    ).to_succeed()

    # Effect: archiving applies the archived_at label, so my-task now shows up
    # under --archived and is filtered out of the cleaned-up --active listing.
    archived_after = e2e.run("mngr list --archived --provider local", comment="my-task now appears as archived")
    expect(archived_after).to_succeed()
    expect(archived_after.stdout).to_contain("my-task")
    active_after = e2e.run(
        "mngr list --active --provider local", comment="my-task is filtered out of the active listing"
    )
    expect(active_after).to_succeed()
    expect(active_after.stdout).not_to_contain("my-task")


# This test only exercises a local command agent plus provider enumeration, so it
# carries neither @pytest.mark.modal nor @pytest.mark.rsync:
#   - modal: `mngr list --ids` / `mngr stop -` reach Modal only through the
#     in-process gRPC SDK, which runs inside the mngr subprocess. The modal
#     resource guard's SDK monkeypatch runs in the pytest process, not the
#     subprocess, and the `modal` CLI binary is never invoked during the call
#     phase -- so an @modal mark is flagged as superfluous ("never invoked modal").
#   - rsync: rsync is only used to sync files to remote hosts; a local command
#     agent is created in a git worktree and never invokes the rsync binary.
# tmux remains because local agent create/start drives the agent's tmux session.
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(240)
def test_stop_dry_run(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # dry-run to see what would be stopped
        mngr list --ids | mngr stop - --dry-run
    """)
    _create_my_task(e2e, 100512)
    # This pipeline enumerates every provider (including Modal) twice: once in
    # `mngr list --ids` and again in `mngr stop -`, which calls find_all_agents
    # to resolve the piped ids. Two sequential Modal enumerations exceed the
    # default 30s per-command timeout, so allow more time. (The sibling
    # test_start_dry_run avoids this because `mngr start -` resolves agents
    # locally rather than enumerating remote providers.)
    dry_run_result = e2e.run(
        "mngr list --ids | mngr stop - --dry-run", comment="dry-run to see what would be stopped", timeout=120.0
    )
    expect(dry_run_result).to_succeed()
    # The dry-run must report the agent that would be stopped...
    expect(dry_run_result.stdout).to_contain("Would stop")
    expect(dry_run_result.stdout).to_contain("my-task")
    # ...without actually stopping it.
    expect(dry_run_result.stdout).not_to_contain("Stopped agent")

    # Confirm the dry-run left the agent running: a real stop still finds and
    # stops it (it would report nothing to stop had the dry-run stopped it).
    # `mngr stop` also enumerates providers (including Modal) via find_all_agents,
    # so give it more than the default 30s as well.
    real_stop_result = e2e.run("mngr stop my-task", comment="verify dry-run left the agent running", timeout=60.0)
    expect(real_stop_result).to_succeed()
    expect(real_stop_result.stdout).to_contain("Stopped agent: my-task")


@pytest.mark.release
@pytest.mark.timeout(60)
def test_stop_by_session_name(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stop has a special variant for finding an agent by its tmux session name:
        mngr stop --session my-session-name
        # this is used primarily to implement the hotkey for exiting from tmux (ex: ctrl-t)
    """)
    # The tutorial's "my-session-name" placeholder lacks the configured tmux
    # session prefix, so mngr should reject it via the --session format guard:
    # a clear validation error and a non-zero exit, cleanly (no Python
    # traceback) rather than crashing.
    result = e2e.run(
        "mngr stop --session my-session-name",
        comment="stop variant that finds an agent by tmux session name",
    )
    combined_output = result.stdout + result.stderr
    assert result.exit_code != 0, f"Expected non-zero exit, got {result.exit_code}: {combined_output}"
    assert "Traceback" not in combined_output, f"mngr crashed instead of exiting cleanly: {combined_output}"
    # The error should explain *why* the session was rejected (prefix mismatch).
    assert "does not match the expected format" in combined_output, combined_output


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_stop_by_session_name_happy_path(e2e: E2eSession) -> None:
    # Shares the same tutorial block as test_stop_by_session_name: that test
    # covers the unhappy path (a bogus session name), while this one covers the
    # happy path where the session name maps to a real, running agent that gets
    # stopped -- the behavior the ctrl-t hotkey relies on.
    e2e.write_tutorial_block("""
        # stop has a special variant for finding an agent by its tmux session name:
        mngr stop --session my-session-name
        # this is used primarily to implement the hotkey for exiting from tmux (ex: ctrl-t)
    """)
    _create_my_task(e2e, 100514)

    # An agent's tmux session name is "{prefix}{agent_name}". Read the configured
    # prefix from the environment rather than hardcoding it, then reconstruct
    # my-task's session name so we can target it the same way the ctrl-t hotkey does.
    prefix_result = e2e.run('printf %s "$MNGR_PREFIX"', comment="read the configured tmux session prefix")
    expect(prefix_result).to_succeed()
    prefix = prefix_result.stdout.strip()
    assert prefix, f"expected MNGR_PREFIX to be set, transcript:\n{e2e.transcript}"
    session_name = f"{prefix}my-task"

    stop_result = e2e.run(
        f"mngr stop --session {session_name}",
        comment="stop the agent found by its tmux session name",
    )
    expect(stop_result).to_succeed()
    expect(stop_result.stdout).to_contain("Stopped agent: my-task")

    # Verify the concrete effect: the agent is no longer running but still exists
    # in a stopped state (stop is not destroy/archive). List queries are scoped to
    # the local provider to avoid enumerating remote providers (which the test
    # never uses), matching the pattern used elsewhere in this file.
    running_after = e2e.run("mngr list --provider local --running", comment="verify my-task is no longer running")
    expect(running_after).to_succeed()
    expect(running_after.stdout).not_to_contain("my-task")
    stopped_after = e2e.run("mngr list --provider local --stopped", comment="verify my-task is now stopped")
    expect(stopped_after).to_succeed()
    expect(stopped_after.stdout).to_contain("my-task")

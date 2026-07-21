"""Tests for the RUNNING NON-AGENT PROCESSES tutorial section."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.mngr.utils.polling import poll_until
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
# This test issues three sequential mngr invocations (create, exec, list). Each
# pays the full mngr CLI startup cost, and `mngr exec` additionally waits for the
# agent's activity tracker. Under the contended sandboxes the release suite runs
# in, a single invocation can take ~20-25s, so the per-test budget must comfortably
# cover the whole sequence (the sibling Modal command-agent tests use 300s for the
# same reason).
@pytest.mark.timeout(300)
def test_command_agent_python_http(e2e: E2eSession) -> None:
    """Tutorial block:
        # run a Python script as a managed process
        mngr create my-server --type command -- python -m http.server 8080

    Scope: `mngr create --type command -- <cmd>` runs an arbitrary long-lived
    process as a managed (non-agent) command agent. The process is actually
    running inside the agent (visible via `mngr exec`), and the agent is listed
    as a local command agent carrying the exact command it was given. The
    tutorial's `python -m http.server 8080` is substituted with a portable sleep
    so the test does not bind a real port.
    """
    # The tutorial runs `python -m http.server 8080`; substitute a portable
    # long-lived sleep so the test does not bind a real port. Bind the command
    # locally so the assertions below can check for the exact string.
    # The mngr CLI startup cost dominates each invocation and balloons under the
    # contended sandboxes the release suite runs in, so give each command generous
    # headroom above the 30s default; the per-test timeout is the real hang guard.
    expected_command = "sleep 100990"
    expect(
        e2e.run(
            f"mngr create my-server --type command --no-ensure-clean --no-connect -- {expected_command}",
            comment="run a Python script as a managed process (substituted with sleep)",
            timeout=90.0,
        )
    ).to_succeed()

    # The managed process should actually be running inside the agent.
    ps_result = e2e.run(
        "mngr exec my-server 'ps aux | grep sleep'",
        comment="verify the managed process is running inside the agent",
        timeout=90.0,
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain(expected_command)

    # The agent should be listed as a local command agent with its configured
    # command. Scope to the local provider so listing stays fast and does not
    # reach out to remote providers (which this local agent never uses).
    list_result = e2e.run(
        "mngr list --provider local --format json",
        comment="verify the agent is listed with the managed command",
        timeout=90.0,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-server"]
    assert len(matching) == 1, f"expected exactly one 'my-server' agent, got {matching}"
    assert matching[0]["command"] == expected_command
    assert matching[0]["type"] == "command"
    assert matching[0]["host"]["provider_name"] == "local"


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(300)
def test_command_agent_data_pipeline(e2e: E2eSession) -> None:
    """Tutorial block:
        # run a long-running data pipeline
        mngr create etl-job --type command --idle-mode run --idle-timeout 60 -- python etl_pipeline.py

    Scope: a command agent created with `--idle-mode run --idle-timeout 60`
    round-trips that idle configuration onto the created agent (idle_mode == RUN,
    idle_timeout_seconds == 60) alongside its command -- the idle settings are
    not silently dropped. Idle detection requires a remote provider (the local
    host cannot be stopped by mngr), so this runs on Modal, substituting the
    python pipeline with a portable sleep.
    """
    # Idle detection (--idle-mode/--idle-timeout) requires a remote provider --
    # the local host cannot be stopped by mngr, so it rejects these options. Run
    # on Modal to exercise the real idle path, substituting the python pipeline
    # with a portable sleep so the test doesn't depend on an etl_pipeline.py.
    expect(
        e2e.run(
            "mngr create etl-job --provider modal --type command --idle-mode run --idle-timeout 60"
            " --no-ensure-clean --no-connect -- sleep 100991",
            comment="run a long-running data pipeline",
            timeout=180.0,
        )
    ).to_succeed()

    # Verify the pipeline command and the idle configuration were actually
    # applied to the created agent (not silently dropped). The idle settings are
    # the distinctive feature of this tutorial line, so assert they round-trip.
    # Scope the listing to the modal provider (where this agent runs) so it does
    # not reach out to other enabled providers (e.g. aws) that may be
    # unconfigured in the test environment.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="verify the etl-job agent's command and idle configuration",
        timeout=120.0,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "etl-job"]
    assert len(matching) == 1, f"expected exactly one etl-job agent, got {matching}"
    etl_job = matching[0]
    assert etl_job["command"] == "sleep 100991", etl_job
    # IdleMode serializes as an upper-case enum value (see primitives.IdleMode).
    assert etl_job["idle_mode"] == "RUN", etl_job
    assert etl_job["idle_timeout_seconds"] == 60, etl_job


@pytest.mark.release
@pytest.mark.tmux
# This test issues several sequential mngr invocations (create, list) plus tmux
# queries. Each mngr invocation pays the full CLI startup cost, which balloons
# under the contended sandboxes the release suite runs in (a single invocation
# can take ~20-25s), so the per-test budget must comfortably cover the whole
# sequence -- matching the sibling command-agent tests, which use 300s for the
# same reason. Without this, the suite-default 10s timeout fires mid-create.
@pytest.mark.timeout(300)
def test_command_agent_dev_server_extra_windows(e2e: E2eSession) -> None:
    """Tutorial block:
        # run a dev server with extra tmux windows for logs
        mngr create dev-env --type command -w logs="tail -f /var/log/app.log" -- npm run dev

    Scope: the `-w NAME=CMD` flag adds an extra tmux window running its own
    command alongside the agent's main command. The command agent is created with
    its given command, and an extra tmux window named "logs" actually exists in
    the agent's tmux session (not just the main window). The npm command and tail
    target are substituted with portable sleeps.
    """
    # Substitute the npm command and tail target with portable sleeps so the
    # test doesn't depend on npm or a /var/log file being present.
    expect(
        e2e.run(
            "touch /tmp/mngr-app.log",
            comment="ensure the tail target exists",
        )
    ).to_succeed()
    # The mngr CLI startup cost dominates each invocation and balloons under the
    # contended sandboxes the release suite runs in, so give each command generous
    # headroom above the 30s default; the per-test timeout is the real hang guard.
    expect(
        e2e.run(
            'mngr create dev-env --type command -w logs="tail -f /tmp/mngr-app.log" --no-ensure-clean --no-connect -- sleep 100992',
            comment="dev server with extra tmux window for logs",
            timeout=90.0,
        )
    ).to_succeed()

    # Verify the agent was created. Scope the listing to the local provider so
    # it stays fast and does not reach out to remote providers (this agent runs
    # purely locally; the Modal command-agent path is covered by
    # test_command_agent_batch_job_modal).
    list_result = e2e.run(
        "mngr list --provider local --format json",
        comment="Verify the dev-env agent was created",
        timeout=90.0,
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "dev-env"]
    assert len(matching) == 1, f"Expected exactly one 'dev-env' agent, got: {agents}"
    assert matching[0]["type"] == "command", f"Expected a command agent, got: {matching[0]}"
    assert matching[0]["command"] == "sleep 100992", f"Unexpected command: {matching[0]}"

    # Verify the extra tmux window named "logs" was actually created alongside
    # the main window, since that is the distinguishing behavior of the -w flag.
    # mngr runs the agent's command and its extra windows in a tmux session named
    # "<prefix><agent>" on the e2e harness's tmux socket ($TMUX_TMPDIR/tmux-0/default,
    # the same socket the generated destroy-env script manages), so query it there.
    windows_result = e2e.run(
        'tmux -S "$TMUX_TMPDIR/tmux-0/default" list-windows -t mngr_test-dev-env -F "#{window_name}"',
        comment="Verify the extra 'logs' tmux window exists",
    )
    expect(windows_result).to_succeed()
    window_names = windows_result.stdout.strip().split("\n")
    assert "logs" in window_names, f"Expected 'logs' window, got: {window_names}"


# The reconnect part of the scope requires snapshotting and stopping the Modal
# host, then restoring it from that snapshot on reconnect. That snapshot/stop +
# snapshot-restore round trip pushes past the 300s used by the create-only
# command-agent tests, so allow a wider budget.
@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(420)
def test_command_agent_batch_job_modal(e2e: E2eSession) -> None:
    """Tutorial block:
        # use --idle-mode run so the host stops when the process finishes
        mngr create batch-job --provider modal --type command --idle-mode run --idle-timeout 30 -- bash -c "python train.py && python evaluate.py"
        # the container will be automatically snapshotted when completed, so you can later come back and connect (and start) to see the results:
        mngr conn batch-job

    Scope: a Modal command agent created with `--idle-mode run --idle-timeout 30`
    is registered on the modal provider (discoverable by name on a modal host)
    with its idle configuration round-tripped (idle_mode == RUN,
    idle_timeout_seconds == 30) and its bash command preserved. After it
    completes and is snapshotted, `mngr conn batch-job` reconnects successfully.
    The train/evaluate python commands are substituted with echoes.
    """
    expect(
        e2e.run(
            'mngr create batch-job --provider modal --type command --idle-mode run --idle-timeout 30 --no-connect --no-ensure-clean -- bash -c "echo train && echo evaluate"',
            comment="modal batch job with --idle-mode run",
            timeout=180.0,
        )
    ).to_succeed()

    # Verify the batch job was actually registered on the Modal provider, not
    # just that create exited 0: it must be discoverable by name on a modal host.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="verify the batch job is registered on the modal provider",
        timeout=120.0,
    )
    expect(list_result).to_succeed()
    matching = [a for a in json.loads(list_result.stdout)["agents"] if a["name"] == "batch-job"]
    assert len(matching) == 1, f"Expected exactly one batch-job agent, got: {matching}"
    batch_job = matching[0]
    assert batch_job["host"]["provider_name"] == "modal", f"Expected batch-job on modal, got: {batch_job['host']}"
    assert batch_job["type"] == "command", batch_job
    # The idle configuration is the distinctive feature of this tutorial line
    # ("use --idle-mode run so the host stops when the process finishes"), so
    # assert it round-trips. IdleMode serializes as an upper-case enum value.
    assert batch_job["idle_mode"] == "RUN", batch_job
    assert batch_job["idle_timeout_seconds"] == 30, batch_job
    # The command's quotes are normalized (double -> single) on round-trip, so
    # assert on the substrings rather than an exact string match.
    assert "echo train" in batch_job["command"], batch_job
    assert "echo evaluate" in batch_job["command"], batch_job

    online_host_states = ("RUNNING", "STARTING", "BUILDING")

    def batch_job_host_state() -> str | None:
        """The batch job's Modal host state per `mngr list`, or None on a transient error."""
        result = e2e.run(
            "mngr list --provider modal --format json",
            comment="check the batch job host state",
        )
        if result.exit_code != 0:
            return None
        matching_agents = [a for a in json.loads(result.stdout)["agents"] if a["name"] == "batch-job"]
        return matching_agents[0]["host"]["state"] if matching_agents else None

    # The distinctive promise of this tutorial line -- "the container will be
    # automatically snapshotted when completed, so you can later come back and
    # connect (and start) to see the results" -- is the reconnect *from a
    # snapshot*. Stopping the (already-completed) batch job snapshots and
    # terminates its Modal sandbox, reaching the same completed-and-snapshotted
    # state the idle watcher would eventually produce. We drive it explicitly
    # rather than waiting on the idle timer to keep the test deterministic: the
    # automatic idle-shutdown-and-snapshot path is covered in depth by
    # mngr_modal.test_modal_idle_shutdown, and this test's idle configuration is
    # already asserted above. Reconnecting must then restore the host from that
    # snapshot.
    expect(
        e2e.run("mngr stop batch-job", comment="snapshot and stop the completed batch job", timeout=180.0)
    ).to_succeed()

    # The stopped host's sandbox is terminated but its snapshots remain, so its
    # state is no longer running -- this is the "snapshotted" precondition for
    # the reconnect below.
    assert poll_until(
        lambda: (batch_job_host_state() or "RUNNING") not in online_host_states,
        timeout=90.0,
        poll_interval=5.0,
    ), f"batch-job host did not stop and snapshot; last observed host state: {batch_job_host_state()}"

    # `mngr conn` defaults to --start, so connecting to the snapshotted host
    # restores it from its snapshot before attaching -- exactly the documented
    # "come back and connect (and start) to see the results" flow. Allow extra
    # time since restoring a Modal sandbox from a snapshot is slower than a plain
    # attach.
    expect(
        e2e.run("mngr conn batch-job", comment="connect back to the snapshotted batch job", timeout=180.0)
    ).to_succeed()

    # A successful reconnect must have brought the host back online: connect
    # errors out before attaching if the snapshotted host cannot be restored, so
    # the observable effect of "reconnects successfully" is that the host is
    # running again rather than still stopped.
    assert poll_until(
        lambda: (batch_job_host_state() or "") in online_host_states,
        timeout=90.0,
        poll_interval=5.0,
    ), (
        f"batch-job host did not return to a running state after reconnect; last observed host state: {batch_job_host_state()}"
    )

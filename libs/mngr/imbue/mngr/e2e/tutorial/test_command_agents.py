"""Tests for the RUNNING NON-AGENT PROCESSES tutorial section."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
# `mngr exec` runs the agent's command and waits for the agent's activity
# tracker, which pushes the test past the default 10s per-test timeout.
@pytest.mark.timeout(60)
def test_command_agent_python_http(e2e: E2eSession) -> None:
    # The tutorial runs `python -m http.server 8080`; substitute a portable
    # long-lived sleep so the test does not bind a real port. Bind the command
    # locally so the assertions below can check for the exact string.
    expected_command = "sleep 100990"
    e2e.write_tutorial_block("""
        # run a Python script as a managed process
        mngr create my-server --type command -- python -m http.server 8080
    """)
    expect(
        e2e.run(
            f"mngr create my-server --type command --no-ensure-clean --no-connect -- {expected_command}",
            comment="run a Python script as a managed process (substituted with sleep)",
        )
    ).to_succeed()

    # The managed process should actually be running inside the agent.
    ps_result = e2e.run(
        "mngr exec my-server 'ps aux | grep sleep'",
        comment="verify the managed process is running inside the agent",
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain(expected_command)

    # The agent should be listed as a local command agent with its configured
    # command. Scope to the local provider so listing stays fast and does not
    # reach out to remote providers (which this local agent never uses).
    list_result = e2e.run(
        "mngr list --provider local --format json",
        comment="verify the agent is listed with the managed command",
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
    e2e.write_tutorial_block("""
        # run a long-running data pipeline
        mngr create etl-job --type command --idle-mode run --idle-timeout 60 -- python etl_pipeline.py
    """)
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
    list_result = e2e.run(
        "mngr list --format json",
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


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
def test_command_agent_dev_server_extra_windows(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a dev server with extra tmux windows for logs
        mngr create dev-env --type command -w logs="tail -f /var/log/app.log" -- npm run dev
    """)
    # Substitute the npm command and tail target with portable sleeps so the
    # test doesn't depend on npm or a /var/log file being present.
    expect(
        e2e.run(
            "touch /tmp/mngr-app.log",
            comment="ensure the tail target exists",
        )
    ).to_succeed()
    expect(
        e2e.run(
            'mngr create dev-env --type command -w logs="tail -f /tmp/mngr-app.log" --no-ensure-clean --no-connect -- sleep 100992',
            comment="dev server with extra tmux window for logs",
        )
    ).to_succeed()

    # Verify the agent was created. Scope the listing to the local provider so
    # it stays fast and does not reach out to remote providers (this agent runs
    # purely locally; the Modal command-agent path is covered by
    # test_command_agent_batch_job_modal).
    list_result = e2e.run("mngr list --provider local --format json", comment="Verify the dev-env agent was created")
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


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(300)
def test_command_agent_batch_job_modal(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # use --idle-mode run so the host stops when the process finishes
        mngr create batch-job --provider modal --type command --idle-mode run --idle-timeout 30 -- bash -c "python train.py && python evaluate.py"
        # the container will be automatically snapshotted when completed, so you can later come back and connect (and start) to see the results:
        mngr conn batch-job
    """)
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

    expect(e2e.run("mngr conn batch-job", comment="connect back to the batch job")).to_succeed()

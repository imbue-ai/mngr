"""Tests for the RUNNING NON-AGENT PROCESSES tutorial section."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_command_agent_python_http(e2e: E2eSession) -> None:
    # The tutorial runs `python -m http.server 8080` as a managed process; we
    # substitute a long sleep so the test doesn't bind a port or depend on the
    # server staying up, while still exercising the same managed-process path.
    command = "sleep 100990"
    e2e.write_tutorial_block("""
        # run a Python script as a managed process
        mngr create my-server --type command -- python -m http.server 8080
    """)
    expect(
        e2e.run(
            f"mngr create my-server --type command --no-ensure-clean --no-connect -- {command}",
            comment="run a Python script as a managed process (substituted with sleep)",
            timeout=120.0,
        )
    ).to_succeed()

    # The point of a command agent is that the process actually runs as a
    # managed process -- verify it is alive inside the agent, not just that
    # `mngr create` exited successfully.
    ps_result = e2e.run(
        "mngr exec my-server 'ps aux | grep sleep'",
        comment="verify the managed process is running inside the agent",
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain(command)

    # Verify the agent is tracked and records the command we asked it to run.
    list_result = e2e.run(
        "mngr list --format json",
        comment="verify the managed process appears in mngr list with its command",
    )
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [agent for agent in agents if agent["name"] == "my-server"]
    assert len(matching) == 1, f"expected exactly one 'my-server' agent, got: {agents}"
    assert matching[0]["command"] == command


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.modal
@pytest.mark.timeout(300)
def test_command_agent_data_pipeline(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a long-running data pipeline (idle-mode/idle-timeout require a remote provider, so use Modal)
        mngr create etl-job --provider modal --type command --idle-mode run --idle-timeout 60 -- python etl_pipeline.py
    """)
    # Substitute the python pipeline with a long sleep so the test doesn't depend
    # on an etl_pipeline.py being present. idle-mode/idle-timeout are only
    # supported on remote providers, so this runs on Modal (matching the block).
    expected_command = "sleep 100991"
    expect(
        e2e.run(
            "mngr create etl-job --provider modal --type command --idle-mode run --idle-timeout 60"
            f" --no-ensure-clean --no-connect -- {expected_command}",
            comment="run a long-running data pipeline",
            timeout=180.0,
        )
    ).to_succeed()

    # Verify the agent landed on Modal with the requested idle settings and the
    # substituted pipeline command (not just that create exited 0).
    list_result = e2e.run("mngr list --format json", comment="inspect the etl-job agent's configuration")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "etl-job"]
    assert len(matching) == 1, f"Expected exactly one etl-job agent, got: {[a['name'] for a in agents]}"
    agent = matching[0]
    assert agent["command"] == expected_command, agent["command"]
    assert agent["host"]["provider_name"] == "modal", agent["host"]["provider_name"]
    assert agent["idle_timeout_seconds"] == 60, agent["idle_timeout_seconds"]
    assert agent["idle_mode"].upper() == "RUN", agent["idle_mode"]

    # Verify the pipeline process is actually running inside the agent's host.
    ps_result = e2e.run(
        "mngr exec etl-job 'ps aux | grep sleep'",
        comment="Verify the pipeline command is actually running",
        timeout=60.0,
    )
    expect(ps_result).to_succeed()
    expect(ps_result.stdout).to_contain(expected_command)


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

    # Verify the agent's tmux session exists and that the extra "logs" window
    # was created alongside the main agent window -- this is the behavior the
    # tutorial block demonstrates. Listing windows on the agent's session also
    # confirms the agent was created (the session is named after the agent). We
    # avoid `mngr list` here because it triggers slow remote-provider discovery.
    session_name = "mngr_test-dev-env"
    windows_result = e2e.run(
        f"tmux list-windows -t {session_name} -F '#{{window_name}}'",
        comment="verify the extra 'logs' tmux window exists",
    )
    expect(windows_result).to_succeed()
    window_names = windows_result.stdout.strip().split("\n")
    assert "logs" in window_names, f"Expected a 'logs' window, got: {window_names}"


@pytest.mark.release
@pytest.mark.modal
@pytest.mark.rsync
@pytest.mark.timeout(420)
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

    # Verify the agent was actually created on Modal with the configuration the
    # tutorial describes: a command-type agent running the batch command, with
    # "run" idle-mode and the 30s idle-timeout so the host stops when the
    # process finishes. Checking the create command's exit code alone would not
    # confirm any of these flags took effect.
    list_result = e2e.run(
        "mngr list --provider modal --format json",
        comment="inspect the created batch job's configuration",
        timeout=120.0,
    )
    expect(list_result).to_succeed()
    listing = json.loads(list_result.stdout)
    batch_jobs = [agent for agent in listing["agents"] if agent["name"] == "batch-job"]
    assert len(batch_jobs) == 1, f"expected exactly one batch-job agent, got: {listing['agents']}"
    batch_job = batch_jobs[0]
    assert batch_job["type"] == "command", f"expected a command-type agent, got: {batch_job['type']}"
    assert "echo train && echo evaluate" in batch_job["command"], (
        f"batch command was not preserved, got: {batch_job['command']}"
    )
    assert batch_job["idle_mode"].lower() == "run", f"expected idle_mode 'run', got: {batch_job['idle_mode']}"
    assert batch_job["idle_timeout_seconds"] == 30, (
        f"expected idle_timeout_seconds 30, got: {batch_job['idle_timeout_seconds']}"
    )

    # The tutorial promises you can come back and connect (which starts the host
    # again if it has already stopped after the batch job finished). Give this a
    # generous timeout because reconnecting may need to restart a stopped host.
    conn_result = e2e.run(
        "mngr conn batch-job",
        comment="connect back to the batch job",
        timeout=180.0,
    )
    expect(conn_result).to_succeed()
    expect(conn_result.stdout + conn_result.stderr).to_contain("batch-job")

"""Tests for the RUNNING NON-AGENT PROCESSES tutorial section."""

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_command_agent_python_http(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a Python script as a managed process
        mngr create my-server --type command -- python -m http.server 8080
    """)
    expect(
        e2e.run(
            "mngr create my-server --type command --no-ensure-clean --no-connect -- sleep 100990",
            comment="run a Python script as a managed process (substituted with sleep)",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
def test_command_agent_data_pipeline(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # run a long-running data pipeline
        mngr create etl-job --type command --idle-mode run --idle-timeout 60 -- python etl_pipeline.py
    """)
    expect(
        e2e.run(
            "mngr create etl-job --type command --idle-mode run --idle-timeout 60 --no-ensure-clean --no-connect -- sleep 100991",
            comment="run a long-running data pipeline",
        )
    ).to_succeed()


@pytest.mark.rsync
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
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
    expect(e2e.run("mngr conn batch-job", comment="connect back to the batch job")).to_succeed()

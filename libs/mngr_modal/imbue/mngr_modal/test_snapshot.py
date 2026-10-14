"""Release tests for the snapshot CLI command on Modal."""

import json
import shlex
from pathlib import Path
from typing import Any

import pytest

from imbue.imbue_common.logging import log_span
from imbue.mngr.utils.testing import ModalSubprocessTestEnv
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_modal.testing import run_mngr_against_modal


def _extract_json(output: str) -> dict[str, Any]:
    """Extract the final JSON object from command output.

    When running CLI commands via subprocess, logger output may precede the
    JSON blob on stdout. This finds the last line that looks like JSON.
    """
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"No JSON found in output:\n{output}")


def _create_modal_agent(
    agent_name: str,
    source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
    agent_command: str,
) -> None:
    """Create a Modal agent via the CLI subprocess."""
    with log_span("Creating Modal agent for snapshot test", agent_name=agent_name):
        result = run_mngr_against_modal(
            "create",
            f"{agent_name}@.modal",
            "--type",
            "command",
            "--no-connect",
            "--no-ensure-clean",
            "--source",
            str(source_dir),
            "--",
            *shlex.split(agent_command),
            modal_subprocess_env=modal_subprocess_env,
            timeout=300,
        )
        assert result.returncode == 0, f"Create agent failed: {result.stderr}\n{result.stdout}"


def _destroy_modal_agent(
    agent_name: str,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Destroy a Modal agent via the CLI subprocess."""
    run_mngr_against_modal(
        "destroy",
        agent_name,
        "--force",
        modal_subprocess_env=modal_subprocess_env,
        timeout=240,
    )


# Boots a real Modal host and SSHes into it. Sporadically fails while waiting
# for sshd on the fresh container ("Error reading SSH protocol banner" ->
# "Create agent failed"), which is Modal-side boot timing rather than a defect
# here. Retried by offload; see the SSH-readiness wait in the create path if
# this starts failing consistently.
@pytest.mark.flaky
@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(400)
def test_snapshot_create_then_list_on_modal(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test snapshot functionality on a Modal agent

    Creates a real Modal agent, takes a snapshot, lists it to verify it exists
    """
    agent_name = f"test-snap-lifecycle-{get_short_random_string()}"

    _create_modal_agent(agent_name, temp_source_dir, modal_subprocess_env, "sleep 400005")
    try:
        # Create a snapshot
        with log_span("Creating snapshot for Modal agent", agent_name=agent_name):
            create_result = run_mngr_against_modal(
                "snapshot",
                "create",
                agent_name,
                "--format",
                "json",
                modal_subprocess_env=modal_subprocess_env,
                timeout=120,
            )
            assert create_result.returncode == 0, (
                f"snapshot create failed: {create_result.stderr}\n{create_result.stdout}"
            )
            create_data = _extract_json(create_result.stdout)
            assert create_data["count"] == 1
            snapshot_id = create_data["snapshots_created"][0]["snapshot_id"]

        # List snapshots and verify the new one appears
        with log_span("Listing snapshots for Modal agent", agent_name=agent_name):
            list_result = run_mngr_against_modal(
                "snapshot",
                "list",
                agent_name,
                "--format",
                "json",
                modal_subprocess_env=modal_subprocess_env,
                timeout=120,
            )
            assert list_result.returncode == 0, f"snapshot list failed: {list_result.stderr}\n{list_result.stdout}"
            list_data = _extract_json(list_result.stdout)
            assert list_data["count"] >= 1
            listed_ids = [s["id"] for s in list_data["snapshots"]]
            assert snapshot_id in listed_ids

    finally:
        with log_span("Destroying Modal agent after snapshot test", agent_name=agent_name):
            _destroy_modal_agent(agent_name, modal_subprocess_env)


@pytest.mark.release
@pytest.mark.rsync
@pytest.mark.skip(
    "Just not worth the extra testing time right now (above and beyond what we're already getting via the above)"
)
@pytest.mark.timeout(300)
def test_snapshot_destroy_then_list_on_modal(
    temp_source_dir: Path,
    modal_subprocess_env: ModalSubprocessTestEnv,
) -> None:
    """Test snapshot deletion on a Modal agent.

    Destroys all original snapshots and verifies they are gone.
    """
    agent_name = f"test-snap-lifecycle-{get_short_random_string()}"

    _create_modal_agent(agent_name, temp_source_dir, modal_subprocess_env, "sleep 400006")
    try:
        # Destroy all
        with log_span("Destroying snapshots for Modal agent", agent_name=agent_name):
            destroy_result = run_mngr_against_modal(
                "snapshot",
                "destroy",
                agent_name,
                "--all-snapshots",
                "--force",
                "--format",
                "json",
                modal_subprocess_env=modal_subprocess_env,
                timeout=120,
            )
            assert destroy_result.returncode == 0
            destroy_data = _extract_json(destroy_result.stdout)
            assert destroy_data["count"] >= 2

        # Verify none remain
        with log_span("Verifying no snapshots remain for Modal agent", agent_name=agent_name):
            list_after = run_mngr_against_modal(
                "snapshot",
                "list",
                agent_name,
                "--format",
                "json",
                modal_subprocess_env=modal_subprocess_env,
                timeout=120,
            )
            assert list_after.returncode == 0
            assert _extract_json(list_after.stdout)["count"] == 0

    finally:
        with log_span("Destroying Modal agent after snapshot test", agent_name=agent_name):
            _destroy_modal_agent(agent_name, modal_subprocess_env)

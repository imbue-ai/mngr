"""
Modal function to snapshot and shut down a host.

This function is deployed as a Modal web endpoint and can be invoked to:
1. Snapshot a running Modal sandbox
2. Store the snapshot ID in the host's volume record
3. Terminate the sandbox

All code is self-contained in this file - no imports from the mng codebase.

Required environment variable (must be set when deploying):
- MNG_MODAL_APP_NAME: The Modal app name (e.g., "mng-<user_id>-modal")

(note: do NOT remove this module docstring--this is a fully standalone script)
"""

import json
import logging
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

# DO NOT add any more 3rd-party imports here!
import modal

# NO SERIOUSLY, DO NOT ADD ANY IMPORTS HERE, whether 3rd-party, or from our own code
# The reason is that this needs to run in many different places and with minimal dependencies beyond modal
# Put the imports into the snapshot_and_shutdown function instead (yes, this violates our normal rules about inline imports, but that's ok for this particular file)


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing."""


if modal.is_local():
    APP_NAME = os.environ.get("MNG_MODAL_APP_NAME")
    if APP_NAME is None:
        raise ConfigurationError("MNG_MODAL_APP_NAME environment variable must be set")
    APP_BUILD_PATH = os.environ.get("MNG_MODAL_APP_BUILD_PATH")
    if APP_BUILD_PATH is None:
        raise ConfigurationError("MNG_MODAL_APP_BUILD_PATH environment variable must be set")
    output_app_name_file = Path(APP_BUILD_PATH) / "app_name"
    output_app_name_file.parent.mkdir(parents=True, exist_ok=True)
    output_app_name_file.write_text(APP_NAME)
    (Path(APP_BUILD_PATH) / "app_build_path").write_text(APP_BUILD_PATH)
else:
    APP_NAME = Path("/deployment/app_name").read_text().strip()
    APP_BUILD_PATH = Path("/deployment/app_build_path").read_text().strip()

image = (
    modal.Image.debian_slim()
    .uv_pip_install("fastapi[standard]")
    .add_local_dir(str(Path(APP_BUILD_PATH)), "/deployment/", copy=True)
)

app = modal.App(name=APP_NAME, image=image)
VOLUME_NAME = f"{APP_NAME}-state"
volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)


def _read_host_record(host_id: str) -> dict[str, Any] | None:
    """Read a host record from the volume.

    Reloads the volume first to ensure we have the latest data,
    as Modal volumes aren't automatically refreshed between function calls.
    """
    # Reload the volume to get latest data (changes made externally)
    volume.reload()
    path = f"/vol/hosts/{host_id}.json"
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _write_host_record(host_record: dict[str, Any]) -> None:
    """Write a host record to the volume."""
    host_id = host_record["certified_host_data"]["host_id"]
    path = f"/vol/hosts/{host_id}.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(host_record, f, indent=2)
    volume.commit()


def _write_agent_records(host_id: str, agents: list[dict[str, Any]]) -> None:
    """Write agent records to the volume.

    Each agent is stored at /vol/hosts/{host_id}/{agent_id}.json so that
    stopped hosts can still show their agents in mng list.
    """
    if not agents:
        return

    # Create the host directory if it doesn't exist
    host_dir = f"/vol/hosts/{host_id}"
    os.makedirs(host_dir, exist_ok=True)

    # Write each agent's data
    for agent in agents:
        agent_id = agent.get("id")
        if agent_id:
            agent_path = f"{host_dir}/{agent_id}.json"
            with open(agent_path, "w") as f:
                json.dump(agent, f, indent=2)

    volume.commit()


@app.function(volumes={"/vol": volume})
@modal.fastapi_endpoint(method="POST", docs=True)
def snapshot_and_shutdown(request_body: dict[str, Any]) -> dict[str, Any]:
    """Snapshot a Modal sandbox and shut it down.

    Request body should contain sandbox_id (Modal sandbox object ID) and
    host_id (mng host ID). Optionally accepts snapshot_name, agents
    (list of agent data to persist to the volume), and stop_reason
    ('PAUSED' for idle shutdown, 'STOPPED' for user-requested stop).

    Must put all imports here, not at the top level--otherwise this fails remotely when deploying
    """
    from fastapi import HTTPException

    logger = logging.getLogger("snapshot_and_shutdown")
    were_snapshots_missing = False

    try:
        try:
            sandbox_id = request_body.get("sandbox_id")
            host_id = request_body.get("host_id")
            snapshot_name = request_body.get("snapshot_name")
            agents = request_body.get("agents", [])
            stop_reason = request_body.get("stop_reason", "PAUSED")

            if not sandbox_id:
                raise HTTPException(status_code=400, detail="sandbox_id is required")
            if not host_id:
                raise HTTPException(status_code=400, detail="host_id is required")

            # Verify host record exists BEFORE creating snapshot to avoid orphaned images
            host_record = _read_host_record(host_id)
            host_record_for_debugging = str(host_record)
            if host_record is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Host record not found for host_id: {host_id}",
                )

            # Get the sandbox by ID
            sandbox = modal.Sandbox.from_id(sandbox_id)

            # Create the filesystem snapshot
            # note that this can sometimes take quite a while kind of randomly, and it's sorta Modal's fault
            # I've observed > 60-second delays even without tons of files (or large files)
            # when there are lots of files (or the files are large), it can take even longer
            # this is just a best-effort compromise between waiting forever and giving up too early - in practice, if it takes more than 5 minutes, something has probably gone pretty wrong
            modal_image = sandbox.snapshot_filesystem(timeout=300)
            # Use the Modal image ID directly as the snapshot ID
            snapshot_id = modal_image.object_id
            created_at = datetime.now(timezone.utc).isoformat()

            if snapshot_name is None:
                short_id = snapshot_id[-8:]
                snapshot_name = f"snapshot-{short_id}"

            # Add the new snapshot to the certified_host_data (id is the Modal image ID)
            new_snapshot = {
                "id": snapshot_id,
                "name": snapshot_name,
                "created_at": created_at,
            }

            certified_data = host_record.get("certified_host_data", {})
            if "snapshots" not in certified_data:
                certified_data["snapshots"] = []
                were_snapshots_missing = True
            certified_data["snapshots"].append(new_snapshot)

            # Record the stop reason (PAUSED for idle, STOPPED for user-requested)
            certified_data["stop_reason"] = stop_reason
            certified_data["updated_at"] = datetime.now(timezone.utc).isoformat()
            host_record["certified_host_data"] = certified_data

            # Write updated host record
            _write_host_record(host_record)

            # Write agent records so they appear in mng list for stopped hosts
            _write_agent_records(host_id, agents)

            # Terminate the sandbox
            sandbox.terminate()

            if were_snapshots_missing:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"Host record was missing 'snapshots' field. Original data was: {host_record_for_debugging}"
                    ),
                )

            return {
                "success": True,
                "snapshot_id": snapshot_id,
                "snapshot_name": snapshot_name,
            }

        # note: do NOT change this--this is just here temporarily while we are debugging intermittent failures during snapshotting
        except BaseException as e:
            logger.error("Error in snapshot_and_shutdown: " + str(e), exc_info=True)
            raise

    except HTTPException:
        raise
    except modal.exception.NotFoundError as e:
        raise HTTPException(status_code=404, detail=f"Sandbox not found: {e}") from None
    except modal.exception.InvalidError as e:
        # Invalid sandbox ID format also counts as "not found"
        raise HTTPException(status_code=404, detail=f"Invalid sandbox ID: {e}") from None
    except modal.exception.Error as e:
        raise HTTPException(status_code=500, detail=f"Modal error: {e}") from None

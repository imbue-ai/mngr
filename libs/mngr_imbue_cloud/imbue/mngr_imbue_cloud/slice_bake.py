import json
import shlex
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError

# Agent name baked onto every slice (and VPS) pool host. Adoption preserves the
# bake's agent name verbatim, so it must match what the user's
# ``mngr create system-services@<host>.imbue_cloud_<slug>`` lease uses.
BAKED_SERVICES_AGENT_NAME: Final[str] = "system-services"

# Provider instance name the slice bake targets on the box (resolved from the
# backend default; box-specific config supplied via ``-S`` overrides).
SLICE_PROVIDER_INSTANCE: Final[str] = "imbue_cloud_slice"

# Path (inside the slice's container) of the FCT bootstrap's initial-chat
# sentinel. Removing it -- after destroying the bootstrap-created chat agent --
# makes the user's first lease+start re-create the chat agent under the user's
# own workspace name. ``/code`` is symlinked to ``/mngr/code`` in the FCT image.
INITIAL_CHAT_SENTINEL_PATH: Final[str] = "/code/runtime/initial_chat_created"


@pure
def build_slice_bake_remote_command(
    *,
    fct_dir: str,
    mngr_bin: str,
    host_name: str,
    attributes_json: str,
    box_public_address: str,
    pool_public_key: str,
    region: str,
    slice_vcpus: int,
    slice_memory_mib: int,
    slice_disk_gib: int,
    port_range_start: int,
    port_range_end: int,
) -> str:
    """Render the bash run on the box to bake one slice and print create JSON to stdout.

    Runs the monorepo's mngr (``mngr_bin``) with the FCT workspace as cwd so it
    picks up the shared ``ovh`` bake template + Dockerfile, but targets the
    ``imbue_cloud_slice`` provider via the address. ``box_public_address`` and
    the pool management key are injected as per-invocation ``-S`` config
    overrides (no FCT provider block needed). ``--format json`` makes the agent
    id / host id / both forwarded ports available on stdout.
    """
    address = f"{BAKED_SERVICES_AGENT_NAME}@{host_name}.{SLICE_PROVIDER_INSTANCE}"
    create_args = [
        "create",
        address,
        "--new-host",
        "--no-connect",
        "--idle-mode",
        "disabled",
        "--template",
        "main",
        "--template",
        "ovh",
        "--format",
        "json",
        "-S",
        f"providers.{SLICE_PROVIDER_INSTANCE}.box_public_address={box_public_address}",
        "-S",
        f"providers.{SLICE_PROVIDER_INSTANCE}.pool_authorized_public_key={pool_public_key}",
        "-S",
        f"providers.{SLICE_PROVIDER_INSTANCE}.slice_region={region}",
        # The carving knobs are computed per box (no provider defaults) so the
        # leased host's actual cores/RAM/disk match its advertised attributes.
        "-S",
        f"providers.{SLICE_PROVIDER_INSTANCE}.slice_vcpus={slice_vcpus}",
        "-S",
        f"providers.{SLICE_PROVIDER_INSTANCE}.slice_memory_mib={slice_memory_mib}",
        "-S",
        f"providers.{SLICE_PROVIDER_INSTANCE}.slice_disk_gib={slice_disk_gib}",
        "-S",
        f"providers.{SLICE_PROVIDER_INSTANCE}.slice_port_range_start={port_range_start}",
        "-S",
        f"providers.{SLICE_PROVIDER_INSTANCE}.slice_port_range_end={port_range_end}",
        "--label",
        f"workspace={BAKED_SERVICES_AGENT_NAME}",
        "--label",
        "user_created=true",
        "--label",
        "is_primary=true",
        "--label",
        f"pool_attributes={attributes_json}",
        "--host-env",
        "MNGR_HOST_DIR=/mngr",
    ]
    quoted_args = " ".join(shlex.quote(arg) for arg in create_args)
    # ``MNGR_PROJECT_CONFIG_DIR`` points mngr at the FCT workspace's ``.mngr``
    # settings (templates, agent types) directly: the synced workspace has no
    # ``.git`` (excluded from the rsync), so mngr's default git-root config
    # discovery would otherwise find nothing and the templates would be missing.
    project_config_dir = shlex.quote(f"{fct_dir}/.mngr")
    # ``mngr_bin`` is left unquoted so a leading ``$HOME`` expands in the remote
    # shell; it is a trusted constant, not user input.
    return (
        f"cd {shlex.quote(fct_dir)} && "
        f"MNGR_PROJECT_CONFIG_DIR={project_config_dir} PATH=$HOME/.local/bin:$PATH "
        f"{mngr_bin} {quoted_args}"
    )


@pure
def build_chat_teardown_container_command(host_name: str) -> str:
    """Render the command (run as root inside the slice's container) that removes the bake's chat agent.

    Destroys the bootstrap-created chat agent (named after the bake host) and
    deletes the initial-chat sentinel so the user's first lease re-creates the
    chat agent under their own name. Best-effort on the destroy (the agent may
    not exist), but the sentinel removal must succeed. Wrapped in a login shell
    so mngr/uv are on PATH inside the container.
    """
    inner = (
        f"cd /mngr/code && (uv run mngr destroy {shlex.quote(host_name)} --force || true) && "
        f"rm -f {shlex.quote(INITIAL_CHAT_SENTINEL_PATH)}"
    )
    return f"bash -lc {shlex.quote(inner)}"


@pure
def build_wait_for_sentinel_container_command(timeout_seconds: int) -> str:
    """Render the command (run as root inside the slice's container) that waits for the initial-chat sentinel.

    Blocks (in the remote shell, not the caller) until the FCT bootstrap writes
    the sentinel or ``timeout_seconds`` elapses. Exit 0 once present, non-zero on
    timeout (the bootstrap may never create a chat agent -- e.g. inference creds
    absent -- in which case there is nothing to tear down).
    """
    inner = f"until test -f {shlex.quote(INITIAL_CHAT_SENTINEL_PATH)}; do sleep 5; done"
    return f"timeout {int(timeout_seconds)} bash -lc {shlex.quote(inner)}"


@pure
def parse_create_json_from_output(stdout: str) -> dict[str, Any]:
    """Return the ``mngr create --format json`` object from the bake's stdout.

    ``--format json`` writes exactly one JSON object to stdout (build logs go to
    stderr), so the last ``{...}`` line is the result. A malformed candidate
    re-raises as ``BareMetalProvisioningError`` (never silently swallowed).
    """
    candidates = [
        line.strip() for line in stdout.splitlines() if line.strip().startswith("{") and line.strip().endswith("}")
    ]
    if not candidates:
        raise BareMetalProvisioningError(f"no create --format json object found in bake output: {stdout[-500:]!r}")
    try:
        parsed = json.loads(candidates[-1])
    except json.JSONDecodeError as exc:
        raise BareMetalProvisioningError(
            f"create --format json output was not valid JSON: {candidates[-1]!r}"
        ) from exc
    if not isinstance(parsed, dict) or "host_id" not in parsed:
        raise BareMetalProvisioningError(f"create --format json output missing host_id: {parsed!r}")
    return parsed

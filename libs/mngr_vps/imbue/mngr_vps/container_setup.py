import os
import re
import shlex
import shutil
import tempfile
import time
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from importlib import resources as importlib_resources
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyExceptionGroup
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.logging import log_span
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import DockerBuilder
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import LogLevel
from imbue.mngr.providers.ssh_host_setup import build_add_authorized_keys_command
from imbue.mngr.providers.ssh_host_setup import build_add_known_hosts_command
from imbue.mngr.providers.ssh_host_setup import build_check_and_install_packages_command
from imbue.mngr.providers.ssh_host_setup import build_configure_ssh_command
from imbue.mngr.providers.ssh_host_setup import build_self_healing_host_entrypoint_command
from imbue.mngr.providers.ssh_host_setup import build_start_sshd_command
from imbue.mngr.utils.git_utils import rsync_worktree_over_clone
from imbue.mngr_vps.errors import ContainerSetupError
from imbue.mngr_vps.errors import VpsProvisioningError
from imbue.mngr_vps.host_store import AGENTS_SUBPATH

# Label constants (same scheme as Docker provider)
LABEL_PREFIX: Final[str] = "com.imbue.mngr."
LABEL_PROVIDER: Final[str] = f"{LABEL_PREFIX}provider"
LABEL_HOST_ID: Final[str] = f"{LABEL_PREFIX}host-id"
LABEL_HOST_NAME: Final[str] = f"{LABEL_PREFIX}host-name"
LABEL_TAGS: Final[str] = f"{LABEL_PREFIX}tags"

# Default image when no user customization
DEFAULT_IMAGE: Final[str] = "debian:bookworm-slim"

# Path inside the agent container where the unified host volume is mounted.
# The container sees three top-level entries under this mount: host_state.json,
# agents/, and host_dir/. The container's mngr host_dir symlink resolves into
# the host_dir/ subdirectory so all of the agent's writes end up on the volume.
HOST_VOLUME_MOUNT_PATH: Final[str] = "/mngr-vol"

# Subdirectory inside the unified volume that backs the agent's mngr host_dir.
HOST_DIR_SUBPATH: Final[str] = "host_dir"

# Shell command for the agent container's PID 1. Self-heals sshd on every
# (re)start once mngr has provisioned a host key, so the container is reachable
# again after an out-of-band restart (VM reboot, `docker restart`) without
# waiting for `mngr start`. Also traps SIGTERM and stays alive until SIGTERM
# arrives so `docker stop` (idle timeout, manual stop) exits cleanly.
CONTAINER_ENTRYPOINT_CMD: Final[str] = build_self_healing_host_entrypoint_command()

# In-container path the host_backup service writes / reads snapshot
# request and result JSON to. Backed by the per-host docker volume
# ``mngr-snapshot-trigger-<host_id_hex>`` which is also bind-mounted on the
# outer at ``/var/lib/mngr-snapshot/`` so the outer-side snapshot helper
# can watch it.
SNAPSHOT_TRIGGER_MOUNT_PATH: Final[str] = "/mngr-snapshot"

# In-container path that exposes the outer's <btrfs-mount>/snapshots/
# directory read-only. host_backup reads ``<this>/current`` after the
# outer helper produces a snapshot there.
SNAPSHOT_READ_MOUNT_PATH: Final[str] = "/mngr-snapshots"

# Outer-host paths for the snapshot-helper protocol files. Must match
# what ``resources/snapshot_helper.sh`` watches.
OUTER_SNAPSHOT_TRIGGER_DIR: Final[Path] = Path("/var/lib/mngr-snapshot")

# Outer-host install paths for the helper script + systemd unit + env file.
OUTER_HELPER_SCRIPT_PATH: Final[Path] = Path("/usr/local/sbin/snapshot_helper.sh")
OUTER_HELPER_SERVICE_PATH: Final[Path] = Path("/etc/systemd/system/snapshot_helper.service")
OUTER_HELPER_ENV_PATH: Final[Path] = Path("/etc/mngr-snapshot-helper.env")
OUTER_HELPER_SERVICE_NAME: Final[str] = "snapshot_helper.service"

# Resolve the depot CLI at run time, preferring a copy already on PATH so an
# existing install is respected. depot.dev's installer drops the CLI at
# $HOME/.depot/bin, which is not on a non-interactive shell's PATH, so we fall
# back to that absolute location and install there only when nothing is found.
# The remote shell captures the result in $DEPOT_BIN (so $HOME expands to the
# connecting user's home), and the same value drives both the install check and
# the invocation below.
_DEPOT_RESOLVE_AND_INSTALL: Final[str] = (
    'DEPOT_BIN="$(command -v depot || echo "$HOME/.depot/bin/depot")"; '
    'test -x "$DEPOT_BIN" || curl -fsSL https://depot.dev/install-cli.sh | sh'
)
_DEPOT_BIN: Final[str] = '"$DEPOT_BIN"'

# Env-var assignments whose values are secrets and must be redacted before any
# remote command string ends up in logs or exception messages.
_SECRET_ENV_VARS: Final[tuple[str, ...]] = ("DEPOT_TOKEN",)
_SECRET_ENV_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(" + "|".join(_SECRET_ENV_VARS) + r")=(?:'[^']*'|\S+)")

_DEPOT_TOKEN_REQUIRED_MESSAGE: Final[str] = (
    "builder=DEPOT requires DEPOT_TOKEN in the agent's environment. "
    "Set DEPOT_TOKEN (and DEPOT_PROJECT_ID if no depot.json is on the VPS), "
    "or set builder=DOCKER."
)


def ensure_depot_token_available(builder: DockerBuilder) -> None:
    """Raise ``MngrError`` if ``builder`` is DEPOT but DEPOT_TOKEN is absent.

    Used both as a create-host preflight -- so a missing token fails fast,
    before a (billable) VPS is provisioned and cloud-init runs, rather than at
    the build step -- and at build time as the last line of defense. A DEPOT
    build is the only thing that needs the token; callers gate this on whether
    a build will actually happen (a plain image pull does not need it).
    """
    if builder is DockerBuilder.DEPOT and not os.environ.get("DEPOT_TOKEN"):
        raise MngrError(_DEPOT_TOKEN_REQUIRED_MESSAGE)


# Absolute path on the outer where rsync stashes partial files between
# attempts. Lives outside the build context (``/tmp/mngr-build-<id>/``) so
# partial-transfer state never gets included in the docker build context
# or copied back to the local repo. Persists across retries so subsequent
# attempts can resume rather than re-uploading completed bytes.
_RSYNC_PARTIAL_DIR_REMOTE: Final[str] = "/tmp/mngr-rsync-partial"

# Backoff between attempts (entry N is the wait *before* attempt N+1). There is
# one entry per retry gap; the total attempt count is derived from its length so
# the two can never drift (the loop indexes this tuple on every non-last attempt).
_UPLOAD_RETRY_BACKOFF_SECONDS: Final[tuple[float, ...]] = (5.0, 15.0)
# How many times to try a failed rsync upload before giving up (initial attempt
# plus one retry per backoff entry).
_UPLOAD_MAX_ATTEMPTS: Final[int] = len(_UPLOAD_RETRY_BACKOFF_SECONDS) + 1

# Substrings in rsync stderr that indicate a transient connection-class
# failure (broken pipe, dropped TCP, fresh-VPS networking flap). Rsync's
# own catch-all exit 255 with these messages is what fresh Vultr VPSes
# produce in the first 30-60s of life. Other rsync errors (permission,
# protocol mismatch, vanished source) are non-transient and we fail fast.
_RETRYABLE_RSYNC_PATTERNS: Final[tuple[str, ...]] = (
    "Broken pipe",
    "Connection reset by peer",
    "Connection refused",
    "Connection timed out",
    "client_loop",
    "ssh: connect to host",
    "kex_exchange_identification",
    "Network is unreachable",
)


def remove_host_from_known_hosts(known_hosts_path: Path, hostname: str, port: int) -> None:
    """Remove a host entry from the known_hosts file."""
    if not known_hosts_path.exists():
        return
    host_pattern = hostname if port == 22 else f"[{hostname}]:{port}"
    lines = known_hosts_path.read_text().splitlines(keepends=True)
    filtered = [line for line in lines if not line.startswith(f"{host_pattern} ")]
    known_hosts_path.write_text("".join(filtered))


def redact_secret_env(remote_command: str) -> str:
    """Return remote_command with values of known-secret env-var assignments replaced."""
    return _SECRET_ENV_PATTERN.sub(r"\1=<redacted>", remote_command)


def is_retryable_rsync_error(stderr: str) -> bool:
    """Return True iff stderr looks like a connection-class rsync failure."""
    return any(pattern in stderr for pattern in _RETRYABLE_RSYNC_PATTERNS)


def host_volume_name_for(host_id: HostId) -> str:
    """Return the unified Docker volume name for a host."""
    return f"mngr-host-vol-{host_id.get_uuid().hex}"


def snapshot_trigger_volume_name_for(host_id: HostId) -> str:
    """Return the per-host snapshot-trigger Docker volume name."""
    return f"mngr-snapshot-trigger-{host_id.get_uuid().hex}"


# The single rule mapping a container's state string (``.State.Status`` or a
# parsed listing's ``CONTAINER_STATE=`` value) to "is the placement running".
# Both the cheap inspect probe and the listing-derived state route through this,
# so the two paths can never disagree on what "running" means.
RUNNING_CONTAINER_STATE: Final[str] = "running"


def is_running_container_state(state: str | None) -> bool:
    """Whether a container state string denotes a running container."""
    return state == RUNNING_CONTAINER_STATE


def docker_inspect_running(outer: OuterHostInterface, container_name: str) -> bool:
    """Return True iff a container with the given name is running on outer.

    Reads ``.State.Status`` and applies the same running-state rule the listing
    path uses (``is_running_container_state``), so the cheap probe and the
    listing-derived state can never disagree.
    """
    result = outer.execute_idempotent_command(
        f"docker inspect --format '{{{{.State.Status}}}}' {shlex.quote(container_name)}"
    )
    if not result.success:
        return False
    return is_running_container_state(result.stdout.strip())


def check_file_exists_on_outer(outer: OuterHostInterface, path: Path) -> bool:
    """Return True iff a file exists on outer."""
    result = outer.execute_idempotent_command(f"test -f {shlex.quote(str(path))}", timeout_seconds=10.0)
    return result.success


def check_directory_exists_on_outer(outer: OuterHostInterface, path: Path) -> bool:
    """Return True iff a directory exists on outer."""
    result = outer.execute_idempotent_command(f"test -d {shlex.quote(str(path))}", timeout_seconds=10.0)
    return result.success


def is_btrfs_progs_installed_on_outer(outer: OuterHostInterface) -> bool:
    """Return True iff ``mkfs.btrfs`` is on the outer's PATH (i.e. btrfs-progs is installed)."""
    result = outer.execute_idempotent_command(
        "command -v mkfs.btrfs >/dev/null 2>&1",
        timeout_seconds=10.0,
    )
    return result.success


def _run_provisioning_step(
    outer: OuterHostInterface,
    command: str,
    *,
    error_prefix: str,
    timeout_seconds: float,
) -> str:
    """Run one provisioning command on the outer; raise ``VpsProvisioningError`` on failure, else return stdout.

    Collapses the repeated "execute, then raise with the command's stderr on
    failure" shape used by the outer btrfs/dir/systemd provisioning steps.
    ``error_prefix`` is the human-readable description of the step ("Failed to
    create outer dir X"); the failure message appends ``: stderr=<stderr>``.
    """
    result = outer.execute_idempotent_command(command, timeout_seconds=timeout_seconds)
    if not result.success:
        raise VpsProvisioningError(f"{error_prefix}: stderr={result.stderr.strip()!r}")
    return result.stdout


def install_btrfs_progs_on_outer(outer: OuterHostInterface) -> None:
    """Install btrfs-progs on the outer via apt-get; raise VpsProvisioningError on failure."""
    command = (
        "DEBIAN_FRONTEND=noninteractive apt-get update && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y btrfs-progs"
    )
    _run_provisioning_step(
        outer, command, error_prefix="Failed to install btrfs-progs on outer", timeout_seconds=300.0
    )


def get_outer_free_disk_gb(outer: OuterHostInterface, path: Path) -> int:
    """Return free space at ``path`` on the outer, floor-divided to whole GiB.

    Reads the available-bytes count with ``df --output=avail -B 1`` and does
    the GiB conversion (`// (1024 ** 3)`) in Python so the result is always
    less-than-or-equal-to the true free space. ``df``'s own ``-B 1G`` form
    rounds up, which would let the caller compute a loop-file size up to
    ~1 GiB larger than actually available; floor-dividing here keeps the
    caller's allocation math conservative.

    Runs the ``df | tail`` pipeline under ``bash -c 'set -o pipefail; ...'``
    so a failing ``df`` (e.g. invalid path, permission issue) is surfaced
    through the "failed to read" branch rather than being masked by
    ``tail``'s zero exit code and falling through to the "could not parse"
    branch.
    """
    pipeline = f"set -o pipefail; df --output=avail -B 1 {shlex.quote(str(path))} | tail -n 1"
    result = outer.execute_idempotent_command(
        f"bash -c {shlex.quote(pipeline)}",
        timeout_seconds=10.0,
    )
    if not result.success:
        raise VpsProvisioningError(
            f"Failed to read free disk space at {path} on outer: stderr={result.stderr.strip()!r}"
        )
    raw = result.stdout.strip()
    try:
        free_bytes = int(raw)
    except ValueError as e:
        raise VpsProvisioningError(f"Could not parse free-space output {raw!r} from df at {path} on outer") from e
    return free_bytes // (1024**3)


def is_path_mounted_on_outer(outer: OuterHostInterface, path: Path) -> bool:
    """Return True iff ``path`` is currently a mountpoint on the outer."""
    result = outer.execute_idempotent_command(
        f"mountpoint -q {shlex.quote(str(path))}",
        timeout_seconds=10.0,
    )
    return result.success


def is_fstab_entry_present_on_outer(outer: OuterHostInterface, loop_file_path: Path) -> bool:
    """Return True iff ``/etc/fstab`` already references ``loop_file_path`` at column 1."""
    pattern = f"^{re.escape(str(loop_file_path))}[[:space:]]"
    result = outer.execute_idempotent_command(
        f"grep -qE {shlex.quote(pattern)} /etc/fstab",
        timeout_seconds=10.0,
    )
    return result.success


def _summarize_concurrency_exception_group(group: ConcurrencyExceptionGroup) -> str:
    """Extract the most useful single message from a ConcurrencyExceptionGroup."""
    if group.main_exception is not None:
        return str(group.main_exception)
    if len(group.exceptions) == 1:
        return str(group.exceptions[0])
    return str(group)


@contextmanager
def translate_outer_concurrency_errors(operation_description: str) -> Iterator[None]:
    """Re-raise raw concurrency-group failures as ContainerSetupError (a MngrError).

    The outer-host docker/rsync/snapshot helpers run their work inside
    ConcurrencyGroups, so failures surface as ConcurrencyExceptionGroup or
    ProcessError/ProcessTimeoutError -- neither of which is a MngrError. Wrapping
    them here lets provider-level ``except MngrError`` cleanup clauses catch them
    (and tear down the half-built host) instead of letting them escape unhandled.
    """
    try:
        yield
    except ConcurrencyExceptionGroup as e:
        raise ContainerSetupError(
            f"Failed to {operation_description}: {_summarize_concurrency_exception_group(e)}"
        ) from e
    except ProcessError as e:
        raise ContainerSetupError(f"Failed to {operation_description}: {e}") from e


def exec_in_container(
    outer: OuterHostInterface,
    container_name: str,
    command: str,
    timeout_seconds: float = 300.0,
) -> str:
    """Execute a shell command inside a running container on outer. Returns stdout.

    Forces ``--workdir /`` so the exec succeeds regardless of whether the
    image's declared ``WORKDIR`` exists at exec time. Mngr's first
    container exec is its own sshd setup -- which runs *before* any
    ``post_host_create_command`` hook -- so a WORKDIR like ``/mngr/code/``
    that the image expects to be populated by a first-boot seed step
    won't be on disk yet. None of mngr's automated setup commands depend
    on the image's WORKDIR; they all use absolute paths.

    Raises MngrError if the command exits non-zero.
    """
    remote = f"docker exec --workdir / {shlex.quote(container_name)} sh -c {shlex.quote(command)}"
    result = outer.execute_idempotent_command(remote, timeout_seconds=timeout_seconds)
    if not result.success:
        raise MngrError(f"docker exec in {container_name} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def run_docker(
    outer: OuterHostInterface,
    docker_args: Sequence[str],
    timeout_seconds: float = 60.0,
) -> str:
    """Run a docker subcommand on outer and return stdout.

    Raises MngrError if the command exits non-zero.
    """
    remote = "docker " + " ".join(shlex.quote(a) for a in docker_args)
    result = outer.execute_idempotent_command(remote, timeout_seconds=timeout_seconds)
    if not result.success:
        raise MngrError(f"docker {' '.join(docker_args[:2])} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def commit_container(outer: OuterHostInterface, container_name: str, image_tag: str) -> str:
    """Commit a container to an image. Returns the image ID."""
    return run_docker(outer, ["commit", container_name, image_tag]).strip()


def stop_container(outer: OuterHostInterface, container_name: str, timeout_seconds: int = 10) -> None:
    """Stop a running container."""
    run_docker(outer, ["stop", "-t", str(timeout_seconds), container_name])


# Hard timeout for the container start: a plain `docker start` is quick, but
# allow generous headroom for a slow runsc sandbox bring-up.
_START_CONTAINER_TIMEOUT_SECONDS: Final[float] = 120.0


def start_container(outer: OuterHostInterface, container_name: str) -> None:
    """Start a stopped container. Raises ``MngrError`` if it fails to start.

    Shared by every docker-based provider (mngr_vps / ovh / lima), all of which
    run the agent container under runsc with ``--overlay2=none`` (so the writable
    layer persists across restart and there is no stale self-overlay filestore to
    recover from).
    """
    run_docker(outer, ["start", container_name], timeout_seconds=_START_CONTAINER_TIMEOUT_SECONDS)


def remove_container(
    outer: OuterHostInterface, container_name: str, force: bool = False, tolerate_missing: bool = False
) -> None:
    """Remove a container. If force=True, kill running containers first.

    If tolerate_missing=True, an already-absent container is a no-op: ``docker rm``
    reports "No such container" (a stable, well-known docker string), which we treat
    as success since the postcondition -- the container is gone -- already holds. Any
    other docker failure still raises, so callers can treat a raised error as a real
    failure (a container that exists but could not be removed).
    """
    args: list[str] = ["rm"]
    if force:
        args.append("-f")
    args.append(container_name)
    try:
        run_docker(outer, args)
    except MngrError as e:
        if tolerate_missing and "no such container" in str(e).lower():
            logger.trace("Container {} already gone -- nothing to remove", container_name)
            return
        raise


def remove_volume(outer: OuterHostInterface, volume_name: str) -> None:
    """Remove a Docker named volume (force)."""
    run_docker(outer, ["volume", "rm", "-f", volume_name])


def load_resource_text(resource_name: str) -> str:
    """Read a bundled package resource as text (e.g. snapshot_helper.sh)."""
    return importlib_resources.files("imbue.mngr_vps.resources").joinpath(resource_name).read_text()


def provision_snapshot_helper_on_outer(
    outer: OuterHostInterface,
    cg: ConcurrencyGroup,
    *,
    host_id: HostId,
    btrfs_mount_path: Path,
    subvolume_path: Path,
    trigger_volume_name: str,
) -> None:
    """Install the snapshot helper systemd unit + the trigger docker volume on the outer.

    Two-phase pipeline (parallelized within each phase via ``cg`` so
    independent SSH operations don't serialize, since each one round-trips
    over the WAN):

    1. Phase A -- 5 parallel ops: write the 3 helper files + ``mkdir -p``
       the trigger dir + ``mkdir -p`` the snapshots dir.
    2. Phase B -- 2 parallel ops: ``systemctl daemon-reload && systemctl
       enable --now snapshot_helper.service`` (chained into one SSH RTT)
       and ``docker volume create`` for the trigger volume (lazy bind, so
       safe to run in parallel with the systemctl step now that the trigger
       dir exists).

    Idempotent: rewriting the script / unit / env file is harmless; the
    ``systemctl enable --now`` re-runs are no-ops when the unit is already
    enabled and active; the ``docker volume create`` is no-op-with-warning
    when the volume already exists.

    Assumes ``inotify-tools`` and ``jq`` are already installed. The cloud-init
    and SSH host-setup paths install both via the shared ``host_setup``
    base-packages step; the slice path installs them in its lima VM provisioning
    (``mngr_imbue_cloud.slices.lima_slice``: ``jq`` via the base lima script,
    ``inotify-tools`` via its own provision step).
    """
    helper_script = load_resource_text("snapshot_helper.sh")
    helper_service = load_resource_text("snapshot_helper.service")
    helper_env = (
        f"MNGR_BTRFS_MOUNT_PATH={btrfs_mount_path}\n"
        f"MNGR_HOST_SUBVOLUME={subvolume_path}\n"
        f"MNGR_TRIGGER_DIR={OUTER_SNAPSHOT_TRIGGER_DIR}\n"
    )
    snapshots_dir = btrfs_mount_path / "snapshots"

    with (
        translate_outer_concurrency_errors("provision the snapshot helper on the host"),
        log_span("Provisioning snapshot helper on outer (host_id={})", host_id),
    ):
        with ConcurrencyGroupExecutor(
            parent_cg=cg,
            name="snapshot_helper_phase_a",
            max_workers=5,
        ) as phase_a:
            phase_a_futures = [
                phase_a.submit(
                    write_outer_file,
                    outer,
                    path=OUTER_HELPER_SCRIPT_PATH,
                    content=helper_script,
                    mode="0755",
                ),
                phase_a.submit(
                    write_outer_file,
                    outer,
                    path=OUTER_HELPER_SERVICE_PATH,
                    content=helper_service,
                    mode="0644",
                ),
                phase_a.submit(
                    write_outer_file,
                    outer,
                    path=OUTER_HELPER_ENV_PATH,
                    content=helper_env,
                    mode="0644",
                ),
                phase_a.submit(ensure_outer_dir, outer, OUTER_SNAPSHOT_TRIGGER_DIR),
                phase_a.submit(ensure_outer_dir, outer, snapshots_dir),
            ]
        for future in phase_a_futures:
            future.result()

        with ConcurrencyGroupExecutor(
            parent_cg=cg,
            name="snapshot_helper_phase_b",
            max_workers=2,
        ) as phase_b:
            phase_b_futures = [
                phase_b.submit(enable_snapshot_helper_unit, outer),
                phase_b.submit(
                    create_bind_volume_on_outer,
                    outer,
                    volume_name=trigger_volume_name,
                    device_path=OUTER_SNAPSHOT_TRIGGER_DIR,
                ),
            ]
        for future in phase_b_futures:
            future.result()


def enable_snapshot_helper_unit(outer: OuterHostInterface) -> None:
    """`systemctl daemon-reload && systemctl enable --now snapshot_helper.service`.

    Chained into a single SSH round-trip so this step costs one RTT
    instead of two. The combined command is idempotent: daemon-reload
    re-reads unit files (no effect if nothing changed) and ``enable --now``
    is a no-op when the unit is already enabled + active.
    """
    command = f"systemctl daemon-reload && systemctl enable --now {OUTER_HELPER_SERVICE_NAME}"
    _run_provisioning_step(outer, command, error_prefix=f"{command} failed", timeout_seconds=20.0)


def write_outer_file(
    outer: OuterHostInterface,
    *,
    path: Path,
    content: str,
    mode: str,
) -> None:
    """Atomically write `content` to `path` on the outer with the given chmod."""
    ensure_outer_dir(outer, path.parent)
    outer.write_file(path, content.encode("utf-8"), mode=mode, is_atomic=True)


def ensure_outer_dir(outer: OuterHostInterface, path: Path) -> None:
    """`mkdir -p` on the outer; raises VpsProvisioningError on failure."""
    _run_provisioning_step(
        outer,
        f"mkdir -p {shlex.quote(str(path))}",
        error_prefix=f"Failed to create outer dir {path}",
        timeout_seconds=10.0,
    )


def create_bind_volume_on_outer(
    outer: OuterHostInterface,
    *,
    volume_name: str,
    device_path: Path,
) -> None:
    """Create a docker named volume that bind-mounts ``device_path`` on container start.

    Uses the ``local`` driver's bind options
    (``type=none``, ``device=<path>``, ``o=bind``) so the docker volume is just
    a name plus a record of the host path it should bind into containers; the
    actual data lives at ``device_path`` (a btrfs subvolume here).
    """
    run_docker(
        outer,
        [
            "volume",
            "create",
            "--driver",
            "local",
            "--opt",
            "type=none",
            "--opt",
            f"device={device_path}",
            "--opt",
            "o=bind",
            volume_name,
        ],
    )


def prepare_btrfs_on_outer(
    outer: OuterHostInterface,
    *,
    host_id: HostId,
    btrfs_mount_path: Path,
    loop_file_path: Path,
    outer_disk_reserved_gb: int,
) -> Path:
    """Ensure btrfs loop FS + per-host subvolume exist on the outer; return the subvolume path.

    Each step is independently idempotent so a partially-failed earlier
    ``mngr create`` (e.g. allocate succeeded, mount failed) can be retried
    cleanly: btrfs-progs install, loop-file allocation + ``mkfs.btrfs``,
    loop mount, ``/etc/fstab`` line append, and ``btrfs subvolume create``
    each gate on a probe and skip when already in place. Never
    ``mkfs.btrfs -f`` -- a populated image file is always preserved.

    Returns the absolute path of the per-host subvolume on the outer,
    suitable for use as the ``device=`` value of a bind-options docker volume.

    Raises ``VpsProvisioningError`` if free space on ``/`` (after subtracting
    ``outer_disk_reserved_gb``) is not positive, or if any setup step fails.
    """
    subvolume_path = btrfs_mount_path / host_id.get_uuid().hex

    # Pre-mounted-btrfs case (slices): the btrfs filesystem is already mounted at
    # ``btrfs_mount_path`` -- it's the VM's lima ``additionalDisk``, not a loop
    # image we manage -- so there is nothing to allocate/mount/fstab. Detected as
    # "mount present AND our loop file absent" so a normal loop-backed VPS re-run
    # (loop file present) still takes the full path below. Just ensure btrfs-progs
    # and the per-host subvolume, then return.
    if is_path_mounted_on_outer(outer, btrfs_mount_path) and not check_file_exists_on_outer(outer, loop_file_path):
        with log_span("Using pre-mounted btrfs at {} (no loop image)", btrfs_mount_path):
            if not is_btrfs_progs_installed_on_outer(outer):
                install_btrfs_progs_on_outer(outer)
            ensure_btrfs_subvolume_on_outer(outer, subvolume_path)
        return subvolume_path

    with log_span("Ensuring btrfs-progs is installed on outer"):
        if not is_btrfs_progs_installed_on_outer(outer):
            install_btrfs_progs_on_outer(outer)

    # Allocate the loop file (if missing) sized to free-space-minus-reserve.
    # The free-space check is skipped when the loop file already exists, so
    # re-running on an already-provisioned VPS doesn't fail when the
    # reserve has since been consumed by docker image layers.
    if not check_file_exists_on_outer(outer, loop_file_path):
        with log_span("Computing btrfs loop file size from free space on /"):
            free_gb = get_outer_free_disk_gb(outer, Path("/"))
            loop_file_size_gb = free_gb - outer_disk_reserved_gb
            if loop_file_size_gb <= 0:
                raise VpsProvisioningError(
                    f"Insufficient free space on outer for btrfs loop file: "
                    f"free={free_gb}GB, outer_disk_reserved_gb={outer_disk_reserved_gb}GB. "
                    f"Need free > reserved. Lower outer_disk_reserved_gb or use a larger VPS plan."
                )
        with log_span("Allocating btrfs loop file at {} ({}GB)", loop_file_path, loop_file_size_gb):
            _run_provisioning_step(
                outer,
                f"mkdir -p {shlex.quote(str(loop_file_path.parent))}",
                error_prefix=f"Failed to create parent dir {loop_file_path.parent} for btrfs loop file",
                timeout_seconds=10.0,
            )
            _run_provisioning_step(
                outer,
                f"fallocate -l {loop_file_size_gb}G {shlex.quote(str(loop_file_path))}",
                error_prefix=f"Failed to fallocate btrfs loop file at {loop_file_path}",
                timeout_seconds=120.0,
            )
            _run_provisioning_step(
                outer,
                f"mkfs.btrfs {shlex.quote(str(loop_file_path))}",
                error_prefix=f"Failed to mkfs.btrfs on {loop_file_path}",
                timeout_seconds=180.0,
            )

    with log_span("Mounting btrfs loop file at {}", btrfs_mount_path):
        _run_provisioning_step(
            outer,
            f"mkdir -p {shlex.quote(str(btrfs_mount_path))}",
            error_prefix=f"Failed to create btrfs mount path {btrfs_mount_path}",
            timeout_seconds=10.0,
        )
        if not is_path_mounted_on_outer(outer, btrfs_mount_path):
            _run_provisioning_step(
                outer,
                f"mount -o loop {shlex.quote(str(loop_file_path))} {shlex.quote(str(btrfs_mount_path))}",
                error_prefix=f"Failed to loop-mount {loop_file_path} at {btrfs_mount_path}",
                timeout_seconds=30.0,
            )

    if not is_fstab_entry_present_on_outer(outer, loop_file_path):
        with log_span("Appending fstab entry for {}", loop_file_path):
            fstab_line = f"{loop_file_path}  {btrfs_mount_path}  btrfs  loop,defaults  0 0"
            # echo + >> is the simplest idempotency-safe form here once the grep
            # above has confirmed the line is absent (no risk of duplicating).
            _run_provisioning_step(
                outer,
                f"echo {shlex.quote(fstab_line)} >> /etc/fstab",
                error_prefix=f"Failed to append fstab entry for {loop_file_path}",
                timeout_seconds=10.0,
            )

    ensure_btrfs_subvolume_on_outer(outer, subvolume_path)
    return subvolume_path


def ensure_btrfs_subvolume_on_outer(outer: OuterHostInterface, subvolume_path: Path) -> None:
    """Create a btrfs subvolume at ``subvolume_path`` if it does not already exist.

    Idempotent: a re-run on an already-provisioned outer is a no-op. Raises
    ``VpsProvisioningError`` if the create fails.
    """
    if check_directory_exists_on_outer(outer, subvolume_path):
        return
    with log_span("Creating btrfs subvolume {}", subvolume_path):
        _run_provisioning_step(
            outer,
            f"btrfs subvolume create {shlex.quote(str(subvolume_path))}",
            error_prefix=f"Failed to create btrfs subvolume at {subvolume_path}",
            timeout_seconds=30.0,
        )


def delete_btrfs_subvolume_on_outer(outer: OuterHostInterface, subvolume_path: Path) -> None:
    """Delete a btrfs subvolume on the outer; raise MngrError on failure.

    No-op when the subvolume's path is already absent on the outer (caller has
    nothing else to do for an already-cleaned-up host).
    """
    if not check_directory_exists_on_outer(outer, subvolume_path):
        return
    result = outer.execute_idempotent_command(
        f"btrfs subvolume delete {shlex.quote(str(subvolume_path))}",
        timeout_seconds=60.0,
    )
    if not result.success:
        raise MngrError(f"btrfs subvolume delete {subvolume_path} failed: stderr={result.stderr.strip()!r}")


def seed_host_volume_layout_on_outer(outer: OuterHostInterface, subvolume_path: Path) -> None:
    """Pre-create the ``host_dir/`` and ``agents/`` subdirectories of the subvolume.

    A single ``mkdir -p`` so downstream writers (the agent container,
    ``persist_agent_data``) don't need to mkdir first. Idempotent.
    """
    host_dir_path = subvolume_path / HOST_DIR_SUBPATH
    agents_dir_path = subvolume_path / AGENTS_SUBPATH
    result = outer.execute_idempotent_command(
        f"mkdir -p {shlex.quote(str(host_dir_path))} {shlex.quote(str(agents_dir_path))}",
        timeout_seconds=10.0,
    )
    if not result.success:
        raise MngrError(f"Failed to seed host volume layout under {subvolume_path}: stderr={result.stderr.strip()!r}")


def pull_image(outer: OuterHostInterface, image: str, timeout_seconds: float = 300.0) -> None:
    """Pull a Docker image."""
    run_docker(outer, ["pull", image], timeout_seconds=timeout_seconds)


def run_container(
    outer: OuterHostInterface,
    *,
    image: str,
    name: str,
    port_mappings: Mapping[str, str],
    volumes: Sequence[str],
    labels: Mapping[str, str],
    extra_args: Sequence[str],
    entrypoint_cmd: str,
) -> str:
    """Run a detached docker container on outer. Returns the container id."""
    args: list[str] = ["run", "-d", "--name", name]
    for host_bind, container_port in port_mappings.items():
        args.extend(["-p", f"{host_bind}:{container_port}"])
    for vol in volumes:
        args.extend(["-v", vol])
    for key, value in labels.items():
        args.extend(["--label", f"{key}={value}"])
    args.extend(extra_args)
    args.extend(["--entrypoint", "sh", image, "-c", entrypoint_cmd])
    output = run_docker(outer, args, timeout_seconds=120.0)
    container_id = output.strip()
    logger.debug("Started container {} ({})", name, container_id[:12])
    return container_id


def setup_container_ssh(
    outer: OuterHostInterface,
    container_name: str,
    *,
    mngr_host_dir: str,
    host_volume_mount_path: str | None,
    container_public_key: str,
    container_host_private_key: str,
    container_host_public_key: str,
    known_hosts_entries: tuple[str, ...],
    authorized_keys_entries: tuple[str, ...],
) -> None:
    """Set up SSH inside the container via docker exec.

    Installs the required packages, points the container's mngr host_dir at
    the mounted volume, installs the client/host SSH keys, seeds known_hosts
    and authorized_keys, and starts sshd. Pure-ish orchestration over
    ``exec_in_container`` so both the VPS and Lima providers share it; the
    caller supplies the keypairs it manages.
    """
    with log_span("Installing packages in container"):
        install_cmd = build_check_and_install_packages_command(
            mngr_host_dir=mngr_host_dir,
            host_volume_mount_path=host_volume_mount_path,
        )
        exec_in_container(outer, container_name, install_cmd, timeout_seconds=300.0)

    with log_span("Configuring SSH in container"):
        ssh_cmd = build_configure_ssh_command(
            user="root",
            client_public_key=container_public_key,
            host_private_key=container_host_private_key,
            host_public_key=container_host_public_key,
        )
        exec_in_container(outer, container_name, ssh_cmd)

    known_hosts_cmd = build_add_known_hosts_command("root", known_hosts_entries)
    if known_hosts_cmd is not None:
        exec_in_container(outer, container_name, known_hosts_cmd)

    auth_keys_cmd = build_add_authorized_keys_command("root", authorized_keys_entries)
    if auth_keys_cmd is not None:
        exec_in_container(outer, container_name, auth_keys_cmd)

    start_container_sshd(outer, container_name)


def start_container_sshd(outer: OuterHostInterface, container_name: str) -> None:
    """(Re)start sshd inside a container in the background.

    Used both during initial container setup and after a container restart.
    The latter matters because sshd is launched via ``docker exec`` rather than
    the container's entrypoint, so it does not survive a ``docker stop``/``start``
    (or a host VM reboot that takes the container down with it); ``/run/sshd``
    is on tmpfs and is recreated here for the same reason.
    """
    with log_span("Starting sshd in container"):
        exec_in_container(
            outer,
            container_name,
            f"{build_start_sshd_command()} &",
        )


def build_ssh_transport_for_outer(outer: OuterHostInterface) -> tuple[str, str, str, int, str]:
    """Build the rsync ssh-transport command and key fields for the given outer.

    Returns (ssh_command, ssh_user, hostname, port, ssh_key_path_str). Raises
    MngrError if outer has no SSH connection info (i.e. is local).
    """
    info = outer.get_ssh_connection_info()
    if info is None:
        raise MngrError("Cannot upload directory to a local outer host")
    user, hostname, port, key_path = info
    # Mirror docker_over_ssh._SSH_BASE_OPTIONS plus the outer host's known_hosts
    # so rsync's ssh subprocess uses the same trust store as the outer host.
    host_data = outer.connector.host.data
    known_hosts = host_data.get("ssh_known_hosts_file", "")
    # Pass the SSH port explicitly. VPS outers listen on 22, but the lima
    # docker-mode outer is the VM reached via a Lima-forwarded port on
    # 127.0.0.1 (e.g. 38519). Without -p, rsync's ssh would connect to
    # 127.0.0.1:22 (the wrong target) and strict host-key checking fails,
    # since the known_hosts entry is keyed as [127.0.0.1]:<forwarded-port>.
    ssh_cmd = (
        f"ssh -i {shlex.quote(str(key_path))} "
        f"-p {port} "
        f"-o UserKnownHostsFile={shlex.quote(str(known_hosts))} "
        f"-o StrictHostKeyChecking=yes "
        f"-o BatchMode=yes "
        f"-o ConnectTimeout=15 "
        f"-o ServerAliveInterval=20 "
        f"-o ServerAliveCountMax=10"
    )
    return ssh_cmd, user, hostname, port, str(key_path)


def _run_rsync_with_retry(
    cg: ConcurrencyGroup,
    cmd: Sequence[str],
    hostname: str,
    operation: str,
    timeout_seconds: float,
) -> None:
    """Run an rsync transfer ``cmd``, retrying connection-class failures with backoff.

    Shared by ``upload_directory_to_outer`` / ``download_directory_from_outer``:
    retries up to ``_UPLOAD_MAX_ATTEMPTS`` with backoff, since fresh Vultr VPSes
    routinely drop the first SSH connection in their first minute of life. A
    whole-process timeout is not retried (the next attempt would just time out
    again, tripling the time to surface a wedged VPS). Non-retryable rsync errors
    fail fast on the first attempt. ``operation`` (e.g. "Upload"/"Download") only
    labels the log/error messages.
    """
    last_stderr = ""
    for attempt in range(1, _UPLOAD_MAX_ATTEMPTS + 1):
        finished = cg.run_process_to_completion(
            command=list(cmd),
            is_checked_after=False,
            timeout=timeout_seconds,
        )
        if finished.is_timed_out:
            raise MngrError(f"{operation} timed out after {timeout_seconds}s")
        if finished.returncode == 0:
            return
        last_stderr = finished.stderr.strip()
        is_last_attempt = attempt == _UPLOAD_MAX_ATTEMPTS
        if is_last_attempt or not is_retryable_rsync_error(last_stderr):
            break
        backoff_seconds = _UPLOAD_RETRY_BACKOFF_SECONDS[attempt - 1]
        logger.warning(
            "{} ({}) attempt {}/{} failed; retrying in {:.0f}s. stderr={!r}",
            operation,
            hostname,
            attempt,
            _UPLOAD_MAX_ATTEMPTS,
            backoff_seconds,
            last_stderr,
        )
        time.sleep(backoff_seconds)
    raise MngrError(f"{operation} failed: {last_stderr}")


def upload_directory_to_outer(
    outer: OuterHostInterface,
    cg: ConcurrencyGroup,
    local_path: Path,
    remote_path: str,
    timeout_seconds: float = 900.0,
) -> None:
    """Upload a local directory to outer via rsync over SSH.

    Mirrors the behavior of the legacy ``DockerOverSsh.upload_directory``:
    retries connection-class failures (broken pipe, RST, ssh-disconnect) with
    backoff (see ``_run_rsync_with_retry``). ``--partial-dir`` lets retries resume
    rather than re-upload from scratch; that path lives outside the build context
    so partial files never end up baked into the docker image.
    """
    ssh_cmd, user, hostname, _port, _key_path = build_ssh_transport_for_outer(outer)
    local_str = str(local_path).rstrip("/") + "/"
    cmd = [
        "rsync",
        "-az",
        "--delete",
        f"--partial-dir={_RSYNC_PARTIAL_DIR_REMOTE}",
        "--exclude=__pycache__",
        "--exclude=.venv",
        "--exclude=node_modules",
        "--exclude=.mypy_cache",
        "--exclude=.ruff_cache",
        "--exclude=.pytest_cache",
        "--exclude=.test_output",
        "--exclude=htmlcov",
        "--exclude=.test_durations",
        "-e",
        ssh_cmd,
        local_str,
        f"{user}@{hostname}:{remote_path}/",
    ]
    logger.debug("Uploading {} to {}@{}:{}", local_path, user, hostname, remote_path)
    _run_rsync_with_retry(cg, cmd, hostname, "Upload", timeout_seconds)


def download_directory_from_outer(
    outer: OuterHostInterface,
    cg: ConcurrencyGroup,
    remote_path: str,
    local_path: Path,
    timeout_seconds: float = 900.0,
) -> None:
    """Download a directory from outer to ``local_path`` via rsync over SSH (the pull twin of upload).

    Same connection-class retry/backoff as ``upload_directory_to_outer`` (see
    ``_run_rsync_with_retry``). rsync copies the regular-file tree (and symlinks,
    with ``-l``) and -- crucially for offline host_dir capture --
    ``--no-specials --no-devices`` makes it skip sockets/FIFOs/device nodes
    entirely rather than failing on them, so a live tmux socket in ``host_dir``
    can't sink the copy. ``local_path`` must already exist; rsync populates it in place.
    """
    ssh_cmd, user, hostname, _port, _key_path = build_ssh_transport_for_outer(outer)
    remote_str = remote_path.rstrip("/") + "/"
    cmd = [
        "rsync",
        "-az",
        "--no-specials",
        "--no-devices",
        "-e",
        ssh_cmd,
        f"{user}@{hostname}:{remote_str}",
        str(local_path).rstrip("/") + "/",
    ]
    logger.debug("Downloading {}@{}:{} to {}", user, hostname, remote_path, local_path)
    _run_rsync_with_retry(cg, cmd, hostname, "Download", timeout_seconds)


def noop_line_sink(_line: str) -> None:
    """No-op line sink for ``execute_streaming_command`` callers that don't care about output."""


def emit_docker_build_output(line: str) -> None:
    """Log a line of docker build output at BUILD level."""
    stripped = line.strip()
    if stripped:
        logger.log(LogLevel.BUILD.value, "{}", stripped, source="docker")


def resolve_dockerfile_paths(
    docker_build_args: Sequence[str],
    remote_build_dir: str,
) -> tuple[str, ...]:
    """Rewrite relative --file/--dockerfile paths to absolute paths on the outer.

    Docker resolves --file relative to the daemon's CWD, not the build context.
    Since the build context was uploaded to remote_build_dir on the outer, any
    relative Dockerfile paths must be prefixed with that directory.

    Handles both ``--file=Dockerfile`` and ``-f Dockerfile`` forms.
    """
    resolved: list[str] = []
    is_next_arg_dockerfile = False
    for arg in docker_build_args:
        if is_next_arg_dockerfile:
            if not arg.startswith("/"):
                arg = f"{remote_build_dir}/{arg}"
            is_next_arg_dockerfile = False
        elif arg in ("-f", "--file", "--dockerfile"):
            is_next_arg_dockerfile = True
        else:
            for prefix in ("--file=", "-f=", "--dockerfile="):
                if arg.startswith(prefix):
                    dockerfile_path = arg[len(prefix) :]
                    if not dockerfile_path.startswith("/"):
                        arg = f"{prefix}{remote_build_dir}/{dockerfile_path}"
                    break
        resolved.append(arg)
    return tuple(resolved)


def build_image_on_outer(
    outer: OuterHostInterface,
    *,
    tag: str,
    build_context_path: str,
    docker_build_args: Sequence[str],
    timeout_seconds: float,
    on_output: Callable[[str], None] | None,
    builder: DockerBuilder,
) -> str:
    """Build a Docker image on outer from a remote build context. Returns the tag.

    When ``builder`` is DEPOT, ensures the depot CLI is installed on outer,
    forwards DEPOT_TOKEN (required) from the agent's environment, optionally
    forwards DEPOT_PROJECT_ID when set, and runs ``depot build --load``.
    """
    if builder is DockerBuilder.DEPOT:
        ensure_depot_token_available(builder)
        depot_token = os.environ["DEPOT_TOKEN"]
        depot_project_id = os.environ.get("DEPOT_PROJECT_ID", "")
        args = ["build", "--load", "-t", tag] + list(docker_build_args) + [build_context_path]
        quoted = " ".join(shlex.quote(a) for a in args)
        env: dict[str, str] = {"DEPOT_TOKEN": depot_token}
        if depot_project_id:
            env["DEPOT_PROJECT_ID"] = depot_project_id
        remote_cmd = f"{_DEPOT_RESOLVE_AND_INSTALL} && {_DEPOT_BIN} {quoted}"
        run_env: Mapping[str, str] | None = env
    else:
        args = ["build", "-t", tag] + list(docker_build_args) + [build_context_path]
        remote_cmd = "docker " + " ".join(shlex.quote(a) for a in args)
        run_env = None

    safe_remote_cmd = redact_secret_env(remote_cmd)
    logger.trace("docker build remote command: {}", safe_remote_cmd)

    # Stream build output line-by-line so the user sees progress during long
    # docker builds. execute_streaming_command treats the command as
    # idempotent and retries transient SSH errors with backoff -- on retry
    # on_output will be re-invoked with the new attempt's output (duplicates
    # are expected and acceptable for docker build).
    line_callback: Callable[[str], None] = on_output if on_output is not None else noop_line_sink
    result = outer.execute_streaming_command(
        remote_cmd,
        line_callback,
        env=run_env,
        timeout_seconds=timeout_seconds,
    )
    if not result.success:
        tail = "\n".join((result.stdout + "\n" + result.stderr).splitlines()[-50:])
        raise MngrError(f"Remote docker build failed: {tail}")
    return tag


def _clone_build_context_for_self_contained_git(local_context: Path, git_depth: int | None) -> Path | None:
    """Clone a local git build context into a temp dir for a self-contained .git.

    Returns the clone path (a ``repo/`` subdir of a fresh temp dir; the caller
    uploads it and removes ``clone.parent`` once the build finishes), or
    ``None`` when ``local_context`` is not a git repo and ``git_depth`` does not
    force a clone -- in which case the caller uploads the context verbatim.

    Cloning is what keeps host-specific git state out of the image. Two shapes
    of ``.git`` matter:
      - a linked worktree's ``.git`` is a gitlink *file* pointing at the
        primary repo's admin dir -- unusable as a standalone repo in the image;
      - a primary checkout's ``.git`` is a directory that also holds a
        ``worktrees/`` admin dir registering the operator's *other* worktree
        branches as checked out. Baked into the image, those registrations make
        the post-build mirror-push seed (``git push +refs/heads/*`` into
        ``/code/mngr``) fail with "refusing to update checked out branch".
    A fresh ``git clone`` carries neither. The operator's working tree
    (committed + uncommitted) is then overlaid back on top, since ``git clone``
    alone only carries committed files at HEAD.
    """
    git_marker = local_context / ".git"
    is_worktree = git_marker.is_file()
    is_checkout = git_marker.is_dir()
    is_git_repo = is_worktree or is_checkout
    if not (is_git_repo or git_depth is not None):
        return None

    if is_worktree:
        clone_reason = "worktree"
    elif is_checkout:
        clone_reason = "checkout"
    else:
        clone_reason = f"--git-depth={git_depth}"
    logger.log(LogLevel.BUILD.value, "Cloning build context locally ({})...", clone_reason, source="vps")

    clone_root = Path(tempfile.mkdtemp(prefix="mngr-vps-build-"))
    clone_target = clone_root / "repo"
    clone_cmd = ["git", "clone"]
    if git_depth is not None:
        clone_cmd.extend(["--depth", str(git_depth)])
    # Use file:// so --depth is honored for local repos
    clone_cmd.extend([f"file://{local_context}", str(clone_target)])
    clone_cg = ConcurrencyGroup(name="git-clone-build-context")
    # The caller only learns about (and cleans up) the temp dir once we return
    # it, so clean up ourselves if we fail before returning.
    cloned_ok = False
    try:
        with translate_outer_concurrency_errors("clone the build context"), clone_cg:
            clone_result = clone_cg.run_process_to_completion(
                command=clone_cmd,
                is_checked_after=False,
                timeout=120.0,
            )
            if clone_result.returncode != 0:
                raise MngrError(f"Failed to clone build context: {clone_result.stderr.strip()}")
            # Overlay the working tree (committed + uncommitted edits, e.g. a
            # locally-rsynced ``vendor/mngr/``) on top of the fresh clone; the
            # clone alone rolls the context back to HEAD. A depth-only clone of a
            # bare repo has no working tree to overlay.
            if is_git_repo:
                rsync_worktree_over_clone(local_context, clone_target, cg=clone_cg)
        cloned_ok = True
    finally:
        if not cloned_ok:
            shutil.rmtree(clone_root, ignore_errors=True)
    return clone_target


def build_image_on_outer_from_build_args(
    outer: OuterHostInterface,
    cg: ConcurrencyGroup,
    *,
    host_id: HostId,
    docker_build_args: tuple[str, ...],
    git_depth: int | None,
    builder: DockerBuilder,
    build_timeout_seconds: float = 600.0,
) -> str:
    """Build a Docker image on the outer from the provided build args. Returns the image tag.

    Uploads the build context (if a local path is referenced) to the outer
    and runs docker build there. If the local build context is a git repo
    (either a linked worktree, whose ``.git`` is a gitlink file, or a normal
    checkout, whose ``.git`` is a directory), clones it into a temp directory
    first so the ``.git`` baked into the image is self-contained -- a fresh
    clone carries neither a worktree gitlink nor a primary checkout's
    ``.git/worktrees/`` admin (which registers *other* branches as checked
    out and would make the post-build mirror-push seed fail with "refusing to
    update checked out branch"). The operator's working tree (including
    uncommitted edits) is overlaid back on top of the clone. If git_depth is
    specified, the clone uses --depth.
    """
    build_tag = f"mngr-build-{host_id}"
    remote_build_dir = f"/tmp/mngr-build-{host_id.get_uuid().hex}"

    # Separate the build context path from other docker build args.
    # Docker build expects the last positional arg to be the context path.
    # We scan for args that look like local paths (not starting with --)
    # and upload them as the build context.
    context_args: list[str] = []
    non_context_args: list[str] = []
    for arg in docker_build_args:
        if not arg.startswith("-") and Path(arg).exists():
            context_args.append(arg)
        else:
            non_context_args.append(arg)

    # If the build context is a local git repo (or --git-depth is set), clone
    # it so the .git baked into the image is self-contained -- see
    # _clone_build_context_for_self_contained_git for why.
    local_clone_dir: Path | None = None
    if context_args:
        local_context = Path(context_args[-1]).resolve()
        clone_target = _clone_build_context_for_self_contained_git(local_context, git_depth)
        if clone_target is not None:
            # clone_target is <tempdir>/repo; remove the whole tempdir on exit.
            local_clone_dir = clone_target.parent
            context_args[-1] = str(clone_target)

    try:
        logger.log(
            LogLevel.BUILD.value,
            "Building Docker image on outer (this may take several minutes)...",
            source="docker",
        )
        if context_args:
            upload_context = Path(context_args[-1])
            logger.log(LogLevel.BUILD.value, "Uploading build context to outer...", source="vps")
            with log_span("Uploading build context to outer"):
                mkdir_result = outer.execute_idempotent_command(f"mkdir -p {shlex.quote(remote_build_dir)}")
                if not mkdir_result.success:
                    raise MngrError(
                        f"Failed to create remote build dir {remote_build_dir}: {mkdir_result.stderr.strip()}"
                    )
                upload_cg = ConcurrencyGroup(name="rsync-build-context")
                with translate_outer_concurrency_errors("upload the build context to the host"), upload_cg:
                    upload_directory_to_outer(outer, upload_cg, upload_context, remote_build_dir)

            # Rewrite --file/--dockerfile paths to absolute paths on the outer.
            resolved_build_args = resolve_dockerfile_paths(non_context_args, remote_build_dir)

            with log_span("Building Docker image on outer"):
                build_image_on_outer(
                    outer,
                    tag=build_tag,
                    build_context_path=remote_build_dir,
                    docker_build_args=tuple(resolved_build_args),
                    timeout_seconds=build_timeout_seconds,
                    on_output=emit_docker_build_output,
                    builder=builder,
                )
        else:
            # No local context -- pass all args to docker build with a minimal context
            mkdir_result = outer.execute_idempotent_command(f"mkdir -p {shlex.quote(remote_build_dir)}")
            if not mkdir_result.success:
                raise MngrError(f"Failed to create remote build dir {remote_build_dir}: {mkdir_result.stderr.strip()}")
            with log_span("Building Docker image on outer"):
                build_image_on_outer(
                    outer,
                    tag=build_tag,
                    build_context_path=remote_build_dir,
                    docker_build_args=tuple(docker_build_args),
                    timeout_seconds=build_timeout_seconds,
                    on_output=emit_docker_build_output,
                    builder=builder,
                )
        logger.log(LogLevel.BUILD.value, "Docker image built successfully", source="docker")
    finally:
        if local_clone_dir is not None:
            shutil.rmtree(local_clone_dir, ignore_errors=True)

    # Clean up remote build directory
    cleanup_result = outer.execute_idempotent_command(f"rm -rf {shlex.quote(remote_build_dir)}")
    if not cleanup_result.success:
        logger.debug("Failed to clean up remote build dir: {}", cleanup_result.stderr.strip())

    return build_tag

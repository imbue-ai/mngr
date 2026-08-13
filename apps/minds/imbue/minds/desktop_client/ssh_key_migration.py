"""One-off migration of RSA client SSH keys to Ed25519 for per-host-keyed workspaces.

Workspaces created before the Ed25519 keygen switch hold RSA-4096 client keys.
RSA keys keep working for SSH, but the workspace owner-exec channel (which the
hosted web chrome drives) only accepts Ed25519 signatures, so an RSA-keyed
workspace can never be driven from the web until its key is rotated.

The rotation mirrors how the key was authorized in the first place (the
connector's lease-time injection): the new public key is appended to
``~/.ssh/authorized_keys`` of the same login user on both layers -- the agent's
container (via ``mngr exec``) and its outer host (via ``mngr exec --outer``) --
and only then are the local key files swapped. The old key is never removed
from any ``authorized_keys``, and the swap is verified end to end (an exec
probe on both layers with the new key) with an automatic rollback to the RSA
pair on failure, so the migration can never lock this install out of a host.

Scope: only hosts with the per-host key layout
(``providers/*/*/hosts/<host_id>/ssh_key``, the imbue_cloud layout). The lima
provider's shared ``root_ssh_key`` is deliberately NOT rotated here: every lima
VM's ``authorized_keys`` is overwritten from its baked lima config each boot,
so an appended key would not survive a VM restart, and a swap would strand any
stopped VM. Lima migrates later, by moving that provider to per-host keys.

A migrated (or confirmed-Ed25519) host gets a marker file so it is never
re-examined; failures carry no marker and are retried on later passes, with a
per-session attempt cap so a persistently failing host cannot hot-loop.
"""

import json
import shlex
import threading
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Final

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.asymmetric import rsa
from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.workspace_record_store import resolve_mngr_profile_dir
from imbue.minds.errors import MindError
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState

# One bounded exec per layer per step; generous because a cold container SSH
# session can take a while to establish on a busy host.
_MIGRATION_EXEC_TIMEOUT_SECONDS: Final[float] = 60.0
# How often the background loop re-scans when nothing changed. Migration is a
# one-off per host, so a slow cadence is plenty; a restart also rescans.
_PASS_INTERVAL_SECONDS: Final[float] = 1800.0
# How often the loop re-checks for the first complete discovery snapshot.
_DISCOVERY_POLL_SECONDS: Final[float] = 5.0
# A host that keeps failing is left for the next app session (or the manual
# CLI) after this many attempts, so the loop cannot hot-loop on a broken host.
_MAX_ATTEMPTS_PER_SESSION: Final[int] = 3
# The RSA pair is kept alongside the new files (never deleted) so an operator
# can always recover a host by hand; restored automatically on a failed verify.
_RSA_BACKUP_SUFFIX: Final[str] = ".rsa-backup"

_STOP_WAIT_TIMEOUT_SECONDS: Final[float] = 10.0


class SshKeyMigrationError(MindError):
    """Raised when one host's RSA -> Ed25519 key rotation fails."""


class ClientKeyType(UpperCaseStrEnum):
    """The algorithm of an on-disk client private key."""

    RSA = auto()
    ED25519 = auto()
    OTHER = auto()
    UNREADABLE = auto()


class MigrationOutcome(UpperCaseStrEnum):
    """What one pass decided for one host."""

    MIGRATED = auto()
    ALREADY_ED25519 = auto()
    SKIPPED_NO_PER_HOST_KEY = auto()
    SKIPPED_NOT_RUNNING = auto()
    SKIPPED_UNSUPPORTED_KEY = auto()
    SKIPPED_ATTEMPTS_EXHAUSTED = auto()
    FAILED = auto()


class MigratableWorkspace(FrozenModel):
    """One workspace host as seen by a migration pass."""

    host_id: str = Field(description="The workspace's host-<hex> coordinate")
    agent_id: str = Field(description="An agent on the host, the exec target")
    is_running: bool = Field(description="Whether the host is RUNNING (migration only touches running hosts)")


class HostMigrationResult(FrozenModel):
    """The outcome of one host's examination in a migration pass."""

    host_id: str = Field(description="The examined host")
    outcome: MigrationOutcome = Field(description="What the pass decided")
    detail: str | None = Field(default=None, description="Failure description (FAILED only)")


@pure
def classify_client_private_key(private_key_text: str) -> ClientKeyType:
    """Classify a client private key by algorithm, tolerating both PEM and OpenSSH containers."""
    key_bytes = private_key_text.encode("utf-8")
    for load_private_key in (serialization.load_pem_private_key, serialization.load_ssh_private_key):
        try:
            private_key = load_private_key(key_bytes, password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            continue
        if isinstance(private_key, rsa.RSAPrivateKey):
            return ClientKeyType.RSA
        elif isinstance(private_key, ed25519.Ed25519PrivateKey):
            return ClientKeyType.ED25519
        else:
            return ClientKeyType.OTHER
    return ClientKeyType.UNREADABLE


def _generate_ed25519_keypair_openssh() -> tuple[str, str]:
    """Generate an Ed25519 keypair as (OpenSSH private key text, OpenSSH public key line).

    Deliberately local (not imported from ``imbue.mngr.providers.ssh_utils``,
    whose module import drags in pyinfra/paramiko): minds talks to mngr via the
    CLI and only mirrors its on-disk key formats.
    """
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_key_text = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_key_line = (
        private_key.public_key()
        .public_bytes(encoding=serialization.Encoding.OpenSSH, format=serialization.PublicFormat.OpenSSH)
        .decode("utf-8")
    )
    return private_key_text, public_key_line


@pure
def build_ensure_authorized_key_command(public_key_line: str) -> str:
    """Shell command that idempotently appends a public key to ``~/.ssh/authorized_keys``.

    Runs as the login user on whichever layer executes it, mirroring the
    connector's lease-time key injection. The trailing grep makes the command's
    exit status prove the key is actually present (not merely that the append
    ran), so a caller can treat exit 0 as "authorized".
    """
    quoted_key = shlex.quote(public_key_line.strip())
    return (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
        "chmod 600 ~/.ssh/authorized_keys && "
        f"{{ grep -qxF {quoted_key} ~/.ssh/authorized_keys || printf '%s\\n' {quoted_key} >> ~/.ssh/authorized_keys; }} && "
        f"grep -qxF {quoted_key} ~/.ssh/authorized_keys"
    )


def find_per_host_key_path(mngr_host_dir: Path, host_id: str) -> Path | None:
    """Locate a host's per-host client key file (the imbue_cloud layout), or None.

    Mirrors the key resolution of ``collect_ssh_key_material``: any provider
    instance's ``providers/*/*/hosts/<host_id>/ssh_key``.
    """
    profile_dir = resolve_mngr_profile_dir(mngr_host_dir)
    if profile_dir is None:
        return None
    providers_dir = profile_dir / "providers"
    if not providers_dir.is_dir():
        return None
    for key_path in sorted(providers_dir.glob(f"*/*/hosts/{host_id}/ssh_key")):
        if key_path.is_file():
            return key_path
    return None


def _run_exec(mngr_caller: MngrCaller, agent_id: str, command: str, is_outer: bool) -> tuple[int, str]:
    argv = ["exec", agent_id, command, "--no-start"]
    if is_outer:
        argv.extend(["--outer", "--missing-outer", "abort"])
    result = mngr_caller.call(argv, timeout=_MIGRATION_EXEC_TIMEOUT_SECONDS)
    return result.returncode, result.stderr.strip()


def _atomic_write_text(path: Path, content: str, mode: int) -> None:
    tmp_path = path.with_name(f".{path.name}.migrate-tmp")
    # Create the temp file owner-only BEFORE writing: this path carries private
    # key material, which must never be group/world-readable, even briefly. A
    # stale temp file from a crashed run is removed first so its (possibly
    # looser) permissions cannot carry over.
    tmp_path.unlink(missing_ok=True)
    tmp_path.touch(mode=0o600)
    tmp_path.write_text(content)
    tmp_path.chmod(mode)
    tmp_path.replace(path)


def _swap_in_new_keypair(key_path: Path, new_private_key: str, new_public_key: str) -> None:
    """Back up the RSA pair (first swap only) and atomically install the Ed25519 pair.

    The public half is replaced first: mngr authenticates with the private key,
    so the old private + new public window is inert, while the reverse window
    would have one connection attempt authenticate with a key whose public half
    is gone.
    """
    public_key_path = key_path.with_name(f"{key_path.name}.pub")
    private_backup_path = key_path.with_name(f"{key_path.name}{_RSA_BACKUP_SUFFIX}")
    public_backup_path = key_path.with_name(f"{key_path.name}.pub{_RSA_BACKUP_SUFFIX}")
    if not private_backup_path.exists():
        _atomic_write_text(private_backup_path, key_path.read_text(), 0o600)
    if not public_backup_path.exists() and public_key_path.exists():
        _atomic_write_text(public_backup_path, public_key_path.read_text(), 0o644)
    _atomic_write_text(public_key_path, new_public_key, 0o644)
    _atomic_write_text(key_path, new_private_key, 0o600)


def _restore_rsa_keypair(key_path: Path) -> None:
    private_backup_path = key_path.with_name(f"{key_path.name}{_RSA_BACKUP_SUFFIX}")
    public_backup_path = key_path.with_name(f"{key_path.name}.pub{_RSA_BACKUP_SUFFIX}")
    if private_backup_path.exists():
        _atomic_write_text(key_path, private_backup_path.read_text(), 0o600)
    if public_backup_path.exists():
        _atomic_write_text(key_path.with_name(f"{key_path.name}.pub"), public_backup_path.read_text(), 0o644)


def migrate_host_client_key(host_id: str, agent_id: str, key_path: Path, mngr_caller: MngrCaller) -> None:
    """Rotate one host's RSA client key to Ed25519, in place and without lockout risk.

    Raises :class:`SshKeyMigrationError` on any failure; the local key files
    are only swapped after the new key is authorized on both layers, and are
    rolled back to the RSA pair when the post-swap connectivity verify fails.
    """
    new_private_key, new_public_key = _generate_ed25519_keypair_openssh()
    ensure_command = build_ensure_authorized_key_command(new_public_key)

    # Authorize the new key on both layers while the RSA key still drives the
    # connections. Container first (cheapest to reach), then the outer host.
    for is_outer, layer_name in ((False, "container"), (True, "outer host")):
        returncode, stderr = _run_exec(mngr_caller, agent_id, ensure_command, is_outer=is_outer)
        if returncode != 0:
            raise SshKeyMigrationError(
                f"Could not authorize the new key on host {host_id}'s {layer_name}: {stderr or 'exec failed'}"
            )

    # Swap the local pair, then prove both layers accept the new key; a failed
    # probe restores the RSA pair (still authorized everywhere) and reports.
    _swap_in_new_keypair(key_path, new_private_key, new_public_key)
    for is_outer, layer_name in ((False, "container"), (True, "outer host")):
        returncode, stderr = _run_exec(mngr_caller, agent_id, "true", is_outer=is_outer)
        if returncode != 0:
            _restore_rsa_keypair(key_path)
            raise SshKeyMigrationError(
                f"Host {host_id}'s {layer_name} rejected the new key (rolled back to RSA): {stderr or 'exec failed'}"
            )


def _marker_path(marker_dir: Path, host_id: str) -> Path:
    return marker_dir / host_id


def _write_marker(marker_dir: Path, host_id: str, outcome: MigrationOutcome) -> None:
    marker_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    _atomic_write_text(_marker_path(marker_dir, host_id), f"{outcome.value} {timestamp}\n", 0o644)


def run_ssh_key_migration_pass(
    workspaces: list[MigratableWorkspace],
    mngr_host_dir: Path,
    marker_dir: Path,
    mngr_caller: MngrCaller,
    # Mutated across passes: per-host failed-attempt counts for this session.
    attempt_count_by_host_id: dict[str, int],
) -> list[HostMigrationResult]:
    """Examine every workspace host once and migrate the RSA-keyed running ones.

    Hosts with a marker file are skipped outright (the one-off already
    happened); every decision for the remaining hosts is returned so callers
    (the background loop, the CLI) can log or print it.
    """
    results: list[HostMigrationResult] = []
    examined_host_ids: set[str] = set()
    for workspace in workspaces:
        if workspace.host_id in examined_host_ids:
            continue
        examined_host_ids.add(workspace.host_id)
        if _marker_path(marker_dir, workspace.host_id).exists():
            continue
        key_path = find_per_host_key_path(mngr_host_dir, workspace.host_id)
        if key_path is None:
            # Not the per-host layout (lima/docker/local rows): out of scope,
            # and rechecked on every pass only via this cheap glob.
            results.append(
                HostMigrationResult(host_id=workspace.host_id, outcome=MigrationOutcome.SKIPPED_NO_PER_HOST_KEY)
            )
            continue
        key_type = classify_client_private_key(key_path.read_text())
        if key_type == ClientKeyType.ED25519:
            _write_marker(marker_dir, workspace.host_id, MigrationOutcome.ALREADY_ED25519)
            results.append(HostMigrationResult(host_id=workspace.host_id, outcome=MigrationOutcome.ALREADY_ED25519))
            continue
        if key_type != ClientKeyType.RSA:
            logger.warning(
                "Client key for host {} is {} (neither RSA nor Ed25519); leaving it alone",
                workspace.host_id,
                key_type,
            )
            results.append(
                HostMigrationResult(host_id=workspace.host_id, outcome=MigrationOutcome.SKIPPED_UNSUPPORTED_KEY)
            )
            continue
        if not workspace.is_running:
            results.append(
                HostMigrationResult(host_id=workspace.host_id, outcome=MigrationOutcome.SKIPPED_NOT_RUNNING)
            )
            continue
        if attempt_count_by_host_id.get(workspace.host_id, 0) >= _MAX_ATTEMPTS_PER_SESSION:
            results.append(
                HostMigrationResult(host_id=workspace.host_id, outcome=MigrationOutcome.SKIPPED_ATTEMPTS_EXHAUSTED)
            )
            continue
        attempt_count_by_host_id[workspace.host_id] = attempt_count_by_host_id.get(workspace.host_id, 0) + 1
        try:
            migrate_host_client_key(workspace.host_id, workspace.agent_id, key_path, mngr_caller)
        except SshKeyMigrationError as e:
            logger.warning("SSH key migration failed for host {}: {}", workspace.host_id, e)
            results.append(
                HostMigrationResult(host_id=workspace.host_id, outcome=MigrationOutcome.FAILED, detail=str(e))
            )
            continue
        _write_marker(marker_dir, workspace.host_id, MigrationOutcome.MIGRATED)
        logger.info("Migrated host {}'s client SSH key from RSA to Ed25519", workspace.host_id)
        results.append(HostMigrationResult(host_id=workspace.host_id, outcome=MigrationOutcome.MIGRATED))
    return results


def list_migratable_workspaces_from_resolver(resolver: BackendResolverInterface) -> list[MigratableWorkspace]:
    """Build a pass's workspace list from the desktop client's discovery view."""
    workspaces: list[MigratableWorkspace] = []
    for agent_id in resolver.list_active_workspace_ids():
        display_info = resolver.get_agent_display_info(agent_id)
        if display_info is None or not display_info.host_id:
            continue
        host_state = resolver.get_host_state(HostId(str(display_info.host_id)))
        workspaces.append(
            MigratableWorkspace(
                host_id=str(display_info.host_id),
                agent_id=str(agent_id),
                is_running=host_state == HostState.RUNNING,
            )
        )
    return workspaces


def list_migratable_workspaces_from_mngr_ls_json(json_text: str) -> list[MigratableWorkspace]:
    """Build a pass's workspace list from ``mngr ls --format json`` output (the manual CLI path).

    One entry per host (the first agent seen on it is the exec target);
    unparseable output raises :class:`SshKeyMigrationError` so the CLI fails
    loudly instead of reporting an empty fleet.
    """
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise SshKeyMigrationError(f"Could not parse `mngr ls --format json` output: {e}") from e
    agents = data.get("agents", []) if isinstance(data, dict) else []
    workspace_by_host_id: dict[str, MigratableWorkspace] = {}
    for agent in agents:
        if not isinstance(agent, dict) or not isinstance(agent.get("id"), str):
            continue
        host = agent.get("host")
        if not isinstance(host, dict) or not isinstance(host.get("id"), str):
            continue
        host_id = host["id"]
        if host_id in workspace_by_host_id:
            continue
        workspace_by_host_id[host_id] = MigratableWorkspace(
            host_id=host_id,
            agent_id=agent["id"],
            is_running=host.get("state") == HostState.RUNNING.value,
        )
    return list(workspace_by_host_id.values())


class SshKeyMigrationScheduler(MutableModel):
    """Background loop that migrates RSA-keyed running workspaces once each."""

    resolver: BackendResolverInterface = Field(frozen=True, description="Discovery view the pass enumerates from")
    mngr_caller: MngrCaller = Field(frozen=True, description="Shared warm mngr caller for the exec steps")
    mngr_host_dir: Path = Field(frozen=True, description="mngr host dir holding the provider key files")
    marker_dir: Path = Field(frozen=True, description="Directory of per-host done markers")
    _attempt_count_by_host_id: dict[str, int] = PrivateAttr(default_factory=dict)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _exited_event: threading.Event = PrivateAttr(default_factory=threading.Event)

    def start(self, concurrency_group: ConcurrencyGroup) -> None:
        concurrency_group.start_new_thread(self._loop, name="ssh-key-migration", daemon=True)

    def stop(self, wait_timeout_seconds: float = _STOP_WAIT_TIMEOUT_SECONDS) -> None:
        """Signal the loop to exit and wait for an in-flight pass to finish.

        Like the sync scheduler, this must complete before the shared mngr
        caller is torn down, so a mid-pass exec cannot race the teardown.
        """
        self._stop_event.set()
        if not self._exited_event.wait(wait_timeout_seconds):
            logger.warning("SSH key migration loop did not exit within {}s", wait_timeout_seconds)

    def run_one_pass_guarded(self) -> None:
        """Run one pass; an expected failure is logged and does not kill the loop."""
        try:
            workspaces = list_migratable_workspaces_from_resolver(self.resolver)
            run_ssh_key_migration_pass(
                workspaces=workspaces,
                mngr_host_dir=self.mngr_host_dir,
                marker_dir=self.marker_dir,
                mngr_caller=self.mngr_caller,
                attempt_count_by_host_id=self._attempt_count_by_host_id,
            )
        except (SshKeyMigrationError, OSError) as e:
            logger.opt(exception=e).warning("SSH key migration pass failed")

    def _loop(self) -> None:
        try:
            while not self._stop_event.is_set() and not self.resolver.has_completed_initial_discovery():
                self._stop_event.wait(_DISCOVERY_POLL_SECONDS)
            while not self._stop_event.is_set():
                self.run_one_pass_guarded()
                self._stop_event.wait(_PASS_INTERVAL_SECONDS)
        finally:
            self._exited_event.set()

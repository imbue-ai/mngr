import shlex
from pathlib import Path
from typing import Any
from typing import Final

from imbue.imbue_common.logging import log_span
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.listing_utils import build_listing_collection_script
from imbue.mngr.providers.listing_utils import extract_agent_data_from_parsed_listing
from imbue.mngr.providers.listing_utils import parse_listing_collection_output
from imbue.mngr.providers.ssh_host_setup import build_add_authorized_keys_command
from imbue.mngr.providers.ssh_host_setup import build_add_known_hosts_command
from imbue.mngr.providers.ssh_host_setup import build_check_and_install_packages_command
from imbue.mngr.providers.ssh_host_setup import build_start_activity_watcher_command
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr_vps.container_setup import HOST_DIR_SUBPATH
from imbue.mngr_vps.data_types import AgentEndpoint
from imbue.mngr_vps.data_types import PlacementHandle
from imbue.mngr_vps.data_types import RealizePlacementContext
from imbue.mngr_vps.data_types import RealizedPlacement
from imbue.mngr_vps.host_store import AGENTS_SUBPATH
from imbue.mngr_vps.host_store import VpsHostRecord
from imbue.mngr_vps.host_store import VpsHostStore
from imbue.mngr_vps.interfaces import HostRealizer
from imbue.mngr_vps.primitives import VPS_KNOWN_HOSTS_NAME
from imbue.mngr_vps.primitives import VPS_SSH_KEY_NAME

# Root-disk directory on the bare VM holding the host record + agent data -- the
# bare analog of the per-host Docker volume. One host per VM, so a fixed path is
# unambiguous; the agent's mngr host_dir is symlinked to ``<dir>/host_dir``.
BARE_HOST_STORE_DIR: Final[Path] = Path("/var/lib/mngr-host")

# On a bare VM the agent IS the root account: it owns the VM's port-22 sshd.
_BARE_AGENT_SSH_USER: Final[str] = "root"


def _run_on_outer(outer: OuterHostInterface, command: str, *, label: str, timeout_seconds: float = 300.0) -> None:
    """Run a host-setup command on the bare VM; raise MngrError on non-zero exit."""
    result = outer.execute_idempotent_command(command, timeout_seconds=timeout_seconds)
    if not result.success:
        raise MngrError(
            f"Bare host-setup step {label!r} failed: stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}"
        )


class BareRealizer(HostRealizer):
    """Places the agent directly on the VPS OS -- no Docker container.

    The agent is the VM's root account, reached at ``vps_ip:22`` with the same
    VPS keypair the provider uses for the outer (the VPS host key was pinned and
    the VPS client key authorized at provision time, so no sshd reconfiguration
    is needed). ``realize_placement`` installs the lightweight host packages and
    the mngr host_dir layout on the VM -- the same setup the container gets,
    applied to the OS -- and the host record lives in a plain directory on the
    root disk. There is no container to stop/start or snapshot; machine
    stop/start/destroy is the substrate's job.
    """

    @property
    def idle_shutdown_command(self) -> str:
        # The agent is the VM's root, so it powers the machine off directly. On a
        # self-stopping cloud substrate (aws/gcp/azure) this stops the instance via
        # the OS-shutdown behavior -- no sentinel or host-side watcher needed.
        return "shutdown -P now"

    @property
    def idle_shutdown_stops_host(self) -> bool:
        return True

    def host_dir_path_on_outer(self, host_id: HostId) -> Path:
        # The agent's host_dir is the symlink target on the VM's root disk.
        return BARE_HOST_STORE_DIR / HOST_DIR_SUBPATH

    def _vps_ssh_keypair(self) -> tuple[Path, str]:
        return load_or_create_ssh_keypair(self.key_dir, VPS_SSH_KEY_NAME)

    def _vps_known_hosts_path(self) -> Path:
        return self.key_dir / VPS_KNOWN_HOSTS_NAME

    def agent_endpoint(self, vps_ip: str) -> AgentEndpoint:
        vps_key_path, _pub = self._vps_ssh_keypair()
        return AgentEndpoint(
            hostname=vps_ip,
            port=22,
            private_key_path=vps_key_path,
            known_hosts_path=self._vps_known_hosts_path(),
            ssh_user=_BARE_AGENT_SSH_USER,
        )

    def open_host_store(self, outer: OuterHostInterface, host_id: HostId) -> VpsHostStore:
        # One host per VM, so the store lives at a fixed root-disk path.
        return VpsHostStore(outer=outer, mountpoint=BARE_HOST_STORE_DIR)

    # --- discovery / listing ----------------------------------------------

    def find_host_record(self, outer: OuterHostInterface) -> tuple[HostId, VpsHostRecord] | None:
        # No container to probe: read the record straight from the fixed store
        # path; the record carries its own host_id.
        record = VpsHostStore(outer=outer, mountpoint=BARE_HOST_STORE_DIR).read_host_record()
        if record is None:
            return None
        return HostId(record.certified_host_data.host_id), record

    def read_live_listing(
        self, outer: OuterHostInterface, host_id: HostId, host_dir: str, prefix: str, window_name: str
    ) -> tuple[list[dict[str, Any]], bool]:
        # The agent's host_dir is on the VM, so the inner listing script runs
        # directly on the outer -- no container indirection. Reaching the VM at
        # all means the agent (the VM) is running.
        raw = self._run_listing_script(
            outer, build_listing_collection_script(host_dir, prefix, window_name), timeout_seconds=60.0
        )
        parsed = parse_listing_collection_output(raw)
        return extract_agent_data_from_parsed_listing(parsed), True

    def is_placement_running(self, outer: OuterHostInterface, handle: PlacementHandle) -> bool:
        # No container: the agent IS the VM, so a reachable VM is a running host.
        return True

    def collect_listing_output(
        self, outer: OuterHostInterface, handle: PlacementHandle, script: str, timeout_seconds: float = 30.0
    ) -> str:
        return self._run_listing_script(outer, script, timeout_seconds=timeout_seconds)

    @staticmethod
    def _run_listing_script(outer: OuterHostInterface, script: str, *, timeout_seconds: float) -> str:
        result = outer.execute_idempotent_command(script, timeout_seconds=timeout_seconds)
        if not result.success:
            raise MngrError(f"Bare listing read failed: stderr={result.stderr.strip()!r}")
        return result.stdout

    def realize_placement(self, outer: OuterHostInterface, ctx: RealizePlacementContext) -> RealizedPlacement:
        host_dir_on_disk = BARE_HOST_STORE_DIR / HOST_DIR_SUBPATH
        agents_dir_on_disk = BARE_HOST_STORE_DIR / AGENTS_SUBPATH

        # Install the host packages and point the agent's mngr host_dir at
        # ``<store>/host_dir`` (the same symlink the container uses, applied to
        # the VM OS). sshd is already configured by cloud-init with the VPS host
        # key and the VPS client key in root's authorized_keys, so it needs no
        # reconfiguration here.
        with log_span("Installing host packages and host_dir layout on bare VM"):
            _run_on_outer(
                outer,
                build_check_and_install_packages_command(
                    mngr_host_dir=str(self.host_dir),
                    host_volume_mount_path=str(host_dir_on_disk),
                ),
                label="install-packages",
            )
            _run_on_outer(
                outer,
                f"mkdir -p {shlex.quote(str(host_dir_on_disk))} {shlex.quote(str(agents_dir_on_disk))}",
                label="seed-store-layout",
                timeout_seconds=10.0,
            )

        known_hosts_cmd = build_add_known_hosts_command(_BARE_AGENT_SSH_USER, tuple(ctx.known_hosts or ()))
        if known_hosts_cmd is not None:
            _run_on_outer(outer, known_hosts_cmd, label="add-known-hosts")

        authorized_keys_cmd = build_add_authorized_keys_command(_BARE_AGENT_SSH_USER, tuple(ctx.authorized_keys or ()))
        if authorized_keys_cmd is not None:
            _run_on_outer(outer, authorized_keys_cmd, label="add-authorized-keys")

        # A bare placement has no container, volume, or container host key, so the
        # handle is empty and the container host key is None.
        return RealizedPlacement()

    def start_activity_watcher(self, outer: OuterHostInterface, handle: PlacementHandle) -> None:
        # Runs on the VM directly (the watcher is pure shell + jq), not via docker exec.
        _run_on_outer(
            outer,
            build_start_activity_watcher_command(str(self.host_dir)),
            label="start-activity-watcher",
        )

    def stop_placement(self, outer: OuterHostInterface, handle: PlacementHandle, timeout_seconds: float) -> None:
        # No container to stop; stopping the machine is the substrate's job (the
        # aws/gcp/azure stop_host override stops the instance).
        return None

    def start_placement(self, outer: OuterHostInterface, handle: PlacementHandle) -> None:
        # The agent's sshd is the VM's own sshd, brought back by the instance
        # start; nothing to re-exec. start_host relaunches the activity watcher
        # separately via start_activity_watcher.
        return None

    def teardown_placement(self, outer: OuterHostInterface, host_id: HostId, handle: PlacementHandle) -> None:
        # Nothing to tear down on the placement: destroy_host destroys the whole VM.
        return None

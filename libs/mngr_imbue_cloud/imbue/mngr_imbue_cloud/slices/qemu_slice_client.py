import base64
import shlex
import tempfile
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ConfigDict

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.ssh import quote_ssh_option_value
from imbue.mngr_imbue_cloud.data_types import BoxManagementTrust
from imbue.mngr_imbue_cloud.data_types import SliceProvisionResult
from imbue.mngr_imbue_cloud.data_types import StorageVolumeState
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError
from imbue.mngr_imbue_cloud.errors import SliceCapacityError
from imbue.mngr_imbue_cloud.errors import SliceCommandError
from imbue.mngr_imbue_cloud.interfaces import SliceVmClientInterface
from imbue.mngr_imbue_cloud.slices.bare_metal import build_read_management_trust_command
from imbue.mngr_imbue_cloud.slices.bare_metal import build_read_storage_volume_command
from imbue.mngr_imbue_cloud.slices.bare_metal import parse_management_trust_output
from imbue.mngr_imbue_cloud.slices.bare_metal import parse_storage_volume_output
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_instance_name
from imbue.mngr_imbue_cloud.slices.box_known_hosts import remove_box_known_hosts_file
from imbue.mngr_imbue_cloud.slices.box_known_hosts import write_box_known_hosts_file
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import SliceInstanceObservation
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_destroy_script
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_list_instance_observations_command
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_list_instances_command
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_slice_env_file
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import parse_slice_instance_observations
from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import MalformedBoxOutputError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_meta_data
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_network_config
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_user_data
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_CONTAINER_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_DISK_SUFFIX
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_INSTANCES_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_DISK_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_PORTS_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_SPACE_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_UNITS_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_VM_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import gen2_disk_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import gen2_instance_dir
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GUEST_RAM_HOLDBACK_MIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_guest_memory_mib
from imbue.mngr_imbue_cloud.slices.qemu_slice import build_qemu_boot_wait_script
from imbue.mngr_imbue_cloud.slices.qemu_slice import build_qemu_reserve_script
from imbue.mngr_imbue_cloud.slices.qemu_slice import build_qemu_start_command
from imbue.mngr_imbue_cloud.slices.qemu_slice import build_qemu_status_command
from imbue.mngr_imbue_cloud.slices.qemu_slice import parse_gen2_reserved_line
from imbue.mngr_vps.primitives import VpsInstanceId
from imbue.mngr_vps.primitives import VpsInstanceStatus

# Box tooling lives in /usr/local/bin; a non-interactive SSH shell may not source
# the slice user's profile, so PATH is set explicitly (same as the gen-1 client).
_BOX_PATH_PREFIX: Final[str] = "PATH=/usr/local/bin:$HOME/.local/bin:$PATH"
_BOX_CONNECT_TIMEOUT_SECONDS: Final[int] = 30
_SHORT_TIMEOUT_SECONDS: Final[float] = 120.0
# The reservation (under the box lock) includes the reflink base-image copy and
# the cidata build -- fast, but give it slack beyond a bare command.
_RESERVE_TIMEOUT_SECONDS: Final[float] = 600.0
# `systemctl start` returns once qemu is spawned (Type=simple), after the root
# net-setup step; the guest boots on afterwards.
_START_TIMEOUT_SECONDS: Final[float] = 300.0
# Generous first-boot budget: base image boot + cloud-init first boot (package
# fallback, btrfs format) on a possibly-loaded box. Mirrors the gen-1 client's
# generous lima start deadline (mngr-internal#469: tight deadlines kill starts
# that were seconds from ready).
_BOOT_WAIT_TIMEOUT_SECONDS: Final[int] = 1500
_BOX_HEALTH_SPLIT_MARKER: Final[str] = "MNGR_BOX_HEALTH_SPLIT"

_STATUS_BY_REPORT: Final[dict[str, VpsInstanceStatus]] = {
    "active": VpsInstanceStatus.ACTIVE,
    "activating": VpsInstanceStatus.PENDING,
    "deactivating": VpsInstanceStatus.HALTED,
    "inactive": VpsInstanceStatus.HALTED,
    "failed": VpsInstanceStatus.HALTED,
}


class QemuSliceVpsClient(SliceVmClientInterface):
    """SliceVmClientInterface for generation-2 slices: raw qemu VMs under systemd on the box.

    Drives the caller-rendered gen-2 scripts **over SSH on the box** (as the
    dedicated slice user, using the pool management key), exactly like the gen-1
    lima client -- so the whole bake runs from the operator's laptop. The box
    needs only the prep-installed artifacts (template unit, root helper, sudoers,
    the running slice DHCP server, the staged base image); everything else
    arrives rendered per call.
    Ordering / snapshot / ssh-key API operations are unavailable.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _box_known_hosts_file(self) -> Path:
        """Write the box's pinned host key to a throwaway known_hosts file for one command and return its path.

        Fails closed when no pinned key is configured -- never trust-on-first-use.
        """
        if not self.box_host_public_key:
            raise SliceCommandError(
                "ssh", 1, f"no pinned host key configured for box {self.box_address}; run the host-key backfill"
            )
        base_dir = Path(self.private_key_path).parent if self.private_key_path else Path(tempfile.gettempdir())
        return write_box_known_hosts_file(base_dir, self.box_address, self.box_ssh_port, self.box_host_public_key)

    def _box_ssh_command(self, remote_command: str, known_hosts_path: Path) -> list[str]:
        """Build the argv that runs ``remote_command`` on the box as the slice user."""
        if not self.private_key_path:
            raise SliceCommandError("ssh", 1, "no pool private key configured for the slice box")
        return [
            "ssh",
            "-i",
            self.private_key_path,
            "-p",
            str(self.box_ssh_port),
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={quote_ssh_option_value(str(known_hosts_path))}",
            "-o",
            f"ConnectTimeout={_BOX_CONNECT_TIMEOUT_SECONDS}",
            "-o",
            "ServerAliveInterval=30",
            f"{self.box_ssh_user}@{self.box_address}",
            # A standalone export rather than an assignment prefix: some remote
            # commands are compound statements (e.g. the disk listing's `for`
            # loop), which an assignment prefix cannot precede.
            f"export {_BOX_PATH_PREFIX}; {remote_command}",
        ]

    def run_on_box(
        self, remote_command: str, *, timeout: float, label: str, is_streaming: bool = False
    ) -> tuple[int | None, str, str]:
        """Run a command on the box over SSH; return (returncode, stdout, stderr)."""
        on_output = (lambda line, _is_stdout: logger.info("  [{}] {}", label, line.rstrip())) if is_streaming else None
        # Built outside the group so a precondition failure (no key, no pinned
        # host key) raises as itself rather than wrapped in the group's error.
        known_hosts_path = self._box_known_hosts_file()
        try:
            command = self._box_ssh_command(remote_command, known_hosts_path)
            cg = ConcurrencyGroup(name=f"slice-box-{label}")
            with cg:
                result = cg.run_process_to_completion(
                    command=command,
                    timeout=timeout,
                    is_checked_after=False,
                    on_output=on_output,
                )
        finally:
            remove_box_known_hosts_file(known_hosts_path)
        return result.returncode, result.stdout, result.stderr

    def _run_script_on_box(
        self, script: str, *, timeout: float, label: str, is_streaming: bool = False
    ) -> tuple[int | None, str, str]:
        """Ship a rendered script to the box (base64, so quoting never mangles it) and run it."""
        encoded = base64.b64encode(script.encode()).decode()
        return self.run_on_box(
            f"echo {shlex.quote(encoded)} | base64 -d | bash",
            timeout=timeout,
            label=label,
            is_streaming=is_streaming,
        )

    def _best_effort_destroy(self, instance_name: str) -> None:
        """Tear down a half-reserved slice after a failed reserve/start; never raises."""
        try:
            self.destroy_instance(VpsInstanceId(instance_name))
        except (SliceCommandError, OSError) as exc:
            logger.warning("Could not clean up half-reserved slice {} on {}: {}", instance_name, self.box_address, exc)

    def provision_slice_vm(
        self,
        *,
        host_id: HostId,
        env_name: str | None,
        vcpus: int,
        memory_mib: int,
        disk_gib: int,
        host_dir: str,
        root_authorized_public_key: str | None,
        host_private_key_pem: str,
        host_public_key_openssh: str,
        boot_disk_gib: int,
        slot_count: int,
        port_range_start: int,
        port_range_end: int,
        extra_root_authorized_keys: tuple[str, ...] = (),
        trusted_user_ca_public_key: str | None = None,
        uplink_mbps: int | None = None,
        units: int | None = None,
        box_total_units: int | None = None,
        box_disk_budget_gib: int | None = None,
    ) -> SliceProvisionResult:
        """Reserve the box's budgets + ports + an ordinal, boot the slice's unit, and wait for its sshd.

        Two phases, mirroring the gen-1 flow: the reserve script (under the box
        lock) claims the machine's share of the box's two budgets (memory units
        and disk), the two host ports, and the ordinal, materializes the slice
        dir, and enables the unit WITHOUT starting; then the long boot runs
        unlocked (``systemctl start`` + an sshd-banner wait against the VM's
        routed address). The ordinal-dependent payloads ship as single
        templates whose placeholder tokens the box substitutes from the chosen
        ordinal.

        Raises ``SliceCapacityError`` when a box budget, the port range, or the
        carve-time df guard refuses; cleans up a half-reserved slice on any
        other failure.
        """
        if units is None or box_total_units is None or box_disk_budget_gib is None:
            raise BareMetalProvisioningError(
                "units / box_total_units / box_disk_budget_gib must all be set to carve a gen-2 machine "
                "(they are computed per box by the operator pool bake)"
            )
        if memory_mib != compute_machine_guest_memory_mib(units):
            raise BareMetalProvisioningError(
                f"memory_mib={memory_mib} disagrees with units={units} (the guest boots with units x 1024 MiB "
                f"minus the {GUEST_RAM_HOLDBACK_MIB} MiB holdback, {compute_machine_guest_memory_mib(units)})"
            )
        instance_name = slice_instance_name(host_id, env_name)
        static_root_keys = tuple(key for key in (root_authorized_public_key, *extra_root_authorized_keys) if key)
        if not static_root_keys and trusted_user_ca_public_key is None:
            raise BareMetalProvisioningError(
                "a gen-2 slice carve needs a root access path: the tier's SSH CA public key (the norm) or a static "
                "root key to authorize; with neither, the VM would boot unreachable"
            )
        user_data = build_qemu_slice_user_data(
            host_dir=host_dir,
            root_authorized_public_keys=static_root_keys,
            host_private_key_pem=host_private_key_pem,
            host_public_key_openssh=host_public_key_openssh,
            trusted_user_ca_public_key=trusted_user_ca_public_key,
        )
        meta_data = build_qemu_slice_meta_data(instance_name)
        network_config = build_qemu_slice_network_config()
        env_file_template = build_qemu_slice_env_file(
            instance_name=instance_name,
            ordinal=None,
            vcpus=vcpus,
            units=units,
            total_units=box_total_units,
            data_disk_gib=disk_gib,
            vm_ssh_host_port=GEN2_VM_SSH_PORT_PLACEHOLDER,
            container_ssh_host_port=GEN2_CONTAINER_SSH_PORT_PLACEHOLDER,
            uplink_mbps=uplink_mbps,
        )
        reserve_script = build_qemu_reserve_script(
            instance_name=instance_name,
            boot_disk_gib=boot_disk_gib,
            data_disk_gib=disk_gib,
            units=units,
            unit_budget_mib=box_total_units * 1024,
            disk_budget_gib=box_disk_budget_gib,
            port_range_start=port_range_start,
            port_range_end=port_range_end,
            user_data_text=user_data,
            meta_data_text=meta_data,
            network_config_text=network_config,
            env_file_template_text=env_file_template,
        )

        # Phase 1: reserve under the box lock (one SSH command; lock auto-releases).
        reserve_rc, reserve_out, reserve_err = self._run_script_on_box(
            reserve_script, timeout=_RESERVE_TIMEOUT_SECONDS, label=f"reserve:{instance_name}", is_streaming=True
        )
        if reserve_rc != 0:
            if GEN2_NO_UNITS_MARKER in reserve_err:
                raise SliceCapacityError(
                    f"bare-metal box {self.box_address} has no free memory units for a "
                    f"{units}-unit machine: {reserve_err.strip()}"
                )
            if GEN2_NO_DISK_MARKER in reserve_err:
                raise SliceCapacityError(
                    f"bare-metal box {self.box_address} has no free disk budget for a machine with a "
                    f"{disk_gib}GiB data disk: {reserve_err.strip()}"
                )
            if GEN2_NO_PORTS_MARKER in reserve_err:
                raise SliceCapacityError(
                    f"no free host ports on box {self.box_address} to reserve a slice: {reserve_err.strip()}"
                )
            if GEN2_NO_SPACE_MARKER in reserve_err:
                raise SliceCapacityError(
                    f"storage filesystem on box {self.box_address} lacks real free space for a slice "
                    f"(the budget model says there is room -- something outside it is consuming disk): "
                    f"{reserve_err.strip()}"
                )
            self._best_effort_destroy(instance_name)
            raise SliceCommandError("reserve", reserve_rc or 1, reserve_err)

        # Phase 2: boot (lock released) and wait for the guest's sshd. The
        # marker parse sits inside the cleanup scope too: a rc-0 reserve with
        # garbled output has still claimed the slot and must not leak it.
        try:
            vm_ssh_host_port, container_ssh_host_port, ordinal = parse_gen2_reserved_line(reserve_out)
            start_rc, _start_out, start_err = self.run_on_box(
                build_qemu_start_command(ordinal),
                timeout=_START_TIMEOUT_SECONDS,
                label=f"start:{instance_name}",
                is_streaming=True,
            )
            if start_rc != 0:
                raise SliceCommandError("start", start_rc or 1, start_err)
            wait_rc, _wait_out, wait_err = self._run_script_on_box(
                build_qemu_boot_wait_script(ordinal=ordinal, timeout_seconds=_BOOT_WAIT_TIMEOUT_SECONDS),
                timeout=float(_BOOT_WAIT_TIMEOUT_SECONDS + 120),
                label=f"boot-wait:{instance_name}",
            )
            if wait_rc != 0:
                raise SliceCommandError("boot-wait", wait_rc or 1, wait_err)
        except (SliceCommandError, BareMetalProvisioningError, OSError):
            self._best_effort_destroy(instance_name)
            raise

        logger.info(
            "Provisioned gen-2 slice VM {} (ordinal {}, ports vm={}/container={}) on {}",
            instance_name,
            ordinal,
            vm_ssh_host_port,
            container_ssh_host_port,
            self.box_address,
        )
        return SliceProvisionResult(
            instance_name=instance_name,
            disk_name=gen2_disk_name(instance_name),
            vm_ssh_host_port=vm_ssh_host_port,
            container_ssh_host_port=container_ssh_host_port,
            slice_ordinal=ordinal,
        )

    def destroy_instance(self, instance_id: VpsInstanceId) -> None:
        """Stop + disable the slice's unit and remove its on-disk state (frees the box slot)."""
        destroy_rc, _out, destroy_err = self._run_script_on_box(
            build_qemu_destroy_script(str(instance_id)), timeout=_SHORT_TIMEOUT_SECONDS, label="destroy"
        )
        if destroy_rc != 0:
            raise SliceCommandError("destroy", destroy_rc or 1, destroy_err)
        logger.info("Destroyed gen-2 slice VM {} on {}", instance_id, self.box_address)

    def list_instance_names(self) -> set[str]:
        """Return the names of all gen-2 slice instances currently on the box."""
        list_rc, list_out, list_err = self.run_on_box(
            build_qemu_list_instances_command(), timeout=_SHORT_TIMEOUT_SECONDS, label="list"
        )
        if list_rc != 0:
            raise SliceCommandError("list", list_rc or 1, list_err)
        return {line.strip() for line in list_out.splitlines() if line.strip()}

    def list_instance_observations(self) -> tuple[SliceInstanceObservation, ...]:
        """Every gen-2 instance on the box with its unit state and age, from one box round-trip."""
        observe_rc, observe_out, observe_err = self.run_on_box(
            build_qemu_list_instance_observations_command(), timeout=_SHORT_TIMEOUT_SECONDS, label="observe"
        )
        if observe_rc != 0:
            raise SliceCommandError("observe", observe_rc or 1, observe_err)
        try:
            return parse_slice_instance_observations(observe_out)
        except MalformedBoxOutputError as e:
            raise SliceCommandError("observe", 1, str(e)) from e

    def list_disk_names(self) -> set[str]:
        """Return the data-disk identifiers (instance + suffix) of every slice holding a data disk."""
        # `continue` rather than `&&`: on a box with no slices the glob stays
        # literal, and a trailing false test would exit the loop (and the whole
        # remote command) non-zero instead of reporting an empty listing.
        command = (
            f"for d in {GEN2_INSTANCES_DIR}/*/datadisk.qcow2; "
            'do [ -e "$d" ] || continue; basename "$(dirname "$d")"; done'
        )
        list_rc, list_out, list_err = self.run_on_box(command, timeout=_SHORT_TIMEOUT_SECONDS, label="disk-list")
        if list_rc != 0:
            raise SliceCommandError("disk list", list_rc or 1, list_err)
        return {gen2_disk_name(line.strip()) for line in list_out.splitlines() if line.strip()}

    def destroy_disk(self, disk_name: str) -> None:
        """Delete a slice's data disk file, tolerating it already being absent."""
        instance_name = disk_name.removesuffix(GEN2_DISK_SUFFIX)
        command = f"rm -f {shlex.quote(f'{gen2_instance_dir(instance_name)}/datadisk.qcow2')}"
        delete_rc, _out, delete_err = self.run_on_box(command, timeout=_SHORT_TIMEOUT_SECONDS, label="disk-delete")
        if delete_rc != 0:
            raise SliceCommandError("disk delete", delete_rc or 1, delete_err)
        logger.info("Destroyed orphan slice disk {} on {}", disk_name, self.box_address)

    def read_management_trust(self) -> BoxManagementTrust:
        read_rc, read_out, read_err = self.run_on_box(
            build_read_management_trust_command(), timeout=_SHORT_TIMEOUT_SECONDS, label="read-management-trust"
        )
        if read_rc != 0:
            raise BareMetalProvisioningError(
                f"could not read the management trust material for {self.box_ssh_user} on {self.box_address} "
                f"(exit {read_rc}): {read_err.strip()}"
            )
        return parse_management_trust_output(read_out)

    def read_box_health_texts(self) -> tuple[str, str]:
        """Return the box's ``/proc/mdstat`` and ``/proc/swaps`` contents in one round-trip."""
        read_rc, read_out, read_err = self.run_on_box(
            f"cat /proc/mdstat && echo {_BOX_HEALTH_SPLIT_MARKER} && cat /proc/swaps",
            timeout=_SHORT_TIMEOUT_SECONDS,
            label="read-box-health",
        )
        if read_rc != 0:
            raise BareMetalProvisioningError(
                f"could not read /proc/mdstat + /proc/swaps on {self.box_address} (exit {read_rc}): {read_err.strip()}"
            )
        mdstat_text, _, proc_swaps_text = read_out.partition(f"{_BOX_HEALTH_SPLIT_MARKER}\n")
        return mdstat_text, proc_swaps_text

    def read_storage_volume_state(self) -> StorageVolumeState:
        read_rc, read_out, read_err = self.run_on_box(
            build_read_storage_volume_command(), timeout=_SHORT_TIMEOUT_SECONDS, label="read-storage-volume"
        )
        if read_rc != 0:
            raise BareMetalProvisioningError(
                f"could not read the storage volume state on {self.box_address} (exit {read_rc}): {read_err.strip()}"
            )
        return parse_storage_volume_output(read_out)

    def get_instance_status(self, instance_id: VpsInstanceId) -> VpsInstanceStatus:
        status_rc, status_out, status_err = self.run_on_box(
            build_qemu_status_command(str(instance_id)), timeout=_SHORT_TIMEOUT_SECONDS, label="status"
        )
        if status_rc != 0:
            raise SliceCommandError("status", status_rc or 1, status_err)
        report = status_out.strip().splitlines()[-1].strip() if status_out.strip() else ""
        if report == "absent":
            return VpsInstanceStatus.UNKNOWN
        return _STATUS_BY_REPORT.get(report, VpsInstanceStatus.UNKNOWN)

    def get_instance_ip(self, instance_id: VpsInstanceId) -> str:
        # The slice's sshd is DNAT-forwarded on the box's interface; external
        # consumers (and the laptop-side bake) reach it at the box's address.
        return self.box_address

    def _unavailable(self, operation: str) -> NotImplementedError:
        return NotImplementedError(
            f"QemuSliceVpsClient does not support '{operation}': slice VMs are provisioned via "
            "provision_slice_vm() and torn down via destroy_instance(); they have no cloud ordering, "
            "snapshot, or ssh-key API."
        )

    def create_instance(
        self,
        label: str,
        region: str,
        plan: str,
        user_data: str,
        ssh_key_ids: Sequence[str],
        tags: Mapping[str, str],
    ) -> VpsInstanceId:
        raise self._unavailable("create_instance")

    def upload_ssh_key(self, name: str, public_key: str) -> str:
        raise self._unavailable("upload_ssh_key")

    def delete_ssh_key(self, key_id: str) -> None:
        raise self._unavailable("delete_ssh_key")

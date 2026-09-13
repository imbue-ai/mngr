import shlex
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.slices.gen2_scripts.errors import MalformedBoxOutputError
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_BY_ORDINAL_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_GATEWAY_IP_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_INSTANCES_DIR
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_MAC_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_DISK_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_NO_UNITS_MARKER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_ORDINAL_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SLICE_SUBNET_BASE
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_VM_IP_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import derive_slice_network
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import gen2_instance_dir
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import slice_mac_address
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import slice_tap_name
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import slice_unix_user
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import DEFAULT_MACHINE_UNITS
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import GEN2_BOOT_DISK_GIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import PER_VM_RAM_OVERHEAD_MIB
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_data_disk_gib
from imbue.mngr_imbue_cloud.slices.gen2_scripts.sizing import compute_machine_guest_memory_mib


class SliceInstanceObservation(FrozenModel):
    """One slice VM instance as seen on its box: whether its unit is running and how long it has existed."""

    instance_name: str = Field(description="Slice VM instance name (also the VpsInstanceId)")
    is_active: bool = Field(description="Whether the instance's VM is running right now (its unit is active)")
    age_seconds: float = Field(
        ge=0, description="Seconds since the instance's on-box state was created, by the box's clock"
    )


# Per-slice env file (consumed by the template unit and the root helper)


@pure
def build_qemu_slice_env_file(
    *,
    instance_name: str,
    # None renders the on-box *template*: every ordinal-derived value becomes
    # its placeholder token, substituted on the box under the reservation lock
    # (the ordinal is chosen there).
    ordinal: int | None,
    vcpus: int,
    units: int,
    total_units: int,
    data_disk_gib: int,
    vm_ssh_host_port: int | str,
    container_ssh_host_port: int | str,
    uplink_mbps: int | None,
) -> str:
    """The per-slice ``env`` file: every per-VM value the unit and helper need.

    ``units`` is the machine's size (1 unit = 1GiB guest RAM; the qemu ``-m``
    value is derived from it here so the two can never disagree) and
    ``total_units`` the box's sellable unit budget (the helper's fair-share
    denominator). ``data_disk_gib`` is recorded so the reserve scripts can sum
    the box's disk budget from the recorded env files. The two host ports may
    be placeholder tokens (str) when the file is rendered as a template whose
    ports are substituted on the box under the reservation lock. An empty
    ``MNGR_SLICE_UPLINK_MBPS`` disables fair-share traffic shaping for this VM.
    """
    if ordinal is None:
        ordinal_text: str = GEN2_ORDINAL_PLACEHOLDER
        mac = GEN2_MAC_PLACEHOLDER
        tap = f"mslice{GEN2_ORDINAL_PLACEHOLDER}"
        unix_user = f"mngr-slice-{GEN2_ORDINAL_PLACEHOLDER}"
        vm_ip = GEN2_VM_IP_PLACEHOLDER
        gateway_ip = GEN2_GATEWAY_IP_PLACEHOLDER
    else:
        network = derive_slice_network(ordinal)
        ordinal_text = str(ordinal)
        mac = slice_mac_address(ordinal)
        tap = slice_tap_name(ordinal)
        unix_user = slice_unix_user(ordinal)
        vm_ip = network.vm_ip
        gateway_ip = network.gateway_ip
    lines = [
        f"MNGR_SLICE_INSTANCE={instance_name}",
        f"MNGR_SLICE_ORDINAL={ordinal_text}",
        f"MNGR_SLICE_VCPUS={vcpus}",
        f"MNGR_SLICE_UNITS={units}",
        f"MNGR_SLICE_TOTAL_UNITS={total_units}",
        f"MNGR_SLICE_MEMORY_MIB={compute_machine_guest_memory_mib(units)}",
        f"MNGR_SLICE_DATA_DISK_GIB={data_disk_gib}",
        f"MNGR_SLICE_MAC={mac}",
        f"MNGR_SLICE_TAP={tap}",
        f"MNGR_SLICE_USER={unix_user}",
        f"MNGR_SLICE_VM_IP={vm_ip}",
        f"MNGR_SLICE_GATEWAY_IP={gateway_ip}",
        "MNGR_SLICE_PREFIX_LENGTH=30",
        f"MNGR_SLICE_VM_SSH_HOST_PORT={vm_ssh_host_port}",
        f"MNGR_SLICE_CONTAINER_SSH_HOST_PORT={container_ssh_host_port}",
        f"MNGR_SLICE_UPLINK_MBPS={uplink_mbps if uplink_mbps is not None else ''}",
    ]
    return "\n".join(lines) + "\n"


# Bash blocks shared by the reserve and resize scripts


@pure
def render_gen2_budget_guard_lines(
    *,
    # The new machine's size in units and data-disk GiB.
    units: int,
    data_disk_gib: int,
    # The box's two budgets: memory (MiB, see ``compute_box_unit_budget_mib``)
    # and disk (GiB, see ``compute_gen2_disk_budget_gib``).
    unit_budget_mib: int,
    disk_budget_gib: int,
    # Bash arithmetic excluding one instance dir from the sums (the in-place
    # resize re-checks capacity for a machine that already holds a slot);
    # empty string sums every recorded env file.
    excluded_instance_name: str,
) -> str:
    """The bash block that enforces the two-budget capacity accounting under the lock.

    Sums the recorded per-slice env files (memory: ``units x 1024`` plus the
    per-VM overhead; disk: the boot disk plus the recorded data-disk GiB) and
    refuses with the ``NO_UNITS`` / ``NO_DISK`` markers when the new machine
    does not fit. Env files predating the sizing columns fall back to the
    default machine size so a mixed box stays conservatively counted.
    """
    default_data_disk_gib = compute_machine_data_disk_gib(DEFAULT_MACHINE_UNITS)
    new_footprint_mib = units * 1024 + PER_VM_RAM_OVERHEAD_MIB
    exclusion_line = (
        f'    [ "$(basename "$(dirname "$env_file")")" = {shlex.quote(excluded_instance_name)} ] && continue\n'
        if excluded_instance_name
        else ""
    )
    return f"""\
used_budget_mib=0
used_disk_gib=0
for env_file in "$INSTANCES_DIR"/*/env; do
    [ -e "$env_file" ] || continue
{exclusion_line}\
    inst_units=$(grep -s '^MNGR_SLICE_UNITS=' "$env_file" | cut -d= -f2 || true)
    case "$inst_units" in ''|*[!0-9]*) inst_units={DEFAULT_MACHINE_UNITS} ;; esac
    inst_disk=$(grep -s '^MNGR_SLICE_DATA_DISK_GIB=' "$env_file" | cut -d= -f2 || true)
    case "$inst_disk" in ''|*[!0-9]*) inst_disk={default_data_disk_gib} ;; esac
    used_budget_mib=$(( used_budget_mib + inst_units * 1024 + {PER_VM_RAM_OVERHEAD_MIB} ))
    used_disk_gib=$(( used_disk_gib + {GEN2_BOOT_DISK_GIB} + inst_disk ))
done
if [ $(( used_budget_mib + {new_footprint_mib} )) -gt {unit_budget_mib} ]; then
    echo "{GEN2_NO_UNITS_MARKER} used_mib=$used_budget_mib requested_mib={new_footprint_mib} budget_mib={unit_budget_mib}" >&2
    exit 4
fi
if [ $(( used_disk_gib + {GEN2_BOOT_DISK_GIB} + {data_disk_gib} )) -gt {disk_budget_gib} ]; then
    echo "{GEN2_NO_DISK_MARKER} used_gib=$used_disk_gib requested_gib={GEN2_BOOT_DISK_GIB + data_disk_gib} budget_gib={disk_budget_gib}" >&2
    exit 8
fi
"""


@pure
def render_gen2_ordinal_derivation_lines() -> str:
    """The bash block deriving a chosen ``$ordinal``'s MAC and /30 addresses.

    The shell-arithmetic twin of :func:`slice_mac_address` and
    :func:`derive_slice_network`, so ONE env-file template (with placeholder
    tokens) ships to the box instead of a per-candidate-ordinal payload table.
    """
    subnet_first_two_octets = ".".join(GEN2_SLICE_SUBNET_BASE.split(".")[:2])
    return f"""\
mac=$(printf '52:54:00:6d:%02x:%02x' $(( ordinal >> 8 )) $(( ordinal & 255 )))
address_offset=$(( 4 * ordinal ))
vm_ip="{subnet_first_two_octets}.$(( address_offset >> 8 )).$(( (address_offset & 255) + 2 ))"
gateway_ip="{subnet_first_two_octets}.$(( address_offset >> 8 )).$(( (address_offset & 255) + 1 ))"
substitute_ordinal_tokens() {{
    sed -e "s/{GEN2_ORDINAL_PLACEHOLDER}/$ordinal/g" \\
        -e "s/{GEN2_MAC_PLACEHOLDER}/$mac/g" \\
        -e "s/{GEN2_VM_IP_PLACEHOLDER}/$vm_ip/g" \\
        -e "s/{GEN2_GATEWAY_IP_PLACEHOLDER}/$gateway_ip/g"
}}
"""


# Box commands (destroy / listing)


@pure
def build_qemu_destroy_script(instance_name: str) -> str:
    """The box command sequence that tears down a gen-2 slice and frees its slot.

    Idempotent: tolerates the instance dir, unit, or ordinal link already being
    gone (a carve can fail between steps), so a re-run after a partial teardown
    converges. The unit's ``ExecStopPost`` removes the tap and rules; this
    removes the systemd registration and the on-disk state.
    """
    quoted_dir = shlex.quote(gen2_instance_dir(instance_name))
    return f"""\
set -u
slice_dir={quoted_dir}
ordinal=$(grep -s '^MNGR_SLICE_ORDINAL=' "$slice_dir/env" 2>/dev/null | cut -d= -f2 || true)
if [ -n "$ordinal" ]; then
    sudo /usr/bin/systemctl stop "mngr-slice@$ordinal" || true
    sudo /usr/bin/systemctl disable "mngr-slice@$ordinal" || true
    sudo /usr/bin/systemctl reset-failed "mngr-slice@$ordinal" 2>/dev/null || true
    rm -f "{GEN2_BY_ORDINAL_DIR}/$ordinal"
fi
rm -rf "$slice_dir"
"""


@pure
def build_qemu_list_instances_command() -> str:
    """The box command listing every gen-2 slice instance name (one per line)."""
    return f"ls -1 {GEN2_INSTANCES_DIR} 2>/dev/null || true"


GEN2_OBSERVATION_NOW_MARKER: Final[str] = "MNGR_SLICE_NOW"


@pure
def build_qemu_list_instance_observations_command() -> str:
    """The box command reporting every gen-2 instance's unit state and creation time in one round-trip.

    Prints ``MNGR_SLICE_NOW <epoch>`` (the box's clock, so the caller's clock skew
    cannot mislead an age check) and then one ``<instance> <unit-state> <mtime-epoch>``
    line per instance dir; the unit state is ``systemctl is-active``'s word
    (``active`` for a running VM) or ``unknown`` when the dir has no ordinal yet
    (a carve between its ``mkdir`` and its env-file write).
    """
    return (
        f'echo "{GEN2_OBSERVATION_NOW_MARKER} $(date +%s)"; '
        f"for d in {GEN2_INSTANCES_DIR}/*/; do "
        '[ -d "$d" ] || continue; '
        'name=$(basename "$d"); '
        "ordinal=$(grep -s '^MNGR_SLICE_ORDINAL=' \"$d/env\" | cut -d= -f2); "
        'if [ -n "$ordinal" ]; then state=$(systemctl is-active "mngr-slice@$ordinal" 2>/dev/null || true); '
        "else state=unknown; fi; "
        'echo "$name ${state:-unknown} $(stat -c %Y "$d")"; '
        "done; true"
    )


@pure
def parse_slice_instance_observations(output: str) -> tuple[SliceInstanceObservation, ...]:
    """Parse :func:`build_qemu_list_instance_observations_command` output.

    Raises :class:`MalformedBoxOutputError` on a malformed line: the command is
    ours, so anything unparseable means the box is not in the state this code
    assumes.
    """
    now_epoch: float | None = None
    observations: list[SliceInstanceObservation] = []
    for raw_line in output.splitlines():
        parts = raw_line.strip().split()
        if not parts:
            continue
        if parts[0] == GEN2_OBSERVATION_NOW_MARKER and len(parts) == 2:
            now_epoch = float(parts[1])
            continue
        if len(parts) != 3 or now_epoch is None:
            raise MalformedBoxOutputError(f"unparseable slice observation line: {raw_line!r}")
        name, state, mtime_text = parts
        try:
            mtime_epoch = float(mtime_text)
        except ValueError:
            raise MalformedBoxOutputError(f"non-numeric mtime in slice observation: {raw_line!r}") from None
        observations.append(
            SliceInstanceObservation(
                instance_name=name, is_active=(state == "active"), age_seconds=max(0.0, now_epoch - mtime_epoch)
            )
        )
    return tuple(observations)

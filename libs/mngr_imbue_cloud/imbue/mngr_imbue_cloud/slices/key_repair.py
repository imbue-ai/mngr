"""Operator fleet sweep repairing slice SSH-key damage from the cidata authorized_keys wipe.

Slices carved before the lima fix (see
``apps/minds/docs/deploy/slice-restart-wipes-owner-ssh-key.md``) carry a truncating
``cat > /root/.ssh/authorized_keys`` step in their stored ``lima.yaml``, so
every VM start wipes the owner's lease-injected key. This sweep, box by box:

1. patches each slice's stored ``lima.yaml`` provision block to the
   append-if-absent form (so future starts stop truncating), and
2. restores the VM root's ``authorized_keys`` from the workspace container's
   own copy (the owner's key survived there -- the container file is not
   touched by cloud-init), via ``limactl shell`` through the box's lima user.

The container copy is the only key source: the sweep never mints or injects
material of its own, so it can repair access the user already had but never
grant anything new. Single-VM scoping is the break-glass mode.
"""

import base64
import shlex
from enum import auto
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.slices.bare_metal import SLICE_LIMA_INSTANCE_PREFIX
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient
from imbue.mngr_lima.lima_yaml import patch_root_authorized_keys_block_in_lima_yaml

_READ_TIMEOUT_SECONDS: Final[float] = 120.0
_WRITE_TIMEOUT_SECONDS: Final[float] = 120.0
_REPAIR_TIMEOUT_SECONDS: Final[float] = 180.0

# Markers the in-VM repair script prints; parsed out of stdout so an SSH-level
# failure stays distinguishable from a repair verdict.
REPAIR_OK_MARKER: Final[str] = "MNGR_KEY_REPAIR_OK"
REPAIR_NO_CONTAINER_MARKER: Final[str] = "MNGR_KEY_REPAIR_NO_CONTAINER"


class SliceKeyRepairStatus(LowerCaseStrEnum):
    """Per-VM outcome of the key-repair sweep, as emitted in the JSON report."""

    REPAIRED = auto()
    PATCHED_YAML_ONLY = auto()
    WOULD_REPAIR = auto()
    FAILED = auto()


class SliceKeyRepairOutcome(FrozenModel):
    """The result of repairing one slice VM."""

    server_id: str = Field(description="The bare_metal_servers row id of the VM's box")
    vm_name: str = Field(description="The slice's lima instance name on the box")
    status: SliceKeyRepairStatus = Field(description="How the VM ended up")
    is_lima_yaml_patched: bool = Field(
        description="Whether this run rewrote the stored lima.yaml (False when it already appends)"
    )
    detail: str | None = Field(default=None, description="Failure description or a repair note")


class SliceKeyRepairReport(FrozenModel):
    """The summary the sweep emits: per-VM outcomes plus counts."""

    repaired: int = Field(description="VMs whose lima.yaml is fixed and whose root authorized_keys was re-asserted")
    patched_yaml_only: int = Field(
        description="VMs whose lima.yaml was patched but whose in-VM repair could not run (e.g. stopped)"
    )
    would_repair: int = Field(description="VMs a non-dry run would touch (dry runs only)")
    failed: int = Field(description="VMs whose repair failed (investigate individually)")
    unreadable_boxes: tuple[str, ...] = Field(
        description="Server ids of boxes that could not be reached (their VMs' state is unknown)"
    )
    vms: tuple[SliceKeyRepairOutcome, ...] = Field(description="Per-VM outcomes")


@pure
def build_vm_root_key_repair_script() -> str:
    """The in-VM script that restores root's authorized_keys from the container's own copy.

    The workspace container's ``/root/.ssh/authorized_keys`` was not touched by
    the cidata replay, so it still carries the owner's lease-injected key; any
    of its key lines missing from the VM root's file are guarded-appended. The
    ``docker cp`` fallback covers a stopped container. Copies only -- the sweep
    never introduces key material of its own.
    """
    return f"""\
set -eu
cid=$(docker ps -aq --filter label=com.imbue.mngr.host-id | head -1)
if [ -z "$cid" ]; then
    echo "{REPAIR_NO_CONTAINER_MARKER}"
    exit 0
fi
container_keys=$(docker exec "$cid" cat /root/.ssh/authorized_keys 2>/dev/null \\
    || docker cp "$cid:/root/.ssh/authorized_keys" - 2>/dev/null | tar -xO 2>/dev/null || true)
mkdir -p /root/.ssh
chmod 700 /root/.ssh
AK=/root/.ssh/authorized_keys
touch "$AK"
if [ -n "$(tail -c1 "$AK")" ]; then printf '\\n' >> "$AK"; fi
added=0
while IFS= read -r line; do
    [ -n "$line" ] || continue
    case "$line" in "#"*) continue ;; esac
    if ! grep -qxF "$line" "$AK"; then
        printf '%s\\n' "$line" >> "$AK"
        added=$((added + 1))
    fi
done <<MNGR_CONTAINER_KEYS
$container_keys
MNGR_CONTAINER_KEYS
chmod 600 "$AK"
chown root:root "$AK"
echo "{REPAIR_OK_MARKER} added=$added"
"""


@pure
def parse_repair_script_output(stdout: str) -> tuple[bool, str | None]:
    """Interpret the repair script's stdout into ``(is_repaired, detail)``.

    Output without either marker counts as a failure (the script always prints
    one, so its absence means the command was cut short).
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(REPAIR_OK_MARKER):
            added_note = stripped.removeprefix(REPAIR_OK_MARKER).strip()
            return True, f"root authorized_keys re-asserted from the container copy ({added_note})"
        elif stripped.startswith(REPAIR_NO_CONTAINER_MARKER):
            return True, "no workspace container on this VM; nothing to copy upward"
        else:
            continue
    return False, f"repair produced no verdict marker: {stdout[-300:]!r}"


def _run_in_vm(
    client: LimaSliceVpsClient, vm_name: str, command: str, *, timeout: float, label: str
) -> tuple[int | None, str, str]:
    """Run a root shell command inside a slice VM through the box's lima user."""
    remote_command = f"limactl shell --workdir / {shlex.quote(vm_name)} sudo bash -c {shlex.quote(command)}"
    return client.run_on_box(remote_command, timeout=timeout, label=label)


def _write_lima_yaml_on_box(client: LimaSliceVpsClient, vm_name: str, patched_text: str) -> tuple[bool, str]:
    """Atomically replace the slice's stored lima.yaml on the box; returns (is_ok, error)."""
    encoded = base64.b64encode(patched_text.encode()).decode()
    quoted_vm = shlex.quote(vm_name)
    command = (
        f"printf '%s' {encoded} | base64 -d > $HOME/.lima/{quoted_vm}/lima.yaml.mngr-patch && "
        f"mv $HOME/.lima/{quoted_vm}/lima.yaml.mngr-patch $HOME/.lima/{quoted_vm}/lima.yaml"
    )
    write_rc, _write_out, write_err = client.run_on_box(
        command, timeout=_WRITE_TIMEOUT_SECONDS, label=f"write-lima-yaml-{vm_name}"
    )
    if write_rc != 0:
        return False, write_err.strip() or f"lima.yaml write exited {write_rc}"
    return True, ""


def repair_slice_keys_on_box(
    client: LimaSliceVpsClient,
    server_id: str,
    *,
    is_dry_run: bool,
    only_vm_names: frozenset[str] | None,
) -> list[SliceKeyRepairOutcome]:
    """Patch lima.yaml + repair the VM root authorized_keys for every slice VM of one box.

    ``only_vm_names`` scopes the sweep (the single-VM break-glass mode); None
    sweeps every slice on the box. A VM whose in-VM repair cannot run (e.g. a
    stopped VM the box cannot ``limactl shell`` into) still gets its stored
    lima.yaml patched -- future starts stop truncating -- and is reported as
    ``patched_yaml_only`` so the operator knows the on-disk file is still
    missing the owner's key until the client's next connect heals it.
    """
    outcomes: list[SliceKeyRepairOutcome] = []
    slice_vm_names = sorted(
        name for name in client.list_instance_names() if name.startswith(SLICE_LIMA_INSTANCE_PREFIX)
    )
    if only_vm_names is not None:
        slice_vm_names = [name for name in slice_vm_names if name in only_vm_names]
    for vm_name in slice_vm_names:
        # Read the stored lima.yaml (also the box-reachability probe for this VM).
        read_rc, read_out, read_err = client.run_on_box(
            f"cat $HOME/.lima/{shlex.quote(vm_name)}/lima.yaml",
            timeout=_READ_TIMEOUT_SECONDS,
            label=f"read-lima-yaml-{vm_name}",
        )
        if read_rc != 0:
            outcomes.append(
                SliceKeyRepairOutcome(
                    server_id=server_id,
                    vm_name=vm_name,
                    status=SliceKeyRepairStatus.FAILED,
                    is_lima_yaml_patched=False,
                    detail=f"could not read the VM's lima.yaml: {read_err.strip() or f'read exited {read_rc}'}",
                )
            )
            continue
        patched_text = patch_root_authorized_keys_block_in_lima_yaml(read_out)

        if is_dry_run:
            outcomes.append(
                SliceKeyRepairOutcome(
                    server_id=server_id,
                    vm_name=vm_name,
                    status=SliceKeyRepairStatus.WOULD_REPAIR,
                    is_lima_yaml_patched=False,
                    detail="lima.yaml needs the append-if-absent patch"
                    if patched_text is not None
                    else "lima.yaml already appends; only the in-VM repair would run",
                )
            )
            continue

        is_yaml_patched = False
        if patched_text is not None:
            is_write_ok, write_error = _write_lima_yaml_on_box(client, vm_name, patched_text)
            if not is_write_ok:
                outcomes.append(
                    SliceKeyRepairOutcome(
                        server_id=server_id,
                        vm_name=vm_name,
                        status=SliceKeyRepairStatus.FAILED,
                        is_lima_yaml_patched=False,
                        detail=f"could not write the patched lima.yaml: {write_error}",
                    )
                )
                continue
            is_yaml_patched = True

        # Restore the VM root's authorized_keys from the container's own copy.
        repair_rc, repair_out, repair_err = _run_in_vm(
            client,
            vm_name,
            build_vm_root_key_repair_script(),
            timeout=_REPAIR_TIMEOUT_SECONDS,
            label=f"repair-keys-{vm_name}",
        )
        if repair_rc != 0:
            not_running_detail = (
                f"in-VM repair could not run ({repair_err.strip() or f'exited {repair_rc}'}); "
                "the owner's key stays missing until the VM starts and the owner reconnects"
            )
            outcomes.append(
                SliceKeyRepairOutcome(
                    server_id=server_id,
                    vm_name=vm_name,
                    status=SliceKeyRepairStatus.PATCHED_YAML_ONLY if is_yaml_patched else SliceKeyRepairStatus.FAILED,
                    is_lima_yaml_patched=is_yaml_patched,
                    detail=not_running_detail,
                )
            )
            continue
        is_repaired, repair_detail = parse_repair_script_output(repair_out)
        if not is_repaired:
            outcomes.append(
                SliceKeyRepairOutcome(
                    server_id=server_id,
                    vm_name=vm_name,
                    status=SliceKeyRepairStatus.FAILED,
                    is_lima_yaml_patched=is_yaml_patched,
                    detail=repair_detail,
                )
            )
            continue
        logger.info("Repaired slice keys on {} (box {}): {}", vm_name, server_id, repair_detail)
        outcomes.append(
            SliceKeyRepairOutcome(
                server_id=server_id,
                vm_name=vm_name,
                status=SliceKeyRepairStatus.REPAIRED,
                is_lima_yaml_patched=is_yaml_patched,
                detail=repair_detail,
            )
        )
    return outcomes


@pure
def build_key_repair_report(
    outcomes: list[SliceKeyRepairOutcome],
    unreadable_boxes: list[str],
) -> SliceKeyRepairReport:
    return SliceKeyRepairReport(
        repaired=sum(1 for o in outcomes if o.status == SliceKeyRepairStatus.REPAIRED),
        patched_yaml_only=sum(1 for o in outcomes if o.status == SliceKeyRepairStatus.PATCHED_YAML_ONLY),
        would_repair=sum(1 for o in outcomes if o.status == SliceKeyRepairStatus.WOULD_REPAIR),
        failed=sum(1 for o in outcomes if o.status == SliceKeyRepairStatus.FAILED),
        unreadable_boxes=tuple(unreadable_boxes),
        vms=tuple(outcomes),
    )

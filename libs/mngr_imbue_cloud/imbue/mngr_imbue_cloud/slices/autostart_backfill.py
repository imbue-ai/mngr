"""Backfill the volume-gated minds-autostart units onto existing slice VMs.

The merged installer (default-workspace-template ``.mngr/settings.toml``,
``post_host_create_outer_command__extend``) only runs at host create, so slices
baked before the reboot-resilience fixes keep the old racy oneshot until an
operator re-applies it (see minds' ``docs/reboot-resilience-rollout.md`` Step
2). This module is that sweep: box by box, it applies the installer to every
``mngr-slice-*`` VM through the box's lima user (``limactl shell``), with the
one per-VM substitution the runbook calls out -- the services-agent start
script path, which older slices bake at ``/mngr/code/scripts/...`` and newer
ones at ``/home/user/workspace/system/scripts/...``. The path is extracted
from each VM's existing ``/usr/local/sbin/minds-outer-autostart.sh`` rather
than assumed.

The installer text is a verbatim copy of the template's block (kept in sync by
hand; the template is the source of truth). It is idempotent, refuses to run
on a VM whose data volume is not mounted (reported as a per-VM failure to
investigate, never bypassed), and applying it to a healthy running workspace
is a no-op for the user.
"""

import re
import shlex
from enum import auto
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.errors import ImbueCloudError
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient

# The services-agent path the current template bakes; older slices carry
# /mngr/code/scripts/minds_start_services_agent.sh instead, which is why the
# path is extracted per VM and substituted into the installer.
DEFAULT_SERVICES_AGENT_PATH: Final[str] = "/home/user/workspace/system/scripts/minds_start_services_agent.sh"

_SERVICES_AGENT_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"(/\S+/minds_start_services_agent\.sh)")

# Only lima instances with this prefix are slice VMs; anything else on a box
# (nothing today) is left alone.
_SLICE_INSTANCE_PREFIX: Final[str] = "mngr-slice-"

# Reading one small file is quick; the installer itself only writes files and
# enables/starts the path unit (the started unit fires the actual workspace
# start asynchronously), so neither command should run long.
_READ_TIMEOUT_SECONDS: Final[float] = 120.0
_INSTALL_TIMEOUT_SECONDS: Final[float] = 300.0

# Verbatim from default-workspace-template .mngr/settings.toml
# (post_host_create_outer_command__extend of the pool/slice provider blocks);
# update this copy when the template's installer changes.
_AUTOSTART_INSTALLER_SCRIPT: Final[str] = """\
set -eu
cat > /usr/local/sbin/minds-outer-autostart.sh <<'BOOT'
#!/bin/sh
# Start every mngr-managed agent container and relaunch the system-services
# agent inside it (in its full host+agent env, via the shared in-container
# start script). Containers are found by the fixed mngr label, so this
# survives container rebuilds. Triggered by minds-autostart.path once the
# /mngr-btrfs data volume is ready (mngr-internal#266); the wait below makes
# a premature trigger park until the mount actually exists instead of
# failing repeatedly (root-fs debris shadowed under the unmounted mountpoint
# once satisfied the old glob trigger early -- see the field report on
# default-workspace-template#381). Any step that still fails makes the unit
# fail, so breakage is visible in systemd instead of being swallowed.
# `mngr start` is idempotent + flock-serialized, so racing the desktop
# client is safe.
set -u

# Wait for the data volume to actually be mounted: generous hard bound, plus
# a warning when it took suspiciously long. The trigger normally fires only
# after the mount, so any wait at all here is unusual; on slices lima can
# take many minutes to mount the disk on boot. Type=oneshot has no start
# timeout, so systemd will not kill this wait.
WAITED=0
until mountpoint -q "$(readlink -f /mngr-btrfs)"; do
    if [ "$WAITED" -ge 1800 ]; then
        echo "timed out after ${WAITED}s waiting for /mngr-btrfs to be mounted" >&2
        exit 1
    fi
    sleep 5
    WAITED=$((WAITED + 5))
done
if [ "$WAITED" -gt 60 ]; then
    echo "warning: waited ${WAITED}s for /mngr-btrfs to be mounted" >&2
fi

FAILED=0
for cid in $(docker ps -aq --filter "label=com.imbue.mngr.host-id"); do
    docker start "$cid" >/dev/null
    if [ "$(docker inspect -f '{{.State.Running}}' "$cid" 2>/dev/null)" != "true" ]; then
        echo "agent container $cid did not reach running" >&2
        FAILED=1
        continue
    fi
    if ! docker exec --workdir / "$cid" bash -lc 'exec /home/user/workspace/system/scripts/minds_start_services_agent.sh'; then
        echo "relaunching the services agent in container $cid failed" >&2
        FAILED=1
    fi
done
exit "$FAILED"
BOOT
chmod +x /usr/local/sbin/minds-outer-autostart.sh
cat > /etc/systemd/system/minds-autostart.service <<'UNIT'
[Unit]
Description=Start the minds system-services agent once the data volume is up
After=docker.service
Requires=docker.service
# Never rate-limit starts: while the trigger condition holds, a failing
# service re-triggers immediately, and hitting the default start limit
# propagates unit-start-limit-hit to minds-autostart.path -- permanently
# disabling the watcher (observed on a staging box reboot; see the field
# report on default-workspace-template#381).
StartLimitIntervalSec=0

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/minds-outer-autostart.sh
UNIT
cat > /etc/systemd/system/minds-autostart.path <<'UNIT'
[Unit]
Description=Watch for the mngr data volume and start the minds workspace

[Path]
# The marker file is created exclusively by this installer, behind a hard
# mountpoint check, so it can only ever exist on the mounted data volume.
# The bare /mngr-btrfs symlink persists (dangling) across reboots, and even
# entries under it cannot be trusted: root-fs debris shadowed beneath the
# unmounted mountpoint (a stale snapshot-helper request once mkdir'd
# /mngr-btrfs/snapshots pre-mount) defeated the previous
# PathExistsGlob=/mngr-btrfs/* condition and fired the service before the
# volume existed. The wake-up event is the symlink recreation the provision
# script performs right after mounting on each boot.
PathExists=/mngr-btrfs/.minds-volume-ready
Unit=minds-autostart.service

[Install]
WantedBy=multi-user.target
UNIT
# The readiness marker the path unit watches may only ever be created here,
# behind a hard mount check, so it can never appear on the unmounted root fs
# (the way shadow debris under the mountpoint defeated the old glob trigger).
if ! mountpoint -q "$(readlink -f /mngr-btrfs)"; then
    echo "refusing to install minds-autostart: /mngr-btrfs is not a mounted volume" >&2
    exit 1
fi
touch /mngr-btrfs/.minds-volume-ready
# The service is only ever triggered by the path unit, so it cannot run before
# the data volume exists. Remove any direct boot enablement left by the
# previous installer (a no-op on fresh VMs), revive units a past boot may have
# left dead of a start-rate limit (re-running this installer is the fleet
# backfill path), then enable + start the watcher -- starting it now makes the
# fix take effect without a reboot: the marker is already visible at install
# time, so the service fires immediately (safe: `docker start` and
# `mngr start` are no-ops on a running workspace).
rm -f /etc/systemd/system/multi-user.target.wants/minds-autostart.service
systemctl daemon-reload
systemctl reset-failed minds-autostart.path minds-autostart.service 2>/dev/null || true
systemctl enable minds-autostart.path
systemctl start minds-autostart.path
"""


class AutostartScriptReadError(ImbueCloudError):
    """Raised when a slice VM's existing autostart script cannot be read (the VM is likely unreachable)."""


class SliceAutostartBackfillStatus(LowerCaseStrEnum):
    """Per-VM outcome of the autostart backfill, as emitted in the JSON report."""

    BACKFILLED = auto()
    WOULD_BACKFILL = auto()
    FAILED = auto()


class SliceAutostartBackfillOutcome(FrozenModel):
    """The result of backfilling one slice VM."""

    server_id: str = Field(description="The bare_metal_servers row id of the VM's box")
    vm_name: str = Field(description="The slice's lima instance name on the box")
    status: SliceAutostartBackfillStatus = Field(description="How the VM ended up")
    services_agent_path: str | None = Field(
        default=None, description="The per-VM start-script path the installer was (or would be) rendered with"
    )
    detail: str | None = Field(default=None, description="Failure description (failed only)")


class SliceAutostartBackfillReport(FrozenModel):
    """The summary the backfill emits: per-VM outcomes plus counts."""

    backfilled: int = Field(description="VMs the installer was applied to and verified on")
    failed: int = Field(description="VMs whose backfill failed (investigate individually)")
    would_backfill: int = Field(description="VMs a non-dry run would apply to (dry runs only)")
    unreadable_boxes: tuple[str, ...] = Field(
        description="Server ids of boxes that could not be reached (their VMs' state is unknown)"
    )
    vms: tuple[SliceAutostartBackfillOutcome, ...] = Field(description="Per-VM outcomes")


@pure
def extract_services_agent_path(autostart_script_text: str) -> str | None:
    """Pull the services-agent start-script path out of an existing autostart script.

    Matches any absolute path ending in ``minds_start_services_agent.sh`` (the
    one line every installer generation has carried), so both the old
    ``/mngr/code/scripts/...`` and the current layout resolve.
    """
    match = _SERVICES_AGENT_PATH_PATTERN.search(autostart_script_text)
    return match.group(1) if match is not None else None


@pure
def build_autostart_installer_script(services_agent_path: str) -> str:
    """Render the installer with the VM's own services-agent path substituted in."""
    return _AUTOSTART_INSTALLER_SCRIPT.replace(DEFAULT_SERVICES_AGENT_PATH, services_agent_path)


def _run_in_vm(
    client: LimaSliceVpsClient, vm_name: str, command: str, *, timeout: float, label: str
) -> tuple[int | None, str, str]:
    """Run a root shell command inside a slice VM through the box's lima user."""
    remote_command = f"limactl shell --workdir / {shlex.quote(vm_name)} sudo bash -c {shlex.quote(command)}"
    return client.run_on_box(remote_command, timeout=timeout, label=label)


def _resolve_vm_services_agent_path(client: LimaSliceVpsClient, vm_name: str) -> str:
    """The VM's existing start-script path, else the current template default.

    A VM without the script (or one whose script carries no recognizable path)
    was never autostart-installed at all; the current layout is the only
    sensible rendering for it, and the installer is safe either way.

    Raises :class:`AutostartScriptReadError` when the read command itself
    fails: the in-VM command ends with ``|| true``, so a non-zero exit means
    the ``limactl shell`` / box SSH invocation broke (e.g. a stopped VM), and
    defaulting there could render an old-layout VM's installer with the wrong
    path.
    """
    returncode, stdout, stderr = _run_in_vm(
        client,
        vm_name,
        "cat /usr/local/sbin/minds-outer-autostart.sh 2>/dev/null || true",
        timeout=_READ_TIMEOUT_SECONDS,
        label=f"read-autostart-{vm_name}",
    )
    if returncode != 0:
        raise AutostartScriptReadError(
            f"could not read {vm_name}'s existing autostart script: "
            f"{stderr.strip() or f'read command exited {returncode}'}"
        )
    extracted_path = extract_services_agent_path(stdout)
    return extracted_path if extracted_path is not None else DEFAULT_SERVICES_AGENT_PATH


def backfill_box_autostart(
    client: LimaSliceVpsClient,
    server_id: str,
    *,
    is_dry_run: bool,
) -> list[SliceAutostartBackfillOutcome]:
    """Apply (or, dry, plan) the autostart installer on every slice VM of one box."""
    outcomes: list[SliceAutostartBackfillOutcome] = []
    slice_vm_names = sorted(name for name in client.list_instance_names() if name.startswith(_SLICE_INSTANCE_PREFIX))
    for vm_name in slice_vm_names:
        # A VM whose script cannot even be read (stopped, unreachable) is
        # reported as its own failure rather than aborting the box sweep --
        # or, worse, being rendered with a silently guessed path.
        try:
            services_agent_path = _resolve_vm_services_agent_path(client, vm_name)
        except AutostartScriptReadError as exc:
            logger.warning("Could not plan autostart backfill for {} (box {}): {}", vm_name, server_id, exc)
            outcomes.append(
                SliceAutostartBackfillOutcome(
                    server_id=server_id,
                    vm_name=vm_name,
                    status=SliceAutostartBackfillStatus.FAILED,
                    detail=str(exc),
                )
            )
            continue
        if is_dry_run:
            outcomes.append(
                SliceAutostartBackfillOutcome(
                    server_id=server_id,
                    vm_name=vm_name,
                    status=SliceAutostartBackfillStatus.WOULD_BACKFILL,
                    services_agent_path=services_agent_path,
                )
            )
            continue
        installer_script = build_autostart_installer_script(services_agent_path)
        returncode, _stdout, stderr = _run_in_vm(
            client, vm_name, installer_script, timeout=_INSTALL_TIMEOUT_SECONDS, label=f"install-autostart-{vm_name}"
        )
        if returncode != 0:
            outcomes.append(
                SliceAutostartBackfillOutcome(
                    server_id=server_id,
                    vm_name=vm_name,
                    status=SliceAutostartBackfillStatus.FAILED,
                    services_agent_path=services_agent_path,
                    detail=stderr.strip() or f"installer exited {returncode}",
                )
            )
            continue
        verify_returncode, verify_stdout, verify_stderr = _run_in_vm(
            client,
            vm_name,
            "systemctl is-active minds-autostart.path",
            timeout=_READ_TIMEOUT_SECONDS,
            label=f"verify-autostart-{vm_name}",
        )
        if verify_returncode != 0 or verify_stdout.strip() != "active":
            outcomes.append(
                SliceAutostartBackfillOutcome(
                    server_id=server_id,
                    vm_name=vm_name,
                    status=SliceAutostartBackfillStatus.FAILED,
                    services_agent_path=services_agent_path,
                    detail=(
                        "installer succeeded but minds-autostart.path is not active: "
                        f"{verify_stdout.strip() or verify_stderr.strip() or 'unknown'}"
                    ),
                )
            )
            continue
        logger.info("Backfilled minds-autostart on {} (box {})", vm_name, server_id)
        outcomes.append(
            SliceAutostartBackfillOutcome(
                server_id=server_id,
                vm_name=vm_name,
                status=SliceAutostartBackfillStatus.BACKFILLED,
                services_agent_path=services_agent_path,
            )
        )
    return outcomes


@pure
def build_autostart_backfill_report(
    outcomes: list[SliceAutostartBackfillOutcome],
    unreadable_boxes: list[str],
) -> SliceAutostartBackfillReport:
    return SliceAutostartBackfillReport(
        backfilled=sum(1 for o in outcomes if o.status == SliceAutostartBackfillStatus.BACKFILLED),
        failed=sum(1 for o in outcomes if o.status == SliceAutostartBackfillStatus.FAILED),
        would_backfill=sum(1 for o in outcomes if o.status == SliceAutostartBackfillStatus.WOULD_BACKFILL),
        unreadable_boxes=tuple(unreadable_boxes),
        vms=tuple(outcomes),
    )

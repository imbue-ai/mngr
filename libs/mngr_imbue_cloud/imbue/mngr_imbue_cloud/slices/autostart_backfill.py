"""Backfill the volume-gated minds-autostart units onto existing slice VMs.

The merged installer (default-workspace-template ``.mngr/settings.toml``,
``post_host_create_outer_command__extend``) only runs at host create, so slices
baked before the reboot-resilience fixes keep the old racy oneshot until an
operator re-applies it (see minds' ``docs/reboot-resilience-rollout.md`` Step
2). This module is that sweep: box by box, it applies the installer to every
``mngr-slice-*`` VM through the box's lima user (``limactl shell``).

The installer text is a verbatim copy of the template's block (kept in sync by
hand; the template is the source of truth). It is idempotent, refuses to run
on a VM whose data volume is not mounted (reported as a per-VM failure to
investigate, never bypassed), and applying it to a healthy running workspace
is a no-op for the user. The in-container relaunch step probes the known
per-generation script locations itself, so no per-VM path substitution is
needed; containers that predate the start script entirely degrade to a
container+sshd start with a journal notice (surfaced in the outcome detail).

Applying the units is not enough to know the workspace actually started: the
installer fires the service explicitly (``systemctl restart --no-block``,
because starting the path unit alone never re-runs a service the old
installer's boot-time oneshot left latched active), and the sweep then watches
the service until it observes a run that started after the install and ended
in success -- the watch runs as one in-VM shell loop so each VM costs a single
extra SSH round trip.
"""

import shlex
from enum import auto
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient

# Only lima instances with this prefix are slice VMs; anything else on a box
# (nothing today) is left alone.
_SLICE_INSTANCE_PREFIX: Final[str] = "mngr-slice-"

# Reading one unit property is quick; the installer itself only writes files,
# enables the path unit, and fires the service without waiting for it
# (--no-block), so neither command should run long. The verification command
# is the one that deliberately waits (in-VM) for the fired run to finish.
_READ_TIMEOUT_SECONDS: Final[float] = 120.0
_INSTALL_TIMEOUT_SECONDS: Final[float] = 300.0

# How long the in-VM verification loop waits for a fresh successful service
# run. The data volume is already mounted when verification runs (the
# installer refuses otherwise), so the run should only need docker start +
# `mngr start` -- normally well under a minute.
_VERIFY_DEADLINE_SECONDS: Final[int] = 120
_VERIFY_TIMEOUT_SECONDS: Final[float] = _VERIFY_DEADLINE_SECONDS + 60.0

# Markers the in-VM verification loop prints; the sweep parses them out of
# stdout so an SSH-level failure (non-zero exit) stays distinguishable from a
# service run that failed.
VERIFY_SUCCESS_MARKER: Final[str] = "MNGR_AUTOSTART_VERIFIED"
VERIFY_FAILURE_MARKER: Final[str] = "MNGR_AUTOSTART_FAILED"

# The journal notice the installer's relaunch step emits when the container
# generation predates minds_start_services_agent.sh (see the installer text);
# its presence classifies a successful run as container-start-only.
_SCRIPTLESS_NOTICE_FRAGMENT: Final[str] = "no minds_start_services_agent.sh in this container generation"

# Outcome detail for a run that succeeded via the scriptless fallback.
CONTAINER_START_ONLY_DETAIL: Final[str] = (
    "services-agent script absent in this container generation; container and sshd started, "
    "agent relaunch left to mngr/the desktop client"
)

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
    # Run the in-container start script from whichever location this
    # container's generation carries. A container older than the script
    # itself has none: for those, make sure sshd is up (its entrypoint is a
    # bare keep-alive) and succeed with a journal notice -- a permanently
    # missing script must not fail the unit, because the path unit
    # retriggers a failing oneshot with no rate limit (the unit sets
    # StartLimitIntervalSec=0 for transient failures), which would hot-loop
    # forever on a condition that can never clear.
    # CLEANUP: drop the /mngr/code candidate and the sshd fallback once no
    # leased workspace container predates minds-v0.3.2 (the first version
    # that ships minds_start_services_agent.sh).
    if ! docker exec --workdir / "$cid" bash -lc 'for s in /home/user/workspace/system/scripts/minds_start_services_agent.sh /mngr/code/scripts/minds_start_services_agent.sh; do if test -x "$s"; then exec "$s"; fi; done; mkdir -p /run/sshd; grep -lxs sshd /proc/[0-9]*/comm >/dev/null 2>&1 || /usr/sbin/sshd -o MaxSessions=100 -o MaxStartups=100:30:200 || echo "warning: could not start sshd in this container" >&2; echo "no minds_start_services_agent.sh in this container generation; started container and sshd only" >&2'; then
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
# backfill path), then enable + start the watcher. Starting the path unit
# alone does NOT fire the service when it is already active -- the previous
# installer's boot-time oneshot swallowed its own failures and exited 0, and
# RemainAfterExit=yes then latches the unit active, which is exactly the
# state of a VM recovered from a wedge -- so explicitly restart the service
# to run the start NOW. --no-block keeps this installer fast (the service
# body may legitimately wait minutes for the data-volume mount), and the run
# is safe on a healthy workspace (`docker start` and `mngr start` are
# no-ops).
rm -f /etc/systemd/system/multi-user.target.wants/minds-autostart.service
systemctl daemon-reload
systemctl reset-failed minds-autostart.path minds-autostart.service 2>/dev/null || true
systemctl enable minds-autostart.path
systemctl start minds-autostart.path
systemctl restart --no-block minds-autostart.service
"""

# Reads the monotonic start stamp of the service's last run (microseconds
# since boot; 0 when it never ran or the unit does not exist yet). Taken
# BEFORE the install so the verification loop can demand a strictly newer
# run -- `is-active` alone cannot tell a fresh success from the stale
# latched-active state the old installer leaves behind.
_PRE_STAMP_COMMAND: Final[str] = (
    "systemctl show minds-autostart.service -p ExecMainStartTimestampMonotonic --value 2>/dev/null || echo 0"
)


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
    detail: str | None = Field(
        default=None,
        description="Failure description, or the container-start-only note on a degraded success",
    )


class SliceAutostartBackfillReport(FrozenModel):
    """The summary the backfill emits: per-VM outcomes plus counts."""

    backfilled: int = Field(description="VMs the installer was applied to and whose start run was verified")
    failed: int = Field(description="VMs whose backfill failed (investigate individually)")
    would_backfill: int = Field(description="VMs a non-dry run would apply to (dry runs only)")
    unreadable_boxes: tuple[str, ...] = Field(
        description="Server ids of boxes that could not be reached (their VMs' state is unknown)"
    )
    vms: tuple[SliceAutostartBackfillOutcome, ...] = Field(description="Per-VM outcomes")


class AutostartRunVerdict(FrozenModel):
    """Parsed result of the in-VM verification loop's stdout."""

    is_verified: bool = Field(description="Whether a fresh successful service run was observed")
    is_container_start_only: bool = Field(
        description="Whether the run's journal carries the scriptless-generation notice"
    )
    detail: str | None = Field(default=None, description="The failure line plus journal tail (failures only)")


@pure
def build_autostart_verify_script(pre_install_stamp_microseconds: int) -> str:
    """Render the in-VM loop that waits for a service run newer than the given stamp.

    Runs entirely inside the VM (one SSH round trip; remote operations are
    slow, so the waiting must not be client-side polling). Prints
    ``MNGR_AUTOSTART_VERIFIED notice=<n>`` once a fresh run has succeeded and
    the path unit is active, or ``MNGR_AUTOSTART_FAILED <reason>`` plus a
    journal tail when the run failed or the deadline passed. Both outcomes
    exit 0 -- a non-zero exit from this command means the SSH/limactl
    invocation itself broke, which callers must report separately.
    """
    return f"""\
set -u
pre={pre_install_stamp_microseconds}
waited=0
while :; do
    stamp=$(systemctl show minds-autostart.service -p ExecMainStartTimestampMonotonic --value 2>/dev/null || echo 0)
    active=$(systemctl show minds-autostart.service -p ActiveState --value 2>/dev/null || echo unknown)
    result=$(systemctl show minds-autostart.service -p Result --value 2>/dev/null || echo unknown)
    path_state=$(systemctl is-active minds-autostart.path 2>/dev/null || true)
    if [ "$stamp" -gt "$pre" ] 2>/dev/null; then
        if [ "$active" = "active" ] && [ "$result" = "success" ] && [ "$path_state" = "active" ]; then
            notice=$(journalctl -u minds-autostart -n 40 --no-pager --output=cat 2>/dev/null | grep -c "{_SCRIPTLESS_NOTICE_FRAGMENT}" || true)
            echo "{VERIFY_SUCCESS_MARKER} notice=$notice"
            exit 0
        fi
        if [ "$active" = "failed" ]; then
            echo "{VERIFY_FAILURE_MARKER} the fired minds-autostart run failed"
            journalctl -u minds-autostart -n 8 --no-pager --output=cat 2>/dev/null || true
            exit 0
        fi
    fi
    if [ "$waited" -ge {_VERIFY_DEADLINE_SECONDS} ]; then
        echo "{VERIFY_FAILURE_MARKER} no fresh successful run within {_VERIFY_DEADLINE_SECONDS}s (stamp=$stamp pre=$pre active=$active result=$result path=$path_state)"
        journalctl -u minds-autostart -n 8 --no-pager --output=cat 2>/dev/null || true
        exit 0
    fi
    sleep 2
    waited=$((waited + 2))
done
"""


@pure
def parse_pre_install_stamp(stdout: str) -> int:
    """Parse the microsecond stamp printed by the pre-install read (0 when unparseable)."""
    stripped = stdout.strip()
    return int(stripped) if stripped.isdigit() else 0


@pure
def parse_autostart_verify_output(stdout: str) -> AutostartRunVerdict:
    """Interpret the verification loop's stdout into a verdict.

    Output without either marker counts as a failure (the loop always prints
    one, so its absence means the command was cut short).
    """
    for line_idx, line in enumerate(stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith(VERIFY_SUCCESS_MARKER):
            is_scriptless = "notice=0" not in stripped
            return AutostartRunVerdict(is_verified=True, is_container_start_only=is_scriptless)
        elif stripped.startswith(VERIFY_FAILURE_MARKER):
            reason = stripped.removeprefix(VERIFY_FAILURE_MARKER).strip()
            journal_tail = "\n".join(stdout.splitlines()[line_idx + 1 :]).strip()
            detail = f"{reason}\n{journal_tail}".strip() if journal_tail else reason
            return AutostartRunVerdict(is_verified=False, is_container_start_only=False, detail=detail)
        else:
            continue
    return AutostartRunVerdict(
        is_verified=False,
        is_container_start_only=False,
        detail=f"verification produced no verdict marker: {stdout[-300:]!r}",
    )


def _run_in_vm(
    client: LimaSliceVpsClient, vm_name: str, command: str, *, timeout: float, label: str
) -> tuple[int | None, str, str]:
    """Run a root shell command inside a slice VM through the box's lima user."""
    remote_command = f"limactl shell --workdir / {shlex.quote(vm_name)} sudo bash -c {shlex.quote(command)}"
    return client.run_on_box(remote_command, timeout=timeout, label=label)


def backfill_box_autostart(
    client: LimaSliceVpsClient,
    server_id: str,
    *,
    is_dry_run: bool,
) -> list[SliceAutostartBackfillOutcome]:
    """Apply (or, dry, plan) the autostart installer on every slice VM of one box.

    Each VM costs three SSH round trips: read the service's pre-install run
    stamp (which doubles as the reachability probe -- a wedged or stopped VM
    fails here and is reported rather than guessed about), apply the
    installer, then wait in-VM for a run newer than the stamp to succeed.
    Dry runs stop after the first step.
    """
    outcomes: list[SliceAutostartBackfillOutcome] = []
    slice_vm_names = sorted(name for name in client.list_instance_names() if name.startswith(_SLICE_INSTANCE_PREFIX))
    for vm_name in slice_vm_names:
        # Read the pre-install stamp; a VM this cannot reach (stopped,
        # wedged) is reported as its own failure rather than aborting the
        # box sweep.
        stamp_returncode, stamp_stdout, stamp_stderr = _run_in_vm(
            client,
            vm_name,
            _PRE_STAMP_COMMAND,
            timeout=_READ_TIMEOUT_SECONDS,
            label=f"read-autostart-stamp-{vm_name}",
        )
        if stamp_returncode != 0:
            logger.warning("Could not read {}'s service state (box {}): {}", vm_name, server_id, stamp_stderr.strip())
            outcomes.append(
                SliceAutostartBackfillOutcome(
                    server_id=server_id,
                    vm_name=vm_name,
                    status=SliceAutostartBackfillStatus.FAILED,
                    detail=(
                        f"could not read the VM's service state: "
                        f"{stamp_stderr.strip() or f'stamp read exited {stamp_returncode}'}"
                    ),
                )
            )
            continue
        if is_dry_run:
            outcomes.append(
                SliceAutostartBackfillOutcome(
                    server_id=server_id,
                    vm_name=vm_name,
                    status=SliceAutostartBackfillStatus.WOULD_BACKFILL,
                )
            )
            continue
        pre_install_stamp = parse_pre_install_stamp(stamp_stdout)

        # Apply the installer (writes the script + units, enables the path
        # unit, and fires the service without waiting for it).
        install_returncode, _install_stdout, install_stderr = _run_in_vm(
            client,
            vm_name,
            _AUTOSTART_INSTALLER_SCRIPT,
            timeout=_INSTALL_TIMEOUT_SECONDS,
            label=f"install-autostart-{vm_name}",
        )
        if install_returncode != 0:
            outcomes.append(
                SliceAutostartBackfillOutcome(
                    server_id=server_id,
                    vm_name=vm_name,
                    status=SliceAutostartBackfillStatus.FAILED,
                    detail=install_stderr.strip() or f"installer exited {install_returncode}",
                )
            )
            continue

        # Wait (in-VM) for a service run newer than the pre-install stamp to
        # finish successfully -- this is what proves the workspace start
        # actually happened, not just that the units are on disk.
        verify_returncode, verify_stdout, verify_stderr = _run_in_vm(
            client,
            vm_name,
            build_autostart_verify_script(pre_install_stamp),
            timeout=_VERIFY_TIMEOUT_SECONDS,
            label=f"verify-autostart-{vm_name}",
        )
        if verify_returncode != 0:
            outcomes.append(
                SliceAutostartBackfillOutcome(
                    server_id=server_id,
                    vm_name=vm_name,
                    status=SliceAutostartBackfillStatus.FAILED,
                    detail=(
                        f"installer succeeded but the verification command could not run: "
                        f"{verify_stderr.strip() or f'verification exited {verify_returncode}'}"
                    ),
                )
            )
            continue
        verdict = parse_autostart_verify_output(verify_stdout)
        if not verdict.is_verified:
            outcomes.append(
                SliceAutostartBackfillOutcome(
                    server_id=server_id,
                    vm_name=vm_name,
                    status=SliceAutostartBackfillStatus.FAILED,
                    detail=verdict.detail,
                )
            )
            continue
        logger.info("Backfilled minds-autostart on {} (box {})", vm_name, server_id)
        outcomes.append(
            SliceAutostartBackfillOutcome(
                server_id=server_id,
                vm_name=vm_name,
                status=SliceAutostartBackfillStatus.BACKFILLED,
                detail=CONTAINER_START_ONLY_DETAIL if verdict.is_container_start_only else None,
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

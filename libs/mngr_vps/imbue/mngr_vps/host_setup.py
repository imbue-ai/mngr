import base64
import json
import math
import shlex
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.providers.ssh_host_setup import build_cap_journald_command
from imbue.mngr_vps.container_setup import LABEL_HOST_ID
from imbue.mngr_vps.container_setup import LABEL_MEMORY_CAP
from imbue.mngr_vps.container_setup import MEMORY_CAP_FOLLOWS_VM_LABEL_VALUE
from imbue.mngr_vps.errors import VpsProvisioningError
from imbue.mngr_vps.systemd import render_systemd_unit

# Exact Docker Engine version we install on every outer. Pinning makes bakes and
# re-provisions reproducible instead of "whatever get.docker.com served that day".
#
# The full apt version string is ``<core>~<id>.<version_id>~<codename>`` where the
# trailing distro suffix is repo-specific. ``PINNED_DOCKER_APT_VERSION_CORE`` is
# the distro-independent prefix; ``_PINNED_DOCKER_INSTALL_SCRIPT`` derives the suffix
# from ``/etc/os-release`` at run time so the same step works on every Debian-
# family outer (Debian 13 "trixie" is the default VM image of every mngr_vps
# cloud provider; the os-release derivation also covers Debian 12 and Ubuntu LTS
# images for anyone overriding a provider's image). Confirm a new core against
# the live repo with ``apt-cache madison docker-ce`` on each release in use.
# 29.6.2 (not 29.5.1): docker-ce 29.5.1 with current containerd.io failed every
# `docker run` with a corrupted shim bootstrap ("failed to create TTRPC
# connection: unsupported protocol: Yunix") on the slice fleet's guest VMs,
# while 29.6.2 with the same containerd.io 2.2.6 works. Images pre-baked with
# an older pin must be re-staged.
PINNED_DOCKER_VERSION: Final[str] = "29.6.2"
PINNED_DOCKER_APT_VERSION_CORE: Final[str] = "5:29.6.2-1"

# containerd.io is pinned alongside docker-ce: the daemon/shim pairing between
# the two is exactly the axis the "unsupported protocol: Yunix" class of
# failure lives on, so letting containerd.io float with the repo while
# docker-ce is pinned reintroduces silent drift between hosts staged at
# different times. 2.2.6 is the version verified working with docker-ce 29.6.2
# across the dev, staging, and production slice fleets. Same core/suffix split
# as the docker pin (no epoch in containerd.io's version).
PINNED_CONTAINERD_APT_VERSION_CORE: Final[str] = "2.2.6-1"

# gVisor publishes date-stamped releases under
# ``https://storage.googleapis.com/gvisor/releases/release/<yyyymmdd>/<arch>/``.
# Pin one so runsc is reproducible; confirm the date exists in that bucket before
# deploying (the apt repo only ever serves "latest", so we download + checksum
# the dated binaries directly instead).
PINNED_GVISOR_RELEASE: Final[str] = "20260601"
GVISOR_UPSTREAM_RELEASES_URL: Final[str] = "https://storage.googleapis.com/gvisor/releases/release"
# The directory holding the pinned release's per-arch subdirectories upstream.
# Callers that mirror the release elsewhere (the imbue slice fleet's artifact
# mirror) render the install script against their own copy of this layout.
PINNED_GVISOR_UPSTREAM_RELEASE_URL: Final[str] = f"{GVISOR_UPSTREAM_RELEASES_URL}/{PINNED_GVISOR_RELEASE}"

# The flags runsc is registered with in the Docker daemon config. ``--overlay2=none``
# writes the container's writable layer through to the persistent Docker overlay2
# layer so it survives a ``docker stop``/``start`` (and a host reboot that brings
# the container back via its restart policy). gVisor's default overlay
# (``--overlay2=root:self``) keeps the rootfs upper in a per-sandbox
# ``.gvisor.filestore`` that is recreated on every start, so without this every
# in-container write outside a named volume -- the injected sshd host key, the
# ``/mngr`` host_dir symlink, mngr's provisioning markers, etc. -- is silently
# lost on restart, leaving the container unreachable until mngr re-provisions it.
GVISOR_RUNSC_RUNTIME_ARGS: Final[tuple[str, ...]] = ("--overlay2=none",)
_GVISOR_INSTALL_DIR: Final[str] = "/usr/local/bin"
GVISOR_RUNSC_BINARY_PATH: Final[str] = f"{_GVISOR_INSTALL_DIR}/runsc"

# Each host-setup step is a self-contained shell script run with a generous hard
# timeout. apt mirror round-trips plus package extraction routinely take a couple
# of minutes on a fresh VPS; the gVisor download adds more, so keep this well
# above the expected worst case to avoid failing an otherwise-fine provision.
_HOST_SETUP_COMMAND_TIMEOUT_SECONDS: Final[float] = 600.0

# First-boot completion marker. The bootstrap (cloud-init runcmd or the GCE
# startup-script) ``touch``es this once Docker and the rest of host setup are in
# place; ``instance._wait_for_cloud_init_marker`` polls for it before proceeding.
# Single source of truth shared by every writer and the poller so the path can
# never drift between them.
MNGR_READY_MARKER_PATH: Final[str] = "/var/run/mngr-ready"


def build_auto_shutdown_command(auto_shutdown_seconds: int) -> str:
    """Return the in-guest ``shutdown -P +N`` command for an auto-shutdown deadline.

    ``shutdown -P`` only accepts whole minutes. Round up so we never halt before the
    deadline, and floor at 1 so any positive sub-minute value still schedules a shutdown.
    Shared by both first-boot renderers (cloud-init ``runcmd`` and the GCE startup-script)
    so the rounding policy and command text stay identical.
    """
    shutdown_minutes = max(1, math.ceil(auto_shutdown_seconds / 60))
    return f"shutdown -P +{shutdown_minutes} 'mngr_vps auto-shutdown after {shutdown_minutes} minutes'"


class HostSetupStep(FrozenModel):
    """A single idempotent host-level provisioning step (a named shell script)."""

    description: str = Field(description="Human-readable summary of what the step does")
    script: str = Field(description="POSIX-sh script that performs the step idempotently")


# ``apt-get update`` with retries: on a cloud VM's first boot the host-setup
# steps run as soon as cloud-init reaches ``runcmd``, which can be before the
# image's apt mirror resolves (Azure's Debian 13 image does this). Only the first
# step needs it; the Docker step's own update runs once the mirror has resolved,
# and that script is shared with the imbue_cloud slice bakes.
_APT_UPDATE_WITH_RETRIES: Final[str] = """for attempt in 1 2 3 4 5 6; do
    if apt-get update; then break; fi
    if [ "$attempt" = 6 ]; then echo "apt-get update failed after 6 attempts" >&2; exit 1; fi
    sleep 10
done"""

# Debian 13 mounts the VM's /tmp as a RAM-backed tmpfs sized at half of RAM.
# On a VM whose agent container is capped at RAM minus a 1 GiB reserve, anything
# written there comes out of that reserve, and a full /tmp ends in the kernel
# OOM-killing the container. The gen-2 boxes hit exactly this and keep /tmp on
# disk through a ``tmp.mount`` of their own; the cloud VMs get the same: a unit
# under /etc/systemd/system overrides the distro's, bind-mounting a root-disk
# directory with the distro tmpfs's ``nosuid,nodev``, and is restarted in place
# when /tmp is currently anything else (a first boot already has the tmpfs up;
# a plain ``start`` of an active unit does nothing). The GCE startup script
# re-runs this every boot, so the change guard keeps it a no-op afterwards.
VM_TMP_BACKING_DIR: Final[str] = "/var/lib/mngr-tmp"
VM_TMP_MOUNT_UNIT_NAME: Final[str] = "tmp.mount"
_VM_TMP_MOUNT_UNIT_PATH: Final[str] = f"/etc/systemd/system/{VM_TMP_MOUNT_UNIT_NAME}"


@pure
def render_vm_tmp_mount_unit() -> str:
    return render_systemd_unit(
        {
            "Unit": [
                ("Description", "mngr: /tmp on the VM's disk instead of a RAM-backed tmpfs"),
                ("DefaultDependencies", "no"),
                ("Conflicts", "umount.target"),
                ("Before", "local-fs.target umount.target"),
                ("After", "local-fs-pre.target"),
                ("ConditionPathIsDirectory", VM_TMP_BACKING_DIR),
            ],
            "Mount": [
                ("What", VM_TMP_BACKING_DIR),
                ("Where", "/tmp"),
                ("Type", "none"),
                ("Options", "bind,nosuid,nodev"),
            ],
            "Install": [("WantedBy", "local-fs.target")],
        }
    )


_VM_TMP_MOUNT_SCRIPT: Final[str] = f"""set -e
install -d -m 1777 {VM_TMP_BACKING_DIR}
MNGR_TMP_STAGE="$(mktemp -d {VM_TMP_BACKING_DIR}/.mngr-stage.XXXXXX)"
cat > "$MNGR_TMP_STAGE/unit" <<'MNGR_TMP_MOUNT_UNIT_EOF'
{render_vm_tmp_mount_unit()}MNGR_TMP_MOUNT_UNIT_EOF
if ! cmp -s "$MNGR_TMP_STAGE/unit" {_VM_TMP_MOUNT_UNIT_PATH}; then
    install -m 0644 "$MNGR_TMP_STAGE/unit" {_VM_TMP_MOUNT_UNIT_PATH}
    systemctl daemon-reload
    systemctl enable {VM_TMP_MOUNT_UNIT_NAME}
fi
rm -rf "$MNGR_TMP_STAGE"
if ! findmnt -no SOURCE /tmp 2>/dev/null | grep -q '{VM_TMP_BACKING_DIR}\\]$'; then
    systemctl restart {VM_TMP_MOUNT_UNIT_NAME} || {{ umount -l /tmp && systemctl restart {VM_TMP_MOUNT_UNIT_NAME}; }}
fi
findmnt -no SOURCE /tmp | grep -q '{VM_TMP_BACKING_DIR}\\]$'"""

# Base packages mngr_vps needs on every outer: curl/ca-certificates/gnupg
# for the Docker apt repo, rsync for the build-context upload, and inotify-tools +
# jq for the per-host snapshot helper.
_BASE_PACKAGES_SCRIPT: Final[str] = f"""set -e
export DEBIAN_FRONTEND=noninteractive
{_APT_UPDATE_WITH_RETRIES}
apt-get install -y curl ca-certificates gnupg rsync inotify-tools jq"""

# Pin Docker + containerd via the official apt repo. ``--allow-downgrades`` plus
# an exact ``=version`` pin makes the pinned versions authoritative in both
# directions, so re-provisioning an old host upgrades (or downgrades) it to
# match. buildx / compose still track the repo's current build (client-side
# plugins with no daemon pairing to protect).
_PINNED_DOCKER_INSTALL_SCRIPT: Final[str] = f"""set -e
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release
DOCKER_APT_VERSION="{PINNED_DOCKER_APT_VERSION_CORE}~${{ID}}.${{VERSION_ID}}~${{VERSION_CODENAME}}"
CONTAINERD_APT_VERSION="{PINNED_CONTAINERD_APT_VERSION_CORE}~${{ID}}.${{VERSION_ID}}~${{VERSION_CODENAME}}"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/${{ID}}/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${{ID}} ${{VERSION_CODENAME}} stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y --allow-downgrades \
docker-ce="${{DOCKER_APT_VERSION}}" docker-ce-cli="${{DOCKER_APT_VERSION}}" \
containerd.io="${{CONTAINERD_APT_VERSION}}" docker-buildx-plugin docker-compose-plugin
systemctl enable docker
systemctl start docker"""


@pure
def render_gvisor_binary_install_script(release_url: str) -> str:
    """The script that downloads the pinned gVisor binaries from ``release_url``'s per-arch subdirectories.

    Verifies the release's published sha512 checksums (served beside the
    binaries) and installs runsc + its containerd shim under /usr/local/bin;
    skipped when runsc is already present. Registration with the Docker daemon
    is the caller's (a live host runs ``runsc install`` + a docker restart, an
    offline image write is the daemon.json directly).
    """
    return f"""set -e
if ! command -v runsc >/dev/null 2>&1; then
    ARCH="$(uname -m)"
    URL="{release_url}/${{ARCH}}"
    GVISOR_TMP="$(mktemp -d /var/tmp/mngr-gvisor.XXXXXX)"
    cd "${{GVISOR_TMP}}"
    curl -fsSL -o runsc "${{URL}}/runsc"
    curl -fsSL -o runsc.sha512 "${{URL}}/runsc.sha512"
    curl -fsSL -o containerd-shim-runsc-v1 "${{URL}}/containerd-shim-runsc-v1"
    curl -fsSL -o containerd-shim-runsc-v1.sha512 "${{URL}}/containerd-shim-runsc-v1.sha512"
    sha512sum -c runsc.sha512
    sha512sum -c containerd-shim-runsc-v1.sha512
    chmod a+rx runsc containerd-shim-runsc-v1
    mv runsc containerd-shim-runsc-v1 {_GVISOR_INSTALL_DIR}/
    cd /
    rm -rf "${{GVISOR_TMP}}"
fi"""


# Rendered against upstream: the variant every VPS provider's host setup runs
# (the gen-2 bare-metal prep renders its own copy against the artifact mirror).
PINNED_GVISOR_BINARY_INSTALL_SCRIPT: Final[str] = render_gvisor_binary_install_script(
    PINNED_GVISOR_UPSTREAM_RELEASE_URL
)

# Install and register the pinned gVisor runsc runtime on a live host (see
# ``GVISOR_RUNSC_RUNTIME_ARGS`` for why ``--overlay2=none``). The daemon
# (re)registration only runs when the flag is not already in the Docker config,
# so a correctly-configured host is a no-op (no docker bounce).
_GVISOR_INSTALL_SCRIPT: Final[str] = f"""{PINNED_GVISOR_BINARY_INSTALL_SCRIPT}
if ! grep -q -- '{GVISOR_RUNSC_RUNTIME_ARGS[0]}' /etc/docker/daemon.json 2>/dev/null; then
    runsc install -- {" ".join(GVISOR_RUNSC_RUNTIME_ARGS)}
    systemctl restart docker
fi"""

# The VM sshd drop-in. A file under ``sshd_config.d`` rather than lines appended
# to ``sshd_config``: Debian's stock config includes the directory first, so the
# drop-in wins, and a file can be compared whole so a re-run only restarts sshd
# when something actually changed.
#
# MaxSessions / MaxStartups: provisioning round-trips (image build + per-host
# setup, many concurrent ssh/rsync/exec calls) trip the default 10:30:100 cap
# and lose connections mid-transfer. PermitRootLogin prohibit-password /
# PasswordAuthentication no / KbdInteractiveAuthentication no: every mngr host
# authenticates by key only. PerSourcePenalties no: OpenSSH >= 9.8 (Debian 13)
# blocks an address for minutes after a few unauthenticated connections, which
# mngr's discovery and readiness probes, the imbue_cloud connector's shared
# egress addresses, and a reconnect burst from the user's laptop all produce
# (the gen-2 slice guests and lima guests apply the same opt-out). The keyword
# is fatal to older sshds, so it is written only when ``sshd -T`` advertises it.
_SSHD_DROP_IN_FILE_NAME: Final[str] = "60-mngr.conf"
MNGR_SSHD_DROP_IN_PATH: Final[str] = f"/etc/ssh/sshd_config.d/{_SSHD_DROP_IN_FILE_NAME}"
_SSHD_DROP_IN_LINES: Final[tuple[str, ...]] = (
    "MaxSessions 100",
    "MaxStartups 100:30:200",
    "PermitRootLogin prohibit-password",
    "PasswordAuthentication no",
    "KbdInteractiveAuthentication no",
)
_SSHD_RESTART_COMMAND: Final[str] = (
    "systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || true"
)


@pure
def render_sshd_drop_in_stage_script(staged_path: str) -> str:
    """Shell that writes the desired content of mngr's sshd drop-in to ``staged_path``.

    Shared by the host-setup step and the GCE startup script so both stage the
    identical file and their change guards agree. ``staged_path`` is substituted
    verbatim inside double quotes, so it may reference a shell variable.
    """
    fixed_lines = "\\n".join(_SSHD_DROP_IN_LINES) + "\\n"
    return (
        f"printf '{fixed_lines}' > \"{staged_path}\"\n"
        f"if sshd -T 2>/dev/null | grep -qi '^persourcepenalties'; then "
        f"printf 'PerSourcePenalties no\\n' >> \"{staged_path}\"; fi"
    )


_SSHD_DROP_IN_SCRIPT: Final[str] = f"""set -e
MNGR_SSHD_STAGE="$(mktemp -d)"
{render_sshd_drop_in_stage_script(f"$MNGR_SSHD_STAGE/{_SSHD_DROP_IN_FILE_NAME}")}
mkdir -p /etc/ssh/sshd_config.d
if ! cmp -s "$MNGR_SSHD_STAGE/{_SSHD_DROP_IN_FILE_NAME}" {MNGR_SSHD_DROP_IN_PATH}; then
    install -m 0644 "$MNGR_SSHD_STAGE/{_SSHD_DROP_IN_FILE_NAME}" {MNGR_SSHD_DROP_IN_PATH}
    {_SSHD_RESTART_COMMAND}
fi
rm -rf "$MNGR_SSHD_STAGE\""""

# Bounds on what the Docker daemon and the journal may accumulate on the VM's
# disk: container logs rotate (a chatty agent cannot fill the root disk),
# the build cache is bounded, and the journal is capped by the snippet every
# mngr-provisioned VM shares. The daemon.json is merged with jq rather than
# written whole because ``runsc install`` (the gVisor step) also writes that
# file; both sides are canonicalized before comparing, so an already-converged
# host gets no docker restart. Docker is restarted only when it is already
# running: on first boot the daemon is installed afterwards and starts with
# this config.
_DOCKER_DAEMON_BOUNDS: Final[dict[str, object]] = {
    "log-driver": "json-file",
    "log-opts": {"max-size": "50m", "max-file": "3"},
    "builder": {"gc": {"enabled": True, "defaultKeepStorage": "1GB"}},
}
_DOCKER_DAEMON_JSON_PATH: Final[str] = "/etc/docker/daemon.json"

_DOCKER_DAEMON_BOUNDS_SCRIPT: Final[str] = f"""set -e
MNGR_BOUNDS_STAGE="$(mktemp -d)"
mkdir -p /etc/docker
if [ -f {_DOCKER_DAEMON_JSON_PATH} ]; then
    jq -S . {_DOCKER_DAEMON_JSON_PATH} > "$MNGR_BOUNDS_STAGE/current.json"
else
    echo '{{}}' > "$MNGR_BOUNDS_STAGE/current.json"
fi
jq -S --argjson bounds '{json.dumps(_DOCKER_DAEMON_BOUNDS, sort_keys=True)}' '. * $bounds' "$MNGR_BOUNDS_STAGE/current.json" > "$MNGR_BOUNDS_STAGE/merged.json"
if ! cmp -s "$MNGR_BOUNDS_STAGE/current.json" "$MNGR_BOUNDS_STAGE/merged.json"; then
    install -m 0644 "$MNGR_BOUNDS_STAGE/merged.json" {_DOCKER_DAEMON_JSON_PATH}
    if systemctl is-active --quiet docker; then systemctl restart docker; fi
fi
rm -rf "$MNGR_BOUNDS_STAGE"
{build_cap_journald_command()}"""

# RAM (MiB) held back from the agent container's hard cap so the VM's own
# daemons (dockerd/containerd, sshd, systemd, journald, the snapshot helper,
# kernel slab and a little file cache) always have room. Without a cap, a
# container at memory capacity collapses the VM-wide page cache and wedges the
# VM's sshd, leaving the host unreachable and unrecoverable (the July 2026
# imbue_cloud incident; the gen-2 slice guests hold back the same 1 GiB).
CONTAINER_MEMORY_RESERVE_MIB: Final[int] = 1024
# The every-boot unit that follows the VM's RAM with the container's cap: reads
# ``MemTotal``, subtracts the reserve, and ``docker update``s every VM-sized mngr
# agent container (the ``memory-cap=vm`` label) whose recorded cap differs, so a
# resized VM re-caps on its next boot.
# The provider also starts it once right after ``docker run`` so a new container
# is capped from its first seconds (see ``build_apply_container_memory_cap_command``).
CONTAINER_MEMORY_UNIT_NAME: Final[str] = "mngr-vps-container-memory.service"
_CONTAINER_MEMORY_SCRIPT_PATH: Final[str] = "/usr/local/sbin/mngr-vps-container-memory.sh"
_CONTAINER_MEMORY_UNIT_PATH: Final[str] = f"/etc/systemd/system/{CONTAINER_MEMORY_UNIT_NAME}"


@pure
def render_container_memory_reconcile_script() -> str:
    """The script that caps every VM-sized mngr agent container at the VM's RAM minus the reserve.

    Only containers carrying the ``memory-cap=vm`` label are touched; one the
    caller sized with an explicit ``--memory`` keeps that cap.

    A failed ``docker update`` is logged, not fatal: at boot the kernel can refuse
    to shrink a cgroup below its current usage, and the pressure then resolves
    itself (earlyoom, then the cgroup OOM killer). A dockerd that never comes up
    is likewise logged and skipped. The create path verifies the cap separately
    (``parse_container_memory_cap_probe``).
    """
    return f"""#!/bin/sh
# Installed by mngr_vps: cap every VM-sized mngr agent container at the VM's RAM minus a reserve.
set -u
attempt=0
until docker info >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        echo "WARNING: docker did not become available; leaving container memory caps unchanged" >&2
        exit 0
    fi
    sleep 2
done
total_kib=$(awk '/^MemTotal:/{{print $2}}' /proc/meminfo)
cap_mib=$(( total_kib / 1024 - {CONTAINER_MEMORY_RESERVE_MIB} ))
if [ "$cap_mib" -le 0 ]; then exit 0; fi
cap_bytes=$(( cap_mib * 1024 * 1024 ))
for container_id in $(docker ps -aq --filter "label={LABEL_HOST_ID}" --filter "label={LABEL_MEMORY_CAP}={MEMORY_CAP_FOLLOWS_VM_LABEL_VALUE}"); do
    current=$(docker inspect --format '{{{{.HostConfig.Memory}}}}' "$container_id")
    if [ "$current" != "$cap_bytes" ]; then
        docker update --memory "${{cap_mib}}m" --memory-swap "${{cap_mib}}m" "$container_id" \\
            || echo "WARNING: could not update the memory cap of $container_id" >&2
    fi
done
"""


@pure
def _render_container_memory_reconcile_unit() -> str:
    return render_systemd_unit(
        {
            "Unit": [
                ("Description", "mngr: cap the agent container's memory at the VM's RAM minus a reserve"),
                ("After", "docker.service"),
                ("Wants", "docker.service"),
            ],
            "Service": [("Type", "oneshot"), ("ExecStart", _CONTAINER_MEMORY_SCRIPT_PATH)],
            "Install": [("WantedBy", "multi-user.target")],
        }
    )


_CONTAINER_MEMORY_INSTALL_SCRIPT: Final[str] = f"""set -e
MNGR_MEMORY_STAGE="$(mktemp -d)"
cat > "$MNGR_MEMORY_STAGE/script" <<'MNGR_MEMORY_SCRIPT_EOF'
{render_container_memory_reconcile_script()}MNGR_MEMORY_SCRIPT_EOF
cat > "$MNGR_MEMORY_STAGE/unit" <<'MNGR_MEMORY_UNIT_EOF'
{_render_container_memory_reconcile_unit()}MNGR_MEMORY_UNIT_EOF
if ! cmp -s "$MNGR_MEMORY_STAGE/script" {_CONTAINER_MEMORY_SCRIPT_PATH} || ! cmp -s "$MNGR_MEMORY_STAGE/unit" {_CONTAINER_MEMORY_UNIT_PATH}; then
    install -m 0755 "$MNGR_MEMORY_STAGE/script" {_CONTAINER_MEMORY_SCRIPT_PATH}
    install -m 0644 "$MNGR_MEMORY_STAGE/unit" {_CONTAINER_MEMORY_UNIT_PATH}
    systemctl daemon-reload
    systemctl enable {CONTAINER_MEMORY_UNIT_NAME}
fi
rm -rf "$MNGR_MEMORY_STAGE\""""

# Prints the VM's RAM in KiB, the input to every RAM-derived size (the container
# memory cap, the /tmp tmpfs cap).
MEM_TOTAL_PROBE_COMMAND: Final[str] = "awk '/^MemTotal:/{print $2}' /proc/meminfo"


@pure
def parse_mem_total_kib(stdout: str) -> int:
    """Parse ``MEM_TOTAL_PROBE_COMMAND`` output; raises ``VpsProvisioningError`` on anything but one integer."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1 or not lines[0].isdigit():
        raise VpsProvisioningError(f"Unexpected MemTotal probe output: {stdout!r}")
    return int(lines[0])


# Marker lines the create-time cap command prints ahead of its probe values, so
# the caller can tell a host without the unit (one whose host setup never ran,
# e.g. an externally provisioned VM) from one where the cap should have applied.
_MEMORY_CAP_UNIT_PRESENT_MARKER: Final[str] = "mngr-memory-unit=present"
_MEMORY_CAP_UNIT_ABSENT_MARKER: Final[str] = "mngr-memory-unit=absent"
# Bounds the create-time cap command; it must outlast the reconciler's one-minute
# wait for dockerd (30 attempts, 2 s apart) plus the update and inspect that follow.
CONTAINER_MEMORY_CAP_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0


class ContainerMemoryCapProbe(FrozenModel):
    """What the VM reported right after the memory reconciler ran for a new container."""

    mem_total_kib: int = Field(description="The VM's MemTotal from /proc/meminfo")
    container_memory_bytes: int = Field(description="The container's HostConfig.Memory (0 when uncapped)")


@pure
def build_apply_container_memory_cap_command(container_name: str) -> str:
    """Shell that runs the memory reconciler once for a just-created container and prints the result.

    Prints the unit-presence marker, then ``MemTotal`` and the container's
    recorded memory cap, one per line, for ``parse_container_memory_cap_probe``.
    """
    return (
        f"if [ -f {_CONTAINER_MEMORY_UNIT_PATH} ]; then "
        f"systemctl start {CONTAINER_MEMORY_UNIT_NAME} && echo {_MEMORY_CAP_UNIT_PRESENT_MARKER}; "
        f"else echo {_MEMORY_CAP_UNIT_ABSENT_MARKER}; fi && "
        f"{MEM_TOTAL_PROBE_COMMAND} && "
        f"docker inspect --format '{{{{.HostConfig.Memory}}}}' {shlex.quote(container_name)}"
    )


@pure
def parse_container_memory_cap_probe(stdout: str) -> ContainerMemoryCapProbe | None:
    """Parse ``build_apply_container_memory_cap_command`` output; None when the unit is not installed.

    Raises ``VpsProvisioningError`` on any other shape, since the command is
    mngr's own and unexpected output means the probe did not run as intended.
    """
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if len(lines) != 3 or lines[0] not in (_MEMORY_CAP_UNIT_PRESENT_MARKER, _MEMORY_CAP_UNIT_ABSENT_MARKER):
        raise VpsProvisioningError(f"Unexpected container memory cap probe output: {stdout!r}")
    if lines[0] == _MEMORY_CAP_UNIT_ABSENT_MARKER:
        return None
    try:
        return ContainerMemoryCapProbe(mem_total_kib=int(lines[1]), container_memory_bytes=int(lines[2]))
    except ValueError as e:
        raise VpsProvisioningError(f"Unexpected container memory cap probe output: {stdout!r}") from e


@pure
def expected_container_memory_cap_bytes(mem_total_kib: int) -> int | None:
    """The cap the reconciler applies on a VM with this much RAM, or None when the VM is too small to cap."""
    cap_mib = mem_total_kib // 1024 - CONTAINER_MEMORY_RESERVE_MIB
    if cap_mib <= 0:
        return None
    return cap_mib * 1024 * 1024


# OVH classic-VPS images ship qemu-guest-agent, which lets the hypervisor run
# automated backups by freezing the guest filesystem -- that freeze hangs the
# agent, so purge every qemu* package. Detects qemu first so the step is a clean
# no-op on an image that ships none (an apt glob matching nothing exits non-zero).
_QEMU_PURGE_SCRIPT: Final[str] = """set -e
export DEBIAN_FRONTEND=noninteractive
if dpkg -l | grep -q qemu; then
    apt-get purge --auto-remove -y 'qemu*'
fi"""


@pure
def build_host_setup_steps(
    *,
    install_gvisor_runtime: bool,
    is_qemu_purge_enabled: bool,
) -> tuple[HostSetupStep, ...]:
    """Build the ordered, idempotent host-setup steps shared by cloud-init and SSH.

    This is the single source of truth for host-level (not agent-level)
    provisioning. ``cloud_init.generate_cloud_init_user_data`` wraps these scripts
    into a first-boot ``runcmd`` block, and ``apply_host_setup_on_outer`` runs the
    same scripts over SSH to re-provision an already-running host. SSH host-key
    injection is intentionally NOT included here -- it is first-boot-only and
    lives in the cloud-init wrapper so re-runs never reset the host key.
    """
    steps: list[HostSetupStep] = [
        HostSetupStep(
            description="Keep the VM's /tmp on its disk rather than a RAM-backed tmpfs",
            script=_VM_TMP_MOUNT_SCRIPT,
        ),
        HostSetupStep(
            description="Install base packages required by mngr_vps",
            script=_BASE_PACKAGES_SCRIPT,
        ),
        HostSetupStep(
            description="Bound container logs and the build cache and cap the journal",
            script=_DOCKER_DAEMON_BOUNDS_SCRIPT,
        ),
        HostSetupStep(
            description=f"Install pinned Docker Engine {PINNED_DOCKER_VERSION}",
            script=_PINNED_DOCKER_INSTALL_SCRIPT,
        ),
    ]
    if install_gvisor_runtime:
        steps.append(
            HostSetupStep(
                description=f"Install and register pinned gVisor runsc runtime {PINNED_GVISOR_RELEASE}",
                script=_GVISOR_INSTALL_SCRIPT,
            )
        )
    steps.append(
        HostSetupStep(
            description="Install the sshd drop-in (session caps, key-only auth, no per-source penalties)",
            script=_SSHD_DROP_IN_SCRIPT,
        )
    )
    steps.append(
        HostSetupStep(
            description="Install the every-boot container memory cap reconciler",
            script=_CONTAINER_MEMORY_INSTALL_SCRIPT,
        )
    )
    if is_qemu_purge_enabled:
        steps.append(
            HostSetupStep(
                description="Purge qemu packages to disable hypervisor backups",
                script=_QEMU_PURGE_SCRIPT,
            )
        )
    return tuple(steps)


def apply_host_setup_on_outer(
    outer: OuterHostInterface,
    *,
    install_gvisor_runtime: bool,
    is_qemu_purge_enabled: bool,
) -> None:
    """Re-apply the shared idempotent host setup on an already-running outer over SSH.

    Used by callers that operate on a VPS whose OS already booted (the OVH bake,
    which has no cloud-init, and the imbue_cloud slow path rebuilding a leased
    pool host) so host-level setup stays consistent even on hosts baked with an
    old version. Each step is run idempotently; any failure raises
    ``VpsProvisioningError`` (fatal -- the caller must not proceed onto a
    misconfigured host).
    """
    steps = build_host_setup_steps(
        install_gvisor_runtime=install_gvisor_runtime,
        is_qemu_purge_enabled=is_qemu_purge_enabled,
    )
    for step in steps:
        with log_span("Applying host-setup step on {}: {}", outer.get_name(), step.description):
            result = outer.execute_idempotent_command(
                build_remote_script_command(step.script),
                timeout_seconds=_HOST_SETUP_COMMAND_TIMEOUT_SECONDS,
            )
        if not result.success:
            raise VpsProvisioningError(
                f"Host-setup step {step.description!r} failed on {outer.get_name()}: "
                f"stderr={result.stderr.strip()!r} stdout={result.stdout.strip()!r}"
            )


@pure
def build_remote_script_command(script: str) -> str:
    """Wrap a shell script so it survives transport to the remote shell verbatim.

    Base64-encodes the script and decodes it on the remote before piping to sh,
    sidestepping any quoting/escaping pitfalls from the multi-line scripts (which
    contain ``$(...)``, single quotes, and printf escapes).
    """
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return f"echo {encoded} | base64 -d | sh"

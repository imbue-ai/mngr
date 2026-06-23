import base64
import math
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr_vps.errors import VpsProvisioningError

# Exact Docker Engine version we install on every outer. Pinning makes bakes and
# re-provisions reproducible instead of "whatever get.docker.com served that day".
#
# The full apt version string is ``<core>~<id>.<version_id>~<codename>`` where the
# trailing distro suffix is repo-specific. ``_PINNED_DOCKER_APT_VERSION_CORE`` is
# the distro-independent prefix; ``_DOCKER_INSTALL_SCRIPT`` derives the suffix
# from ``/etc/os-release`` at run time so the same step works on every Debian-
# family outer (Debian 12 "bookworm" across all providers; the os-release
# derivation also covers Ubuntu LTS images for anyone overriding the GCP image).
# Confirm a new core against the live repo with ``apt-cache madison docker-ce``.
# ``PINNED_DOCKER_APT_VERSION`` is the fully-rendered Debian 12 apt version
# string, exported for any caller or test that needs the exact Debian value
# rather than the runtime-derived suffix.
PINNED_DOCKER_VERSION: Final[str] = "29.5.1"
_PINNED_DOCKER_APT_VERSION_CORE: Final[str] = "5:29.5.1-1"
PINNED_DOCKER_APT_VERSION: Final[str] = f"{_PINNED_DOCKER_APT_VERSION_CORE}~debian.12~bookworm"

# gVisor publishes date-stamped releases under
# ``https://storage.googleapis.com/gvisor/releases/release/<yyyymmdd>/<arch>/``.
# Pin one so runsc is reproducible; confirm the date exists in that bucket before
# deploying (the apt repo only ever serves "latest", so we download + checksum
# the dated binaries directly instead).
PINNED_GVISOR_RELEASE: Final[str] = "20260601"

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


# Base packages mngr_vps needs on every outer: curl/ca-certificates/gnupg
# for the Docker apt repo, rsync for the build-context upload, and inotify-tools +
# jq for the per-host snapshot helper.
_BASE_PACKAGES_SCRIPT: Final[str] = """set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y curl ca-certificates gnupg rsync inotify-tools jq"""

# Pin Docker via the official apt repo. ``--allow-downgrades`` plus an exact
# ``=version`` pin makes the pinned version authoritative in both directions, so
# re-provisioning an old host upgrades (or downgrades) it to match. containerd.io
# / buildx / compose track the repo's current build, matching Docker's own docs.
_DOCKER_INSTALL_SCRIPT: Final[str] = f"""set -e
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release
DOCKER_APT_VERSION="{_PINNED_DOCKER_APT_VERSION_CORE}~${{ID}}.${{VERSION_ID}}~${{VERSION_CODENAME}}"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/${{ID}}/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${{ID}} ${{VERSION_CODENAME}} stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y --allow-downgrades \
docker-ce="${{DOCKER_APT_VERSION}}" docker-ce-cli="${{DOCKER_APT_VERSION}}" \
containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable docker
systemctl start docker"""

# Install and register the pinned gVisor runsc runtime by downloading the dated
# release binaries and verifying their checksums.
#
# runsc is registered with ``--overlay2=none`` so the container's writable layer
# is written through to the persistent Docker overlay2 layer and survives a
# ``docker stop``/``start`` (and a host reboot that brings the container back via
# its restart policy). gVisor's default overlay (``--overlay2=root:self``) keeps
# the rootfs upper in a per-sandbox ``.gvisor.filestore`` that is recreated on
# every start, so without this every in-container write outside a named volume --
# the injected sshd host key, the ``/mngr`` host_dir symlink, mngr's
# provisioning markers, etc. -- is silently lost on restart, leaving the
# container unreachable until mngr re-provisions it.
#
# The binary download is skipped when runsc is already present; the daemon
# (re)registration only runs when the ``--overlay2=none`` flag is not already in
# the Docker config, so a correctly-configured host is a no-op (no docker bounce).
_GVISOR_INSTALL_SCRIPT: Final[str] = f"""set -e
if ! command -v runsc >/dev/null 2>&1; then
    ARCH="$(uname -m)"
    URL="https://storage.googleapis.com/gvisor/releases/release/{PINNED_GVISOR_RELEASE}/${{ARCH}}"
    GVISOR_TMP="$(mktemp -d)"
    cd "${{GVISOR_TMP}}"
    curl -fsSL -o runsc "${{URL}}/runsc"
    curl -fsSL -o runsc.sha512 "${{URL}}/runsc.sha512"
    curl -fsSL -o containerd-shim-runsc-v1 "${{URL}}/containerd-shim-runsc-v1"
    curl -fsSL -o containerd-shim-runsc-v1.sha512 "${{URL}}/containerd-shim-runsc-v1.sha512"
    sha512sum -c runsc.sha512
    sha512sum -c containerd-shim-runsc-v1.sha512
    chmod a+rx runsc containerd-shim-runsc-v1
    mv runsc containerd-shim-runsc-v1 /usr/local/bin/
    cd /
    rm -rf "${{GVISOR_TMP}}"
fi
if ! grep -q -- '--overlay2=none' /etc/docker/daemon.json 2>/dev/null; then
    runsc install -- --overlay2=none
    systemctl restart docker
fi"""

# Raise sshd's session/pre-auth caps so provisioning round-trips (image build +
# per-host setup + the imbue_cloud pool baking's many concurrent ssh/rsync/exec
# calls) don't trip the default 10:30:100 cap and lose connections mid-transfer.
# Uses printf (not a heredoc) so it behaves identically whether run over SSH or
# rendered into a cloud-init runcmd block.
_SSHD_TUNING_SCRIPT: Final[str] = """set -e
if ! grep -q '^MaxSessions' /etc/ssh/sshd_config 2>/dev/null; then
    printf '\\nMaxSessions 100\\nMaxStartups 100:30:200\\n' >> /etc/ssh/sshd_config
    systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || true
fi"""

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
            description="Install base packages required by mngr_vps",
            script=_BASE_PACKAGES_SCRIPT,
        ),
        HostSetupStep(
            description=f"Install pinned Docker Engine {PINNED_DOCKER_VERSION}",
            script=_DOCKER_INSTALL_SCRIPT,
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
            description="Tune sshd MaxSessions / MaxStartups",
            script=_SSHD_TUNING_SCRIPT,
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

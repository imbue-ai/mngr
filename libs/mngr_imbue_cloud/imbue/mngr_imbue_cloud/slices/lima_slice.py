import base64
import shlex
from typing import Any
from typing import Final

from imbue.mngr_imbue_cloud.errors import SliceReserveOutputError
from imbue.mngr_imbue_cloud.slices.bare_metal import SLICE_LIMA_INSTANCE_PREFIX
from imbue.mngr_lima.lima_yaml import generate_default_lima_yaml

# Placeholder tokens for the two box host ports in a slice YAML *template*. The
# real ports are chosen on the box under the reservation lock and substituted for
# these tokens (see ``build_slice_reserve_script``), so the YAML built off-box need
# not know them. They are plain scalars so PyYAML emits them unquoted.
VM_SSH_PORT_PLACEHOLDER: Final[str] = "__MNGR_VM_SSH_PORT__"
CONTAINER_SSH_PORT_PLACEHOLDER: Final[str] = "__MNGR_CONTAINER_SSH_PORT__"

# Box-wide advisory lock file (under the lima service user's home) serializing the
# brief slice reservation critical section across all bakes (any env) on one box.
SLICE_ALLOC_LOCK_RELPATH: Final[str] = ".mngr-slice-alloc.lock"

# Marker the reserve script prints on success, followed by the two chosen ports.
SLICE_RESERVED_MARKER: Final[str] = "MNGR_SLICE_RESERVED"
# Marker the reserve script prints to stderr when the box is already at capacity.
SLICE_BOX_FULL_MARKER: Final[str] = "MNGR_SLICE_BOX_FULL"
# Marker the reserve script prints to stderr when no free port pair remains.
SLICE_NO_PORTS_MARKER: Final[str] = "MNGR_SLICE_NO_PORTS"

# Inside the slice VM, the vps_docker bake publishes the agent container's sshd on
# this guest port (matches VpsProviderConfig.container_ssh_port). Each is
# forwarded to a distinct host port on the bare-metal box so the slice looks like
# a VPS (box-IP + two ports).
_CONTAINER_SSH_GUEST_PORT: Final[int] = 2222
# Lima reserves guest port 22 for its own (loopback-only) SSH and rejects any
# portForward targeting it, so we make the VM's sshd ALSO listen on this extra
# guest port and forward that one to the box's external interface for the "outer"
# (root) SSH that the vps_docker provider uses.
_VM_SSH_GUEST_PORT: Final[int] = 2200


def _vm_ssh_extra_port_script(extra_port: int) -> str:
    """Bash that makes the VM's sshd listen on ``extra_port`` in addition to 22.

    Lima's own SSH needs guest 22, so we keep it and add the extra port (sshd
    drops the implicit 22 default once any ``Port`` line is present, so both must
    be listed). Idempotent across re-provisions.
    """
    return f"""\
#!/bin/bash
set -eux -o pipefail
if ! grep -q '^Port {extra_port}$' /etc/ssh/sshd_config; then
    printf 'Port 22\\nPort {extra_port}\\n' >> /etc/ssh/sshd_config
    systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || true
fi
"""


# Installs Docker on the VM so the shared vps_docker bake can run its container.
# Idempotent: get.docker.com no-ops when docker is already present.
_DOCKER_INSTALL_SCRIPT: Final[str] = """\
#!/bin/bash
set -eux -o pipefail
if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker 2>/dev/null || true
"""


# Installs inotify-tools on the VM so the vps_docker snapshot helper (the
# outer_trigger btrfs helper provisioned by mngr_vps) can run: its
# systemd unit execs `inotifywait` to watch for snapshot requests, and without
# it the unit crash-loops (exit 127) -- servicing requests only by accident of
# its restart cadence and emitting spurious "already exists" failures. The OVH
# VPS path gets this from host_setup's base packages; the slice VM (this VM is
# the helper's "outer") provisions it here, alongside Docker. `jq`, the helper's
# other dependency, is already installed by the base lima provisioning script.
# Idempotent: skips the apt run when inotifywait is already present.
_INOTIFY_TOOLS_INSTALL_SCRIPT: Final[str] = """\
#!/bin/bash
set -eux -o pipefail
if ! command -v inotifywait >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq inotify-tools
fi
"""


def _append_root_authorized_keys_script(extra_root_authorized_keys: tuple[str, ...]) -> str:
    """Bash that appends each key to the VM root's authorized_keys (idempotent).

    The base lima config already authorizes the provider's bake key for root
    (``root_authorized_public_key``); this adds further keys -- e.g. the pool
    management key the connector uses at lease time to inject the user's key and
    at release time to reach the VM -- without dropping the bake key.
    """
    append_lines = "\n".join(
        f'grep -qxF {shlex.quote(key)} "$AK" || printf \'%s\\n\' {shlex.quote(key)} >> "$AK"'
        for key in extra_root_authorized_keys
    )
    return f"""\
#!/bin/bash
set -eux -o pipefail
mkdir -p /root/.ssh
chmod 700 /root/.ssh
AK=/root/.ssh/authorized_keys
touch "$AK"
{append_lines}
chmod 600 "$AK"
chown -R root:root /root/.ssh
"""


def build_slice_lima_yaml(
    *,
    host_dir: str,
    vcpus: int,
    memory_mib: int,
    disk_gib: int,
    boot_disk_gib: int,
    disk_name: str,
    root_authorized_public_key: str,
    host_private_key_pem: str,
    host_public_key_openssh: str,
    vm_ssh_host_port: int | str,
    container_ssh_host_port: int | str,
    extra_root_authorized_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the Lima YAML for a VPS-parity slice VM.

    ``vm_ssh_host_port`` / ``container_ssh_host_port`` are normally the concrete
    box host ports, but may be placeholder tokens (str) when the YAML is a template
    whose ports are chosen and substituted on the box under the reservation lock.

    Produces a VM that, from the rest of the stack's perspective, looks like a
    freshly-delivered OVH VPS: reachable as root over SSH, with Docker installed
    and a btrfs data disk mounted at ``host_dir`` (so the shared vps_docker bake
    can run its container on it with no loopback). Two host ports on the box are
    forwarded in -- one to the VM's root sshd, one to the inner container sshd --
    and all other guest ports are suppressed (per-VM NAT already isolates VMs
    from each other). The ``boot_disk_gib`` boot disk (OS + Docker) plus the
    ``disk_gib`` btrfs data disk sum to the slice's disk budget, so the box is
    never over-provisioned on disk (lima would otherwise default the boot disk to
    100GiB, unaccounted).
    """
    config = generate_default_lima_yaml(
        volume_host_path=None,
        host_dir=host_dir,
        host_private_key_pem=host_private_key_pem,
        host_public_key_openssh=host_public_key_openssh,
        host_data_disk_name=disk_name,
        host_data_disk_size=f"{disk_gib}GiB",
        root_authorized_public_key=root_authorized_public_key,
    )
    config["cpus"] = vcpus
    config["memory"] = f"{memory_mib}MiB"
    # Size the boot disk explicitly (OS + Docker storage). Without this lima would
    # default it to 100GiB, which -- unaccounted against the per-slice budget --
    # would massively overcommit the box's disk.
    config["disk"] = f"{boot_disk_gib}GiB"
    # The base config already populated portForwards with mngr_lima's rules that
    # disable auto-forwarding of every guest port. Prepend the slice's two explicit
    # allow rules (matched first) so only the VM sshd and the inner container sshd
    # are reachable on the box's external interface; the inherited disable rules
    # that follow keep every other guest port private.
    config["portForwards"] = [
        {"guestPort": _VM_SSH_GUEST_PORT, "hostPort": vm_ssh_host_port, "hostIP": "0.0.0.0"},
        {"guestPort": _CONTAINER_SSH_GUEST_PORT, "hostPort": container_ssh_host_port, "hostIP": "0.0.0.0"},
        *config["portForwards"],
    ]
    # After the base provisioning (packages, sshd, root key, btrfs disk mount):
    # make sshd listen on the extra forwardable port, install Docker so the
    # shared vps_docker bake can run its container on this VM, and install
    # inotify-tools so the vps_docker snapshot helper's systemd unit can run.
    config["provision"] = list(config["provision"]) + [
        {"mode": "system", "script": _vm_ssh_extra_port_script(_VM_SSH_GUEST_PORT)},
        {"mode": "system", "script": _DOCKER_INSTALL_SCRIPT},
        {"mode": "system", "script": _INOTIFY_TOOLS_INSTALL_SCRIPT},
    ]
    # Authorize any extra keys for root (e.g. the pool management key the
    # connector uses at lease/release time), in addition to the bake key already
    # authorized by the base config.
    if extra_root_authorized_keys:
        config["provision"] = list(config["provision"]) + [
            {"mode": "system", "script": _append_root_authorized_keys_script(extra_root_authorized_keys)},
        ]
    return config


def build_slice_reserve_script(
    *,
    instance_name: str,
    disk_name: str,
    disk_gib: int,
    slot_count: int,
    port_range_start: int,
    port_range_end: int,
    yaml_template_text: str,
    lima_service_user: str,
) -> str:
    """Render the bash that atomically reserves one slice's box slot + host ports.

    Run as a single SSH command on the box (so the ``flock`` it holds is released the
    instant the command exits -- no stale locks). Under the lock it:

    1. counts all ``mngr-slice-`` data disks on the box (every env + legacy) and
       refuses if the box is already at ``slot_count`` -- the authoritative,
       cross-env over-allocation guard;
    2. computes the host ports already in use -- bound TCP ports PLUS the ports
       recorded in every existing lima instance's ``portForwards`` -- so a slot
       reserved-but-not-yet-started by another bake is respected;
    3. picks two free ports in ``[start, end)``, substitutes them into the shipped
       YAML template, then claims the slot WITHOUT booting: ``limactl disk create``
       + ``limactl create`` (which records the chosen ``portForwards`` into
       LIMA_HOME). The long ``limactl start`` runs later, after the lock is released.

    Prints ``MNGR_SLICE_RESERVED <vm_port> <container_port>`` on success.
    """
    encoded_yaml = base64.b64encode(yaml_template_text.encode()).decode()
    lock_path = f"/home/{lima_service_user}/{SLICE_ALLOC_LOCK_RELPATH}"
    return f"""\
#!/bin/bash
set -euo pipefail
export PATH=/usr/local/bin:$HOME/.local/bin:$PATH

# Serialize the whole reservation across every bake on this box (any env). The lock
# is held only for this short critical section and auto-released when this command
# exits; the long VM boot happens afterwards, unlocked.
exec 9>{shlex.quote(lock_path)}
flock 9

# 1. Cross-env capacity guard: count ALL slice data disks on the box. The
# trailing ``|| true`` keeps an empty box (grep matches nothing, exits 1) from
# aborting the whole script under ``set -o pipefail`` -- ``wc -l`` still yields 0.
disk_count=$(limactl disk list --json 2>/dev/null \
    | grep -oE '"name":[[:space:]]*"{SLICE_LIMA_INSTANCE_PREFIX}[^"]*"' | wc -l | tr -d ' ' || true)
if [ "$disk_count" -ge {slot_count} ]; then
    echo "{SLICE_BOX_FULL_MARKER} $disk_count/{slot_count}" >&2
    exit 4
fi

# 2. Host ports already in use: bound TCP ports + every existing instance's forwards.
used_ports_file=$(mktemp)
yaml_file=$(mktemp)
trap 'rm -f "$used_ports_file" "$yaml_file"' EXIT
ss -Htln 2>/dev/null | awk '{{print $4}}' | sed 's/.*://' | grep -E '^[0-9]+$' >> "$used_ports_file" || true
for inst_yaml in "$HOME"/.lima/*/lima.yaml; do
    [ -f "$inst_yaml" ] || continue
    grep -oE 'hostPort:[[:space:]]*[0-9]+' "$inst_yaml" | grep -oE '[0-9]+' >> "$used_ports_file" || true
done

pick_port() {{
    local p
    for ((p={port_range_start}; p<{port_range_end}; p++)); do
        if ! grep -qx "$p" "$used_ports_file"; then
            echo "$p"
            return 0
        fi
    done
    return 1
}}

vm_port=$(pick_port) || {{ echo "{SLICE_NO_PORTS_MARKER}" >&2; exit 3; }}
echo "$vm_port" >> "$used_ports_file"
container_port=$(pick_port) || {{ echo "{SLICE_NO_PORTS_MARKER}" >&2; exit 3; }}

# 3. Materialize the YAML (0600: it embeds the VM's SSH host private key) with the
#    chosen ports, claim the slot + record the ports without booting.
umask 077
echo {shlex.quote(encoded_yaml)} | base64 -d > "$yaml_file"
sed -i "s/{VM_SSH_PORT_PLACEHOLDER}/$vm_port/g; s/{CONTAINER_SSH_PORT_PLACEHOLDER}/$container_port/g" "$yaml_file"
limactl disk create {shlex.quote(disk_name)} --size {disk_gib}GiB
limactl create --name={shlex.quote(instance_name)} "$yaml_file"

echo "{SLICE_RESERVED_MARKER} $vm_port $container_port"
"""


def parse_reserved_ports(stdout: str) -> tuple[int, int]:
    """Parse the ``MNGR_SLICE_RESERVED <vm> <container>`` line from a reserve run's stdout.

    Raises ``SliceReserveOutputError`` if the marker line is missing or malformed.
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(SLICE_RESERVED_MARKER):
            parts = stripped.split()
            if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                return int(parts[1]), int(parts[2])
            raise SliceReserveOutputError(f"malformed {SLICE_RESERVED_MARKER} line: {stripped!r}")
    raise SliceReserveOutputError(f"no {SLICE_RESERVED_MARKER} line in reserve output: {stdout[-500:]!r}")

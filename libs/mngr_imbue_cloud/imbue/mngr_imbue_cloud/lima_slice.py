from typing import Any
from typing import Final

from imbue.mngr_lima.lima_yaml import generate_default_lima_yaml

# Inside the slice VM, the vps_docker bake publishes the agent container's sshd on
# this guest port (matches VpsDockerProviderConfig.container_ssh_port). Each is
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


def build_slice_lima_yaml(
    *,
    host_dir: str,
    vcpus: int,
    memory_mib: int,
    disk_gib: int,
    disk_name: str,
    root_authorized_public_key: str,
    host_private_key_pem: str,
    host_public_key_openssh: str,
    vm_ssh_host_port: int,
    container_ssh_host_port: int,
) -> dict[str, Any]:
    """Build the Lima YAML for a VPS-parity slice VM.

    Produces a VM that, from the rest of the stack's perspective, looks like a
    freshly-delivered OVH VPS: reachable as root over SSH, with Docker installed
    and a btrfs data disk mounted at ``host_dir`` (so the shared vps_docker bake
    can run its container on it with no loopback). Two host ports on the box are
    forwarded in -- one to the VM's root sshd, one to the inner container sshd --
    and all other guest ports are suppressed (per-VM NAT already isolates VMs
    from each other).
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
    # make sshd listen on the extra forwardable port, then install Docker so the
    # shared vps_docker bake can run its container on this VM.
    config["provision"] = list(config["provision"]) + [
        {"mode": "system", "script": _vm_ssh_extra_port_script(_VM_SSH_GUEST_PORT)},
        {"mode": "system", "script": _DOCKER_INSTALL_SCRIPT},
    ]
    return config

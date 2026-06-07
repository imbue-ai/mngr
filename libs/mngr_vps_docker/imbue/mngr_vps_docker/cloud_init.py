from imbue.mngr_vps_docker.host_setup import HostSetupStep
from imbue.mngr_vps_docker.host_setup import build_host_setup_steps


def generate_cloud_init_user_data(
    host_private_key: str,
    host_public_key: str,
    install_gvisor_runtime: bool,
) -> str:
    """Generate a cloud-init user_data script for VPS provisioning.

    Injects the SSH host key so we know it before the VPS boots (no TOFU),
    disables password authentication, then runs the shared host-setup steps
    (``host_setup.build_host_setup_steps``) as ``runcmd`` blocks. The host-setup
    steps are the single source of truth shared with
    ``host_setup.apply_host_setup_on_outer`` (the SSH re-provisioning path), so
    cloud-init backends (Vultr) and SSH-only backends (OVH) install the same
    pinned Docker, optional gVisor runsc, sshd tuning, and base packages.

    The first-boot-only pieces stay here in the cloud-init wrapper and are
    deliberately excluded from the shared steps: injecting the SSH host key,
    ``ssh_deletekeys``, disabling password auth, and the ``mngr-ready`` marker
    that ``_wait_for_cloud_init`` polls for.

    Cloud-init backends never need the qemu purge (their images don't ship the
    qemu guest agent), so it is left disabled here; OVH enables it via the SSH
    path instead.
    """
    steps = build_host_setup_steps(install_gvisor_runtime=install_gvisor_runtime, is_qemu_purge_enabled=False)
    runcmd_block = "\n".join(_render_runcmd_step(step) for step in steps)
    return f"""#cloud-config
ssh_deletekeys: true
ssh_keys:
  ed25519_private: |
{_indent(host_private_key, 4)}
  ed25519_public: {host_public_key}
ssh_pwauth: false
runcmd:
{runcmd_block}
  - touch /var/run/mngr-ready
"""


def _render_runcmd_step(step: HostSetupStep) -> str:
    """Render a host-setup step as a cloud-init ``runcmd`` ``- |`` script block.

    The six-space indent on the script body matches cloud-init's list nesting
    (two for the list item, four for the block scalar content).
    """
    return f"  - |\n{_indent(step.script, 6)}"


def _indent(text: str, spaces: int) -> str:
    """Indent each line of text by the given number of spaces."""
    prefix = " " * spaces
    return "\n".join(prefix + line for line in text.splitlines())

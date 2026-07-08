import shlex

from imbue.mngr_vps.host_setup import HostSetupStep
from imbue.mngr_vps.host_setup import MNGR_READY_MARKER_PATH
from imbue.mngr_vps.host_setup import build_auto_shutdown_command
from imbue.mngr_vps.host_setup import build_host_setup_steps


def generate_cloud_init_user_data(
    host_private_key: str,
    host_public_key: str,
    install_gvisor_runtime: bool,
    auto_shutdown_seconds: int | None = None,
    authorized_user_public_key: str | None = None,
) -> str:
    """Generate a cloud-init user_data script for VPS provisioning.

    Injects the SSH host key so we know it before the VPS boots (no TOFU),
    forwards the provider's SSH key from the cloud-image default user
    (admin / ec2-user / ubuntu / etc.) into root's authorized_keys so
    mngr can SSH in as root on AMIs that don't put the key there directly,
    disables password authentication, then runs the shared host-setup steps
    (``host_setup.build_host_setup_steps``) as ``runcmd`` blocks. The host-setup
    steps are the single source of truth shared with
    ``host_setup.apply_host_setup_on_outer`` (the SSH re-provisioning path), so
    cloud-init backends (Vultr, AWS) and SSH-only backends (OVH) install the same
    pinned Docker, optional gVisor runsc, sshd tuning, and base packages.

    The first-boot-only pieces stay here in the cloud-init wrapper and are
    deliberately excluded from the shared steps: injecting the SSH host key,
    ``ssh_deletekeys``, disabling password auth, forwarding the default user's
    key into root, and the ``mngr-ready`` marker that ``_wait_for_cloud_init``
    polls for.

    ``disable_root: false`` is set because cloud-init disables root SSH by
    default, prefixing root's authorized_keys with a
    ``no-pty,command="echo 'Please login as the user...'"`` wrapper.
    mngr_vps SSHes in as root and runs interactive shell-y commands via
    pyinfra, so that wrapper would silently break every poll.

    When ``auto_shutdown_seconds`` is set, the VPS schedules a
    ``shutdown -P +N`` from cloud-init, so the OS halts itself after the
    deadline. On AWS, paired with ``InstanceInitiatedShutdownBehavior=
    terminate``, this means the EC2 instance auto-terminates and stops
    billing even if the orchestrating process is killed. On Vultr the OS
    halts but billing continues until the VPS is destroyed -- still useful
    as a circuit-breaker so an abandoned VPS becomes obviously unreachable
    rather than silently consuming the agent slot.

    Cloud-init backends never need the qemu purge (their images don't ship the
    qemu guest agent), so it is left disabled here; OVH enables it via the SSH
    path instead.

    ``authorized_user_public_key``, when provided, is written straight into
    root's authorized_keys by cloud-init itself, independent of the
    copy-from-default-user step above. On GCE the provider's SSH key is
    provisioned into the ``ubuntu`` user's authorized_keys asynchronously by the
    google guest agent, which races the cloud-init ``runcmd`` copy and can leave
    root without the key; injecting it directly removes that dependency. Harmless
    and idempotent for the other cloud-init backends (the key also lands in root
    via the default-user copy, so a duplicate line is a no-op).
    """
    shutdown_block = ""
    if auto_shutdown_seconds is not None:
        shutdown_block = "  - " + build_auto_shutdown_command(auto_shutdown_seconds) + "\n"
    root_key_block = ""
    if authorized_user_public_key is not None:
        # Append directly to root's authorized_keys (the /root/.ssh dir is made
        # by the mkdir runcmd entry rendered just above this block), quoting the
        # key so its embedded spaces/comment survive the shell.
        root_key_block = (
            f"  - printf '%s\\n' {shlex.quote(authorized_user_public_key)} >> /root/.ssh/authorized_keys\n"
        )
    steps = build_host_setup_steps(install_gvisor_runtime=install_gvisor_runtime, is_qemu_purge_enabled=False)
    runcmd_block = "\n".join(_render_runcmd_step(step) for step in steps)
    return f"""#cloud-config
ssh_deletekeys: true
ssh_keys:
  ed25519_private: |
{_indent(host_private_key, 4)}
  ed25519_public: {host_public_key}
ssh_pwauth: false
# Cloud-init disables root SSH by default (``disable_root: true``), which
# prefixes root's authorized_keys with a ``no-port-forwarding,no-X11-forwarding,
# no-agent-forwarding,no-pty,command="echo 'Please login as the user...'"``
# wrapper. mngr_vps SSHes in as root and runs interactive shell-y
# commands via pyinfra, so that wrapper would silently break every poll.
# Set to false so root's authorized_keys takes the keys verbatim.
disable_root: false
runcmd:
  # Some cloud images install the provider-side SSH key into the default
  # user's authorized_keys (e.g. AWS Debian AMIs use 'admin', AL2/AL2023
  # use 'ec2-user', Ubuntu uses 'ubuntu') rather than root's. mngr_vps
  # SSHes in as root (see ``_make_outer_for_vps_ip``), so without this
  # copy the provisioning poll loop would hang trying to authenticate.
  # Vultr / OVH put the key on root directly so this is a no-op there.
  # Paired with ``disable_root: false`` above so cloud-init doesn't prefix
  # root's keys with a ``no-pty,command="echo 'Please login as ...'"``
  # wrapper that would silently break every poll command. Runs before the
  # shared host-setup steps so root SSH becomes reachable while the long
  # apt/Docker installs are still in flight.
  - mkdir -p /root/.ssh && chmod 0700 /root/.ssh
{root_key_block}  - for u in admin ec2-user ubuntu debian fedora centos; do if [ -f "/home/$u/.ssh/authorized_keys" ]; then cat "/home/$u/.ssh/authorized_keys" >> /root/.ssh/authorized_keys; fi; done
  - touch /root/.ssh/authorized_keys && chmod 0600 /root/.ssh/authorized_keys
{runcmd_block}
  - touch {MNGR_READY_MARKER_PATH}
{shutdown_block}"""


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

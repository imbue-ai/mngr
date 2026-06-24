import shlex

from imbue.mngr_vps.host_setup import HostSetupStep
from imbue.mngr_vps.host_setup import MNGR_READY_MARKER_PATH
from imbue.mngr_vps.host_setup import build_auto_shutdown_command
from imbue.mngr_vps.host_setup import build_host_setup_steps


def generate_gce_startup_script(
    host_private_key: str,
    host_public_key: str,
    install_gvisor_runtime: bool,
    auto_shutdown_seconds: int | None = None,
    authorized_user_public_key: str | None = None,
) -> str:
    """Generate a GCE ``startup-script`` for VPS provisioning (the cloud-init analog for GCP).

    Stock GCE images ship no cloud-init; the google-guest-agent runs the
    ``startup-script`` metadata on every image instead. This renders the same shared
    ``host_setup.build_host_setup_steps`` plus the GCE-only first-boot pieces that
    ``generate_cloud_init_user_data`` does, as one bash script.

    Two non-obvious points:

    - Host key: cloud-init's ``ssh_keys`` installs our key before sshd first starts;
      the guest agent has no pre-sshd hook, so sshd boots with a random key first.
      We install ours and restart sshd as the first action to shrink that window;
      the provisioner closes it by polling the live key until it matches
      (``GcpProvider._wait_for_expected_host_key``).
    - Each host-setup step runs in its own subshell so a step's early ``exit``
      cannot skip the ``mngr-ready`` marker; the outer ``set -e`` still aborts on
      any step failure.

    The guest agent re-runs this on every boot, so every step is idempotent.
    """
    root_key_inject = ""
    if authorized_user_public_key is not None:
        # Inject the access key straight into root, independent of the
        # copy-from-default-user step (the guest agent races that copy on GCE).
        root_key_inject = f"printf '%s\\n' {shlex.quote(authorized_user_public_key)} >> /root/.ssh/authorized_keys\n"

    steps = build_host_setup_steps(install_gvisor_runtime=install_gvisor_runtime, is_qemu_purge_enabled=False)
    host_setup_block = "\n".join(_render_step(step) for step in steps)

    shutdown_block = ""
    if auto_shutdown_seconds is not None:
        shutdown_block = build_auto_shutdown_command(auto_shutdown_seconds) + "\n"

    return f"""#!/bin/bash
set -e

# Install our SSH host key and restart sshd first, so the server stops serving its
# boot-generated random key as soon as possible (see _wait_for_expected_host_key).
cat > /etc/ssh/ssh_host_ed25519_key <<'MNGR_HOST_KEY_EOF'
{host_private_key.rstrip()}
MNGR_HOST_KEY_EOF
chmod 0600 /etc/ssh/ssh_host_ed25519_key
cat > /etc/ssh/ssh_host_ed25519_key.pub <<'MNGR_HOST_PUB_EOF'
{host_public_key.rstrip()}
MNGR_HOST_PUB_EOF
chmod 0644 /etc/ssh/ssh_host_ed25519_key.pub

# Disable password auth, allow key-based root login (mngr SSHes in as root). A
# drop-in wins because the stock sshd_config's ``Include`` precedes its values.
mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/60-mngr.conf <<'MNGR_SSHD_EOF'
PasswordAuthentication no
PermitRootLogin prohibit-password
MNGR_SSHD_EOF

systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart

# Forward the provider key into root (some images install it on the default user
# instead), so root SSH is reachable before the long Docker install.
mkdir -p /root/.ssh && chmod 0700 /root/.ssh
{root_key_inject}for u in admin ec2-user ubuntu debian fedora centos; do if [ -f "/home/$u/.ssh/authorized_keys" ]; then cat "/home/$u/.ssh/authorized_keys" >> /root/.ssh/authorized_keys; fi; done
touch /root/.ssh/authorized_keys && chmod 0600 /root/.ssh/authorized_keys

{host_setup_block}

touch {MNGR_READY_MARKER_PATH}
{shutdown_block}"""


def _render_step(step: HostSetupStep) -> str:
    """Render a host-setup step as a self-contained subshell block.

    The subshell isolates the step's exit status (so an early ``exit`` inside it
    does not terminate the whole startup-script) while the outer ``set -e`` still
    aborts if the subshell fails.
    """
    return f"# {step.description}\n(\n{step.script}\n)"

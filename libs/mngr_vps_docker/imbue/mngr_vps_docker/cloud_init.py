def generate_cloud_init_user_data(
    host_private_key: str,
    host_public_key: str,
) -> str:
    """Generate a cloud-init user_data script for VPS provisioning.

    Injects the SSH host key so we know it before the VPS boots (no TOFU),
    disables password authentication, installs Docker, and bumps sshd's
    ``MaxStartups`` / ``MaxSessions`` so the provisioning round-trips
    (image build + per-host setup + the imbue_cloud pool baking's many
    concurrent ``mngr exec`` / ``rsync`` / ``ssh`` calls) don't trip the
    default 10:30:100 pre-auth cap and lose connections mid-transfer.
    Mirrors the equivalent ``MaxSessions=100`` / ``MaxStartups=100:30:200``
    knob the lima provider applies to its VMs.
    """
    return f"""#cloud-config
ssh_deletekeys: true
ssh_keys:
  ed25519_private: |
{_indent(host_private_key, 4)}
  ed25519_public: {host_public_key}
ssh_pwauth: false
package_update: true
packages:
  - curl
  - ca-certificates
runcmd:
  - curl -fsSL https://get.docker.com | sh
  - systemctl enable docker
  - systemctl start docker
  - |
    if ! grep -q '^MaxSessions' /etc/ssh/sshd_config 2>/dev/null; then
        cat >> /etc/ssh/sshd_config <<SSHD_EOF
    MaxSessions 100
    MaxStartups 100:30:200
    SSHD_EOF
        systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || true
    fi
  - touch /var/run/mngr-ready
"""


def _indent(text: str, spaces: int) -> str:
    """Indent each line of text by the given number of spaces."""
    prefix = " " * spaces
    return "\n".join(prefix + line for line in text.splitlines())

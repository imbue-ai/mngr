def generate_cloud_init_user_data(
    host_private_key: str,
    host_public_key: str,
) -> str:
    """Generate a cloud-init user_data script for VPS provisioning.

    Injects the SSH host key so we know it before the VPS boots (no TOFU),
    disables password authentication, and installs Docker.
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
  - touch /var/run/mngr-ready
"""


def _indent(text: str, spaces: int) -> str:
    """Indent each line of text by the given number of spaces."""
    prefix = " " * spaces
    return "\n".join(prefix + line for line in text.splitlines())

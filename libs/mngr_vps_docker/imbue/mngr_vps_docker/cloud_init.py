# Idempotent shell that installs and registers the gVisor `runsc` runtime with the
# Docker daemon via gVisor's official APT repository, then re-registers it with
# `runsc install` and restarts Docker. Guarded so it is a no-op when runsc is
# already registered (e.g. baked into the base image), avoiding a needless apt
# round-trip and Docker restart. Indented to sit under a cloud-init `runcmd: - |`
# block (six spaces: two for the list item body, four for cloud-init's list nesting).
_GVISOR_RUNSC_INSTALL_RUNCMD = """  - |
    if ! docker info 2>/dev/null | grep -q runsc; then
        curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" > /etc/apt/sources.list.d/gvisor.list
        apt-get update && apt-get install -y runsc
        runsc install
        systemctl restart docker
    fi"""


def generate_cloud_init_user_data(
    host_private_key: str,
    host_public_key: str,
    install_gvisor_runtime: bool,
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

    ``rsync`` is explicit in the package list because
    ``mngr_vps_docker.container_setup.upload_directory_to_outer`` requires it for the
    build-context push. Standard Debian/Ubuntu cloud images ship rsync
    by default so this is belt-and-suspenders on cloud-init backends;
    non-cloud-init backends (e.g. OVH) install it from their own
    bootstrap path.

    ``inotify-tools`` and ``jq`` are needed by the per-host
    ``snapshot_helper.sh`` (installed later, after the btrfs mount is
    ready) -- pre-baked here so the helper install via SSH only needs
    to drop files in place, no extra package install round-trips.

    When ``install_gvisor_runtime`` is True, an idempotent runcmd step installs
    and registers the gVisor ``runsc`` runtime after Docker is up (a no-op when
    runsc is already present).
    """
    gvisor_install_block = f"\n{_GVISOR_RUNSC_INSTALL_RUNCMD}" if install_gvisor_runtime else ""
    # The gVisor install block dearmors the archive key with `gpg`, which is not
    # guaranteed to be present on minimal cloud images; install gnupg when needed.
    gvisor_packages = "\n  - gnupg" if install_gvisor_runtime else ""
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
  - rsync
  - inotify-tools
  - jq{gvisor_packages}
runcmd:
  - curl -fsSL https://get.docker.com | sh
  - systemctl enable docker
  - systemctl start docker{gvisor_install_block}
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

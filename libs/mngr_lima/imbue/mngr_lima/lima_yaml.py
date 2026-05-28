import os
import platform
import tempfile
from pathlib import Path

import yaml
from loguru import logger

from imbue.mngr.errors import MngrError
from imbue.mngr_lima.constants import DEFAULT_IMAGE_URL_AARCH64
from imbue.mngr_lima.constants import DEFAULT_IMAGE_URL_X86_64
from imbue.mngr_lima.constants import HOST_VOLUME_MOUNT_PATH


def _get_default_image_url(
    config_image_url_aarch64: str | None = None,
    config_image_url_x86_64: str | None = None,
) -> str:
    """Get the default image URL for the current architecture.

    Prefers config-level overrides when set, otherwise falls back to the
    hardcoded constants.
    """
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return config_image_url_aarch64 or DEFAULT_IMAGE_URL_AARCH64
    return config_image_url_x86_64 or DEFAULT_IMAGE_URL_X86_64


def _get_arch_string() -> str:
    """Get the Lima-compatible architecture string."""
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    return "x86_64"


def _disable_port_forwards_rules() -> list[dict]:
    """Lima portForwards entries that disable all guest -> host port forwarding.

    Lima always appends a fallback rule that forwards any guest TCP/UDP
    socket to host loopback; an empty list does not suppress it. User
    rules match the guest bind address literally (Lima 2.1.1), so a
    single `guestIP: 0.0.0.0` rule does not catch `127.0.0.1`-bound
    sockets and vice versa. We supply one rule for each so neither
    leaks. SSH uses Lima's separate ssh.localPort mechanism and is
    unaffected.
    """
    return [
        {
            "guestIPMustBeZero": True,
            "guestIP": "0.0.0.0",
            "proto": "any",
            "guestPortRange": [1, 65535],
            "ignore": True,
        },
        {
            "guestIP": "127.0.0.1",
            "proto": "any",
            "guestPortRange": [1, 65535],
            "ignore": True,
        },
    ]


def generate_default_lima_yaml(
    volume_host_path: Path | None,
    host_dir: str,
    custom_image_url: str | None = None,
    config_image_url_aarch64: str | None = None,
    config_image_url_x86_64: str | None = None,
    host_private_key_pem: str | None = None,
    host_public_key_openssh: str | None = None,
    host_data_disk_name: str | None = None,
    host_data_disk_size: str | None = None,
) -> dict:
    """Generate the default Lima YAML configuration.

    Args:
        volume_host_path: Path on the host machine for the 9p bind-mounted
            persistent volume. Pass None to omit the `mounts:` block entirely
            (used when the host_dir is instead backed by an additional disk).
        host_dir: Mount point inside the VM (e.g. /mngr).
        custom_image_url: Optional override for the image URL (takes highest priority).
        config_image_url_aarch64: Config-level override for aarch64 image URL.
        config_image_url_x86_64: Config-level override for x86_64 image URL.
        host_private_key_pem: Optional pre-generated SSH host private key (OpenSSH PEM format).
            When provided alongside host_public_key_openssh, the guest's sshd is configured
            to use this key as its ed25519 host key, eliminating the ssh-keyscan race during
            VM bring-up.
        host_public_key_openssh: Optional matching public key (single-line OpenSSH format,
            e.g. ``ssh-ed25519 AAAA...``).
        host_data_disk_name: Optional Lima `additionalDisks` name backing the
            host_dir. When provided alongside `host_data_disk_size`, an
            additional btrfs-formatted disk is attached and `host_dir` is
            symlinked into it via the provisioning script.
        host_data_disk_size: Logical size of the additional disk (Lima size
            string, e.g. '100GiB'). Ignored unless `host_data_disk_name` is set.
    """
    image_url = custom_image_url or _get_default_image_url(config_image_url_aarch64, config_image_url_x86_64)
    arch = _get_arch_string()

    config: dict = {
        "images": [
            {
                "location": image_url,
                "arch": arch,
            },
        ],
        "portForwards": _disable_port_forwards_rules(),
        # Provision required packages if not in the image
        "provision": [
            {
                "mode": "system",
                "script": _build_provisioning_script(
                    host_private_key_pem,
                    host_public_key_openssh,
                    host_dir=host_dir,
                    host_data_disk_name=host_data_disk_name,
                ),
            },
        ],
    }

    if volume_host_path is not None:
        config["mounts"] = [
            {
                "location": str(volume_host_path),
                "mountPoint": host_dir,
                "writable": True,
            },
        ]

    if host_data_disk_name is not None:
        if host_data_disk_size is None:
            raise MngrError("host_data_disk_size is required when host_data_disk_name is set")
        config["additionalDisks"] = [
            {
                "name": host_data_disk_name,
                "format": True,
                "fsType": "btrfs",
                "size": host_data_disk_size,
            },
        ]

    return config


def _build_provisioning_script(
    host_private_key_pem: str | None = None,
    host_public_key_openssh: str | None = None,
    host_dir: str = "/mngr",
    host_data_disk_name: str | None = None,
) -> str:
    """Build the Lima ``provision[mode=system]`` script that installs required packages, configures sshd, optionally lands the btrfs host-data disk at the canonical mount point, and (when a keypair is supplied) installs it as the guest's ed25519 sshd host key."""
    host_key_block = _build_host_key_block(host_private_key_pem, host_public_key_openssh)
    host_data_disk_block = _build_host_data_disk_block(host_data_disk_name, host_dir)
    return f"""\
#!/bin/bash
set -eux -o pipefail

# Install required packages if missing
PKGS_TO_INSTALL=""
command -v tmux >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL tmux"
command -v git >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL git"
command -v jq >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL jq"
command -v rsync >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL rsync"
command -v curl >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL curl"
command -v xxd >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL xxd"
test -x /usr/sbin/sshd || PKGS_TO_INSTALL="$PKGS_TO_INSTALL openssh-server"
test -f /etc/ssl/certs/ca-certificates.crt || PKGS_TO_INSTALL="$PKGS_TO_INSTALL ca-certificates"

if [ -n "$PKGS_TO_INSTALL" ]; then
    apt-get update -qq && apt-get install -y -qq $PKGS_TO_INSTALL
fi

mkdir -p /run/sshd

# Create /code directory for agent work directories (writable by all users).
# Lima VMs run as a regular user, not root, so /code must be pre-created.
mkdir -p /code && chmod 777 /code

# Install the caller-provided sshd host key (when given).
SSH_KEY_CHANGED=0
{host_key_block}

# Increase SSH limits so pyinfra can open enough concurrent channels and
# connections. The defaults (MaxSessions=10, MaxStartups=10:30:100) cause
# "channel open FAILED" and "no more sessions" errors during provisioning.
# Docker and Modal providers pass -o MaxSessions=100 when starting sshd
# directly; Lima VMs run sshd via systemd so we configure sshd_config.
SSHD_CONFIG_CHANGED=0
if ! grep -q '^MaxSessions' /etc/ssh/sshd_config 2>/dev/null; then
    cat >> /etc/ssh/sshd_config <<SSHD_EOF
MaxSessions 100
MaxStartups 100:30:200
SSHD_EOF
    SSHD_CONFIG_CHANGED=1
fi

if [ "$SSH_KEY_CHANGED" = "1" ] || [ "$SSHD_CONFIG_CHANGED" = "1" ]; then
    systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || true
fi

# Optional: if a Lima-managed btrfs additional disk was attached, bind-mount
# it at the canonical HOST_VOLUME_MOUNT_PATH and replace host_dir with a
# symlink. No-op when the block below is the inert comment placeholder.
{host_data_disk_block}
"""


def _build_host_data_disk_block(host_data_disk_name: str | None, host_dir: str) -> str:
    """Return a bash block that bind-mounts Lima's btrfs additional disk at HOST_VOLUME_MOUNT_PATH and replaces ``host_dir`` with a symlink to it, or an inert comment when no disk was attached.

    Lima auto-mounts named ``additionalDisks`` with ``format: true`` inside
    the guest under ``/mnt/lima-<disk_name>``. We bind-mount that to the
    canonical ``HOST_VOLUME_MOUNT_PATH`` (matching the host_volume_mount_path
    naming used by the other providers) and add an /etc/fstab entry so the
    bind survives reboots. ``host_dir`` is then replaced (even if it already
    exists as a real directory from a pre-btrfs image) with a symlink.
    """
    if host_data_disk_name is None:
        return "# (no host-data disk attached; host_dir uses today's bind mount or local fs)"
    lima_mount = f"/mnt/lima-{host_data_disk_name}"
    return f"""\
# Wait for Lima to finish auto-mounting the additional btrfs disk.
for _ in $(seq 1 60); do
    if mountpoint -q {lima_mount}; then
        break
    fi
    sleep 1
done
if ! mountpoint -q {lima_mount}; then
    echo "ERROR: Lima additional disk not mounted at {lima_mount}" >&2
    exit 1
fi

# Bind-mount the btrfs filesystem at the canonical host-volume path.
mkdir -p {HOST_VOLUME_MOUNT_PATH}
if ! mountpoint -q {HOST_VOLUME_MOUNT_PATH}; then
    mount --bind {lima_mount} {HOST_VOLUME_MOUNT_PATH}
fi
# Persist the bind across reboots (idempotent).
if ! grep -qE "[[:space:]]{HOST_VOLUME_MOUNT_PATH}[[:space:]]" /etc/fstab; then
    echo "{lima_mount} {HOST_VOLUME_MOUNT_PATH} none bind 0 0" >> /etc/fstab
fi

# Open up the btrfs root so the Lima default (non-root) user can write to
# host_dir without sudo (a fresh mkfs.btrfs leaves the root dir owned by
# root:root with 0755). Mirrors the chmod 777 the script applies to /code.
chmod 0777 {HOST_VOLUME_MOUNT_PATH}

# Replace host_dir with a symlink to the btrfs-backed mount. ``ln -sfn``
# alone won't replace an existing directory, so rm any real dir first.
if [ -L {host_dir} ] || [ ! -e {host_dir} ]; then
    ln -sfn {HOST_VOLUME_MOUNT_PATH} {host_dir}
else
    rm -rf {host_dir}
    ln -sfn {HOST_VOLUME_MOUNT_PATH} {host_dir}
fi"""


def _build_host_key_block(
    host_private_key_pem: str | None,
    host_public_key_openssh: str | None,
) -> str:
    """Return a bash block that installs the given keypair as the guest's ed25519 sshd host key, or an inert comment when either argument is ``None``."""
    if host_private_key_pem is None or host_public_key_openssh is None:
        return "# (no pre-injected host key)"
    return f"""\
umask 077
cat > /etc/ssh/ssh_host_ed25519_key <<'MNGR_LIMA_HOST_PRIV_KEY'
{host_private_key_pem.rstrip()}
MNGR_LIMA_HOST_PRIV_KEY
chmod 600 /etc/ssh/ssh_host_ed25519_key
chown root:root /etc/ssh/ssh_host_ed25519_key
umask 022
cat > /etc/ssh/ssh_host_ed25519_key.pub <<'MNGR_LIMA_HOST_PUB_KEY'
{host_public_key_openssh.strip()}
MNGR_LIMA_HOST_PUB_KEY
chmod 644 /etc/ssh/ssh_host_ed25519_key.pub
chown root:root /etc/ssh/ssh_host_ed25519_key.pub
# Remove other host-key types so sshd presents only the pre-trusted ed25519.
rm -f /etc/ssh/ssh_host_rsa_key /etc/ssh/ssh_host_rsa_key.pub
rm -f /etc/ssh/ssh_host_ecdsa_key /etc/ssh/ssh_host_ecdsa_key.pub
rm -f /etc/ssh/ssh_host_dsa_key /etc/ssh/ssh_host_dsa_key.pub
SSH_KEY_CHANGED=1"""


def write_lima_yaml(config: dict, output_path: Path | None = None) -> Path:
    """Write a Lima YAML config to a file.

    If output_path is None, writes to a temporary file.
    Returns the path to the written file.
    """
    if output_path is None:
        fd, path_str = tempfile.mkstemp(suffix=".yaml", prefix="mngr-lima-")
        output_path = Path(path_str)
        os.close(fd)

    output_path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    logger.trace("Wrote Lima YAML config to {}", output_path)
    return output_path


def load_user_lima_yaml(yaml_path: Path) -> dict:
    """Load a user-provided Lima YAML config file."""
    content = yaml_path.read_text()
    config = yaml.safe_load(content)
    if not isinstance(config, dict):
        raise MngrError(f"Lima YAML config must be a mapping, got {type(config).__name__}")
    return config


_LIST_EXTEND_KEYS = frozenset({"provision", "mounts", "additionalDisks"})
_LOCKED_KEYS = frozenset({"portForwards"})


def merge_lima_yaml(base: dict, override: dict) -> dict:
    """Merge a user-provided YAML config with the base config.

    Most keys are replaced by the user's value. For `provision` and `mounts`,
    the user's list is appended after the base's (base entries first) so mngr's
    load-bearing entries -- the host-key injection in `provision`, the `/mngr`
    volume mount in `mounts` -- are not silently dropped by a user who only
    meant to add their own. Lima runs `provision[mode=system]` scripts in list
    order, so base-first means mngr's host-key swap runs before any user
    script. Keys in `_LOCKED_KEYS` (currently `portForwards`) are retained
    from the base so a user `--file` YAML cannot reopen security-sensitive
    defaults.
    """
    merged = dict(base)
    for key, value in override.items():
        if key in _LOCKED_KEYS:
            logger.trace("Ignoring locked key {!r} in user Lima YAML", key)
            continue
        if key in _LIST_EXTEND_KEYS and isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = list(merged[key]) + list(value)
        else:
            merged[key] = value
    return merged


def parse_build_args_for_yaml_path(build_args: tuple[str, ...]) -> Path | None:
    """Parse --file from build_args to extract a Lima YAML config path.

    Returns the path if found, None otherwise.
    """
    for i, arg in enumerate(build_args):
        if arg == "--file" and i + 1 < len(build_args):
            return Path(build_args[i + 1])
        if arg.startswith("--file="):
            return Path(arg.split("=", 1)[1])
    return None

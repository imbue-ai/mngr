import os
import platform
import tempfile
from pathlib import Path

import yaml
from loguru import logger

from imbue.mngr.errors import MngrError
from imbue.mngr_lima.constants import DEFAULT_IMAGE_URL_AARCH64
from imbue.mngr_lima.constants import DEFAULT_IMAGE_URL_X86_64
from imbue.mngr_lima.constants import lima_host_data_disk_mount_path


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
    root_authorized_public_key: str | None = None,
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
        root_authorized_public_key: Optional client public key to authorize for
            the VM's root user. When provided, the provisioning script enables
            key-based root login so mngr can run the agent as root.
    """
    image_url = custom_image_url or _get_default_image_url(config_image_url_aarch64, config_image_url_x86_64)
    arch = _get_arch_string()

    port_forwards = _disable_port_forwards_rules()
    provision_script = _build_provisioning_script(
        host_private_key_pem,
        host_public_key_openssh,
        host_dir=host_dir,
        host_data_disk_name=host_data_disk_name,
        root_authorized_public_key=root_authorized_public_key,
    )

    config: dict = {
        "images": [
            {
                "location": image_url,
                "arch": arch,
            },
        ],
        "portForwards": port_forwards,
        # Provision required packages if not in the image
        "provision": [
            {
                "mode": "system",
                "script": provision_script,
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
    root_authorized_public_key: str | None = None,
) -> str:
    """Build the Lima ``provision[mode=system]`` script that installs required packages, configures sshd, optionally lands the btrfs host-data disk at the canonical mount point, optionally enables key-based root login (when running the agent as root), and (when a keypair is supplied) installs it as the guest's ed25519 sshd host key."""
    host_key_block = _build_host_key_block(host_private_key_pem, host_public_key_openssh)
    host_data_disk_block = _build_host_data_disk_block(host_data_disk_name, host_dir)
    root_authorized_keys_block = _build_root_authorized_keys_block(root_authorized_public_key)
    # Permit key-based root login only when the agent runs as root; the default
    # (non-root) path leaves sshd's PermitRootLogin untouched.
    permit_root_login_line = "\nPermitRootLogin prohibit-password" if root_authorized_public_key is not None else ""
    # The btrfs data disk is formatted in-guest (see _build_format_and_mount_data_disk_block),
    # so mkfs.btrfs must be present; minimal images (e.g. Debian genericcloud) don't ship it.
    btrfs_pkg_line = (
        '\ncommand -v mkfs.btrfs >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL btrfs-progs"'
        if host_data_disk_name is not None
        else ""
    )
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
command -v flock >/dev/null 2>&1 || PKGS_TO_INSTALL="$PKGS_TO_INSTALL util-linux"
test -x /usr/sbin/sshd || PKGS_TO_INSTALL="$PKGS_TO_INSTALL openssh-server"
test -f /etc/ssl/certs/ca-certificates.crt || PKGS_TO_INSTALL="$PKGS_TO_INSTALL ca-certificates"{btrfs_pkg_line}

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
MaxStartups 100:30:200{permit_root_login_line}
SSHD_EOF
    SSHD_CONFIG_CHANGED=1
fi

# Authorize mngr's client key for root when the agent runs as root, so mngr can
# ssh in as root. No-op (inert comment) in the default non-root path.
{root_authorized_keys_block}

if [ "$SSH_KEY_CHANGED" = "1" ] || [ "$SSHD_CONFIG_CHANGED" = "1" ]; then
    systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || true
fi

# Optional: if a btrfs additional disk was attached, format + mount it at the
# canonical path and symlink host_dir to it. No-op when the block below is the
# inert comment placeholder.
{host_data_disk_block}
"""


def _build_root_authorized_keys_block(root_authorized_public_key: str | None) -> str:
    """Return a bash block that authorizes ``root_authorized_public_key`` for the VM's root user, or an inert comment."""
    if root_authorized_public_key is None:
        return "# (no client key to authorize for root)"
    return f"""\
mkdir -p /root/.ssh
chmod 700 /root/.ssh
cat > /root/.ssh/authorized_keys <<'MNGR_LIMA_ROOT_KEY'
{root_authorized_public_key.strip()}
MNGR_LIMA_ROOT_KEY
chmod 600 /root/.ssh/authorized_keys
chown -R root:root /root/.ssh"""


def _build_host_data_disk_block(host_data_disk_name: str | None, host_dir: str) -> str:
    """Return a bash block that symlinks ``host_dir`` to Lima's auto-mounted btrfs disk, or an inert comment when no disk was attached.

    Lima auto-mounts named ``additionalDisks`` with ``format: true`` inside
    the guest under ``/mnt/lima-<disk_name>`` via a generated systemd .mount
    unit. We symlink ``host_dir`` directly to that path -- no intermediate
    bind-mount onto a canonical "host-volume" path. The bind-mount approach
    we tried first stacked an empty-ext4 layer under the btrfs mount when
    the fstab-generated unit fired before Lima's auto-mount; symlinking
    directly to Lima's path avoids the ordering issue entirely.
    """
    if host_data_disk_name is None:
        return "# (no host-data disk attached; host_dir uses today's bind mount or local fs)"
    lima_mount = lima_host_data_disk_mount_path(host_data_disk_name)
    format_and_mount_block = _build_format_and_mount_data_disk_block(host_data_disk_name)
    return f"""\
# Format + mount the additional btrfs disk ourselves (Lima can't on minimal images).
{format_and_mount_block}

# Open up the btrfs root so the Lima default (non-root) user can write to
# host_dir without sudo (a fresh mkfs.btrfs leaves the root dir owned by
# root:root with 0755). Mirrors the chmod 777 the script applies to /code.
chmod 0777 {lima_mount}

# Replace host_dir with a symlink to the mounted btrfs disk.
# ``ln -sfn`` alone won't replace an existing directory, so rm any real
# dir first. Idempotent across re-runs.
if [ -L {host_dir} ] || [ ! -e {host_dir} ]; then
    ln -sfn {lima_mount} {host_dir}
else
    rm -rf {host_dir}
    ln -sfn {lima_mount} {host_dir}
fi"""


def _build_format_and_mount_data_disk_block(host_data_disk_name: str) -> str:
    """Return a bash block that formats (if needed) and mounts the Lima btrfs data disk.

    Minimal cloud images (e.g. Debian genericcloud) ship no ``mkfs.btrfs``, so
    Lima's guestagent cannot format the ``format: true`` btrfs additionalDisk at
    boot -- it partitions the disk but leaves it unformatted, and nothing mounts
    at ``/mnt/lima-<name>``. ``btrfs-progs`` is installed earlier in the
    provisioning script; here we format + mount the disk ourselves at exactly the
    path Lima would have used, so the per-host btrfs subvolume can be created.

    Idempotent: a no-op when already mounted, and ``mkfs`` runs only when the
    device is not already btrfs (so re-provisioning and existing snapshot data
    survive). The data disk is identified as the one ``disk``-type block device
    that is not the root disk -- in this mode there is exactly one additional
    disk. On later boots Lima's guestagent handles the mount itself (``btrfs-progs``
    now persists in the image's root fs), so this is first-boot setup; the mount
    path is the same either way.
    """
    lima_mount = lima_host_data_disk_mount_path(host_data_disk_name)
    return f"""\
if ! mountpoint -q {lima_mount}; then
    DATA_ROOT_SRC="$(findmnt -no SOURCE /)"
    DATA_ROOT_DISK="$(lsblk -no PKNAME "$DATA_ROOT_SRC" | head -1)"
    if [ -z "$DATA_ROOT_DISK" ]; then
        echo "ERROR: could not determine root disk for $DATA_ROOT_SRC; refusing to format data disk" >&2
        exit 1
    fi
    DATA_DISK=""
    for DATA_CANDIDATE in $(lsblk -dn -o NAME,TYPE | awk '$2=="disk"{{print $1}}'); do
        if [ "$DATA_CANDIDATE" != "$DATA_ROOT_DISK" ]; then
            DATA_DISK="$DATA_CANDIDATE"
            break
        fi
    done
    if [ -z "$DATA_DISK" ]; then
        echo "ERROR: no additional data disk found to back {lima_mount}" >&2
        exit 1
    fi
    DATA_PART="$(lsblk -ln -o NAME "/dev/$DATA_DISK" | sed -n '2p')"
    DATA_DEV="/dev/${{DATA_PART:-$DATA_DISK}}"
    if ! blkid -t TYPE=btrfs "$DATA_DEV" >/dev/null 2>&1; then
        mkfs.btrfs -f "$DATA_DEV"
    fi
    mkdir -p {lima_mount}
    mount "$DATA_DEV" {lima_mount}
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

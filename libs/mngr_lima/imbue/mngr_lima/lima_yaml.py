import os
import platform
import tempfile
from pathlib import Path

import yaml
from loguru import logger

from imbue.mngr.errors import MngrError
from imbue.mngr_lima.constants import DEFAULT_IMAGE_URL_AARCH64
from imbue.mngr_lima.constants import DEFAULT_IMAGE_URL_X86_64


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

    Lima appends one internal fallback rule that forwards any TCP/UDP guest
    socket on guestIP 127.0.0.1 -- which also matches bind addresses 0.0.0.0,
    ::, and ::1 -- to host 127.0.0.1. An empty user list does not override
    that fallback. We supply Lima's documented "disable all forwarding"
    catchall (one rule, proto any, full port range, ignore true) so the
    fallback never fires. Lima manages the SSH port outside of portForwards,
    so it remains reachable.
    """
    return [
        {
            "guestIP": "0.0.0.0",
            "proto": "any",
            "guestPortRange": [1, 65535],
            "ignore": True,
        },
    ]


def generate_default_lima_yaml(
    volume_host_path: Path,
    host_dir: str,
    custom_image_url: str | None = None,
    config_image_url_aarch64: str | None = None,
    config_image_url_x86_64: str | None = None,
    host_private_key_pem: str | None = None,
    host_public_key_openssh: str | None = None,
) -> dict:
    """Generate the default Lima YAML configuration.

    Args:
        volume_host_path: Path on the host machine for the persistent volume.
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
        "mounts": [
            {
                "location": str(volume_host_path),
                "mountPoint": host_dir,
                "writable": True,
            },
        ],
        "portForwards": _disable_port_forwards_rules(),
        # Provision required packages if not in the image
        "provision": [
            {
                "mode": "system",
                "script": _build_provisioning_script(host_private_key_pem, host_public_key_openssh),
            },
        ],
    }

    return config


def _build_provisioning_script(
    host_private_key_pem: str | None = None,
    host_public_key_openssh: str | None = None,
) -> str:
    """Build the Lima ``provision[mode=system]`` script that installs required packages, configures sshd, and (when a keypair is supplied) installs it as the guest's ed25519 sshd host key."""
    host_key_block = _build_host_key_block(host_private_key_pem, host_public_key_openssh)
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
"""


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


_LIST_EXTEND_KEYS = frozenset({"provision", "mounts"})
_LOCKED_KEYS = frozenset({"portForwards"})


def merge_lima_yaml(base: dict, override: dict) -> dict:
    """Merge a user-provided YAML config with the base config.

    Most keys are replaced by the user's value. For `provision` and `mounts`,
    the user's list is appended after the base's (base entries first) so mngr's
    load-bearing entries -- the host-key injection in `provision`, the `/mngr`
    volume mount in `mounts` -- are not silently dropped by a user who only
    meant to add their own. Lima runs `provision[mode=system]` scripts in list
    order, so base-first means mngr's host-key swap runs before any user
    script. Keys in `_LOCKED_KEYS` (currently `portForwards`) are not
    overridable -- the base's value wins, with a warning -- so security-
    sensitive defaults can't be unset via a user `--file` YAML.
    """
    merged = dict(base)
    for key, value in override.items():
        if key in _LOCKED_KEYS:
            logger.warning("Ignoring locked key {!r} in user-provided Lima YAML.", key)
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

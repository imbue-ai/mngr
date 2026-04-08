import os
import platform
import tempfile
from pathlib import Path

import yaml
from loguru import logger

from imbue.mngr.errors import MngrError
from imbue.mngr_lima.constants import DEFAULT_IMAGE_URL_AARCH64
from imbue.mngr_lima.constants import DEFAULT_IMAGE_URL_X86_64


def _get_default_image_url() -> str:
    """Get the default image URL for the current architecture."""
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return DEFAULT_IMAGE_URL_AARCH64
    return DEFAULT_IMAGE_URL_X86_64


def _get_arch_string() -> str:
    """Get the Lima-compatible architecture string."""
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    return "x86_64"


def generate_default_lima_yaml(
    volume_host_path: Path,
    host_dir: str,
    custom_image_url: str | None = None,
) -> dict:
    """Generate the default Lima YAML configuration.

    Args:
        volume_host_path: Path on the host machine for the persistent volume.
        host_dir: Mount point inside the VM (e.g. /mngr).
        custom_image_url: Optional override for the image URL.
    """
    image_url = custom_image_url or _get_default_image_url()
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
        # Disable port forwarding -- use SSH for everything
        "portForwards": [],
        # Provision required packages if not in the image
        "provision": [
            {
                "mode": "system",
                "script": _build_provisioning_script(),
            },
        ],
    }

    return config


def _build_provisioning_script() -> str:
    """Build the cloud-init provisioning script that ensures required packages are installed."""
    return """\
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
"""


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


def merge_lima_yaml(base: dict, override: dict) -> dict:
    """Merge a user-provided YAML config with the base config.

    User-provided values override base values. Lists are replaced, not merged.
    """
    merged = dict(base)
    for key, value in override.items():
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

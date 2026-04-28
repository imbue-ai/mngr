"""Translate MINDS_ROOT_NAME into MNGR_HOST_DIR and MNGR_PREFIX.

This must run before any ``imbue.mngr.*`` module is imported, because mngr reads
``MNGR_HOST_DIR`` and ``MNGR_PREFIX`` during its own module-level initialization
(plugin manager construction, config discovery, etc.).

Kept intentionally minimal -- only stdlib and loguru -- so it stays cheap to
import and cannot accidentally pull in mngr before translation happens.
"""

import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Final

import tomlkit
from loguru import logger

MINDS_ROOT_NAME_ENV_VAR: Final[str] = "MINDS_ROOT_NAME"
DEFAULT_MINDS_ROOT_NAME: Final[str] = "minds"
MINDS_ROOT_NAME_PATTERN: Final[str] = r"[a-z0-9_-]+"


def resolve_minds_root_name() -> str:
    """Read MINDS_ROOT_NAME from the environment or return the default.

    Validates the value against MINDS_ROOT_NAME_PATTERN and exits with status
    1 if invalid. Validation is duplicated here (instead of going through a
    pydantic primitive) so this module never has to import pydantic/mngr.
    """
    value = os.environ.get(MINDS_ROOT_NAME_ENV_VAR, DEFAULT_MINDS_ROOT_NAME)
    if not re.fullmatch(MINDS_ROOT_NAME_PATTERN, value):
        logger.error("{} must match {!r}; got {!r}", MINDS_ROOT_NAME_ENV_VAR, MINDS_ROOT_NAME_PATTERN, value)
        sys.exit(1)
    return value


def minds_data_dir_for(root_name: str) -> Path:
    """Return the minds data directory for a given root name (e.g. ~/.minds)."""
    return Path.home() / ".{}".format(root_name)


def mngr_host_dir_for(root_name: str) -> Path:
    """Return the mngr host directory for a given root name (e.g. ~/.minds/mngr)."""
    return minds_data_dir_for(root_name) / "mngr"


def mngr_prefix_for(root_name: str) -> str:
    """Return the mngr prefix for a given root name (e.g. minds-)."""
    return "{}-".format(root_name)


def _ensure_mngr_settings(root_name: str) -> None:
    """Ensure the mngr settings.toml has an SSH provider configured.

    The SSH provider reads dynamic host entries written by the leased-host
    flow. Without this provider, ``mngr rename``/``mngr start`` cannot
    discover agents on leased hosts.

    Only adds the SSH provider section if it is missing -- does not
    overwrite any existing configuration.
    """
    mngr_host_dir = mngr_host_dir_for(root_name)
    root_config_path = mngr_host_dir / "config.toml"
    if not root_config_path.exists():
        return
    root_config = tomllib.loads(root_config_path.read_text())
    profile_id = root_config.get("profile")
    if not profile_id:
        return
    settings_dir = mngr_host_dir / "profiles" / profile_id
    if not settings_dir.exists():
        return
    settings_path = settings_dir / "settings.toml"

    data_dir = minds_data_dir_for(root_name)
    expected_dynamic_hosts_file = str(data_dir / "ssh" / "dynamic_hosts.toml")

    if settings_path.exists():
        existing = tomllib.loads(settings_path.read_text())
        providers = existing.get("providers", {})
        ssh_config = providers.get("ssh", {})
        if (
            ssh_config.get("backend") == "ssh"
            and ssh_config.get("dynamic_hosts_file") == expected_dynamic_hosts_file
            and ssh_config.get("host_dir") == "/mngr"
        ):
            return
    else:
        existing = {}

    # Build the config content with the SSH provider section
    ssh_config = {
        "providers": {
            "ssh": {
                "backend": "ssh",
                "host_dir": "/mngr",
                "dynamic_hosts_file": expected_dynamic_hosts_file,
            },
        },
    }

    if settings_path.exists():
        doc = tomlkit.loads(settings_path.read_text())
        doc.update(ssh_config)
    else:
        doc = tomlkit.document()
        doc.update(ssh_config)

    settings_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = settings_path.with_suffix(".tmp")
    tmp_path.write_text(tomlkit.dumps(doc))
    tmp_path.rename(settings_path)
    logger.debug("Updated mngr settings at {} with SSH provider config", settings_path)


def apply_bootstrap() -> None:
    """Set MNGR_HOST_DIR and MNGR_PREFIX in os.environ from MINDS_ROOT_NAME.

    Must be called before any ``imbue.mngr.*`` module is imported. Explicit
    ``MNGR_HOST_DIR``/``MNGR_PREFIX`` values already in the environment take
    precedence -- they are not overwritten, so tests and advanced users can
    still pin them independently.
    """
    root_name = resolve_minds_root_name()
    os.environ.setdefault("MNGR_HOST_DIR", str(mngr_host_dir_for(root_name)))
    os.environ.setdefault("MNGR_PREFIX", mngr_prefix_for(root_name))
    _ensure_mngr_settings(root_name)

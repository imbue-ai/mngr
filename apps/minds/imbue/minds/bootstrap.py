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
from pathlib import Path
from typing import Final

from loguru import logger

MINDS_ROOT_NAME_ENV_VAR: Final[str] = "MINDS_ROOT_NAME"
DEFAULT_MINDS_ROOT_NAME: Final[str] = "minds"
MINDS_ROOT_NAME_PATTERN: Final[str] = r"[a-z0-9_-]+"


def resolve_minds_root_name() -> str:
    """Read MINDS_ROOT_NAME from the environment or return the default.

    Validates the value against MINDS_ROOT_NAME_PATTERN and prints an error +
    exits 1 if invalid. A duplicate of MindsRootName's validation in stdlib
    form so that this module never has to import pydantic/mngr.
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

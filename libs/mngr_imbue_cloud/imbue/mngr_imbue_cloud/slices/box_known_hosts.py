"""Per-command known_hosts files pinning a bare-metal box's sshd key for the slice clients.

Every box SSH pins the box's recorded host key through a throwaway known_hosts
file next to the management key: one file per command (concurrent commands to
the same box must not unlink each other's file mid-connect), removed once the
command has run, with a sweep of anything a killed process left behind.
"""

import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts

BOX_KNOWN_HOSTS_FILE_PREFIX: Final[str] = ".box_known_hosts_"
# A file older than this belongs to a process that never removed it.
BOX_KNOWN_HOSTS_MAX_AGE_SECONDS: Final[float] = 24 * 3600.0


def write_box_known_hosts_file(base_dir: Path, box_address: str, box_ssh_port: int, box_host_public_key: str) -> Path:
    """Write a fresh known_hosts file pinning ``box_host_public_key`` for the box endpoint and return its path."""
    sweep_stale_box_known_hosts_files(base_dir)
    endpoint_digest = hashlib.sha256(f"{box_address}:{box_ssh_port}".encode()).hexdigest()[:16]
    known_hosts_fd, known_hosts_name = tempfile.mkstemp(
        prefix=f"{BOX_KNOWN_HOSTS_FILE_PREFIX}{endpoint_digest}_", dir=base_dir
    )
    os.close(known_hosts_fd)
    known_hosts_path = Path(known_hosts_name)
    add_host_to_known_hosts(known_hosts_path, box_address, box_ssh_port, box_host_public_key)
    return known_hosts_path


def remove_box_known_hosts_file(known_hosts_path: Path) -> None:
    """Remove a file written by :func:`write_box_known_hosts_file`; a missing file is fine."""
    known_hosts_path.unlink(missing_ok=True)


def sweep_stale_box_known_hosts_files(base_dir: Path) -> int:
    """Delete the box known_hosts files in ``base_dir`` older than the max age; return how many went."""
    current_time = time.time()
    removed_count = 0
    for candidate in base_dir.glob(f"{BOX_KNOWN_HOSTS_FILE_PREFIX}*"):
        try:
            if current_time - candidate.stat().st_mtime > BOX_KNOWN_HOSTS_MAX_AGE_SECONDS:
                candidate.unlink()
                removed_count += 1
        except OSError as exc:
            logger.debug("Could not sweep stale box known_hosts file {}: {}", candidate, exc)
    return removed_count

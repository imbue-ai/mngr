"""A startup read of a Full Disk Access protected file, so that macOS lists Imbue Studio.

macOS offers no API, prompt, or entitlement that asks for Full Disk Access: the
user turns it on by hand in System Settings > Privacy & Security > Full Disk
Access. That list shows an application only once a process the application is
responsible for has been refused a protected read; one that has never tried is
absent, and a user who goes looking for Studio has to add it with the "+"
button, which few know to do. This process is spawned by the Electron app, so
a refusal here is charged to Imbue Studio and lists it, unchecked, where the
user can then find it. Nothing reads the outcome yet beyond the log.
"""

from enum import auto
from pathlib import Path

from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.minds.desktop_client.local_prerequisites import HostPlatform
from imbue.mngr.utils.thread_cleanup import start_mngr_thread


class FullDiskAccessProbeOutcome(UpperCaseStrEnum):
    """What reading the protected file came to."""

    READABLE = auto()
    REFUSED = auto()
    MISSING = auto()


def full_disk_access_protected_path() -> Path:
    """The per-user TCC database, which exists on every macOS account and which only Full Disk Access lets an application read."""
    return Path.home() / "Library" / "Application Support" / "com.apple.TCC" / "TCC.db"


def probe_full_disk_access_protected_path(path: Path) -> FullDiskAccessProbeOutcome:
    """Read the first byte of ``path`` and report whether the OS let this process do so.

    An open rather than a stat, because that is the operation macOS refuses
    (with EPERM) and records against the responsible application.
    """
    try:
        with path.open("rb") as protected_file:
            protected_file.read(1)
    except PermissionError:
        return FullDiskAccessProbeOutcome.REFUSED
    except FileNotFoundError:
        return FullDiskAccessProbeOutcome.MISSING
    return FullDiskAccessProbeOutcome.READABLE


def start_full_disk_access_probe(root_concurrency_group: ConcurrencyGroup, host_platform: HostPlatform) -> None:
    """On macOS, read the protected file once on a background thread; elsewhere do nothing.

    The read is the point, not its answer: nothing waits for the thread, and the
    outcome is only logged.
    """
    if host_platform is not HostPlatform.DARWIN:
        return
    start_mngr_thread(
        root_concurrency_group,
        target=_run_full_disk_access_probe,
        name="full-disk-access-probe",
        daemon=True,
        is_checked=False,
    )


def _run_full_disk_access_probe() -> None:
    path = full_disk_access_protected_path()
    outcome = probe_full_disk_access_protected_path(path)
    logger.debug("Probed the Full Disk Access protected file {}: {}", path, outcome.value)

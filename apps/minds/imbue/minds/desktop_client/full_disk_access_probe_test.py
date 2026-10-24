import os
import stat
import threading
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.full_disk_access_probe import FullDiskAccessProbeOutcome
from imbue.minds.desktop_client.full_disk_access_probe import full_disk_access_protected_path
from imbue.minds.desktop_client.full_disk_access_probe import probe_full_disk_access_protected_path
from imbue.minds.desktop_client.full_disk_access_probe import start_full_disk_access_probe
from imbue.minds.desktop_client.local_prerequisites import HostPlatform


def test_probe_reports_a_readable_file(tmp_path: Path) -> None:
    protected = tmp_path / "TCC.db"
    protected.write_bytes(b"SQLite format 3")
    assert probe_full_disk_access_protected_path(protected) is FullDiskAccessProbeOutcome.READABLE


def test_probe_reports_a_missing_file(tmp_path: Path) -> None:
    assert probe_full_disk_access_protected_path(tmp_path / "absent.db") is FullDiskAccessProbeOutcome.MISSING


def test_probe_reports_a_refused_read(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads a file whatever its mode, so no refusal can be staged")
    protected = tmp_path / "TCC.db"
    protected.write_bytes(b"SQLite format 3")
    protected.chmod(0)
    try:
        assert probe_full_disk_access_protected_path(protected) is FullDiskAccessProbeOutcome.REFUSED
    finally:
        protected.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_protected_path_is_the_user_tcc_database() -> None:
    path = full_disk_access_protected_path()
    assert path.is_relative_to(Path.home())
    assert path.name == "TCC.db"


def test_start_probe_does_nothing_off_macos() -> None:
    with ConcurrencyGroup(name="test-full-disk-access-probe") as concurrency_group:
        start_full_disk_access_probe(concurrency_group, HostPlatform.LINUX)
        assert all(thread.name != "full-disk-access-probe" for thread in threading.enumerate())

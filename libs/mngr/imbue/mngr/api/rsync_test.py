"""Unit tests for ``api/rsync.py``."""

from pathlib import Path
from typing import cast

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.rsync import RsyncEndpointError
from imbue.mngr.api.rsync import RsyncResult
from imbue.mngr.api.rsync import _build_remote_rsync_command
from imbue.mngr.api.rsync import _build_rsync_command
from imbue.mngr.api.rsync import rsync
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import UncommittedChangesMode

# =============================================================================
# RsyncResult model
# =============================================================================


def test_rsync_result_can_be_created_with_all_fields() -> None:
    result = RsyncResult(
        files_transferred=10,
        bytes_transferred=1024,
        source_path=Path("/source"),
        destination_path=Path("/dest"),
        is_dry_run=False,
    )

    assert result.files_transferred == 10
    assert result.bytes_transferred == 1024
    assert result.source_path == Path("/source")
    assert result.destination_path == Path("/dest")
    assert result.is_dry_run is False


def test_rsync_result_supports_dry_run() -> None:
    result = RsyncResult(
        files_transferred=5,
        bytes_transferred=0,
        source_path=Path("/source"),
        destination_path=Path("/dest"),
        is_dry_run=True,
    )

    assert result.is_dry_run is True


# =============================================================================
# rsync command builders
# =============================================================================


def test_build_rsync_command_includes_stats_and_excludes_git() -> None:
    cmd = _build_rsync_command(Path("/src"), Path("/dst"), is_dry_run=False, is_delete=False)
    assert "--stats" in cmd
    assert "--exclude=.git" in cmd
    assert cmd[-2] == "/src/"
    assert cmd[-1] == "/dst"


def test_build_rsync_command_adds_dry_run_flag() -> None:
    cmd = _build_rsync_command(Path("/src"), Path("/dst"), is_dry_run=True, is_delete=False)
    assert "--dry-run" in cmd


def test_build_rsync_command_adds_delete_flag() -> None:
    cmd = _build_rsync_command(Path("/src"), Path("/dst"), is_dry_run=False, is_delete=True)
    assert "--delete" in cmd


def test_build_remote_rsync_command_push_uses_remote_destination() -> None:
    ssh_info = ("root", "example.com", 22, Path("/tmp/key"))
    cmd = _build_remote_rsync_command(
        local_path=Path("/local/src"),
        remote_path=Path("/remote/dst"),
        ssh_info=ssh_info,
        known_hosts_file=None,
        is_push=True,
        is_dry_run=False,
        is_delete=False,
    )
    # Only the source gets a trailing slash; rsync ignores trailing slashes on
    # the destination.
    assert cmd[-2] == "/local/src/"
    assert cmd[-1] == "root@example.com:/remote/dst"
    assert "-e" in cmd


def test_build_remote_rsync_command_pull_uses_remote_source() -> None:
    ssh_info = ("user", "host.com", 2222, Path("/key"))
    cmd = _build_remote_rsync_command(
        local_path=Path("/local/dst"),
        remote_path=Path("/remote/src"),
        ssh_info=ssh_info,
        known_hosts_file=None,
        is_push=False,
        is_dry_run=False,
        is_delete=False,
    )
    assert cmd[-2] == "user@host.com:/remote/src/"
    assert cmd[-1] == "/local/dst"


# =============================================================================
# rsync endpoint validation
# =============================================================================


def test_rsync_rejects_remote_to_remote_transfers(
    tmp_path: Path,
    cg: ConcurrencyGroup,
) -> None:
    source_host = cast(OnlineHostInterface, FakeHost(is_local=False))
    destination_host = cast(OnlineHostInterface, FakeHost(is_local=False))
    with pytest.raises(RsyncEndpointError):
        rsync(
            source_host=source_host,
            source_path=tmp_path / "src",
            destination_host=destination_host,
            destination_path=tmp_path / "dst",
            is_dry_run=False,
            is_delete=False,
            uncommitted_changes=UncommittedChangesMode.FAIL,
            cg=cg,
        )

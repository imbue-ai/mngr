"""Unit tests for ``api/rsync.py``."""

from pathlib import Path
from typing import cast

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.api.rsync import RsyncEndpointError
from imbue.mngr.api.rsync import _build_rsync_command
from imbue.mngr.api.rsync import rsync
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import UncommittedChangesMode

# =============================================================================
# rsync command builder
# =============================================================================


def test_build_rsync_command_includes_defaults_and_passes_paths_verbatim() -> None:
    cmd = _build_rsync_command("/src", "/dst", extra_args=(), ssh_transport=None)
    assert cmd[0] == "rsync"
    assert "-avz" in cmd
    assert "--stats" in cmd
    assert "--exclude=.git" in cmd
    # Paths are passed through with no mangling.
    assert cmd[-2] == "/src"
    assert cmd[-1] == "/dst"


def test_build_rsync_command_preserves_trailing_slash_on_source() -> None:
    cmd = _build_rsync_command("/src/", "/dst", extra_args=(), ssh_transport=None)
    assert cmd[-2] == "/src/"
    assert cmd[-1] == "/dst"


def test_build_rsync_command_inserts_extra_args_between_defaults_and_paths() -> None:
    cmd = _build_rsync_command("/src", "/dst", extra_args=("--dry-run", "--delete"), ssh_transport=None)
    assert "--dry-run" in cmd
    assert "--delete" in cmd
    # User args precede the source/destination so they're parsed as options.
    assert cmd.index("--dry-run") < cmd.index("/src")
    assert cmd.index("--delete") < cmd.index("/src")


def test_build_rsync_command_adds_ssh_transport_when_provided() -> None:
    cmd = _build_rsync_command("/src", "user@host:/dst", extra_args=(), ssh_transport="ssh -i /key")
    assert "-e" in cmd
    e_index = cmd.index("-e")
    assert cmd[e_index + 1] == "ssh -i /key"
    assert cmd[-1] == "user@host:/dst"


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
            source_path=str(tmp_path / "src"),
            destination_host=destination_host,
            destination_path=str(tmp_path / "dst"),
            extra_args=(),
            uncommitted_changes=UncommittedChangesMode.FAIL,
            cg=cg,
        )

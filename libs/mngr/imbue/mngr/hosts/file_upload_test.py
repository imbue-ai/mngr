"""Unit tests for the bulk file-upload helper.

The remote (rsync) branch is covered end-to-end by the Modal acceptance test
``test_upload_deploy_files_handles_large_set_on_modal`` (and rsync's no-delete
behavior by ``test_rsync_does_not_delete_existing_files_by_default`` in test_host.py).
These unit tests cover the branch-agnostic logic and the local branch.
"""

from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.api.testing import FakeHost
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.file_upload import resolve_remote_path
from imbue.mngr.hosts.file_upload import upload_files_in_bulk


def _local_host() -> Any:
    return FakeHost(host_dir=Path("/tmp/mngr-test/host"), is_local=True)


# --- resolve_remote_path ---


def test_resolve_remote_path_bare_tilde() -> None:
    assert resolve_remote_path(Path("~"), "/home/u") == Path("/home/u")


def test_resolve_remote_path_tilde_prefix() -> None:
    assert resolve_remote_path(Path("~/.mngr/config.toml"), "/home/u") == Path("/home/u/.mngr/config.toml")


def test_resolve_remote_path_relative_resolves_under_home() -> None:
    assert resolve_remote_path(Path(".claude/settings.json"), "/home/u") == Path("/home/u/.claude/settings.json")


def test_resolve_remote_path_absolute_unchanged() -> None:
    assert resolve_remote_path(Path("/etc/thing"), "/home/u") == Path("/etc/thing")


# --- upload_files_in_bulk: local branch ---


def test_local_upload_writes_each_source_type(tmp_path: Path) -> None:
    dest_dir = tmp_path / "dest"
    src = tmp_path / "src.txt"
    src.write_text("from-path")
    files: dict[Path, bytes | str | Path] = {
        dest_dir / "a.txt": src,
        dest_dir / "b.txt": "from-str",
        dest_dir / "sub" / "c.bin": b"from-bytes",
    }

    count = upload_files_in_bulk(_local_host(), files, "", skip_missing=False)

    assert count == 3
    assert (dest_dir / "a.txt").read_text() == "from-path"
    assert (dest_dir / "b.txt").read_text() == "from-str"
    assert (dest_dir / "sub" / "c.bin").read_bytes() == b"from-bytes"


def test_local_upload_source_equals_dest_is_noop(tmp_path: Path) -> None:
    """A local Path source that already IS the destination must not error (no-op).

    On a local host an agent file transfer can target a file already present in the
    work_dir, so source == dest. The helper must treat this as a no-op (shutil.copyfile
    would otherwise raise SameFileError).
    """
    dest = tmp_path / "work" / ".claude" / "settings.local.json"
    dest.parent.mkdir(parents=True)
    dest.write_text("keep")
    files: dict[Path, bytes | str | Path] = {dest: dest}

    count = upload_files_in_bulk(_local_host(), files, "", skip_missing=False)

    assert count == 1
    assert dest.read_text() == "keep"


def test_local_upload_does_not_delete_existing_files(tmp_path: Path) -> None:
    """Uploading into a dir with unrelated pre-existing files must not remove them.

    (The remote/rsync branch's no-delete behavior is covered by
    test_rsync_does_not_delete_existing_files_by_default in test_host.py.)
    """
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "preexisting.txt").write_text("keep me")

    files: dict[Path, bytes | str | Path] = {dest_dir / "new.txt": "new"}
    upload_files_in_bulk(_local_host(), files, "", skip_missing=False)

    assert (dest_dir / "new.txt").read_text() == "new"
    assert (dest_dir / "preexisting.txt").read_text() == "keep me"


# --- missing sources: skip vs error ---


def test_missing_source_raises_when_skip_missing_false(tmp_path: Path) -> None:
    files: dict[Path, bytes | str | Path] = {tmp_path / "dest" / "a.txt": tmp_path / "missing.txt"}

    with pytest.raises(MngrError, match="do not exist locally"):
        upload_files_in_bulk(_local_host(), files, "", skip_missing=False)


def test_skip_missing_true_skips_missing_sources(tmp_path: Path) -> None:
    dest_dir = tmp_path / "dest"
    present = tmp_path / "present.txt"
    present.write_text("here")
    files: dict[Path, bytes | str | Path] = {
        dest_dir / "a.txt": present,
        dest_dir / "b.txt": tmp_path / "missing.txt",
    }

    count = upload_files_in_bulk(_local_host(), files, "", skip_missing=True)

    assert count == 1
    assert (dest_dir / "a.txt").read_text() == "here"
    assert not (dest_dir / "b.txt").exists()

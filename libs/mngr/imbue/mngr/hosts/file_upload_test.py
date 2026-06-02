"""Unit tests for the bulk file-upload helper."""

from pathlib import Path
from typing import Any

from pydantic import Field

from imbue.mngr.api.testing import FakeHost
from imbue.mngr.hosts.file_upload import resolve_remote_path
from imbue.mngr.hosts.file_upload import upload_files_in_bulk


class _RecordingRemoteHost(FakeHost):
    """A non-local FakeHost that records copy_directory calls and snapshots the staged tree.

    Used to exercise upload_files_in_bulk's remote (rsync) branch without actually
    writing to the filesystem root ("/"). The real rsync transfer is covered by the
    copy_directory tests in test_host.py and the Modal acceptance test.
    """

    copy_directory_count: int = Field(default=0)
    staged_tree: dict[str, str] = Field(default_factory=dict)
    last_target: Path | None = Field(default=None)

    def copy_directory(
        self,
        source_host: object,
        source_path: Path,
        target_path: Path,
        extra_args: str | None = None,
        exclude_git: bool = False,
    ) -> None:
        self.copy_directory_count += 1
        self.last_target = target_path
        for staged in Path(source_path).rglob("*"):
            if staged.is_file():
                self.staged_tree[staged.relative_to(source_path).as_posix()] = staged.read_text()


def _remote_host() -> Any:
    return _RecordingRemoteHost(host_dir=Path("/tmp/mngr-test/host"), is_local=False)


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


# --- upload_files_in_bulk: remote (rsync) branch ---


def test_remote_upload_uses_single_copy_directory(tmp_path: Path) -> None:
    source_file = tmp_path / "a.txt"
    source_file.write_text("aaa")
    host = _remote_host()
    files: dict[Path, bytes | str | Path] = {
        Path("~/.claude/a.txt"): source_file,
        Path("~/.mngr/b.toml"): "bbb",
        Path("/etc/c.conf"): b"ccc",
    }

    count = upload_files_in_bulk(host, _local_host(), files, "/home/u")

    assert count == 3
    assert host.copy_directory_count == 1
    assert host.last_target == Path("/")
    # Staged tree mirrors absolute remote paths (leading slash stripped).
    assert host.staged_tree == {
        "home/u/.claude/a.txt": "aaa",
        "home/u/.mngr/b.toml": "bbb",
        "etc/c.conf": "ccc",
    }


def test_remote_upload_skips_missing_path_sources(tmp_path: Path) -> None:
    host = _remote_host()
    files: dict[Path, bytes | str | Path] = {Path("~/.mngr/x"): tmp_path / "nope.txt"}

    count = upload_files_in_bulk(host, _local_host(), files, "/home/u")

    assert count == 0
    # Nothing to transfer -> no rsync.
    assert host.copy_directory_count == 0


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

    count = upload_files_in_bulk(_local_host(), _local_host(), files, "")

    assert count == 3
    assert (dest_dir / "a.txt").read_text() == "from-path"
    assert (dest_dir / "b.txt").read_text() == "from-str"
    assert (dest_dir / "sub" / "c.bin").read_bytes() == b"from-bytes"


def test_local_upload_source_equals_dest_is_noop(tmp_path: Path) -> None:
    """A local Path source that already IS the destination must not error (no-op).

    On a local host an agent file transfer can target a file already present in the
    work_dir, so source == dest. The old write_file(path, path.read_bytes()) handled
    this; the helper must too (shutil.copyfile would otherwise raise SameFileError).
    """
    dest = tmp_path / "work" / ".claude" / "settings.local.json"
    dest.parent.mkdir(parents=True)
    dest.write_text("keep")
    files: dict[Path, bytes | str | Path] = {dest: dest}

    count = upload_files_in_bulk(_local_host(), _local_host(), files, "")

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
    upload_files_in_bulk(_local_host(), _local_host(), files, "")

    assert (dest_dir / "new.txt").read_text() == "new"
    assert (dest_dir / "preexisting.txt").read_text() == "keep me"

"""Unit tests for ``BaseObjectStoreVolume``'s cloud-agnostic shared logic.

A tiny in-memory subclass exercises the listing / existence / read / write /
delete behavior that lives on the base, without any cloud SDK. The two concrete
clouds (``S3Volume`` / ``BlobVolume``) are covered separately by their own
provider tests; this pins down the shared logic once.
"""

from collections.abc import Iterator
from contextlib import AbstractContextManager
from contextlib import contextmanager

import pytest
from pydantic import Field

from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr_vps.state_bucket_base import BaseObjectStoreVolume
from imbue.mngr_vps.state_bucket_base import ObjectStoreEntry


class _FakeObjectStoreError(MngrError):
    """Error type used to mark a not-found in the in-memory fake."""


class _MissingObjectError(_FakeObjectStoreError):
    """Raised by the fake's read/delete when the key is absent (the not-found marker)."""


@contextmanager
def _passthrough_translate() -> Iterator[None]:
    yield


class _InMemoryObjectStoreVolume(BaseObjectStoreVolume):
    """A ``BaseObjectStoreVolume`` over a flat in-memory dict, for exercising the shared logic."""

    object_bytes_by_key: dict[str, bytes] = Field(
        default_factory=dict, description="Flat in-memory key -> bytes store"
    )

    def _translate_errors(self) -> AbstractContextManager[None]:
        return _passthrough_translate()

    def _is_not_found(self, error: MngrError) -> bool:
        return isinstance(error, _MissingObjectError)

    @property
    def _bucket_error_type(self) -> type[MngrError]:
        return _FakeObjectStoreError

    def _make_missing_file_error(self, path: str) -> MngrError:
        return _FakeObjectStoreError(f"File {path!r} does not exist")

    def _iter_delimited_entries(self, prefix: str) -> Iterator[ObjectStoreEntry]:
        seen_dirs: set[str] = set()
        for key in sorted(self.object_bytes_by_key):
            if not key.startswith(prefix):
                continue
            remainder = key[len(prefix) :]
            head, sep, _tail = remainder.partition("/")
            if sep:
                dir_name = f"{prefix}{head}"
                if dir_name not in seen_dirs:
                    seen_dirs.add(dir_name)
                    yield ObjectStoreEntry(name=dir_name, is_directory=True, mtime=0, size=0)
            else:
                yield ObjectStoreEntry(name=key, is_directory=False, mtime=7, size=len(self.object_bytes_by_key[key]))

    def _prefix_has_any_object(self, prefix: str) -> bool:
        return any(key.startswith(prefix) for key in self.object_bytes_by_key)

    def _has_object_at_key(self, key: str) -> bool:
        return key in self.object_bytes_by_key

    def _read_object_bytes(self, key: str) -> bytes:
        if key not in self.object_bytes_by_key:
            raise _MissingObjectError(f"no object at {key!r}")
        return self.object_bytes_by_key[key]

    def _delete_single_object(self, key: str) -> None:
        if key not in self.object_bytes_by_key:
            raise _MissingObjectError(f"no object at {key!r}")
        del self.object_bytes_by_key[key]

    def _delete_prefix(self, prefix: str) -> None:
        for key in [k for k in self.object_bytes_by_key if k.startswith(prefix)]:
            del self.object_bytes_by_key[key]

    def _write_object(self, key: str, content: bytes) -> None:
        self.object_bytes_by_key[key] = content


def _volume(object_bytes_by_key: dict[str, bytes]) -> _InMemoryObjectStoreVolume:
    return _InMemoryObjectStoreVolume(object_bytes_by_key=dict(object_bytes_by_key))


def test_listdir_synthesizes_dirs_and_strips_prefix() -> None:
    volume = _volume(
        {
            "host_dir/top.txt": b"hello",
            "host_dir/logs/a.log": b"a",
            "host_dir/logs/b.log": b"bb",
        }
    ).scoped("host_dir")
    entries = {e.path: e for e in volume.listdir("")}
    assert set(entries) == {"top.txt", "logs"}
    assert entries["top.txt"].file_type == FileType.FILE
    assert entries["top.txt"].size == len(b"hello")
    assert entries["top.txt"].mtime == 7
    assert entries["logs"].file_type == FileType.DIRECTORY
    sub = {e.path: e for e in volume.listdir("logs")}
    assert set(sub) == {"a.log", "b.log"}


def test_path_exists_for_file_and_directory() -> None:
    volume = _volume({"host_dir/logs/out.txt": b"x"}).scoped("host_dir")
    assert volume.path_exists("logs/out.txt") is True
    assert volume.path_exists("logs") is True
    assert volume.path_exists("missing") is False


def test_path_exists_with_prefix_colliding_sibling() -> None:
    # ``foobar`` shares the bare prefix of dir ``foo`` and sorts earlier; the
    # separate dir probe (on ``foo/``) keeps the directory from being masked.
    volume = _volume({"host_dir/foobar": b"f", "host_dir/foo/bar": b"b"}).scoped("host_dir")
    assert volume.path_exists("foo") is True
    assert volume.path_exists("foobar") is True
    assert volume.path_exists("nope") is False


def test_read_file_missing_raises_via_not_found_seam() -> None:
    volume = _volume({}).scoped("host_dir")
    with pytest.raises(_FakeObjectStoreError):
        volume.read_file("nope.txt")


def test_remove_file_is_idempotent_on_missing() -> None:
    volume = _volume({}).scoped("host_dir")
    # The not-found seam makes a delete of an absent object a no-op (no raise).
    volume.remove_file("gone.txt")


def test_write_read_and_recursive_remove_round_trip() -> None:
    volume = _volume({}).scoped("host_dir")
    volume.write_files({"dir/f.txt": b"data", "dir/g.txt": b"more"})
    assert volume.read_file("dir/f.txt") == b"data"
    assert volume.path_exists("dir") is True
    # Recursive remove maps to ``_delete_prefix`` and clears the whole subtree.
    volume.remove_file("dir", recursive=True)
    assert volume.path_exists("dir") is False
    assert volume.path_exists("dir/f.txt") is False

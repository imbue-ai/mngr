"""Shared host file readers for tests of host file reads and listings (imported explicitly, defines no tests)."""

import threading
from datetime import datetime
from pathlib import Path

from pydantic import Field
from pydantic import PrivateAttr

from imbue.mngr.interfaces.data_types import FileTailRead
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.host import HostFileReadInterface


class InMemoryHostFileReader(HostFileReadInterface):
    """Serves files from memory and counts the content bytes each tail read returns."""

    contents_by_path: dict[Path, bytes] = Field(default_factory=dict, description="File contents by path")
    content_bytes_read: int = Field(default=0, description="Total content bytes returned by tail reads")
    tail_read_count: int = Field(default=0, description="Number of tail reads served")

    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def append(self, path: Path, content: bytes) -> None:
        self.contents_by_path[path] = self.contents_by_path.get(path, b"") + content

    def read_file(self, path: Path) -> bytes:
        if path not in self.contents_by_path:
            raise FileNotFoundError(f"File not found: {path}")
        return self.contents_by_path[path]

    def read_file_tail_from_offset(
        self, path: Path, start_byte: int, max_byte_count: int | None = None
    ) -> FileTailRead:
        content = self.read_file(path)
        tail = content[start_byte : None if max_byte_count is None else start_byte + max_byte_count]
        with self._lock:
            self.content_bytes_read += len(tail)
            self.tail_read_count += 1
        return FileTailRead(file_size=len(content), content=tail)

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        return self.read_file(path).decode(encoding)

    def path_exists(self, path: Path) -> bool:
        return path in self.contents_by_path

    def get_file_mtime(self, path: Path) -> datetime | None:
        return None

    def list_directory(self, path: Path, *, recursive: bool = False) -> list[VolumeFile]:
        return []


class _DelegatingHostFileReader(HostFileReadInterface):
    """Passes every call on to another reader; subclasses override the calls they change."""

    inner: HostFileReadInterface = Field(description="The reader every call is passed on to")

    def read_file(self, path: Path) -> bytes:
        return self.inner.read_file(path)

    def read_file_tail_from_offset(
        self, path: Path, start_byte: int, max_byte_count: int | None = None
    ) -> FileTailRead:
        return self.inner.read_file_tail_from_offset(path, start_byte, max_byte_count)

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        return self.inner.read_text_file(path, encoding)

    def path_exists(self, path: Path) -> bool:
        return self.inner.path_exists(path)

    def get_file_mtime(self, path: Path) -> datetime | None:
        return self.inner.get_file_mtime(path)

    def list_directory(self, path: Path, *, recursive: bool = False) -> list[VolumeFile]:
        return self.inner.list_directory(path, recursive=recursive)


class ListingRecordingHostFileReader(_DelegatingHostFileReader):
    """Reads through another reader while recording every directory listing asked of it."""

    listing_requests: list[tuple[Path, bool]] = Field(
        default_factory=list, description="The (path, recursive) of each list_directory call, in order"
    )

    def list_directory(self, path: Path, *, recursive: bool = False) -> list[VolumeFile]:
        self.listing_requests.append((path, recursive))
        return self.inner.list_directory(path, recursive=recursive)


class FileListingAsItselfHostFileReader(_DelegatingHostFileReader):
    """Reads through a reader of the local filesystem, but lists a file path as that one file, as a Modal volume does."""

    def list_directory(self, path: Path, *, recursive: bool = False) -> list[VolumeFile]:
        if path.is_file():
            stat = path.stat()
            return [VolumeFile(path=str(path), file_type=FileType.FILE, mtime=int(stat.st_mtime), size=stat.st_size)]
        return self.inner.list_directory(path, recursive=recursive)

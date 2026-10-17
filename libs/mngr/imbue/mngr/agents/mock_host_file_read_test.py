"""Shared in-memory file reader for tests of host file reads (imported explicitly, defines no tests)."""

import threading
from datetime import datetime
from pathlib import Path

from pydantic import Field
from pydantic import PrivateAttr

from imbue.mngr.interfaces.data_types import FileTailRead
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

    def read_file_tail_from_offset(self, path: Path, start_byte: int) -> FileTailRead:
        content = self.read_file(path)
        tail = content[start_byte:]
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

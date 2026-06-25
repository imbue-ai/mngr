from typing import Mapping

from pydantic import Field

from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.volume import BaseVolume


class InMemoryVolume(BaseVolume):
    """In-memory volume implementation for testing.

    Backs the ``Volume`` interface with a plain ``{path: bytes}`` dict so tests
    can exercise ``ScopedVolume`` / ``HostVolume`` behavior without touching a
    real filesystem or remote host.
    """

    files: dict[str, bytes] = Field(default_factory=dict)

    def listdir(self, path: str) -> list[VolumeFile]:
        path = path.rstrip("/")
        results: list[VolumeFile] = []
        for file_path in sorted(self.files):
            parent = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
            if parent == path or (not path and "/" not in file_path):
                results.append(
                    VolumeFile(path=file_path, file_type=FileType.FILE, mtime=0, size=len(self.files[file_path]))
                )
        return results

    def path_exists(self, path: str) -> bool:
        if path in self.files:
            return True
        prefix = path.rstrip("/") + "/"
        return any(k.startswith(prefix) for k in self.files)

    def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        if not recursive:
            if path not in self.files:
                raise FileNotFoundError(path)
            del self.files[path]
            return
        prefix = path.rstrip("/") + "/"
        to_delete = [k for k in self.files if k == path or k.startswith(prefix)]
        if not to_delete:
            raise FileNotFoundError(path)
        for k in to_delete:
            del self.files[k]

    def remove_directory(self, path: str) -> None:
        prefix = path.rstrip("/") + "/"
        to_delete = [k for k in self.files if k.startswith(prefix) or k == path.rstrip("/")]
        for k in to_delete:
            del self.files[k]

    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        self.files.update(file_contents_by_path)

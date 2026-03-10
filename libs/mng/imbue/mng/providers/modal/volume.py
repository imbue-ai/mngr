import io
from typing import Mapping

import modal
import modal.exception
from grpclib.exceptions import ProtocolError
from grpclib.exceptions import StreamTerminatedError
from modal.volume import FileEntry
from modal.volume import FileEntryType
from pydantic import Field
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.mng.interfaces.data_types import VolumeFile
from imbue.mng.interfaces.data_types import VolumeFileType
from imbue.mng.interfaces.volume import BaseVolume

# Retry parameters for Modal volume operations.
# modal.exception.InternalError (e.g. "could not start volume metadata engine")
# is transient and typically resolves on retry.
# grpclib StreamTerminatedError/ProtocolError are transient gRPC protocol errors
# that occur when the HTTP/2 stream is terminated unexpectedly.
_VOLUME_RETRY_PARAMS = retry_if_exception_type((modal.exception.InternalError, StreamTerminatedError, ProtocolError))
_VOLUME_STOP_PARAMS = stop_after_attempt(3)
_VOLUME_WAIT_PARAMS = wait_exponential(multiplier=1, min=1, max=3)


@retry(retry=_VOLUME_RETRY_PARAMS, stop=_VOLUME_STOP_PARAMS, wait=_VOLUME_WAIT_PARAMS, reraise=True)
def _modal_volume_listdir(volume: modal.Volume, path: str) -> list[FileEntry]:
    """List directory contents on a Modal volume with retry on transient errors."""
    return volume.listdir(path)


@retry(retry=_VOLUME_RETRY_PARAMS, stop=_VOLUME_STOP_PARAMS, wait=_VOLUME_WAIT_PARAMS, reraise=True)
def _modal_volume_read_file(volume: modal.Volume, path: str) -> bytes:
    """Read a file from a Modal volume with retry on transient errors."""
    return b"".join(volume.read_file(path))


@retry(retry=_VOLUME_RETRY_PARAMS, stop=_VOLUME_STOP_PARAMS, wait=_VOLUME_WAIT_PARAMS, reraise=True)
def _modal_volume_remove_file(volume: modal.Volume, path: str, *, recursive: bool = False) -> None:
    """Remove a file or directory from a Modal volume with retry on transient errors."""
    volume.remove_file(path, recursive=recursive)


@retry(retry=_VOLUME_RETRY_PARAMS, stop=_VOLUME_STOP_PARAMS, wait=_VOLUME_WAIT_PARAMS, reraise=True)
def _modal_volume_write_files(volume: modal.Volume, file_contents_by_path: Mapping[str, bytes]) -> None:
    """Upload files to a Modal volume with retry on transient errors."""
    with volume.batch_upload(force=True) as batch:
        for path, file_data in file_contents_by_path.items():
            batch.put_file(io.BytesIO(file_data), path)


def _modal_file_type_to_volume_file_type(modal_type: FileEntryType) -> VolumeFileType:
    """Convert a Modal FileEntryType to our VolumeFileType."""
    match modal_type:
        case FileEntryType.DIRECTORY:
            return VolumeFileType.DIRECTORY
        case _:
            return VolumeFileType.FILE


def _file_entry_to_volume_file(entry: FileEntry) -> VolumeFile:
    """Convert a Modal FileEntry to a mng VolumeFile."""
    return VolumeFile(
        path=entry.path,
        file_type=_modal_file_type_to_volume_file_type(entry.type),
        mtime=entry.mtime,
        size=entry.size,
    )


class ModalVolume(BaseVolume):
    """Volume implementation backed by a Modal Volume.

    Wraps a modal.Volume and implements the mng Volume interface.
    All operations include retry logic for transient Modal errors.
    """

    modal_volume: modal.Volume = Field(frozen=True, description="The underlying Modal volume")

    def listdir(self, path: str) -> list[VolumeFile]:
        entries = _modal_volume_listdir(self.modal_volume, path)
        return [_file_entry_to_volume_file(e) for e in entries]

    def read_file(self, path: str) -> bytes:
        return _modal_volume_read_file(self.modal_volume, path)

    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        _modal_volume_remove_file(self.modal_volume, path, recursive=recursive)

    def remove_directory(self, path: str) -> None:
        _modal_volume_remove_file(self.modal_volume, path, recursive=True)

    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        _modal_volume_write_files(self.modal_volume, file_contents_by_path)

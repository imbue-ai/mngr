from collections.abc import Callable
from functools import wraps
from typing import Mapping
from typing import ParamSpec
from typing import TypeVar

from pydantic import Field

from imbue.mngr.interfaces.data_types import VolumeFile
from imbue.mngr.interfaces.data_types import VolumeFileType
from imbue.mngr.interfaces.volume import BaseVolume
from imbue.mngr_modal.errors import ModalMngrError
from imbue.modal_proxy.data_types import FileEntry as ProxyFileEntry
from imbue.modal_proxy.data_types import FileEntryType as ProxyFileEntryType
from imbue.modal_proxy.errors import ModalProxyInternalError
from imbue.modal_proxy.errors import ModalProxyRateLimitError
from imbue.modal_proxy.interface import VolumeInterface

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _translate_transient_proxy_errors(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """Translate transient ModalProxy errors to ModalMngrError at the mngr_modal boundary.

    Rate-limit and internal errors that survive retry are translated so that
    the mngr layer's ``except (MngrError, OSError)`` guards can catch them
    instead of letting them crash the process.  Semantic errors like
    ModalProxyNotFoundError are left untouched.
    """

    @wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return func(*args, **kwargs)
        except (ModalProxyRateLimitError, ModalProxyInternalError) as e:
            raise ModalMngrError(str(e)) from e

    return wrapper


def _proxy_file_entry_type_to_volume_file_type(proxy_type: ProxyFileEntryType) -> VolumeFileType:
    """Convert a modal_proxy FileEntryType to our VolumeFileType."""
    match proxy_type:
        case ProxyFileEntryType.DIRECTORY:
            return VolumeFileType.DIRECTORY
        case _:
            return VolumeFileType.FILE


def _proxy_file_entry_to_volume_file(entry: ProxyFileEntry) -> VolumeFile:
    """Convert a modal_proxy FileEntry to a mngr VolumeFile."""
    return VolumeFile(
        path=entry.path,
        file_type=_proxy_file_entry_type_to_volume_file_type(entry.type),
        mtime=int(entry.mtime),
        size=entry.size,
    )


class ModalVolume(BaseVolume):
    """Volume implementation backed by a VolumeInterface.

    Wraps a VolumeInterface (from modal_proxy) and implements the mngr Volume
    interface. Retry logic for transient errors is handled by the underlying
    VolumeInterface implementation (e.g. DirectVolume).
    """

    modal_volume: VolumeInterface = Field(frozen=True, description="The underlying volume interface")

    @_translate_transient_proxy_errors
    def listdir(self, path: str) -> list[VolumeFile]:
        entries = self.modal_volume.listdir(path)
        return [_proxy_file_entry_to_volume_file(e) for e in entries]

    @_translate_transient_proxy_errors
    def read_file(self, path: str) -> bytes:
        return self.modal_volume.read_file(path)

    @_translate_transient_proxy_errors
    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        self.modal_volume.remove_file(path, recursive=recursive)

    @_translate_transient_proxy_errors
    def remove_directory(self, path: str) -> None:
        self.modal_volume.remove_file(path, recursive=True)

    @_translate_transient_proxy_errors
    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        self.modal_volume.write_files(file_contents_by_path)

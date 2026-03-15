from contextlib import AbstractContextManager
from pathlib import Path
from typing import Mapping
from typing import Sequence

import pytest
from modal.stream_type import StreamType as ModalStreamType
from modal.volume import FileEntryType as ModalFileEntryType

from imbue.modal_proxy.data_types import FileEntry
from imbue.modal_proxy.data_types import FileEntryType
from imbue.modal_proxy.data_types import StreamType
from imbue.modal_proxy.direct import DirectApp
from imbue.modal_proxy.direct import DirectImage
from imbue.modal_proxy.direct import DirectSecret
from imbue.modal_proxy.direct import DirectVolume
from imbue.modal_proxy.direct import _to_file_entry_type
from imbue.modal_proxy.direct import _to_modal_stream_type
from imbue.modal_proxy.direct import _unwrap_app
from imbue.modal_proxy.direct import _unwrap_image
from imbue.modal_proxy.direct import _unwrap_secret
from imbue.modal_proxy.direct import _unwrap_volume
from imbue.modal_proxy.errors import ModalProxyTypeError
from imbue.modal_proxy.interface import AppInterface
from imbue.modal_proxy.interface import ImageInterface
from imbue.modal_proxy.interface import SecretInterface
from imbue.modal_proxy.interface import VolumeInterface


class _FakeAppInterface(AppInterface):
    """Non-Direct AppInterface for testing unwrap rejection."""

    def get_app_id(self) -> str:
        return "fake"

    def get_name(self) -> str:
        return "fake"

    def run(self, *, environment_name: str) -> AbstractContextManager["AppInterface"]:
        raise NotImplementedError


class _FakeImageInterface(ImageInterface):
    """Non-Direct ImageInterface for testing unwrap rejection."""

    def get_object_id(self) -> str:
        return "fake"

    def apt_install(self, *packages: str) -> "ImageInterface":
        raise NotImplementedError

    def dockerfile_commands(
        self,
        commands: Sequence[str],
        *,
        context_dir: Path | None = None,
        secrets: Sequence[SecretInterface] = (),
    ) -> "ImageInterface":
        raise NotImplementedError


class _FakeVolumeInterface(VolumeInterface):
    """Non-Direct VolumeInterface for testing unwrap rejection."""

    def get_name(self) -> str | None:
        return None

    def listdir(self, path: str) -> list[FileEntry]:
        raise NotImplementedError

    def read_file(self, path: str) -> bytes:
        raise NotImplementedError

    def remove_file(self, path: str, *, recursive: bool = False) -> None:
        raise NotImplementedError

    def write_files(self, file_contents_by_path: Mapping[str, bytes]) -> None:
        raise NotImplementedError

    def reload(self) -> None:
        raise NotImplementedError

    def commit(self) -> None:
        raise NotImplementedError


class _FakeSecretInterface(SecretInterface):
    """Non-Direct SecretInterface for testing unwrap rejection."""


# --- StreamType conversion ---


def test_to_modal_stream_type_pipe() -> None:
    assert _to_modal_stream_type(StreamType.PIPE) == ModalStreamType.PIPE


def test_to_modal_stream_type_devnull() -> None:
    assert _to_modal_stream_type(StreamType.DEVNULL) == ModalStreamType.DEVNULL


# --- FileEntryType conversion ---


def test_to_file_entry_type_file() -> None:
    assert _to_file_entry_type(ModalFileEntryType.FILE) == FileEntryType.FILE


def test_to_file_entry_type_directory() -> None:
    assert _to_file_entry_type(ModalFileEntryType.DIRECTORY) == FileEntryType.DIRECTORY


# --- Unwrap helpers reject non-Direct types ---


def test_unwrap_image_rejects_non_direct() -> None:
    with pytest.raises(ModalProxyTypeError, match="Expected DirectImage"):
        _unwrap_image(_FakeImageInterface.model_construct())


def test_unwrap_app_rejects_non_direct() -> None:
    with pytest.raises(ModalProxyTypeError, match="Expected DirectApp"):
        _unwrap_app(_FakeAppInterface.model_construct())


def test_unwrap_volume_rejects_non_direct() -> None:
    with pytest.raises(ModalProxyTypeError, match="Expected DirectVolume"):
        _unwrap_volume(_FakeVolumeInterface.model_construct())


def test_unwrap_secret_rejects_non_direct() -> None:
    with pytest.raises(ModalProxyTypeError, match="Expected DirectSecret"):
        _unwrap_secret(_FakeSecretInterface.model_construct())


# --- Unwrap helpers accept Direct types ---


def test_unwrap_image_accepts_direct() -> None:
    sentinel = object()
    direct = DirectImage.model_construct(image=sentinel)
    assert _unwrap_image(direct) is sentinel


def test_unwrap_app_accepts_direct() -> None:
    sentinel = object()
    direct = DirectApp.model_construct(app=sentinel)
    assert _unwrap_app(direct) is sentinel


def test_unwrap_volume_accepts_direct() -> None:
    sentinel = object()
    direct = DirectVolume.model_construct(volume=sentinel)
    assert _unwrap_volume(direct) is sentinel


def test_unwrap_secret_accepts_direct() -> None:
    sentinel = object()
    direct = DirectSecret.model_construct(secret=sentinel)
    assert _unwrap_secret(direct) is sentinel

from collections.abc import Generator
from pathlib import Path
from typing import Any
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

# --- Fake implementations for testing unwrap rejection ---


class _FakeApp(AppInterface):
    """Non-Direct AppInterface for testing unwrap rejection."""

    def get_app_id(self) -> str:
        return "fake"

    def get_name(self) -> str:
        return "fake"

    def run(self, *, environment_name: str) -> Generator["AppInterface", None, None]:
        raise NotImplementedError


class _FakeImage(ImageInterface):
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


class _FakeVolume(VolumeInterface):
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


class _FakeSecret(SecretInterface):
    """Non-Direct SecretInterface for testing unwrap rejection."""


# --- Conversion tests ---


@pytest.mark.parametrize(
    ("ours", "modals"),
    [
        (StreamType.PIPE, ModalStreamType.PIPE),
        (StreamType.DEVNULL, ModalStreamType.DEVNULL),
    ],
)
def test_to_modal_stream_type(ours: StreamType, modals: ModalStreamType) -> None:
    assert _to_modal_stream_type(ours) == modals


@pytest.mark.parametrize(
    ("modal_type", "expected"),
    [
        (ModalFileEntryType.FILE, FileEntryType.FILE),
        (ModalFileEntryType.DIRECTORY, FileEntryType.DIRECTORY),
    ],
)
def test_to_file_entry_type(modal_type: ModalFileEntryType, expected: FileEntryType) -> None:
    assert _to_file_entry_type(modal_type) == expected


# --- Unwrap helpers ---


_UNWRAP_CASES: list[tuple[Any, Any, type, str]] = [
    (_unwrap_image, _FakeImage, DirectImage, "image"),
    (_unwrap_app, _FakeApp, DirectApp, "app"),
    (_unwrap_volume, _FakeVolume, DirectVolume, "volume"),
    (_unwrap_secret, _FakeSecret, DirectSecret, "secret"),
]


@pytest.mark.parametrize(
    ("unwrap_fn", "fake_cls", "direct_cls", "field_name"),
    _UNWRAP_CASES,
    ids=["image", "app", "volume", "secret"],
)
def test_unwrap_rejects_non_direct(unwrap_fn: Any, fake_cls: Any, direct_cls: Any, field_name: str) -> None:
    with pytest.raises(ModalProxyTypeError):
        unwrap_fn(fake_cls.model_construct())


@pytest.mark.parametrize(
    ("unwrap_fn", "fake_cls", "direct_cls", "field_name"),
    _UNWRAP_CASES,
    ids=["image", "app", "volume", "secret"],
)
def test_unwrap_accepts_direct(unwrap_fn: Any, fake_cls: Any, direct_cls: Any, field_name: str) -> None:
    sentinel = object()
    direct = direct_cls.model_construct(**{field_name: sentinel})
    assert unwrap_fn(direct) is sentinel

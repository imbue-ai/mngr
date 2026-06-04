from collections.abc import Callable
from collections.abc import Generator
from pathlib import Path
from typing import Any
from typing import Mapping
from typing import Sequence
from uuid import uuid4

import modal
import modal.exception
import pytest
from grpclib.exceptions import ProtocolError
from grpclib.exceptions import StreamTerminatedError
from modal.stream_type import StreamType as ModalStreamType
from modal.volume import FileEntryType as ModalFileEntryType

from imbue.modal_proxy.data_types import FileEntry
from imbue.modal_proxy.data_types import FileEntryType
from imbue.modal_proxy.data_types import StreamType
from imbue.modal_proxy.direct import DirectApp
from imbue.modal_proxy.direct import DirectImage
from imbue.modal_proxy.direct import DirectSecret
from imbue.modal_proxy.direct import DirectVolume
from imbue.modal_proxy.direct import _should_retry_volume_op
from imbue.modal_proxy.direct import _to_file_entry_type
from imbue.modal_proxy.direct import _to_modal_stream_type
from imbue.modal_proxy.direct import _translate_modal_cli_not_found
from imbue.modal_proxy.direct import _translate_modal_error
from imbue.modal_proxy.direct import _unwrap_app
from imbue.modal_proxy.direct import _unwrap_image
from imbue.modal_proxy.direct import _unwrap_secret
from imbue.modal_proxy.direct import _unwrap_volume
from imbue.modal_proxy.errors import ModalProxyAuthError
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.errors import ModalProxyInternalError
from imbue.modal_proxy.errors import ModalProxyInvalidError
from imbue.modal_proxy.errors import ModalProxyNotFoundError
from imbue.modal_proxy.errors import ModalProxyPermissionDeniedError
from imbue.modal_proxy.errors import ModalProxyRateLimitError
from imbue.modal_proxy.errors import ModalProxyRemoteError
from imbue.modal_proxy.errors import ModalProxyTypeError
from imbue.modal_proxy.interface import AppInterface
from imbue.modal_proxy.interface import ImageInterface
from imbue.modal_proxy.interface import SecretInterface
from imbue.modal_proxy.interface import VolumeInterface

# --- Fake implementations for testing unwrap rejection ---
#
# These are deliberately local concrete stubs used only by
# `test_unwrap_rejects_non_direct` to supply a non-`Direct*` instance of each
# interface. They are intentionally minimal (every method raises) because the
# rejection happens before any method is called. If a second test file ever
# needs interface stand-ins, hoist these into a shared `mock_*_test.py` next to
# the interface definitions per the style guide.


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

    def build(self, app: "AppInterface") -> None:
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


def test_to_modal_stream_type_rejects_unsupported_value() -> None:
    # A value outside the StreamType enum hits the `case _` default arm, which
    # must raise rather than silently returning None / mapping to a real member.
    unsupported_value: Any = "not-a-stream-type"
    with pytest.raises(ModalProxyError, match="Unsupported StreamType"):
        _to_modal_stream_type(unsupported_value)


@pytest.mark.parametrize(
    ("modal_type", "expected"),
    [
        (ModalFileEntryType.FILE, FileEntryType.FILE),
        (ModalFileEntryType.DIRECTORY, FileEntryType.DIRECTORY),
    ],
)
def test_to_file_entry_type(modal_type: ModalFileEntryType, expected: FileEntryType) -> None:
    assert _to_file_entry_type(modal_type) == expected


def test_to_file_entry_type_rejects_unsupported_value() -> None:
    # A value outside the Modal FileEntryType enum (e.g. SYMLINK/FIFO members
    # that we don't map) hits the `case _` default arm and must raise.
    unsupported_value: Any = object()
    with pytest.raises(ModalProxyError, match="Unsupported Modal FileEntryType"):
        _to_file_entry_type(unsupported_value)


# --- Unwrap helpers ---


@pytest.mark.parametrize(
    ("unwrap_fn", "fake_cls"),
    [
        (_unwrap_image, _FakeImage),
        (_unwrap_app, _FakeApp),
        (_unwrap_volume, _FakeVolume),
        (_unwrap_secret, _FakeSecret),
    ],
    ids=["image", "app", "volume", "secret"],
)
def test_unwrap_rejects_non_direct(unwrap_fn: Callable[[Any], Any], fake_cls: Any) -> None:
    with pytest.raises(ModalProxyTypeError):
        unwrap_fn(fake_cls.model_construct())


def test_unwrap_image_returns_underlying_modal_image() -> None:
    # Build a genuinely-validated DirectImage around a real modal.Image (these
    # construct offline without credentials) so the test exercises real pydantic
    # field validation, not just attribute selection on a model_construct shell.
    image = modal.Image.debian_slim()
    assert _unwrap_image(DirectImage(image=image)) is image


def test_unwrap_app_returns_underlying_modal_app() -> None:
    app = modal.App(f"test-app-{uuid4().hex}")
    assert _unwrap_app(DirectApp(app=app)) is app


def test_unwrap_volume_returns_underlying_modal_volume() -> None:
    volume = modal.Volume.from_name(f"test-vol-{uuid4().hex}", create_if_missing=False)
    assert _unwrap_volume(DirectVolume(volume=volume)) is volume


def test_unwrap_secret_returns_underlying_modal_secret() -> None:
    secret = modal.Secret.from_dict({"token": uuid4().hex})
    assert _unwrap_secret(DirectSecret(secret=secret)) is secret


# --- CLI not found tests ---


def _make_file_not_found(filename: str) -> FileNotFoundError:
    e = FileNotFoundError(2, "No such file or directory")
    e.filename = filename
    return e


def test_translate_modal_cli_not_found_raises_for_modal() -> None:
    with pytest.raises(ModalProxyError, match="modal.*CLI command was not found"):
        _translate_modal_cli_not_found(_make_file_not_found("modal"))


def test_translate_modal_cli_not_found_reraises_for_other() -> None:
    with pytest.raises(FileNotFoundError):
        _translate_modal_cli_not_found(_make_file_not_found("other_binary"))


# --- Error translation tests ---


@pytest.mark.parametrize(
    ("modal_exc", "expected_type"),
    [
        pytest.param(modal.exception.AuthError("auth"), ModalProxyAuthError, id="auth"),
        pytest.param(
            modal.exception.PermissionDeniedError("denied"),
            ModalProxyPermissionDeniedError,
            id="permission_denied",
        ),
        pytest.param(modal.exception.NotFoundError("missing"), ModalProxyNotFoundError, id="not_found"),
        pytest.param(modal.exception.InvalidError("invalid"), ModalProxyInvalidError, id="invalid"),
        pytest.param(modal.exception.InternalError("internal"), ModalProxyInternalError, id="internal"),
        pytest.param(
            modal.exception.ResourceExhaustedError("rate"),
            ModalProxyRateLimitError,
            id="resource_exhausted",
        ),
        pytest.param(modal.exception.RemoteError("remote"), ModalProxyRemoteError, id="remote"),
        # A bare modal.exception.Error that matches none of the specific branches
        # must fall through to the generic ModalProxyError.
        pytest.param(modal.exception.Error("generic"), ModalProxyError, id="fallback_generic"),
    ],
)
def test_translate_modal_error_maps_each_branch_to_its_proxy_type(
    modal_exc: modal.exception.Error, expected_type: type[ModalProxyError]
) -> None:
    result = _translate_modal_error(modal_exc)
    # Use exact type, not isinstance: every ModalProxy* error subclasses
    # ModalProxyError, so isinstance would not catch a branch that mapped to the
    # wrong (more general or sibling) type.
    assert type(result) is expected_type
    # The original message must be preserved through the translation.
    assert str(result) == str(modal_exc)


# --- Volume retry predicate tests ---


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(modal.exception.InternalError("server error"), True, id="internal_error"),
        pytest.param(modal.exception.ResourceExhaustedError("rate limit"), True, id="resource_exhausted"),
        pytest.param(StreamTerminatedError("stream dropped"), True, id="stream_terminated"),
        pytest.param(ProtocolError("protocol error"), True, id="protocol_error"),
        pytest.param(
            modal.exception.NotFoundError("Environment 'mngr-abc123' not found"),
            True,
            id="environment_not_found",
        ),
        pytest.param(
            modal.exception.NotFoundError("File '/hosts/foo.json' not found"),
            False,
            id="path_not_found",
        ),
        # Regression: a path-level not-found whose path contains the substring "Environment"
        # must not be misclassified as an environment-not-found error.
        pytest.param(
            modal.exception.NotFoundError("File '/Environment/foo.json' not found"),
            False,
            id="path_containing_environment_substring",
        ),
        pytest.param(modal.exception.AuthError("bad token"), False, id="auth_error"),
    ],
)
def test_should_retry_volume_op(exc: BaseException, expected: bool) -> None:
    assert _should_retry_volume_op(exc) is expected

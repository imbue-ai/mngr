import os
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
from imbue.modal_proxy.direct import DirectFunction
from imbue.modal_proxy.direct import DirectImage
from imbue.modal_proxy.direct import DirectModalInterface
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
from imbue.modal_proxy.errors import ModalProxyAppLockedError
from imbue.modal_proxy.errors import ModalProxyAuthError
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.errors import ModalProxyInternalError
from imbue.modal_proxy.errors import ModalProxyInvalidError
from imbue.modal_proxy.errors import ModalProxyNotFoundError
from imbue.modal_proxy.errors import ModalProxyRateLimitError
from imbue.modal_proxy.errors import ModalProxyRemoteError
from imbue.modal_proxy.errors import ModalProxyTypeError
from imbue.modal_proxy.errors import is_app_locked_error
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


# --- App-locked detection tests ---


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param(
            "The selected app is locked - probably due to a concurrent modification",
            True,
            id="real_modal_message",
        ),
        pytest.param("ERROR: the SELECTED APP IS LOCKED right now", True, id="case_insensitive"),
        pytest.param("Failed to deploy snapshot.py: some other error", False, id="unrelated_error"),
        pytest.param("", False, id="empty"),
    ],
)
def test_is_app_locked_error(message: str, expected: bool) -> None:
    assert is_app_locked_error(message) is expected


# --- Deploy retry tests ---

# The real Modal message; deploy must classify this as retryable.
_LOCKED_APP_MESSAGE = "Error: The selected app is locked - probably due to a concurrent modification"


def _write_fake_modal(bin_dir: Path, counter_file: Path, *, fail_times: int, error_message: str) -> None:
    """Install a fake ``modal`` executable on PATH that fails the first ``fail_times`` invocations.

    Each call increments ``counter_file``; while the count is within
    ``fail_times`` it prints ``error_message`` to stderr and exits 1, otherwise
    it exits 0. This lets deploy retry tests run hermetically without Modal
    credentials or network access.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "modal"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'counter="{counter_file}"\n'
        'n=$(cat "$counter" 2>/dev/null || echo 0)\n'
        "n=$((n + 1))\n"
        'echo "$n" > "$counter"\n'
        f'if [ "$n" -le {fail_times} ]; then\n'
        f'  echo "{error_message}" >&2\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
    )
    script.chmod(0o755)


def test_deploy_retries_on_locked_app_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    counter = tmp_path / "count"
    _write_fake_modal(bin_dir, counter, fail_times=1, error_message=_LOCKED_APP_MESSAGE)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    # Should ride through the transient lock and return normally.
    DirectModalInterface().deploy(tmp_path / "snapshot.py", app_name="my-app")

    assert counter.read_text().strip() == "2", "expected one failed attempt followed by a successful retry"


def test_deploy_does_not_retry_on_non_lock_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    counter = tmp_path / "count"
    _write_fake_modal(bin_dir, counter, fail_times=10, error_message="Error: image build failed")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(ModalProxyError) as exc_info:
        DirectModalInterface().deploy(tmp_path / "snapshot.py", app_name="my-app")

    assert not isinstance(exc_info.value, ModalProxyAppLockedError)
    assert counter.read_text().strip() == "1", "non-lock failures must not be retried"


# --- Post-deploy lookup retry tests ---


class _FakeFunction:
    """A stand-in modal.Function whose get_web_url raises a fixed number of times.

    Used to drive DirectFunction.get_web_url through its retry path without a
    real Modal connection.
    """

    def __init__(self, error: Exception, *, fail_times: int, web_url: str | None = "https://example.com") -> None:
        self._error = error
        self._fail_times = fail_times
        self._web_url = web_url
        self.call_count = 0

    def get_web_url(self) -> str | None:
        self.call_count += 1
        if self.call_count <= self._fail_times:
            raise self._error
        return self._web_url


def test_get_web_url_retries_on_not_found_then_succeeds() -> None:
    fake = _FakeFunction(modal.exception.NotFoundError("Lookup failed for Function 'foo'"), fail_times=1)
    function = DirectFunction.model_construct(function=fake)

    assert function.get_web_url() == "https://example.com"
    assert fake.call_count == 2, "expected one failed lookup followed by a successful retry"


def test_get_web_url_does_not_retry_on_other_error() -> None:
    fake = _FakeFunction(modal.exception.InvalidError("bad request"), fail_times=10)
    function = DirectFunction.model_construct(function=fake)

    with pytest.raises(ModalProxyInvalidError):
        function.get_web_url()

    assert fake.call_count == 1, "non-NotFound failures must not be retried"

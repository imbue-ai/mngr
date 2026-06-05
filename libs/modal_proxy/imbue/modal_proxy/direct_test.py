import os
from collections.abc import Generator
from pathlib import Path
from typing import Any
from typing import Mapping
from typing import Sequence

import modal.exception
import pytest
from modal.stream_type import StreamType as ModalStreamType
from modal.volume import FileEntryType as ModalFileEntryType

from imbue.modal_proxy.data_types import FileEntry
from imbue.modal_proxy.data_types import FileEntryType
from imbue.modal_proxy.data_types import StreamType
from imbue.modal_proxy.direct import DirectApp
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
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.errors import ModalProxyPermissionDeniedError
from imbue.modal_proxy.errors import ModalProxyRateLimitError
from imbue.modal_proxy.errors import ModalProxyTypeError
from imbue.modal_proxy.errors import is_app_locked_error
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


def test_translate_resource_exhausted_to_rate_limit_error() -> None:
    modal_err = modal.exception.ResourceExhaustedError("VolumeListFiles rate limit exceeded")
    result = _translate_modal_error(modal_err)
    assert isinstance(result, ModalProxyRateLimitError)
    assert "rate limit" in str(result)


def test_translate_permission_denied_to_permission_denied_error() -> None:
    modal_err = modal.exception.PermissionDeniedError("user lacks access to environment 'mngr_test-abc'")
    result = _translate_modal_error(modal_err)
    assert isinstance(result, ModalProxyPermissionDeniedError)
    assert "lacks access" in str(result)


# --- Volume retry predicate tests ---


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        pytest.param(modal.exception.InternalError("server error"), True, id="internal_error"),
        pytest.param(modal.exception.ResourceExhaustedError("rate limit"), True, id="resource_exhausted"),
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

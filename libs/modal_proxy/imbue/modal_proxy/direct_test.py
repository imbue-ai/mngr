import os
from collections.abc import Callable
from collections.abc import Generator
from pathlib import Path
from types import FunctionType
from typing import Any
from typing import Final
from typing import Mapping
from typing import Sequence
from uuid import uuid4

import modal
import modal.exception
import pytest
from grpclib.exceptions import ProtocolError
from grpclib.exceptions import StreamTerminatedError
from modal.config import config
from modal.stream_type import StreamType as ModalStreamType
from modal.types import FileEntryType as ModalFileEntryType
from pydantic import BaseModel
from tenacity import RetryCallState
from tenacity import Retrying

from imbue.modal_proxy.data_types import FileEntry
from imbue.modal_proxy.data_types import FileEntryType
from imbue.modal_proxy.data_types import StreamType
from imbue.modal_proxy.direct import DirectApp
from imbue.modal_proxy.direct import DirectFunction
from imbue.modal_proxy.direct import DirectImage
from imbue.modal_proxy.direct import DirectModalInterface
from imbue.modal_proxy.direct import DirectSandbox
from imbue.modal_proxy.direct import DirectSecret
from imbue.modal_proxy.direct import DirectVolume
from imbue.modal_proxy.direct import _MAX_RETRY_JITTER_SECONDS
from imbue.modal_proxy.direct import _TRANSIENT_RETRY
from imbue.modal_proxy.direct import _TRANSIENT_RETRY_BUDGET_SECONDS
from imbue.modal_proxy.direct import _TRANSIENT_STOP
from imbue.modal_proxy.direct import _is_transient_modal_error
from imbue.modal_proxy.direct import _to_file_entry_type
from imbue.modal_proxy.direct import _to_modal_stream_type
from imbue.modal_proxy.direct import _transient_wait
from imbue.modal_proxy.direct import _translate_modal_cli_not_found
from imbue.modal_proxy.direct import _translate_modal_error
from imbue.modal_proxy.direct import _unwrap_app
from imbue.modal_proxy.direct import _unwrap_image
from imbue.modal_proxy.direct import _unwrap_secret
from imbue.modal_proxy.direct import _unwrap_volume
from imbue.modal_proxy.errors import ModalProxyAppLockedError
from imbue.modal_proxy.errors import ModalProxyAuthError
from imbue.modal_proxy.errors import ModalProxyConnectionError
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.errors import ModalProxyImageBuildError
from imbue.modal_proxy.errors import ModalProxyInternalError
from imbue.modal_proxy.errors import ModalProxyInvalidError
from imbue.modal_proxy.errors import ModalProxyNotFoundError
from imbue.modal_proxy.errors import ModalProxyRateLimitError
from imbue.modal_proxy.errors import ModalProxyRemoteError
from imbue.modal_proxy.errors import ModalProxyTypeError
from imbue.modal_proxy.errors import is_app_locked_error
from imbue.modal_proxy.errors import is_deploy_function_vanished_error
from imbue.modal_proxy.interface import AppInterface
from imbue.modal_proxy.interface import ImageInterface
from imbue.modal_proxy.interface import SecretInterface
from imbue.modal_proxy.interface import VolumeInterface

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

    def fetch_build_logs(self) -> str:
        raise NotImplementedError


class _FakeVolume(VolumeInterface):
    """Non-Direct VolumeInterface for testing unwrap rejection."""

    def get_name(self) -> str | None:
        return None

    def get_object_id(self) -> str:
        raise NotImplementedError

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
        # ImageBuildError subclasses RemoteError, so it only gets its own type if
        # its branch is checked first. Callers rely on the distinction to know
        # they can ask Modal for the failed layer's build output.
        pytest.param(
            modal.exception.ImageBuildError("build failed", "im-123"),
            ModalProxyImageBuildError,
            id="image_build",
        ),
        # The control plane could not be reached at all -- what a dropped network
        # or a Modal outage looks like. This must get its own type rather than
        # falling through to the generic branch: consumers decide "Modal is
        # temporarily unavailable" (skippable) from "Modal answered with a real
        # failure" (fatal) on exactly this distinction.
        pytest.param(
            modal.exception.ConnectionError("Could not connect to the Modal server."),
            ModalProxyConnectionError,
            id="connection",
        ),
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
def test_is_transient_modal_error(exc: BaseException, expected: bool) -> None:
    assert _is_transient_modal_error(exc) is expected


def _make_retry_state(exception: BaseException | None, attempt_number: int) -> RetryCallState:
    retry_state = RetryCallState(retry_object=Retrying(), fn=None, args=(), kwargs={})
    retry_state.attempt_number = attempt_number
    if exception is not None:
        retry_state.set_exception((type(exception), exception, None))
    return retry_state


@pytest.mark.parametrize(
    ("exception", "attempt_number", "expected_curve_wait"),
    [
        # Rate limits get the long curve (5/10/20/30s): the limit is shared
        # across every concurrent client, so a burst can outlast the ordinary
        # curve's ~15s of total backoff.
        pytest.param(modal.exception.ResourceExhaustedError("rate limit"), 1, 5.0, id="rate_limit_first"),
        pytest.param(modal.exception.ResourceExhaustedError("rate limit"), 4, 30.0, id="rate_limit_capped"),
        # Ordinary transient errors keep the original fast curve (1/2/4/8s) so
        # genuine failures still surface quickly.
        pytest.param(modal.exception.InternalError("server error"), 1, 1.0, id="transient_first"),
        pytest.param(modal.exception.InternalError("server error"), 4, 8.0, id="transient_fourth"),
        # No recorded outcome falls back to the ordinary curve.
        pytest.param(None, 1, 1.0, id="no_outcome"),
    ],
)
def test_transient_wait_gives_rate_limits_longer_backoff(
    exception: BaseException | None, attempt_number: int, expected_curve_wait: float
) -> None:
    retry_state = _make_retry_state(exception, attempt_number)
    waits = [_transient_wait(retry_state) for _ in range(20)]

    assert all(expected_curve_wait <= wait <= expected_curve_wait + _MAX_RETRY_JITTER_SECONDS for wait in waits)
    assert len(set(waits)) > 1, "concurrent readers must not march back in lockstep"


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


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param("Error: Function fu-7O62Sp60LIHTdROU1VJl6q not found", True, id="real_modal_message"),
        pytest.param("error: FUNCTION FU-abc123 NOT FOUND", True, id="case_insensitive"),
        pytest.param("Lookup failed for Function 'snapshot_and_shutdown' not found", False, id="name_not_id"),
        pytest.param("Failed to deploy snapshot.py: some other error", False, id="unrelated_error"),
        pytest.param("", False, id="empty"),
    ],
)
def test_is_deploy_function_vanished_error(message: str, expected: bool) -> None:
    assert is_deploy_function_vanished_error(message) is expected


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


def test_deploy_retries_on_vanished_function_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The function-vanished flavor of the concurrent-deploy race must retry like the app lock."""
    bin_dir = tmp_path / "bin"
    counter = tmp_path / "count"
    _write_fake_modal(
        bin_dir, counter, fail_times=1, error_message="Error: Function fu-7O62Sp60LIHTdROU1VJl6q not found"
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

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


def test_direct_modal_interface_enables_sandbox_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing the real Modal boundary opts every Sandbox operation into the V2 Sandbox backend."""
    monkeypatch.delenv("MODAL_SANDBOX_V2", raising=False)

    DirectModalInterface()

    assert os.environ["MODAL_SANDBOX_V2"] == "1"
    assert config.get("sandbox_v2") is True


def test_direct_modal_interface_respects_explicit_sandbox_v2_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit MODAL_SANDBOX_V2=0 survives construction, so V2 can still be turned off."""
    monkeypatch.setenv("MODAL_SANDBOX_V2", "0")

    DirectModalInterface()

    assert os.environ["MODAL_SANDBOX_V2"] == "0"
    assert config.get("sandbox_v2") is False


class _FakeModalSandbox:
    """Minimal stand-in for modal.Sandbox.

    Raises ``error`` until ``fail_times`` calls have been made (forever when it
    is None), which drives the boundary's retry path without a Modal connection.
    """

    def __init__(
        self,
        poll_result: int | None = None,
        error: modal.exception.Error | None = None,
        fail_times: int | None = None,
        tags: Mapping[str, str] = {},
    ) -> None:
        self._poll_result = poll_result
        self._error = error
        self._fail_times = fail_times
        self._tags = dict(tags)
        self.call_count = 0

    def _fail_while_due(self) -> None:
        self.call_count += 1
        if self._error is not None and (self._fail_times is None or self.call_count <= self._fail_times):
            raise self._error

    def poll(self) -> int | None:
        self._fail_while_due()
        return self._poll_result

    def get_tags(self) -> dict[str, str]:
        self._fail_while_due()
        return self._tags


def test_direct_sandbox_poll_returns_none_while_running() -> None:
    sandbox = DirectSandbox.model_construct(sandbox=_FakeModalSandbox(poll_result=None))
    assert sandbox.poll() is None


def test_direct_sandbox_poll_returns_exit_code_when_finished() -> None:
    sandbox = DirectSandbox.model_construct(sandbox=_FakeModalSandbox(poll_result=137))
    assert sandbox.poll() == 137


def test_direct_sandbox_poll_translates_modal_errors() -> None:
    sandbox = DirectSandbox.model_construct(sandbox=_FakeModalSandbox(error=modal.exception.NotFoundError("gone")))
    with pytest.raises(ModalProxyNotFoundError):
        sandbox.poll()


def test_get_tags_rides_out_a_transient_modal_failure() -> None:
    """A tag read is made on every discovery; one transient status must not fail the whole command."""
    fake = _FakeModalSandbox(error=modal.exception.InternalError("PU7M8MBO"), fail_times=1, tags={"host_id": "h-1"})
    sandbox = DirectSandbox.model_construct(sandbox=fake)

    assert sandbox.get_tags() == {"host_id": "h-1"}
    assert fake.call_count == 2, "expected one transient failure followed by a successful retry"


def test_get_tags_does_not_retry_a_semantic_modal_failure() -> None:
    """Modal answering "no" is a verdict, not a blip: surface it on the first attempt."""
    fake = _FakeModalSandbox(error=modal.exception.InvalidError("bad sandbox"))
    sandbox = DirectSandbox.model_construct(sandbox=fake)

    with pytest.raises(ModalProxyInvalidError):
        sandbox.get_tags()

    assert fake.call_count == 1, "a semantic failure must not be retried"


def test_transient_retry_gives_up_on_elapsed_time_not_attempt_count() -> None:
    """However many attempts fit in the budget, what ends the retry is spending its seconds."""

    def is_stopped_after(elapsed_seconds: float, attempt_number: int) -> bool:
        retry_state = _make_retry_state(modal.exception.InternalError("PU7M8MBO"), attempt_number)
        retry_state.start_time = retry_state.start_time - elapsed_seconds
        return _TRANSIENT_STOP(retry_state)

    assert is_stopped_after(_TRANSIENT_RETRY_BUDGET_SECONDS - 1.0, attempt_number=99) is False
    assert is_stopped_after(_TRANSIENT_RETRY_BUDGET_SECONDS + 1.0, attempt_number=2) is True


# Every Modal call this boundary makes, and whether a transient failure makes
# us re-issue it. Retrying is safe exactly when issuing the call twice is
# indistinguishable from issuing it once, which is a property of the operation,
# so every call has to answer for itself.
_IS_RETRIED_BY_BOUNDARY_CALL: Final[Mapping[FunctionType, bool]] = {
    # Reads, made on every discovery.
    DirectModalInterface.app_lookup: True,
    DirectModalInterface.sandbox_list: True,
    DirectModalInterface.sandbox_from_id: True,
    DirectModalInterface.volume_list: True,
    DirectModalInterface.is_function_deployed: True,
    DirectSandbox.get_tags: True,
    DirectSandbox.poll: True,
    DirectSandbox.tunnels: True,
    DirectVolume.get_object_id: True,
    DirectVolume.listdir: True,
    DirectVolume.read_file: True,
    # Writes that land the same result however many times they run.
    DirectVolume.remove_file: True,
    DirectVolume.write_files: True,
    # Mutations: Modal may have applied one of these before failing to say so.
    DirectModalInterface.sandbox_create: False,
    DirectModalInterface.volume_delete: False,
    DirectModalInterface.environment_create: False,
    DirectSandbox.exec: False,
    DirectSandbox.set_tags: False,
    DirectSandbox.snapshot_filesystem: False,
    DirectSandbox.terminate: False,
    DirectImage.build: False,
    DirectApp.run: False,
    # Local construction or SDK-lazy handles -- no control-plane call to retry.
    DirectModalInterface.app_create: False,
    DirectModalInterface.image_debian_slim: False,
    DirectModalInterface.image_from_registry: False,
    DirectModalInterface.image_from_id: False,
    DirectModalInterface.volume_from_name: False,
    DirectModalInterface.secret_from_dict: False,
    DirectModalInterface.function_from_name: False,
    DirectModalInterface.enable_output_capture: False,
    DirectSandbox.get_object_id: False,
    DirectVolume.get_name: False,
    DirectVolume.reload: False,
    DirectVolume.commit: False,
    DirectImage.get_object_id: False,
    DirectImage.apt_install: False,
    DirectImage.dockerfile_commands: False,
    DirectApp.get_app_id: False,
    DirectApp.get_name: False,
    # These carry retries of their own, sized for a different failure.
    DirectModalInterface.deploy: False,
    DirectFunction.get_web_url: False,
    DirectImage.fetch_build_logs: False,
}

_BOUNDARY_CLASSES: Final[Sequence[type]] = (
    DirectModalInterface,
    DirectApp,
    DirectImage,
    DirectSandbox,
    DirectVolume,
    DirectFunction,
)


def _public_methods(cls: type) -> set[FunctionType]:
    """The Modal calls a boundary class defines, excluding pydantic's own model hooks."""
    return {
        member
        for name, member in vars(cls).items()
        if not name.startswith("_") and isinstance(member, FunctionType) and name not in dir(BaseModel)
    }


@pytest.mark.parametrize(
    ("boundary_call", "is_retried"),
    [
        pytest.param(boundary_call, is_retried, id=boundary_call.__qualname__)
        for boundary_call, is_retried in _IS_RETRIED_BY_BOUNDARY_CALL.items()
    ],
)
def test_boundary_call_retries_transient_failures_iff_it_is_safe_to_reissue(
    boundary_call: FunctionType, is_retried: bool
) -> None:
    # tenacity records its policy on the function it wraps, and functools.wraps
    # carries that through _translate_exceptions.
    retrying = vars(boundary_call).get("retry")
    if not is_retried:
        assert retrying is None or retrying.retry is not _TRANSIENT_RETRY
        return
    assert retrying is not None, f"{boundary_call.__qualname__} carries no retry"
    assert retrying.retry is _TRANSIENT_RETRY, f"{boundary_call.__qualname__} does not use the shared policy"
    assert retrying.stop is _TRANSIENT_STOP, f"{boundary_call.__qualname__} does not use the shared budget"


def test_every_boundary_call_declares_whether_it_is_safe_to_reissue() -> None:
    """A method added to the boundary has to state whether it is safe to re-issue."""
    declared = set(_IS_RETRIED_BY_BOUNDARY_CALL)
    defined = {method for cls in _BOUNDARY_CLASSES for method in _public_methods(cls)}

    assert sorted(method.__qualname__ for method in defined - declared) == []
    assert sorted(method.__qualname__ for method in declared - defined) == []

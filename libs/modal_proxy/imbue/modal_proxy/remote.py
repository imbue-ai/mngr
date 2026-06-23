# Remote implementation of ModalInterface that proxies to the imbue_cloud
# connector ("gateway") over HTTP, instead of talking to Modal directly.
#
# This is the ``ModalMode.PROXIED`` transport: the client never holds Modal
# credentials. Each operation becomes an HTTP call to the connector's
# ``/modal/sandboxes`` routes; the connector -- a Modal *function*, which (unlike
# a sandbox) has control-plane access -- fulfils it with a DirectModalInterface
# against its own workspace. See ``libs/mngr/future_specs/providers/modal.md``
# for the bridge-function principle this implements.
#
# Only the subset of ModalInterface needed for the ephemeral, non-persistent
# testing flow (create sandbox -> provision over exec -> reach via tunnel ->
# terminate) is supported. Persistent-host features (volumes, snapshots, image
# builds, deploy) raise a clear error: PROXIED is "Modal (experimental)".

from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import AbstractContextManager
from contextlib import nullcontext
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.modal_proxy.data_types import FileEntry
from imbue.modal_proxy.data_types import SandboxCreateRequest
from imbue.modal_proxy.data_types import SandboxCreateResponse
from imbue.modal_proxy.data_types import SandboxExecRequest
from imbue.modal_proxy.data_types import SandboxExecResponse
from imbue.modal_proxy.data_types import SandboxListResponse
from imbue.modal_proxy.data_types import SandboxTagsRequest
from imbue.modal_proxy.data_types import SandboxTunnelsResponse
from imbue.modal_proxy.data_types import StreamType
from imbue.modal_proxy.data_types import TunnelInfo
from imbue.modal_proxy.errors import ModalProxyAuthError
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.errors import ModalProxyInvalidError
from imbue.modal_proxy.errors import ModalProxyNotFoundError
from imbue.modal_proxy.errors import ModalProxyRateLimitError
from imbue.modal_proxy.errors import ModalProxyRemoteError
from imbue.modal_proxy.interface import AppInterface
from imbue.modal_proxy.interface import ExecOutput
from imbue.modal_proxy.interface import ExecProcess
from imbue.modal_proxy.interface import FunctionInterface
from imbue.modal_proxy.interface import ImageInterface
from imbue.modal_proxy.interface import ModalInterface
from imbue.modal_proxy.interface import SandboxInterface
from imbue.modal_proxy.interface import SecretInterface
from imbue.modal_proxy.interface import VolumeInterface
from imbue.modal_proxy.log_utils import ModalLoguruWriter

# Per-request timeouts. Exec runs synchronously server-side and may install
# packages (slow), so it gets a generous budget; other calls are quick.
_DEFAULT_TIMEOUT_SECONDS = 120.0
_EXEC_TIMEOUT_SECONDS = 600.0

_UNSUPPORTED_MESSAGE = (
    "{op} is not supported in PROXIED Modal mode (Modal experimental). PROXIED supports only "
    "ephemeral, non-persistent sandboxes; use DIRECT mode for volumes/snapshots/image builds."
)


def _raise_for_unsupported(op: str) -> None:
    raise ModalProxyError(_UNSUPPORTED_MESSAGE.format(op=op))


def _is_background_command(args: Sequence[str], stdout: StreamType) -> bool:
    """Mirror FakeSandbox's heuristic for commands that must not block.

    The create flow starts ``sshd -D`` as a long-running background process
    (with DEVNULL streams) and ``.wait()``s on every other exec. Keeping the
    detection identical keeps DIRECT/FAKE/PROXIED behaviour aligned.
    """
    if not args:
        return False
    if args[-1] == "&":
        return True
    if args[0] == "/usr/sbin/sshd" and "-D" in args:
        return True
    if "nohup" in args[0]:
        return True
    return False


class _ConnectorTransport(MutableModel):
    """Thin authenticated HTTP client for the connector's ``/modal`` routes.

    Held as a private attr on the interface/objects. ``token_provider`` is
    called per request so a freshly-refreshed bearer token is always used (the
    connector rotates SuperTokens sessions). ``injected_client`` lets tests
    supply an httpx ``MockTransport`` client so the whole client->connector->modal
    path can run in-process without a network.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    base_url: str = Field(description="Connector base URL")
    token_provider: Callable[[], SecretStr] = Field(repr=False, description="Returns a fresh bearer token")
    injected_client: httpx.Client | None = Field(default=None, repr=False, description="Test-injected client")
    _client: httpx.Client = PrivateAttr()

    def model_post_init(self, _context: object) -> None:
        self._client = (
            self.injected_client if self.injected_client is not None else httpx.Client(timeout=_DEFAULT_TIMEOUT_SECONDS)
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token_provider().get_secret_value()}"}

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        url = f"{self.base_url.rstrip('/')}{path}"
        try:
            response = self._client.request(
                method, url, json=json_body, params=params, headers=self._headers(), timeout=timeout
            )
        except httpx.RequestError as e:
            raise ModalProxyError(f"Connector request to {path} failed: {e}") from e
        _raise_for_status(response, path)
        if not response.content:
            return {}
        return response.json()


def _raise_for_status(response: httpx.Response, path: str) -> None:
    """Translate connector HTTP errors into ModalProxy* exceptions at the boundary."""
    if response.is_success:
        return
    code = response.status_code
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    message = f"Connector {path} returned {code}: {detail}"
    if code in (401, 403):
        raise ModalProxyAuthError(message)
    if code == 404:
        raise ModalProxyNotFoundError(message)
    if code == 429:
        raise ModalProxyRateLimitError(message)
    if code >= 500:
        raise ModalProxyRemoteError(message)
    raise ModalProxyInvalidError(message)


# ---------------------------------------------------------------------------
# Object implementations -- lightweight handles that defer to the connector
# ---------------------------------------------------------------------------


class RemoteExecOutput(ExecOutput):
    """Captured stdout from a completed (synchronous) proxied exec."""

    output_text: str = Field(default="", description="The captured stdout text")

    def read(self) -> str:
        return self.output_text


class RemoteExecProcess(ExecProcess):
    """A proxied exec result.

    Proxied execs are synchronous: by the time the call returns, the command
    has already run to completion server-side (or been started detached, for
    background commands), so the exit code and stdout are already known.
    """

    exit_code_value: int = Field(description="The process exit code")
    stdout_text: str = Field(default="", description="The captured stdout")

    def get_stdout(self) -> ExecOutput:
        return RemoteExecOutput(output_text=self.stdout_text)

    def wait(self) -> int:
        return self.exit_code_value


class RemoteImage(ImageInterface):
    """A deferred image spec: the real modal.Image is built connector-side at create."""

    image_ref: str | None = Field(default=None, description="Registry ref; None => debian_slim base")
    apt_packages: tuple[str, ...] = Field(default=(), description="apt packages layered on the base")

    def get_object_id(self) -> str:
        return self.image_ref or "debian_slim"

    def apt_install(self, *packages: str) -> "ImageInterface":
        return RemoteImage(image_ref=self.image_ref, apt_packages=self.apt_packages + tuple(packages))

    def dockerfile_commands(
        self,
        commands: Sequence[str],
        *,
        context_dir: Path | None = None,
        secrets: Sequence[SecretInterface] = (),
    ) -> "ImageInterface":
        _raise_for_unsupported("Dockerfile image builds")
        raise AssertionError("unreachable")

    def build(self, app: AppInterface) -> None:
        # No-op: the connector builds the image when it creates the sandbox.
        return None


class RemoteApp(AppInterface):
    """A deferred app handle. The connector resolves/creates the real app at create time."""

    app_name: str = Field(description="The modal app name")
    environment_name: str = Field(description="The modal environment scoping this app")

    def get_app_id(self) -> str:
        # The app name doubles as the id for proxied listing/scoping; the real
        # modal app id only exists connector-side.
        return self.app_name

    def get_name(self) -> str:
        return self.app_name

    def run(self, *, environment_name: str) -> Generator["AppInterface", None, None]:
        # No-op context: there is no client-side ephemeral Modal session to hold
        # open; the connector owns the app lifecycle.
        yield self


class RemoteSandbox(SandboxInterface):
    """A sandbox living in the connector's Modal workspace, reached over HTTP."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    sandbox_id: str = Field(description="The sandbox's object id")
    _transport: _ConnectorTransport = PrivateAttr()

    def get_object_id(self) -> str:
        return self.sandbox_id

    def exec(
        self,
        *args: str,
        stdout: StreamType = StreamType.PIPE,
        stderr: StreamType = StreamType.PIPE,
    ) -> ExecProcess:
        is_background = _is_background_command(args, stdout)
        request = SandboxExecRequest(
            args=tuple(args),
            is_background=is_background,
            is_stdout_captured=(stdout == StreamType.PIPE),
        )
        raw = self._transport.request(
            "POST",
            f"/modal/sandboxes/{self.sandbox_id}/exec",
            json_body=request.model_dump(mode="json"),
            timeout=_EXEC_TIMEOUT_SECONDS,
        )
        response = SandboxExecResponse.model_validate(raw)
        return RemoteExecProcess(exit_code_value=response.exit_code, stdout_text=response.stdout)

    def tunnels(self, *, timeout: int = 50) -> dict[int, TunnelInfo]:
        raw = self._transport.request(
            "GET",
            f"/modal/sandboxes/{self.sandbox_id}/tunnels",
            params={"timeout": timeout},
            timeout=float(timeout) + _DEFAULT_TIMEOUT_SECONDS,
        )
        response = SandboxTunnelsResponse.model_validate(raw)
        return {int(port): info for port, info in response.tunnels.items()}

    def get_tags(self) -> dict[str, str]:
        raw = self._transport.request("GET", f"/modal/sandboxes/{self.sandbox_id}/tags")
        return SandboxTagsRequest.model_validate(raw).tags

    def set_tags(self, tags: Mapping[str, str]) -> None:
        request = SandboxTagsRequest(tags=dict(tags))
        self._transport.request(
            "PUT", f"/modal/sandboxes/{self.sandbox_id}/tags", json_body=request.model_dump(mode="json")
        )

    def snapshot_filesystem(self, timeout: int = 120) -> ImageInterface:
        _raise_for_unsupported("Filesystem snapshots")
        raise AssertionError("unreachable")

    def terminate(self) -> None:
        self._transport.request("DELETE", f"/modal/sandboxes/{self.sandbox_id}")


# ---------------------------------------------------------------------------
# Top-level implementation
# ---------------------------------------------------------------------------


class RemoteModalInterface(ModalInterface):
    """ModalInterface implementation that proxies the testing subset to the connector.

    Construct with the connector ``base_url``, a ``token_provider`` returning a
    fresh bearer token, and the Modal ``environment`` to scope resources to.
    Every app handle is stamped with that environment so ``sandbox_create`` can
    forward it to the connector (the interface's ``sandbox_create`` signature
    carries the app, not the env).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    base_url: str = Field(description="Connector base URL")
    environment: str = Field(description="Modal environment name to scope apps/sandboxes to")
    token_provider: Callable[[], SecretStr] = Field(repr=False, description="Returns a fresh bearer token")
    http_client: httpx.Client | None = Field(
        default=None, repr=False, description="Injected HTTP client (tests use a MockTransport client)"
    )
    _transport: _ConnectorTransport = PrivateAttr()

    def model_post_init(self, _context: object) -> None:
        self._transport = _ConnectorTransport(
            base_url=self.base_url, token_provider=self.token_provider, injected_client=self.http_client
        )

    # Environment -- the connector ensures the environment exists at create time.
    def environment_create(self, name: str) -> None:
        return None

    # App -- both paths return a deferred handle carrying the configured env.
    def app_create(self, name: str) -> AppInterface:
        return RemoteApp(app_name=name, environment_name=self.environment)

    def app_lookup(
        self,
        name: str,
        *,
        create_if_missing: bool = True,
        environment_name: str,
    ) -> AppInterface:
        return RemoteApp(app_name=name, environment_name=environment_name)

    # Image -- deferred specs; the connector builds the real image at create.
    def image_debian_slim(self) -> ImageInterface:
        return RemoteImage(image_ref=None)

    def image_from_registry(self, name: str) -> ImageInterface:
        return RemoteImage(image_ref=name)

    def image_from_id(self, image_id: str) -> ImageInterface:
        # Loading an image by id is only used to restart from a snapshot, which
        # PROXIED does not support.
        _raise_for_unsupported("image_from_id (snapshot restart)")
        raise AssertionError("unreachable")

    # Sandbox
    def sandbox_create(
        self,
        *,
        image: ImageInterface,
        app: AppInterface,
        timeout: int,
        cpu: float,
        memory: int,
        unencrypted_ports: Sequence[int] = (),
        gpu: str | None = None,
        region: str | None = None,
        cidr_allowlist: Sequence[str] | None = None,
        volumes: Mapping[str, VolumeInterface] | None = None,
    ) -> SandboxInterface:
        if volumes:
            _raise_for_unsupported("Mounting volumes on a sandbox")
        if not isinstance(image, RemoteImage):
            raise ModalProxyError(f"Expected RemoteImage in PROXIED mode, got {type(image).__name__}")
        if not isinstance(app, RemoteApp):
            raise ModalProxyError(f"Expected RemoteApp in PROXIED mode, got {type(app).__name__}")
        request = SandboxCreateRequest(
            app_name=app.app_name,
            environment_name=app.environment_name,
            image_ref=image.image_ref,
            apt_packages=image.apt_packages,
            timeout=timeout,
            cpu=cpu,
            memory=memory,
            unencrypted_ports=tuple(unencrypted_ports),
            gpu=gpu,
            region=region,
            cidr_allowlist=tuple(cidr_allowlist) if cidr_allowlist is not None else None,
        )
        raw = self._transport.request(
            "POST", "/modal/sandboxes", json_body=request.model_dump(mode="json")
        )
        response = SandboxCreateResponse.model_validate(raw)
        return self._wrap_sandbox(response.sandbox_id)

    def sandbox_list(self, *, app_id: str) -> list[SandboxInterface]:
        raw = self._transport.request("GET", "/modal/sandboxes", params={"app_id": app_id})
        response = SandboxListResponse.model_validate(raw)
        return [self._wrap_sandbox(info.sandbox_id) for info in response.sandboxes]

    def sandbox_from_id(self, sandbox_id: str) -> SandboxInterface:
        return self._wrap_sandbox(sandbox_id)

    def _wrap_sandbox(self, sandbox_id: str) -> RemoteSandbox:
        sandbox = RemoteSandbox(sandbox_id=sandbox_id)
        sandbox._transport = self._transport
        return sandbox

    # Volume -- not supported in PROXIED (testing sandboxes are non-persistent).
    def volume_from_name(
        self,
        name: str,
        *,
        create_if_missing: bool = True,
        environment_name: str,
        version: int | None = None,
    ) -> VolumeInterface:
        _raise_for_unsupported("Volumes")
        raise AssertionError("unreachable")

    def volume_list(self, *, environment_name: str) -> list[VolumeInterface]:
        _raise_for_unsupported("Volumes")
        raise AssertionError("unreachable")

    def volume_delete(self, name: str, *, environment_name: str) -> None:
        _raise_for_unsupported("Volumes")

    # Secret / Function / CLI -- only needed for image builds & deploys.
    def secret_from_dict(self, values: Mapping[str, str | None]) -> SecretInterface:
        _raise_for_unsupported("Secrets")
        raise AssertionError("unreachable")

    def function_from_name(
        self,
        name: str,
        *,
        app_name: str,
        environment_name: str | None = None,
    ) -> FunctionInterface:
        _raise_for_unsupported("Function lookup")
        raise AssertionError("unreachable")

    def deploy(
        self,
        script_path: Path,
        *,
        app_name: str,
        environment_name: str | None = None,
    ) -> None:
        _raise_for_unsupported("Deploying Modal apps")

    def enable_output_capture(
        self, is_logging_to_loguru: bool = True
    ) -> AbstractContextManager[tuple[StringIO, "ModalLoguruWriter | None"]]:
        # Nothing to capture client-side: Modal SDK output happens on the connector.
        return nullcontext((StringIO(), None))

"""End-to-end ("sample run") test for the PROXIED Modal transport.

Wires a real ``RemoteModalInterface`` to an in-process fake connector via
``httpx.MockTransport``. The fake connector handler mirrors the real connector
routes (``apps/remote_service_connector``) but is backed by a
``FakeModalInterface`` instead of the raw Modal SDK, so the whole
client -> wire-protocol -> connector-logic path runs without a network, Modal
credentials, or a deploy. This exercises the same JSON contract the deployed
connector speaks.
"""

import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.modal_proxy.data_types import SandboxCreateRequest
from imbue.modal_proxy.data_types import SandboxCreateResponse
from imbue.modal_proxy.data_types import SandboxExecRequest
from imbue.modal_proxy.data_types import SandboxExecResponse
from imbue.modal_proxy.data_types import SandboxInfo
from imbue.modal_proxy.data_types import SandboxListResponse
from imbue.modal_proxy.data_types import SandboxTagsRequest
from imbue.modal_proxy.data_types import SandboxTunnelsResponse
from imbue.modal_proxy.data_types import StreamType
from imbue.modal_proxy.data_types import TunnelInfo
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.remote import RemoteModalInterface
from imbue.modal_proxy.remote import _is_background_command
from imbue.modal_proxy.testing import FakeModalInterface

_EXPECTED_BEARER = "test-token-123"


def _make_fake_connector(fake: FakeModalInterface) -> httpx.MockTransport:
    """An httpx transport that answers the connector's ``/modal`` routes via ``fake``.

    The handler logic intentionally mirrors the real connector handlers in
    ``apps/remote_service_connector/imbue/remote_service_connector/app.py`` so
    that this test validates the exact wire contract the deployed connector uses.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # Every route is authenticated with the bearer token from the provider.
        assert request.headers.get("Authorization") == f"Bearer {_EXPECTED_BEARER}"
        parts = request.url.path.strip("/").split("/")
        # parts: ["modal", "sandboxes", <sandbox_id?>, <subresource?>]
        body = json.loads(request.content) if request.content else {}

        if request.method == "POST" and parts == ["modal", "sandboxes"]:
            req = SandboxCreateRequest.model_validate(body)
            app = fake.app_lookup(req.app_name, environment_name=req.environment_name)
            if req.image_ref is not None:
                image = fake.image_from_registry(req.image_ref)
            else:
                image = fake.image_debian_slim()
                if req.apt_packages:
                    image = image.apt_install(*req.apt_packages)
            sandbox = fake.sandbox_create(
                image=image,
                app=app,
                timeout=req.timeout,
                cpu=req.cpu,
                memory=req.memory,
                unencrypted_ports=req.unencrypted_ports,
                gpu=req.gpu,
                region=req.region,
                cidr_allowlist=req.cidr_allowlist,
            )
            return httpx.Response(200, json=SandboxCreateResponse(sandbox_id=sandbox.get_object_id()).model_dump())

        if request.method == "GET" and parts == ["modal", "sandboxes"]:
            sandboxes = [
                SandboxInfo(sandbox_id=sb.get_object_id(), tags=sb.get_tags())
                for sb in fake.sandbox_list(app_id=request.url.params.get("app_id", ""))
            ]
            return httpx.Response(200, json=SandboxListResponse(sandboxes=tuple(sandboxes)).model_dump())

        sandbox_id = parts[2]
        subresource = parts[3] if len(parts) > 3 else None

        if request.method == "POST" and subresource == "exec":
            exec_req = SandboxExecRequest.model_validate(body)
            sandbox = fake.sandbox_from_id(sandbox_id)
            if exec_req.is_background:
                # Mirror the connector: start detached, return immediately.
                stream = StreamType.DEVNULL if not exec_req.is_stdout_captured else StreamType.PIPE
                sandbox.exec(*exec_req.args, stdout=stream)
                return httpx.Response(200, json=SandboxExecResponse(exit_code=0, stdout="").model_dump())
            process = sandbox.exec(*exec_req.args)
            stdout = process.get_stdout().read() if exec_req.is_stdout_captured else ""
            return httpx.Response(
                200, json=SandboxExecResponse(exit_code=process.wait(), stdout=stdout).model_dump()
            )

        if request.method == "GET" and subresource == "tunnels":
            sandbox = fake.sandbox_from_id(sandbox_id)
            raw = sandbox.tunnels(timeout=int(request.url.params.get("timeout", "50")))
            tunnels = {str(port): info for port, info in raw.items()}
            return httpx.Response(200, json=SandboxTunnelsResponse(tunnels=tunnels).model_dump())

        if request.method == "GET" and subresource == "tags":
            sandbox = fake.sandbox_from_id(sandbox_id)
            return httpx.Response(200, json=SandboxTagsRequest(tags=sandbox.get_tags()).model_dump())

        if request.method == "PUT" and subresource == "tags":
            tags_req = SandboxTagsRequest.model_validate(body)
            sandbox = fake.sandbox_from_id(sandbox_id)
            sandbox.set_tags(tags_req.tags)
            return httpx.Response(200, json={"status": "ok"})

        if request.method == "DELETE" and subresource is None:
            sandbox = fake.sandbox_from_id(sandbox_id)
            sandbox.terminate()
            return httpx.Response(200, json={"status": "terminated"})

        return httpx.Response(404, json={"detail": f"no route for {request.method} {request.url.path}"})

    return httpx.MockTransport(handler)


def _make_interface(fake: FakeModalInterface) -> RemoteModalInterface:
    client = httpx.Client(transport=_make_fake_connector(fake))
    return RemoteModalInterface(
        base_url="http://connector.test",
        environment="test-env",
        token_provider=lambda: SecretStr(_EXPECTED_BEARER),
        http_client=client,
    )


def test_proxied_sandbox_lifecycle_sample_run(tmp_path: Path) -> None:
    """Full create -> exec -> tunnels -> tags -> list -> terminate over the proxy."""
    root = tmp_path / "modal_root"
    root.mkdir()
    with ConcurrencyGroup(name="proxied-modal-test") as cg:
        fake = FakeModalInterface(root_dir=root, concurrency_group=cg)
        iface = _make_interface(fake)

        # Create (app + image resolved deferred; connector builds the real ones).
        app = iface.app_lookup("mngr-test-app", environment_name="test-env")
        image = iface.image_debian_slim()
        sandbox = iface.sandbox_create(
            image=image, app=app, timeout=900, cpu=1.0, memory=512, unencrypted_ports=[22]
        )
        assert sandbox.get_object_id().startswith("sb-")

        # Foreground exec returns real stdout + exit code over the wire.
        process = sandbox.exec("sh", "-c", "echo hello-from-sandbox")
        assert "hello-from-sandbox" in process.get_stdout().read()
        assert process.wait() == 0

        # A background command (the sshd -D pattern) returns immediately with exit 0.
        bg = sandbox.exec("/usr/sbin/sshd", "-D", stdout=StreamType.DEVNULL)
        assert bg.wait() == 0

        # Tunnels round-trip (string JSON keys -> int ports; tuple tcp_socket).
        tunnels = sandbox.tunnels(timeout=5)
        assert tunnels[22] == TunnelInfo(tcp_socket=("127.0.0.1", 22222))

        # Tags set/get round-trip.
        sandbox.set_tags({"mngr-host-id": "host-1", "mngr-host-name": "demo"})
        assert sandbox.get_tags() == {"mngr-host-id": "host-1", "mngr-host-name": "demo"}

        # List finds the sandbox.
        listed = iface.sandbox_list(app_id="mngr-test-app")
        assert sandbox.get_object_id() in [sb.get_object_id() for sb in listed]

        # Terminate removes it from the list.
        sandbox.terminate()
        assert iface.sandbox_list(app_id="mngr-test-app") == []

        fake.cleanup()


def test_background_command_detection() -> None:
    """The client classifies long-running commands as background (must not block)."""
    assert _is_background_command(("/usr/sbin/sshd", "-D", "-E", "/log"), StreamType.DEVNULL) is True
    assert _is_background_command(("nohup", "something"), StreamType.PIPE) is True
    assert _is_background_command(("sleep", "30", "&"), StreamType.PIPE) is True
    assert _is_background_command(("sh", "-c", "echo hi"), StreamType.PIPE) is False
    assert _is_background_command((), StreamType.PIPE) is False


def test_proxied_unsupported_operations_raise(tmp_path: Path) -> None:
    """Persistent-host features are rejected with a clear error in PROXIED mode."""
    root = tmp_path / "modal_root2"
    root.mkdir()
    with ConcurrencyGroup(name="proxied-modal-test") as cg:
        fake = FakeModalInterface(root_dir=root, concurrency_group=cg)
        iface = _make_interface(fake)

        with pytest.raises(ModalProxyError):
            iface.volume_from_name("v", environment_name="test-env")
        with pytest.raises(ModalProxyError):
            iface.image_from_id("im-123")
        with pytest.raises(ModalProxyError):
            iface.deploy(Path("/tmp/x.py"), app_name="a")

        image = iface.image_debian_slim()
        with pytest.raises(ModalProxyError):
            image.dockerfile_commands(["RUN echo hi"])

        app = iface.app_lookup("a", environment_name="test-env")
        sandbox = iface.sandbox_create(image=image, app=app, timeout=60, cpu=1.0, memory=256)
        with pytest.raises(ModalProxyError):
            sandbox.snapshot_filesystem()
        fake.cleanup()

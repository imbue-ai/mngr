import json
import os
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_latchkey.devices import DEVICES_DIR_ENV_VAR
from imbue.mngr_latchkey.devices import DEVICE_ANNOUNCEMENT_INTERVAL_ENV_VAR
from imbue.mngr_latchkey.devices import DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS
from imbue.mngr_latchkey.devices import DEVICE_HEADER
from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.devices import DeviceRecord
from imbue.mngr_latchkey.devices import MULTIPLE_DESKTOPS_MATCHED_HEADER
from imbue.mngr_latchkey.devices import device_record_filename
from imbue.mngr_latchkey.testing import http_request
from imbue.mngr_latchkey.testing import http_request_with_headers
from imbue.mngr_latchkey.testing import node_extension_gateway

_EXTENSION_PATH: Final[Path] = Path(__file__).resolve().parent / "desktop_gateway_proxy.mjs"

_PASSWORD_HEADER: Final[str] = "X-Latchkey-Gateway-Password"
_OVERRIDE_HEADER: Final[str] = "X-Latchkey-Gateway-Permissions-Override"


class _RecordedRequest(FrozenModel):
    """One request a fake desktop gateway received."""

    method: str = Field(description="The HTTP method")
    path: str = Field(description="The path and query string")
    headers: dict[str, str] = Field(description="The headers, lower-cased")
    body: bytes = Field(description="The request body")


class _RecordingHandler(BaseHTTPRequestHandler):
    """HTTP handler standing in for a desktop gateway: records requests and echoes its name and the path."""

    def _handle(self) -> None:
        server = self.server
        assert isinstance(server, _RecordingServer)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        server.received.append(
            _RecordedRequest(
                method=self.command,
                path=self.path,
                headers={name.lower(): value for name, value in self.headers.items()},
                body=body,
            )
        )
        response = json.dumps({"served_by": server.label, "path": self.path, "body": body.decode("utf-8")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _RecordingServer(ThreadingHTTPServer):
    """Threading HTTP server carrying the requests its handler recorded, and a label to echo."""

    received: list[_RecordedRequest]
    label: str


@contextmanager
def _desktop_gateway(label: str) -> Generator[tuple[int, _RecordingServer], None, None]:
    """A fake desktop gateway on a loopback port, as a desktop's tunnel would expose it on a machine."""
    server = _RecordingServer(("127.0.0.1", 0), _RecordingHandler)
    server.received = []
    server.label = label
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1]), server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def _machine_env(devices_dir: Path) -> dict[str, str]:
    return {
        DEVICES_DIR_ENV_VAR: str(devices_dir),
        DEVICE_ANNOUNCEMENT_INTERVAL_ENV_VAR: str(DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS),
    }


def _announce(devices_dir: Path, device_id: str, port: int, seconds_ago: float = 0.0) -> DeviceRecord:
    """Leave the record a desktop's announcement leaves, its age as ``seconds_ago`` says."""
    record = DeviceRecord(
        device_id=DesktopDeviceId(device_id),
        hostname=f"{device_id}.example",
        port=port,
        gateway_password=f"password-of-{device_id}",
        permissions_override=f"jwt-of-{device_id}",
    )
    path = devices_dir / device_record_filename(record.device_id)
    path.write_text(record.model_dump_json())
    announced_at = time.time() - seconds_ago
    os.utime(path, (announced_at, announced_at))
    return record


def _caller_headers(desktop: str | None = None) -> dict[str, str]:
    headers = {_PASSWORD_HEADER: "the-machines-own-password", _OVERRIDE_HEADER: "a-jwt-of-the-callers-choosing"}
    if desktop is not None:
        headers[DEVICE_HEADER] = desktop
    return headers


def test_devices_lists_every_announced_desktop_most_recently_heard_from_first(tmp_path: Path) -> None:
    """The listing reports when each desktop was last heard from and how often one checks in, and judges neither."""
    _announce(tmp_path, "desktop-old", 41001, seconds_ago=3600)
    _announce(tmp_path, "desktop-a", 41002, seconds_ago=30)
    _announce(tmp_path, "desktop-b", 41003, seconds_ago=5)
    with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
        status, body = http_request(f"{gateway_url}/devices")

    assert status == 200
    listing = json.loads(body)
    devices = listing["devices"]
    assert [(device["device_id"], device["hostname"]) for device in devices] == [
        ("desktop-b", "desktop-b.example"),
        ("desktop-a", "desktop-a.example"),
        ("desktop-old", "desktop-old.example"),
    ]
    assert all(device["last_seen_at"].endswith("Z") for device in devices)
    assert listing["announcement_interval_seconds"] == DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS
    # Nothing a desktop announced beyond its identity is listed.
    assert all(set(device) == {"device_id", "hostname", "last_seen_at"} for device in devices)


def test_a_request_naming_no_desktop_goes_to_the_most_recently_announced_one(tmp_path: Path) -> None:
    """What every workspace built before the header did keeps working, against whichever desktop is here now."""
    with _desktop_gateway("older") as (older_port, older), _desktop_gateway("newer") as (newer_port, newer):
        _announce(tmp_path, "desktop-older", older_port, seconds_ago=40)
        _announce(tmp_path, "desktop-newer", newer_port, seconds_ago=2)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            status, body = http_request(
                f"{gateway_url}/permission-requests/approve/abc?follow=true&x=1",
                method="POST",
                headers={**_caller_headers(), "Authorization": "Bearer original"},
                body=b'{"account": "me"}',
            )

    assert status == 200
    assert json.loads(body) == {
        "served_by": "newer",
        "path": "/permission-requests/approve/abc?follow=true&x=1",
        "body": '{"account": "me"}',
    }
    assert older.received == []
    (received,) = newer.received
    assert received.method == "POST"
    # The caller's own gateway credentials are dropped in favor of the
    # desktop's, which its announcement carried; everything else survives.
    assert received.headers["x-latchkey-gateway-password"] == "password-of-desktop-newer"
    assert received.headers["x-latchkey-gateway-permissions-override"] == "jwt-of-desktop-newer"
    assert received.headers["authorization"] == "Bearer original"
    assert "x-latchkey-device" not in received.headers


def test_a_request_naming_one_desktop_goes_there_and_nowhere_else(tmp_path: Path) -> None:
    with _desktop_gateway("a") as (port_a, server_a), _desktop_gateway("b") as (port_b, server_b):
        _announce(tmp_path, "desktop-a", port_a, seconds_ago=1)
        _announce(tmp_path, "desktop-b", port_b, seconds_ago=20)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            status, body = http_request(f"{gateway_url}/permissions/self", headers=_caller_headers("desktop-b"))

    assert status == 200
    assert json.loads(body)["served_by"] == "b"
    assert server_a.received == []
    assert server_b.received[0].headers["x-latchkey-gateway-password"] == "password-of-desktop-b"
    assert "x-latchkey-device" not in server_b.received[0].headers


def test_a_desktop_is_tried_however_long_ago_it_announced_itself(tmp_path: Path) -> None:
    """A record's age is the caller's to judge; the gateway refuses only a desktop it has never heard from.

    A desktop that is gone fails on its tunnel (the machine's sshd retires the
    listener of a dead session), which is what makes the attempt cheap.
    """
    _announce(tmp_path, "desktop-gone", 1, seconds_ago=3600)
    with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
        unknown_status, unknown_body = http_request(
            f"{gateway_url}/permissions/self", headers=_caller_headers("desktop-nobody")
        )
        gone_status, gone_body = http_request(
            f"{gateway_url}/permissions/self", headers=_caller_headers("desktop-gone")
        )
        default_status, default_body = http_request(f"{gateway_url}/permissions/self", headers=_caller_headers())

    assert (unknown_status, json.loads(unknown_body)["error"]) == (
        503,
        "Desktop desktop-nobody is not known to this gateway.",
    )
    assert gone_status == 502
    assert json.loads(gone_body)["error"].startswith("Desktop desktop-gone (desktop-gone.example) is unreachable: ")
    # The most recently announced desktop is the one that is gone, so the
    # default route tries it too.
    assert default_status == 502


def test_a_machine_no_desktop_has_announced_itself_to_says_so(tmp_path: Path) -> None:
    with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
        status, body = http_request(f"{gateway_url}/minds-api-proxy/api/schema", headers=_caller_headers())

    assert (status, json.loads(body)["error"]) == (503, "No desktop has announced itself to this machine.")


def test_a_request_for_every_desktop_reaches_each_one_and_answers_with_all_responses(tmp_path: Path) -> None:
    with _desktop_gateway("a") as (port_a, server_a), _desktop_gateway("b") as (port_b, server_b):
        _announce(tmp_path, "desktop-a", port_a, seconds_ago=1)
        _announce(tmp_path, "desktop-b", port_b, seconds_ago=2)
        _announce(tmp_path, "desktop-gone", 1, seconds_ago=3600)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            status, response_headers, body = http_request_with_headers(
                f"{gateway_url}/permission-requests",
                method="POST",
                headers=_caller_headers("*"),
                body=b'{"rationale": "please"}',
            )

    assert status == 200
    assert response_headers[MULTIPLE_DESKTOPS_MATCHED_HEADER.lower()] == "true"
    responses = json.loads(body)["responses"]
    assert [(entry["device_id"], entry["hostname"], entry["status"]) for entry in responses] == [
        ("desktop-a", "desktop-a.example", 200),
        ("desktop-b", "desktop-b.example", 200),
        ("desktop-gone", "desktop-gone.example", 502),
    ]
    assert [json.loads(entry["body"])["served_by"] for entry in responses[:2]] == ["a", "b"]
    assert all(entry["content_type"] == "application/json" for entry in responses[:2])
    assert responses[2]["error"].startswith("Desktop is unreachable: ")
    # The body went to each desktop whole, with that desktop's own credentials.
    assert server_a.received[0].body == b'{"rationale": "please"}'
    assert server_b.received[0].body == b'{"rationale": "please"}'
    assert server_a.received[0].headers["x-latchkey-gateway-password"] == "password-of-desktop-a"
    assert server_b.received[0].headers["x-latchkey-gateway-password"] == "password-of-desktop-b"


def test_a_request_for_a_list_of_desktops_answers_for_each_known_one(tmp_path: Path) -> None:
    """A desktop the gateway does not know is left out; one that cannot be reached is an entry among the others."""
    with _desktop_gateway("a") as (port_a, _server_a):
        _announce(tmp_path, "desktop-a", port_a, seconds_ago=1)
        # Announced, but nothing listens on its port anymore.
        _announce(tmp_path, "desktop-dead", 1, seconds_ago=1)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            status, response_headers, body = http_request_with_headers(
                f"{gateway_url}/permissions/self",
                headers=_caller_headers("desktop-dead, desktop-nobody,desktop-a"),
            )

    assert status == 200
    assert response_headers[MULTIPLE_DESKTOPS_MATCHED_HEADER.lower()] == "true"
    responses = json.loads(body)["responses"]
    assert [(entry["device_id"], entry["hostname"], entry["status"]) for entry in responses] == [
        ("desktop-dead", "desktop-dead.example", 502),
        ("desktop-a", "desktop-a.example", 200),
    ]
    assert responses[0]["error"].startswith("Desktop is unreachable: ")
    assert json.loads(responses[1]["body"])["served_by"] == "a"


def test_a_plural_request_that_comes_down_to_one_desktop_answers_with_its_response_alone(tmp_path: Path) -> None:
    """With one desktop in play, ``*`` or a list answers exactly as a local workspace's gateway does: unwrapped."""
    with _desktop_gateway("a") as (port_a, server_a):
        _announce(tmp_path, "desktop-a", port_a)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            all_status, all_headers, all_body = http_request_with_headers(
                f"{gateway_url}/permission-requests",
                method="POST",
                headers=_caller_headers("*"),
                body=b'{"rationale": "please"}',
            )
            listed_status, listed_headers, listed_body = http_request_with_headers(
                f"{gateway_url}/permissions/self", headers=_caller_headers("desktop-nobody,desktop-a")
            )

    assert (all_status, json.loads(all_body)) == (
        200,
        {"served_by": "a", "path": "/permission-requests", "body": '{"rationale": "please"}'},
    )
    assert (listed_status, json.loads(listed_body)["served_by"]) == (200, "a")
    assert MULTIPLE_DESKTOPS_MATCHED_HEADER.lower() not in all_headers
    assert MULTIPLE_DESKTOPS_MATCHED_HEADER.lower() not in listed_headers
    assert [received.headers["x-latchkey-gateway-password"] for received in server_a.received] == [
        "password-of-desktop-a",
        "password-of-desktop-a",
    ]
    assert all("x-latchkey-device" not in received.headers for received in server_a.received)


def test_a_plural_request_that_comes_down_to_one_unreachable_desktop_answers_502_by_name(tmp_path: Path) -> None:
    _announce(tmp_path, "desktop-gone", 1, seconds_ago=3600)
    with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
        status, response_headers, body = http_request_with_headers(
            f"{gateway_url}/permissions/self", headers=_caller_headers("*")
        )

    assert status == 502
    assert MULTIPLE_DESKTOPS_MATCHED_HEADER.lower() not in response_headers
    assert json.loads(body)["error"].startswith("Desktop desktop-gone (desktop-gone.example) is unreachable: ")


def test_a_plural_request_that_comes_down_to_no_desktop_answers_503(tmp_path: Path) -> None:
    """Not a 502: no desktop was picked, so none failed to answer; it is the same as no desktop being connected."""
    with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
        all_status, all_headers, all_body = http_request_with_headers(
            f"{gateway_url}/permissions/self", headers=_caller_headers("*")
        )
    with _desktop_gateway("a") as (port_a, server_a):
        _announce(tmp_path, "desktop-a", port_a)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            listed_status, listed_headers, listed_body = http_request_with_headers(
                f"{gateway_url}/permissions/self", headers=_caller_headers("desktop-nobody,desktop-else")
            )

    assert (all_status, json.loads(all_body)["error"]) == (503, "No desktop has announced itself to this machine.")
    assert (listed_status, json.loads(listed_body)["error"]) == (
        503,
        f"None of the desktops {DEVICE_HEADER} names (desktop-nobody, desktop-else) is known to this gateway.",
    )
    assert MULTIPLE_DESKTOPS_MATCHED_HEADER.lower() not in all_headers
    assert MULTIPLE_DESKTOPS_MATCHED_HEADER.lower() not in listed_headers
    assert server_a.received == []


def test_a_device_header_mixing_all_with_names_is_refused(tmp_path: Path) -> None:
    with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
        status, body = http_request(f"{gateway_url}/permissions/self", headers=_caller_headers("*, desktop-a"))

    assert status == 400
    assert DEVICE_HEADER in json.loads(body)["error"]


def test_a_record_that_is_not_a_desktops_announcement_is_ignored(tmp_path: Path) -> None:
    """A malformed or foreign file under the records must not take every request on the machine down."""
    (tmp_path / "desktop-broken.json").write_text("{not json")
    (tmp_path / "desktop-thin.json").write_text(json.dumps({"device_id": "desktop-thin", "port": 41020}))
    (tmp_path / "notes.txt").write_text("nothing to do with desktops")
    with _desktop_gateway("a") as (port_a, _server_a):
        _announce(tmp_path, "desktop-a", port_a)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            status, body = http_request(f"{gateway_url}/devices")

    assert status == 200
    assert [device["device_id"] for device in json.loads(body)["devices"]] == ["desktop-a"]


def test_an_unreachable_desktop_answers_502_by_name(tmp_path: Path) -> None:
    with _desktop_gateway("a") as (port_a, _server_a):
        pass
    _announce(tmp_path, "desktop-a", port_a)
    with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
        status, body = http_request(f"{gateway_url}/minds-api-proxy/api/schema", headers=_caller_headers())

    assert status == 502
    assert json.loads(body)["error"].startswith("Desktop desktop-a (desktop-a.example) is unreachable: ")


def test_only_the_desktop_owned_route_families_and_devices_are_taken(tmp_path: Path) -> None:
    with _desktop_gateway("a") as (port_a, server_a):
        _announce(tmp_path, "desktop-a", port_a)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            taken_paths = (
                "/permissions",
                "/permissions/self",
                "/permission-requests",
                "/permission-requests/abc",
                "/minds-api-proxy",
                "/minds-api-proxy/api/v1/timezone",
            )
            for path in taken_paths:
                assert json.loads(http_request(f"{gateway_url}{path}")[1])["served_by"] == "a"
            for path in ("/permission", "/permission-requests-extra", "/devices/desktop-a", "/gateway/https://x"):
                assert json.loads(http_request(f"{gateway_url}{path}")[1])["served_locally"] is True
            assert http_request(f"{gateway_url}/devices", method="POST")[0] == 405

    assert [received.path for received in server_a.received] == list(taken_paths)


def test_a_gateway_told_of_no_desktops_says_so() -> None:
    with node_extension_gateway(_EXTENSION_PATH, {}) as gateway_url:
        status, body = http_request(f"{gateway_url}/permissions/self", headers=_caller_headers())
        devices_status, devices_body = http_request(f"{gateway_url}/devices")

    assert status == 503
    assert DEVICES_DIR_ENV_VAR in json.loads(body)["error"]
    assert devices_status == 503
    assert DEVICES_DIR_ENV_VAR in json.loads(devices_body)["error"]

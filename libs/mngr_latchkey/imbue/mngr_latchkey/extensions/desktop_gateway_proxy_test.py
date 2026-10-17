import json
import os
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
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
# The desktop's own extension, for the one test that puts real desktops behind the machine.
_DESKTOP_EXTENSION_PATH: Final[Path] = Path(__file__).resolve().parent / "permission_requests.mjs"

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
        self.send_response(server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _RecordingServer(ThreadingHTTPServer):
    """Threading HTTP server carrying the requests its handler recorded, a label to echo and the status it answers."""

    received: list[_RecordedRequest]
    label: str
    status: int


@contextmanager
def _desktop_gateway(label: str, status: int = 200) -> Generator[tuple[int, _RecordingServer], None, None]:
    """A fake desktop gateway on a loopback port, as a desktop's tunnel would expose it on a machine."""
    server = _RecordingServer(("127.0.0.1", 0), _RecordingHandler)
    server.received = []
    server.label = label
    server.status = status
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
        "LATCHKEY_DIRECTORY": str(_latchkey_dir(devices_dir)),
    }


def _latchkey_dir(devices_dir: Path) -> Path:
    """The machine's latchkey directory, beside the device records (which are the ``.json`` files, not this)."""
    return devices_dir / "latchkey"


def _filed_requests(devices_dir: Path) -> dict[str, dict[str, Any]]:
    """The requests the machine kept, by request id."""
    directory = _latchkey_dir(devices_dir) / "filed_permission_requests" / "v1"
    if not directory.is_dir():
        return {}
    return {path.stem: json.loads(path.read_text()) for path in sorted(directory.iterdir())}


def _file_request(devices_dir: Path, request_id: str) -> Path:
    """Leave the machine holding a filed request, as an earlier filing would have."""
    directory = _latchkey_dir(devices_dir) / "filed_permission_requests" / "v1"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{request_id}.json"
    path.write_text(
        json.dumps({"request_id": request_id, "devices": "*", "created_at": "2026-01-01T00:00:00.000Z", "body": {}})
    )
    return path


_FILED_BODY: Final[dict[str, Any]] = {
    "agent_id": "agent-" + "0" * 32,
    "rationale": "please",
    "type": "accounts",
    "payload": {},
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


def _assert_opens_with_the_filing(
    answer: dict[str, Any],
    request_id: str,
    request_type: str | None,
    rationale: str | None,
    payload: dict[str, Any] | None,
) -> None:
    """Every answer the machine composes for a filed request opens with what was filed, and under which id."""
    expected = [
        ("request_id", request_id),
        ("request_type", request_type),
        ("rationale", rationale),
        ("payload", payload),
    ]
    assert list(answer.items())[: len(expected)] == expected


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
    # The body went to each desktop whole (a filed request gaining its id, see
    # below), with that desktop's own credentials.
    assert json.loads(server_a.received[0].body)["rationale"] == "please"
    assert json.loads(server_b.received[0].body)["rationale"] == "please"
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

    answered = json.loads(all_body)
    assert (all_status, answered["served_by"], answered["path"]) == (200, "a", "/permission-requests")
    # The one change to a forwarded body: a filed request carries the id the
    # machine gave it.
    assert json.loads(answered["body"]) == {
        "rationale": "please",
        "request_id": _filed_requests(tmp_path).popitem()[0],
    }
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


def test_a_filed_request_gets_one_id_for_every_desktop_and_is_kept_on_the_machine(tmp_path: Path) -> None:
    """Every desktop files the same request under the machine's id, and the machine keeps what the agent sent."""
    with (
        _desktop_gateway("a", status=201) as (port_a, server_a),
        _desktop_gateway("b", status=201) as (port_b, server_b),
    ):
        _announce(tmp_path, "desktop-a", port_a, seconds_ago=1)
        _announce(tmp_path, "desktop-b", port_b, seconds_ago=2)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            status, response_headers, body = http_request_with_headers(
                f"{gateway_url}/permission-requests",
                method="POST",
                headers={**_caller_headers("*"), "Content-Type": "application/json"},
                body=json.dumps(_FILED_BODY).encode(),
            )

    assert status == 200
    assert response_headers[MULTIPLE_DESKTOPS_MATCHED_HEADER.lower()] == "true"
    sent_to_a = json.loads(server_a.received[0].body)
    sent_to_b = json.loads(server_b.received[0].body)
    request_id = sent_to_a["request_id"]
    assert sent_to_b["request_id"] == request_id
    assert {key: value for key, value in sent_to_a.items() if key != "request_id"} == _FILED_BODY
    # The body was rewritten, so its length was too.
    assert server_a.received[0].headers["content-length"] == str(len(server_a.received[0].body))
    answer = json.loads(body)
    assert [(entry["device_id"], entry["status"]) for entry in answer["responses"]] == [
        ("desktop-a", 201),
        ("desktop-b", 201),
    ]
    # What was filed, and under which id, leads the answer: a parser that
    # reads only the start of the agent's output still finds it.
    _assert_opens_with_the_filing(
        answer, request_id, _FILED_BODY["type"], _FILED_BODY["rationale"], _FILED_BODY["payload"]
    )
    (kept,) = _filed_requests(tmp_path).values()
    assert kept["request_id"] == request_id
    assert kept["devices"] == "*"
    assert kept["body"] == _FILED_BODY
    assert kept["created_at"].endswith("Z")


def test_a_filed_request_one_desktop_took_is_answered_as_that_desktop_answered(tmp_path: Path) -> None:
    """To the agent a request that reached one desktop reads exactly as if the desktop's gateway were its own."""
    with _desktop_gateway("only", status=201) as (port, server):
        _announce(tmp_path, "desktop-only", port)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            status, response_headers, body = http_request_with_headers(
                f"{gateway_url}/permission-requests",
                method="POST",
                headers=_caller_headers(),
                body=json.dumps(_FILED_BODY).encode(),
            )

    assert status == 201
    assert MULTIPLE_DESKTOPS_MATCHED_HEADER.lower() not in response_headers
    # The desktop's own answer, byte for byte: nothing prepended.
    assert json.loads(body) == {
        "served_by": "only",
        "path": "/permission-requests",
        "body": server.received[0].body.decode(),
    }
    request_id = json.loads(server.received[0].body)["request_id"]
    # A request that named no desktop went to the one desktop there was, and is
    # kept for that desktop alone: it is what every agent from before the header
    # expects, not a broadcast.
    assert _filed_requests(tmp_path)[request_id]["devices"] == ["desktop-only"]


def test_a_filed_request_every_desktop_refused_is_not_kept(tmp_path: Path) -> None:
    """The desktops are the judges of a request: one they all turned down has no desktop left to show it."""
    with _desktop_gateway("a", status=400) as (port_a, server_a), _desktop_gateway("b", status=400) as (port_b, _b):
        _announce(tmp_path, "desktop-a", port_a, seconds_ago=1)
        _announce(tmp_path, "desktop-b", port_b, seconds_ago=2)
        _announce(tmp_path, "desktop-gone", 1, seconds_ago=3600)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            status, _response_headers, body = http_request_with_headers(
                f"{gateway_url}/permission-requests",
                method="POST",
                headers=_caller_headers("*"),
                body=json.dumps(_FILED_BODY).encode(),
            )

    assert status == 200
    answer = json.loads(body)
    assert [entry["status"] for entry in answer["responses"]] == [400, 400, 502]
    assert answer["request_id"] == json.loads(server_a.received[0].body)["request_id"]
    assert _filed_requests(tmp_path) == {}


def test_a_filed_request_one_desktop_refused_is_answered_as_that_desktop_answered(tmp_path: Path) -> None:
    """A refusal, like an acceptance, is the one desktop's own answer, relayed untouched: nothing was filed to report."""
    with _desktop_gateway("only", status=400) as (port, server):
        _announce(tmp_path, "desktop-only", port)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            status, _response_headers, body = http_request_with_headers(
                f"{gateway_url}/permission-requests",
                method="POST",
                headers=_caller_headers(),
                body=json.dumps(_FILED_BODY).encode(),
            )

    assert status == 400
    assert json.loads(body) == {
        "served_by": "only",
        "path": "/permission-requests",
        "body": server.received[0].body.decode(),
    }
    assert _filed_requests(tmp_path) == {}


def test_a_filed_request_one_desktop_took_is_kept_whatever_the_others_said(tmp_path: Path) -> None:
    with _desktop_gateway("a", status=201) as (port_a, _server_a), _desktop_gateway("b", status=400) as (port_b, _b):
        _announce(tmp_path, "desktop-a", port_a, seconds_ago=1)
        _announce(tmp_path, "desktop-b", port_b, seconds_ago=2)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            status, _response_headers, _body = http_request_with_headers(
                f"{gateway_url}/permission-requests",
                method="POST",
                headers=_caller_headers("desktop-a,desktop-b,desktop-later"),
                body=json.dumps(_FILED_BODY).encode(),
            )

    assert status == 200
    (kept,) = _filed_requests(tmp_path).values()
    # Kept for every desktop the header named, the one the gateway has never
    # heard from included: it picks the request up when it first connects.
    assert kept["devices"] == ["desktop-a", "desktop-b", "desktop-later"]


def test_a_filed_request_no_desktop_is_there_to_receive_is_kept_and_the_503_says_so(tmp_path: Path) -> None:
    """The agent still gets the 503 every forwarded request gets, but told that the request waits for a desktop."""
    with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
        all_status, all_headers, all_body = http_request_with_headers(
            f"{gateway_url}/permission-requests",
            method="POST",
            headers=_caller_headers("*"),
            body=json.dumps(_FILED_BODY).encode(),
        )
        one_status, _, one_body = http_request_with_headers(
            f"{gateway_url}/permission-requests",
            method="POST",
            headers=_caller_headers("desktop-nobody"),
            body=json.dumps({"agent_id": _FILED_BODY["agent_id"], "rationale": "for one desktop"}).encode(),
        )

    assert all_status == 503
    assert MULTIPLE_DESKTOPS_MATCHED_HEADER.lower() not in all_headers
    all_answer = json.loads(all_body)
    assert all_answer["error"] == (
        "No desktop has announced itself to this machine. "
        "The request was kept on this machine for the desktops to pick up when they next connect."
    )
    assert one_status == 503
    one_answer = json.loads(one_body)
    assert one_answer["error"] == (
        "Desktop desktop-nobody is not known to this gateway. "
        "The request was kept on this machine for the desktops to pick up when they next connect."
    )
    kept = _filed_requests(tmp_path)
    assert {entry["body"]["rationale"]: entry["devices"] for entry in kept.values()} == {
        "please": "*",
        "for one desktop": ["desktop-nobody"],
    }
    assert all(kept_id == entry["request_id"] for kept_id, entry in kept.items())
    kept_id_by_rationale = {entry["body"]["rationale"]: kept_id for kept_id, entry in kept.items()}
    _assert_opens_with_the_filing(
        all_answer, kept_id_by_rationale["please"], _FILED_BODY["type"], "please", _FILED_BODY["payload"]
    )
    assert set(all_answer) == {"request_id", "request_type", "rationale", "payload", "error"}
    _assert_opens_with_the_filing(one_answer, kept_id_by_rationale["for one desktop"], None, "for one desktop", None)


def test_a_filed_request_for_a_desktop_that_cannot_be_reached_is_kept_and_answered_with_its_502(
    tmp_path: Path,
) -> None:
    """The desktop's failure is the agent's answer, as for any forwarded request; the machine keeps the request all the same."""
    _announce(tmp_path, "desktop-gone", 1, seconds_ago=3600)
    with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
        status, _response_headers, body = http_request_with_headers(
            f"{gateway_url}/permission-requests",
            method="POST",
            headers=_caller_headers("desktop-gone"),
            body=json.dumps(_FILED_BODY).encode(),
        )

    assert status == 502
    answer = json.loads(body)
    assert answer["error"].startswith("Desktop desktop-gone (desktop-gone.example) is unreachable: ")
    (kept,) = _filed_requests(tmp_path).values()
    assert kept["devices"] == ["desktop-gone"]
    _assert_opens_with_the_filing(
        answer, kept["request_id"], _FILED_BODY["type"], _FILED_BODY["rationale"], _FILED_BODY["payload"]
    )


def test_a_filed_request_naming_its_own_request_id_is_refused(tmp_path: Path) -> None:
    with _desktop_gateway("a", status=201) as (port_a, server_a):
        _announce(tmp_path, "desktop-a", port_a)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            status, _response_headers, body = http_request_with_headers(
                f"{gateway_url}/permission-requests",
                method="POST",
                headers=_caller_headers("*"),
                body=json.dumps({**_FILED_BODY, "request_id": "mine"}).encode(),
            )

    assert status == 400
    assert json.loads(body)["error"] == (
        "A permission request's request_id is assigned by the gateway; the body must not carry one."
    )
    assert server_a.received == []
    assert _filed_requests(tmp_path) == {}


def test_a_filed_request_that_is_not_a_json_object_is_forwarded_as_sent_and_not_kept(tmp_path: Path) -> None:
    """There is no shape to put an id into, so the desktops answer for it, the machine keeps nothing and reports nothing filed."""
    with (
        _desktop_gateway("a", status=400) as (port_a, server_a),
        _desktop_gateway("b", status=400) as (port_b, server_b),
    ):
        _announce(tmp_path, "desktop-a", port_a, seconds_ago=1)
        _announce(tmp_path, "desktop-b", port_b, seconds_ago=2)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            status, _response_headers, body = http_request_with_headers(
                f"{gateway_url}/permission-requests",
                method="POST",
                headers=_caller_headers("*"),
                body=b"not json at all",
            )

    assert status == 200
    answer = json.loads(body)
    # The answer the machine composes has no request_id to open with: none was assigned.
    assert set(answer) == {"responses"}
    assert [(entry["device_id"], entry["status"]) for entry in answer["responses"]] == [
        ("desktop-a", 400),
        ("desktop-b", 400),
    ]
    assert server_a.received[0].body == server_b.received[0].body == b"not json at all"
    assert _filed_requests(tmp_path) == {}


def test_a_forwarded_delete_drops_the_machines_copy_of_the_request(tmp_path: Path) -> None:
    """A request withdrawn through the machine is forgotten there too, whatever the desktops answer."""
    kept_path = _file_request(tmp_path, "withdrawn-9d1f")
    other_path = _file_request(tmp_path, "still-pending-4c2a")
    with _desktop_gateway("a", status=404) as (port_a, server_a):
        _announce(tmp_path, "desktop-a", port_a)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            status, _response_headers, _body = http_request_with_headers(
                f"{gateway_url}/permission-requests/withdrawn-9d1f", method="DELETE", headers=_caller_headers("*")
            )

    assert status == 404
    assert server_a.received[0].method == "DELETE"
    assert not kept_path.exists()
    assert other_path.exists()


@contextmanager
def _real_desktop_gateway(tmp_path: Path, name: str) -> Generator[tuple[int, Path], None, None]:
    """A desktop's own ``permission-requests`` extension on a loopback port, with its pending directory."""
    desktop_dir = tmp_path / name
    latchkey_directory = desktop_dir / "latchkey"
    latchkey_directory.mkdir(parents=True)
    env = {"LATCHKEY_DIRECTORY": str(latchkey_directory), "HOME": str(desktop_dir), "TMPDIR": str(desktop_dir)}
    with node_extension_gateway(
        _DESKTOP_EXTENSION_PATH, env, permissions_config_path=desktop_dir / "host_permissions.json"
    ) as gateway_url:
        yield int(gateway_url.rsplit(":", 1)[1]), latchkey_directory / "permission_requests" / "v3"


def _pending_ids(pending_dir: Path) -> list[str]:
    return sorted(path.stem for path in pending_dir.iterdir()) if pending_dir.is_dir() else []


def test_two_real_desktops_file_the_same_request_under_the_machines_id_and_both_drop_it_on_a_withdrawal(
    tmp_path: Path,
) -> None:
    """The whole round trip through the desktops' own extension: one id everywhere, one DELETE clears it everywhere."""
    with (
        _real_desktop_gateway(tmp_path, "desktop-a") as (port_a, pending_a),
        _real_desktop_gateway(tmp_path, "desktop-b") as (
            port_b,
            pending_b,
        ),
    ):
        _announce(tmp_path, "desktop-a", port_a, seconds_ago=1)
        _announce(tmp_path, "desktop-b", port_b, seconds_ago=2)
        with node_extension_gateway(_EXTENSION_PATH, _machine_env(tmp_path)) as gateway_url:
            filed_status, _headers, filed_body = http_request_with_headers(
                f"{gateway_url}/permission-requests",
                method="POST",
                headers={**_caller_headers("*"), "Content-Type": "application/json"},
                body=json.dumps(_FILED_BODY).encode(),
            )
            assert filed_status == 200
            responses = json.loads(filed_body)["responses"]
            assert [entry["status"] for entry in responses] == [201, 201]
            filed_ids = {json.loads(entry["body"])["request_id"] for entry in responses}
            (request_id,) = filed_ids
            assert _pending_ids(pending_a) == _pending_ids(pending_b) == [request_id]
            assert list(_filed_requests(tmp_path)) == [request_id]

            withdrawn_status, _headers, withdrawn_body = http_request_with_headers(
                f"{gateway_url}/permission-requests/{request_id}", method="DELETE", headers=_caller_headers("*")
            )

    assert withdrawn_status == 200
    assert [entry["status"] for entry in json.loads(withdrawn_body)["responses"]] == [204, 204]
    assert _pending_ids(pending_a) == _pending_ids(pending_b) == []
    assert _filed_requests(tmp_path) == {}


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

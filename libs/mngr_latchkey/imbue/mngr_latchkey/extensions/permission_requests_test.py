"""End-to-end tests for the ``permission_requests`` gateway extension.

The extension is a Node ESM module. We can't import it from Python, so
this file follows the same pattern as ``minds_api_proxy_test.py``:

1. Spawn a Node child process that loads the extension's default
   export and mounts it on a Node HTTP server. The Node driver also
   passes a synthetic ``ExtensionContext`` (carrying
   ``permissionsConfigPath``) through to the handler so the
   ``/approve`` endpoint can find a target permissions.json to write.
2. Hit the Node server with ``urllib`` and assert on the response,
   plus on the on-disk side effects in the temporary
   ``LATCHKEY_DIRECTORY`` we point the child at.

Tests skip cleanly when Node is unavailable, mirroring the
minds-api-proxy test module.
"""

import hashlib
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from pathlib import Path
from typing import Final

import pytest

_NODE_BINARY: Final[str | None] = shutil.which("node")

_EXTENSION_PATH: Final[Path] = Path(__file__).resolve().parent / "permission_requests.mjs"

_NODE_READY_TIMEOUT_SECONDS: Final[float] = 15.0
_POLL_INTERVAL_SECONDS: Final[float] = 0.02

_FILE_SHARING_SCOPE_SCHEMA_NAME: Final[str] = "minds-file-server"
_FILE_SHARING_PROXY_PATH: Final[str] = "/extensions/minds-api-proxy/api/v1/file-server"
_FILE_SHARING_GATEWAY_HOST: Final[str] = "latchkey-self.invalid"
_FILE_SHARING_PERMISSION_PREFIX: Final[str] = "minds-file-server-"


pytestmark = pytest.mark.skipif(_NODE_BINARY is None, reason="node binary not available on PATH")


def _file_sharing_permission_name(path: str) -> str:
    """Mirror the JS helper: SHA-256(path) truncated to 32 hex chars."""
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:32]
    return f"{_FILE_SHARING_PERMISSION_PREFIX}{digest}"


# The Node driver mounts the extension under a HTTP server, passing a
# synthetic ExtensionContext whose ``permissionsConfigPath`` is read from
# the ``TEST_PERMISSIONS_CONFIG_PATH`` env var. Spawning Node fresh per
# test gives each test its own LATCHKEY_DIRECTORY and target file path
# without any in-memory state leaking between cases.
_NODE_DRIVER_SCRIPT_TEMPLATE: Final[str] = r"""
import http from 'node:http';
import handler from {EXTENSION_PATH_LITERAL};

const targetPath = process.env.TEST_PERMISSIONS_CONFIG_PATH ?? '';
const context = Object.freeze({{ permissionsConfigPath: targetPath }});

const server = http.createServer(async (request, response) => {{
  try {{
    const handled = await handler(request, response, context);
    if (!handled && !response.headersSent) {{
      response.writeHead(404, {{ 'Content-Type': 'application/json' }});
      response.end(JSON.stringify({{ error: 'not handled by extension' }}));
    }}
  }} catch (error) {{
    if (!response.headersSent) {{
      response.writeHead(500, {{ 'Content-Type': 'application/json' }});
      response.end(JSON.stringify({{ error: String(error && error.message) }}));
    }}
  }}
}});

server.listen(0, '127.0.0.1', () => {{
  const address = server.address();
  process.stdout.write('PORT=' + address.port + '\n');
}});

process.on('SIGTERM', () => server.close(() => process.exit(0)));
process.on('SIGINT', () => server.close(() => process.exit(0)));
"""


def _build_node_driver_script() -> str:
    return _NODE_DRIVER_SCRIPT_TEMPLATE.format(
        EXTENSION_PATH_LITERAL=json.dumps(_EXTENSION_PATH.as_uri()),
    )


def _wait_for_node_port(process: subprocess.Popen[str]) -> int:
    deadline = time.monotonic() + _NODE_READY_TIMEOUT_SECONDS
    assert process.stdout is not None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr_tail = ""
            if process.stderr is not None:
                stderr_tail = process.stderr.read() or ""
            raise AssertionError(
                f"node child exited prematurely with code {process.returncode}; stderr={stderr_tail!r}",
            )
        line = process.stdout.readline()
        if not line:
            threading.Event().wait(timeout=_POLL_INTERVAL_SECONDS)
            continue
        line = line.strip()
        if line.startswith("PORT="):
            return int(line.removeprefix("PORT="))
    raise AssertionError(f"node child never printed PORT= within {_NODE_READY_TIMEOUT_SECONDS}s")


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=_POLL_INTERVAL_SECONDS):
                return True
        except OSError:
            threading.Event().wait(timeout=_POLL_INTERVAL_SECONDS)
    return False


@pytest.fixture
def node_extension(tmp_path: Path) -> Generator[tuple[str, Path, Path], None, None]:
    """Spawn the Node driver pointed at a fresh LATCHKEY_DIRECTORY + target path.

    Yields ``(base_url, latchkey_directory, permissions_config_path)`` so
    tests can both hit the HTTP endpoints and inspect the on-disk
    files the extension created.
    """
    assert _NODE_BINARY is not None
    latchkey_directory = tmp_path / "latchkey"
    latchkey_directory.mkdir()
    permissions_config_path = tmp_path / "permissions.json"
    script = _build_node_driver_script()
    process = subprocess.Popen(
        [_NODE_BINARY, "--input-type=module", "-e", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "LATCHKEY_DIRECTORY": str(latchkey_directory),
            "TEST_PERMISSIONS_CONFIG_PATH": str(permissions_config_path),
            "PATH": "/usr/bin:/bin",
        },
        text=True,
    )
    try:
        port = _wait_for_node_port(process)
        base_url = f"http://127.0.0.1:{port}"
        assert _wait_for_port("127.0.0.1", port)
        yield base_url, latchkey_directory, permissions_config_path
    finally:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)


def _http(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return int(resp.status), dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return int(e.code), dict(e.headers or {}), e.read()


def _post_json(url: str, payload: object) -> tuple[int, bytes]:
    status, _, body = _http(
        url,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps(payload).encode("utf-8"),
    )
    return status, body


# -- POST /permission-requests: body validation --


def test_post_creates_predefined_request_with_target_and_effect(
    node_extension: tuple[str, Path, Path],
) -> None:
    base_url, latchkey_directory, permissions_config_path = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": "agent-1",
            "rationale": "needs slack",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
        },
    )
    assert status == 201
    parsed = json.loads(body)
    assert parsed["agent_id"] == "agent-1"
    assert parsed["rationale"] == "needs slack"
    # The persisted/streamed shape renames the wire field ``type`` to
    # ``request_type`` to avoid shadowing the Python ``type`` builtin
    # in the consumer's pydantic model.
    assert parsed["request_type"] == "predefined"
    assert parsed["payload"] == {"scope": "slack-api", "permissions": ["slack-read-all"]}
    assert parsed["target"] == str(permissions_config_path)
    assert parsed["effect"] == {"rules": [{"slack-api": ["slack-read-all"]}]}
    # The persisted file should match the response on disk.
    stored = next((latchkey_directory / "permission_requests" / "v2").iterdir())
    assert json.loads(stored.read_text()) == parsed


def test_post_creates_file_sharing_request_with_schemas_and_rules(
    node_extension: tuple[str, Path, Path],
) -> None:
    base_url, _latchkey_directory, _permissions_config_path = node_extension
    target_path = "/home/example/data.txt"
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": "agent-1",
            "rationale": "needs to read example data",
            "type": "file-sharing",
            "payload": {"path": target_path},
        },
    )
    assert status == 201
    parsed = json.loads(body)
    assert parsed["request_type"] == "file-sharing"
    assert parsed["payload"] == {"path": target_path}
    effect = parsed["effect"]
    permission_name = _file_sharing_permission_name(target_path)
    assert effect["rules"] == [{_FILE_SHARING_SCOPE_SCHEMA_NAME: [permission_name]}]
    schemas = effect["schemas"]
    assert set(schemas.keys()) == {_FILE_SHARING_SCOPE_SCHEMA_NAME, permission_name}
    # The scope schema constrains the gateway-self host + proxy path.
    scope_schema = schemas[_FILE_SHARING_SCOPE_SCHEMA_NAME]
    assert scope_schema["properties"]["domain"] == {"const": _FILE_SHARING_GATEWAY_HOST}
    assert scope_schema["properties"]["path"] == {"const": _FILE_SHARING_PROXY_PATH}
    # The per-path permission schema constrains queryParams.path.
    perm_schema = schemas[permission_name]
    assert perm_schema["properties"]["queryParams"]["properties"]["path"] == {"const": target_path}
    assert perm_schema["properties"]["method"] == {"enum": ["GET", "POST"]}


def test_post_rejects_unknown_type(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": "agent-1",
            "rationale": "x",
            "type": "wholesale",
            "payload": {},
        },
    )
    assert status == 400
    assert "type" in json.loads(body)["error"]


def test_post_rejects_relative_path_in_file_sharing(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": "agent-1",
            "rationale": "x",
            "type": "file-sharing",
            "payload": {"path": "relative/path.txt"},
        },
    )
    assert status == 400
    assert "absolute" in json.loads(body)["error"].lower()


def test_post_rejects_traversal_in_file_sharing(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    for traversal_path in (
        "/home/user/../etc/passwd",
        "/..",
        "/foo/..",
        "/foo/../bar",
        "/foo/bar/..",
    ):
        status, body = _post_json(
            f"{base_url}/permission-requests",
            {
                "agent_id": "agent-1",
                "rationale": "x",
                "type": "file-sharing",
                "payload": {"path": traversal_path},
            },
        )
        assert status == 400, (traversal_path, body)
        message = json.loads(body)["error"].lower()
        assert "traversal" in message or "absolute" in message, (traversal_path, message)


def test_post_rejects_extraneous_top_level_field(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": "agent-1",
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
            "request_id": "spoofed",
        },
    )
    assert status == 400
    assert "request_id" in json.loads(body)["error"]


def test_post_rejects_extraneous_payload_field(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": "agent-1",
            "rationale": "x",
            "type": "file-sharing",
            "payload": {"path": "/tmp/ok.txt", "extra": "no"},
        },
    )
    assert status == 400
    assert "extra" in json.loads(body)["error"]


# -- GET /permission-requests --


def test_get_returns_all_pending_requests(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    payloads = [
        {
            "agent_id": "agent-1",
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
        },
        {
            "agent_id": "agent-2",
            "rationale": "y",
            "type": "file-sharing",
            "payload": {"path": "/tmp/visible.txt"},
        },
    ]
    for payload in payloads:
        status, _ = _post_json(f"{base_url}/permission-requests", payload)
        assert status == 201
    status, _, body = _http(f"{base_url}/permission-requests")
    assert status == 200
    lines = [line for line in body.decode("utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    decoded = [json.loads(line) for line in lines]
    types = {entry["request_type"] for entry in decoded}
    assert types == {"predefined", "file-sharing"}


# -- POST /permission-requests/approve/<id> --


def test_approve_writes_target_permissions_for_file_sharing(
    node_extension: tuple[str, Path, Path],
) -> None:
    base_url, latchkey_directory, permissions_config_path = node_extension
    target_path = "/home/example/data.txt"
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": "agent-1",
            "rationale": "needs to read example data",
            "type": "file-sharing",
            "payload": {"path": target_path},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]

    approve_status, approve_body = _post_json(f"{base_url}/permission-requests/approve/{request_id}", None)
    assert approve_status == 200, approve_body
    response = json.loads(approve_body)
    assert response["request_id"] == request_id
    assert response["target"] == str(permissions_config_path)

    # The on-disk target was written with the effect applied.
    applied = json.loads(permissions_config_path.read_text())
    permission_name = _file_sharing_permission_name(target_path)
    assert applied["rules"] == [{_FILE_SHARING_SCOPE_SCHEMA_NAME: [permission_name]}]
    assert _FILE_SHARING_SCOPE_SCHEMA_NAME in applied["schemas"]
    assert permission_name in applied["schemas"]

    # Pending request file was removed.
    pending_dir = latchkey_directory / "permission_requests" / "v2"
    assert list(pending_dir.iterdir()) == []


def test_approve_merges_predefined_into_existing_rules(
    node_extension: tuple[str, Path, Path],
) -> None:
    base_url, _latchkey_directory, permissions_config_path = node_extension
    # Seed the target with a pre-existing rule for the same scope.
    permissions_config_path.write_text(json.dumps({"rules": [{"slack-api": ["slack-read-all"]}]}))
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": "agent-1",
            "rationale": "wants more slack",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-write-all"]},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    approve_status, _ = _post_json(f"{base_url}/permission-requests/approve/{request_id}", None)
    assert approve_status == 200
    applied = json.loads(permissions_config_path.read_text())
    # Permissions from both the seed and the new effect are unioned in
    # a single rule entry (same scope key).
    assert applied["rules"] == [{"slack-api": ["slack-read-all", "slack-write-all"]}]


def test_approve_creates_target_when_missing(node_extension: tuple[str, Path, Path]) -> None:
    base_url, _latchkey_directory, permissions_config_path = node_extension
    assert not permissions_config_path.exists()
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": "agent-1",
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    approve_status, _ = _post_json(f"{base_url}/permission-requests/approve/{request_id}", None)
    assert approve_status == 200
    assert permissions_config_path.exists()
    applied = json.loads(permissions_config_path.read_text())
    assert applied["rules"] == [{"slack-api": ["slack-read-all"]}]


def test_approve_404s_on_unknown_request_id(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(f"{base_url}/permission-requests/approve/nope", None)
    assert status == 404
    assert "not found" in json.loads(body)["error"].lower()


def test_delete_removes_pending_request(node_extension: tuple[str, Path, Path]) -> None:
    base_url, latchkey_directory, _permissions_config_path = node_extension
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": "agent-1",
            "rationale": "x",
            "type": "file-sharing",
            "payload": {"path": "/tmp/data.txt"},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    status, _, _ = _http(f"{base_url}/permission-requests/{request_id}", method="DELETE")
    assert status == 204
    pending_dir = latchkey_directory / "permission_requests" / "v2"
    assert list(pending_dir.iterdir()) == []

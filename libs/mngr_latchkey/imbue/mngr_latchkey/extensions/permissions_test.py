"""End-to-end tests for the ``permissions`` extension's ``available`` endpoint.

The extension is a Node ESM module that cannot be imported from Python,
so this follows the same pattern as ``permission_requests_test.py`` and
``minds_api_proxy_test.py``: spawn a Node child that loads the
extension's default export, mount it on an HTTP server, and hit it with
``urllib``.

These tests focus on ``GET /permissions/available/<service_name>``
(the per-service catalog endpoint -- the bare collection endpoint is not
served), asserting that the catalog it serves (backed by the bundled
``services.json``) carries detent's per-schema descriptions -- the
scope-level ``description`` and each permission's ``{name, description}``
-- end to end.

Tests skip cleanly when Node is unavailable, mirroring the sibling
extension test modules.
"""

import contextlib
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
from urllib.parse import quote

import pytest

_NODE_BINARY: Final[str | None] = shutil.which("node")

_EXTENSION_PATH: Final[Path] = Path(__file__).resolve().parent / "permissions.mjs"

_NODE_READY_TIMEOUT_SECONDS: Final[float] = 15.0
_POLL_INTERVAL_SECONDS: Final[float] = 0.02

# The available endpoints read ``services.json`` from next to the
# extension and do not consult any caller-supplied path, so the driver
# only needs to mount the default export -- no ExtensionContext required.
_NODE_DRIVER_SCRIPT_TEMPLATE: Final[str] = r"""
import http from 'node:http';
import handler from {EXTENSION_PATH_LITERAL};

const server = http.createServer(async (request, response) => {{
  try {{
    const handled = await handler(request, response, Object.freeze({{}}));
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


pytestmark = pytest.mark.skipif(_NODE_BINARY is None, reason="node binary not available on PATH")


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


@contextlib.contextmanager
def _spawn_node_extension(env: dict[str, str]) -> Generator[str, None, None]:
    """Spawn the Node driver with ``env`` and yield the extension's base URL."""
    assert _NODE_BINARY is not None
    script = _build_node_driver_script()
    process = subprocess.Popen(
        [_NODE_BINARY, "--input-type=module", "-e", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        port = _wait_for_node_port(process)
        base_url = f"http://127.0.0.1:{port}"
        assert _wait_for_port("127.0.0.1", port)
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)


@pytest.fixture
def node_extension() -> Generator[str, None, None]:
    """Spawn the Node driver and yield the extension's base URL."""
    with _spawn_node_extension({"PATH": "/usr/bin:/bin"}) as base_url:
        yield base_url


def _get_json(url: str) -> tuple[int, object]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return int(response.status), json.loads(response.read())
    except urllib.error.HTTPError as e:
        return int(e.code), None


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return {str(key): item for key, item in value.items()}


def _as_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return [item for item in value]


def _as_nonempty_str(value: object) -> str:
    assert isinstance(value, str) and len(value) > 0
    return value


def _assert_entry_well_formed(entry: object) -> None:
    # ``name`` is required on every permission; the scope-level and
    # per-permission ``description`` fields are optional (detent's
    # ``$comment``), so we only assert their type when present.
    entry_dict = _as_dict(entry)
    assert {"scope", "display_name", "permissions"} <= set(entry_dict.keys())
    assert isinstance(entry_dict.get("description", ""), str)
    for permission in _as_list(entry_dict["permissions"]):
        permission_dict = _as_dict(permission)
        _as_nonempty_str(permission_dict["name"])
        assert isinstance(permission_dict.get("description", ""), str)


def test_available_for_service_exposes_descriptions(node_extension: str) -> None:
    """``GET /permissions/available/<service>`` returns description-bearing entries."""
    status, payload = _get_json(f"{node_extension}/permissions/available/slack")

    assert status == 200
    entries = _as_list(payload)
    assert len(entries) > 0
    for entry in entries:
        _assert_entry_well_formed(entry)

    # Pin Slack concretely so the test fails loudly if descriptions ever
    # stop flowing through: its scope and its read-all permission both
    # carry a non-empty summary.
    slack_entry = _as_dict(entries[0])
    _as_nonempty_str(slack_entry["description"])
    slack_permissions = [_as_dict(p) for p in _as_list(slack_entry["permissions"])]
    slack_read_all = next(p for p in slack_permissions if p["name"] == "slack-read-all")
    _as_nonempty_str(slack_read_all["description"])


def test_available_injects_any_permission_for_every_scope(node_extension: str) -> None:
    """The catch-all ``any`` permission is injected at index 0 of every scope.

    A service whose catalog lists at least one permission (Slack) and a
    service whose catalog lists none (Linear) must both surface ``any``
    as the first available permission, so a caller can always request
    unrestricted access under a known scope.
    """
    for service in ("slack", "linear"):
        status, payload = _get_json(f"{node_extension}/permissions/available/{service}")
        assert status == 200, service
        entries = _as_list(payload)
        assert len(entries) > 0, service
        for entry in entries:
            permissions = [_as_dict(p) for p in _as_list(_as_dict(entry)["permissions"])]
            assert permissions[0]["name"] == "any", service
            # ``any`` appears exactly once even if the catalog ever lists it.
            assert [p["name"] for p in permissions].count("any") == 1, service
            _as_nonempty_str(permissions[0]["description"])


def test_available_for_unknown_service_returns_404(node_extension: str) -> None:
    status, _ = _get_json(f"{node_extension}/permissions/available/not-a-real-service")

    assert status == 404


def _post_json(url: str, body: object) -> tuple[int, object]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return int(response.status), json.loads(response.read())
    except urllib.error.HTTPError as e:
        return int(e.code), None


def test_post_rule_creates_missing_host_directory(tmp_path: Path) -> None:
    """``POST /permissions/rules`` materializes the parent host directory if absent.

    Regression test: the minds desktop grant flow targets
    ``<root>/hosts/<host_id>/latchkey_permissions.json``, whose ``hosts/<id>/``
    directory may not have been created yet (e.g. agent creation's
    finalize/link step was skipped or failed). The atomic write must create the
    directory and succeed rather than failing with ENOENT (a confusing 500).
    """
    target = tmp_path / "hosts" / "host-deadbeefdeadbeefdeadbeefdeadbeef" / "latchkey_permissions.json"
    assert not target.parent.exists()
    env = {"PATH": "/usr/bin:/bin", "LATCHKEY_EXTENSION_PERMISSIONS_ROOT": str(tmp_path)}
    with _spawn_node_extension(env) as base_url:
        url = f"{base_url}/permissions/rules?path={quote(str(target))}&rule_key=slack-api"
        status, payload = _post_json(url, ["any"])

    assert status == 201, payload
    assert target.is_file()
    on_disk = json.loads(target.read_text())
    assert on_disk["rules"] == [{"slack-api": ["any"]}]

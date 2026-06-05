"""End-to-end tests for the ``permissions`` extension's ``available`` endpoints.

The extension is a Node ESM module that cannot be imported from Python,
so this follows the same pattern as ``permission_requests_test.py`` and
``minds_api_proxy_test.py``: spawn a Node child that loads the
extension's default export, mount it on an HTTP server, and hit it with
``urllib``.

These tests focus on ``GET /permissions/available`` and ``GET
/permissions/available/<service_name>``, asserting that the catalog they
serve (backed by the bundled ``services.json``) carries detent's
per-schema descriptions -- the scope-level ``description`` and each
permission's ``{name, description}`` -- end to end.

Tests skip cleanly when Node is unavailable, mirroring the sibling
extension test modules.
"""

import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final
from urllib.parse import urlencode

import pytest

_NODE_BINARY: Final[str | None] = shutil.which("node")

_EXTENSION_PATH: Final[Path] = Path(__file__).resolve().parent / "permissions.mjs"

_NODE_READY_TIMEOUT_SECONDS: Final[float] = 15.0
_POLL_INTERVAL_SECONDS: Final[float] = 0.02

# Env var the extension reads to locate the directory its caller-supplied
# ``path`` params must resolve underneath (see ``resolveRootDirectory`` in
# ``permissions.mjs``). The ``available`` endpoints ignore it; the
# ``/permissions/rules`` endpoints require it.
_PERMISSIONS_ROOT_ENV_VAR: Final[str] = "LATCHKEY_EXTENSION_PERMISSIONS_ROOT"

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


@contextmanager
def _running_extension(extra_env: dict[str, str] | None = None) -> Generator[str, None, None]:
    """Spawn the Node driver with the extension mounted and yield its base URL.

    ``extra_env`` is merged over the minimal base environment, letting
    rule-endpoint tests inject ``LATCHKEY_EXTENSION_PERMISSIONS_ROOT``
    while the available-services tests run without it.
    """
    assert _NODE_BINARY is not None
    script = _build_node_driver_script()
    env = {"PATH": "/usr/bin:/bin"}
    if extra_env is not None:
        env.update(extra_env)
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
    with _running_extension() as base_url:
        yield base_url


def _get_json(url: str) -> tuple[int, object]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return int(response.status), json.loads(response.read())
    except urllib.error.HTTPError as e:
        return int(e.code), None


def _post_json(url: str, body: object) -> int:
    """POST ``body`` as JSON to ``url`` and return the response status code."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        method="POST",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return int(response.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


def _rules_url(base_url: str, permissions_file: Path, rule_key: str) -> str:
    query = urlencode({"path": str(permissions_file), "rule_key": rule_key})
    return f"{base_url}/permissions/rules?{query}"


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


def test_available_collection_exposes_descriptions(node_extension: str) -> None:
    """``GET /permissions/available`` returns scope and per-permission descriptions."""
    status, payload = _get_json(f"{node_extension}/permissions/available")

    assert status == 200
    catalog = _as_dict(payload)
    assert len(catalog) > 0
    for entries in catalog.values():
        entry_list = _as_list(entries)
        assert len(entry_list) > 0
        for entry in entry_list:
            _assert_entry_well_formed(entry)

    # Pin a concrete service so the test fails loudly if descriptions ever
    # stop flowing through: Slack's scope and its read-all permission both
    # carry a non-empty summary.
    slack_entry = _as_dict(_as_list(catalog["slack"])[0])
    _as_nonempty_str(slack_entry["description"])
    slack_permissions = [_as_dict(p) for p in _as_list(slack_entry["permissions"])]
    slack_read_all = next(p for p in slack_permissions if p["name"] == "slack-read-all")
    _as_nonempty_str(slack_read_all["description"])


def test_available_for_service_exposes_descriptions(node_extension: str) -> None:
    """``GET /permissions/available/<service>`` returns the same description-bearing entries."""
    status, payload = _get_json(f"{node_extension}/permissions/available/slack")

    assert status == 200
    entries = _as_list(payload)
    assert len(entries) > 0
    for entry in entries:
        _assert_entry_well_formed(entry)
    _as_nonempty_str(_as_dict(entries[0])["description"])


def test_available_for_unknown_service_returns_404(node_extension: str) -> None:
    status, _ = _get_json(f"{node_extension}/permissions/available/not-a-real-service")

    assert status == 404


# -- POST /permissions/rules: the authoritative merge behaviour --
#
# The desktop client never merges permissions itself: it POSTs a flat
# permission array here and this extension owns the rewrite. The desktop
# ``FakeLatchkeyGatewayClient`` only *mirrors* the rewrite for its own
# unit tests, so the tests below are the real coverage for replace-not-
# append and sibling-key preservation.


def test_post_rule_replaces_existing_rule_for_same_scope(tmp_path: Path) -> None:
    """A second POST for the same scope replaces the rule rather than appending a duplicate."""
    permissions_file = tmp_path / "latchkey_permissions.json"
    with _running_extension({_PERMISSIONS_ROOT_ENV_VAR: str(tmp_path)}) as base_url:
        url = _rules_url(base_url, permissions_file, "slack-api")
        first_status = _post_json(url, ["slack-read-all"])
        second_status = _post_json(url, ["slack-read-all", "slack-write-all"])

    # 201 Created when the rule is first written, 200 OK on the in-place replace.
    assert first_status == 201
    assert second_status == 200
    on_disk = json.loads(permissions_file.read_text())
    assert on_disk["rules"] == [{"slack-api": ["slack-read-all", "slack-write-all"]}]


def test_post_rule_preserves_other_top_level_keys(tmp_path: Path) -> None:
    """Posting a rule rewrites ``rules`` only; sibling keys such as ``schemas`` survive.

    The per-agent baseline writes inline ``schemas`` definitions alongside
    its ``latchkey-self`` rule; the extension's ``{...file, rules}`` spread
    must keep that block intact across later user-driven grants.
    """
    permissions_file = tmp_path / "latchkey_permissions.json"
    baseline = {
        "rules": [{"latchkey-self": ["latchkey-self-create-permission-request"]}],
        "schemas": {"latchkey-self": {"properties": {"domain": {"const": "latchkey-self.invalid"}}}},
    }
    permissions_file.write_text(json.dumps(baseline))

    with _running_extension({_PERMISSIONS_ROOT_ENV_VAR: str(tmp_path)}) as base_url:
        url = _rules_url(base_url, permissions_file, "slack-api")
        status = _post_json(url, ["slack-read-all"])

    assert status == 201
    on_disk = json.loads(permissions_file.read_text())
    # The pre-existing schemas block is preserved verbatim.
    assert on_disk["schemas"] == baseline["schemas"]
    # Both the baseline rule and the newly-granted scope are present.
    assert {"latchkey-self": ["latchkey-self-create-permission-request"]} in on_disk["rules"]
    assert {"slack-api": ["slack-read-all"]} in on_disk["rules"]

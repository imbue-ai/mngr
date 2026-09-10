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

Node ships in the shared mngr image, so these tests run on offload and
assert the binary is present (a missing Node fails loudly rather than
skipping), mirroring the minds-api-proxy test module.
"""

import contextlib
import json
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.primitives import AgentId
from imbue.mngr_latchkey.account_scopes import account_scope_key
from imbue.mngr_latchkey.account_scopes import build_account_scope_schema
from imbue.mngr_latchkey.baseline_permissions import AGENT_BASELINE_PERMISSIONS
from imbue.mngr_latchkey.workspace_permissions import WORKSPACE_VERBS

_NODE_BINARY: Final[str | None] = shutil.which("node")

# A syntactically valid AgentId (``agent-`` + 32 hex chars) used as the request
# author in tests. The gateway validates agent_id against this exact format (see
# ``VALID_AGENT_ID_PATTERN`` in permission_requests.mjs), so a body must carry a
# conforming id or the POST is rejected with 400.
_VALID_AGENT_ID: Final[str] = "agent-" + "0" * 32

_EXTENSION_PATH: Final[Path] = Path(__file__).resolve().parent / "permission_requests.mjs"

_NODE_READY_TIMEOUT_SECONDS: Final[float] = 15.0
_POLL_INTERVAL_SECONDS: Final[float] = 0.02

# The file-sharing rule attaches to the pre-existing ``latchkey-self``
# scope from the agent baseline (defined in ``agent_setup.py``) rather
# than minting its own scope schema.
_FILE_SHARING_SCOPE_NAME: Final[str] = "latchkey-self"
_FILE_SHARING_PROXY_PATH_PREFIX: Final[str] = "/minds-api-proxy/api/v1/files"
_FILE_SHARING_PERMISSION_PREFIX: Final[str] = "minds-file-server-"

# The cross-workspace verbs also attach to the pre-existing ``latchkey-self``
# scope (like file-sharing and accounts): the grant rule is keyed here, and no
# ``minds-workspaces`` scope schema is minted. ``_LEGACY_WORKSPACES_SCOPE_NAME``
# is the scope an older build used and that must no longer appear on disk.
_WORKSPACE_GRANT_SCOPE_NAME: Final[str] = "latchkey-self"
_LEGACY_WORKSPACES_SCOPE_NAME: Final[str] = "minds-workspaces"
_FILE_SHARING_READ_METHODS: Final[tuple[str, ...]] = (
    "GET",
    "HEAD",
    "OPTIONS",
    "PROPFIND",
)
# Note: ``COPY`` and ``MOVE`` are intentionally not in this list -- both
# carry a second path in the ``Destination`` header that the per-file
# permission schema does not constrain, so granting either would let an
# agent write to a different file inside the WebDAV mount than the one
# the user actually shared. See ``permission_requests.mjs`` for the
# explanation.
_FILE_SHARING_WRITE_METHODS: Final[tuple[str, ...]] = (
    *_FILE_SHARING_READ_METHODS,
    "PUT",
    "DELETE",
    "PROPPATCH",
    "MKCOL",
    "LOCK",
    "UNLOCK",
)


def _file_sharing_permission_name(path: str, access: str) -> str:
    """Mirror the JS helper: ``minds-file-server-<access_lower>-<path>``."""
    return f"{_FILE_SHARING_PERMISSION_PREFIX}{access.lower()}-{path}"


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
            # File-sharing path validation rejects paths outside the WebDAV
            # mount roots, which the extension derives from the process's
            # HOME / TMPDIR (Node's ``homedir()`` / ``tmpdir()``). Pin both
            # to deterministic values so tests can use stable in-root paths
            # (``/home/example/...`` and ``/tmp/...``) regardless of the
            # runner's real HOME / TMPDIR.
            "HOME": "/home/example",
            "TMPDIR": "/tmp",
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


@contextlib.contextmanager
def _staged_extension(tmp_path: Path, extra_services: dict[str, object]) -> Generator[str, None, None]:
    """Run the extension against a copy of its data files, with the catalog doctored.

    Mirrors what ``core._materialize_bundled_extensions`` does in production --
    the extension resolves ``services.json`` next to itself, so overlaying a
    custom service means giving it a different directory to sit in.
    """
    assert _NODE_BINARY is not None
    staged = tmp_path / "extensions"
    staged.mkdir()
    (staged / _EXTENSION_PATH.name).write_text(_EXTENSION_PATH.read_text(), encoding="utf-8")
    package_dir = _EXTENSION_PATH.parent
    (staged / "workspace_permissions.json").write_text(
        (package_dir / "workspace_permissions.json").read_text(), encoding="utf-8"
    )
    catalog = json.loads((package_dir / "services.json").read_text())
    catalog.update(extra_services)
    (staged / "services.json").write_text(json.dumps(catalog), encoding="utf-8")

    script = _NODE_DRIVER_SCRIPT_TEMPLATE.format(
        EXTENSION_PATH_LITERAL=json.dumps((staged / _EXTENSION_PATH.name).as_uri())
    )
    process = subprocess.Popen(
        [_NODE_BINARY, "--input-type=module", "-e", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "LATCHKEY_DIRECTORY": str(tmp_path / "latchkey"),
            "TEST_PERMISSIONS_CONFIG_PATH": str(tmp_path / "permissions.json"),
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/example",
            "TMPDIR": "/tmp",
        },
        text=True,
    )
    try:
        port = _wait_for_node_port(process)
        assert _wait_for_port("127.0.0.1", port)
        yield f"http://127.0.0.1:{port}"
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
            "agent_id": _VALID_AGENT_ID,
            "rationale": "needs slack",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"], "account": "alice@example.com"},
        },
    )
    assert status == 201
    parsed = json.loads(body)
    assert parsed["agent_id"] == _VALID_AGENT_ID
    assert parsed["rationale"] == "needs slack"
    # The persisted/streamed shape renames the wire field ``type`` to
    # ``request_type`` to avoid shadowing the Python ``type`` builtin
    # in the consumer's pydantic model.
    assert parsed["request_type"] == "predefined"
    assert parsed["payload"] == {
        "scope": "slack-api",
        "permissions": ["slack-read-all"],
        "account": "alice@example.com",
    }
    assert parsed["target"] == str(permissions_config_path)
    # The grant is scoped to the named account: the rule key carries it and the
    # effect ships the generated schema that gates the built-in scope on it.
    rule_key = account_scope_key("slack-api", "alice@example.com")
    assert parsed["effect"] == {
        "schemas": {rule_key: build_account_scope_schema("slack-api", "alice@example.com")},
        "rules": [{rule_key: ["slack-read-all"]}],
    }
    # The persisted file should match the response on disk.
    stored = next((latchkey_directory / "permission_requests" / "v3").iterdir())
    assert json.loads(stored.read_text()) == parsed


def test_post_creates_predefined_request_for_additional_service(
    node_extension: tuple[str, Path, Path],
) -> None:
    """A ``predefined`` request for a custom (additional) service validates like a builtin one."""
    base_url, _latchkey_directory, _permissions_config_path = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "needs claude.ai",
            "type": "predefined",
            "payload": {"scope": "claude-ai", "permissions": ["everything"], "account": "me@example.com"},
        },
    )
    assert status == 201, body
    parsed = json.loads(body)
    assert parsed["payload"] == {
        "scope": "claude-ai",
        "permissions": ["everything"],
        "account": "me@example.com",
    }
    # A custom scope composes with per-account scoping exactly like a builtin one:
    # the rule key carries the account and the effect ships the generated
    # account-gating schema. The custom scope's *own* schema is not in the effect --
    # the target permissions file already carries it from the agent baseline.
    rule_key = account_scope_key("claude-ai", "me@example.com")
    assert parsed["effect"] == {
        "schemas": {rule_key: build_account_scope_schema("claude-ai", "me@example.com")},
        "rules": [{rule_key: ["everything"]}],
    }


def test_post_rejects_unknown_permission_for_additional_service(
    node_extension: tuple[str, Path, Path],
) -> None:
    """A permission not offered by a custom service is rejected, just like for a builtin one."""
    base_url, _latchkey_directory, _permissions_config_path = node_extension
    status, _body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "needs claude.ai",
            "type": "predefined",
            "payload": {"scope": "claude-ai", "permissions": ["not-a-real-permission"]},
        },
    )
    assert status == 400


@pytest.mark.parametrize(
    ("access", "expected_methods"),
    [
        ("READ", _FILE_SHARING_READ_METHODS),
        ("WRITE", _FILE_SHARING_WRITE_METHODS),
    ],
)
def test_post_creates_file_sharing_request_with_schemas_and_rules(
    node_extension: tuple[str, Path, Path],
    access: str,
    expected_methods: tuple[str, ...],
) -> None:
    base_url, _latchkey_directory, _permissions_config_path = node_extension
    target_path = "/home/example/data.txt"
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "needs to access example data",
            "type": "file-sharing",
            "payload": {"path": target_path, "access": access},
        },
    )
    assert status == 201
    parsed = json.loads(body)
    assert parsed["request_type"] == "file-sharing"
    assert parsed["payload"] == {"path": target_path, "access": access}
    effect = parsed["effect"]
    permission_name = _file_sharing_permission_name(target_path, access)
    # The rule attaches the new per-file permission to the pre-existing
    # ``latchkey-self`` scope from the agent baseline; we do not mint a
    # scope schema of our own here.
    assert effect["rules"] == [{_FILE_SHARING_SCOPE_NAME: [permission_name]}]
    schemas = effect["schemas"]
    assert set(schemas.keys()) == {permission_name}
    # The per-path permission schema constrains the URL path via a
    # regex ``pattern`` (not a ``const``): granting access to a
    # resource at ``<base>`` admits the exact path, the same path
    # with a trailing slash, and any sub-path nested below it (so a
    # grant on a directory transitively covers files inside).
    # ``method`` stays a plain enum of WebDAV verbs for the requested
    # access mode.
    #
    # The pattern deliberately does *not* try to reject ``..``
    # segments: detent feeds the permission check a request built
    # from a WHATWG URL, and the WHATWG URL parser already collapses
    # both literal ``..`` and percent-encoded ``%2e%2e`` segments out
    # of ``pathname`` before the pattern is ever evaluated. So the
    # assertions below only check the surface contract -- exact /
    # trailing-slash / nested-below match for paths that start with
    # ``<base>/``, and rejection of anything that doesn't.
    perm_schema = schemas[permission_name]
    expected_webdav_path = f"{_FILE_SHARING_PROXY_PATH_PREFIX}{target_path}"
    assert perm_schema["properties"]["path"]["type"] == "string"
    path_pattern = re.compile(perm_schema["properties"]["path"]["pattern"])
    for url_path in (
        expected_webdav_path,
        f"{expected_webdav_path}/",
        f"{expected_webdav_path}/sub",
        f"{expected_webdav_path}/sub/",
        f"{expected_webdav_path}/a/b/c",
        f"{expected_webdav_path}/a/b/c/",
    ):
        assert path_pattern.fullmatch(url_path), url_path
    # The grant must not extend to a sibling under the share, and
    # must not be activated by a path that merely shares a prefix
    # with ``<base>`` but does not start with ``<base>/`` (so e.g.
    # ``<base>suffix`` is rejected).
    for url_path in (
        f"{expected_webdav_path}suffix",
        f"{_FILE_SHARING_PROXY_PATH_PREFIX}/home/example/other.txt",
    ):
        assert not path_pattern.fullmatch(url_path), url_path
    assert perm_schema["properties"]["method"] == {"enum": list(expected_methods)}


@pytest.mark.parametrize(
    "target_path",
    [
        "/home/example/My Documents/data.txt",
        "/home/example/My Documents/",
        "/home/example/r\u00e9sum\u00e9s/sp ace/d\u00ef.txt",
    ],
)
def test_file_sharing_pattern_matches_percent_encoded_request_path(
    node_extension: tuple[str, Path, Path],
    target_path: str,
) -> None:
    """A shared path with spaces / non-ASCII matches the encoded request path.

    The gateway builds the permission check's request from a WHATWG URL,
    so detent matches the per-file schema's ``pattern`` against the
    percent-encoded ``URL.pathname`` (a space becomes ``%20``, non-ASCII
    its UTF-8 ``%XX`` sequence). The pattern must therefore embed the
    encoded form -- embedding the raw path (with a literal space) would
    never match the request and the grant would be silently inert.
    """
    base_url, _latchkey_directory, _permissions_config_path = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "needs the shared directory",
            "type": "file-sharing",
            "payload": {"path": target_path, "access": "READ"},
        },
    )
    assert status == 201, body
    parsed = json.loads(body)
    # The schema *name* is a human-readable plaintext key, so it keeps
    # the raw path verbatim for auditability.
    permission_name = _file_sharing_permission_name(target_path, "READ")
    schema = parsed["effect"]["schemas"][permission_name]
    path_pattern = re.compile(schema["properties"]["path"]["pattern"])
    # ``urllib.parse.quote`` with ``safe='/'`` reproduces the WHATWG
    # path-percent-encode set for these characters (space -> %20,
    # non-ASCII -> UTF-8 %XX), matching what detent sees on the request.
    encoded_path = urllib.parse.quote(f"{_FILE_SHARING_PROXY_PATH_PREFIX}{target_path}", safe="/")
    raw_path = f"{_FILE_SHARING_PROXY_PATH_PREFIX}{target_path}"
    # The encoded request path matches; the raw (literal-space) path does
    # not -- the request never arrives un-encoded, and matching it would
    # be a sign the pattern was built from the wrong (raw) form.
    assert path_pattern.fullmatch(encoded_path), encoded_path
    assert path_pattern.fullmatch(f"{encoded_path}/sub"), encoded_path
    if raw_path != encoded_path:
        assert not path_pattern.fullmatch(raw_path), raw_path


@pytest.mark.parametrize(
    ("requested_path", "expanded_path"),
    [
        ("~", "/home/example"),
        ("~/", "/home/example/"),
        ("~/Documents/shared.txt", "/home/example/Documents/shared.txt"),
        ("~/My Documents/data.txt", "/home/example/My Documents/data.txt"),
    ],
)
def test_post_expands_tilde_home_path_in_file_sharing(
    node_extension: tuple[str, Path, Path],
    requested_path: str,
    expanded_path: str,
) -> None:
    """A ``~`` / ``~/...`` path expands to the current user's home directory.

    The fixture pins ``HOME=/home/example`` (Node's ``homedir()``), so
    the grant must be stored and built against the expanded absolute
    path -- the persisted payload, the per-file schema name, and the
    WebDAV pattern all use the expanded form rather than the ``~``
    shorthand.
    """
    base_url, _latchkey_directory, _permissions_config_path = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "wants something in the home directory",
            "type": "file-sharing",
            "payload": {"path": requested_path, "access": "READ"},
        },
    )
    assert status == 201, body
    parsed = json.loads(body)
    # The persisted payload carries the expanded absolute path, not the
    # ``~`` shorthand the agent supplied.
    assert parsed["payload"]["path"] == expanded_path
    expanded_name = _file_sharing_permission_name(expanded_path, "READ")
    requested_name = _file_sharing_permission_name(requested_path, "READ")
    schemas = parsed["effect"]["schemas"]
    assert expanded_name in schemas
    assert requested_name not in schemas
    # The WebDAV pattern matches the percent-encoded expanded path.
    path_pattern = re.compile(schemas[expanded_name]["properties"]["path"]["pattern"])
    encoded_webdav_path = urllib.parse.quote(f"{_FILE_SHARING_PROXY_PATH_PREFIX}{expanded_path}", safe="/")
    assert path_pattern.fullmatch(encoded_webdav_path), encoded_webdav_path


def test_post_rejects_tilde_user_notation_in_file_sharing(
    node_extension: tuple[str, Path, Path],
) -> None:
    """``~user`` (another user's home) cannot be resolved here and is rejected."""
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "file-sharing",
            "payload": {"path": "~otheruser/secret.txt", "access": "READ"},
        },
    )
    assert status == 400, body
    message = json.loads(body)["error"]
    assert "~user" in message


def test_post_rejects_tilde_traversal_in_file_sharing(
    node_extension: tuple[str, Path, Path],
) -> None:
    """``~/../...`` must not escape the home directory via expansion."""
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "file-sharing",
            "payload": {"path": "~/../etc/passwd", "access": "READ"},
        },
    )
    assert status == 400, body
    assert "traversal" in json.loads(body)["error"].lower()


def test_approve_with_tilde_path_override_expands_home(
    node_extension: tuple[str, Path, Path],
) -> None:
    """A ``~``-prefixed path edited into the approve dialog expands to home."""
    base_url, _latchkey_directory, permissions_config_path = node_extension
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "needs a file",
            "type": "file-sharing",
            "payload": {"path": "/home/example/requested.txt", "access": "READ"},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]

    approve_status, approve_body = _post_json(
        f"{base_url}/permission-requests/approve/{request_id}",
        {"path": "~/Documents/Shared"},
    )
    assert approve_status == 200, approve_body
    applied = json.loads(permissions_config_path.read_text())
    expanded_name = _file_sharing_permission_name("/home/example/Documents/Shared", "READ")
    assert applied["rules"] == [{_FILE_SHARING_SCOPE_NAME: [expanded_name]}]
    assert expanded_name in applied["schemas"]


def test_read_and_write_grants_for_same_path_coexist_in_persisted_record(
    node_extension: tuple[str, Path, Path],
) -> None:
    """READ and WRITE grants for the same path use distinct permission schema names.

    They must not collide so a user can hold one or both grants for the
    same path independently (a WRITE grant does not silently overwrite
    an earlier READ grant or vice versa).
    """
    base_url, _latchkey_directory, _permissions_config_path = node_extension
    target_path = "/home/example/data.txt"
    read_status, read_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "r",
            "type": "file-sharing",
            "payload": {"path": target_path, "access": "READ"},
        },
    )
    write_status, write_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "w",
            "type": "file-sharing",
            "payload": {"path": target_path, "access": "WRITE"},
        },
    )
    assert read_status == 201, read_body
    assert write_status == 201, write_body
    read_name = _file_sharing_permission_name(target_path, "READ")
    write_name = _file_sharing_permission_name(target_path, "WRITE")
    assert read_name != write_name
    assert read_name.startswith(f"{_FILE_SHARING_PERMISSION_PREFIX}read-")
    assert write_name.startswith(f"{_FILE_SHARING_PERMISSION_PREFIX}write-")


@pytest.mark.parametrize(
    ("missing_or_invalid_payload", "expected_message_fragment"),
    [
        ({"path": "/tmp/ok.txt"}, "access"),
        ({"path": "/tmp/ok.txt", "access": ""}, "access"),
        ({"path": "/tmp/ok.txt", "access": "ReadWrite"}, "access"),
        ({"path": "/tmp/ok.txt", "access": "read"}, "access"),
        ({"path": "/tmp/ok.txt", "access": None}, "access"),
    ],
)
def test_post_rejects_missing_or_invalid_access_in_file_sharing(
    node_extension: tuple[str, Path, Path],
    missing_or_invalid_payload: dict[str, object],
    expected_message_fragment: str,
) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "file-sharing",
            "payload": missing_or_invalid_payload,
        },
    )
    assert status == 400, body
    assert expected_message_fragment in json.loads(body)["error"].lower()


def test_post_rejects_unknown_type(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "wholesale",
            "payload": {},
        },
    )
    assert status == 400
    assert "type" in json.loads(body)["error"]


# -- POST /permission-requests: accounts type --


def test_post_creates_accounts_request_with_fixed_permission_under_latchkey_self(
    node_extension: tuple[str, Path, Path],
) -> None:
    base_url, _latchkey_directory, _permissions_config_path = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "needs to discover an account to associate",
            "type": "accounts",
            "payload": {},
        },
    )
    assert status == 201
    parsed = json.loads(body)
    assert parsed["request_type"] == "accounts"
    assert parsed["payload"] == {}
    effect = parsed["effect"]
    # A single fixed permission under the pre-existing ``latchkey-self`` scope --
    # no new scope is minted (mirrors file-sharing).
    assert effect["rules"] == [{"latchkey-self": ["minds-accounts-read"]}]
    schema = effect["schemas"]["minds-accounts-read"]
    assert schema["properties"]["method"] == {"const": "GET"}
    assert schema["properties"]["path"]["pattern"] == r"^/minds-api-proxy/api/v1/accounts(/|$)"


def test_post_rejects_accounts_payload_with_fields(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, _body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "accounts",
            "payload": {"permissions": ["minds-accounts-read"]},
        },
    )
    assert status == 400


# -- POST /permission-requests: workspace type --


def test_post_creates_workspace_request_with_target_and_effect(
    node_extension: tuple[str, Path, Path],
) -> None:
    base_url, latchkey_directory, permissions_config_path = node_extension
    target_id = str(AgentId.generate())
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "needs to destroy a sibling workspace",
            "type": "workspace",
            "payload": {
                "permissions": ["minds-workspaces-destroy"],
                "target_workspace_id": target_id,
            },
        },
    )
    assert status == 201, body
    parsed = json.loads(body)
    assert parsed["request_type"] == "workspace"
    assert parsed["payload"] == {
        "permissions": ["minds-workspaces-destroy"],
        "target_workspace_id": target_id,
    }
    assert parsed["target"] == str(permissions_config_path)
    # The effect is a self-contained patch (a uniquely-named per-target verb
    # schema + a grant rule attaching it to the ``latchkey-self`` scope), applied
    # via /approve like file-sharing -- not an informational rules-only stub. No
    # ``minds-workspaces`` scope schema is minted.
    effect = parsed["effect"]
    per_target_name = f"minds-workspaces-destroy-{target_id}"
    assert effect["rules"] == [{_WORKSPACE_GRANT_SCOPE_NAME: [per_target_name]}]
    assert set(effect["schemas"].keys()) == {per_target_name}
    # The per-target schema pins the single target's destroy path.
    perm_schema = effect["schemas"][per_target_name]
    assert perm_schema["properties"]["method"] == {"const": "POST"}
    path_pattern = re.compile(perm_schema["properties"]["path"]["pattern"])
    prefix = "/minds-api-proxy/api/v1/workspaces"
    assert path_pattern.fullmatch(f"{prefix}/{target_id}/destroy")
    # A different workspace id is not covered by the single-target grant.
    other_id = str(AgentId.generate())
    assert not path_pattern.fullmatch(f"{prefix}/{other_id}/destroy")
    stored = next((latchkey_directory / "permission_requests" / "v3").iterdir())
    assert json.loads(stored.read_text()) == parsed


def test_post_creates_workspace_request_all_workspaces_uses_wildcard(
    node_extension: tuple[str, Path, Path],
) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "needs ssh to any workspace",
            "type": "workspace",
            "payload": {"permissions": ["minds-workspaces-ssh"], "target_workspace_id": None},
        },
    )
    assert status == 201, body
    effect = json.loads(body)["effect"]
    # With no target, the targeted verb is keyed by the plain verb name and its
    # path uses the ``[^/]+`` id wildcard (an "all workspaces" grant).
    assert effect["rules"] == [{_WORKSPACE_GRANT_SCOPE_NAME: ["minds-workspaces-ssh"]}]
    ssh_schema = effect["schemas"]["minds-workspaces-ssh"]
    path_pattern = re.compile(ssh_schema["properties"]["path"]["pattern"])
    prefix = "/minds-api-proxy/api/v1/workspaces"
    assert path_pattern.fullmatch(f"{prefix}/{AgentId.generate()}/ssh")
    assert path_pattern.fullmatch(f"{prefix}/{AgentId.generate()}/ssh")


def test_post_creates_workspace_request_multi_method_verb(
    node_extension: tuple[str, Path, Path],
) -> None:
    # A verb whose catalog ``method`` is an array (here ``minds-workspaces-recover``
    # matches GET + POST) produces a schema whose ``method`` is an ``enum`` of all
    # its methods, and whose targeted path pattern includes the verb's suffix.
    base_url, *_ = node_extension
    target_id = str(AgentId.generate())
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "needs to recover a sibling workspace",
            "type": "workspace",
            "payload": {
                "permissions": ["minds-workspaces-recover"],
                "target_workspace_id": target_id,
            },
        },
    )
    assert status == 201, body
    effect = json.loads(body)["effect"]
    per_target_name = f"minds-workspaces-recover-{target_id}"
    perm_schema = effect["schemas"][per_target_name]
    # Multi-method verb -> enum of every method (order-independent).
    assert perm_schema["properties"]["method"] == {"enum": ["GET", "POST"]}
    path_pattern = re.compile(perm_schema["properties"]["path"]["pattern"])
    prefix = "/minds-api-proxy/api/v1/workspaces"
    # The suffix admits both the health and restart sub-paths for this target.
    assert path_pattern.fullmatch(f"{prefix}/{target_id}/health")
    assert path_pattern.fullmatch(f"{prefix}/{target_id}/restart")
    # A different workspace id is not covered by the single-target grant.
    assert not path_pattern.fullmatch(f"{prefix}/{AgentId.generate()}/health")


def test_approve_workspace_override_recomputes_and_accumulates(
    node_extension: tuple[str, Path, Path],
) -> None:
    """Approving a workspace request with an override splices the recomputed effect.

    Two successive approvals for different targets accumulate as distinct,
    uniquely-named per-target schemas through the gateway's schema-by-name merge.
    """
    base_url, _latchkey_directory, permissions_config_path = node_extension
    target_a = str(AgentId.generate())
    target_b = str(AgentId.generate())

    for target in (target_a, target_b):
        status, body = _post_json(
            f"{base_url}/permission-requests",
            {
                "agent_id": _VALID_AGENT_ID,
                "rationale": "destroy a sibling",
                "type": "workspace",
                "payload": {"permissions": ["minds-workspaces-destroy"], "target_workspace_id": target},
            },
        )
        assert status == 201, body
        request_id = json.loads(body)["request_id"]
        # Approve with an override echoing the request's verbs + selected target.
        status, approve_body = _http(
            f"{base_url}/permission-requests/approve/{request_id}",
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"permissions": ["minds-workspaces-destroy"], "target_workspace_id": target}).encode(
                "utf-8"
            ),
        )[0:3:2]
        assert status == 200, approve_body

    applied = json.loads(permissions_config_path.read_text())
    # Both targets accumulated as distinct per-target schemas, both referenced
    # by the single minds-workspaces rule.
    name_a = f"minds-workspaces-destroy-{target_a}"
    name_b = f"minds-workspaces-destroy-{target_b}"
    assert name_a in applied["schemas"]
    assert name_b in applied["schemas"]
    rule = next(r for r in applied["rules"] if list(r.keys()) == [_WORKSPACE_GRANT_SCOPE_NAME])
    assert name_a in rule[_WORKSPACE_GRANT_SCOPE_NAME]
    assert name_b in rule[_WORKSPACE_GRANT_SCOPE_NAME]


def test_approve_workspace_all_override_grants_broadly(
    node_extension: tuple[str, Path, Path],
) -> None:
    """An ``all`` override (null target) grants the broad verb schema."""
    base_url, _latchkey_directory, permissions_config_path = node_extension
    target = str(AgentId.generate())
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "destroy a sibling",
            "type": "workspace",
            "payload": {"permissions": ["minds-workspaces-destroy"], "target_workspace_id": target},
        },
    )
    assert status == 201, body
    request_id = json.loads(body)["request_id"]
    status = _http(
        f"{base_url}/permission-requests/approve/{request_id}",
        method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"permissions": ["minds-workspaces-destroy"], "target_workspace_id": None}).encode("utf-8"),
    )[0]
    assert status == 200
    applied = json.loads(permissions_config_path.read_text())
    # The broad (all-workspaces) schema is keyed by the plain verb name.
    assert "minds-workspaces-destroy" in applied["schemas"]
    assert f"minds-workspaces-destroy-{target}" not in applied["schemas"]


def test_approve_workspace_grant_folds_into_latchkey_self_on_real_baseline(
    node_extension: tuple[str, Path, Path],
) -> None:
    """A workspace grant approved on top of the real agent baseline lands on ``latchkey-self``.

    The verbs attach as permissions on the single domain-only ``latchkey-self``
    scope the baseline already declares (like file-sharing and accounts), so no
    second gateway-self rule and no ``minds-workspaces`` scope schema appear.
    Because there is only ever one gateway-self rule, detent's
    first-matching-scope-wins evaluation reaches the grant regardless of order.

    Unlike the other approve tests, this one seeds the target file with the real
    baseline first, so the fold onto the pre-existing ``latchkey-self`` rule is
    exercised (the others start empty).
    """
    base_url, _latchkey_directory, permissions_config_path = node_extension
    # Seed the exact per-host baseline a live host has: the per-agent gate followed
    # by the domain-only ``latchkey-self`` rule.
    permissions_config_path.write_text(json.dumps(AGENT_BASELINE_PERMISSIONS.model_dump()))

    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "manage sibling workspaces",
            "type": "workspace",
            "payload": {
                "permissions": ["minds-workspaces-read", "minds-workspaces-create"],
                "target_workspace_id": None,
            },
        },
    )
    assert status == 201, body
    request_id = json.loads(body)["request_id"]
    status, approve_body = _http(
        f"{base_url}/permission-requests/approve/{request_id}",
        method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps(
            {"permissions": ["minds-workspaces-read", "minds-workspaces-create"], "target_workspace_id": None}
        ).encode("utf-8"),
    )[0:3:2]
    assert status == 200, approve_body

    applied = json.loads(permissions_config_path.read_text())
    rule_keys = [next(iter(rule.keys())) for rule in applied["rules"]]
    # The grant folds onto the single ``latchkey-self`` rule: no ``minds-workspaces``
    # rule, and no duplicate gateway-self rule was introduced.
    assert _LEGACY_WORKSPACES_SCOPE_NAME not in rule_keys
    assert rule_keys.count(_WORKSPACE_GRANT_SCOPE_NAME) == 1
    latchkey_self_rule = next(rule for rule in applied["rules"] if _WORKSPACE_GRANT_SCOPE_NAME in rule)
    granted = latchkey_self_rule[_WORKSPACE_GRANT_SCOPE_NAME]
    assert {"minds-workspaces-read", "minds-workspaces-create"}.issubset(set(granted))
    # No ``minds-workspaces`` scope schema is emitted.
    assert _LEGACY_WORKSPACES_SCOPE_NAME not in applied["schemas"]


def test_approve_workspace_rejects_unknown_verb_in_override(
    node_extension: tuple[str, Path, Path],
) -> None:
    base_url, *_ = node_extension
    target = str(AgentId.generate())
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "destroy a sibling",
            "type": "workspace",
            "payload": {"permissions": ["minds-workspaces-destroy"], "target_workspace_id": target},
        },
    )
    request_id = json.loads(body)["request_id"]
    status, approve_body = _http(
        f"{base_url}/permission-requests/approve/{request_id}",
        method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"permissions": ["minds-workspaces-teleport"]}).encode("utf-8"),
    )[0:3:2]
    assert status == 400
    assert "permissions" in json.loads(approve_body)["error"]


def test_post_creates_workspace_request_without_target(
    node_extension: tuple[str, Path, Path],
) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "needs to create + list workspaces",
            "type": "workspace",
            "payload": {"permissions": ["minds-workspaces-read", "minds-workspaces-create"]},
        },
    )
    assert status == 201, body
    parsed = json.loads(body)
    # An omitted target normalizes to null in the persisted payload.
    assert parsed["payload"]["target_workspace_id"] is None
    assert parsed["payload"]["permissions"] == ["minds-workspaces-read", "minds-workspaces-create"]


def test_post_accepts_all_python_workspace_verbs(node_extension: tuple[str, Path, Path]) -> None:
    # Cross-language drift guard: every verb the Python
    # ``workspace_permissions`` source of truth defines must be accepted by the
    # gateway's hard-coded ``VALID_WORKSPACE_VERBS`` set.
    base_url, *_ = node_extension
    python_verbs = [verb.permission for verb in WORKSPACE_VERBS]
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "all verbs",
            "type": "workspace",
            "payload": {"permissions": python_verbs},
        },
    )
    assert status == 201, body


def test_post_rejects_unknown_workspace_verb(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "workspace",
            "payload": {"permissions": ["minds-workspaces-teleport"]},
        },
    )
    assert status == 400
    assert "permissions" in json.loads(body)["error"]


def test_post_rejects_empty_workspace_permissions(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "workspace",
            "payload": {"permissions": []},
        },
    )
    assert status == 400
    assert "permissions" in json.loads(body)["error"]


def test_post_rejects_invalid_workspace_target_id(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "workspace",
            "payload": {
                "permissions": ["minds-workspaces-destroy"],
                "target_workspace_id": "not-an-agent-id",
            },
        },
    )
    assert status == 400
    assert "target_workspace_id" in json.loads(body)["error"]


def test_post_rejects_extraneous_workspace_payload_field(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "workspace",
            "payload": {"permissions": ["minds-workspaces-read"], "bonus": 1},
        },
    )
    assert status == 400
    assert "bonus" in json.loads(body)["error"]


@pytest.mark.parametrize(
    "malformed_agent_id",
    [
        pytest.param("ENV_AGENT", id="placeholder-from-crash-report"),
        pytest.param("agent-1", id="right-prefix-wrong-length"),
        pytest.param("agent-" + "g" * 32, id="non-hex-characters"),
        pytest.param("agent-" + "0" * 31, id="one-char-too-short"),
        pytest.param("agent-" + "0" * 33, id="one-char-too-long"),
        pytest.param("0" * 32, id="missing-agent-prefix"),
    ],
)
def test_post_rejects_malformed_agent_id(
    node_extension: tuple[str, Path, Path],
    malformed_agent_id: str,
) -> None:
    # A malformed agent_id is rejected at the gateway with a 400 -- so the agent
    # is notified at its tool call -- and is never persisted. Otherwise the
    # consumer's ``AgentId(...)`` parse would raise later and kill the
    # permission-requests consumer thread.
    base_url, latchkey_directory, _permissions_config_path = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": malformed_agent_id,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
        },
    )
    assert status == 400, body
    assert "agent_id" in json.loads(body)["error"]
    persisted_dir = latchkey_directory / "permission_requests" / "v3"
    persisted = list(persisted_dir.iterdir()) if persisted_dir.exists() else []
    assert persisted == [], f"a rejected request must not be persisted, found {persisted}"


def test_post_accepts_generated_agent_id(node_extension: tuple[str, Path, Path]) -> None:
    # Cross-language drift guard: a real id minted by the Python ``AgentId``
    # source of truth must satisfy the gateway's JS ``VALID_AGENT_ID_PATTERN``.
    # If the two ever diverge, this 201 assertion fails.
    base_url, _latchkey_directory, _permissions_config_path = node_extension
    generated_agent_id = AgentId.generate()
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": generated_agent_id,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
        },
    )
    assert status == 201, body
    parsed = json.loads(body)
    # The persisted agent_id round-trips back through the Python validator.
    assert AgentId(parsed["agent_id"]) == generated_agent_id


def test_post_rejects_relative_path_in_file_sharing(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
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
                "agent_id": _VALID_AGENT_ID,
                "rationale": "x",
                "type": "file-sharing",
                "payload": {"path": traversal_path},
            },
        )
        assert status == 400, (traversal_path, body)
        message = json.loads(body)["error"].lower()
        assert "traversal" in message or "absolute" in message, (traversal_path, message)


def test_post_rejects_path_outside_mount_roots(node_extension: tuple[str, Path, Path]) -> None:
    """A path outside the home / temp WebDAV mounts is rejected at creation."""
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "wants a system file",
            "type": "file-sharing",
            "payload": {"path": "/etc/passwd", "access": "READ"},
        },
    )
    assert status == 400, body
    message = json.loads(body)["error"]
    assert "shared root" in message
    # The error names the roots so the agent can self-correct.
    assert "/home/example" in message
    assert "/tmp" in message


def test_post_accepts_path_under_temp_root(node_extension: tuple[str, Path, Path]) -> None:
    """A path under the system temp mount is accepted (the temp dir is a shared root)."""
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "share a scratch file",
            "type": "file-sharing",
            "payload": {"path": "/tmp/scratch/output.txt", "access": "WRITE"},
        },
    )
    assert status == 201, body


def test_post_rejects_extraneous_top_level_field(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
            "request_id": "spoofed",
        },
    )
    assert status == 400
    assert "request_id" in json.loads(body)["error"]


def test_post_predefined_without_account_has_empty_effect(
    node_extension: tuple[str, Path, Path],
) -> None:
    """An agent that names no account files a request nobody can blindly approve.

    Grants are per account, so a request that names none has nothing to apply:
    the effect is empty and a bare ``/approve`` is a no-op. The account has to
    come from the approving client (the minds dialog, or an approve override).
    """
    base_url, _latchkey_directory, permissions_config_path = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
        },
    )
    assert status == 201, body
    parsed = json.loads(body)
    assert parsed["payload"]["account"] is None
    assert parsed["effect"] == {}

    approve_status, _ = _post_json(f"{base_url}/permission-requests/approve/{parsed['request_id']}", None)
    assert approve_status == 200
    # Nothing was granted: the file holds no rules at all.
    assert json.loads(permissions_config_path.read_text())["rules"] == []


def test_approve_with_account_override_grants_that_account(
    node_extension: tuple[str, Path, Path],
) -> None:
    """The approving client supplies the account the user picked."""
    base_url, _latchkey_directory, permissions_config_path = node_extension
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all", "slack-write-all"]},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    approve_status, approve_body = _post_json(
        f"{base_url}/permission-requests/approve/{request_id}",
        {"account": "bob@example.com", "permissions": ["slack-read-all"]},
    )
    assert approve_status == 200, approve_body
    applied = json.loads(permissions_config_path.read_text())
    rule_key = account_scope_key("slack-api", "bob@example.com")
    # Only the permission subset the user kept is granted, and only for the
    # account they picked.
    assert applied["rules"] == [{rule_key: ["slack-read-all"]}]
    assert applied["schemas"][rule_key] == build_account_scope_schema("slack-api", "bob@example.com")


def test_approve_account_override_replaces_the_requested_account(
    node_extension: tuple[str, Path, Path],
) -> None:
    """The user may grant a different account than the agent asked for."""
    base_url, _latchkey_directory, permissions_config_path = node_extension
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"], "account": "alice@example.com"},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    approve_status, _ = _post_json(
        f"{base_url}/permission-requests/approve/{request_id}",
        {"account": "bob@example.com"},
    )
    assert approve_status == 200
    rule_keys = [next(iter(rule)) for rule in json.loads(permissions_config_path.read_text())["rules"]]
    assert rule_keys == [account_scope_key("slack-api", "bob@example.com")]


def test_post_accepts_an_account_containing_the_naming_separator(
    node_extension: tuple[str, Path, Path],
) -> None:
    """The rule key is never parsed, so a colon in the account is harmless.

    What pins the grant to the account is the generated schema's ``const`` gate;
    the key is only a (human-readable) identifier.
    """
    base_url, _latchkey_directory, permissions_config_path = node_extension
    account = "we:ird@example.com"
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"], "account": account},
        },
    )
    assert status == 201, body
    request_id = json.loads(body)["request_id"]
    approve_status, _ = _post_json(f"{base_url}/permission-requests/approve/{request_id}", None)
    assert approve_status == 200
    applied = json.loads(permissions_config_path.read_text())
    rule_key = account_scope_key("slack-api", account)
    assert applied["rules"] == [{rule_key: ["slack-read-all"]}]
    # The account survives verbatim in the gate, which is what detent matches on.
    assert applied["schemas"][rule_key] == build_account_scope_schema("slack-api", account)


def test_post_rejects_non_string_account(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["any"], "account": 7},
        },
    )
    assert status == 400, body
    assert "account" in json.loads(body)["error"]


def test_post_rejects_unknown_scope_in_predefined(node_extension: tuple[str, Path, Path]) -> None:
    """Predefined requests must name a scope from the bundled services catalog."""
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "made-up-api", "permissions": ["slack-read-all"]},
        },
    )
    assert status == 400, body
    error = json.loads(body)["error"]
    assert "scope" in error
    assert "made-up-api" in error


def test_post_rejects_unknown_permission_in_predefined(node_extension: tuple[str, Path, Path]) -> None:
    """Predefined requests must only name permissions that the catalog lists for the scope."""
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all", "made-up-perm"]},
        },
    )
    assert status == 400, body
    error = json.loads(body)["error"]
    assert "permissions" in error
    assert "made-up-perm" in error


@pytest.mark.parametrize("scope", ["slack-api", "linear-api"])
def test_post_accepts_any_permission_for_known_scope(node_extension: tuple[str, Path, Path], scope: str) -> None:
    """The catch-all ``any`` permission is valid under any known scope.

    This holds even for a scope whose catalog enumerates no permissions
    (``linear-api``), so a caller can always request unrestricted access
    under a known scope.
    """
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": scope, "permissions": ["any"], "account": "alice@example.com"},
        },
    )
    assert status == 201, body
    parsed = json.loads(body)
    assert parsed["effect"]["rules"] == [{account_scope_key(scope, "alice@example.com"): ["any"]}]


def test_post_rejects_permission_from_a_different_scope(node_extension: tuple[str, Path, Path]) -> None:
    """A permission valid under one scope must not be accepted under a different scope."""
    base_url, *_ = node_extension
    # ``github-read-all`` lives under the ``github-rest-api`` scope, not ``slack-api``.
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["github-read-all"]},
        },
    )
    assert status == 400, body
    error = json.loads(body)["error"]
    assert "github-read-all" in error


def test_post_rejects_extraneous_payload_field(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "file-sharing",
            "payload": {"path": "/tmp/ok.txt", "access": "READ", "extra": "no"},
        },
    )
    assert status == 400
    assert "extra" in json.loads(body)["error"]


# -- GET /permission-requests --


def test_get_returns_all_pending_requests(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    payloads = [
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
        },
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "y",
            "type": "file-sharing",
            "payload": {"path": "/tmp/visible.txt", "access": "READ"},
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
            "agent_id": _VALID_AGENT_ID,
            "rationale": "needs to read example data",
            "type": "file-sharing",
            "payload": {"path": target_path, "access": "READ"},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]

    approve_status, approve_body = _post_json(f"{base_url}/permission-requests/approve/{request_id}", None)
    assert approve_status == 200, approve_body
    response = json.loads(approve_body)
    assert response["request_id"] == request_id
    assert response["target"] == str(permissions_config_path)

    # The on-disk target was written with the effect applied. The
    # file-sharing effect adds *only* the per-file permission schema;
    # the scope (``latchkey-self``) is assumed to already exist in the
    # agent baseline, so the merged schemas should contain just the
    # one new entry.
    applied = json.loads(permissions_config_path.read_text())
    permission_name = _file_sharing_permission_name(target_path, "READ")
    assert applied["rules"] == [{_FILE_SHARING_SCOPE_NAME: [permission_name]}]
    assert permission_name in applied["schemas"]
    assert _FILE_SHARING_SCOPE_NAME not in applied["schemas"]

    # Pending request file was removed.
    pending_dir = latchkey_directory / "permission_requests" / "v3"
    assert list(pending_dir.iterdir()) == []


def test_approve_merges_predefined_into_existing_rules(
    node_extension: tuple[str, Path, Path],
) -> None:
    base_url, _latchkey_directory, permissions_config_path = node_extension
    # Seed the target with a pre-existing rule for the same scope + account.
    rule_key = account_scope_key("slack-api", "alice@example.com")
    permissions_config_path.write_text(json.dumps({"rules": [{rule_key: ["slack-read-all"]}]}))
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "wants more slack",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-write-all"], "account": "alice@example.com"},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    approve_status, _ = _post_json(f"{base_url}/permission-requests/approve/{request_id}", None)
    assert approve_status == 200
    applied = json.loads(permissions_config_path.read_text())
    # Permissions from both the seed and the new effect are unioned in
    # a single rule entry (same scope key).
    assert applied["rules"] == [{rule_key: ["slack-read-all", "slack-write-all"]}]


def test_approve_creates_target_when_missing(node_extension: tuple[str, Path, Path]) -> None:
    base_url, _latchkey_directory, permissions_config_path = node_extension
    assert not permissions_config_path.exists()
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"], "account": ""},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    approve_status, _ = _post_json(f"{base_url}/permission-requests/approve/{request_id}", None)
    assert approve_status == 200
    assert permissions_config_path.exists()
    applied = json.loads(permissions_config_path.read_text())
    # The unnamed default account is a real account: its key ends in the bare
    # separator and its schema pins the injected account to the empty string.
    default_key = account_scope_key("slack-api", "")
    assert applied["rules"] == [{default_key: ["slack-read-all"]}]
    assert applied["schemas"][default_key] == build_account_scope_schema("slack-api", "")


def test_approve_preserves_symlink_at_target_path(
    node_extension: tuple[str, Path, Path],
    tmp_path: Path,
) -> None:
    """Approving a request must not replace a symlinked target with a regular file.

    ``mngr latchkey link-permissions`` swings a per-agent opaque path
    into the canonical host permissions file via a symlink. If the
    extension wrote through ``rename(2)`` on the link itself, the
    symlink would be replaced by a literal file and subsequent agents
    sharing the canonical host file would silently desync from the
    granted permissions. This test asserts that the symlink survives.
    """
    base_url, _latchkey_directory, permissions_config_path = node_extension
    # Replace the (non-existent) target with a symlink pointing at a
    # canonical file elsewhere on disk. The canonical file starts
    # empty (no rules / no schemas) so we can verify both that the
    # rules landed underneath the symlink and that the link itself
    # survived the write.
    canonical_path = tmp_path / "canonical_permissions.json"
    canonical_path.write_text(json.dumps({"rules": []}))
    assert not permissions_config_path.exists()
    permissions_config_path.symlink_to(canonical_path)
    assert permissions_config_path.is_symlink()

    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"], "account": "alice@example.com"},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    approve_status, _ = _post_json(f"{base_url}/permission-requests/approve/{request_id}", None)
    assert approve_status == 200

    # Target path is still a symlink pointing at the canonical file.
    assert permissions_config_path.is_symlink()
    assert permissions_config_path.resolve() == canonical_path.resolve()
    # The merge landed on the canonical file underneath.
    applied = json.loads(canonical_path.read_text())
    assert applied["rules"] == [{account_scope_key("slack-api", "alice@example.com"): ["slack-read-all"]}]


def test_approve_404s_on_unknown_request_id(node_extension: tuple[str, Path, Path]) -> None:
    base_url, *_ = node_extension
    status, body = _post_json(f"{base_url}/permission-requests/approve/nope", None)
    assert status == 404
    assert "not found" in json.loads(body)["error"].lower()


def test_approve_with_path_override_recomputes_file_sharing_effect(
    node_extension: tuple[str, Path, Path],
) -> None:
    """A user-edited path in the approve body retargets the file-sharing grant.

    The agent requests one path; the user edits it before approving. The
    grant that lands must target the user's path -- the per-file schema
    name and pattern derive from the edited path, and the originally
    requested path must not appear in the applied permissions.
    """
    base_url, _latchkey_directory, permissions_config_path = node_extension
    requested_path = "/home/example/requested.txt"
    edited_path = "/home/example/Documents/Shared"
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "needs a file",
            "type": "file-sharing",
            "payload": {"path": requested_path, "access": "READ"},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]

    approve_status, approve_body = _post_json(
        f"{base_url}/permission-requests/approve/{request_id}",
        {"path": edited_path},
    )
    assert approve_status == 200, approve_body

    applied = json.loads(permissions_config_path.read_text())
    edited_name = _file_sharing_permission_name(edited_path, "READ")
    requested_name = _file_sharing_permission_name(requested_path, "READ")
    # The grant targets the edited path, not the originally requested one.
    assert applied["rules"] == [{_FILE_SHARING_SCOPE_NAME: [edited_name]}]
    assert edited_name in applied["schemas"]
    assert requested_name not in applied["schemas"]
    # The schema's URL pattern embeds the edited path under the WebDAV mount.
    pattern = applied["schemas"][edited_name]["properties"]["path"]["pattern"]
    assert edited_path in pattern
    assert requested_path not in pattern


def test_approve_with_path_override_preserves_requested_access_mode(
    node_extension: tuple[str, Path, Path],
) -> None:
    """Editing the path must not change the access mode fixed at request time."""
    base_url, _latchkey_directory, permissions_config_path = node_extension
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "needs to write",
            "type": "file-sharing",
            "payload": {"path": "/home/example/orig", "access": "WRITE"},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    approve_status, _ = _post_json(
        f"{base_url}/permission-requests/approve/{request_id}",
        {"path": "/home/example/edited"},
    )
    assert approve_status == 200
    applied = json.loads(permissions_config_path.read_text())
    # The recomputed schema keeps the WRITE access mode (write verbs present).
    write_name = _file_sharing_permission_name("/home/example/edited", "WRITE")
    assert write_name in applied["schemas"]
    methods = applied["schemas"][write_name]["properties"]["method"]["enum"]
    assert "PUT" in methods and "DELETE" in methods


def test_approve_rejects_path_override_for_predefined_request(
    node_extension: tuple[str, Path, Path],
) -> None:
    """A path override only makes sense for file-sharing; reject it elsewhere."""
    base_url, _latchkey_directory, permissions_config_path = node_extension
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "predefined",
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"], "account": "alice@example.com"},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    status, body = _post_json(
        f"{base_url}/permission-requests/approve/{request_id}",
        {"path": "/home/example/whatever"},
    )
    assert status == 400, body
    assert "path" in json.loads(body)["error"]
    # The grant was not applied (the request stays pending).
    assert not permissions_config_path.exists()


def test_approve_rejects_traversal_in_path_override(
    node_extension: tuple[str, Path, Path],
) -> None:
    """A ``..`` segment in the edited path is rejected just like at creation."""
    base_url, *_ = node_extension
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "file-sharing",
            "payload": {"path": "/home/example/ok.txt", "access": "READ"},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    status, body = _post_json(
        f"{base_url}/permission-requests/approve/{request_id}",
        {"path": "/home/example/../../etc/shadow"},
    )
    assert status == 400, body
    assert "traversal" in json.loads(body)["error"].lower()


def test_approve_rejects_path_override_outside_mount_roots(
    node_extension: tuple[str, Path, Path],
) -> None:
    """An edited path outside the WebDAV mounts is rejected on approve, same as at creation."""
    base_url, _latchkey_directory, permissions_config_path = node_extension
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "file-sharing",
            "payload": {"path": "/home/example/ok.txt", "access": "READ"},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    status, body = _post_json(
        f"{base_url}/permission-requests/approve/{request_id}",
        {"path": "/etc/shadow"},
    )
    assert status == 400, body
    assert "shared root" in json.loads(body)["error"]
    # The grant was not applied (the request stays pending).
    assert not permissions_config_path.exists()


def test_approve_rejects_extraneous_field_in_override_body(
    node_extension: tuple[str, Path, Path],
) -> None:
    """Only ``path`` is allowed in the approve override body."""
    base_url, *_ = node_extension
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "file-sharing",
            "payload": {"path": "/home/example/ok.txt", "access": "READ"},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    status, body = _post_json(
        f"{base_url}/permission-requests/approve/{request_id}",
        {"path": "/home/example/ok.txt", "access": "WRITE"},
    )
    assert status == 400, body
    assert "access" in json.loads(body)["error"]


def test_delete_removes_pending_request(node_extension: tuple[str, Path, Path]) -> None:
    base_url, latchkey_directory, _permissions_config_path = node_extension
    create_status, create_body = _post_json(
        f"{base_url}/permission-requests",
        {
            "agent_id": _VALID_AGENT_ID,
            "rationale": "x",
            "type": "file-sharing",
            "payload": {"path": "/tmp/data.txt", "access": "WRITE"},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    status, _, _ = _http(f"{base_url}/permission-requests/{request_id}", method="DELETE")
    assert status == 204
    pending_dir = latchkey_directory / "permission_requests" / "v3"
    assert list(pending_dir.iterdir()) == []


# -- POST /permission-requests: custom-service --


def _custom_service_body(payload: object, rationale: str = "needs the widget API") -> dict[str, object]:
    # A well-formed payload names a scheme; tests about the rest of the payload
    # get https unless they say otherwise.
    if isinstance(payload, dict) and "scheme" not in payload:
        payload = {"scheme": "https", **payload}
    return {
        "agent_id": _VALID_AGENT_ID,
        "rationale": rationale,
        "type": "custom-service",
        "payload": payload,
    }


def test_post_creates_custom_service_request_with_empty_effect(
    node_extension: tuple[str, Path, Path],
) -> None:
    base_url, latchkey_directory, permissions_config_path = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        _custom_service_body(
            {
                "domain": "api.example.com",
                "login": {
                    "url": "https://api.example.com/login",
                    "flow": "cookie-capture",
                    "flow_params": {"cookieKeys": ["session"], "cookieUrl": "https://api.example.com/"},
                },
            }
        ),
    )
    assert status == 201
    parsed = json.loads(body)
    assert parsed["request_type"] == "custom-service"
    # The login fields are stored as sent: they are latchkey's own flags, and
    # the desktop registers them verbatim.
    assert parsed["payload"] == {
        "domain": "api.example.com",
        "scheme": "https",
        "login": {
            "url": "https://api.example.com/login",
            "flow": "cookie-capture",
            "flow_params": {"cookieKeys": ["session"], "cookieUrl": "https://api.example.com/"},
        },
    }
    assert parsed["target"] == str(permissions_config_path)
    # A service being created has no accounts to pick from, so there is nothing
    # to grant until the approval names one: a bare ``/approve`` must be a no-op
    # rather than silently granting every account.
    assert parsed["effect"] == {}
    stored = next((latchkey_directory / "permission_requests" / "v3").iterdir())
    assert json.loads(stored.read_text()) == parsed


def test_post_creates_custom_service_request_without_login(
    node_extension: tuple[str, Path, Path],
) -> None:
    # No cookie flow does not mean no credentials: the user supplies a token
    # through the dialog's existing credential form instead.
    base_url, _, _ = node_extension
    status, body = _post_json(f"{base_url}/permission-requests", _custom_service_body({"domain": "example.com"}))
    assert status == 201
    assert json.loads(body)["payload"] == {
        "domain": "example.com",
        "scheme": "https",
        "login": None,
    }


@pytest.mark.parametrize(
    # In order: empty, the shapes a URL carries that a bare hostname must not
    # (scheme, path, port, userinfo, query), what the derived service name
    # cannot represent (a wildcard, an underscore -- load-bearing, since the
    # name swaps ``_`` for ``.`` -- and an IPv6 literal), and the gateway's own
    # address.
    "domain",
    [
        "",
        " example.com ",
        "https://example.com",
        "example.com/v1",
        "example.com:8443",
        "user@example.com",
        "example.com?q=1",
        "*.example.com",
        "a_b.com",
        "[::1]",
        "latchkey-self.invalid",
    ],
)
def test_post_rejects_bad_custom_service_domain(node_extension: tuple[str, Path, Path], domain: str) -> None:
    base_url, _, _ = node_extension
    status, body = _post_json(f"{base_url}/permission-requests", _custom_service_body({"domain": domain}))
    assert status == 400, body
    assert b"domain" in body


def test_post_accepts_a_custom_service_that_already_exists(tmp_path: Path) -> None:
    """A second workspace asking for an origin someone already connected.

    Its own gateway has no service for the origin, so this is the only request
    it can make, and refusing it would leave that workspace no way in. Whether
    the desktop already has the service is the desktop's question, answered at
    approve time by connecting the workspace to the service as it is.

    Staged against a *materialized* catalog, because that is the only place a
    ``custom_`` entry ever appears: the shipped catalog has none by
    construction.
    """
    with _staged_extension(
        tmp_path,
        extra_services={
            "custom_https_widgets_example_com": [
                {"scope": "custom_https_widgets_example_com", "display_name": "widgets.example.com", "permissions": []}
            ]
        },
    ) as base_url:
        status, body = _post_json(
            f"{base_url}/permission-requests", _custom_service_body({"domain": "widgets.example.com"})
        )
        assert status == 201, body
        assert json.loads(body)["effect"] == {}


def test_post_rejects_agent_supplied_display_text(node_extension: tuple[str, Path, Path]) -> None:
    # A custom service is labelled by its domain, which cannot misdescribe what
    # it reaches. An agent that could name it could present a tracker as Drive,
    # so a smuggled label is refused rather than quietly ignored.
    base_url, _, _ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        _custom_service_body({"domain": "example.com", "display_name": "Google Drive"}),
    )
    assert status == 400
    assert b"display_name" in body


@pytest.mark.parametrize(
    "login,expected_fragment",
    [
        # The object is one sign-in: a flow needs a URL and parameters.
        ({"url": "https://example.com/l"}, b"'flow' is required"),
        ({"flow": "cookie-capture", "flow_params": {"cookieKeys": ["s"]}}, b"'url' is required"),
        ({"url": "https://example.com/l", "flow": "cookie-capture"}, b"'flow_params' is required"),
        ({"url": "https://example.com/l", "flow": "magic", "flow_params": {}}, b"cookie-capture, token-capture"),
        # Every URL stays on the domain, whichever scheme it uses.
        (
            {"url": "https://evil.test/l", "flow": "cookie-capture", "flow_params": {"cookieKeys": ["s"]}},
            b"'url' must be on example.com",
        ),
        (
            {
                "url": "https://example.com/l",
                "flow": "cookie-capture",
                "flow_params": {"cookieKeys": ["s"], "cookieUrl": "https://evil.test/"},
            },
            b"'flow_params.cookieUrl' must be on example.com",
        ),
        (
            {
                "url": "https://example.com/l",
                "flow": "token-capture",
                "flow_params": {"tokenUrl": "https://evil.test/session", "tokenField": "t"},
            },
            b"'flow_params.tokenUrl' must be on example.com",
        ),
        # The parameters are latchkey's schema for the flow, no more and no less.
        (
            {"url": "https://example.com/l", "flow": "cookie-capture", "flow_params": {"cookieKeys": []}},
            b"at least one cookie",
        ),
        (
            {
                "url": "https://example.com/l",
                "flow": "cookie-capture",
                "flow_params": {"cookieUrl": "https://example.com/"},
            },
            b"cookieKeys",
        ),
        (
            {
                "url": "https://example.com/l",
                "flow": "cookie-capture",
                "flow_params": {"cookieKeys": ["s"], "cookie_keys": ["s"]},
            },
            b"cookie_keys",
        ),
        (
            {
                "url": "https://example.com/l",
                "flow": "token-capture",
                "flow_params": {"tokenUrl": "https://example.com/s"},
            },
            b"tokenField",
        ),
        (
            {
                "url": "https://example.com/l",
                "flow": "token-capture",
                "flow_params": {"tokenUrl": "https://example.com/s", "tokenField": "t", "header": "X-Token: nope"},
            },
            b"{token}",
        ),
        (
            {
                "url": "https://example.com/l",
                "flow": "token-capture",
                "flow_params": {"tokenUrl": "https://example.com/s", "tokenField": "t", "header": "Bearer {token}"},
            },
            b"header line",
        ),
        # The old field names are refused, not silently ignored.
        (
            {"login_url": "https://example.com/l", "cookie_url": "https://example.com/", "cookie_keys": ["s"]},
            b"login_url",
        ),
    ],
)
def test_post_rejects_bad_custom_service_login(
    node_extension: tuple[str, Path, Path], login: dict[str, object], expected_fragment: bytes
) -> None:
    base_url, _, _ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests", _custom_service_body({"domain": "example.com", "login": login})
    )
    assert status == 400, body
    assert expected_fragment in body


def test_post_rejects_flat_login_fields(node_extension: tuple[str, Path, Path]) -> None:
    # The same three things outside the ``login`` object are unknown payload
    # fields, refused rather than picked up.
    base_url, _, _ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        _custom_service_body(
            {"domain": "example.com", "login_url": "https://example.com/l", "login_flow": "cookie-capture"}
        ),
    )
    assert status == 400, body
    assert b"login_url" in body


def test_post_accepts_a_token_capture_login(node_extension: tuple[str, Path, Path]) -> None:
    # The second generic flow, with its optional header: accepted and stored as
    # sent, so latchkey reads back exactly the parameters its own CLI takes.
    base_url, _, _ = node_extension
    params = {
        "tokenUrl": "https://app.example.com/api/auth/session",
        "tokenField": "data.accessToken",
        "header": "X-Token: {token}",
    }
    status, body = _post_json(
        f"{base_url}/permission-requests",
        _custom_service_body(
            {
                "domain": "example.com",
                "login": {"url": "https://app.example.com/auth/login", "flow": "token-capture", "flow_params": params},
            }
        ),
    )
    assert status == 201, body
    assert json.loads(body)["payload"]["login"]["flow_params"] == params


def test_approve_custom_service_applies_nothing_and_drops_the_record(
    node_extension: tuple[str, Path, Path],
) -> None:
    """The grant is the desktop's to write, through ``/permissions/rules``.

    The account to gate the rule on does not exist until the desktop's sign-in
    produces it, so the record's effect is empty and approving it as-is only
    removes the record -- it must never grant the service to every account.
    """
    base_url, latchkey_directory, permissions_config_path = node_extension
    status, body = _post_json(f"{base_url}/permission-requests", _custom_service_body({"domain": "example.com"}))
    assert status == 201
    request_id = json.loads(body)["request_id"]

    status, _ = _post_json(f"{base_url}/permission-requests/approve/{request_id}", None)
    assert status == 200
    assert json.loads(permissions_config_path.read_text())["rules"] == []
    assert list((latchkey_directory / "permission_requests" / "v3").iterdir()) == []


def test_approve_custom_service_takes_no_override(node_extension: tuple[str, Path, Path]) -> None:
    # Nothing in an approve body could change what the desktop writes, so an
    # account named here is refused rather than silently ignored.
    base_url, _, _ = node_extension
    status, body = _post_json(f"{base_url}/permission-requests", _custom_service_body({"domain": "example.com"}))
    request_id = json.loads(body)["request_id"]
    status, body = _post_json(f"{base_url}/permission-requests/approve/{request_id}", {"account": "me@example.com"})
    assert status == 400
    assert b"account" in body


def test_post_custom_service_requires_a_scheme(node_extension: tuple[str, Path, Path]) -> None:
    base_url, _, _ = node_extension
    body_without_scheme = {**_custom_service_body({"domain": "example.com"}), "payload": {"domain": "example.com"}}
    status, body = _post_json(f"{base_url}/permission-requests", body_without_scheme)
    assert status == 400, body
    assert b"payload.'scheme' is required" in body


def test_post_custom_service_accepts_http_and_an_http_login_for_it(node_extension: tuple[str, Path, Path]) -> None:
    base_url, _, _ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        _custom_service_body(
            {
                "domain": "intranet.acme-widgets.com",
                "scheme": "http",
                "login": {
                    "url": "http://intranet.acme-widgets.com/login",
                    "flow": "cookie-capture",
                    "flow_params": {"cookieKeys": ["session"], "cookieUrl": "http://intranet.acme-widgets.com/"},
                },
            }
        ),
    )
    assert status == 201, body
    assert json.loads(body)["payload"]["scheme"] == "http"


def test_post_custom_service_accepts_an_http_login_for_an_https_service(
    node_extension: tuple[str, Path, Path],
) -> None:
    # The sign-in page's scheme is the user's business; the domain check is
    # what keeps the sign-in on the service.
    base_url, _, _ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests",
        _custom_service_body(
            {
                "domain": "example.com",
                "login": {
                    "url": "http://example.com/login",
                    "flow": "cookie-capture",
                    "flow_params": {"cookieKeys": ["session"]},
                },
            }
        ),
    )
    assert status == 201, body
    assert json.loads(body)["payload"]["login"]["url"] == "http://example.com/login"


@pytest.mark.parametrize("scheme", ["ftp", "", 7, "HTTPS://"])
def test_post_rejects_a_custom_service_scheme_that_is_not_http_or_https(
    node_extension: tuple[str, Path, Path], scheme: object
) -> None:
    base_url, _, _ = node_extension
    status, body = _post_json(
        f"{base_url}/permission-requests", _custom_service_body({"domain": "example.com", "scheme": scheme})
    )
    assert status == 400, body
    assert b"payload.'scheme'" in body


@pytest.mark.parametrize(
    "domain,expected",
    # What a private network calls its services: a single label, a private
    # suffix, an address -- taken as the platform URL parser spells them.
    [
        ("intranet", "intranet"),
        ("Vault.INTERNAL", "vault.internal"),
        ("foo.localhost", "foo.localhost"),
        ("10.0.0.5", "10.0.0.5"),
    ],
)
def test_post_accepts_the_hostnames_a_private_network_uses(
    node_extension: tuple[str, Path, Path], domain: str, expected: str
) -> None:
    base_url, _, _ = node_extension
    status, body = _post_json(f"{base_url}/permission-requests", _custom_service_body({"domain": domain}))
    assert status == 201, body
    assert json.loads(body)["payload"]["domain"] == expected

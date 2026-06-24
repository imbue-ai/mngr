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
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
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
    assert parsed["payload"] == {"scope": "slack-api", "permissions": ["slack-read-all"]}
    assert parsed["target"] == str(permissions_config_path)
    assert parsed["effect"] == {"rules": [{"slack-api": ["slack-read-all"]}]}
    # The persisted file should match the response on disk.
    stored = next((latchkey_directory / "permission_requests" / "v2").iterdir())
    assert json.loads(stored.read_text()) == parsed


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
    persisted_dir = latchkey_directory / "permission_requests" / "v2"
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
            "payload": {"scope": scope, "permissions": ["any"]},
        },
    )
    assert status == 201, body
    parsed = json.loads(body)
    assert parsed["effect"] == {"rules": [{scope: ["any"]}]}


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
            "agent_id": _VALID_AGENT_ID,
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
            "agent_id": _VALID_AGENT_ID,
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
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
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
    assert applied["rules"] == [{"slack-api": ["slack-read-all"]}]


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
            "payload": {"scope": "slack-api", "permissions": ["slack-read-all"]},
        },
    )
    assert create_status == 201
    request_id = json.loads(create_body)["request_id"]
    status, body = _post_json(
        f"{base_url}/permission-requests/approve/{request_id}",
        {"path": "/home/example/whatever"},
    )
    assert status == 400, body
    assert "file-sharing" in json.loads(body)["error"]
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
    pending_dir = latchkey_directory / "permission_requests" / "v2"
    assert list(pending_dir.iterdir()) == []

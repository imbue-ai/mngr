"""Test helpers for ``mngr_latchkey`` unit + integration tests.

Per CLAUDE.md, do not create tests for this module itself; the helpers
are exercised through the tests that import them.
"""

import io
import json
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
from collections.abc import Generator
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

import psutil
import pytest
from pydantic import JsonValue
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import LATCHKEY_AUTH_OPTION_BROWSER
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import LatchkeyJwtMintError
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from imbue.mngr_latchkey.core import ServiceAccountCredential
from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.migrations.interface import PermissionsMigrationContext
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import permissions_path_for_host

_POLL_INTERVAL_SECONDS: Final[float] = 0.05

# Upper bound for the tests' process-state polls (wait_for_process_exit and
# the test modules that import this). Purely a worst-case ceiling
# (every poll returns as soon as its condition holds): spawning and tearing
# down real subprocesses has been seen to exceed a 5s bound on a heavily
# loaded machine, which is noise, not a bug in the code under test.
PROCESS_WAIT_TIMEOUT_SECONDS: Final[float] = 15.0


def _has_process_exited(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def wait_for_process_exit(pid: int, timeout: float = PROCESS_WAIT_TIMEOUT_SECONDS) -> bool:
    """Poll until ``pid`` is gone or has become a zombie.

    Zombies count as "exited": the subprocesses these tests spawn are children
    of the test process whose ``Popen`` is never waited on, so a terminated
    child lingers as a zombie until it is reaped. The session-end leak check
    (``session_cleanup`` in libs/mngr's conftest) ignores zombies but fails the
    run over a live child, and pytest reports that against whichever test ran
    last -- so every test that spawns a child waits for it before returning.
    """
    return poll_until(lambda: _has_process_exited(pid), timeout=timeout, poll_interval=_POLL_INTERVAL_SECONDS)


class FakeLatchkey(Latchkey):
    """Test double for :class:`Latchkey` that never spawns subprocesses.

    Each method either returns the configured fake value or raises the
    configured fake error so individual tests can assert the degradation
    semantics of callers that depend on ``Latchkey``.
    """

    _gateway_url: str | None = PrivateAttr(default=None)
    _gateway_error: BaseException | None = PrivateAttr(default=None)
    _password: str | None = PrivateAttr(default=None)
    _password_error: BaseException | None = PrivateAttr(default=None)
    _jwt: str | None = PrivateAttr(default=None)
    _jwt_error: BaseException | None = PrivateAttr(default=None)
    _is_stopped: bool = PrivateAttr(default=False)
    # Every ``services_info`` call as ``(service_name, is_offline)``, in order, so
    # tests can assert which services a caller probed and whether it hit the
    # network -- the difference between a probe that renews a credential and one
    # that only reads the store.
    _services_info_calls: list[tuple[str, bool]] = PrivateAttr(default_factory=list)
    # Every ``auth_list`` call's ``is_offline``, in order, for the same reason:
    # an offline list is a local read of the store, a non-offline one validates
    # (and may refresh) every stored account against its third party.
    _auth_list_calls: list[bool] = PrivateAttr(default_factory=list)
    _accounts_by_service: dict[str, tuple[ServiceAccountCredential, ...]] = PrivateAttr(default_factory=dict)
    _auth_list_error: BaseException | None = PrivateAttr(default=None)
    # What ``import_chrome_browser_state`` reports, and how often it was asked.
    _import_chrome_result: tuple[bool, str] = PrivateAttr(default=(True, ""))
    _import_chrome_call_count: int = PrivateAttr(default=0)

    # Auth / services-info doubles. The credential-grant flow now lives in the
    # real ``Latchkey.auth_browser`` (tested against a fake binary in
    # core_test.py); the fake only needs non-spawning stand-ins so it never
    # shells out. ``services_info`` reports a browser-capable MISSING service.
    _service_info: LatchkeyServiceInfo = PrivateAttr(
        default=LatchkeyServiceInfo(
            credential_status=CredentialStatus.MISSING,
            auth_options=frozenset({LATCHKEY_AUTH_OPTION_BROWSER}),
            set_credentials_example=None,
        )
    )

    def configure(
        self,
        *,
        gateway_url: str | None = None,
        gateway_error: BaseException | None = None,
        password: str | None = None,
        password_error: BaseException | None = None,
        jwt: str | None = None,
        jwt_error: BaseException | None = None,
        service_info: LatchkeyServiceInfo | None = None,
        accounts_by_service: dict[str, tuple[ServiceAccountCredential, ...]] | None = None,
        auth_list_error: BaseException | None = None,
        import_chrome_result: tuple[bool, str] | None = None,
    ) -> None:
        """Install the given doubles, leaving everything not passed as it was.

        Additive rather than wholesale, so a fake built by
        :func:`make_full_fake_latchkey` can have one more behaviour layered on
        without losing the ones it was built with.
        """
        if gateway_url is not None:
            self._gateway_url = gateway_url
        if gateway_error is not None:
            self._gateway_error = gateway_error
        if password is not None:
            self._password = password
        if password_error is not None:
            self._password_error = password_error
        if jwt is not None:
            self._jwt = jwt
        if jwt_error is not None:
            self._jwt_error = jwt_error
        if auth_list_error is not None:
            self._auth_list_error = auth_list_error
        if service_info is not None:
            # What every ``services_info`` call reports, including the stored
            # accounts the per-account permission dialog offers.
            self._service_info = service_info
        if accounts_by_service is not None:
            # What ``auth_list`` reports: the stored accounts per service.
            self._accounts_by_service = accounts_by_service
        if import_chrome_result is not None:
            self._import_chrome_result = import_chrome_result

    @property
    def services_info_calls(self) -> tuple[tuple[str, bool], ...]:
        """Every ``services_info`` call so far, as ``(service_name, is_offline)``."""
        return tuple(self._services_info_calls)

    @property
    def auth_list_calls(self) -> tuple[bool, ...]:
        """Every ``auth_list`` call so far, as its ``is_offline``."""
        return tuple(self._auth_list_calls)

    def services_info(self, service_name: str, *, is_offline: bool = False) -> LatchkeyServiceInfo:
        self._services_info_calls.append((service_name, is_offline))
        return self._service_info

    def auth_list(self, *, is_offline: bool = False) -> dict[str, tuple[ServiceAccountCredential, ...]]:
        self._auth_list_calls.append(is_offline)
        if self._auth_list_error is not None:
            raise self._auth_list_error
        return dict(self._accounts_by_service)

    def auth_prepare(self, service_name: str, client_id: str, client_secret: str) -> tuple[bool, str]:
        del service_name, client_id, client_secret
        return (True, "")

    def auth_prepare_redirect_uri(self, service_name: str, redirect_uri: str) -> tuple[bool, str]:
        del service_name, redirect_uri
        return (True, "")

    def auth_clear(
        self,
        service_name: str,
        *,
        account: str | None = None,
        is_all: bool = False,
    ) -> tuple[bool, str]:
        del service_name, account, is_all
        return (True, "")

    def auth_browser_login(
        self, service_name: str, *, is_ephemeral: bool = False, account: str | None = None
    ) -> tuple[bool, str]:
        del service_name, is_ephemeral, account
        return (True, "")

    def auth_browser(
        self, service_name: str, *, is_ephemeral: bool = False, account: str | None = None
    ) -> tuple[bool, str]:
        del service_name, is_ephemeral, account
        return (True, "")

    def add_account(self, service_name: str) -> tuple[bool, str]:
        del service_name
        return (True, "")

    @property
    def import_chrome_call_count(self) -> int:
        """How many times ``import_chrome_browser_state`` has been called."""
        return self._import_chrome_call_count

    def import_chrome_browser_state(self) -> tuple[bool, str]:
        self._import_chrome_call_count += 1
        return self._import_chrome_result

    def initialize(self) -> None:
        # No-op: the real implementation runs ``latchkey --version`` and
        # reconciles the on-disk gateway record, neither of which we want
        # in unit tests. Subclasses inherit the ``_is_initialized`` private
        # attribute so we mark ourselves initialized for any downstream
        # invariant check.
        self._is_initialized = True

    def start_gateway(self, concurrency_group: ConcurrencyGroup) -> int:
        # The fake never actually spawns; the CG argument is accepted
        # only to mirror the production signature.
        del concurrency_group
        if self._gateway_error is not None:
            raise self._gateway_error
        if self._gateway_url is None:
            raise LatchkeyError("FakeLatchkey: configure gateway_url before calling start_gateway")
        parts = urlsplit(self._gateway_url)
        if parts.hostname is None or parts.port is None:
            raise LatchkeyError(f"FakeLatchkey: unparseable url: {self._gateway_url}")
        return parts.port

    def derive_gateway_password(self) -> str:
        if self._password_error is not None:
            raise self._password_error
        if self._password is None:
            raise LatchkeyJwtMintError("FakeLatchkey: configure password before calling derive_gateway_password")
        return self._password

    def create_permissions_override_jwt(self, permissions_path: Path) -> str:
        del permissions_path
        if self._jwt_error is not None:
            raise self._jwt_error
        if self._jwt is None:
            raise LatchkeyJwtMintError("FakeLatchkey: configure jwt before calling create_permissions_override_jwt")
        return self._jwt

    def create_admin_permissions_jwt(self) -> str:
        # The real mint shells out *and* materializes the admin permissions
        # file; the fake does neither, so callers that only need admin
        # credentials (the desktop client's gateway client) stay off disk.
        if self._jwt_error is not None:
            raise self._jwt_error
        if self._jwt is None:
            raise LatchkeyJwtMintError("FakeLatchkey: configure jwt before calling create_admin_permissions_jwt")
        return self._jwt

    def stop_gateway(self) -> None:
        # Record the call so tests can verify ``mngr latchkey forward``'s
        # coupled-lifetime shutdown semantics without spawning a real
        # gateway subprocess.
        self._is_stopped = True

    @property
    def is_stopped(self) -> bool:
        return self._is_stopped


def make_full_fake_latchkey(latchkey_directory: Path) -> FakeLatchkey:
    """Return a :class:`FakeLatchkey` with every method's success path pre-configured."""
    fake = FakeLatchkey(latchkey_directory=latchkey_directory)
    fake.configure(
        gateway_url="http://127.0.0.1:55555",
        password="hunter2",
        jwt="header.payload.signature",
    )
    return fake


# The credential-header corpus, run case for case against both validators
# (`custom_services_test.py` for the Python one, `permission_requests_test.py` for
# the gateway extension's), as the domain grammar is, so the two cannot drift.
ACCEPTED_CREDENTIAL_HEADERS = (
    "Authorization: Bearer {token}",
    "X-Api-Key: {token}",
    "x-api-key:{token}",
    "Authorization: Token {token}",
    "Cookie: session={token}; theme=dark",
    "Authorization: Bearer\t{token}",
)
REJECTED_CREDENTIAL_HEADERS = (
    "",
    "X-Api-Key",
    "X-Api-Key: nope",
    "Bearer {token}",
    "{token}: X-Api-Key",
    ": {token}",
    "X Api Key: {token}",
    "X-Api-Key\n: {token}",
    "X-Api-Key: {token}\n",
    "X-Api-Key: {token}\r\nHost: evil.example",
    "Host: {token}",
    "host: {token}",
    "X-Latchkey-Gateway-Password: {token}",
    "x-latchkey-anything: {token}",
)

# The credential-instructions corpus, run against both validators the same way.
# The 500 astral characters are 1000 UTF-16 units: accepted only if the gateway
# counts code points, as Python does.
ACCEPTED_CREDENTIAL_INSTRUCTIONS = (
    pytest.param(
        "In ClickUp: avatar (bottom left) -> Settings -> Apps.\nGenerate an API token and copy it.", id="multi-line"
    ),
    pytest.param("\U0001f511" * 500, id="500-astral-code-points"),
)
# Each with a fragment both validators' messages contain.
REJECTED_CREDENTIAL_INSTRUCTIONS = (
    pytest.param("", "must be a non-empty string", id="empty"),
    pytest.param("  \n ", "must be a non-empty string", id="blank"),
    pytest.param("x" * 501, "at most 500 characters", id="501-code-points"),
)

# The ``ar`` container a ``.deb`` is: a magic line, then 60-byte member headers
# each followed by the member's bytes (padded to an even length).
_AR_MAGIC = b"!<arch>\n"
_AR_HEADER_SIZE = 60


def read_deb_members(content: bytes) -> dict[str, bytes]:
    """The members of a ``.deb`` (``debian-binary``, ``control.tar.gz``, ``data.tar.gz``), in archive order."""
    assert content.startswith(_AR_MAGIC), "not an ar archive"
    members: dict[str, bytes] = {}
    offset = len(_AR_MAGIC)
    while offset < len(content):
        header = content[offset : offset + _AR_HEADER_SIZE]
        name = header[:16].decode("ascii").strip()
        size = int(header[48:58].decode("ascii").strip())
        members[name] = content[offset + _AR_HEADER_SIZE : offset + _AR_HEADER_SIZE + size]
        offset += _AR_HEADER_SIZE + size + (size % 2)
    return members


def read_deb_tar(content: bytes, member_name: str) -> tarfile.TarFile:
    return tarfile.open(fileobj=io.BytesIO(read_deb_members(content)[member_name]), mode="r:gz")


def extract_deb_data(content: bytes, destination: Path) -> None:
    """Unpack the package's files under ``destination``, the way dpkg would under ``/``."""
    with read_deb_tar(content, "data.tar.gz") as tar:
        tar.extractall(destination, filter="data")


def read_deb_control_field(content: bytes, field_name: str) -> str:
    with read_deb_tar(content, "control.tar.gz") as tar:
        control_file = tar.extractfile("./control")
        assert control_file is not None
        control = control_file.read().decode("utf-8")
    for line in control.splitlines():
        if line.startswith(f"{field_name}: "):
            return line.removeprefix(f"{field_name}: ")
    raise AssertionError(f"no {field_name} field in the control file:\n{control}")


# The desktop a test's migrations run as: what grants from before grants named a
# desktop are attributed to.
MIGRATING_DEVICE_ID: Final[DesktopDeviceId] = DesktopDeviceId("host-migrating-desktop-5170")
MIGRATION_CONTEXT: Final[PermissionsMigrationContext] = PermissionsMigrationContext(device_id=MIGRATING_DEVICE_ID)


def rule_keys_of_permissions_json(permissions_json: str | None) -> list[str]:
    """The key of every rule in a policy, in order: which grants it carries, as a migration leaves them."""
    assert permissions_json is not None
    return [key for rule in json.loads(permissions_json)["rules"] for key in rule]


def permissions_config_holding_grants(
    *grants: tuple[str, tuple[str, ...], dict[str, JsonValue]],
) -> LatchkeyPermissionsConfig:
    """The policy holding ``grants`` in order, each the ``(rule_key, permissions, schemas)`` a grant builder returns."""
    schemas: dict[str, JsonValue] = {}
    for _rule_key, _permissions, grant_schemas in grants:
        schemas.update(grant_schemas)
    return LatchkeyPermissionsConfig(
        rules=tuple({rule_key: list(permissions)} for rule_key, permissions, _schemas in grants),
        schemas=schemas,
    )


def write_raw_host_permissions(data_dir: Path, host_id: HostId, permissions_json: str) -> Path:
    """Make ``permissions_json``, verbatim, this computer's policy for ``host_id``, and return where it landed."""
    path = permissions_path_for_host(data_dir, host_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(permissions_json)
    return path


def _node_extension_driver_script(extension_path: Path, permissions_config_path: Path | None) -> str:
    # A request the extension leaves alone is answered by a stand-in for the
    # next extension, so a test can tell a request that passed through from
    # one that was handled. The context is what the gateway hands an extension:
    # the permissions file the caller's credentials name, when the test says.
    context = {} if permissions_config_path is None else {"permissionsConfigPath": str(permissions_config_path)}
    return f"""
import http from 'node:http';
import handler from {json.dumps(extension_path.as_uri())};
const context = Object.freeze({json.dumps(context)});
const server = http.createServer((request, response) => {{
  handler(request, response, context).then((handled) => {{
    if (!handled && !response.headersSent) {{
      response.writeHead(200, {{'Content-Type': 'application/json'}});
      response.end(JSON.stringify({{served_locally: true, path: request.url}}));
    }}
  }});
}});
server.listen(0, '127.0.0.1', () => {{
  process.stdout.write(`PORT=${{server.address().port}}\\n`);
}});
process.on('SIGTERM', () => server.close(() => process.exit(0)));
"""


@contextmanager
def node_extension_gateway(
    extension_path: Path, env: Mapping[str, str], permissions_config_path: Path | None = None
) -> Generator[str, None, None]:
    """Serve one gateway extension from a node process with ``env``, yielding its base URL.

    A request the extension does not handle is answered ``{"served_locally": true, "path": ...}``.
    ``permissions_config_path`` is handed to the extension as the caller's context, as the gateway
    hands it the permissions file the caller's credentials name.
    """
    node_binary = shutil.which("node")
    assert node_binary is not None
    process = subprocess.Popen(
        [
            node_binary,
            "--input-type=module",
            "-e",
            _node_extension_driver_script(extension_path, permissions_config_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PATH": "/usr/bin:/bin", **env},
    )
    try:
        assert process.stdout is not None
        line = process.stdout.readline().strip()
        assert line.startswith("PORT="), process.stderr.read() if process.stderr is not None else ""
        yield f"http://127.0.0.1:{int(line.removeprefix('PORT='))}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)


def http_request_with_headers(
    url: str, method: str = "GET", headers: dict[str, str] | None = None, body: bytes | None = None
) -> tuple[int, dict[str, str], bytes]:
    """Send one HTTP request and return its status, lower-cased response headers and body, an error status included."""
    request = urllib.request.Request(url, method=method, headers=headers or {}, data=body)
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return (
                int(response.status),
                {name.lower(): value for name, value in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as error:
        return int(error.code), {name.lower(): value for name, value in error.headers.items()}, error.read()


def http_request(
    url: str, method: str = "GET", headers: dict[str, str] | None = None, body: bytes | None = None
) -> tuple[int, bytes]:
    """Send one HTTP request and return its status and body, an error status included."""
    status, _response_headers, response_body = http_request_with_headers(url, method, headers, body)
    return status, response_body

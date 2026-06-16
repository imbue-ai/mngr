"""Shared test helpers for the latchkey request-event handlers.

These factory functions and test doubles are imported by both the handler unit tests
(``*_test.py``) and the end-to-end dispatcher integration tests
(``test_latchkey_handlers.py``), so they live in a public, explicitly
imported module rather than in any single test file.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Final

import httpx
from pydantic import Field
from starlette.testclient import TestClient

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.handlers.file_sharing import FileSharingGrantHandler
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.mngr.primitives import AgentId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog

HttpxHandler: Final = Callable[[httpx.Request], httpx.Response]


def make_recording_binary(tmp_path: Path, name: str, *, exit_code: int = 0, stderr: str = "") -> Path:
    """Build a fake binary that appends its argv to a report file and exits."""
    script = tmp_path / name
    report_path = tmp_path / f"{name}_report.jsonl"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"report = {str(report_path)!r}\n"
        "with open(report, 'a') as f:\n"
        "    f.write(json.dumps({'argv': sys.argv[1:], 'env_LATCHKEY_DIRECTORY': os.environ.get('LATCHKEY_DIRECTORY', '')}) + '\\n')\n"
        f"if {stderr!r}:\n"
        f"    sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return script


def read_recording(report_path: Path) -> list[dict[str, list[str] | str]]:
    """Parse the JSONL recording emitted by ``make_recording_binary``."""
    if not report_path.exists():
        return []
    parsed: list[dict[str, list[str] | str]] = []
    for line in report_path.read_text().splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        argv_raw = raw["argv"]
        env_raw = raw["env_LATCHKEY_DIRECTORY"]
        assert isinstance(argv_raw, list)
        assert all(isinstance(a, str) for a in argv_raw)
        assert isinstance(env_raw, str)
        parsed.append({"argv": [str(a) for a in argv_raw], "env_LATCHKEY_DIRECTORY": env_raw})
    return parsed


SLACK_SERVICE_INFO = ServicePermissionInfo(
    name="slack",
    scope="slack-api",
    display_name="Slack",
    permission_schemas=(
        "any",
        "slack-read-all",
        "slack-write-all",
        "slack-chat-read",
    ),
)


SLACK_AVAILABLE_PAYLOAD: dict[str, object] = {
    "slack": [
        {
            "scope": "slack-api",
            "display_name": "Slack",
            "description": "Any interaction with the Slack API.",
            "permissions": [
                {"name": "slack-read-all", "description": "All read operations across the Slack API."},
                {"name": "slack-write-all"},
                {"name": "slack-chat-read"},
            ],
        },
    ],
}


def build_slack_services_catalog() -> ServicesCatalog:
    """Return a :class:`ServicesCatalog` pre-seeded with the Slack fixture.

    Uses an explicit catalog payload so we don't depend on the real
    ``services.json`` data file.
    """
    return ServicesCatalog.from_catalog_payload(SLACK_AVAILABLE_PAYLOAD)


DEFAULT_AUTH_OPTIONS_JSON: str = json.dumps(["browser", "set"])
DEFAULT_SET_EXAMPLE: str = 'latchkey auth set slack -H "Authorization: Bearer xoxb-your-token"'


def make_latchkey_with_status(
    tmp_path: Path,
    *,
    credential_status: str,
    auth_browser_exit: int = 0,
    auth_browser_stderr: str = "",
    auth_options_json: str = DEFAULT_AUTH_OPTIONS_JSON,
    set_credentials_example: str = DEFAULT_SET_EXAMPLE,
    latchkey_directory: Path | None = None,
) -> Latchkey:
    """Build a ``Latchkey`` that uses two fake binaries.

    Both ``services info`` and ``auth browser`` call the same fake binary
    via ``latchkey_binary``. The binary inspects ``argv[0]`` (``services``
    or ``auth``) and either prints a JSON payload or appends to the
    auth-browser recording. ``auth_options_json`` controls the
    ``authOptions`` array latchkey reports; pass ``json.dumps(["set"])``
    to simulate a service that doesn't support browser sign-in.
    """
    binary = tmp_path / "latchkey"
    auth_recording = tmp_path / "auth_latchkey_report.jsonl"
    services_payload = json.dumps(
        {
            "credentialStatus": credential_status,
            "authOptions": json.loads(auth_options_json),
            "setCredentialsExample": set_credentials_example,
        }
    )
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "if argv[:2] == ['services', 'info']:\n"
        f"    print({services_payload!r})\n"
        "    sys.exit(0)\n"
        "elif argv[:2] == ['auth', 'browser']:\n"
        f"    with open({str(auth_recording)!r}, 'a') as f:\n"
        "        f.write(json.dumps({'argv': argv, 'env_LATCHKEY_DIRECTORY': os.environ.get('LATCHKEY_DIRECTORY', '')}) + '\\n')\n"
        f"    if {auth_browser_stderr!r}:\n"
        f"        sys.stderr.write({auth_browser_stderr!r})\n"
        f"    sys.exit({auth_browser_exit})\n"
        "else:\n"
        "    sys.stderr.write('unexpected argv: ' + repr(argv))\n"
        "    sys.exit(99)\n"
    )
    binary.chmod(0o755)
    # ``latchkey_directory`` is required on ``Latchkey``; default to ``tmp_path``
    # for tests that don't care about the credential-store location.
    return Latchkey(latchkey_binary=str(binary), latchkey_directory=latchkey_directory or tmp_path)


def build_handler_with_gateway_client(
    tmp_path: Path,
    gateway_client: FakeLatchkeyGatewayClient,
    *,
    credential_status: str = "valid",
) -> LatchkeyPermissionGrantHandler:
    """Build a handler wired to a caller-supplied fake gateway client.

    Tests that need to assert on the gateway client's recorded calls
    (``set_calls`` / ``deleted_request_ids``) pass in their own
    :class:`FakeLatchkeyGatewayClient` and keep the reference. The catalog
    is independent of the gateway client (it reads a local payload), so the
    two are wired separately.
    """
    latchkey = make_latchkey_with_status(tmp_path, credential_status=credential_status)
    mngr_binary = make_recording_binary(tmp_path, "mngr", exit_code=0)
    return LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=latchkey,
        services_catalog=build_slack_services_catalog(),
        mngr_message_sender=MngrMessageSender(mngr_binary=str(mngr_binary)),
        gateway_client=gateway_client,
    )


def build_handler(
    tmp_path: Path,
    *,
    credential_status: str,
    auth_browser_exit: int = 0,
    auth_browser_stderr: str = "",
    auth_options_json: str = DEFAULT_AUTH_OPTIONS_JSON,
    set_credentials_example: str = DEFAULT_SET_EXAMPLE,
    latchkey_directory: Path | None = None,
) -> LatchkeyPermissionGrantHandler:
    latchkey = make_latchkey_with_status(
        tmp_path,
        credential_status=credential_status,
        auth_browser_exit=auth_browser_exit,
        auth_browser_stderr=auth_browser_stderr,
        auth_options_json=auth_options_json,
        set_credentials_example=set_credentials_example,
        latchkey_directory=latchkey_directory,
    )
    mngr_binary = make_recording_binary(tmp_path, "mngr", exit_code=0)
    return LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=latchkey,
        services_catalog=build_slack_services_catalog(),
        mngr_message_sender=MngrMessageSender(mngr_binary=str(mngr_binary)),
        gateway_client=build_fake_gateway_client(),
    )


class RecordingMessageSender(MngrMessageSender):
    """Test double for ``MngrMessageSender`` that records calls instead of running mngr."""

    sent_messages: list[tuple[str, str]] = Field(default_factory=list)

    def send(self, agent_id: AgentId, text: str) -> None:
        self.sent_messages.append((str(agent_id), text))


def build_gateway_client(handler: HttpxHandler) -> LatchkeyGatewayClient:
    return LatchkeyGatewayClient.from_credentials(
        transport=httpx.MockTransport(handler),
        base_url="http://gateway.invalid:1989",
        password="hunter2",
        admin_jwt="admin-jwt-token",
    )


# Broad share roots so the existing tests' representative paths
# (``/home/...``, ``/Users/...``, ``/tmp/...``) all validate as in-root.
# Tests that exercise the out-of-root rejection inject a narrower set.
DEFAULT_TEST_SHARE_ROOTS: Final = (Path("/home"), Path("/Users"), Path("/tmp"))


def make_file_sharing_handler(
    tmp_path: Path,
    gateway_handler: HttpxHandler,
    share_roots: tuple[Path, ...] = DEFAULT_TEST_SHARE_ROOTS,
    home_dir: Path = Path("/home/example"),
) -> tuple[FileSharingGrantHandler, RecordingMessageSender]:
    sender = RecordingMessageSender(sent_messages=[])
    return (
        FileSharingGrantHandler(
            data_dir=tmp_path,
            gateway_client=build_gateway_client(gateway_handler),
            mngr_message_sender=sender,
            share_roots=share_roots,
            home_dir=home_dir,
        ),
        sender,
    )


def build_authenticated_client(
    tmp_path: Path,
    handler: RequestEventHandler,
    inbox: RequestInbox,
) -> TestClient:
    """Wire ``handler`` into a desktop-client app with a valid session cookie.

    Stands up the full FastAPI desktop client so HTTP-level tests exercise
    the same dispatcher path the real desktop client uses.
    """
    auth_dir = tmp_path / "auth"
    auth_store = FileAuthStore(data_directory=auth_dir)
    backend_resolver: BackendResolverInterface = StaticBackendResolver(url_by_agent_and_service={})
    paths = WorkspacePaths(data_dir=tmp_path)
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
        paths=paths,
        request_inbox=inbox,
        request_event_handlers=(handler,),
    )
    client = TestClient(app, base_url="http://localhost")
    cookie_value = create_session_cookie(signing_key=auth_store.get_signing_key())
    client.cookies.set(SESSION_COOKIE_NAME, cookie_value, path="/")
    return client

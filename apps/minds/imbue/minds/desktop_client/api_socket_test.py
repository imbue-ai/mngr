import os
import socket
import struct
from collections.abc import Callable
from collections.abc import Iterable
from pathlib import Path

import httpx2
import pytest

from imbue.minds.desktop_client.api_socket import API_SOCKET_ENV_VAR
from imbue.minds.desktop_client.api_socket import ApiSocketError
from imbue.minds.desktop_client.api_socket import ApiSocketInUseError
from imbue.minds.desktop_client.api_socket import PeerCredentials
from imbue.minds.desktop_client.api_socket import PeerCredentialsError
from imbue.minds.desktop_client.api_socket import create_local_socket_server
from imbue.minds.desktop_client.api_socket import fallback_api_socket_path
from imbue.minds.desktop_client.api_socket import is_peer_allowed
from imbue.minds.desktop_client.api_socket import max_socket_path_bytes
from imbue.minds.desktop_client.api_socket import parse_peer_credentials
from imbue.minds.desktop_client.api_socket import prepare_socket_path
from imbue.minds.desktop_client.api_socket import read_peer_credentials
from imbue.minds.desktop_client.api_socket import resolve_api_socket_location
from imbue.minds.desktop_client.api_socket import restrict_socket_file
from imbue.minds.desktop_client.api_transport import LOCAL_TRUSTED_ENVIRON_KEY
from imbue.minds.desktop_client.api_transport import WsgiApplication
from imbue.minds.desktop_client.testing import serve_wsgi_server_in_thread

_DARWIN = "darwin"
_LINUX = "linux"


def test_canonical_path_is_used_when_it_fits() -> None:
    location = resolve_api_socket_location(
        Path("/Users/alice/Library/Application Support/Imbue Studio/production"),
        "production",
        platform=_DARWIN,
        environ={},
        uid=501,
    )
    assert location.path == Path("/Users/alice/Library/Application Support/Imbue Studio/production/api.sock")
    assert location.is_fallback is False


def test_explicit_env_override_wins_over_everything() -> None:
    location = resolve_api_socket_location(
        Path("/x" * 80), "production", platform=_LINUX, environ={API_SOCKET_ENV_VAR: "/tmp/t.sock"}, uid=1000
    )
    assert location.path == Path("/tmp/t.sock")
    assert location.is_fallback is False


def test_too_long_canonical_path_falls_back_to_the_runtime_dir_on_each_platform() -> None:
    long_state_dir = Path("/Users/" + "a" * 60 + "/Library/Application Support/Imbue Studio/production")
    assert len(str(long_state_dir / "api.sock").encode()) > max_socket_path_bytes(_DARWIN)

    darwin = resolve_api_socket_location(
        long_state_dir, "production", platform=_DARWIN, environ={"TMPDIR": "/var/folders/zz/T"}, uid=501
    )
    assert darwin.path == Path("/var/folders/zz/T/imbue-studio-production.sock")
    assert darwin.is_fallback is True

    linux_with_runtime = resolve_api_socket_location(
        long_state_dir, "staging", platform=_LINUX, environ={"XDG_RUNTIME_DIR": "/run/user/1000"}, uid=1000
    )
    assert linux_with_runtime.path == Path("/run/user/1000/imbue-studio-staging.sock")

    linux_without_runtime = resolve_api_socket_location(
        long_state_dir, "staging", platform=_LINUX, environ={}, uid=1000
    )
    assert linux_without_runtime.path == Path("/tmp/imbue-studio-1000-staging.sock")


def test_fallback_path_names_the_tier_and_qualifies_the_shared_tmp_with_the_uid() -> None:
    assert fallback_api_socket_path("dev-josh-1", _DARWIN, {"TMPDIR": "/var/folders/zz/T"}, 501) == Path(
        "/var/folders/zz/T/imbue-studio-dev-josh-1.sock"
    )
    # /tmp is shared between users on both platforms, so the name carries the uid there.
    assert fallback_api_socket_path("dev-josh-1", _DARWIN, {}, 501) == Path("/tmp/imbue-studio-501-dev-josh-1.sock")
    assert fallback_api_socket_path("dev-josh-1", _LINUX, {}, 1000) == Path("/tmp/imbue-studio-1000-dev-josh-1.sock")


def test_parse_peer_credentials_decodes_both_platform_layouts() -> None:
    linux_raw = struct.pack("iII", 4242, 1000, 1000)
    assert parse_peer_credentials(linux_raw, _LINUX) == PeerCredentials(uid=1000, pid=4242)

    # struct xucred: version, uid, ngroups (short), groups[16]; alignment padding after the short.
    darwin_raw = struct.pack("IIhxx16I", 0, 501, 1, *([20] + [0] * 15))
    assert parse_peer_credentials(darwin_raw, _DARWIN) == PeerCredentials(uid=501, pid=None)


def test_parse_peer_credentials_rejects_short_buffers() -> None:
    with pytest.raises(PeerCredentialsError):
        parse_peer_credentials(b"\x00\x01", _LINUX)
    with pytest.raises(PeerCredentialsError):
        parse_peer_credentials(b"\x00", _DARWIN)


def test_read_peer_credentials_reports_our_own_uid_over_a_socketpair() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        credentials = read_peer_credentials(right)
    finally:
        left.close()
        right.close()
    assert credentials.uid == os.getuid()
    assert is_peer_allowed(credentials, os.getuid())
    assert not is_peer_allowed(credentials, os.getuid() + 1)


def test_read_peer_credentials_raises_when_the_socket_refuses_the_option() -> None:
    closed = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    closed.close()
    with pytest.raises(PeerCredentialsError):
        read_peer_credentials(closed)


def test_prepare_socket_path_clears_a_stale_socket_and_creates_a_private_directory(short_tmp_path: Path) -> None:
    socket_dir = short_tmp_path / "sockdir"
    path = socket_dir / "api.sock"
    prepare_socket_path(path)
    assert socket_dir.is_dir()
    assert (socket_dir.stat().st_mode & 0o777) == 0o700

    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    assert path.exists()
    prepare_socket_path(path)
    assert not path.exists()


def test_prepare_socket_path_refuses_a_path_over_the_platform_limit(short_tmp_path: Path) -> None:
    just_over_darwin = short_tmp_path / ("a" * (max_socket_path_bytes(_DARWIN) + 1 - len(str(short_tmp_path)) - 1))
    assert len(str(just_over_darwin).encode()) == max_socket_path_bytes(_DARWIN) + 1
    with pytest.raises(ApiSocketError):
        prepare_socket_path(just_over_darwin, platform=_DARWIN)
    # The same path fits Linux's longer limit, and nothing was created by the refusal above.
    prepare_socket_path(just_over_darwin, platform=_LINUX)
    assert not just_over_darwin.exists()


def test_prepare_socket_path_reports_an_unusable_directory_as_a_socket_error(short_tmp_path: Path) -> None:
    not_a_directory = short_tmp_path / "file"
    not_a_directory.write_text("")
    with pytest.raises(ApiSocketError):
        prepare_socket_path(not_a_directory / "api.sock")


def test_prepare_socket_path_refuses_to_remove_a_file_that_is_not_a_socket(short_tmp_path: Path) -> None:
    not_a_socket = short_tmp_path / "api.sock"
    not_a_socket.write_text("someone else's file")
    with pytest.raises(ApiSocketError):
        prepare_socket_path(not_a_socket)
    assert not_a_socket.read_text() == "someone else's file"


def test_prepare_socket_path_refuses_a_live_socket(short_tmp_path: Path) -> None:
    path = short_tmp_path / "live.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as live:
        live.bind(str(path))
        live.listen(1)
        with pytest.raises(ApiSocketInUseError):
            prepare_socket_path(path)


def _marker_app(seen: list[dict[str, object]]) -> WsgiApplication:
    """A WSGI app that records each request's environ and answers ``ok``."""

    def app(environ: dict[str, object], start_response: Callable[..., object]) -> Iterable[bytes]:
        seen.append(dict(environ))
        start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
        return [b"ok"]

    return app


def test_local_socket_server_marks_requests_trusted_and_locks_the_socket_file(short_tmp_path: Path) -> None:
    path = short_tmp_path / "api.sock"
    seen: list[dict[str, object]] = []
    server = create_local_socket_server(path, _marker_app(seen), numthreads=2)
    with serve_wsgi_server_in_thread(server, "api-socket-test"):
        restrict_socket_file(path)
        assert (path.stat().st_mode & 0o777) == 0o600
        with httpx2.Client(transport=httpx2.HTTPTransport(uds=str(path)), timeout=5.0) as client:
            response = client.get("http://localhost/api/v1/app/version", headers={"X-Imbue-Studio-Agent": "tester"})
        assert response.status_code == 200
        assert seen[0][LOCAL_TRUSTED_ENVIRON_KEY] is True
        assert seen[0]["HTTP_X_IMBUE_STUDIO_AGENT"] == "tester"


def test_local_socket_server_closes_connections_from_other_uids(short_tmp_path: Path) -> None:
    path = short_tmp_path / "api.sock"
    seen: list[dict[str, object]] = []
    # Our own uid is the only one that can connect in a test, so the allowed uid is set to someone else.
    server = create_local_socket_server(path, _marker_app(seen), numthreads=2, allowed_uid=os.getuid() + 1)
    with serve_wsgi_server_in_thread(server, "api-socket-test"):
        with httpx2.Client(transport=httpx2.HTTPTransport(uds=str(path)), timeout=5.0) as client:
            with pytest.raises(httpx2.HTTPError):
                client.get("http://localhost/api/v1/app/version")
        assert seen == []

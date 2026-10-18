from pathlib import Path

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.api_transport import LOCAL_TRUSTED_ENVIRON_KEY
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.testing import StaticPendingRequests

_LOCAL = {LOCAL_TRUSTED_ENVIRON_KEY: True}


def test_local_socket_requests_pass_every_auth_check_without_a_credential(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=False,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key="k",
        pending_requests=StaticPendingRequests(),
    )

    # The versioned API, the /ui surface, and the bare-origin requests routes all refuse an anonymous TCP request.
    assert client.get("/api/v1/app/version").status_code == 401
    assert client.get("/ui/api/settings").status_code == 401
    assert client.post("/requests/nope/grant").status_code == 403

    # The same requests over the local socket are the user.
    assert client.get("/api/v1/app/version", environ_base=_LOCAL).status_code == 200
    assert client.get("/ui/api/settings", environ_base=_LOCAL).status_code == 200
    # Past auth; the request id is not in the (empty) pending set, which is the next check's answer.
    assert client.post("/requests/nope/grant", environ_base=_LOCAL).status_code == 404


def test_webdav_mount_admits_local_socket_requests(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=False,
        paths=InstallationPaths(data_dir=tmp_path / "minds"),
        minds_api_key="k",
        device_id="host-" + "0" * 32,
    )
    assert client.get("/api/v1/files/").status_code == 401
    # Past the bearer gate: the device gate answers for a path without this device's id.
    assert client.get("/api/v1/files/", environ_base=_LOCAL).status_code != 401

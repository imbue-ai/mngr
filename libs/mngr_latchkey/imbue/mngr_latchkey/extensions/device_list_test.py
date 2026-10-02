import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final

from imbue.mngr_latchkey.devices import DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS
from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.devices import DesktopDeviceIdentity
from imbue.mngr_latchkey.devices import LOCAL_DEVICE_ID_ENV_VAR
from imbue.mngr_latchkey.devices import build_local_device_env
from imbue.mngr_latchkey.testing import http_request
from imbue.mngr_latchkey.testing import node_extension_gateway

_EXTENSION_PATH: Final[Path] = Path(__file__).resolve().parent / "device_list.mjs"

_LOCAL_DEVICE: Final[DesktopDeviceIdentity] = DesktopDeviceIdentity(
    device_id=DesktopDeviceId("desktop-local"), hostname="this-laptop"
)


def test_a_desktop_gateway_lists_itself_as_the_one_connected_desktop() -> None:
    """The listing has the shape a machine's gateway answers in, so a workspace reads it the same way."""
    with node_extension_gateway(_EXTENSION_PATH, build_local_device_env(_LOCAL_DEVICE)) as gateway_url:
        status, body = http_request(f"{gateway_url}/devices")

    assert status == 200
    listing = json.loads(body)
    (device,) = listing["devices"]
    assert (device["device_id"], device["hostname"]) == ("desktop-local", "this-laptop")
    last_seen_at = datetime.fromisoformat(device["last_seen_at"].replace("Z", "+00:00"))
    assert datetime.now(timezone.utc) - last_seen_at < timedelta(seconds=10)
    assert listing["announcement_interval_seconds"] == DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS


def test_only_devices_is_taken_and_only_for_get() -> None:
    with node_extension_gateway(_EXTENSION_PATH, build_local_device_env(_LOCAL_DEVICE)) as gateway_url:
        for path in ("/permissions/self", "/devices/desktop-local", "/device"):
            assert json.loads(http_request(f"{gateway_url}{path}")[1]) == {"served_locally": True, "path": path}
        post_status, post_body = http_request(f"{gateway_url}/devices", method="POST")

    assert (post_status, json.loads(post_body)["error"]) == (405, "/devices only answers GET.")


def test_a_gateway_told_of_no_local_device_says_so() -> None:
    with node_extension_gateway(_EXTENSION_PATH, {}) as gateway_url:
        status, body = http_request(f"{gateway_url}/devices")

    assert status == 503
    assert LOCAL_DEVICE_ID_ENV_VAR in json.loads(body)["error"]

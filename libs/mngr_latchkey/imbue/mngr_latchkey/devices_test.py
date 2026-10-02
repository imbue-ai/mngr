import pytest

from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.devices import DesktopDeviceIdentity
from imbue.mngr_latchkey.devices import build_local_device_env
from imbue.mngr_latchkey.devices import device_record_filename


@pytest.mark.parametrize("value", ["desktop-1", "host-0123456789abcdef", "laptop.home_1", " padded-8213 "])
def test_a_device_id_is_a_safe_file_name(value: str) -> None:
    device_id = DesktopDeviceId(value)

    assert device_id == value.strip()
    assert device_record_filename(device_id) == f"{value.strip()}.json"


@pytest.mark.parametrize("value", ["", "   ", "../escape", ".hidden", "a/b", "with space", "tab\there"])
def test_a_device_id_that_could_not_name_a_file_safely_is_refused(value: str) -> None:
    with pytest.raises(InvalidPrimitiveValueError):
        DesktopDeviceId(value)


def test_the_local_device_env_names_the_desktop_to_its_own_gateway() -> None:
    identity = DesktopDeviceIdentity(device_id=DesktopDeviceId("desktop-4471"), hostname="laptop.example")

    assert build_local_device_env(identity) == {
        "LATCHKEY_EXTENSION_LOCAL_DEVICE_ID": "desktop-4471",
        "LATCHKEY_EXTENSION_LOCAL_DEVICE_HOSTNAME": "laptop.example",
        "LATCHKEY_EXTENSION_DEVICE_ANNOUNCEMENT_INTERVAL_SECONDS": "30",
    }

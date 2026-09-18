from inline_snapshot import snapshot

from imbue.mngr_latchkey.device_metadata import build_device_metadata_env


def test_build_device_metadata_env_encodes_the_device_id_as_detent_custom_metadata() -> None:
    env = build_device_metadata_env("host-4f1c2a9be7d34c0f8a6b5d3e2c1f0a9b")

    assert env == snapshot({"DETENT_CUSTOM_METADATA": '{"deviceId": "host-4f1c2a9be7d34c0f8a6b5d3e2c1f0a9b"}'})


def test_build_device_metadata_env_escapes_a_device_id_that_is_not_plain_json_text() -> None:
    env = build_device_metadata_env('quote"and\\backslash')

    assert env == snapshot({"DETENT_CUSTOM_METADATA": '{"deviceId": "quote\\"and\\\\backslash"}'})

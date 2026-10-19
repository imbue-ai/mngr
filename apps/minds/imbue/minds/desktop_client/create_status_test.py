from imbue.minds.desktop_client.create_status import status_text_for
from imbue.minds.primitives import LaunchMode


def test_status_text_for_failed_surfaces_error_message() -> None:
    assert status_text_for("FAILED", error="boom 41938") == "Failed: boom 41938"
    assert status_text_for("FAILED") == "Failed: unknown error"


def test_status_text_for_imbue_cloud_uses_pool_host_wording() -> None:
    assert status_text_for("CLONING_REPO", launch_mode=LaunchMode.IMBUE_CLOUD) == "Connecting to host..."
    assert status_text_for("CREATING_WORKSPACE", launch_mode=LaunchMode.IMBUE_CLOUD) == "Setting up agent..."


def test_status_text_for_unknown_status_falls_back_to_working() -> None:
    assert status_text_for("SOME_FUTURE_STATUS") == "Working..."

from imbue.minds.desktop_client.create_status import DEFAULT_EXPECTED_CREATE_ATTEMPT_DURATION_SECONDS
from imbue.minds.desktop_client.create_status import expected_create_attempt_duration_seconds
from imbue.minds.desktop_client.create_status import status_text_for
from imbue.minds.primitives import LaunchMode


def test_expected_duration_per_launch_mode() -> None:
    assert expected_create_attempt_duration_seconds(LaunchMode.DOCKER) == 30.0
    assert expected_create_attempt_duration_seconds(LaunchMode.IMBUE_CLOUD) == 30.0
    assert expected_create_attempt_duration_seconds(LaunchMode.LIMA) == 600.0
    assert expected_create_attempt_duration_seconds(LaunchMode.VULTR) == 300.0


def test_expected_duration_covers_every_launch_mode() -> None:
    # Every launch mode must resolve to a positive duration so the progress
    # bar never divides by zero; unmapped modes fall back to the default.
    for launch_mode in LaunchMode:
        assert expected_create_attempt_duration_seconds(launch_mode) > 0
    assert DEFAULT_EXPECTED_CREATE_ATTEMPT_DURATION_SECONDS == 60.0


def test_status_text_for_failed_surfaces_error_message() -> None:
    assert status_text_for("FAILED", error="boom 41938") == "Failed: boom 41938"
    assert status_text_for("FAILED") == "Failed: unknown error"


def test_status_text_for_imbue_cloud_uses_pool_host_wording() -> None:
    assert status_text_for("CLONING_REPO", launch_mode=LaunchMode.IMBUE_CLOUD) == "Connecting to host..."
    assert status_text_for("CREATING_WORKSPACE", launch_mode=LaunchMode.IMBUE_CLOUD) == "Setting up agent..."


def test_status_text_for_unknown_status_falls_back_to_working() -> None:
    assert status_text_for("SOME_FUTURE_STATUS") == "Working..."

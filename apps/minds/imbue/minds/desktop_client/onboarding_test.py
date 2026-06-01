import base64
import json
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.onboarding import DEFAULT_EXPECTED_CREATION_DURATION_SECONDS
from imbue.minds.desktop_client.onboarding import OnboardingAnswers
from imbue.minds.desktop_client.onboarding import OnboardingApplier
from imbue.minds.desktop_client.onboarding import PERMISSIONS_PREFERENCES_REMOTE_PATH
from imbue.minds.desktop_client.onboarding import USER_CONTEXT_PLACEHOLDER_DETAILS
from imbue.minds.desktop_client.onboarding import build_permissions_write_script
from imbue.minds.desktop_client.onboarding import build_user_context_document
from imbue.minds.desktop_client.onboarding import expected_creation_duration_seconds
from imbue.minds.desktop_client.onboarding import resolve_local_user_name
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import UserDataPreference


def test_is_noop_for_empty_answers() -> None:
    assert OnboardingAnswers().is_noop is True


def test_is_noop_for_control_only() -> None:
    # CONTROL means "gather nothing", so a data_preference of CONTROL with no
    # text answers is still a no-op.
    assert OnboardingAnswers(data_preference=UserDataPreference.CONTROL).is_noop is True


def test_not_noop_when_scan_requested() -> None:
    assert OnboardingAnswers(data_preference=UserDataPreference.PRIVACY).is_noop is False
    assert OnboardingAnswers(data_preference=UserDataPreference.CONVENIENCE).is_noop is False


def test_not_noop_when_text_answers_present() -> None:
    assert OnboardingAnswers(initial_problem="do a thing").is_noop is False
    assert OnboardingAnswers(permissions_preference="be safe").is_noop is False


def test_noop_ignores_whitespace_only_text() -> None:
    assert OnboardingAnswers(initial_problem="   ", permissions_preference="\n\t").is_noop is True


def test_expected_duration_per_launch_mode() -> None:
    assert expected_creation_duration_seconds(LaunchMode.DOCKER) == 30.0
    assert expected_creation_duration_seconds(LaunchMode.IMBUE_CLOUD) == 30.0
    assert expected_creation_duration_seconds(LaunchMode.LIMA) == 300.0
    assert expected_creation_duration_seconds(LaunchMode.CLOUD) == 300.0


def test_expected_duration_covers_every_launch_mode() -> None:
    # Every launch mode must resolve to a positive duration so the progress
    # bar never divides by zero; unmapped modes fall back to the default.
    for launch_mode in LaunchMode:
        assert expected_creation_duration_seconds(launch_mode) > 0
    assert DEFAULT_EXPECTED_CREATION_DURATION_SECONDS == 60.0


def test_build_user_context_document() -> None:
    document = build_user_context_document("Ada Lovelace")
    assert document == {"name": "Ada Lovelace", "details": USER_CONTEXT_PLACEHOLDER_DETAILS}


def test_build_permissions_write_script_round_trips_text() -> None:
    text = "be safe -- ask before 'rm -rf'\nand newlines\twork"
    script = build_permissions_write_script(text)
    # The script targets the documented memory path and creates its dir.
    assert PERMISSIONS_PREFERENCES_REMOTE_PATH in script
    assert "mkdir -p /mngr/code/runtime/memory" in script
    # The embedded base64 decodes back to the original text exactly.
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    assert encoded in script
    assert base64.b64decode(encoded).decode("utf-8") == text


def test_resolve_local_user_name_is_non_empty() -> None:
    # Always falls back to the login username, so this is never empty.
    assert resolve_local_user_name() != ""


def _make_applier(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> OnboardingApplier:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_creator = AgentCreator(
        paths=paths,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )
    return OnboardingApplier(
        agent_creator=agent_creator,
        paths=paths,
        message_sender=MngrMessageSender(mngr_binary="mngr"),
        root_concurrency_group=root_concurrency_group,
    )


def test_user_context_scan_writes_json_file(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    applier = _make_applier(tmp_path, root_concurrency_group, notification_dispatcher)
    creation_id = CreationId()

    applier._run_user_context_scan(creation_id)

    context_path = tmp_path / "user_context" / f"{creation_id}.json"
    assert context_path.exists()
    document = json.loads(context_path.read_text())
    assert document["name"] != ""
    assert document["details"] == USER_CONTEXT_PLACEHOLDER_DETAILS


def test_start_apply_noop_writes_nothing(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    applier = _make_applier(tmp_path, root_concurrency_group, notification_dispatcher)
    creation_id = CreationId()

    applier.start_apply(creation_id, OnboardingAnswers(data_preference=UserDataPreference.CONTROL))

    # CONTROL is a no-op, so no background work runs and no file is written.
    assert not (tmp_path / "user_context").exists()

import base64
import json
import threading
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreationStatus
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
from imbue.minds.desktop_client.testing import seed_agent_creator_creation
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
    assert expected_creation_duration_seconds(LaunchMode.LIMA) == 600.0
    assert expected_creation_duration_seconds(LaunchMode.VULTR) == 300.0


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
    user_name_resolver: Callable[[], str] | None = None,
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
        user_name_resolver=user_name_resolver or resolve_local_user_name,
    )


def test_user_context_scan_writes_json_file(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    # Inject a fixed resolver so the scan is hermetic (no real git / GECOS /
    # getpass lookup) and the written name is exactly what we control, which
    # would catch a name/details field swap.
    resolved_name = f"Test User {uuid4().hex}"
    applier = _make_applier(
        tmp_path,
        root_concurrency_group,
        notification_dispatcher,
        user_name_resolver=lambda: resolved_name,
    )
    creation_id = CreationId()

    applier._run_user_context_scan(creation_id)

    context_path = tmp_path / "user_context" / f"{creation_id}.json"
    assert context_path.exists()
    document = json.loads(context_path.read_text())
    assert document["name"] == resolved_name
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


class _HandshakeApplier(OnboardingApplier):
    """Applier whose Q2/Q3 strands each wait on the other's start signal.

    Used to prove the two run concurrently: each strand records whether it
    observed the other strand start within the timeout. If they ran
    sequentially, the first strand would never see the second and its wait
    would return False.
    """

    _q2_started: threading.Event = PrivateAttr(default_factory=threading.Event)
    _q3_started: threading.Event = PrivateAttr(default_factory=threading.Event)
    _is_q3_observed_by_q2: bool = PrivateAttr(default=False)
    _is_q2_observed_by_q3: bool = PrivateAttr(default=False)

    def _send_initial_problem(self, host_name: str, initial_problem: str) -> None:
        self._q2_started.set()
        self._is_q3_observed_by_q2 = self._q3_started.wait(timeout=10.0)

    def _apply_permissions_preference(self, creation_id: CreationId, permissions_text: str) -> None:
        self._q3_started.set()
        self._is_q2_observed_by_q3 = self._q2_started.wait(timeout=10.0)


def test_q2_and_q3_are_applied_concurrently(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_creator = AgentCreator(
        paths=paths,
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
    )
    creation_id = CreationId()
    # Seed a tracked creation with a host name so the Q2 strand has a target
    # (otherwise it would short-circuit and only one strand would run).
    seed_agent_creator_creation(
        agent_creator,
        creation_id,
        status=AgentCreationStatus.WAITING_FOR_READY,
        host_name="assistant",
    )

    applier = _HandshakeApplier(
        agent_creator=agent_creator,
        paths=paths,
        message_sender=MngrMessageSender(mngr_binary="mngr"),
        root_concurrency_group=root_concurrency_group,
    )

    # _apply returns only once both strands have finished (the delivery
    # group's context-exit joins them).
    applier._apply(creation_id, OnboardingAnswers(initial_problem="do a thing", permissions_preference="be safe"))

    # Each strand saw the other start -> they ran at the same time.
    assert applier._is_q3_observed_by_q2 is True
    assert applier._is_q2_observed_by_q3 is True

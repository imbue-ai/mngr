import json
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.minds.desktop_client.account_settings_store import AccountSettingsStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.settings.keybindings import CHORD_MODIFIER_ORDER
from imbue.minds.desktop_client.settings.keybindings import CHORD_NAMED_KEYS
from imbue.minds.desktop_client.testing import decode_written_profile_snapshot
from imbue.minds.desktop_client.workspace_profile_writer import SNAPSHOT_FORMAT
from imbue.minds.desktop_client.workspace_profile_writer import USER_TIMEZONE_PATH
from imbue.minds.desktop_client.workspace_profile_writer import WORKSPACE_PROFILE_SNAPSHOT_PATH
from imbue.minds.desktop_client.workspace_profile_writer import WorkspaceProfileWriter
from imbue.minds.desktop_client.workspace_profile_writer import _timezone_once_clause
from imbue.minds.desktop_client.workspace_profile_writer import build_profile_snapshot_document
from imbue.minds.desktop_client.workspace_profile_writer import profile_snapshot_content_hash
from imbue.minds.desktop_client.workspace_profile_writer import read_account_plan_name
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId

_NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)
_CONTRACT_PATH = Path(__file__).parent / "workspace_profile_contract.json"


def _writer(
    tmp_path: Path,
    caller: RecordingMngrCaller,
    resolver: StaticBackendResolver,
    settings_store: AccountSettingsStore | None = None,
    session_store: MultiAccountSessionStore | None = None,
    plan_name_by_email: dict[str, str | None] | None = None,
    plan_read_interval_seconds: float = 0.0,
    plan_reads: list[str] | None = None,
    read_plan_name: Callable[[str], str | None] | None = None,
) -> WorkspaceProfileWriter:
    plans = plan_name_by_email if plan_name_by_email is not None else {}

    def read_plan_from_plans(email: str) -> str | None:
        if plan_reads is not None:
            plan_reads.append(email)
        return plans.get(email)

    return WorkspaceProfileWriter(
        mngr_caller=caller,
        backend_resolver=resolver,
        session_store=session_store,
        settings_store=settings_store,
        device_id="device-under-test",
        hashes_dir=tmp_path / "workspace_profile_hashes",
        read_plan_name=read_plan_name if read_plan_name is not None else read_plan_from_plans,
        plan_read_interval_seconds=plan_read_interval_seconds,
    )


def _store_owning(
    tmp_path: Path, *agent_ids: AgentId, cli: FakeImbueCloudCli | None = None
) -> MultiAccountSessionStore:
    """A session store with one signed-in account that owns each of ``agent_ids``."""
    effective_cli = cli if cli is not None else make_fake_imbue_cloud_cli()
    effective_cli.add_account(user_id="user-1", email="owner@example.com")
    store = make_session_store_for_test(tmp_path / "sessions", cli=effective_cli)
    for agent_id in agent_ids:
        store.associate_created_workspace(
            user_id="user-1",
            agent_id=str(agent_id),
            host_id=str(HostId.generate()),
            display_name="",
            color=None,
            is_cloud_row=False,
        )
    return store


def _written_documents(caller: RecordingMngrCaller) -> list[dict[str, object]]:
    return [decode_written_profile_snapshot(call[2]) for call in caller.calls]


def test_the_snapshot_document_matches_the_published_contract_fixture() -> None:
    """The template vendors this fixture and pins its reader against it; the writer must match it too."""
    contract = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
    document = build_profile_snapshot_document("account-1", "device-1", _NOW, {}, False)

    assert contract["snapshot_format"] == SNAPSHOT_FORMAT == document["snapshot_format"]
    assert contract["snapshot_relative_path"] == WORKSPACE_PROFILE_SNAPSHOT_PATH
    assert set(document.keys()) == set(contract["header_keys"]) | set(contract["setting_keys"])


def test_the_chord_grammar_vocabulary_matches_the_published_contract_fixture() -> None:
    """The template's grammar mirror pins against the same fixture, so the vocabularies never drift."""
    contract = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))

    assert tuple(contract["chord_modifier_order"]) == CHORD_MODIFIER_ORDER
    assert tuple(contract["chord_named_keys"]) == CHORD_NAMED_KEYS


def test_the_content_hash_ignores_the_write_stamp_but_not_the_account() -> None:
    first = build_profile_snapshot_document("account-1", "device-1", _NOW, {}, False)
    restamped = build_profile_snapshot_document(
        "account-1", "device-1", datetime(2026, 11, 1, tzinfo=timezone.utc), {}, False
    )
    moved = build_profile_snapshot_document("account-2", "device-1", _NOW, {}, False)

    assert profile_snapshot_content_hash(first) == profile_snapshot_content_hash(restamped)
    assert profile_snapshot_content_hash(first) != profile_snapshot_content_hash(moved)


def test_the_content_hash_moves_when_the_keybindings_move() -> None:
    plain = build_profile_snapshot_document("account-1", "device-1", _NOW, {}, False)
    bound = build_profile_snapshot_document("account-1", "device-1", _NOW, {"chat": {"send": ("ctrl+enter",)}}, False)

    assert profile_snapshot_content_hash(plain) != profile_snapshot_content_hash(bound)


def test_the_create_write_carries_the_snapshot_and_the_once_only_timezone(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    writer = _writer(tmp_path, caller, StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}))

    assert writer.write_profile_for_create(agent_id, "America/New_York") is True

    (call,) = caller.calls
    assert call[0] == "exec" and "--no-start" in call
    command = call[2]
    document = decode_written_profile_snapshot(command)
    assert document["account_id"] == "local"
    assert document["owner_user_id"] == "local"
    assert document["written_by"] == "device-under-test"
    assert f"test -f {USER_TIMEZONE_PATH} ||" in command
    # The hash is stored, so the next sync pass does not rewrite an unchanged snapshot.
    assert (tmp_path / "workspace_profile_hashes" / str(agent_id)).exists()


def test_the_snapshot_carries_the_owning_accounts_keybindings(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    settings_store = AccountSettingsStore(data_dir=tmp_path / "settings-data", cli=None)
    settings_store.apply_settings_values("local", {"keybindings": {"chat": {"send": ["ctrl+enter"]}}})
    resolver = StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}})
    writer = _writer(tmp_path, caller, resolver, settings_store=settings_store)

    writer.sync_profiles()

    (call,) = caller.calls
    document = decode_written_profile_snapshot(call[2])
    assert document["keybindings"] == {"chat": {"send": ["ctrl+enter"]}}


def test_without_an_answer_the_snapshot_follows_the_owning_accounts_plan(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    plans: dict[str, str | None] = {"owner@example.com": "ally"}
    writer = _writer(
        tmp_path,
        caller,
        StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        session_store=_store_owning(tmp_path, agent_id),
        plan_name_by_email=plans,
    )

    writer.sync_profiles()
    # Leaving the plan moves the content, so a running workspace learns it on the next pass.
    plans["owner@example.com"] = "free"
    writer.sync_profiles()

    assert [document["experimental_features_enabled"] for document in _written_documents(caller)] == [True, False]


def test_a_workspace_with_no_owning_account_and_no_answer_is_opted_out(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    plan_reads: list[str] = []
    writer = _writer(
        tmp_path, caller, StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}), plan_reads=plan_reads
    )

    writer.sync_profiles()

    assert [document["experimental_features_enabled"] for document in _written_documents(caller)] == [False]
    assert plan_reads == []


def test_a_plan_that_cannot_be_read_keeps_the_last_answer(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    plans: dict[str, str | None] = {"owner@example.com": "ally"}
    writer = _writer(
        tmp_path,
        caller,
        StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        session_store=_store_owning(tmp_path, agent_id),
        plan_name_by_email=plans,
    )

    writer.sync_profiles()
    plans["owner@example.com"] = None
    writer.sync_profiles()

    assert [document["experimental_features_enabled"] for document in _written_documents(caller)] == [True]


def test_an_owner_with_no_readable_account_here_keeps_the_snapshot_it_has(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    cli = make_fake_imbue_cloud_cli()
    session_store = _store_owning(tmp_path, agent_id, cli=cli)
    writer = _writer(
        tmp_path,
        caller,
        StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        session_store=session_store,
        plan_name_by_email={"owner@example.com": "ally"},
    )
    writer.sync_profiles()

    cli.is_auth_list_failing = True
    session_store.invalidate_identity_cache()
    writer.sync_profiles()

    cli.is_auth_list_failing = False
    cli.accounts_to_return.clear()
    session_store.invalidate_identity_cache()
    writer.sync_profiles()

    assert [document["experimental_features_enabled"] for document in _written_documents(caller)] == [True]


def test_a_plan_never_read_leaves_an_existing_snapshot_alone_but_still_writes_a_first_one(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    resolver = StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}})
    session_store = _store_owning(tmp_path, agent_id)
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    _writer(tmp_path, caller, resolver, session_store=session_store).sync_profiles()
    assert [document["experimental_features_enabled"] for document in _written_documents(caller)] == [False]

    # A new process with the connector still down: the stored hash says a snapshot exists, so it stands.
    _writer(tmp_path, caller, resolver, session_store=session_store).sync_profiles()
    assert len(caller.calls) == 1


@pytest.mark.parametrize("plan_name", ["ally", None])
def test_an_accounts_plan_is_tried_once_per_interval_across_its_workspaces(
    tmp_path: Path, plan_name: str | None
) -> None:
    agent_id = AgentId.generate()
    other_agent_id = AgentId.generate()
    plan_reads: list[str] = []
    writer = _writer(
        tmp_path,
        RecordingMngrCaller(result=MngrCallResult(returncode=0)),
        StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}, str(other_agent_id): {}}),
        session_store=_store_owning(tmp_path, agent_id, other_agent_id),
        plan_name_by_email={"owner@example.com": plan_name},
        plan_read_interval_seconds=3600.0,
        plan_reads=plan_reads,
    )

    writer.sync_profiles()
    writer.sync_profiles()

    assert plan_reads == ["owner@example.com"]


def test_a_caller_behind_a_running_plan_read_shares_its_answer(tmp_path: Path) -> None:
    is_read_started = threading.Event()
    is_read_released = threading.Event()
    plan_reads: list[str] = []

    def read_plan_name(email: str) -> str | None:
        plan_reads.append(email)
        is_read_started.set()
        is_read_released.wait(timeout=30)
        return "ally"

    writer = _writer(
        tmp_path,
        RecordingMngrCaller(),
        StaticBackendResolver(url_by_agent_and_service={}),
        plan_read_interval_seconds=300.0,
        read_plan_name=read_plan_name,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(writer.read_experimental_features_plan_default, "owner@example.com")
        assert is_read_started.wait(timeout=30)
        second = pool.submit(writer.read_experimental_features_plan_default, "owner@example.com")
        # Long enough for a second read to start if the caller were not held behind the first.
        wait([second], timeout=0.2)
        is_read_released.set()

        assert first.result(timeout=30) is True
        assert second.result(timeout=30) is True

    assert plan_reads == ["owner@example.com"]


def test_the_create_write_carries_the_experimental_features_setting(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    writer = _writer(
        tmp_path,
        caller,
        StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        session_store=_store_owning(tmp_path, agent_id),
        plan_name_by_email={"owner@example.com": "ally"},
    )

    assert writer.write_profile_for_create(agent_id, "") is True

    assert [document["experimental_features_enabled"] for document in _written_documents(caller)] == [True]


def test_a_create_write_with_an_unreadable_plan_is_corrected_once_a_sync_pass_can_read_it(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    plans: dict[str, str | None] = {"owner@example.com": None}
    writer = _writer(
        tmp_path,
        caller,
        StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        session_store=_store_owning(tmp_path, agent_id),
        plan_name_by_email=plans,
    )

    assert writer.write_profile_for_create(agent_id, "") is True
    plans["owner@example.com"] = "ally"
    writer.sync_profiles()

    assert [document["experimental_features_enabled"] for document in _written_documents(caller)] == [False, True]


def test_read_account_plan_name_answers_the_plan_or_none_when_the_connector_cannot_say() -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.account_info_by_email["owner@example.com"] = {"plan_name": "ally"}

    assert read_account_plan_name(cli, "owner@example.com") == "ally"
    # The fake has no account info for this email, so it fails as a connector error does.
    assert read_account_plan_name(cli, "nobody@example.com") is None


def test_the_create_write_skips_the_timezone_clause_when_the_zone_is_unknown(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    writer = _writer(tmp_path, caller, StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}))

    writer.write_profile_for_create(agent_id, "")

    (call,) = caller.calls
    assert "test -f" not in call[2]


def test_the_sync_pass_writes_once_and_rewrites_only_when_the_content_moves(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    writer = _writer(tmp_path, caller, StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}))

    writer.sync_profiles()
    writer.sync_profiles()

    assert len(caller.calls) == 1

    # An association change drops the hash, so the next pass rewrites.
    writer.mark_profile_stale(agent_id)
    writer.sync_profiles()
    assert len(caller.calls) == 2


def test_a_failed_write_keeps_no_hash_so_the_next_pass_retries(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="unreachable"))
    writer = _writer(tmp_path, caller, StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}))

    writer.sync_profiles()
    writer.sync_profiles()

    assert len(caller.calls) == 2


def test_the_timezone_clause_writes_the_file_only_when_absent(tmp_path: Path) -> None:
    """The clause is real shell; run it twice against a staged root to prove the once-only guard."""
    clause = _timezone_once_clause("America/New_York")

    subprocess.run(["bash", "-c", clause], cwd=tmp_path, check=True)
    timezone_file = tmp_path / USER_TIMEZONE_PATH
    assert timezone_file.read_text() == "America/New_York\n"

    second_clause = _timezone_once_clause("Europe/Berlin")
    subprocess.run(["bash", "-c", second_clause], cwd=tmp_path, check=True)
    assert timezone_file.read_text() == "America/New_York\n"


@pytest.mark.parametrize(
    ("plan_name", "answer"),
    [("ally", False), ("free", True)],
)
def test_the_accounts_own_answer_beats_its_plan_and_needs_no_plan_read(
    tmp_path: Path, plan_name: str, answer: bool
) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    settings_store = AccountSettingsStore(data_dir=tmp_path / "settings")
    settings_store.apply_settings_values("user-1", {"experimental_features_opt_in": answer})
    plan_reads: list[str] = []
    writer = _writer(
        tmp_path,
        caller,
        StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        settings_store=settings_store,
        session_store=_store_owning(tmp_path, agent_id),
        plan_name_by_email={"owner@example.com": plan_name},
        plan_reads=plan_reads,
    )

    writer.sync_profiles()

    assert [document["experimental_features_enabled"] for document in _written_documents(caller)] == [answer]
    assert plan_reads == []


def test_answering_moves_a_running_workspace_on_the_next_pass(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    settings_store = AccountSettingsStore(data_dir=tmp_path / "settings")
    writer = _writer(
        tmp_path,
        caller,
        StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        settings_store=settings_store,
        session_store=_store_owning(tmp_path, agent_id),
        plan_name_by_email={"owner@example.com": "free"},
    )

    writer.sync_profiles()
    settings_store.apply_settings_values("user-1", {"experimental_features_opt_in": True})
    writer.sync_profiles()

    assert [document["experimental_features_enabled"] for document in _written_documents(caller)] == [False, True]


def test_a_workspace_with_no_owning_account_follows_the_devices_answer(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    settings_store = AccountSettingsStore(data_dir=tmp_path / "settings")
    settings_store.apply_settings_values(LOCAL_PROFILE_USER_ID, {"experimental_features_opt_in": True})
    writer = _writer(
        tmp_path,
        caller,
        StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        settings_store=settings_store,
    )

    writer.sync_profiles()

    assert [document["experimental_features_enabled"] for document in _written_documents(caller)] == [True]


def test_an_owner_signed_out_here_with_an_answer_is_not_left_waiting(tmp_path: Path) -> None:
    """Only an owner with no answer needs a plan, so only that owner waits on a session gone from this device."""
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    settings_store = AccountSettingsStore(data_dir=tmp_path / "settings")
    settings_store.apply_settings_values("user-1", {"experimental_features_opt_in": True})
    cli = make_fake_imbue_cloud_cli()
    session_store = _store_owning(tmp_path, agent_id, cli=cli)
    writer = _writer(
        tmp_path,
        caller,
        StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}),
        settings_store=settings_store,
        session_store=session_store,
    )
    (tmp_path / "workspace_profile_hashes").mkdir(parents=True)
    (tmp_path / "workspace_profile_hashes" / str(agent_id)).write_text("an-older-snapshot")
    cli.remove_account("user-1")

    writer.sync_profiles()

    assert [document["experimental_features_enabled"] for document in _written_documents(caller)] == [True]

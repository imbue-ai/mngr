import base64
import json
import re
import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.minds.desktop_client.account_settings_store import AccountSettingsStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.settings.keybindings import CHORD_MODIFIER_ORDER
from imbue.minds.desktop_client.settings.keybindings import CHORD_NAMED_KEYS
from imbue.minds.desktop_client.workspace_profile_writer import SNAPSHOT_FORMAT
from imbue.minds.desktop_client.workspace_profile_writer import USER_TIMEZONE_PATH
from imbue.minds.desktop_client.workspace_profile_writer import WORKSPACE_PROFILE_SNAPSHOT_PATH
from imbue.minds.desktop_client.workspace_profile_writer import WorkspaceProfileWriter
from imbue.minds.desktop_client.workspace_profile_writer import _timezone_once_clause
from imbue.minds.desktop_client.workspace_profile_writer import build_profile_snapshot_document
from imbue.minds.desktop_client.workspace_profile_writer import profile_snapshot_content_hash
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId

_NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)
_CONTRACT_PATH = Path(__file__).parent / "workspace_profile_contract.json"


def _writer(
    tmp_path: Path,
    caller: RecordingMngrCaller,
    resolver: StaticBackendResolver,
    settings_store: AccountSettingsStore | None = None,
) -> WorkspaceProfileWriter:
    return WorkspaceProfileWriter(
        mngr_caller=caller,
        backend_resolver=resolver,
        session_store=None,
        settings_store=settings_store,
        device_id="device-under-test",
        hashes_dir=tmp_path / "workspace_profile_hashes",
    )


def test_the_snapshot_document_matches_the_published_contract_fixture() -> None:
    """The template vendors this fixture and pins its reader against it; the writer must match it too."""
    contract = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
    document = build_profile_snapshot_document("account-1", "device-1", _NOW, {})

    assert contract["snapshot_format"] == SNAPSHOT_FORMAT == document["snapshot_format"]
    assert contract["snapshot_relative_path"] == WORKSPACE_PROFILE_SNAPSHOT_PATH
    assert set(document.keys()) == set(contract["header_keys"]) | set(contract["setting_keys"])


def test_the_chord_grammar_vocabulary_matches_the_published_contract_fixture() -> None:
    """The template's grammar mirror pins against the same fixture, so the vocabularies never drift."""
    contract = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))

    assert tuple(contract["chord_modifier_order"]) == CHORD_MODIFIER_ORDER
    assert tuple(contract["chord_named_keys"]) == CHORD_NAMED_KEYS


def test_the_content_hash_ignores_the_write_stamp_but_not_the_account() -> None:
    first = build_profile_snapshot_document("account-1", "device-1", _NOW, {})
    restamped = build_profile_snapshot_document(
        "account-1", "device-1", datetime(2026, 11, 1, tzinfo=timezone.utc), {}
    )
    moved = build_profile_snapshot_document("account-2", "device-1", _NOW, {})

    assert profile_snapshot_content_hash(first) == profile_snapshot_content_hash(restamped)
    assert profile_snapshot_content_hash(first) != profile_snapshot_content_hash(moved)


def test_the_content_hash_moves_when_the_keybindings_move() -> None:
    plain = build_profile_snapshot_document("account-1", "device-1", _NOW, {})
    bound = build_profile_snapshot_document("account-1", "device-1", _NOW, {"chat": {"send": ("ctrl+enter",)}})

    assert profile_snapshot_content_hash(plain) != profile_snapshot_content_hash(bound)


def _decode_written_snapshot(exec_command: str) -> dict[str, object]:
    """The snapshot document the exec's base64-encoded atomic write carries."""
    encoded_payloads = re.findall(r"printf '%s' ([A-Za-z0-9+/=]+) \| base64 -d", exec_command)
    assert encoded_payloads, exec_command
    return json.loads(base64.b64decode(encoded_payloads[0]).decode("utf-8"))


def test_the_create_write_carries_the_snapshot_and_the_once_only_timezone(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0))
    writer = _writer(tmp_path, caller, StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}}))

    assert writer.write_profile_for_create(agent_id, "America/New_York") is True

    (call,) = caller.calls
    assert call[0] == "exec" and "--no-start" in call
    command = call[2]
    document = _decode_written_snapshot(command)
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
    document = _decode_written_snapshot(call[2])
    assert document["keybindings"] == {"chat": {"send": ["ctrl+enter"]}}


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

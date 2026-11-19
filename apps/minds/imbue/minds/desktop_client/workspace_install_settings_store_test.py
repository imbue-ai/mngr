"""Unit tests for the record of what this install holds per workspace."""

import json
from pathlib import Path

import pytest

from imbue.minds.desktop_client.settings.schema import ClientPermissionDecision
from imbue.minds.desktop_client.settings.schema import ClientPermissionName
from imbue.minds.desktop_client.settings.schema import SUPPORTED_SETTINGS_FORMAT
from imbue.minds.desktop_client.settings.schema import WorkspaceInstallSettings
from imbue.minds.desktop_client.workspace_install_settings_store import WorkspaceInstallSettingsStore
from imbue.minds.errors import WorkspaceInstallSettingsTooNewError
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.testing import capture_loguru

_AGENT_ID = "agent-000102030405060708090a0b0c0d0e0f"
_OTHER_AGENT_ID = "agent-0f0e0d0c0b0a090807060504030201ff"


def test_a_workspace_nothing_was_decided_about_reads_as_an_empty_record(tmp_path: Path) -> None:
    """Built at startup, before any decision was ever made, over a directory that is not there yet."""
    store = WorkspaceInstallSettingsStore(records_dir=tmp_path / "not-yet")
    assert store.read(_AGENT_ID) == WorkspaceInstallSettings()
    assert not (tmp_path / "not-yet").exists()


def test_a_decision_is_there_for_the_next_launch(tmp_path: Path) -> None:
    """A store built fresh over the same directory sees what an earlier one wrote."""
    written = WorkspaceInstallSettingsStore(records_dir=tmp_path).set_client_permission(
        _AGENT_ID, ClientPermissionName.MICROPHONE, ClientPermissionDecision.GRANTED
    )
    reopened = WorkspaceInstallSettingsStore(records_dir=tmp_path)
    assert reopened.read(_AGENT_ID) == written
    assert reopened.read(_AGENT_ID).client_permissions == {"microphone": ClientPermissionDecision.GRANTED}


def test_the_file_is_the_record_keyed_by_the_permission_name_string(tmp_path: Path) -> None:
    """The on-disk shape is the spec's: a settings_format and decisions by Permissions Policy feature name."""
    WorkspaceInstallSettingsStore(records_dir=tmp_path).set_client_permission(
        _AGENT_ID, ClientPermissionName.MICROPHONE, ClientPermissionDecision.DENIED
    )
    assert json.loads((tmp_path / f"{_AGENT_ID}.json").read_text()) == {
        "settings_format": SUPPORTED_SETTINGS_FORMAT,
        "client_permissions": {"microphone": "denied"},
    }


def test_deciding_again_replaces_the_earlier_decision(tmp_path: Path) -> None:
    store = WorkspaceInstallSettingsStore(records_dir=tmp_path)
    store.set_client_permission(_AGENT_ID, ClientPermissionName.MICROPHONE, ClientPermissionDecision.GRANTED)
    store.set_client_permission(_AGENT_ID, ClientPermissionName.MICROPHONE, ClientPermissionDecision.ASK)
    assert store.read(_AGENT_ID).client_permissions == {"microphone": ClientPermissionDecision.ASK}


def test_deleting_the_last_entry_takes_the_file_with_it(tmp_path: Path) -> None:
    """An empty file and an absent one mean the same thing; only one accumulates."""
    store = WorkspaceInstallSettingsStore(records_dir=tmp_path)
    store.set_client_permission(_AGENT_ID, ClientPermissionName.MICROPHONE, ClientPermissionDecision.GRANTED)
    assert (tmp_path / f"{_AGENT_ID}.json").is_file()
    cleared = store.set_client_permission(_AGENT_ID, ClientPermissionName.MICROPHONE, None)
    assert cleared == WorkspaceInstallSettings()
    assert not (tmp_path / f"{_AGENT_ID}.json").exists()
    assert store.read(_AGENT_ID) == WorkspaceInstallSettings()


def test_deleting_an_entry_that_was_never_there_is_not_an_error_and_tells_nobody(tmp_path: Path) -> None:
    """Deferring a workspace that already defers is the state asked for."""
    store = WorkspaceInstallSettingsStore(records_dir=tmp_path)
    changes: list[int] = []
    store.add_on_change_callback(lambda: changes.append(1))
    assert store.set_client_permission(_AGENT_ID, ClientPermissionName.MICROPHONE, None) == WorkspaceInstallSettings()
    assert changes == []
    assert not (tmp_path / f"{_AGENT_ID}.json").exists()


def test_workspaces_are_kept_in_separate_files_and_forgotten_one_at_a_time(tmp_path: Path) -> None:
    store = WorkspaceInstallSettingsStore(records_dir=tmp_path)
    store.set_client_permission(_AGENT_ID, ClientPermissionName.MICROPHONE, ClientPermissionDecision.GRANTED)
    store.set_client_permission(_OTHER_AGENT_ID, ClientPermissionName.MICROPHONE, ClientPermissionDecision.DENIED)
    assert (tmp_path / f"{_AGENT_ID}.json").is_file()
    assert (tmp_path / f"{_OTHER_AGENT_ID}.json").is_file()
    store.forget_workspace(_AGENT_ID)
    assert not (tmp_path / f"{_AGENT_ID}.json").exists()
    assert store.read(_AGENT_ID) == WorkspaceInstallSettings()
    assert store.read(_OTHER_AGENT_ID).client_permissions == {"microphone": ClientPermissionDecision.DENIED}


def test_forgetting_a_workspace_never_recorded_is_a_no_op(tmp_path: Path) -> None:
    store = WorkspaceInstallSettingsStore(records_dir=tmp_path)
    changes: list[int] = []
    store.add_on_change_callback(lambda: changes.append(1))
    store.forget_workspace(_AGENT_ID)
    assert changes == []


def test_a_malformed_record_is_ignored_rather_than_failing_the_rest(tmp_path: Path) -> None:
    """One corrupt file must not cost the user every other workspace's decisions; the workspace defers meanwhile."""
    WorkspaceInstallSettingsStore(records_dir=tmp_path).set_client_permission(
        _OTHER_AGENT_ID, ClientPermissionName.MICROPHONE, ClientPermissionDecision.GRANTED
    )
    (tmp_path / f"{_AGENT_ID}.json").write_text("{ not json")
    with capture_loguru(level="WARNING") as log_output:
        reopened = WorkspaceInstallSettingsStore(records_dir=tmp_path)
        assert reopened.read(_AGENT_ID) == WorkspaceInstallSettings()
    assert reopened.read(_OTHER_AGENT_ID).client_permissions == {"microphone": ClientPermissionDecision.GRANTED}
    assert "not valid" in log_output.getvalue()


def test_a_file_whose_name_is_not_a_workspace_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "notes.json").write_text("{}")
    with capture_loguru(level="WARNING") as log_output:
        assert WorkspaceInstallSettingsStore(records_dir=tmp_path).read(_AGENT_ID) == WorkspaceInstallSettings()
    assert "non-agent-id" in log_output.getvalue()


def test_a_record_from_a_newer_build_is_read_but_refuses_writes(tmp_path: Path) -> None:
    """Spec section 4.3: a too-new record stays resolvable, and this build must not rewrite what it cannot read."""
    (tmp_path / f"{_AGENT_ID}.json").write_text(
        json.dumps(
            {
                "settings_format": SUPPORTED_SETTINGS_FORMAT + 1,
                "client_permissions": {"microphone": "granted", "camera": "denied"},
                "future_key": True,
            }
        )
    )
    store = WorkspaceInstallSettingsStore(records_dir=tmp_path)
    assert store.read(_AGENT_ID).client_permissions == {
        "microphone": ClientPermissionDecision.GRANTED,
        "camera": ClientPermissionDecision.DENIED,
    }
    with pytest.raises(WorkspaceInstallSettingsTooNewError):
        store.set_client_permission(_AGENT_ID, ClientPermissionName.MICROPHONE, ClientPermissionDecision.DENIED)
    assert store.read(_AGENT_ID).client_permissions["microphone"] is ClientPermissionDecision.GRANTED


def test_a_write_keeps_what_a_newer_build_recorded_beside_it(tmp_path: Path) -> None:
    """A name this build does not declare, and a key it does not know, both survive a write of the one it does."""
    (tmp_path / f"{_AGENT_ID}.json").write_text(
        json.dumps(
            {
                "settings_format": SUPPORTED_SETTINGS_FORMAT,
                "client_permissions": {"camera": "denied"},
                "future_key": "kept",
            }
        )
    )
    store = WorkspaceInstallSettingsStore(records_dir=tmp_path)
    store.set_client_permission(_AGENT_ID, ClientPermissionName.MICROPHONE, ClientPermissionDecision.GRANTED)
    assert json.loads((tmp_path / f"{_AGENT_ID}.json").read_text()) == {
        "settings_format": SUPPORTED_SETTINGS_FORMAT,
        "client_permissions": {"camera": "denied", "microphone": "granted"},
        "future_key": "kept",
    }
    # Deleting this build's one entry leaves a record that still says something, so the file stays.
    store.set_client_permission(_AGENT_ID, ClientPermissionName.MICROPHONE, None)
    assert json.loads((tmp_path / f"{_AGENT_ID}.json").read_text())["client_permissions"] == {"camera": "denied"}


def test_a_change_tells_whoever_is_watching(tmp_path: Path) -> None:
    store = WorkspaceInstallSettingsStore(records_dir=tmp_path)
    changes: list[int] = []
    store.add_on_change_callback(lambda: changes.append(1))
    store.set_client_permission(_AGENT_ID, ClientPermissionName.MICROPHONE, ClientPermissionDecision.GRANTED)
    store.set_client_permission(_AGENT_ID, ClientPermissionName.MICROPHONE, None)
    store.set_client_permission(_OTHER_AGENT_ID, ClientPermissionName.MICROPHONE, ClientPermissionDecision.ASK)
    store.forget_workspace(_OTHER_AGENT_ID)
    assert len(changes) == 4


def test_a_failing_watcher_does_not_lose_the_write_or_the_other_watchers(tmp_path: Path) -> None:
    store = WorkspaceInstallSettingsStore(records_dir=tmp_path)
    seen: list[int] = []

    def _fail() -> None:
        raise ValueError("watcher broke")

    store.add_on_change_callback(_fail)
    store.add_on_change_callback(lambda: seen.append(1))
    with capture_loguru(level="WARNING") as log_output:
        store.set_client_permission(_AGENT_ID, ClientPermissionName.MICROPHONE, ClientPermissionDecision.GRANTED)
    assert seen == [1]
    assert store.read(_AGENT_ID).client_permissions == {"microphone": ClientPermissionDecision.GRANTED}
    assert "watcher broke" in log_output.getvalue()


def test_the_record_dir_is_named_by_agent_id(tmp_path: Path) -> None:
    """A real id round-trips through the file name, which is how the directory is read back."""
    agent_id = AgentId.generate()
    records_dir = tmp_path / "workspace_install_settings"
    WorkspaceInstallSettingsStore(records_dir=records_dir).set_client_permission(
        str(agent_id), ClientPermissionName.MICROPHONE, ClientPermissionDecision.GRANTED
    )
    assert [path.name for path in records_dir.iterdir()] == [f"{agent_id}.json"]
    assert WorkspaceInstallSettingsStore(records_dir=records_dir).read(str(agent_id)).client_permissions == {
        "microphone": ClientPermissionDecision.GRANTED
    }

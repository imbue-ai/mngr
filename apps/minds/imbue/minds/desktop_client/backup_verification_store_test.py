"""Unit tests for the per-workspace backup verification flag over the facts cache."""

import json
from pathlib import Path

from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backup_verification_store import flush_pending_backup_verification_writes
from imbue.minds.desktop_client.backup_verification_store import is_backup_verification_enabled
from imbue.minds.desktop_client.backup_verification_store import set_backup_verification_enabled
from imbue.minds.desktop_client.testing import exec_result_stdout
from imbue.minds.desktop_client.workspace_facts_cache import WorkspaceFactsCache
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.minds.workspace_programs.backup_protocol import VERIFICATION_FLAG_RESULT_MARKER
from imbue.mngr.primitives import AgentId


def _cache(tmp_path: Path) -> WorkspaceFactsCache:
    return WorkspaceFactsCache(records_dir=tmp_path / "workspace_facts_cache")


def _flag_answer(*, is_disabled: bool, is_set_applied: bool) -> MngrCallResult:
    payload = {"schema": 1, "verification_disabled": is_disabled, "is_set_applied": is_set_applied}
    return MngrCallResult(
        returncode=0, stdout=exec_result_stdout(f"{VERIFICATION_FLAG_RESULT_MARKER}{json.dumps(payload)}\n")
    )


def test_verification_is_enabled_by_default(tmp_path: Path) -> None:
    assert is_backup_verification_enabled(_cache(tmp_path), AgentId.generate()) is True


def test_disabling_answers_at_once_and_writes_into_the_workspace(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=_flag_answer(is_disabled=True, is_set_applied=True))
    resolver = StaticBackendResolver(url_by_agent_and_service={})

    set_backup_verification_enabled(cache, agent_id, False, mngr_caller=caller, backend_resolver=resolver)

    assert is_backup_verification_enabled(cache, agent_id) is False
    # Other workspaces are unaffected.
    assert is_backup_verification_enabled(cache, AgentId.generate()) is True
    assert cache.read(agent_id).is_backup_verification_dirty is False
    assert any("--set" in part for call in caller.calls for part in call)


def test_a_failed_write_through_stays_queued_and_still_answers_from_the_cache(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    agent_id = AgentId.generate()
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="host is stopped"))
    resolver = StaticBackendResolver(url_by_agent_and_service={})

    set_backup_verification_enabled(cache, agent_id, False, mngr_caller=caller, backend_resolver=resolver)

    assert is_backup_verification_enabled(cache, agent_id) is False
    assert cache.read(agent_id).is_backup_verification_dirty is True


def test_the_flush_writes_queued_flags_into_reachable_workspaces(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    agent_id = AgentId.generate()
    cache.set_backup_verification_enabled(agent_id, False)
    caller = RecordingMngrCaller(result=_flag_answer(is_disabled=True, is_set_applied=True))
    resolver = StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}})

    flush_pending_backup_verification_writes(cache, mngr_caller=caller, backend_resolver=resolver)

    assert cache.read(agent_id).is_backup_verification_dirty is False
    assert len(caller.calls) == 1


def test_the_flush_leaves_a_clean_cache_alone(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    caller = RecordingMngrCaller()
    resolver = StaticBackendResolver(url_by_agent_and_service={})

    flush_pending_backup_verification_writes(cache, mngr_caller=caller, backend_resolver=resolver)

    assert caller.calls == []

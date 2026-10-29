from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.minds.desktop_client.workspace_facts_cache import WorkspaceFactsCache
from imbue.minds.desktop_client.workspace_facts_cache import WorkspaceSkippedTarget
from imbue.minds.desktop_client.workspace_facts_cache import WorkspaceUpdateDismissals
from imbue.minds.desktop_client.workspace_facts_cache import is_run_dismissed
from imbue.minds.desktop_client.workspace_facts_cache import parse_workspace_dismissals
from imbue.minds.desktop_client.workspace_facts_cache import parse_workspace_skipped_target
from imbue.mngr.primitives import AgentId

_EARLIER = datetime(2026, 10, 1, tzinfo=timezone.utc)
_LATER = datetime(2026, 10, 20, tzinfo=timezone.utc)


def _cache(tmp_path: Path) -> WorkspaceFactsCache:
    return WorkspaceFactsCache(records_dir=tmp_path / "workspace_facts_cache")


def test_a_dismissal_is_queued_and_survives_a_relaunch(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    agent_id = AgentId.generate()
    cache.dismiss_outcome(agent_id, _EARLIER)

    relaunched = _cache(tmp_path).read(agent_id)

    assert relaunched.outcome_run_started_at == _EARLIER
    assert relaunched.is_outcome_dirty is True


def test_dismissing_an_older_run_never_rolls_a_newer_dismissal_back(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    agent_id = AgentId.generate()
    cache.dismiss_note(agent_id, _LATER)
    cache.dismiss_note(agent_id, _EARLIER)

    assert cache.read(agent_id).note_run_started_at == _LATER


def test_adopting_a_workspace_read_that_covers_the_queued_dismissal_goes_clean(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    agent_id = AgentId.generate()
    cache.dismiss_outcome(agent_id, _EARLIER)

    cache.adopt_update_facts(agent_id, None, WorkspaceUpdateDismissals(outcome_run_started_at=_EARLIER))

    record = cache.read(agent_id)
    assert record.outcome_run_started_at == _EARLIER
    assert record.is_outcome_dirty is False


def test_adopting_a_read_from_before_the_write_through_stays_dirty(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    agent_id = AgentId.generate()
    cache.dismiss_outcome(agent_id, _LATER)

    # The workspace only knows an older dismissal (another computer's); ours still has to land.
    cache.adopt_update_facts(agent_id, None, WorkspaceUpdateDismissals(outcome_run_started_at=_EARLIER))

    record = cache.read(agent_id)
    assert record.outcome_run_started_at == _LATER
    assert record.is_outcome_dirty is True


def test_a_workspace_without_a_fact_never_clears_the_cached_one(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    agent_id = AgentId.generate()
    cache.record_skip(agent_id, "minds-v0.9.0")
    cache.dismiss_note(agent_id, _EARLIER)

    cache.adopt_update_facts(agent_id, None, None)

    record = cache.read(agent_id)
    assert record.skipped_target_ref == "minds-v0.9.0"
    assert record.note_run_started_at == _EARLIER


def test_a_newer_skip_from_the_workspace_replaces_the_cached_one(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    agent_id = AgentId.generate()
    cache.record_skip(agent_id, "minds-v0.9.0")

    cache.adopt_update_facts(
        agent_id,
        WorkspaceSkippedTarget(target_ref="minds-v0.9.5", skipped_at=datetime.now(timezone.utc)),
        None,
    )

    record = cache.read(agent_id)
    assert record.skipped_target_ref == "minds-v0.9.5"
    assert record.is_skip_dirty is False


def test_an_older_workspace_skip_does_not_replace_a_queued_newer_one(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    agent_id = AgentId.generate()
    cache.record_skip(agent_id, "minds-v0.9.5")

    cache.adopt_update_facts(agent_id, WorkspaceSkippedTarget(target_ref="minds-v0.9.0", skipped_at=_EARLIER), None)

    record = cache.read(agent_id)
    assert record.skipped_target_ref == "minds-v0.9.5"
    assert record.is_skip_dirty is True


def test_pending_arguments_carry_exactly_the_queued_halves(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    agent_id = AgentId.generate()
    assert cache.pending_update_fact_arguments(agent_id) == []

    cache.record_skip(agent_id, "minds-v0.9.0")
    cache.dismiss_note(agent_id, _EARLIER)
    arguments = cache.pending_update_fact_arguments(agent_id)

    assert arguments[:2] == ["--skip-target", "minds-v0.9.0"]
    assert "--dismiss-note-at" in arguments
    assert "--dismiss-outcome-at" not in arguments


def test_backup_verification_adoption_honors_the_queued_write(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    agent_id = AgentId.generate()
    cache.set_backup_verification_enabled(agent_id, False)

    # A read that predates the write-through must not undo the queued choice.
    cache.adopt_backup_verification(agent_id, is_disabled=False, is_queued_write_applied=False)
    assert cache.read(agent_id).is_backup_verification_disabled is True

    cache.adopt_backup_verification(agent_id, is_disabled=True, is_queued_write_applied=True)
    record = cache.read(agent_id)
    assert record.is_backup_verification_disabled is True
    assert record.is_backup_verification_dirty is False


def test_a_stale_write_through_confirmation_never_clobbers_a_newer_toggle(tmp_path: Path) -> None:
    """A toggle made while a write-through exec is in flight must survive the late confirmation."""
    cache = _cache(tmp_path)
    agent_id = AgentId.generate()
    cache.set_backup_verification_enabled(agent_id, False)
    # The user re-enables while the disable's exec is still in flight.
    cache.set_backup_verification_enabled(agent_id, True)

    # The disable's confirmation lands late: it carried an older value, so it must not stick.
    cache.adopt_backup_verification(agent_id, is_disabled=True, is_queued_write_applied=True)
    record = cache.read(agent_id)
    assert record.is_backup_verification_disabled is False
    assert record.is_backup_verification_dirty is True

    # The enable's own write-through settles the record.
    cache.adopt_backup_verification(agent_id, is_disabled=False, is_queued_write_applied=True)
    record = cache.read(agent_id)
    assert record.is_backup_verification_disabled is False
    assert record.is_backup_verification_dirty is False


def test_parse_helpers_tolerate_malformed_payloads() -> None:
    assert parse_workspace_skipped_target(None) is None
    assert parse_workspace_skipped_target({"target_ref": ""}) is None
    assert parse_workspace_dismissals("not a dict") is None
    parsed = parse_workspace_skipped_target({"target_ref": "minds-v1", "skipped_at": "soon"})
    assert parsed is not None and parsed.skipped_at is None


def test_is_run_dismissed_covers_the_run_and_every_older_one() -> None:
    assert is_run_dismissed(None, _EARLIER) is False
    assert is_run_dismissed(_LATER, _EARLIER) is True
    assert is_run_dismissed(_EARLIER, _LATER) is False
    assert is_run_dismissed(_EARLIER, None) is True

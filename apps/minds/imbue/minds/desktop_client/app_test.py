"""Unit tests for desktop-client request helpers that don't need a live app.

Currently scoped to ``_resolve_destroying_for_landing`` -- the landing-page
helper that turns on-disk destroy records into per-row markers and finalizes
completed destroys. Its status inputs are covered exhaustively in
``destroying_test.py``; here we pin the landing-specific finalize timing.
"""

import os
from pathlib import Path

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import _landing_agent_ids_to_display
from imbue.minds.desktop_client.app import _resolve_destroying_for_landing
from imbue.mngr.primitives import AgentId


def _write_record(tmp_path: Path, agent_id: AgentId, pid: int, result: str | None) -> Path:
    dir_path = tmp_path / "destroying" / str(agent_id)
    dir_path.mkdir(parents=True)
    (dir_path / "pid").write_text(f"{pid}\n")
    if result is not None:
        (dir_path / "result").write_text(f"{result}\n")
    return dir_path


def test_resolve_destroying_none_paths_returns_empty() -> None:
    assert _resolve_destroying_for_landing(None, ()) == {}


def test_resolve_destroying_marks_running_while_in_flight(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    # Live pid (the test process itself), no recorded result -> running.
    _write_record(tmp_path, agent_id, pid=os.getpid(), result=None)
    marker = _resolve_destroying_for_landing(paths, (agent_id,))
    assert marker == {str(agent_id): "running"}


def test_resolve_destroying_marks_failed_on_nonzero_exit(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    dir_path = _write_record(tmp_path, agent_id, pid=os.getpid(), result="1")
    # Failed regardless of whether the agent is still in the resolver.
    marker = _resolve_destroying_for_landing(paths, ())
    assert marker == {str(agent_id): "failed"}
    assert dir_path.exists()


def test_resolve_destroying_keeps_marker_for_done_until_agent_gone(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    dir_path = _write_record(tmp_path, agent_id, pid=os.getpid(), result="0")
    # Succeeded, but discovery still lists the agent -> keep "Destroying…"
    # and leave the record in place rather than flicker to a normal row.
    marker = _resolve_destroying_for_landing(paths, (agent_id,))
    assert marker == {str(agent_id): "running"}
    assert dir_path.exists()


def test_resolve_destroying_finalizes_done_when_agent_gone(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    dir_path = _write_record(tmp_path, agent_id, pid=os.getpid(), result="0")
    # Succeeded and discovery has dropped the agent -> finalize: no marker,
    # record deleted.
    marker = _resolve_destroying_for_landing(paths, ())
    assert marker == {}
    assert not dir_path.exists()


def test_display_ids_passes_through_discovered_agents_with_no_destroy() -> None:
    a, b = AgentId.generate(), AgentId.generate()
    assert _landing_agent_ids_to_display((a, b), {}) == (a, b)


def test_display_ids_does_not_duplicate_a_discovered_agent_being_destroyed() -> None:
    a = AgentId.generate()
    assert _landing_agent_ids_to_display((a,), {str(a): "running"}) == (a,)


def test_display_ids_surfaces_failed_orphan_not_in_resolver() -> None:
    # A failed destroy whose agent discovery no longer lists must still
    # appear, so a still-billing host can't become invisible.
    discovered = AgentId.generate()
    orphan = AgentId.generate()
    result = _landing_agent_ids_to_display((discovered,), {str(orphan): "failed"})
    assert result == (discovered, orphan)


def test_display_ids_surfaces_orphan_when_nothing_discovered() -> None:
    orphan = AgentId.generate()
    assert _landing_agent_ids_to_display((), {str(orphan): "failed"}) == (orphan,)

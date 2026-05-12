"""Unit tests for the DENY-mode label-driven SessionStart reaper."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.utils.testing import make_test_agent_details
from imbue.mngr_claude_subagent_proxy.hooks import deny_reap


def _child(name: str, parent_id: str, state: AgentLifecycleState) -> AgentDetails:
    """Build an AgentDetails with the parent_id label set to ``parent_id``."""
    return make_test_agent_details(
        name=name,
        state=state,
        labels={deny_reap.PARENT_ID_LABEL: parent_id},
    )


def _unlabeled(name: str, state: AgentLifecycleState) -> AgentDetails:
    """Build an AgentDetails with no parent_id label (e.g. a top-level agent)."""
    return make_test_agent_details(name=name, state=state)


def test_find_terminal_children_returns_only_terminal_for_matching_parent() -> None:
    """The reaper picks up DONE / STOPPED children whose parent_id label matches.

    Non-matching parents are ignored entirely; matching parents whose
    state is not terminal (RUNNING / WAITING) are also left alone, since
    they may still be doing useful work.
    """
    agents = {
        "child-done": _child("child-done", parent_id="parent-A", state=AgentLifecycleState.DONE),
        "child-stopped": _child("child-stopped", parent_id="parent-A", state=AgentLifecycleState.STOPPED),
        "child-running": _child("child-running", parent_id="parent-A", state=AgentLifecycleState.RUNNING),
        "child-waiting": _child("child-waiting", parent_id="parent-A", state=AgentLifecycleState.WAITING),
        "other-parent-done": _child("other-parent-done", parent_id="parent-B", state=AgentLifecycleState.DONE),
        "unrelated": _unlabeled("unrelated", state=AgentLifecycleState.DONE),
    }

    terminals = deny_reap.find_terminal_children("parent-A", agents)

    matched_names = sorted(child.name for child in terminals)
    assert matched_names == ["child-done", "child-stopped"]


def test_find_terminal_children_returns_empty_when_no_matches() -> None:
    """No-op if no children match the parent_id label."""
    agents = {
        "other": _child("other", parent_id="parent-B", state=AgentLifecycleState.DONE),
        "unlabeled": _unlabeled("unlabeled", state=AgentLifecycleState.DONE),
    }
    assert deny_reap.find_terminal_children("parent-A", agents) == []


def test_run_skips_when_parent_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without MNGR_AGENT_ID the reaper cannot identify its children; no-op."""
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", "/tmp/state")

    destroy_calls: list[tuple[str, Path]] = []

    def list_stub() -> dict[str, AgentDetails]:
        return {"child": _child("child", "parent-A", AgentLifecycleState.DONE)}

    deny_reap.run(
        stdin=io.StringIO(""),
        list_callable=list_stub,
        destroy_callable=lambda name, log: destroy_calls.append((name, log)),
    )

    assert destroy_calls == []


def test_run_skips_when_state_dir_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without MNGR_AGENT_STATE_DIR the reaper can't anchor the destroy log; no-op."""
    monkeypatch.setenv("MNGR_AGENT_ID", "parent-A")
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR", raising=False)

    destroy_calls: list[tuple[str, Path]] = []

    def list_stub() -> dict[str, AgentDetails]:
        return {"child": _child("child", "parent-A", AgentLifecycleState.DONE)}

    deny_reap.run(
        stdin=io.StringIO(""),
        list_callable=list_stub,
        destroy_callable=lambda name, log: destroy_calls.append((name, log)),
    )

    assert destroy_calls == []


def test_run_destroys_each_terminal_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Happy path: one destroy_callable per terminal child, log path under state_dir."""
    monkeypatch.setenv("MNGR_AGENT_ID", "parent-A")
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    destroy_calls: list[tuple[str, Path]] = []

    def list_stub() -> dict[str, AgentDetails]:
        return {
            "child-done": _child("child-done", "parent-A", AgentLifecycleState.DONE),
            "child-stopped": _child("child-stopped", "parent-A", AgentLifecycleState.STOPPED),
            "child-running": _child("child-running", "parent-A", AgentLifecycleState.RUNNING),
        }

    deny_reap.run(
        stdin=io.StringIO(""),
        list_callable=list_stub,
        destroy_callable=lambda name, log: destroy_calls.append((name, log)),
    )

    destroyed_names = sorted(name for name, _ in destroy_calls)
    assert destroyed_names == ["child-done", "child-stopped"]
    expected_log = tmp_path / "subagent_destroy.log"
    assert all(log == expected_log for _, log in destroy_calls)


def test_run_no_op_when_list_callable_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If `mngr list` returns None (failure), the reaper bails without destroying anything."""
    monkeypatch.setenv("MNGR_AGENT_ID", "parent-A")
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(tmp_path))

    destroy_calls: list[tuple[str, Path]] = []

    deny_reap.run(
        stdin=io.StringIO(""),
        list_callable=lambda: None,
        destroy_callable=lambda name, log: destroy_calls.append((name, log)),
    )

    assert destroy_calls == []

"""Tests for the discovery-poll agent registry and its live-coding filter."""

from __future__ import annotations

import queue
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr_foreman import agent_registry as ar
from imbue.mngr_foreman.agent_registry import AgentRegistry


def _fake_agent(agent_id: str, name: str, state: str = "WAITING", agent_type: str = "claude") -> AgentDetails:
    """Minimal stand-in exposing the fields the registry filter + projection read."""
    return cast(
        AgentDetails,
        SimpleNamespace(
            id=agent_id,
            name=name,
            type=agent_type,
            state=state,
            host=SimpleNamespace(name="boxa", provider_name="ssh"),
            labels={},
            agent_activity_time=None,
            user_activity_time=None,
            create_time=None,
        ),
    )


def _registry() -> AgentRegistry:
    return AgentRegistry(cast(MngrContext, SimpleNamespace()))


def _patch_list(monkeypatch: pytest.MonkeyPatch, agents: tuple[AgentDetails, ...]) -> None:
    monkeypatch.setattr(ar, "list_agents", lambda *_a, **_k: SimpleNamespace(agents=agents))


def test_poll_keeps_only_live_coding_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    # A running claude and a waiting codex are shown; a stopped claude, a done
    # opencode, and a running non-coding worker are all hidden.
    _patch_list(
        monkeypatch,
        (
            _fake_agent("a1", "alpha", state="RUNNING", agent_type="claude"),
            _fake_agent("a2", "beta", state="WAITING", agent_type="codex"),
            _fake_agent("a3", "gamma", state="STOPPED", agent_type="claude"),
            _fake_agent("a4", "delta", state="DONE", agent_type="opencode"),
            _fake_agent("a5", "worker", state="RUNNING", agent_type="mngr_worker"),
        ),
    )
    registry = _registry()
    registry._poll_once()
    assert {c["name"] for c in registry.snapshot()} == {"alpha", "beta"}


@pytest.mark.parametrize("agent_type", ["claude", "codex", "opencode", "pi-coding"])
def test_each_coding_type_is_shown(monkeypatch: pytest.MonkeyPatch, agent_type: str) -> None:
    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state="RUNNING", agent_type=agent_type),))
    registry = _registry()
    registry._poll_once()
    assert [c["name"] for c in registry.snapshot()] == ["alpha"]


@pytest.mark.parametrize("state", ["STOPPED", "DONE", "REPLACED", "UNKNOWN", "RUNNING_UNKNOWN_AGENT_TYPE"])
def test_non_live_states_are_hidden(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state=state, agent_type="claude"),))
    registry = _registry()
    registry._poll_once()
    assert registry.snapshot() == []


def test_get_agent_by_name_and_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state="RUNNING"),))
    registry = _registry()
    registry._poll_once()
    assert registry.get_agent("alpha") is not None
    assert registry.get_agent("a1") is not None
    assert registry.get_agent("nope") is None


def test_poll_broadcasts_snapshot_only_on_change(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state="RUNNING"),))
    registry = _registry()
    q: queue.Queue[dict] = queue.Queue()
    registry._subscribers.add(q)
    registry._poll_once()
    registry._poll_once()  # identical result -> must not re-broadcast
    assert q.qsize() == 1
    msg = q.get_nowait()
    assert msg["type"] == "snapshot"
    assert [a["name"] for a in msg["agents"]] == ["alpha"]


def test_on_change_fires_only_when_name_set_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    fired: list[int] = []
    registry = _registry()
    registry.set_on_change(lambda: fired.append(1))

    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state="RUNNING"),))
    registry._poll_once()
    registry._poll_once()  # same name set -> no extra fire
    assert len(fired) == 1

    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state="RUNNING"), _fake_agent("a2", "beta", state="WAITING")))
    registry._poll_once()  # beta appeared -> fire so the pool warms it
    assert len(fired) == 2

    _patch_list(monkeypatch, ())
    registry._poll_once()  # both gone -> fire so the pool drops them
    assert len(fired) == 3


def test_poll_keeps_last_set_on_list_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state="RUNNING"),))
    registry = _registry()
    registry._poll_once()

    def _boom(*_a: object, **_k: object) -> Any:
        raise RuntimeError("provider down")

    monkeypatch.setattr(ar, "list_agents", _boom)
    registry._poll_once()  # must not raise; keeps the previous set
    assert [c["name"] for c in registry.snapshot()] == ["alpha"]


def test_start_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_list(monkeypatch, ())
    registry = _registry()
    registry.start()
    first = registry._thread
    registry.start()
    try:
        assert registry._thread is first  # one poll thread, not two
    finally:
        registry.stop()


def test_subscribe_yields_initial_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_list(monkeypatch, (_fake_agent("a1", "alpha", state="RUNNING"),))
    registry = _registry()
    registry._poll_once()
    first = next(registry.subscribe())
    assert first["type"] == "snapshot"
    assert [a["name"] for a in first["agents"]] == ["alpha"]

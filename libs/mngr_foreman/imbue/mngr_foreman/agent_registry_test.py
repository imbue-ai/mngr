"""Tests for the observe-stream agent registry and its live-coding filter.

These feed synthetic ``mngr observe --stream-events`` JSONL lines into the consumer
(``_consume_line``) and assert the resulting snapshot, exactly as the real stdout
reader does -- no subprocess is spawned. The freshness watchdog is tested via its
pure decision (``_is_stale_at``) and its per-tick bounce (``_watchdog_tick``).
"""

from __future__ import annotations

import json
import queue
import time
from types import SimpleNamespace
from typing import Any
from typing import cast
from uuid import uuid4

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr_foreman import agent_registry as ar
from imbue.mngr_foreman.agent_registry import AgentRegistry


def _new_id(prefix: str) -> str:
    # AgentId / HostId require a 32-hex-char suffix.
    return f"{prefix}-{uuid4().hex}"


def _agent_dict(
    name: str,
    state: str = "WAITING",
    agent_type: str = "claude",
    provider: str = "docker",
    agent_id: str | None = None,
    host_id: str | None = None,
) -> dict[str, Any]:
    """A serialized ``AgentDetails`` exactly as it appears inside an observe event."""
    host_state = "UNKNOWN" if state == "UNKNOWN" else "RUNNING"
    agent = AgentDetails.model_validate(
        {
            "id": agent_id or _new_id("agent"),
            "name": name,
            "type": agent_type,
            "command": "claude",
            "work_dir": "/tmp/work",
            "initial_branch": None,
            "create_time": "2026-07-21T21:15:17.233784Z",
            "start_on_boot": False,
            "state": state,
            "host": {
                "id": host_id or _new_id("host"),
                "name": "boxa",
                "provider_name": provider,
                "state": host_state,
            },
        }
    )
    return agent.model_dump(mode="json")


def _full_state_line(*agents: dict[str, Any]) -> str:
    return json.dumps({"type": "AGENTS_FULL_STATE", "agents": list(agents)})


def _agent_state_line(agent: dict[str, Any]) -> str:
    return json.dumps({"type": "AGENT_STATE", "agent": agent})


def _agent_removed_line(agent_id: str, name: str = "gone") -> str:
    return json.dumps({"type": "AGENT_REMOVED", "agent_id": agent_id, "agent_name": name})


def _registry() -> AgentRegistry:
    return AgentRegistry(cast(MngrContext, SimpleNamespace()))


def _names(registry: AgentRegistry) -> set[str]:
    return {c["name"] for c in registry.snapshot()}


# --- filtering / folding ------------------------------------------------------


def test_full_state_shows_only_live_coding_agents() -> None:
    # A running claude and a waiting codex are shown; a stopped claude, a done
    # opencode, and a running non-coding worker are all hidden.
    registry = _registry()
    registry._consume_line(
        _full_state_line(
            _agent_dict("alpha", state="RUNNING", agent_type="claude"),
            _agent_dict("beta", state="WAITING", agent_type="codex"),
            _agent_dict("gamma", state="STOPPED", agent_type="claude"),
            _agent_dict("delta", state="DONE", agent_type="opencode"),
            _agent_dict("worker", state="RUNNING", agent_type="mngr_worker"),
        )
    )
    assert _names(registry) == {"alpha", "beta"}


@pytest.mark.parametrize("agent_type", ["claude", "codex", "opencode", "pi-coding"])
def test_each_coding_type_is_shown(agent_type: str) -> None:
    registry = _registry()
    registry._consume_line(_full_state_line(_agent_dict("alpha", state="RUNNING", agent_type=agent_type)))
    assert [c["name"] for c in registry.snapshot()] == ["alpha"]


@pytest.mark.parametrize("state", ["STOPPED", "DONE", "REPLACED", "RUNNING_UNKNOWN_AGENT_TYPE"])
def test_dead_states_are_hidden(state: str) -> None:
    registry = _registry()
    registry._consume_line(_full_state_line(_agent_dict("alpha", state=state, agent_type="claude")))
    assert registry.snapshot() == []


def test_unknown_state_is_kept_as_unreachable() -> None:
    # A provider went unreachable: mngr synthesizes the agent as UNKNOWN. We KEEP it
    # (marked UNKNOWN) instead of dropping it -- the keep-last-known guarantee.
    registry = _registry()
    registry._consume_line(_full_state_line(_agent_dict("alpha", state="UNKNOWN", agent_type="claude")))
    cards = registry.snapshot()
    assert [c["name"] for c in cards] == ["alpha"]
    assert cards[0]["state"] == "UNKNOWN"


def test_get_agent_by_name_and_id_returns_agent_details() -> None:
    registry = _registry()
    agent = _agent_dict("alpha", state="RUNNING", agent_id=_new_id("agent"))
    registry._consume_line(_full_state_line(agent))
    got = registry.get_agent("alpha")
    assert isinstance(got, AgentDetails)  # consumers read .state/.type/.host/.plugin off this
    assert registry.get_agent(agent["id"]) is not None
    assert registry.get_agent("nope") is None


def test_full_state_replaces_the_set() -> None:
    # A later full snapshot is authoritative: an agent absent from it (implicitly
    # destroyed on a healthy provider) is dropped; a still-present one stays.
    registry = _registry()
    alpha = _agent_dict("alpha", state="RUNNING", agent_id=_new_id("agent"))
    beta = _agent_dict("beta", state="WAITING", agent_id=_new_id("agent"))
    registry._consume_line(_full_state_line(alpha, beta))
    assert _names(registry) == {"alpha", "beta"}
    registry._consume_line(_full_state_line(alpha))  # beta gone from the snapshot
    assert _names(registry) == {"alpha"}


def test_provider_error_keeps_last_known_agent_via_full_state() -> None:
    # docker healthy -> the agent shows; docker unreachable -> mngr re-emits it as
    # UNKNOWN in the next full snapshot, so it stays in the list (never emptied).
    registry = _registry()
    aid = _new_id("agent")
    registry._consume_line(_full_state_line(_agent_dict("cloudie", state="WAITING", provider="docker", agent_id=aid)))
    assert _names(registry) == {"cloudie"}
    registry._consume_line(_full_state_line(_agent_dict("cloudie", state="UNKNOWN", provider="docker", agent_id=aid)))
    cards = registry.snapshot()
    assert [c["name"] for c in cards] == ["cloudie"]  # kept, not dropped
    assert cards[0]["state"] == "UNKNOWN"


def test_agent_state_upserts_a_single_agent() -> None:
    registry = _registry()
    registry._consume_line(_full_state_line(_agent_dict("alpha", state="RUNNING")))
    beta = _agent_dict("beta", state="WAITING")
    registry._consume_line(_agent_state_line(beta))  # a new agent appears
    assert _names(registry) == {"alpha", "beta"}
    # The same agent going to a dead state removes it from the shown set.
    registry._consume_line(_agent_state_line(_agent_dict("beta", state="STOPPED", agent_id=beta["id"])))
    assert _names(registry) == {"alpha"}


def test_agent_removed_drops_the_agent() -> None:
    registry = _registry()
    alpha = _agent_dict("alpha", state="RUNNING", agent_id=_new_id("agent"))
    registry._consume_line(_full_state_line(alpha))
    assert _names(registry) == {"alpha"}
    registry._consume_line(_agent_removed_line(alpha["id"], "alpha"))
    assert registry.snapshot() == []


def test_non_json_line_is_ignored() -> None:
    registry = _registry()
    registry._consume_line(_full_state_line(_agent_dict("alpha", state="RUNNING")))
    registry._consume_line("not json at all\n")  # a stray log line must not crash or clear
    registry._consume_line("")
    assert _names(registry) == {"alpha"}


# --- broadcast / on_change ----------------------------------------------------


def test_broadcasts_snapshot_only_on_change() -> None:
    registry = _registry()
    q: queue.Queue[dict] = queue.Queue()
    registry._subscribers.add(q)
    line = _full_state_line(_agent_dict("alpha", state="RUNNING", agent_id=_new_id("agent")))
    registry._consume_line(line)
    registry._consume_line(line)  # identical snapshot -> must not re-broadcast
    assert q.qsize() == 1
    msg = q.get_nowait()
    assert msg["type"] == "snapshot"
    assert [a["name"] for a in msg["agents"]] == ["alpha"]


def test_on_change_fires_only_when_name_set_changes() -> None:
    fired: list[int] = []
    registry = _registry()
    registry.set_on_change(lambda: fired.append(1))

    alpha = _agent_dict("alpha", state="RUNNING", agent_id=_new_id("agent"))
    registry._consume_line(_full_state_line(alpha))
    registry._consume_line(_full_state_line(alpha))  # same name set -> no extra fire
    assert len(fired) == 1

    beta = _agent_dict("beta", state="WAITING", agent_id=_new_id("agent"))
    registry._consume_line(_full_state_line(alpha, beta))  # beta appeared -> fire (pool warms it)
    assert len(fired) == 2

    registry._consume_line(_full_state_line())  # both gone -> fire (pool drops them)
    assert len(fired) == 3


def test_subscribe_yields_initial_snapshot() -> None:
    registry = _registry()
    registry._consume_line(_full_state_line(_agent_dict("alpha", state="RUNNING")))
    first = next(registry.subscribe())
    assert first["type"] == "snapshot"
    assert [a["name"] for a in first["agents"]] == ["alpha"]


# --- freshness watchdog -------------------------------------------------------


def test_is_stale_at_startup_grace() -> None:
    # Before the first event, the startup bound applies.
    just_after = ar._OBSERVE_STARTUP_STALE_SECONDS - 1.0
    just_over = ar._OBSERVE_STARTUP_STALE_SECONDS + 1.0
    assert not AgentRegistry._is_stale_at(now=just_after, last_event_at=0.0, seen_event=False)
    assert AgentRegistry._is_stale_at(now=just_over, last_event_at=0.0, seen_event=False)


def test_is_stale_at_steady_state() -> None:
    # After the first event, a much wider bound applies (a healthy idle fleet only
    # re-emits the periodic full snapshot), so the startup window alone is not stale.
    assert not AgentRegistry._is_stale_at(
        now=ar._OBSERVE_STARTUP_STALE_SECONDS + 5.0, last_event_at=0.0, seen_event=True
    )
    assert not AgentRegistry._is_stale_at(now=ar._OBSERVE_STALE_SECONDS - 1.0, last_event_at=0.0, seen_event=True)
    assert AgentRegistry._is_stale_at(now=ar._OBSERVE_STALE_SECONDS + 1.0, last_event_at=0.0, seen_event=True)


def test_consume_line_marks_freshness() -> None:
    registry = _registry()
    assert registry._seen_event is False
    registry._consume_line(_full_state_line(_agent_dict("alpha", state="RUNNING")))
    assert registry._seen_event is True
    assert registry._last_event_at > 0.0


class _FakeProc:
    """A stand-in RunningProcess recording terminate() for the watchdog tests."""

    def __init__(self, alive: bool = True) -> None:
        self._alive = alive
        self.terminated = False

    def poll(self) -> int | None:
        return None if self._alive else 0

    def terminate(self, force_kill_seconds: float = 5.0) -> None:
        self.terminated = True
        self._alive = False


def test_watchdog_bounces_a_silent_stream() -> None:
    registry = _registry()
    proc = _FakeProc(alive=True)
    registry._proc = cast(Any, proc)
    registry._seen_event = True
    # Last event is well past the steady-state freshness bound -> wedged pipeline.
    registry._last_event_at = time.monotonic() - (ar._OBSERVE_STALE_SECONDS + 10.0)
    assert registry._watchdog_tick() is True
    assert proc.terminated is True  # bounced (reader loop will respawn on EOF)


def test_watchdog_leaves_a_fresh_stream_alone() -> None:
    registry = _registry()
    proc = _FakeProc(alive=True)
    registry._proc = cast(Any, proc)
    registry._seen_event = True
    registry._last_event_at = time.monotonic()  # just saw an event
    assert registry._watchdog_tick() is False
    assert proc.terminated is False


def test_watchdog_ignores_a_dead_process() -> None:
    # A process that already exited is the reader loop's job to respawn, not the
    # watchdog's to bounce.
    registry = _registry()
    proc = _FakeProc(alive=False)
    registry._proc = cast(Any, proc)
    registry._seen_event = True
    registry._last_event_at = time.monotonic() - (ar._OBSERVE_STALE_SECONDS + 10.0)
    assert registry._watchdog_tick() is False
    assert proc.terminated is False


def test_start_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry()
    # Don't launch a real observer: a no-op spawn keeps the reader loop harmless.
    monkeypatch.setattr(registry, "_spawn", lambda: None)
    registry.start()
    first = registry._thread
    registry.start()
    try:
        assert registry._thread is first  # one observe thread, not two
    finally:
        registry.stop()

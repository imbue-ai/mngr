"""Route-level tests for the Flask app via a test client (fakes for pool/registry)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_foreman.agent_registry import AgentRegistry
from imbue.mngr_foreman.connection_pool import ConnectionPool
from imbue.mngr_foreman.server import create_app

# foreman-local state (backburner/shortcuts) lives under mngr_ctx.config.default_host_dir;
# point it at a throwaway temp dir for the route tests.
_STATE_DIR = Path(tempfile.mkdtemp(prefix="foreman-test-state-"))

_CARD = {
    "id": "agent-1",
    "name": "worker",
    "type": "claude",
    "state": "WAITING",
    "host_name": "boxb",
    "provider": "ssh",
    "labels": {},
    "activity_time": None,
    "supports_chat": True,
}


class _FakeRegistry:
    def __init__(self, cards: list[dict], agents: dict[str, Any] | None = None) -> None:
        self._cards = cards
        self._agents = agents or {}

    def snapshot(self) -> list[dict]:
        return self._cards

    def get_agent(self, name: str) -> Any:
        return self._agents.get(name)

    def set_backburner_predicate(self, _pred: Any) -> None:
        pass

    def republish(self) -> None:
        pass


def _client(registry: _FakeRegistry | None = None) -> Any:
    reg = registry or _FakeRegistry([_CARD], agents={"worker": SimpleNamespace(type="claude", name="worker")})
    pool = SimpleNamespace(mngr_ctx=SimpleNamespace())
    ctx = SimpleNamespace(config=SimpleNamespace(default_host_dir=_STATE_DIR))
    app = create_app(cast(MngrContext, ctx), cast(AgentRegistry, reg), cast(ConnectionPool, pool), 20000)
    app.config["TESTING"] = True
    return app.test_client()


def test_index_page_served() -> None:
    resp = _client().get("/")
    assert resp.status_code == 200
    assert b"foreman" in resp.data.lower()


def test_static_js_revalidate_and_gzip() -> None:
    resp = _client().get("/static/app.js", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-cache"
    assert resp.headers.get("Content-Encoding") == "gzip"


def test_vendor_asset_immutable_cache() -> None:
    # atkinson.css is the one vendor/ file that still ships in the package (the
    # rest are fetched at runtime); it exercises the immutable-cache header path.
    resp = _client().get("/static/vendor/atkinson.css")
    assert resp.status_code == 200
    assert "immutable" in resp.headers["Cache-Control"]


def test_static_missing_is_404() -> None:
    assert _client().get("/static/does-not-exist.js").status_code == 404


def test_static_traversal_blocked() -> None:
    # ".." is rejected by _read_static before touching the filesystem.
    assert _client().get("/static/../server.py").status_code == 404


def test_api_agents_lists_cards() -> None:
    resp = _client().get("/api/agents")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["agents"][0]["name"] == "worker"


def test_input_state_unknown_agent_not_running() -> None:
    resp = _client().get("/api/agents/nope/input-state")
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["running"] is False and d["busy"] is False


def _agent(agent_type: str, state: str, plugin: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(type=agent_type, name="worker", state=state, plugin=plugin or {})


def test_input_state_unsupported_type_not_running() -> None:
    # A type foreman has no chat strategy for (no registry row) reports not-running
    # -- it is gated out before any state/pane work.
    reg = _FakeRegistry([_CARD], agents={"worker": _agent("antigravity", "RUNNING")})
    d = _client(reg).get("/api/agents/worker/input-state").get_json()
    assert d["running"] is False and d["busy"] is False


def test_input_state_opencode_permission_blocked_pane_less() -> None:
    # opencode surfaces a permission block via the waiting_reason field (state
    # WAITING), with no tmux pane capture -- so this resolves with a fake pool that
    # cannot capture panes.
    plugin = {"opencode": {"waiting_reason": "PERMISSIONS"}}
    reg = _FakeRegistry([_CARD], agents={"worker": _agent("opencode", "WAITING", plugin)})
    d = _client(reg).get("/api/agents/worker/input-state").get_json()
    assert d["blocked"] is True and d["reason"] == "permission prompt"
    assert d["running"] is True and d["busy"] is False


def test_input_state_codex_permission_blocked_pane_less() -> None:
    # codex blocks like opencode: a tool-approval prompt promotes it to WAITING and
    # publishes waiting_reason == PERMISSIONS, surfaced as needs-input with no pane
    # scrape (not a bare WAITING dot).
    plugin = {"codex": {"waiting_reason": "PERMISSIONS"}}
    reg = _FakeRegistry([_CARD], agents={"worker": _agent("codex", "WAITING", plugin)})
    d = _client(reg).get("/api/agents/worker/input-state").get_json()
    assert d["blocked"] is True and d["reason"] == "permission prompt"
    assert d["running"] is True and d["busy"] is False


def test_input_state_opencode_running_not_blocked() -> None:
    reg = _FakeRegistry([_CARD], agents={"worker": _agent("opencode", "RUNNING")})
    d = _client(reg).get("/api/agents/worker/input-state").get_json()
    assert d["blocked"] is False and d["reason"] is None
    assert d["running"] is True and d["busy"] is True


def test_message_empty_is_400() -> None:
    resp = _client().post("/api/agents/worker/message", json={"message": "   "})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_transcript_image_bad_id_is_404() -> None:
    resp = _client().get("/api/agents/worker/timage/../etc/passwd")
    assert resp.status_code == 404


def test_upload_missing_file_is_400() -> None:
    resp = _client().post("/api/agents/worker/upload", data={})
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "path,expected",
    [
        ("agent.html", "text/html; charset=utf-8"),
        ("app.js", "application/javascript; charset=utf-8"),
        ("foreman.css", "text/css; charset=utf-8"),
    ],
)
def test_content_type_for_known_assets(path: str, expected: str) -> None:
    from imbue.mngr_foreman.server import _content_type_for

    assert _content_type_for(path) == expected

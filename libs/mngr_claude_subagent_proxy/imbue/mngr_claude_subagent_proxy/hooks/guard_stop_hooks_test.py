"""Unit tests for the PROXY-mode SessionStart guard hook (guard_stop_hooks.py).

The hook wraps every Stop / SubagentStop command in this agent's
per-agent plugin cache with the ``MNGR_CLAUDE_SUBAGENT_PROXY_CHILD``
env-conditional guard. The wrap is idempotent on subsequent SessionStarts.

The deep behavior of ``guard_per_agent_plugin_cache`` itself is tested
in ``hooks_test.py``; this file just pins that the SessionStart hook
calls it on the right state dir.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from imbue.mngr_claude_subagent_proxy.hooks import guard_stop_hooks


def _write_unguarded_orchestrator_hooks(path: Path) -> None:
    """Helper: write a hooks.json mimicking what Claude Code fetches from
    a stop-hook plugin marketplace (un-guarded orchestrator command)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "timeout": 900,
                                    "command": "${CLAUDE_PLUGIN_ROOT}/scripts/stop_hook_orchestrator.sh",
                                }
                            ]
                        }
                    ]
                }
            }
        )
        + "\n"
    )


def test_guard_stop_hooks_run_wraps_per_agent_plugin_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reading MNGR_AGENT_STATE_DIR from env, run() wraps every Stop hook
    in that agent's plugin cache with the proxy-child env guard.
    """
    state_dir = tmp_path / "agent-state"
    cache_hooks = (
        state_dir
        / "plugin"
        / "claude"
        / "anthropic"
        / "plugins"
        / "marketplaces"
        / "x"
        / "plugins"
        / "p"
        / "hooks"
        / "hooks.json"
    )
    _write_unguarded_orchestrator_hooks(cache_hooks)
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))

    guard_stop_hooks.run(io.StringIO(""))

    cmd = json.loads(cache_hooks.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd.startswith('[ -n "$MNGR_CLAUDE_SUBAGENT_PROXY_CHILD" ] && exit 0; '), (
        f"guard_stop_hooks.run did not wrap the per-agent plugin cache. Command: {cmd!r}"
    )


def test_guard_stop_hooks_run_noop_when_state_dir_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SessionStart fires on plain Claude sessions without mngr context;
    the hook must tolerate the missing-env case silently."""
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR", raising=False)
    # Should not raise.
    guard_stop_hooks.run(io.StringIO(""))

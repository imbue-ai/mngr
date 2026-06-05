"""Unit tests for the tmux target helpers' rendering contract.

End-to-end coverage of the underlying prefix-matching failure mode (the
polling-loop-never-terminates behavior) lives in the per-project background-tasks
regression tests:
- libs/mngr_claude/.../test_background_tasks_prefix_collision.py
- libs/mngr_gemini/.../test_background_tasks_prefix_collision.py
"""

import pydantic
import pytest

from imbue.mngr.hosts.tmux import TmuxSessionTarget
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.hosts.tmux import build_tmux_capture_pane_command


def test_tmux_session_target_renders_with_exact_match_prefix() -> None:
    target = TmuxSessionTarget(session_name="mngr-my-agent")
    assert target.session_name == "mngr-my-agent"
    assert target.as_shell_arg() == "=mngr-my-agent"


def test_tmux_session_target_as_target_arg_is_raw_unquoted() -> None:
    """as_target_arg is the raw `=name` for argv contexts; as_shell_arg wraps it in shell quoting."""
    plain = TmuxSessionTarget(session_name="mngr-my-agent")
    assert plain.as_target_arg() == "=mngr-my-agent"
    assert plain.as_shell_arg() == "=mngr-my-agent"

    spaced = TmuxSessionTarget(session_name="mngr-weird name")
    assert spaced.as_target_arg() == "=mngr-weird name"
    assert spaced.as_shell_arg() == "'=mngr-weird name'"


def test_tmux_window_target_default_window_is_zero() -> None:
    target = TmuxWindowTarget(session_name="mngr-my-agent")
    assert target.session_name == "mngr-my-agent"
    assert target.window == 0
    assert target.as_shell_arg() == "=mngr-my-agent:0"


def test_tmux_window_target_with_explicit_window() -> None:
    assert TmuxWindowTarget(session_name="mngr-my-agent", window=2).as_shell_arg() == "=mngr-my-agent:2"
    assert TmuxWindowTarget(session_name="mngr-my-agent", window="watcher").as_shell_arg() == "=mngr-my-agent:watcher"


def test_tmux_session_target_rejects_empty_session_name() -> None:
    with pytest.raises(pydantic.ValidationError):
        TmuxSessionTarget(session_name="")


def test_tmux_window_target_rejects_empty_session_name() -> None:
    with pytest.raises(pydantic.ValidationError):
        TmuxWindowTarget(session_name="")


def test_build_tmux_capture_pane_command_visible_only() -> None:
    result = build_tmux_capture_pane_command(TmuxWindowTarget(session_name="mngr-my-agent"))
    # shlex.quote leaves =, :, -, alnum unquoted (none are shell-special), so the
    # rendered target appears bare. The leading '=' still forces exact session
    # matching at the tmux argv level.
    assert result == "tmux capture-pane -t =mngr-my-agent:0 -p"


def test_build_tmux_capture_pane_command_with_scrollback() -> None:
    result = build_tmux_capture_pane_command(TmuxWindowTarget(session_name="mngr-my-agent"), include_scrollback=True)
    assert result == "tmux capture-pane -t =mngr-my-agent:0 -S - -p"

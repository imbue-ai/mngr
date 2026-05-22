"""Tests for tmux helpers, including end-to-end prefix-collision behavior.

The prefix-collision tests in this file exist because of a recurring class of bug:
tmux's ``-t <target>`` resolution falls back to *session prefix matching* when no
exact session matches. If a session named ``mngr-foo`` is gone but ``mngr-foo-bar``
is alive, ``tmux ... -t 'mngr-foo:0'`` silently lands on ``mngr-foo-bar`` and the
caller never knows. That can deliver keystrokes to the wrong agent, kill the
wrong session, or capture the wrong agent's pane content.

These tests set up the exact collision scenario and verify that targets built
via :class:`TmuxSessionTarget` / :class:`TmuxWindowTarget` (which always render
with a leading ``=``) refuse to misroute. They also document the parsing nuances
that are easy to get wrong: for target-window/-pane commands, the ``:window``
component is *required*, and for ``list-panes -s`` the ``=`` prefix is *ignored*
(cmd-find.c routes ``-t`` through window resolution despite the man page).
"""

import subprocess
from collections.abc import Generator

import pydantic
import pytest

from imbue.mngr.hosts.tmux import TmuxSessionTarget
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.hosts.tmux import build_tmux_capture_pane_command
from imbue.mngr.utils.testing import get_short_random_string


def test_tmux_session_target_renders_with_exact_match_prefix() -> None:
    target = TmuxSessionTarget(session_name="mngr-my-agent")
    assert target.session_name == "mngr-my-agent"
    assert target.as_shell_arg() == "=mngr-my-agent"


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


# ---------------------------------------------------------------------------
# Prefix-collision end-to-end behavior.
#
# Each test creates two sessions whose names share a prefix, kills the shorter
# one, and then asserts that a command built via the helper does NOT match the
# longer (still-alive) session. These are the exact conditions of the
# stale-WAITING bug. Without the helpers' leading ``=``, every one of these
# would silently misroute.
# ---------------------------------------------------------------------------


@pytest.fixture
def colliding_session_pair() -> Generator[tuple[str, str], None, None]:
    """Create two sessions whose names share a prefix, then kill the shorter.

    Yields ``(stopped_name, alive_name)`` where ``stopped_name`` is a prefix of
    ``alive_name``. The alive session is cleaned up after the test.
    """
    stopped_name = f"mngr-pfx-test-{get_short_random_string()}"
    alive_name = f"{stopped_name}-sibling"
    # Both sessions start out alive so the kill below is well-defined.
    subprocess.run(["tmux", "new-session", "-d", "-s", stopped_name, "sleep", "60"], check=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", alive_name, "sleep", "60"], check=True)
    # Tear down the shorter-named session so a bare-name query for it would
    # fall back to prefix matching and land on alive_name.
    subprocess.run(
        ["tmux", "kill-session", "-t", f"={stopped_name}"],
        check=True,
    )
    try:
        yield (stopped_name, alive_name)
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={alive_name}"],
            check=False,
        )


@pytest.mark.tmux
def test_bare_session_target_silently_matches_sibling(
    colliding_session_pair: tuple[str, str],
) -> None:
    """Sanity-check the bug we're guarding against: bare names *do* misroute.

    This test deliberately asks tmux for a stopped session by its bare name and
    confirms tmux returns data from a sibling session whose name starts with
    that prefix. If this ever stops being true (e.g. tmux changes its default
    matching behavior), the helpers below become unnecessary and this test
    should fail loudly so we know.
    """
    stopped, alive = colliding_session_pair
    # Bare-name query for the dead session -- expected to misroute to ``alive``.
    result = subprocess.run(
        ["tmux", "list-panes", "-t", f"{stopped}:0", "-F", "#S"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == alive, (
        "tmux prefix-matching behavior changed; the bare-name fallback no "
        "longer routes to the sibling session. Re-evaluate whether the "
        "TmuxSessionTarget / TmuxWindowTarget helpers are still needed."
    )


@pytest.mark.tmux
def test_tmux_window_target_blocks_prefix_match_on_list_panes(
    colliding_session_pair: tuple[str, str],
) -> None:
    """``tmux list-panes -t <TmuxWindowTarget(stopped).as_shell_arg()>`` must NOT find the sibling."""
    stopped, _ = colliding_session_pair
    result = subprocess.run(
        ["tmux", "list-panes", "-t", TmuxWindowTarget(session_name=stopped, window=0).as_shell_arg()],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "can't find session" in result.stderr or "no current target" in result.stderr, (
        f"Expected tmux to refuse the exact-match query; got stderr={result.stderr!r}"
    )


@pytest.mark.tmux
def test_tmux_window_target_blocks_prefix_match_on_send_keys(
    colliding_session_pair: tuple[str, str],
) -> None:
    """``tmux send-keys -t <TmuxWindowTarget(stopped).as_shell_arg()>`` must NOT deliver to the sibling."""
    stopped, alive = colliding_session_pair
    # The sibling started with ``sleep 60`` -- if send-keys misroutes, an
    # interrupt or any command would change its observable state. We test the
    # cleanest signal: tmux's own error.
    result = subprocess.run(
        [
            "tmux",
            "send-keys",
            "-t",
            TmuxWindowTarget(session_name=stopped, window=0).as_shell_arg(),
            "echo hi",
            "Enter",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        f"Expected send-keys to fail for a stopped session, but it succeeded "
        f"(likely misrouted to sibling {alive!r}); stderr={result.stderr!r}"
    )


@pytest.mark.tmux
def test_tmux_window_target_blocks_prefix_match_on_capture_pane(
    colliding_session_pair: tuple[str, str],
) -> None:
    """``tmux capture-pane -t <TmuxWindowTarget(stopped).as_shell_arg()>`` must NOT capture the sibling.

    Regression for the original bug: BaseAgent.get_lifecycle_state() captured
    the sibling's pane state, saw a live ``claude`` process there, and reported
    the stopped agent as WAITING.
    """
    stopped, _ = colliding_session_pair
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", TmuxWindowTarget(session_name=stopped, window=0).as_shell_arg(), "-p"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "can't find session" in result.stderr or "no current target" in result.stderr


@pytest.mark.tmux
def test_tmux_session_target_blocks_prefix_match_on_has_session(
    colliding_session_pair: tuple[str, str],
) -> None:
    """``tmux has-session -t <TmuxSessionTarget(stopped).as_shell_arg()>`` must report not-found."""
    stopped, _ = colliding_session_pair
    result = subprocess.run(
        ["tmux", "has-session", "-t", TmuxSessionTarget(session_name=stopped).as_shell_arg()],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        f"Expected has-session to fail for a stopped session; got returncode=0, stderr={result.stderr!r}"
    )


@pytest.mark.tmux
def test_tmux_session_target_blocks_prefix_match_on_kill_session(
    colliding_session_pair: tuple[str, str],
) -> None:
    """``tmux kill-session -t <TmuxSessionTarget(stopped).as_shell_arg()>`` must NOT kill the sibling.

    This is the worst-case misrouting: a cleanup operation aimed at a dead
    agent accidentally tears down a live sibling. The ``=`` prefix prevents
    that.
    """
    stopped, alive = colliding_session_pair
    # Should fail (session is gone) without affecting the sibling.
    result = subprocess.run(
        ["tmux", "kill-session", "-t", TmuxSessionTarget(session_name=stopped).as_shell_arg()],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    # Sibling must still be alive.
    sibling_check = subprocess.run(
        ["tmux", "has-session", "-t", TmuxSessionTarget(session_name=alive).as_shell_arg()],
        capture_output=True,
    )
    assert sibling_check.returncode == 0, "kill-session misrouted and tore down the sibling"

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
# We split this in two:
#
# 1. ``test_bare_session_target_silently_matches_sibling`` requires two real
#    sessions with overlapping names because its assertion is "the bare-name
#    query landed on the OTHER session" -- you can't observe that without an
#    actual sibling for tmux to misroute to. This test is the canary: if tmux
#    ever changes its default prefix-matching behavior, this test fails and we
#    re-evaluate whether the helpers below are still needed.
#
# 2. Every other test below only needs to assert that the helper-built target
#    refuses to resolve when the targeted session is gone -- which is true
#    regardless of whether a colliding sibling exists. A single create-and-kill
#    fixture is sufficient and the cheaper setup matches what the assertion is
#    actually proving.
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


@pytest.fixture
def dead_session_name() -> Generator[str, None, None]:
    """Yield the name of a session that was created and then killed.

    Sufficient for tests asserting "the helper refuses to resolve a gone
    session" -- which doesn't require a sibling to be observable, since the
    assertion is on the returncode/stderr of the helper-built target, not on
    which other session got the command.
    """
    name = f"mngr-dead-test-{get_short_random_string()}"
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep", "60"], check=True)
    subprocess.run(["tmux", "kill-session", "-t", f"={name}"], check=True)
    yield name


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
def test_tmux_window_target_refuses_dead_session_on_list_panes(dead_session_name: str) -> None:
    """``tmux list-panes -t <TmuxWindowTarget(stopped).as_shell_arg()>`` must fail, not resolve."""
    result = subprocess.run(
        ["tmux", "list-panes", "-t", TmuxWindowTarget(session_name=dead_session_name, window=0).as_shell_arg()],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert (
        "can't find session" in result.stderr
        or "no current target" in result.stderr
        or "no server running" in result.stderr
    ), f"Expected tmux to refuse the exact-match query; got stderr={result.stderr!r}"


@pytest.mark.tmux
def test_tmux_window_target_refuses_dead_session_on_send_keys(dead_session_name: str) -> None:
    """``tmux send-keys -t <TmuxWindowTarget(stopped).as_shell_arg()>`` must fail, not deliver."""
    result = subprocess.run(
        [
            "tmux",
            "send-keys",
            "-t",
            TmuxWindowTarget(session_name=dead_session_name, window=0).as_shell_arg(),
            "echo hi",
            "Enter",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, f"Expected send-keys to fail for a dead session; stderr={result.stderr!r}"


@pytest.mark.tmux
def test_tmux_window_target_refuses_dead_session_on_capture_pane(dead_session_name: str) -> None:
    """``tmux capture-pane -t <TmuxWindowTarget(stopped).as_shell_arg()>`` must fail, not capture.

    Regression for the original bug: BaseAgent.get_lifecycle_state() captured
    a sibling pane's state, saw a live ``claude`` process there, and reported
    the stopped agent as WAITING.
    """
    result = subprocess.run(
        [
            "tmux",
            "capture-pane",
            "-t",
            TmuxWindowTarget(session_name=dead_session_name, window=0).as_shell_arg(),
            "-p",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert (
        "can't find session" in result.stderr
        or "no current target" in result.stderr
        or "no server running" in result.stderr
    )


@pytest.mark.tmux
def test_tmux_session_target_refuses_dead_session_on_has_session(dead_session_name: str) -> None:
    """``tmux has-session -t <TmuxSessionTarget(stopped).as_shell_arg()>`` must report not-found."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", TmuxSessionTarget(session_name=dead_session_name).as_shell_arg()],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        f"Expected has-session to fail for a dead session; got returncode=0, stderr={result.stderr!r}"
    )


@pytest.mark.tmux
def test_tmux_session_target_refuses_dead_session_on_kill_session(dead_session_name: str) -> None:
    """``tmux kill-session -t <TmuxSessionTarget(stopped).as_shell_arg()>`` must fail, not kill another."""
    result = subprocess.run(
        ["tmux", "kill-session", "-t", TmuxSessionTarget(session_name=dead_session_name).as_shell_arg()],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0

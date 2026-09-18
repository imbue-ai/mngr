import pytest
from inline_snapshot import snapshot

from imbue.mngr_notifications.terminals import ITermApp
from imbue.mngr_notifications.terminals import KittyApp
from imbue.mngr_notifications.terminals import TerminalApp
from imbue.mngr_notifications.terminals import TerminalDotApp
from imbue.mngr_notifications.terminals import WezTermApp
from imbue.mngr_notifications.terminals import get_terminal_app


def test_iterm_build_connect_command_full_command() -> None:
    """Pin the exact composed shell+AppleScript command so any assembly, quoting,
    or ordering regression in the tab-search/activate/create pipeline is caught."""
    result = ITermApp().build_connect_command("mngr connect my-agent", "my-agent")
    assert result == snapshot(
        "export PATH=$($SHELL -lc 'echo $PATH' 2>/dev/null || echo $PATH); SESSION=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -F my-agent | head -1) && if [ -n \"$SESSION\" ]; then for TTY in $(osascript -e 'tell app \"iTerm2\"' -e 'set r to \"\"' -e 'repeat with w in windows' -e 'repeat with t in tabs of w' -e 'set r to r & (tty of current session of t) & \" \"' -e 'end repeat' -e 'end repeat' -e 'return r' -e 'end tell' 2>/dev/null); do SHORT_TTY=$(echo \"$TTY\" | sed \"s|/dev/||\"); if ps -t \"$SHORT_TTY\" -o command= 2>/dev/null | grep -qF \"tmux attach -t =$SESSION\"; then osascript -e 'on run argv' -e 'set targetTTY to item 1 of argv' -e 'tell app \"iTerm2\"' -e 'repeat with w in windows' -e 'repeat with t in tabs of w' -e 'if tty of current session of t is targetTTY then' -e 'select t' -e 'set index of w to 1' -e 'activate' -e 'return \"found\"' -e 'end if' -e 'end repeat' -e 'end repeat' -e 'return \"notfound\"' -e 'end tell' -e 'end run' -- \"$TTY\" 2>/dev/null; exit 0; fi; done; fi; osascript -e 'tell app \"iTerm2\"' -e 'activate' -e 'if (count of windows) is 0 then' -e 'create window with default profile' -e 'else' -e 'tell current window' -e 'create tab with default profile' -e 'end tell' -e 'end if' -e 'tell current session of current window' -e 'write text \"mngr connect my-agent\"' -e 'end tell' -e 'end tell'"
    )


def test_terminal_dot_app_build_connect_command() -> None:
    result = TerminalDotApp().build_connect_command("mngr connect my-agent", "my-agent")
    assert '"Terminal"' in result
    assert "do script" in result
    assert "mngr connect my-agent" in result


def test_wezterm_build_connect_command() -> None:
    result = WezTermApp().build_connect_command("mngr connect my-agent", "my-agent")
    assert result == "wezterm cli spawn -- mngr connect my-agent"


def test_kitty_build_connect_command() -> None:
    result = KittyApp().build_connect_command("mngr connect my-agent", "my-agent")
    assert result == "kitty @ launch --type=tab -- mngr connect my-agent"


@pytest.mark.parametrize(
    ("name", "expected_class"),
    [
        ("iterm", ITermApp),
        ("iTerm", ITermApp),
        ("ITERM", ITermApp),
        ("iterm2", ITermApp),
        ("terminal", TerminalDotApp),
        ("terminal.app", TerminalDotApp),
        ("Terminal", TerminalDotApp),
        ("wezterm", WezTermApp),
        ("WezTerm", WezTermApp),
        ("kitty", KittyApp),
        ("Kitty", KittyApp),
    ],
)
def test_get_terminal_app_resolves_name_to_correct_class(name: str, expected_class: type[TerminalApp]) -> None:
    """Each supported name (and its case variants/aliases) resolves to the right app class,
    so a swapped or mis-cased mapping in _TERMINAL_APPS is caught."""
    app = get_terminal_app(name)
    assert isinstance(app, expected_class)


def test_get_terminal_app_unsupported_returns_none() -> None:
    assert get_terminal_app("Hyper") is None
    assert get_terminal_app("alacritty") is None

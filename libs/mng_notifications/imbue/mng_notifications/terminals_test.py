from imbue.mng_notifications.terminals import ITermApp
from imbue.mng_notifications.terminals import KittyApp
from imbue.mng_notifications.terminals import TerminalDotApp
from imbue.mng_notifications.terminals import WezTermApp
from imbue.mng_notifications.terminals import get_terminal_app


def test_iterm_searches_tmux_clients() -> None:
    result = ITermApp().build_connect_command("mng connect my-agent", "my-agent")
    assert "tmux list-sessions" in result
    assert "tmux list-clients" in result
    assert "my-agent" in result


def test_iterm_activates_matching_tab_by_tty() -> None:
    result = ITermApp().build_connect_command("mng connect my-agent", "my-agent")
    assert "tty of current session of t is targetTTY" in result
    assert "select t" in result
    assert "activate" in result


def test_iterm_creates_new_tab_if_not_found() -> None:
    result = ITermApp().build_connect_command("mng connect my-agent", "my-agent")
    assert "create tab with default profile" in result
    assert "write text" in result
    assert "mng connect my-agent" in result


def test_terminal_dot_app_build_connect_command() -> None:
    result = TerminalDotApp().build_connect_command("mng connect my-agent", "my-agent")
    assert '"Terminal"' in result
    assert "do script" in result
    assert "mng connect my-agent" in result


def test_wezterm_build_connect_command() -> None:
    result = WezTermApp().build_connect_command("mng connect my-agent", "my-agent")
    assert result == "wezterm cli spawn -- mng connect my-agent"


def test_kitty_build_connect_command() -> None:
    result = KittyApp().build_connect_command("mng connect my-agent", "my-agent")
    assert result == "kitty @ launch --type=tab -- mng connect my-agent"


def test_get_terminal_app_case_insensitive() -> None:
    assert get_terminal_app("iTerm") is not None
    assert get_terminal_app("ITERM") is not None
    assert get_terminal_app("iterm2") is not None


def test_get_terminal_app_all_supported() -> None:
    for name in ("iterm", "iterm2", "terminal", "terminal.app", "wezterm", "kitty"):
        assert get_terminal_app(name) is not None, f"{name} should be supported"


def test_get_terminal_app_unsupported_returns_none() -> None:
    assert get_terminal_app("Hyper") is None
    assert get_terminal_app("alacritty") is None

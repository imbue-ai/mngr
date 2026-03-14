from imbue.mng.hosts.tmux import build_tmux_capture_pane_command


def test_build_tmux_capture_pane_command_visible_only() -> None:
    result = build_tmux_capture_pane_command("mng-my-agent")
    assert result == "tmux capture-pane -t 'mng-my-agent' -p"


def test_build_tmux_capture_pane_command_with_scrollback() -> None:
    result = build_tmux_capture_pane_command("mng-my-agent", include_scrollback=True)
    assert result == "tmux capture-pane -t 'mng-my-agent' -S - -p"

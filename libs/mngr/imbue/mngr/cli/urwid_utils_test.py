from pathlib import Path

from imbue.mngr.cli.urwid_utils import has_interactive_terminal


def test_has_interactive_terminal_when_stdin_is_tty() -> None:
    """Returns True immediately when stdin reports as a tty."""
    assert has_interactive_terminal(stdin_is_tty=True) is True


def test_has_interactive_terminal_falls_back_to_dev_tty(tmp_path: Path) -> None:
    """Returns True when stdin is not a tty but a controlling terminal exists."""
    # Create a real file to stand in for /dev/tty.  os.open() on a regular
    # file succeeds, which is sufficient for the probe logic.
    fake_tty = tmp_path / "fake_tty"
    fake_tty.touch()
    assert has_interactive_terminal(stdin_is_tty=False, tty_path=str(fake_tty)) is True


def test_has_interactive_terminal_no_terminal(tmp_path: Path) -> None:
    """Returns False when stdin is not a tty and no controlling terminal exists."""
    nonexistent = tmp_path / "nonexistent"
    assert has_interactive_terminal(stdin_is_tty=False, tty_path=str(nonexistent)) is False

import shlex
from abc import ABC
from abc import abstractmethod

from imbue.imbue_common.pure import pure


class TerminalApp(ABC):
    """A terminal application that can open a new tab and run a command."""

    @abstractmethod
    def build_connect_command(self, mngr_connect: str, agent_name: str) -> str:
        """Build a shell command that opens a terminal tab running the given command.

        If a tab already connected to this agent exists, implementations may
        activate it instead of creating a new one.
        """


class ITermApp(TerminalApp):
    """iTerm2 on macOS. Finds an existing tab attached to the agent's tmux session, or opens a new one."""

    def build_connect_command(self, mngr_connect: str, agent_name: str) -> str:
        escaped_cmd = _escape_for_applescript(mngr_connect)
        quoted_agent = shlex.quote(agent_name)

        # AppleScript that takes a TTY argument, searches iTerm tabs for it, and activates it.
        activate_script = (
            "osascript"
            " -e 'on run argv'"
            " -e 'set targetTTY to item 1 of argv'"
            " -e 'tell app \"iTerm2\"'"
            " -e 'repeat with w in windows'"
            " -e 'repeat with t in tabs of w'"
            " -e 'if tty of current session of t is targetTTY then'"
            " -e 'select t'"
            " -e 'set index of w to 1'"
            " -e 'activate'"
            " -e 'return \"found\"'"
            " -e 'end if'"
            " -e 'end repeat'"
            " -e 'end repeat'"
            " -e 'return \"notfound\"'"
            " -e 'end tell'"
            " -e 'end run'"
        )

        # AppleScript to get all iTerm tab TTYs (space-separated)
        get_iterm_ttys = (
            "osascript"
            " -e 'tell app \"iTerm2\"'"
            " -e 'set r to \"\"'"
            " -e 'repeat with w in windows'"
            " -e 'repeat with t in tabs of w'"
            " -e 'set r to r & (tty of current session of t) & \" \"'"
            " -e 'end repeat'"
            " -e 'end repeat'"
            " -e 'return r'"
            " -e 'end tell'"
        )

        create_tab_script = (
            "osascript"
            " -e 'tell app \"iTerm2\"'"
            " -e 'activate'"
            " -e 'if (count of windows) is 0 then'"
            " -e 'create window with default profile'"
            " -e 'else'"
            " -e 'tell current window'"
            " -e 'create tab with default profile'"
            " -e 'end tell'"
            " -e 'end if'"
            " -e 'tell current session of current window'"
            f" -e 'write text \"{escaped_cmd}\"'"
            " -e 'end tell'"
            " -e 'end tell'"
        )

        # alerter runs with a bare system PATH, so resolve the user's real PATH first.
        resolve_path = "export PATH=$($SHELL -lc 'echo $PATH' 2>/dev/null || echo $PATH)"

        # Find the tmux session containing this agent name
        find_session = (
            f"SESSION=$(tmux list-sessions -F '#{{session_name}}' 2>/dev/null | grep -F {quoted_agent} | head -1)"
        )

        # Get all iTerm tab TTYs and check which one has tmux attached to our session.
        # This approach works even when tmux is nested inside another tmux session,
        # because we check the process tree on each iTerm tab's TTY rather than
        # matching tmux client TTYs (which may be nested PTYs, not iTerm TTYs).
        find_existing_tab = (
            'if [ -n "$SESSION" ]; then'
            f" for TTY in $({get_iterm_ttys} 2>/dev/null); do"
            ' SHORT_TTY=$(echo "$TTY" | sed "s|/dev/||");'
            ' if ps -t "$SHORT_TTY" -o command= 2>/dev/null | grep -qF "tmux attach -t =$SESSION"; then'
            f' {activate_script} -- "$TTY" 2>/dev/null;'
            " exit 0; fi;"
            " done; fi"
        )

        return f"{resolve_path}; {find_session} && {find_existing_tab}; {create_tab_script}"


class TerminalDotApp(TerminalApp):
    """Terminal.app on macOS. Opens a new window via AppleScript."""

    def build_connect_command(self, mngr_connect: str, agent_name: str) -> str:
        escaped = _escape_for_applescript(mngr_connect)
        return f'osascript -e \'tell app "Terminal" to do script "{escaped}"\''


class WezTermApp(TerminalApp):
    """WezTerm. Spawns a new tab via its CLI."""

    def build_connect_command(self, mngr_connect: str, agent_name: str) -> str:
        return f"wezterm cli spawn -- {mngr_connect}"


class KittyApp(TerminalApp):
    """Kitty. Launches a new tab via its remote control CLI."""

    def build_connect_command(self, mngr_connect: str, agent_name: str) -> str:
        return f"kitty @ launch --type=tab -- {mngr_connect}"


_TERMINAL_APPS: dict[str, TerminalApp] = {
    "iterm": ITermApp(),
    "iterm2": ITermApp(),
    "terminal": TerminalDotApp(),
    "terminal.app": TerminalDotApp(),
    "wezterm": WezTermApp(),
    "kitty": KittyApp(),
}


def get_terminal_app(name: str) -> TerminalApp | None:
    """Look up a terminal app by name (case-insensitive). Returns None if unsupported."""
    return _TERMINAL_APPS.get(name.lower())


@pure
def _escape_for_applescript(s: str) -> str:
    """Escape a string for use inside AppleScript double quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"')

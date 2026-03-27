import shlex
from abc import ABC
from abc import abstractmethod

from imbue.imbue_common.pure import pure


class TerminalApp(ABC):
    """A terminal application that can open a new tab and run a command."""

    @abstractmethod
    def build_connect_command(self, mng_connect: str, agent_name: str) -> str:
        """Build a shell command that opens a terminal tab running the given command.

        If a tab already connected to this agent exists, implementations may
        activate it instead of creating a new one.
        """


class ITermApp(TerminalApp):
    """iTerm2 on macOS. Finds an existing tab attached to the agent's tmux session, or opens a new one."""

    def build_connect_command(self, mng_connect: str, agent_name: str) -> str:
        escaped_cmd = _escape_for_applescript(mng_connect)
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

        # terminal-notifier runs with a bare system PATH (/usr/bin:/bin:/usr/sbin:/sbin),
        # so we resolve the user's real PATH from their login shell first.
        resolve_path = "export PATH=$($SHELL -lc 'echo $PATH' 2>/dev/null || echo $PATH)"

        # Find the tmux session containing this agent name
        find_session = (
            f"SESSION=$(tmux list-sessions -F '#{{session_name}}' 2>/dev/null | grep -F {quoted_agent} | head -1)"
        )

        # Loop through attached tmux clients and try to find a matching iTerm tab by TTY
        find_existing_tab = (
            'if [ -n "$SESSION" ]; then'
            " for CLIENT_TTY in $(tmux list-clients -t \"$SESSION\" -F '#{client_tty}' 2>/dev/null); do"
            f' FOUND=$({activate_script} -- "$CLIENT_TTY" 2>/dev/null);'
            ' if [ "$FOUND" = "found" ]; then exit 0; fi;'
            " done; fi"
        )

        return f"{resolve_path}; {find_session} && {find_existing_tab}; {create_tab_script}"


class TerminalDotApp(TerminalApp):
    """Terminal.app on macOS. Opens a new window via AppleScript."""

    def build_connect_command(self, mng_connect: str, agent_name: str) -> str:
        escaped = _escape_for_applescript(mng_connect)
        return f'osascript -e \'tell app "Terminal" to do script "{escaped}"\''


class WezTermApp(TerminalApp):
    """WezTerm. Spawns a new tab via its CLI."""

    def build_connect_command(self, mng_connect: str, agent_name: str) -> str:
        return f"wezterm cli spawn -- {mng_connect}"


class KittyApp(TerminalApp):
    """Kitty. Launches a new tab via its remote control CLI."""

    def build_connect_command(self, mng_connect: str, agent_name: str) -> str:
        return f"kitty @ launch --type=tab -- {mng_connect}"


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

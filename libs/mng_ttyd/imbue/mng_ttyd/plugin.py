from typing import Any

from imbue.mng import hookimpl

TTYD_WINDOW_NAME = "ttyd"
TTYD_SERVER_NAME = "ttyd"


def build_ttyd_server_command(ttyd_invocation: str, server_name: str) -> str:
    """Build a shell command that runs ttyd with port detection and server registration.

    Takes a ttyd invocation string (e.g. 'ttyd -p 0 bash') and a server name,
    and wraps it with a port-watching loop that:
    1. Pipes ttyd's stderr through a line reader
    2. Detects the assigned port from the "Listening on port:" message
    3. Writes a servers.jsonl record for the changelings forwarding server
    """
    return (
        ttyd_invocation + " 2>&1 | "
        "while IFS= read -r line; do "
        'echo "$line" >&2; '
        'if echo "$line" | grep -q "Listening on port:"; then '
        '_PORT=$(echo "$line" | awk '
        "'{print $NF}'); "
        'if [ -n "$MNG_AGENT_STATE_DIR" ] && [ -n "$_PORT" ]; then '
        'mkdir -p "$MNG_AGENT_STATE_DIR/logs" && '
        'printf \'{"server":"' + server_name + '","url":"http://127.0.0.1:%s"}\\n\' '
        '"$_PORT" >> "$MNG_AGENT_STATE_DIR/logs/servers.jsonl"; '
        "fi; fi; done"
    )


# FIXME: technically, this plugin ought to have some settings to configure whether the ttyd server is writable or not, and it should figure out what shell the user actually uses, rather than hardcoding it here (I think this is done somewhere else already, likely in our tmux config)
# Bash wrapper that starts ttyd on a random port (-p 0), watches its stderr for
# the assigned port number, and writes a servers.jsonl record so the changelings
# forwarding server can discover it. The wrapper stays alive as long as ttyd does.
TTYD_COMMAND = build_ttyd_server_command("ttyd -p 0 -t disableLeaveAlert=true -W bash", TTYD_SERVER_NAME)


@hookimpl
def override_command_options(
    command_name: str,
    command_class: type,
    params: dict[str, Any],
) -> None:
    """Add a ttyd web terminal server as an additional command when creating agents."""
    if command_name != "create":
        return

    existing = params.get("add_command", ())
    params["add_command"] = (*existing, f'{TTYD_WINDOW_NAME}="{TTYD_COMMAND}"')

from typing import Any

from imbue.mng import hookimpl

TTYD_WINDOW_NAME = "terminal"
TTYD_SERVER_NAME = "terminal"


def _build_ttyd_command() -> str:
    """Build the ttyd shell command with URL-arg dispatch and multi-server event registration.

    Starts a single ttyd on a random port with --url-arg (-a) enabled.
    The inline dispatch script routes based on the first URL argument:
    - No arg: exec bash (plain terminal)
    - arg=<KEY>: runs $MNG_AGENT_STATE_DIR/commands/ttyd/<KEY>.sh with remaining args

    The port-detection wrapper watches stderr for the assigned port and writes
    ServerLogRecord events to events/servers/events.jsonl:
    - One "terminal" event with the base URL
    - One event per .sh script found in commands/ttyd/ with ?arg=<KEY> appended
    """
    ttyd_invocation = (
        "ttyd -p 0 -a -t disableLeaveAlert=true -W bash -c '"
        'KEY="${1:-}"; '
        'if [ -z "$KEY" ]; then exec bash; fi; '
        'SCRIPT="$MNG_AGENT_STATE_DIR/commands/ttyd/$KEY.sh"; '
        'if [ -f "$SCRIPT" ]; then shift; exec bash "$SCRIPT" "$@"; fi; '
        'echo "Unknown ttyd key: $KEY" >&2; read -r; exit 1'
        "' --"
    )
    write_event_fn = (
        "_write_evt() { "
        'local _N="$1" _U="$2"; '
        '_TS=$(date -u +"%Y-%m-%dT%H:%M:%S.000000000Z"); '
        '_EID="evt-$(echo "$_N:$_U" | sha256sum | cut -c1-32)"; '
        'printf \'{"timestamp":"%s","type":"server_registered","event_id":"%s","source":"servers",'
        '"server":"%s","url":"%s"}\\n\' '
        '"$_TS" "$_EID" "$_N" "$_U" >> "$MNG_AGENT_STATE_DIR/events/servers/events.jsonl"; '
        "}; "
    )
    return (
        ttyd_invocation + " 2>&1 | "
        "while IFS= read -r line; do "
        'echo "$line" >&2; '
        'if echo "$line" | grep -q "Listening on port:"; then '
        '_PORT=$(echo "$line" | awk '
        "'{print $NF}'); "
        'if [ -n "$MNG_AGENT_STATE_DIR" ] && [ -n "$_PORT" ]; then '
        'mkdir -p "$MNG_AGENT_STATE_DIR/events/servers" && '
        + write_event_fn
        + '_write_evt terminal "http://127.0.0.1:$_PORT"; '
        'for _S in "$MNG_AGENT_STATE_DIR/commands/ttyd/"*.sh; do '
        'if [ -f "$_S" ]; then '
        '_K=$(basename "$_S" .sh); '
        '_write_evt "$_K" "http://127.0.0.1:$_PORT?arg=$_K"; '
        "fi; done; "
        "fi; fi; done"
    )


TTYD_COMMAND = _build_ttyd_command()


@hookimpl
def override_command_options(
    command_name: str,
    command_class: type,
    params: dict[str, Any],
) -> None:
    """Add a ttyd web terminal server as an additional command when creating agents."""
    if command_name != "create":
        return

    existing = params.get("extra_window", ())
    params["extra_window"] = (*existing, f'{TTYD_WINDOW_NAME}="{TTYD_COMMAND}"')

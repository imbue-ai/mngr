import gzip
import importlib.resources
import shlex
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import install_packaged_script_on_host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_ttyd import resources as ttyd_resources

TTYD_WINDOW_NAME = "terminal"
TTYD_SERVICE_NAME = "terminal"
TTYD_VERSION = "1.7.7"

# Filename of the custom web client (a self-contained index.html) installed into
# each agent's commands/ttyd/ dir and served to the stock ttyd binary via -I. The
# stock 1.7.7 client has no OSC 52 handler, so a tmux copy inside the browser
# terminal never reaches the system clipboard; this client does, while keeping
# tmux `mouse on` (so wheel scroll and in-app mouse still work). The client is
# vendored gzip-compressed (TTYD_INDEX_RESOURCE) and decompressed on install. See
# scripts/build_patched_ttyd_client.sh for how the resource is produced.
#
# The vendored bundle is a compiled build of ttyd (https://github.com/tsl0922/ttyd),
# which is MIT-licensed; we redistribute it, so its license (and the licenses of the
# JS libraries bundled into it) ships alongside it at
# resources/ttyd_index.html.gz.LICENSE.
TTYD_INDEX_FILENAME = "index.html"
TTYD_INDEX_RESOURCE = "ttyd_index.html.gz"


def _build_ttyd_command() -> str:
    """Build the ttyd shell command with URL-arg dispatch and multi-service event registration.

    Starts a single ttyd on a random port with --url-arg (-a) enabled.
    The inline dispatch script routes based on the first URL argument:
    - No arg: exec bash (plain terminal)
    - arg=<KEY>: runs $MNGR_AGENT_STATE_DIR/commands/ttyd/<KEY>.sh with remaining args

    The port-detection wrapper watches stderr for the assigned port and writes
    ServiceLogRecord events to events/services/events.jsonl:
    - One "terminal" event with the base URL
    - One event per .sh script found in commands/ttyd/ with ?arg=<KEY> appended

    A custom OSC 52-capable web client is served via -I when present (installed by
    on_after_provisioning before ttyd starts). The flag is added conditionally so a
    missing client file cleanly falls back to ttyd's built-in client rather than
    refusing to start.
    """
    ttyd_invocation = (
        f'_TTYD_INDEX="$MNGR_AGENT_STATE_DIR/commands/ttyd/{TTYD_INDEX_FILENAME}"; '
        "ttyd -p 0 -a -t disableLeaveAlert=true "
        '$([ -f "$_TTYD_INDEX" ] && echo -I "$_TTYD_INDEX") '
        "-W bash -c '"
        'KEY="${1:-}"; '
        'if [ -z "$KEY" ]; then exec bash; fi; '
        'SCRIPT="$MNGR_AGENT_STATE_DIR/commands/ttyd/$KEY.sh"; '
        'if [ -f "$SCRIPT" ]; then shift; exec bash "$SCRIPT" "$@"; fi; '
        'echo "Unknown ttyd key: $KEY" >&2; read -r; exit 1'
        "' --"
    )
    write_event_fn = (
        "_write_evt() { "
        'local _N="$1" _U="$2"; '
        '_TS=$(date -u +"%Y-%m-%dT%H:%M:%S.000000000Z"); '
        '_EID="evt-$(tr -d - < /proc/sys/kernel/random/uuid)"; '
        'printf \'{"timestamp":"%s","type":"service_registered","event_id":"%s","source":"services",'
        '"service":"%s","url":"%s"}\\n\' '
        '"$_TS" "$_EID" "$_N" "$_U" >> "$MNGR_AGENT_STATE_DIR/events/services/events.jsonl"; '
        "}; "
    )
    return (
        ttyd_invocation + " 2>&1 | "
        "while IFS= read -r line; do "
        'echo "$line" >&2; '
        'if echo "$line" | grep -q "Listening on port:"; then '
        '_PORT=$(echo "$line" | awk '
        "'{print $NF}'); "
        'if [ -n "$MNGR_AGENT_STATE_DIR" ] && [ -n "$_PORT" ]; then '
        'mkdir -p "$MNGR_AGENT_STATE_DIR/events/services" && '
        + write_event_fn
        + '_write_evt terminal "http://127.0.0.1:$_PORT"; '
        'for _S in "$MNGR_AGENT_STATE_DIR/commands/ttyd/"*.sh; do '
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


def _build_ttyd_install_command() -> str:
    """Build a shell command that downloads the ttyd binary for the current architecture.

    Uses sudo when not running as root (e.g. Lima VMs) since the install
    target /usr/local/bin/ requires elevated permissions.
    """
    return (
        "ARCH=$(uname -m) && "
        '_SUDO=""; [ "$(id -u)" != "0" ] && _SUDO=sudo && '
        f'curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/{TTYD_VERSION}/ttyd.${{ARCH}}" '
        "-o /tmp/ttyd.$$ && $_SUDO mv /tmp/ttyd.$$ /usr/local/bin/ttyd && "
        "$_SUDO chmod +x /usr/local/bin/ttyd"
    )


TTYD_INSTALL_COMMAND = _build_ttyd_install_command()


def _ensure_ttyd_installed(host: OnlineHostInterface) -> None:
    """Check if ttyd is installed on the host and install it if missing.

    Downloads the ttyd binary from GitHub releases for the host's architecture.
    """
    check_result = host.execute_idempotent_command("command -v ttyd >/dev/null 2>&1", timeout_seconds=10.0)
    if check_result.success:
        logger.debug("ttyd is already installed on the host")
        return

    logger.info("ttyd is not installed on the host, installing...")
    install_result = host.execute_idempotent_command(
        TTYD_INSTALL_COMMAND,
        timeout_seconds=120.0,
    )
    if not install_result.success:
        logger.warning("Failed to install ttyd: {}", install_result.stderr)
    else:
        logger.info("ttyd installed successfully")


@hookimpl
def on_after_provisioning(
    agent: AgentInterface,
    host: OnlineHostInterface,
    mngr_ctx: MngrContext,
) -> None:
    """Provision ttyd on the host and write the agent terminal dispatch script.

    Ensures ttyd is installed on the host, then writes commands/ttyd/agent.sh
    so that the ttyd server can attach to the primary agent's tmux session
    via URL-arg dispatch (?arg=agent), and installs the custom OSC 52-capable
    web client (commands/ttyd/index.html) that the ttyd command serves via -I.

    This runs before the agent's tmux windows (including the ttyd window) start,
    so the client file is present by the time ttyd reads it at startup.
    """
    _ensure_ttyd_installed(host)

    agent_dir = get_agent_state_dir_path(host.host_dir, agent.id)
    ttyd_dir = agent_dir / "commands" / "ttyd"

    host.execute_idempotent_command(f"mkdir -p {shlex.quote(str(ttyd_dir))}", timeout_seconds=10.0)

    script_path = ttyd_dir / "agent.sh"
    logger.debug("Writing ttyd/agent.sh to {}", script_path)
    install_packaged_script_on_host(host, module=ttyd_resources, filename="ttyd_agent.sh", dest=script_path)

    # Install the custom web client served via `ttyd -I` (see TTYD_INDEX_FILENAME).
    index_path = ttyd_dir / TTYD_INDEX_FILENAME
    logger.debug("Writing ttyd/{} to {}", TTYD_INDEX_FILENAME, index_path)
    _install_ttyd_web_client(host, index_path)


def _install_ttyd_web_client(host: OnlineHostInterface, dest: Path) -> None:
    """Decompress the vendored OSC 52-capable ttyd web client and write it onto the host.

    The client is shipped gzip-compressed as a package resource; ttyd serves the
    decompressed file as-is via -I, so it is written uncompressed (mode 0644).
    """
    compressed = importlib.resources.files(ttyd_resources).joinpath(TTYD_INDEX_RESOURCE).read_bytes()
    host.write_file(dest, gzip.decompress(compressed), mode="0644")

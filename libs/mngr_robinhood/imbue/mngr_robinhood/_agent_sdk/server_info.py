"""One-shot ``claude`` probe that captures real server info (commands / output style / tools).

``get_server_info()`` and the real ``system``/``init`` tool list come from claude's control-protocol
initialize response, which the mngr transport (session-JSONL only) never sees. To surface them, this
module runs a single ``claude -p ... --output-format stream-json`` invocation in the session's cwd
and parses the leading ``system``/``init`` event, which carries the negotiated slash commands,
output style, tools, and MCP servers. The result is cached per session, so the probe runs at most
once and only when ``get_server_info()`` is actually called.
"""

import json
import subprocess
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure

# Generous hard timeout for the probe subprocess; the init event is emitted near-immediately but
# the invocation runs a trivial turn to completion.
_PROBE_TIMEOUT_SECONDS: Final[float] = 120.0
_PROBE_PROMPT: Final[str] = "Reply with hi."

_SYSTEM_EVENT_TYPE: Final[str] = "system"
_INIT_SUBTYPE: Final[str] = "init"


@pure
def find_init_event(stream_json_stdout: str) -> dict[str, Any] | None:
    """Return the first ``system``/``init`` event object in a ``--output-format stream-json`` stream."""
    for line in stream_json_stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(parsed, dict)
            and parsed.get("type") == _SYSTEM_EVENT_TYPE
            and parsed.get("subtype") == _INIT_SUBTYPE
        ):
            return parsed
    return None


@pure
def build_server_info(init_event: dict[str, Any] | None) -> dict[str, Any]:
    """Shape a ``system``/``init`` event into the documented ``get_server_info()`` dict.

    The real SDK surfaces ``commands`` and ``output_style``; the stream-json init event names the
    command list ``slash_commands``. Falls back to empty / default values if the probe yielded no
    init event so the documented keys are always present.
    """
    if init_event is None:
        return {"commands": [], "output_style": "default", "tools": [], "mcp_servers": []}
    commands = init_event.get("slash_commands")
    if not isinstance(commands, list):
        commands = init_event.get("commands") if isinstance(init_event.get("commands"), list) else []
    output_style = init_event.get("output_style")
    return {
        "commands": commands,
        "output_style": output_style if isinstance(output_style, str) else "default",
        "tools": init_event.get("tools", []),
        "mcp_servers": init_event.get("mcp_servers", []),
    }


def probe_server_info(cwd: Path, model: str | None) -> dict[str, Any]:
    """Run a one-shot ``claude`` probe in ``cwd`` and return its parsed server info.

    Returns documented-shape defaults (empty commands, ``default`` output style) if the probe fails,
    so ``get_server_info()`` always returns a usable dict rather than raising.
    """
    args = ["claude", "-p", _PROBE_PROMPT, "--output-format", "stream-json", "--verbose"]
    if model:
        args.extend(["--model", model])
    try:
        result = subprocess.run(
            args, cwd=str(cwd), capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("get_server_info probe failed to run claude: {}", exc)
        return build_server_info(None)
    init_event = find_init_event(result.stdout)
    if init_event is None:
        logger.warning("get_server_info probe produced no system/init event (exit {})", result.returncode)
    return build_server_info(init_event)

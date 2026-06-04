import json
import sys
from collections.abc import Mapping
from typing import Any
from typing import assert_never

from imbue.minds.primitives import OutputFormat


def write_stdout_line(message: str) -> None:
    """Write a line to stdout. Use for command output, not logging."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def emit_event(
    # The type of event (e.g., "login_url", "server_ready")
    event_type: str,
    data: Mapping[str, Any],
    output_format: OutputFormat,
) -> None:
    """Emit a structured event to stdout in the appropriate format."""
    match output_format:
        case OutputFormat.HUMAN:
            # Some events are machine-only coordination signals (e.g.
            # ``mngr_forward_started``) with no human-readable form, so they
            # carry no ``message`` key and are intentionally suppressed in
            # HUMAN mode. JSONL consumers still receive the full event below.
            if "message" in data:
                write_stdout_line(str(data["message"]))
        case OutputFormat.JSONL:
            event = {**data, "event": event_type}
            sys.stdout.write(json.dumps(event) + "\n")
            sys.stdout.flush()
        case OutputFormat.JSON:
            # ``emit_event`` is for streaming, per-event output. JSON denotes a
            # single self-contained document, into which a stream of events has
            # no place, so emitting events is a deliberate no-op here. Commands
            # that produce a JSON document build it separately (see
            # ``cli/env.py``); structured consumers of streaming commands such
            # as ``minds run`` should select ``jsonl`` rather than ``json``.
            pass
        case _ as unreachable:
            assert_never(unreachable)

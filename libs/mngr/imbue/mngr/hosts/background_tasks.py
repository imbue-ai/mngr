"""The readers of an agent's background task records and its ``turn_end`` record."""

from __future__ import annotations

import shlex
import unicodedata
from datetime import datetime
from typing import Final
from urllib.parse import unquote

from loguru import logger

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.hosts.common import determine_lifecycle_probe_result
from imbue.mngr.hosts.common import timestamp_to_datetime
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import BACKGROUND_TASK_NAME_PATTERN
from imbue.mngr.primitives import BackgroundTask
from imbue.mngr.primitives import BackgroundTaskKind
from imbue.mngr.primitives import LifecycleProbeReading
from imbue.mngr.primitives import LifecycleProbeResult
from imbue.mngr.primitives import TurnEnd
from imbue.mngr.primitives import TurnEndKind

# Directory under an agent's state dir holding one record file per background task.
BACKGROUND_TASK_DIR_NAME: Final[str] = "background_tasks"

# The token keys of a record's line, e.g. `seen:1792575895 kind:SUBAGENT native_type:cloud%20session`.
BACKGROUND_TASK_SEEN_KEY: Final[str] = "seen"
BACKGROUND_TASK_KIND_KEY: Final[str] = "kind"
BACKGROUND_TASK_NATIVE_TYPE_KEY: Final[str] = "native_type"
BACKGROUND_TASK_SINCE_KEY: Final[str] = "since"

# The file under an agent's state dir holding how its last turn ended, e.g. `ended:1792575895 kind:FAILED`.
TURN_END_FILE_NAME: Final[str] = "turn_end"
TURN_END_ENDED_KEY: Final[str] = "ended"
_TURN_END_KIND_VALUES: Final[frozenset[str]] = frozenset(kind.value for kind in TurnEndKind)

# The longest native type a record publishes, in characters.
BACKGROUND_TASK_NATIVE_TYPE_MAX_LENGTH: Final[int] = 200
_UNPRINTABLE_REPLACEMENT: Final[str] = "\ufffd"
_LINE_SEPARATORS: Final[frozenset[str]] = frozenset({"\u2028", "\u2029"})
# An epoch second with more digits than this is past what a datetime can hold.
_EPOCH_SECONDS_MAX_DIGITS: Final[int] = 12

# How long after a record was last seen the heartbeat counts it, and the environment variable that overrides it.
BACKGROUND_TASK_KEEPALIVE_ENV_VAR: Final[str] = "HOOK_BACKGROUND_TASK_KEEPALIVE_SECONDS"
DEFAULT_BACKGROUND_TASK_KEEPALIVE_SECONDS: Final[int] = 86400

# Marks each part of a work-state read, so one command collects every part.
_ACTIVE_PREFIX: Final[str] = "ACTIVE="
TURN_END_PREFIX: Final[str] = "TURN_END="
BACKGROUND_TASK_PREFIX: Final[str] = "BACKGROUND_TASK="


@pure
def _build_background_task_scan_program(record_key_expression: str) -> str:
    """Build the awk printing `BACKGROUND_TASK=<key>\\t<first line>` per path on stdin, the key built from ``parts[1..n]``."""
    program = (
        '{ first_line = ""; if ((getline first_line < $0) >= 0) '
        f'{{ n = split($0, parts, "/"); print "{BACKGROUND_TASK_PREFIX}" {record_key_expression} "\\t" first_line }} '
        "close($0) }"
    )
    return f"awk {shlex.quote(program)}"


# Names a record from its own path, for a scan over one agent's record directory.
_ONE_AGENT_RECORD_KEY_EXPRESSION: Final[str] = "parts[n]"


@pure
def build_background_task_scan(record_glob: str, record_key_expression: str) -> str:
    """Build the scan that hands awk each regular file ``record_glob`` matches."""
    return (
        f"for mngr_task_record in {record_glob}; do "
        f"""if [ -f "$mngr_task_record" ]; then printf '%s\\n' "$mngr_task_record"; fi; done """
        f"| {_build_background_task_scan_program(record_key_expression)}"
    )


@pure
def build_turn_end_read(turn_end_path: str) -> str:
    """Build the shell builtins printing `TURN_END=<first line>` of the file at the shell word ``turn_end_path``."""
    return (
        f"if [ -f {turn_end_path} ]; then mngr_turn_end=; IFS= read -r mngr_turn_end 2>/dev/null < {turn_end_path}; "
        f"""printf '{TURN_END_PREFIX}%s\\n' "$mngr_turn_end"; fi"""
    )


@pure
def build_lifecycle_probe_read_command(agent_dir: str) -> str:
    """Build the command printing one agent's turn marker, as `ACTIVE=true` or `ACTIVE=`, records and turn end."""
    quoted_agent_dir = shlex.quote(agent_dir)
    quoted_record_dir = shlex.quote(f"{agent_dir}/{BACKGROUND_TASK_DIR_NAME}")
    record_scan = build_background_task_scan(f"{quoted_record_dir}/*", _ONE_AGENT_RECORD_KEY_EXPRESSION)
    return (
        f'if [ -f {quoted_agent_dir}/active ]; then echo "{_ACTIVE_PREFIX}true"; '
        f'else echo "{_ACTIVE_PREFIX}"; fi; '
        f"if [ -d {quoted_record_dir} ]; then {record_scan} 2>/dev/null; fi; "
        f"{build_turn_end_read(shlex.quote(f'{agent_dir}/{TURN_END_FILE_NAME}'))}; :"
    )


@pure
def parse_lifecycle_probe_reading(stdout: str) -> LifecycleProbeReading:
    """Read probe output into the turn marker, absent as `ACTIVE=` or `ACTIVE=false`, raw records and turn end."""
    is_active = False
    record_lines: list[str] = []
    raw_turn_end = ""
    for line in stdout.split("\n"):
        if line.startswith(_ACTIVE_PREFIX):
            marker = line[len(_ACTIVE_PREFIX) :].strip()
            is_active = marker != "" and marker != "false"
        elif line.startswith(BACKGROUND_TASK_PREFIX):
            record_lines.append(line[len(BACKGROUND_TASK_PREFIX) :])
        elif line.startswith(TURN_END_PREFIX):
            raw_turn_end = line[len(TURN_END_PREFIX) :]
        else:
            pass
    return LifecycleProbeReading(
        is_active=is_active, raw_background_tasks="\n".join(record_lines), raw_turn_end=raw_turn_end
    )


@pure
def _parse_record_tokens(first_line: str) -> dict[str, str]:
    """The space-separated `<key>:<value>` tokens of a record line, keeping the first value given for a key."""
    tokens: dict[str, str] = {}
    for token in first_line.rstrip("\r").split(" "):
        key, separator, value = token.partition(":")
        if separator and key and key not in tokens:
            tokens[key] = value
    return tokens


@pure
def _parse_record_kind(raw_kind: str) -> BackgroundTaskKind:
    if not raw_kind:
        return BackgroundTaskKind.OTHER
    try:
        return BackgroundTaskKind(raw_kind)
    except ValueError:
        logger.trace("Reading background task kind {!r} as OTHER: this reader does not know it", raw_kind)
        return BackgroundTaskKind.OTHER


@pure
def _parse_record_native_type(raw_native_type: str) -> str:
    """A native type as published: decoded, capped, and with every control character replaced."""
    native_type = unquote(raw_native_type)[:BACKGROUND_TASK_NATIVE_TYPE_MAX_LENGTH]
    return "".join(
        _UNPRINTABLE_REPLACEMENT
        if unicodedata.category(character) == "Cc" or character in _LINE_SEPARATORS
        else character
        for character in native_type
    )


@pure
def _parse_record_instant(raw_epoch_seconds: str) -> datetime | None:
    """The instant an epoch-seconds token names, or None when it is not a number a datetime can hold."""
    is_epoch_seconds = (
        raw_epoch_seconds.isascii()
        and raw_epoch_seconds.isdecimal()
        and len(raw_epoch_seconds) <= _EPOCH_SECONDS_MAX_DIGITS
    )
    return timestamp_to_datetime(int(raw_epoch_seconds)) if is_epoch_seconds else None


@pure
def parse_background_tasks(raw_background_tasks: str) -> tuple[BackgroundTask, ...]:
    """Parse ``<name>\\t<first line>`` lines into background tasks, one per validly named record whatever its fields."""
    background_tasks: list[BackgroundTask] = []
    for line in raw_background_tasks.split("\n"):
        if not line:
            continue
        raw_name, _, first_line = line.partition("\t")
        name = raw_name.strip()
        if BACKGROUND_TASK_NAME_PATTERN.match(name) is None:
            logger.trace("Dropping background task record {!r}: its name is not a record name", line)
            continue
        tokens = _parse_record_tokens(first_line)
        background_tasks.append(
            BackgroundTask(
                name=name,
                kind=_parse_record_kind(tokens.get(BACKGROUND_TASK_KIND_KEY, "")),
                native_type=_parse_record_native_type(tokens.get(BACKGROUND_TASK_NATIVE_TYPE_KEY, "")),
                started_at=_parse_record_instant(tokens.get(BACKGROUND_TASK_SINCE_KEY, "")),
                last_seen_at=_parse_record_instant(tokens.get(BACKGROUND_TASK_SEEN_KEY, "")),
            )
        )
    return tuple(background_tasks)


@pure
def parse_turn_end(raw_turn_end: str) -> TurnEnd | None:
    """Parse a line like `ended:1792575895 kind:FAILED native_type:rate_limit`; None without a valid kind and ended."""
    tokens = _parse_record_tokens(raw_turn_end)
    raw_kind = tokens.get(BACKGROUND_TASK_KIND_KEY, "")
    ended_at = _parse_record_instant(tokens.get(TURN_END_ENDED_KEY, ""))
    if raw_kind not in _TURN_END_KIND_VALUES or ended_at is None:
        if raw_turn_end:
            logger.trace(
                "Reading turn_end line {!r} as no turn end: it lacks a known kind or a valid ended", raw_turn_end
            )
        return None
    return TurnEnd(
        kind=TurnEndKind(raw_kind),
        native_type=_parse_record_native_type(tokens.get(BACKGROUND_TASK_NATIVE_TYPE_KEY, "")),
        ended_at=ended_at,
    )


@pure
def determine_agent_probe_result(
    tmux_info: str | None,
    is_blocked_on_dialog: bool,
    expected_process_name: str,
    # None when the process table could not be read.
    ps_output: str | None,
    reading: LifecycleProbeReading,
    is_agent_type_known: bool = True,
) -> LifecycleProbeResult:
    """One probe's state and turn end: RUNNING_BACKGROUND where a WAITING agent that no dialog holds has a record."""
    probe = determine_lifecycle_probe_result(
        tmux_info=tmux_info,
        is_active=reading.is_active,
        is_blocked_on_dialog=is_blocked_on_dialog,
        expected_process_name=expected_process_name,
        ps_output=ps_output,
        is_agent_type_known=is_agent_type_known,
    )
    if not reading.is_active:
        probe = probe.model_copy_update(to_update(probe.field_ref().turn_end, parse_turn_end(reading.raw_turn_end)))
    # An agent a dialog holds reads WAITING whatever it has recorded.
    if probe.state is not AgentLifecycleState.WAITING or is_blocked_on_dialog:
        return probe
    background_tasks = parse_background_tasks(reading.raw_background_tasks)
    if not background_tasks:
        return probe
    return probe.model_copy_update(
        to_update(probe.field_ref().state, AgentLifecycleState.RUNNING_BACKGROUND),
        to_update(probe.field_ref().background_tasks, background_tasks),
    )

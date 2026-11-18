"""The reader of background task records, one file per task under an agent's ``background_tasks/`` directory."""

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
from imbue.mngr.primitives import LifecycleProbeReading
from imbue.mngr.primitives import LifecycleProbeResult
from imbue.mngr.primitives import WORK_LEASE_NAME_PATTERN
from imbue.mngr.primitives import WorkLease
from imbue.mngr.primitives import WorkLeaseKind

# Directory under an agent's state dir holding one record file per background task.
WORK_LEASE_DIR_NAME: Final[str] = "background_tasks"

# The token keys of a record's line, e.g. `seen:1792575895 kind:SUBAGENT native_type:cloud%20session`.
WORK_LEASE_SEEN_KEY: Final[str] = "seen"
WORK_LEASE_KIND_KEY: Final[str] = "kind"
WORK_LEASE_NATIVE_TYPE_KEY: Final[str] = "native_type"
WORK_LEASE_SINCE_KEY: Final[str] = "since"

# The longest native type a record publishes, in characters.
WORK_LEASE_NATIVE_TYPE_MAX_LENGTH: Final[int] = 200
_UNPRINTABLE_REPLACEMENT: Final[str] = "\ufffd"
_LINE_SEPARATORS: Final[frozenset[str]] = frozenset({"\u2028", "\u2029"})
# An epoch second with more digits than this is past what a datetime can hold.
_EPOCH_SECONDS_MAX_DIGITS: Final[int] = 12

# How long after a record was last seen the heartbeat counts it, and the environment variable that overrides it.
WORK_LEASE_TTL_ENV_VAR: Final[str] = "HOOK_WORK_LEASE_TTL_SECONDS"
DEFAULT_WORK_LEASE_TTL_SECONDS: Final[int] = 86400

# Marks each half of a work-state read, so one command collects the turn marker and the leases.
_ACTIVE_PREFIX: Final[str] = "ACTIVE="
LEASE_PREFIX: Final[str] = "LEASE="


@pure
def _build_work_lease_scan_program(lease_key_expression: str) -> str:
    """Build the awk printing `LEASE=<key>\\t<first line>` per path on stdin, the key built from ``parts[1..n]``."""
    program = (
        '{ first_line = ""; if ((getline first_line < $0) >= 0) '
        f'{{ n = split($0, parts, "/"); print "{LEASE_PREFIX}" {lease_key_expression} "\\t" first_line }} '
        "close($0) }"
    )
    return f"awk {shlex.quote(program)}"


# Names a lease from its own path, for a scan over one agent's lease directory.
_ONE_AGENT_LEASE_KEY_EXPRESSION: Final[str] = "parts[n]"


@pure
def build_work_lease_scan(lease_glob: str, lease_key_expression: str) -> str:
    """Build the scan that hands awk each regular file ``lease_glob`` matches."""
    return (
        f"for mngr_lease in {lease_glob}; do "
        f"""if [ -f "$mngr_lease" ]; then printf '%s\\n' "$mngr_lease"; fi; done """
        f"| {_build_work_lease_scan_program(lease_key_expression)}"
    )


@pure
def build_lifecycle_probe_read_command(agent_dir: str) -> str:
    """Build the command printing one agent's turn marker, as `ACTIVE=true` or `ACTIVE=`, and its record lines."""
    quoted_agent_dir = shlex.quote(agent_dir)
    quoted_lease_dir = shlex.quote(f"{agent_dir}/{WORK_LEASE_DIR_NAME}")
    lease_scan = build_work_lease_scan(f"{quoted_lease_dir}/*", _ONE_AGENT_LEASE_KEY_EXPRESSION)
    return (
        f'if [ -f {quoted_agent_dir}/active ]; then echo "{_ACTIVE_PREFIX}true"; '
        f'else echo "{_ACTIVE_PREFIX}"; fi; '
        f"if [ -d {quoted_lease_dir} ]; then {lease_scan} 2>/dev/null; fi; :"
    )


@pure
def parse_lifecycle_probe_reading(stdout: str) -> LifecycleProbeReading:
    """Read probe output into the turn marker, absent as `ACTIVE=` or `ACTIVE=false`, and the raw record lines."""
    is_active = False
    lease_lines: list[str] = []
    for line in stdout.split("\n"):
        if line.startswith(_ACTIVE_PREFIX):
            marker = line[len(_ACTIVE_PREFIX) :].strip()
            is_active = marker != "" and marker != "false"
        elif line.startswith(LEASE_PREFIX):
            lease_lines.append(line[len(LEASE_PREFIX) :])
        else:
            pass
    return LifecycleProbeReading(is_active=is_active, raw_leases="\n".join(lease_lines))


@pure
def _parse_lease_tokens(first_line: str) -> dict[str, str]:
    """The space-separated `<key>:<value>` tokens of a record line, keeping the first value given for a key."""
    tokens: dict[str, str] = {}
    for token in first_line.rstrip("\r").split(" "):
        key, separator, value = token.partition(":")
        if separator and key and key not in tokens:
            tokens[key] = value
    return tokens


@pure
def _parse_lease_kind(raw_kind: str) -> WorkLeaseKind:
    if not raw_kind:
        return WorkLeaseKind.OTHER
    try:
        return WorkLeaseKind(raw_kind)
    except ValueError:
        logger.trace("Reading background task kind {!r} as OTHER: this reader does not know it", raw_kind)
        return WorkLeaseKind.OTHER


@pure
def _parse_lease_native_type(raw_native_type: str) -> str:
    """A native type as published: decoded, capped, and with every control character replaced."""
    native_type = unquote(raw_native_type)[:WORK_LEASE_NATIVE_TYPE_MAX_LENGTH]
    return "".join(
        _UNPRINTABLE_REPLACEMENT
        if unicodedata.category(character) == "Cc" or character in _LINE_SEPARATORS
        else character
        for character in native_type
    )


@pure
def _parse_lease_instant(raw_epoch_seconds: str) -> datetime | None:
    """The instant an epoch-seconds token names, or None when it is not a number a datetime can hold."""
    is_epoch_seconds = (
        raw_epoch_seconds.isascii()
        and raw_epoch_seconds.isdecimal()
        and len(raw_epoch_seconds) <= _EPOCH_SECONDS_MAX_DIGITS
    )
    return timestamp_to_datetime(int(raw_epoch_seconds)) if is_epoch_seconds else None


@pure
def parse_work_leases(raw_leases: str) -> tuple[WorkLease, ...]:
    """Parse ``<name>\\t<first line>`` lines into background tasks, one per validly named record whatever its fields."""
    leases: list[WorkLease] = []
    for line in raw_leases.split("\n"):
        if not line:
            continue
        raw_name, _, first_line = line.partition("\t")
        name = raw_name.strip()
        if WORK_LEASE_NAME_PATTERN.match(name) is None:
            logger.trace("Dropping background task record {!r}: its name is not a record name", line)
            continue
        tokens = _parse_lease_tokens(first_line)
        leases.append(
            WorkLease(
                name=name,
                kind=_parse_lease_kind(tokens.get(WORK_LEASE_KIND_KEY, "")),
                native_type=_parse_lease_native_type(tokens.get(WORK_LEASE_NATIVE_TYPE_KEY, "")),
                started_at=_parse_lease_instant(tokens.get(WORK_LEASE_SINCE_KEY, "")),
                last_seen_at=_parse_lease_instant(tokens.get(WORK_LEASE_SEEN_KEY, "")),
            )
        )
    return tuple(leases)


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
    """One probe's lifecycle state: RUNNING_BACKGROUND where a WAITING agent that no dialog holds has a record."""
    probe = determine_lifecycle_probe_result(
        tmux_info=tmux_info,
        is_active=reading.is_active,
        is_blocked_on_dialog=is_blocked_on_dialog,
        expected_process_name=expected_process_name,
        ps_output=ps_output,
        is_agent_type_known=is_agent_type_known,
    )
    # An agent a dialog holds reads WAITING whatever it has recorded.
    if probe.state is not AgentLifecycleState.WAITING or is_blocked_on_dialog:
        return probe
    leases = parse_work_leases(reading.raw_leases)
    if not leases:
        return probe
    return probe.model_copy_update(
        to_update(probe.field_ref().state, AgentLifecycleState.RUNNING_BACKGROUND),
        to_update(probe.field_ref().leases, leases),
    )

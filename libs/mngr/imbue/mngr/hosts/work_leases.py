"""The work-lease protocol: how work that outlives a turn declares itself, and how it is read.

A lease is one file under an agent's ``leases/`` directory naming a unit of work that will
wake the agent -- a background command, a subagent, a monitor, a workflow. This module holds
both sides of the format, and the rule that turns a read of them into the agent's
``RUNNING_BACKGROUND`` state. The shell half ships as ``resources/mngr_work_lease_lib.sh`` for the
turn-end hooks that write the leases.

The leases are what lets ``hosts.common``'s lifecycle rule separate an agent whose turn ended
with something still running from one that is genuinely waiting on the user.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from datetime import datetime
from typing import Final

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

# Directory under an agent's state dir holding one file per outstanding unit of work.
WORK_LEASE_DIR_NAME: Final[str] = "leases"

# A lease's first line is whitespace-separated `<key>:<value>` tokens, e.g.
# `until:1792575895`. A reader ignores keys it does not know, so the grammar can grow
# without breaking one that has already shipped.
WORK_LEASE_UNTIL_KEY: Final[str] = "until"

# Marks each half of a work-state read, so one command collects the turn marker and the leases.
_ACTIVE_PREFIX: Final[str] = "ACTIVE="
LEASE_PREFIX: Final[str] = "LEASE="


@pure
def _build_work_lease_scan_program(lease_key_expression: str) -> str:
    """Build the awk that emits one `LEASE=<key>\\t<first line>` per lease path read on stdin.

    ``lease_key_expression`` names the lease from the components of its own path, which awk
    holds in ``parts[1..n]``: ``parts[n]`` is the lease's file name, and a scan carrying more
    than one agent reaches further up for whose lease it is.

    One spawn covers every path, which matters on a sandboxed runtime (gVisor) where each
    spawn costs tens of milliseconds. The paths arrive on stdin rather than as arguments, and
    each is read with `getline`, because awk aborts outright on an argument it cannot open and
    would drop every lease after it -- and a lease can vanish between the shell expanding the
    glob and awk reaching it. ``build_work_lease_scan`` is what feeds it.

    Every path is closed, read or not: awk holds an opened redirection for the life of the
    program, so an empty lease file left open would spend a descriptor, and a scan over a
    whole host's leases that runs out of them drops every lease after that -- which reads as
    the work having ended.
    """
    program = (
        "{ if ((getline bound < $0) > 0) "
        f'{{ n = split($0, parts, "/"); print "{LEASE_PREFIX}" {lease_key_expression} "\\t" bound }} '
        "close($0) }"
    )
    return f"awk {shlex.quote(program)}"


# Names a lease from its own path, for a scan over one agent's lease directory.
_ONE_AGENT_LEASE_KEY_EXPRESSION: Final[str] = "parts[n]"


@pure
def build_work_lease_scan(lease_glob: str, lease_key_expression: str) -> str:
    """Build the scan over ``lease_glob``, which hands awk only paths it can read a line from.

    The glob is filtered down to regular files in the shell, where ``[ -f ]`` and ``printf``
    are builtins and the filter costs no spawn. awk survives a path it cannot *open* -- a
    lease cleared between the glob and the read -- but dies on one it opens and cannot read,
    which a directory under the lease dir is, and takes with it every lease that sorts after
    it, in a host-wide scan every other agent's leases too.
    """
    return (
        f"for mngr_lease in {lease_glob}; do "
        f"""if [ -f "$mngr_lease" ]; then printf '%s\\n' "$mngr_lease"; fi; done """
        f"| {_build_work_lease_scan_program(lease_key_expression)}"
    )


@pure
def build_lifecycle_probe_read_command(agent_dir: str) -> str:
    """Build the command reading one agent's turn marker and work leases in a single call.

    `ACTIVE=` carries `true` where the marker is present and nothing where it is not, which
    is what having a turn open means. Ends in `:` so a lease read that exits non-zero still
    reports the marker.
    """
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
    """Read one agent's work-state output into the turn marker and the raw lease lines.

    Only the marker's presence is read, and both spellings of its absence are honoured: the
    empty value this command emits and the `false` the batched listing uses.
    """
    is_active = False
    lease_lines: list[str] = []
    for line in stdout.splitlines():
        if line.startswith(_ACTIVE_PREFIX):
            marker = line[len(_ACTIVE_PREFIX) :].strip()
            is_active = marker != "" and marker != "false"
        elif line.startswith(LEASE_PREFIX):
            lease_lines.append(line[len(LEASE_PREFIX) :])
        else:
            pass
    return LifecycleProbeReading(is_active=is_active, raw_leases="\n".join(lease_lines))


@pure
def _parse_lease_tokens(bound: str) -> dict[str, str]:
    """The `<key>:<value>` tokens of a lease line, keeping the first value given for a key."""
    tokens: dict[str, str] = {}
    for token in bound.split():
        key, separator, value = token.partition(":")
        if separator and key and key not in tokens:
            tokens[key] = value
    return tokens


@pure
def parse_work_leases(raw_leases: str) -> tuple[WorkLease, ...]:
    """Parse ``<name>\\t<bound>`` lines into leases, dropping any line without a usable bound.

    An unbounded, badly named or otherwise malformed lease is dropped rather than trusted, so
    a bad writer can never pin an agent to RUNNING_BACKGROUND. A bound is tested with ``isdecimal``
    rather than ``isdigit`` because the latter also admits characters ``int`` refuses, e.g. a
    superscript.
    """
    leases: list[WorkLease] = []
    for line in raw_leases.splitlines():
        raw_name, separator, raw_bound = line.partition("\t")
        name = raw_name.strip()
        is_named = bool(separator) and bool(name) and WORK_LEASE_NAME_PATTERN.match(name) is not None
        tokens = _parse_lease_tokens(raw_bound)
        raw_until = tokens.get(WORK_LEASE_UNTIL_KEY, "")
        expires_at = timestamp_to_datetime(int(raw_until)) if raw_until.isdecimal() else None
        if not is_named or expires_at is None:
            logger.trace("Dropping work lease {}: it is not both named and bounded", line)
            continue
        leases.append(WorkLease(name=name, expires_at=expires_at))
    return tuple(leases)


@pure
def select_live_work_leases(leases: Sequence[WorkLease], now: datetime) -> tuple[WorkLease, ...]:
    """The leases that still count: those whose deadline has not passed."""
    return tuple(lease for lease in leases if lease.expires_at is not None and lease.expires_at > now)


@pure
def resolve_live_work_leases(raw_leases: str, now: datetime) -> tuple[WorkLease, ...]:
    """An agent's work leases that still hold, from one collected read."""
    return select_live_work_leases(parse_work_leases(raw_leases), now)


@pure
def determine_agent_probe_result(
    tmux_info: str | None,
    is_blocked_on_dialog: bool,
    expected_process_name: str,
    # None when the process table could not be read.
    ps_output: str | None,
    reading: LifecycleProbeReading,
    now: datetime,
    is_agent_type_known: bool = True,
) -> LifecycleProbeResult:
    """One probe's lifecycle state, including whether declared work outlives the agent's turn.

    The single entry point for every caller that probes an agent, so no caller assembles the
    state itself. ``determine_lifecycle_probe_result`` settles it from the turn marker and the
    process table; the leases only come into it once that says the turn has ended, where they
    separate an agent something will wake from one waiting on the user. A lease is read for a
    WAITING agent no dialog holds, and nowhere else: an agent with a turn open is already
    RUNNING, a stopped or replaced one is carrying nothing whatever its directory still holds,
    and one held on a dialog is the user's to move whatever it left running. So ``leases`` is
    non-empty exactly where the state is RUNNING_BACKGROUND, which is what it is published as.
    """
    probe = determine_lifecycle_probe_result(
        tmux_info=tmux_info,
        is_active=reading.is_active,
        is_blocked_on_dialog=is_blocked_on_dialog,
        expected_process_name=expected_process_name,
        ps_output=ps_output,
        is_agent_type_known=is_agent_type_known,
    )
    # A dialog is the user's turn to move, so it outranks work the agent left running.
    if probe.state is not AgentLifecycleState.WAITING or is_blocked_on_dialog:
        return probe
    live_leases = resolve_live_work_leases(reading.raw_leases, now)
    if not live_leases:
        return probe
    return probe.model_copy_update(
        to_update(probe.field_ref().state, AgentLifecycleState.RUNNING_BACKGROUND),
        to_update(probe.field_ref().leases, live_leases),
    )

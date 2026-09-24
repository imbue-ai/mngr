#!/usr/bin/env python3
"""Common-transcript converter for codex agents (invoked by common_transcript.sh).

Reads the raw codex rollout stream (``logs/codex_transcript/events.jsonl``,
produced verbatim by stream_transcript.sh) and appends the semantically
important rollout items to ``events/codex/common_transcript/events.jsonl`` as
ATIF-shaped stream records (``header`` / ``step`` / ``observation``; see
``specs/atif-transcript-alignment/spec.md`` and the canonical schema in
``imbue/mngr/agents/common_transcript_records.py``).

codex rollout wire shape (verified live against codex 0.64.0 and the patched
codex 0.146.0 build):
  {"timestamp":"<ISO8601>","type":<t>,"payload":<p>}
Each rollout item under type "response_item" maps to exactly one record:
  payload.type=="message", role=="user"      -> user step (or a system step, for
                                    the instruction injections described below)
  payload.type=="message", role=="assistant" -> agent step (message only;
                                    dropped when it carries no text)
  payload.type=="reasoning"                  -> agent step carrying
                                    reasoning_content (dropped when the item
                                    exposes no extractable text)
  payload.type=="function_call"              -> agent step with one tool_call
                                    (arguments parsed from payload.arguments);
                                    the tool name is remembered by payload.call_id
  payload.type=="function_call_output"       -> observation record, keyed by call_id
  payload.type=="custom_tool_call"           -> agent step with one tool_call; the
                                    0.146 unified exec tool emits these
                                    (payload.input, not .arguments)
  payload.type=="custom_tool_call_output"    -> observation record, keyed by call_id

User-role messages that are instruction injections rather than genuine user
turns -- the AGENTS.md context blob ("# AGENTS.md instructions for <dir>" with
an "<INSTRUCTIONS>" envelope) and codex's own "<user_instructions>" /
"<environment_context>" initial-context items -- become *system* steps carrying
their full text, so the session-configured instructions survive without being
mistaken for user turns.

Two rollout types outside "response_item" are read, because they are the only
place codex reports what an inference cost and which model ran it. Neither
produces a record of its own; both only decorate the agent steps around them.
  type "turn_context"  -> the turn's model / reasoning effort (payload.model,
                          payload.effort), stamped on every agent step of that
                          turn as ATIF model_name / reasoning_effort. codex
                          writes one per user turn and again after a mid-turn
                          compaction, so the most recent one before a step is
                          the context that step ran under. Its turn id
                          (payload.turn_id) is also the turn each call after
                          it was made in, for command attribution (below).
  type "event_msg" with payload.type == "token_count"
                       -> the closing usage of one model response
                          (payload.info.last_token_usage), stamped as ATIF
                          metrics. Every other event_msg but a CommandExecution's
                          item_completed (below) stays ignored: the rest are
                          display duplicates of response_items.

Pairing rule for token_count. codex emits exactly one token_count per model
response, after the response's items and the outputs of every tool call it made
are persisted, so a token_count measures the *last agent step emitted since the
turn began* -- the observation records in between do not count, and a
user/system step or a new turn_context ends the window. One inference can produce
several agent steps (reasoning, message, tool call are separate rollout items); the
metrics land on the last of them alone, so
summing the steps' metrics reproduces codex's own session total instead of
multiplying it. Two consequences follow, both deliberate:
  - a token_count with no unmeasured agent step in its window (the first one
    after a compaction, or a second one for an inference already measured) is
    dropped: there is nothing it can describe.
  - an agent step that no token_count ever closes (an aborted or interrupted
    response) is emitted with no ``metrics`` at all -- never with fabricated
    zeros, which a consumer would price as a real free inference.

codex also writes a token_count again, unchanged, partway through the response after
the one it measured. Its running total (payload.info.total_token_usage) equals the
previous token_count's, and no response finishes without adding input tokens to that
total, so the repeat reports nothing new: it is ignored outright, neither measuring a
step nor closing the window.

Because the output is append-only and deduped by event_id, a step emitted
before its token_count arrives would stay unmeasured forever. A pass over an
input that may still grow therefore holds back the trailing unmeasured agent
step, and everything after it (append order is authoritative), until a later
pass sees the token_count. Only a pass told the input is complete emits such a
step, which is what lets a genuinely unmeasured one -- an aborted response --
through; common_transcript.sh says so once the rollout has stopped growing (and
on its ``--single-pass`` path). Even then, a step whose window still has a tool
call awaiting its output stays held: a long-running tool leaves the rollout
silent for as long as it runs, and its token_count is still coming. An
interrupted call does not strand its step, because codex writes an "aborted"
output for it, and a new user turn ends the window either way. A rollout counts
as reporting usage from its first turn_context, which codex writes before the
turn's first inference, so that inference is not emitted before the session's
first token_count. A rollout carrying neither holds nothing back: there is
nothing to wait for. A build that wrote turn_context but no token_count would
only see its trailing step delayed until the completing pass, never lost.

A rollout written in codex's paginated history mode (codex 0.154 writes them that
way by default) also carries, for every shell command a tool call started, an
"event_msg" with payload.type == "item_completed" whose payload.item.type is
"CommandExecution", written once the command has finished. Inside one code-mode
``exec`` program there can be several. It records what actually ran -- the command
line, its exit code and status, its output -- including a command the program
built at run time and one that exited nonzero inside a program that did not fail,
none of which the call's own observation shows: that carries only what the program
chose to print. Each becomes one observation record whose single result has
``content`` "" and ``extra`` {"tool_name": "command_execution",
"command_execution": {cmd, argv, cwd, status, exit_code, duration_ms,
started_at_ms, completed_at_ms, output, attribution}}. The empty ``content`` keeps
a reader that scans a call's result text from reading the same output twice.

codex records no link from a command to the call that started it, so the result's
``source_call_id`` is read from time. A command belongs to a call whose program was
running when the command started: from the call's line to its output's line, or,
for a code-mode program that yielded ("Script running with cell ID <n>"), until the
``wait`` that collects that cell returns something other than another yield. When
several programs were running, the one whose own text names the command owns it.
Otherwise, among the programs that name it -- or all of them, when none does -- the
latest-started one whose call was made in the command's own turn owns it, and
failing that the latest-started one. A command that started while no program ran
goes to the nearest call before it in its own turn, and failing that to the nearest
call before it. The turn -- the command's ``item_completed`` turn_id against the
turn_context in force when each call was made -- only settles a guess the timing
rules leave open and never overrides them; it matters when programs from two turns
overlap, as a yielded program that no ``wait`` collected stays open into later
turns. ``attribution`` records which rule decided (running_program /
named_in_program / latest_running_program_in_turn / latest_running_program /
nearest_preceding_call_in_turn / nearest_preceding_call). A command record is
appended after its call's output record, never before, so the call's own result
stays the first one attached to it; one whose call has not answered yet is left for
a later pass. Rollouts in the legacy history mode (codex 0.147 and older) carry no
such items and convert as before.

codex models a tool invocation as its own rollout item, separate from the
assistant's text (a distinct ``message`` item), so a call is emitted as its own
agent step with an empty ``message`` -- matching ATIF's one-step-per-inference
convention. The tool_call_id is codex's own ``call_id``, so the doc-builder pairs
each observation result back to its step by that native id.

Nothing is truncated: ``arguments`` is the complete parsed object (an invocation
payload that is not a JSON object rides whole under ``_raw``) and observation
``content`` is the full stringified output. The one clipped value is the copy of a
command's output under ``extra.command_execution`` (see _COMMAND_OUTPUT_CHAR_LIMIT).

Event ids are synthesized by hashing the line's own timestamp and content (plus
the item kind), so re-processing the same input never produces duplicates and ids
never repeat across agents or hosts; the converter also dedupes against the set of
event_ids already in the output file (the ``header`` line included).

Invoked as ``python3 common_transcript_convert.py`` with the input/output paths
passed via the ``_INPUT_FILE`` / ``_OUTPUT_FILE`` environment variables (and the
input-is-complete flag via ``_MNGR_EMIT_TRAILING_AGENT_STEP``) that
common_transcript.sh sets. Malformed or null lines are dropped silently; only an
uncaught exception writes to stderr, which the shell reports as a convert error
(the count of appended records is printed to stdout for common_transcript.sh to
capture). Split out of
the shell script (it used to be an inline ``python3`` heredoc) so the logic is
lintable, type-checked, and unit-testable directly rather than only through a
subprocess.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import time
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Union
from urllib.parse import unquote

# A parsed-JSON value of unspecified shape. Stdlib-only (pydantic isn't importable
# under the host's bare python3). Spelled with Union, not ``|``: this assignment runs
# at import, and ``|`` on types needs python 3.10+. noqa stops ruff rewriting it.
JsonValue = Union[str, int, float, bool, None, list, dict]  # noqa: UP007

_EMITTER = "codex/common_transcript"
# The ATIF revision these records follow; must match PINNED_ATIF_SCHEMA_VERSION in
# imbue/mngr/agents/common_transcript_records.py.
_SCHEMA_VERSION = "ATIF-v1.7"

# The tool name recorded on an observation whose call was never seen (a rollout
# tailed from mid-turn). The result is still emitted -- the doc-builder warns on an
# unmatched source_call_id rather than the output being lost here.
_UNKNOWN_TOOL_NAME = "unknown"

# codex wraps its own initial-context items in these envelopes and carries them
# as user-role messages; they are session-configured instructions, not user turns.
_CONTEXT_INJECTION_PREFIXES = ("<user_instructions>", "<environment_context>")
# The AGENTS.md context injection (seen live on codex 0.146.0): a user-role
# message opening with this header, with the file body in an <INSTRUCTIONS> envelope.
_AGENTS_MD_INJECTION_PREFIX = "# AGENTS.md instructions for "
_AGENTS_MD_INJECTION_ENVELOPE = "<INSTRUCTIONS>"

# Fields of a ``reasoning`` rollout item that can carry plain text: ``summary[]``
# holds ``summary_text`` items, and ``content[]`` holds ``reasoning_text`` items on
# builds that expose it. The always-present ``encrypted_content`` is opaque.
_REASONING_TEXT_FIELDS = ("summary", "content")

# Set to "1" by common_transcript.sh for a pass whose input has stopped growing.
# See the "held back" paragraph of the module docstring for why a pass over an
# input that just grew must not say so.
_TRAILING_STEP_ENV_VAR = "_MNGR_EMIT_TRAILING_AGENT_STEP"

# The token buckets of codex's ``TokenUsage``. ``input_tokens`` is inclusive of the
# cached and cache-written subsets (codex derives its own display value as
# ``input_tokens - cached_input_tokens``) and ``output_tokens`` is inclusive of
# ``reasoning_output_tokens``, so the buckets are carried across to ATIF, never
# summed -- summing would double count.
_USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

# The tools a shell command can be started from: codex's code-mode ``exec`` program, and its
# shell tools when code mode is off. A CommandExecution is only ever attributed to one of these.
_COMMAND_STARTING_TOOL_NAMES = frozenset({"exec", "exec_command", "shell", "shell_command", "local_shell"})

# The code-mode tool that collects a yielded program's cell.
_WAIT_TOOL_NAME = "wait"

# What a code-mode program that yielded answers instead of finishing. The program keeps running
# in the named cell, and the rest of its output arrives on the ``wait`` that collects that cell.
_YIELDED_CELL_PREFIX = "Script running with cell ID "

# The tool name on a command record's result, so a reader can tell it from the call's own result.
_COMMAND_EXECUTION_TOOL_NAME = "command_execution"

# The most characters of a command's output copied onto its record. The call's own observation
# already carries what the model was shown; this copy serves readers that want one command's
# output by itself, and a command that printed megabytes must not multiply the stream's size.
_COMMAND_OUTPUT_CHAR_LIMIT = 16_000

# The flags codex passes a shell when it runs a command string (``["/bin/zsh", "-lc", <script>]``);
# for that argv the script is the command a reader wants.
_SHELL_SCRIPT_FLAGS = frozenset({"-c", "-lc", "-ic", "-lic"})


def _is_injected_instructions(text: str) -> bool:
    """True for instruction-injection messages that are system steps, not user turns."""
    stripped = text.lstrip()
    if stripped.startswith(_AGENTS_MD_INJECTION_PREFIX) and _AGENTS_MD_INJECTION_ENVELOPE in stripped:
        return True
    return stripped.startswith(_CONTEXT_INJECTION_PREFIXES)


def _iso_timestamp(value: JsonValue) -> str:
    """Return the record's ISO 8601 timestamp, falling back to conversion time.

    ATIF step and observation records require a timestamp, but a truncated or
    malformed rollout line can lack one. Conversion time is the closest
    approximation available on the host, and the doc-builder orders by stream
    position rather than by timestamp.
    """
    if isinstance(value, str) and value:
        return value
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _join_content_text(content: JsonValue, item_type: str) -> str:
    """Join the .text of payload.content[] items whose type matches item_type."""
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != item_type:
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _join_reasoning_text(payload: dict[str, Any]) -> str:
    """Extract the plain reasoning text of a ``reasoning`` rollout item, best-effort.

    Blocks are joined with a blank line, per the spec's reasoning_content rule.
    Returns "" when the item exposes nothing beyond its encrypted payload.
    """
    blocks = []
    for field_name in _REASONING_TEXT_FIELDS:
        value = payload.get(field_name)
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                blocks.append(text)
    return "\n\n".join(blocks)


def _parse_arguments(invocation: JsonValue) -> dict[str, Any]:
    """Return the complete ATIF ``arguments`` object for a native invocation payload.

    codex serializes a call's arguments as a JSON string (``function_call.arguments``)
    or as a free-form script string (``custom_tool_call.input``, which is JavaScript,
    not JSON). Whatever does not parse to a JSON object rides whole under ``_raw`` so
    nothing is lost.
    """
    if isinstance(invocation, dict):
        return invocation
    if isinstance(invocation, str):
        # An absent or empty native payload means "no arguments", not a raw empty string.
        if not invocation.strip():
            return {}
        try:
            parsed = json.loads(invocation)
        except json.JSONDecodeError:
            return {"_raw": invocation}
        return parsed if isinstance(parsed, dict) else {"_raw": invocation}
    return {"_raw": json.dumps(invocation, separators=(",", ":"))}


def _stringify_output(output: JsonValue) -> str:
    """Render a tool output payload (.output), which is a string OR a content array."""
    if isinstance(output, str):
        return output
    # An array of content items: join the text of each, falling back to a JSON
    # dump of any item that doesn't carry a plain .text field.
    if isinstance(output, list):
        parts = []
        for item in output:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(json.dumps(item, separators=(",", ":")))
        return "".join(parts)
    # Anything else (a bare object/number): render it as JSON so nothing is lost.
    return json.dumps(output, separators=(",", ":"))


def _token_count_value(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) else 0


def _build_metrics(usage: dict[str, Any]) -> dict[str, Any]:
    """Map one codex ``TokenUsage`` onto ATIF metric names.

    ATIF's ``prompt_tokens`` is *all* input tokens including cached ones, which is
    already what codex's ``input_tokens`` counts, so it is carried straight across;
    the same holds for ``output_tokens`` and ``completion_tokens``, whose reasoning
    share codex reports separately. Cache *writes* have no ATIF field of their own
    and ride under ``extra`` -- under the key the claude emitter uses, so a consumer
    reads one name across both agents.
    """
    metrics: dict[str, Any] = {
        "prompt_tokens": _token_count_value(usage, "input_tokens"),
        "completion_tokens": _token_count_value(usage, "output_tokens"),
        "cached_tokens": _token_count_value(usage, "cached_input_tokens"),
    }
    extra: dict[str, Any] = {}
    if "cache_write_input_tokens" in usage:
        extra["cache_creation_input_tokens"] = _token_count_value(usage, "cache_write_input_tokens")
    if "reasoning_output_tokens" in usage:
        extra["reasoning_output_tokens"] = _token_count_value(usage, "reasoning_output_tokens")
    if extra:
        metrics["extra"] = extra
    return metrics


def _closing_metrics(info: JsonValue) -> dict[str, Any] | None:
    """The ATIF metrics of the one response a ``token_count`` closes, or None.

    ``last_token_usage`` is that response's own usage; the sibling
    ``total_token_usage`` is the session running total, so per-step metrics summed
    over the trajectory reproduce it. A block carrying none of the known buckets
    describes nothing and yields None rather than a confident row of zeros.
    """
    if not isinstance(info, dict):
        return None
    usage = info.get("last_token_usage")
    if not isinstance(usage, dict):
        return None
    if not any(field in usage for field in _USAGE_TOKEN_FIELDS):
        return None
    return _build_metrics(usage)


def _epoch_ms(value: JsonValue) -> int | None:
    """Milliseconds since the epoch for a rollout line's ISO 8601 timestamp, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round(parsed.timestamp() * 1000)


def _json_int(value: JsonValue) -> int | None:
    """An integer JSON value, or None; a JSON boolean is not a number here."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _command_text(argv: JsonValue) -> str:
    """The command a CommandExecution ran, as one shell command line.

    codex runs a command string through a shell (``["/bin/zsh", "-lc", "<script>"]``), and for
    that argv the script is the command; any other argv is joined with shell quoting.
    """
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        return ""
    if len(argv) == 3 and argv[1] in _SHELL_SCRIPT_FLAGS:
        return argv[2]
    return shlex.join(argv)


def _duration_ms(duration: JsonValue) -> int | None:
    """A CommandExecution's ``{"secs", "nanos"}`` duration in milliseconds, or None."""
    if not isinstance(duration, dict):
        return None
    seconds = _json_int(duration.get("secs"))
    nanos = _json_int(duration.get("nanos"))
    if seconds is None or nanos is None:
        return None
    return seconds * 1000 + nanos // 1_000_000


def _local_path(cwd: JsonValue) -> str:
    """A CommandExecution's working directory as a path; codex records it as a ``file://`` URI."""
    if not isinstance(cwd, str):
        return ""
    if cwd.startswith("file://"):
        return unquote(cwd[len("file://") :])
    return cwd


def _yielded_cell_id(output_text: str) -> str | None:
    """The cell a code-mode program keeps running in, when its output says it yielded."""
    if not output_text.startswith(_YIELDED_CELL_PREFIX):
        return None
    words = output_text[len(_YIELDED_CELL_PREFIX) :].split(None, 1)
    return words[0] if words else None


def _close_program_window(
    call_id: str,
    output_text: str,
    output_ms: int | None,
    program_window_by_call_id: dict[str, dict[str, Any]],
    call_id_by_live_cell_id: dict[str, str],
    cell_id_by_wait_call_id: dict[str, str],
) -> None:
    """Record what a call's output says about when its program stopped running.

    A code-mode program that yielded has not stopped: its window stays open, under the cell it
    runs in, until a ``wait`` on that cell answers with anything but another yield.
    """
    live_cell_id = _yielded_cell_id(output_text)
    window = program_window_by_call_id.get(call_id)
    if window is not None:
        if live_cell_id is None:
            window["finished_ms"] = output_ms
        else:
            call_id_by_live_cell_id[live_cell_id] = call_id
    collected_cell_id = cell_id_by_wait_call_id.get(call_id)
    if collected_cell_id is not None and live_cell_id is None:
        owner_call_id = call_id_by_live_cell_id.pop(collected_cell_id, None)
        if owner_call_id is not None:
            program_window_by_call_id[owner_call_id]["finished_ms"] = output_ms


def _program_names_command(program: str, command: str) -> bool:
    """Whether a call's own invocation spells out the command, raw or JSON-escaped."""
    if not command:
        return False
    return command in program or json.dumps(command)[1:-1] in program


def _calls_in_turn(
    call_ids: list[str], turn_id: str, program_window_by_call_id: dict[str, dict[str, Any]]
) -> list[str]:
    """Those of ``call_ids`` made in the turn ``turn_id`` names, in call order; none when the turn is unknown."""
    if not turn_id:
        return []
    return [call_id for call_id in call_ids if program_window_by_call_id[call_id]["turn_id"] == turn_id]


def _owning_call(
    started_at_ms: int | None, command: str, turn_id: str, program_window_by_call_id: dict[str, dict[str, Any]]
) -> tuple[str, str] | None:
    """The call a command belongs to and the rule that decided it (see the module docstring)."""
    if started_at_ms is None:
        return None
    started_before = [
        call_id
        for call_id, window in program_window_by_call_id.items()
        if window["started_ms"] is not None and window["started_ms"] <= started_at_ms
    ]
    running = [
        call_id
        for call_id in started_before
        if program_window_by_call_id[call_id]["finished_ms"] is None
        or started_at_ms <= program_window_by_call_id[call_id]["finished_ms"]
    ]
    if len(running) == 1:
        return running[0], "running_program"
    if running:
        naming = [
            call_id
            for call_id in running
            if _program_names_command(program_window_by_call_id[call_id]["program"], command)
        ]
        if len(naming) == 1:
            return naming[0], "named_in_program"
        # Programs that spell out the command stay ahead of those that do not, even when several do.
        candidates = naming or running
        candidates_in_turn = _calls_in_turn(candidates, turn_id, program_window_by_call_id)
        if candidates_in_turn:
            return candidates_in_turn[-1], "latest_running_program_in_turn"
        return candidates[-1], "latest_running_program"
    if started_before:
        preceding_in_turn = _calls_in_turn(started_before, turn_id, program_window_by_call_id)
        if preceding_in_turn:
            return preceding_in_turn[-1], "nearest_preceding_call_in_turn"
        return started_before[-1], "nearest_preceding_call"
    return None


def _command_execution_record(
    raw: dict[str, Any],
    payload: dict[str, Any],
    item: dict[str, Any],
    program_window_by_call_id: dict[str, dict[str, Any]],
    existing_ids: set[str],
    line_index: int,
) -> dict[str, Any] | None:
    """The observation record for one finished shell command.

    None when no call can own the command (it started before any program ran) and when the
    stream already carries it.
    """
    started_at_ms = _json_int(payload.get("started_at_ms"))
    command = _command_text(item.get("command"))
    turn_id = payload.get("turn_id")
    owner = _owning_call(
        started_at_ms, command, turn_id if isinstance(turn_id, str) else "", program_window_by_call_id
    )
    if owner is None:
        return None
    call_id, attribution = owner
    raw_timestamp = raw.get("timestamp")
    id_timestamp = raw_timestamp if isinstance(raw_timestamp, str) else ""
    item_id = item.get("id")
    id_content = (
        item_id if isinstance(item_id, str) and item_id else json.dumps(item, sort_keys=True, separators=(",", ":"))
    )
    event_id = _make_event_id(id_timestamp, id_content, _COMMAND_EXECUTION_TOOL_NAME)
    if _is_already_converted(existing_ids, event_id, line_index, _COMMAND_EXECUTION_TOOL_NAME):
        return None
    argv = item.get("command")
    status = item.get("status")
    output = item.get("aggregated_output")
    output_text = output if isinstance(output, str) else ""
    command_execution: dict[str, Any] = {
        "cmd": command,
        "argv": argv if isinstance(argv, list) else [],
        "cwd": _local_path(item.get("cwd")),
        "status": status if isinstance(status, str) else "",
        "exit_code": _json_int(item.get("exit_code")),
        "duration_ms": _duration_ms(item.get("duration")),
        "started_at_ms": started_at_ms,
        "completed_at_ms": _json_int(payload.get("completed_at_ms")),
        "output": output_text[:_COMMAND_OUTPUT_CHAR_LIMIT],
        "attribution": attribution,
    }
    if len(output_text) > _COMMAND_OUTPUT_CHAR_LIMIT:
        command_execution["output_length"] = len(output_text)
    return {
        "type": "observation",
        "event_id": event_id,
        "emitter": _EMITTER,
        "timestamp": _iso_timestamp(raw_timestamp),
        "results": [
            {
                "source_call_id": call_id,
                "content": "",
                "extra": {"tool_name": _COMMAND_EXECUTION_TOOL_NAME, "command_execution": command_execution},
            }
        ],
    }


def _header_event_id(agent_id: str) -> str:
    """A per-stream header id, hashed from the agent id and emitter.

    A fixed "header" id repeats identically for every agent on every host, so
    analytics' fleet-wide event-id dedupe collapses all header rows to one --
    destroying the per-agent (emitter, schema_version) mix. The agent id is a
    UUID4-based value, so distinct agents never collide; a migrated agent keeps
    its id on the new host, which is the right grain for its one logical stream.
    """
    digest = hashlib.sha256(f"{agent_id}:{_EMITTER}".encode("utf-8", "replace")).hexdigest()[:32]
    return f"header-{digest}"


def _header_record(agent_id: str) -> dict[str, Any]:
    return {
        "type": "header",
        "event_id": _header_event_id(agent_id),
        "emitter": _EMITTER,
        "schema_version": _SCHEMA_VERSION,
    }


def _step_record(event_id: str, timestamp: str, source: str, message: str) -> dict[str, Any]:
    return {
        "type": "step",
        "event_id": event_id,
        "emitter": _EMITTER,
        "timestamp": timestamp,
        "source": source,
        "message": message,
    }


def _stamp_turn_context(step: dict[str, Any], model_name: str, reasoning_effort: JsonValue) -> None:
    """Record on an agent step which model, at which effort, produced it."""
    if model_name:
        step["model_name"] = model_name
    # ATIF types reasoning_effort as a string or a number; codex names it
    # ("minimal" / "low" / ...) and omits it entirely when the turn set none.
    if isinstance(reasoning_effort, (str, float)) and reasoning_effort != "":
        step["reasoning_effort"] = reasoning_effort


def _make_event_id(timestamp: str, content: str, kind: str) -> str:
    """A stable, globally unique event id from the record's own timestamp and content.

    Line-index ids repeat identically for every agent on every host (analytics
    dedupes transcripts fleet-wide by event id); hashing the line's timestamp
    plus content keeps re-processing idempotent while making ids unique.
    """
    digest = hashlib.sha256(f"{timestamp}:{content[:1024]}".encode("utf-8", "replace")).hexdigest()[:32]
    return f"evt-{digest}-{kind}"


def _is_already_converted(existing_ids: set[str], event_id: str, line_index: int, kind: str) -> bool:
    # CLEANUP: drop the legacy line-index id check once transcripts converted
    # before the content-hash ids existed have aged out of live agent hosts.
    return event_id in existing_ids or f"line-{line_index}-{kind}" in existing_ids


def _load_existing_ids(output_file: str) -> set[str]:
    ids: set[str] = set()
    if not os.path.isfile(output_file):
        return ids
    with open(output_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)["event_id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return ids


def convert(input_file: str, output_file: str, agent_id: str = "", is_input_complete: bool = False) -> int:
    """Append new common-transcript records from ``input_file`` to ``output_file``; return the count.

    ``agent_id`` seeds the stream's header event id; the production entrypoint
    passes the agent state directory's basename.

    Set ``is_input_complete`` only when the input is known to have stopped growing;
    otherwise a trailing agent step whose ``token_count`` has not landed yet is held
    back rather than emitted with its metrics missing for good. A complete input
    still holds the step while a call in its window awaits output (see the module
    docstring).
    """
    existing_ids = _load_existing_ids(output_file)
    if not os.path.isfile(input_file):
        return 0

    # Records in input-stream order: the doc-builder treats append order as
    # authoritative, so an observation must never precede its own call's step.
    records: list[dict[str, Any]] = []
    # Tool names of calls awaiting their output, keyed by codex's native call_id
    # (which is also the ATIF tool_call_id).
    pending_tool_name_by_call_id: dict[str, str] = {}
    # The model and reasoning effort of the turn in progress, from the most recent
    # turn_context.
    turn_model = ""
    turn_effort: JsonValue = None
    # The id of the turn in progress, from the most recent turn_context; empty when unknown.
    current_turn_id = ""
    # Index in ``records`` of the agent step the next token_count measures, or None
    # when there is none in the current window (see the pairing rule in the module
    # docstring).
    unmeasured_step_index: int | None = None
    # Calls started in the current measurement window whose output has not landed. codex
    # writes the window's token_count only after these outputs, so while any is pending
    # the usage is still coming however long the rollout has been silent.
    window_call_ids_awaiting_output: set[str] = set()
    # Whether this rollout reports usage, which decides whether a trailing unmeasured
    # step is worth waiting for. A turn_context counts too: codex writes one before a
    # turn's first inference, while the first token_count lands only after it.
    has_token_count = False
    has_turn_context = False
    # The running session total the previous token_count reported, to recognize a repeat of it.
    previous_total_usage: JsonValue = None
    # When each command-starting call's program ran, by call_id in call order: the call's
    # time, its stop time (None while it runs), its invocation text, and the turn it was made in.
    program_window_by_call_id: dict[str, dict[str, Any]] = {}
    # The call whose yielded program each live cell belongs to, and the cell each wait collects.
    call_id_by_live_cell_id: dict[str, str] = {}
    cell_id_by_wait_call_id: dict[str, str] = {}
    # Calls whose own output is already in the stream, and command records held until their
    # call's output lands so they never precede it.
    call_ids_with_output: set[str] = set()
    command_records_by_call_id: dict[str, list[dict[str, Any]]] = {}

    with open(input_file, encoding="utf-8", errors="replace") as f:
        for line_index, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue

            raw_type = raw.get("type")
            payload = raw.get("payload")

            if raw_type == "turn_context":
                # A new turn context both re-reads the model/effort and closes the
                # previous turn's measurement window (codex writes one after a
                # mid-turn compaction too, whose token_count measures nothing here).
                has_turn_context = True
                if isinstance(payload, dict):
                    model = payload.get("model")
                    # codex always writes a model; a line that lost it is truncated,
                    # not a model change, so the turn keeps the one it had.
                    if isinstance(model, str) and model:
                        turn_model = model
                    # effort is genuinely optional, so an absent one means "unset".
                    turn_effort = payload.get("effort")
                    # A turn_context without a turn id leaves the turn unknown rather than
                    # carrying the previous one forward onto this turn's calls.
                    turn_id = payload.get("turn_id")
                    current_turn_id = turn_id if isinstance(turn_id, str) else ""
                unmeasured_step_index = None
                window_call_ids_awaiting_output.clear()
                continue

            if raw_type == "event_msg":
                # token_count and a CommandExecution's item_completed are the event_msgs that
                # are not display duplicates of a response_item.
                if isinstance(payload, dict) and payload.get("type") == "token_count":
                    has_token_count = True
                    info = payload.get("info")
                    total_usage = info.get("total_token_usage") if isinstance(info, dict) else None
                    # A repeat of the previous token_count reports no response of its own (see the
                    # module docstring), so the inference in progress stays open.
                    if isinstance(total_usage, dict) and total_usage == previous_total_usage:
                        continue
                    previous_total_usage = total_usage
                    # A token_count closes an inference with its token usage.
                    metrics = _closing_metrics(info)
                    if metrics is not None and unmeasured_step_index is not None:
                        records[unmeasured_step_index]["metrics"] = metrics
                    # The inference is closed either way: a following token_count
                    # belongs to a later one.
                    unmeasured_step_index = None
                    window_call_ids_awaiting_output.clear()
                elif isinstance(payload, dict) and payload.get("type") == "item_completed":
                    item = payload.get("item")
                    if isinstance(item, dict) and item.get("type") == "CommandExecution":
                        command_record = _command_execution_record(
                            raw, payload, item, program_window_by_call_id, existing_ids, line_index
                        )
                        if command_record is not None:
                            owner_call_id = command_record["results"][0]["source_call_id"]
                            if owner_call_id in call_ids_with_output:
                                records.append(command_record)
                            else:
                                command_records_by_call_id.setdefault(owner_call_id, []).append(command_record)
                else:
                    # Every other event_msg is a display duplicate of a response_item.
                    continue
                continue

            if raw_type != "response_item":
                continue
            if not isinstance(payload, dict):
                continue

            raw_timestamp = raw.get("timestamp")
            timestamp = _iso_timestamp(raw_timestamp)
            # Hash the line's own timestamp, never _iso_timestamp's conversion-time
            # fallback: a wall-clock fallback would mint a fresh id on every run and
            # re-append the record each time.
            id_timestamp = raw_timestamp if isinstance(raw_timestamp, str) else ""
            payload_type = payload.get("type")

            if payload_type == "message" and payload.get("role") == "user":
                text = _join_content_text(payload.get("content"), "input_text")
                # An empty user message carries no signal -> drop it.
                if not text:
                    continue
                # Instruction injections ride in as user-role messages; they are
                # session configuration, so they become system steps instead.
                kind = "system" if _is_injected_instructions(text) else "user"
                # A turn boundary: a later token_count measures the reply to this
                # message, never an agent step from before it.
                unmeasured_step_index = None
                window_call_ids_awaiting_output.clear()
                event_id = _make_event_id(id_timestamp, text, kind)
                if _is_already_converted(existing_ids, event_id, line_index, kind):
                    continue
                records.append(_step_record(event_id, timestamp, kind, text))

            elif payload_type == "message" and payload.get("role") == "assistant":
                text = _join_content_text(payload.get("content"), "output_text")
                # An assistant message with no text carries no signal (codex models a
                # tool invocation as its own item, so there is nothing else on it) ->
                # drop it, exactly as an empty user message is dropped.
                if not text:
                    continue
                event_id = _make_event_id(id_timestamp, text, "assistant")
                if _is_already_converted(existing_ids, event_id, line_index, "assistant"):
                    # Already emitted, so out of reach of this pass's stamping: a
                    # later token_count must not fall back onto an earlier step.
                    unmeasured_step_index = None
                    continue
                message_step = _step_record(event_id, timestamp, "agent", text)
                _stamp_turn_context(message_step, turn_model, turn_effort)
                records.append(message_step)
                unmeasured_step_index = len(records) - 1

            elif payload_type == "reasoning":
                reasoning = _join_reasoning_text(payload)
                # An item whose only payload is encrypted_content exposes no reasoning
                # text, so there is nothing to record.
                if not reasoning:
                    continue
                event_id = _make_event_id(id_timestamp, reasoning, "reasoning")
                if _is_already_converted(existing_ids, event_id, line_index, "reasoning"):
                    unmeasured_step_index = None
                    continue
                reasoning_step = _step_record(event_id, timestamp, "agent", "")
                _stamp_turn_context(reasoning_step, turn_model, turn_effort)
                reasoning_step["reasoning_content"] = reasoning
                records.append(reasoning_step)
                unmeasured_step_index = len(records) - 1

            elif payload_type in ("function_call", "custom_tool_call"):
                call_id = payload.get("call_id")
                # Without codex's native call id there is no tool_call_id to pair a
                # result to, so the call (and its later output) is dropped.
                if not isinstance(call_id, str) or not call_id:
                    continue
                name = payload.get("name")
                tool_name = name if isinstance(name, str) else ""
                pending_tool_name_by_call_id[call_id] = tool_name
                window_call_ids_awaiting_output.add(call_id)
                # The 0.146 unified exec tool carries its invocation under "input";
                # every other call kind carries it under "arguments".
                invocation = (
                    payload.get("input", "") if payload_type == "custom_tool_call" else payload.get("arguments", "")
                )
                invocation_text = (
                    invocation if isinstance(invocation, str) else json.dumps(invocation, separators=(",", ":"))
                )
                # Tracked even for a call an earlier pass emitted: command attribution is
                # rebuilt from the whole rollout on every pass.
                if tool_name in _COMMAND_STARTING_TOOL_NAMES:
                    program_window_by_call_id[call_id] = {
                        "started_ms": _epoch_ms(raw_timestamp),
                        "finished_ms": None,
                        "program": invocation_text,
                        "turn_id": current_turn_id,
                    }
                elif tool_name == _WAIT_TOOL_NAME:
                    cell_id = _parse_arguments(invocation).get("cell_id")
                    if isinstance(cell_id, (str, int)) and not isinstance(cell_id, bool):
                        cell_id_by_wait_call_id[call_id] = str(cell_id)
                else:
                    # Any other tool (apply_patch, view_image, ...) neither starts a shell
                    # command nor collects a yielded cell, so it opens no program window.
                    pass
                event_id = _make_event_id(id_timestamp, invocation_text, "assistant")
                if _is_already_converted(existing_ids, event_id, line_index, "assistant"):
                    unmeasured_step_index = None
                    continue
                call_step = _step_record(event_id, timestamp, "agent", "")
                _stamp_turn_context(call_step, turn_model, turn_effort)
                call_step["tool_calls"] = [
                    {
                        "tool_call_id": call_id,
                        "function_name": tool_name,
                        "arguments": _parse_arguments(invocation),
                    }
                ]
                records.append(call_step)
                unmeasured_step_index = len(records) - 1

            elif payload_type in ("function_call_output", "custom_tool_call_output"):
                call_id = payload.get("call_id")
                # ATIF requires source_call_id on every streamed result; without the
                # native call id there is nothing to attach the result to.
                if not isinstance(call_id, str) or not call_id:
                    continue
                content = _stringify_output(payload.get("output", ""))
                _close_program_window(
                    call_id,
                    content,
                    _epoch_ms(raw_timestamp),
                    program_window_by_call_id,
                    call_id_by_live_cell_id,
                    cell_id_by_wait_call_id,
                )
                call_ids_with_output.add(call_id)
                window_call_ids_awaiting_output.discard(call_id)
                held_command_records = command_records_by_call_id.pop(call_id, [])
                event_id = _make_event_id(id_timestamp, content, "tool_result")
                if _is_already_converted(existing_ids, event_id, line_index, "tool_result"):
                    records.extend(held_command_records)
                    continue
                # An output whose call was never seen is still emitted, under the
                # "unknown" tool name; the doc-builder warns on the unmatched id.
                tool_name = pending_tool_name_by_call_id.pop(call_id, _UNKNOWN_TOOL_NAME)
                records.append(
                    {
                        "type": "observation",
                        "event_id": event_id,
                        "emitter": _EMITTER,
                        "timestamp": timestamp,
                        "results": [
                            {
                                "source_call_id": call_id,
                                "content": content,
                                "extra": {"is_error": False, "tool_name": tool_name},
                            }
                        ],
                    }
                )
                records.extend(held_command_records)

            else:
                # Other payload types (web_search_call, ...) and non-user/assistant
                # message roles (codex's own developer-role instruction items) are
                # bookkeeping, not conversation content.
                continue

    # Hold back the trailing agent step whose token_count has not landed yet, and
    # everything after it so append order stays authoritative. Only a rollout that
    # reports usage is worth waiting on. A complete input emits the step
    # unmeasured, since no token_count is coming -- unless a call in its window still
    # awaits output, which means the tool is still running and its usage will follow.
    is_usage_still_coming = not is_input_complete or bool(window_call_ids_awaiting_output)
    reports_usage = has_token_count or has_turn_context
    if reports_usage and unmeasured_step_index is not None and is_usage_still_coming:
        records = records[:unmeasured_step_index]

    if not records:
        return 0

    # The header is the first line of the stream, written on the first append and
    # deduped by its event_id like every other record.
    if _header_event_id(agent_id) not in existing_ids:
        records.insert(0, _header_record(agent_id))

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")

    return len(records)


if __name__ == "__main__":
    _state_dir = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    _agent_id = os.path.basename(os.path.normpath(_state_dir)) if _state_dir else ""
    print(
        convert(
            os.environ["_INPUT_FILE"],
            os.environ["_OUTPUT_FILE"],
            agent_id=_agent_id,
            is_input_complete=os.environ.get(_TRAILING_STEP_ENV_VAR) == "1",
        )
    )

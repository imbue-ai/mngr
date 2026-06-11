# agy conversation format: schema recovery + decoding

Developer tooling for reading agy's conversation store. Nothing here ships in the wheel
(it lives outside `imbue/`); it is the documented, repeatable process for recovering agy's
protobuf schema and decoding a conversation `.db`.

## Background: why this exists

agy stores each conversation as a SQLite database at
`$HOME/.gemini/antigravity-cli/conversations/<conversation_id>.db`.

This was **not always the case**. The transition, from agy's own changelog and confirmed
against real `~/.gemini` history:

| agy versions | released | conversation store |
|---|---|---|
| 1.0.0 – 1.0.3 | through 2026-05-31 | per-conversation JSONL at `brain/<id>/.system_generated/logs/transcript.jsonl` |
| 1.0.4 | 2026-06-01 | **SQLite `.db` becomes "the CLI's conversation format"**; JSONL still written for a few days |
| 1.0.5+ | 2026-06-03+ | SQLite `.db` only (interactive mode stops writing the JSONL) |

The original transcript streamer (`resources/stream_transcript.sh`) tails the **JSONL**,
which it was validated against on a live agy 1.0.0. Interactive agy >= ~1.0.5 no longer
writes it, so the streamer captures nothing. Note that `agy -p` (print mode) *still* writes
the JSONL as a side artifact -- which is why a print-mode "isolated repro" looked fine and
masked the break. The durable fix is to read the SQLite `.db`.

## The `.db` is protobuf, and agy publishes no schema

```
$ sqlite3 <conversation>.db .schema
CREATE TABLE `steps` (`idx` integer, `step_type` integer, `status` integer,
                      `step_payload` blob, ...);
CREATE TABLE `trajectory_meta` (`trajectory_id` text, `cascade_id` text, ...);
```

`steps.step_payload` is a **protobuf** blob (`json_valid` is false; first byte `0x08`), not
JSON. agy's GitHub repo is distribution-only (no `.proto` files), there is no `agy export`
command, and no schema is published anywhere -- the community "wire-walks" it blind.

We do better: the `agy` binary is built with `google.golang.org/protobuf`, which **embeds
each `.proto` file's `FileDescriptorProto`** as a raw byte slice. We extract those to get
the real field names and enum values.

## Recovering the schema (`extract_agy_proto_schema.py`, in the appendix)

Save the appendix script, then:

```
uv run python extract_agy_proto_schema.py "$(which agy)" --out /tmp/agy_descriptors --grep CortexStepType
```

This scans the binary for embedded `FileDescriptorProto`s (anchored on the
`0x0A <len> "<name>.proto"` name field), validates them, and writes/greps them. On agy
1.0.7 it recovers ~164 descriptors. `--grep` prints matching message/enum layouts.

A few large descriptors registered via protobuf-go's legacy gzip path are not recovered
(e.g. `codeium_common.proto`, which defines `ChatToolCall`/`ModelUsageStats`). The decoder
below does not need them for the message text -- only for full tool-call/usage detail.

## The recovered schema (agy 1.0.7 -- re-verify on each release)

SQLite tables map to `exa.analytics_pb` "Cortex trajectory" messages:
`trajectory_meta` <- `RecordCortexTrajectoryRequest`; each `steps` row's `step_payload`
is a serialized **`gemini_coder.Step`** (defined in `third_party/gemini_coder/proto/trajectory.proto`):

```
gemini_coder.Step:
  f1  type     enum exa.cortex_pb.CortexStepType
  f4  status   enum exa.cortex_pb.CortexStepStatus
  f5  metadata      exa.cortex_pb.CortexStepMetadata { f1 created_at{f1 sec,f2 nanos}; f3 source }
  f6  subtrajectory gemini_coder.Trajectory          (subagent steps)
  f10 code_action   exa.cortex_pb.CortexStepCodeAction
  f19 user_input    exa.cortex_pb.CortexStepUserInput        { f1 query; f2 user_response }
  f20 planner_response exa.cortex_pb.CortexStepPlannerResponse { f1 response; f3 thinking; f7 tool_calls[] }
  f24 error_message exa.cortex_pb.CortexStepErrorMessage
  ... ~60 more tool/browser step types
```

Enums (subset relevant to a transcript):

```
CortexStepType:   14 USER_INPUT   15 PLANNER_RESPONSE   5 CODE_ACTION   17 ERROR_MESSAGE
                  98 CONVERSATION_HISTORY (bookkeeping; dropped)   101 SYSTEM_MESSAGE
CortexStepSource: 2 MODEL   3 USER_IMPLICIT   4 USER_EXPLICIT   5 SYSTEM   6 SYSTEM_SDK
CortexStepStatus: 1 PENDING 2 RUNNING 3 DONE 7 ERROR 8 GENERATING 9 WAITING 11 QUEUED ...
```

Common-transcript mapping (mirrors the old JSONL converter's source/type mapping):

| Step.type | role | text field |
|---|---|---|
| `USER_INPUT` (14) | `user_message` | `user_input.query` or `user_input.user_response` |
| `PLANNER_RESPONSE` (15) | `assistant_message` | `planner_response.response` (+ `thinking`, `tool_calls`) |
| `CODE_ACTION` (5) | `tool_result` | `code_action.action_result` |
| `CONVERSATION_HISTORY` (98) | dropped | -- |

## Decoding a conversation (`decode_agy_conversation_db.py`, in the appendix)

```
uv run python decode_agy_conversation_db.py <conversation.db>
```

A self-contained protobuf wire-walker keyed to the field map above -- **no `protobuf`
library and no shipped descriptors required** (only Python's stdlib `sqlite3`). This is the
reference for the production transcript decoder. It opens the `.db` read-only with
`immutable=1` because it reads a completed, static snapshot -- not a db agy is actively
writing. (The production decoder, which streams a *live* db, deliberately uses `mode=ro`
*without* `immutable=1`; see its `stream_conversation` docstring for why `immutable=1` is
unsafe against a concurrent writer.)

## Redoing this after an agy release

agy ships ~weekly and can renumber fields/enums. To re-verify or update the field map:

1. `extract_agy_proto_schema.py "$(which agy)" --grep CortexStep` and diff the
   `gemini_coder.Step` / `CortexStepType` / `CortexStepSource` output against the tables above.
2. If field numbers moved, update the constants in `decode_agy_conversation_db.py` (and the
   production decoder) accordingly.
3. Decode a fresh `.db` and confirm user/assistant text round-trips.

## Appendix: scripts

Save and run these with `uv run python <file> ...` from the repo root (they use the venv's
stdlib `sqlite3` and, for extraction only, the `google.protobuf` library already present in
the dev venv). They live here as documentation rather than as committed `.py` files because
the project's ratchet/pyright/ruff checks scan all in-tree `.py`, and these dev tools use
patterns (raw `while True` varint loops, broad protobuf parse guards) that those checks
intentionally discourage in production code.

### extract_agy_proto_schema.py

```python
"""Recover agy's embedded protobuf schema from the ``agy`` binary.

Why this exists
---------------
agy stores each conversation as a SQLite ``.db`` (one file per conversation under
``$HOME/.gemini/antigravity-cli/conversations/<id>.db``). The conversation rows live in
the ``steps`` table, and ``steps.step_payload`` is a **protobuf** blob (``gemini_coder.Step``)
-- not JSON. agy publishes no ``.proto`` schema (the GitHub repo is distribution-only) and
ships no export command, so to decode a conversation we need the field/enum layout.

The ``agy`` binary is built with ``google.golang.org/protobuf``, which embeds each
``.proto`` file's serialized ``FileDescriptorProto`` as a raw (uncompressed) byte slice.
This script scans the binary for those, validates them, and writes them out. From the
recovered descriptors you can read the real field names/numbers and enum values that
``decode_agy_conversation_db.py`` keys off.

agy ships ~weekly and may renumber fields, so re-run this against each new binary and
diff the output (see ``dev/README.md`` for the full procedure and the schema map).

Usage
-----
    uv run python libs/mngr_antigravity/dev/extract_agy_proto_schema.py "$(which agy)" \
        --out /tmp/agy_descriptors [--grep Step]

``--grep`` prints message/enum types whose fully-qualified name contains the substring
(case-insensitive), e.g. ``--grep CortexStep``. Without ``--out`` nothing is written.

Method
------
Each ``FileDescriptorProto`` begins with field 1 (``name``): tag ``0x0A``, a varint
length, then ``"<path>.proto"``. We anchor on those, walk only top-level fields valid for
``FileDescriptorProto`` (numbers 1..14) to find the extent, and accept the slice if it
parses and carries content. A handful of large descriptors that protobuf-go registers via
the legacy gzip path are not recovered this way (e.g. ``codeium_common.proto``); the
targeted decoder does not need them (see the decoder's module docstring).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from google.protobuf import descriptor_pb2

# Top-level field numbers defined by FileDescriptorProto (1..12 plus 13/14 for the
# edition-era additions). The extent walker stops at the first field outside this set.
_FDP_TOP_LEVEL_FIELDS = set(range(1, 15))


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while i < len(data):
        byte = data[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")
    raise ValueError("varint truncated")


def _walk_extent(data: bytes, start: int) -> int | None:
    """Return the end offset of a plausible FileDescriptorProto at ``start``, or None.

    Walks only top-level fields whose number is valid for FileDescriptorProto and whose
    wire type is varint (0) or length-delimited (2), skipping over nested sub-messages by
    their length prefix. Stops at the first byte that is not such a field.
    """
    i = start
    saw_name = False
    n = len(data)
    while i < n:
        try:
            tag, j = _read_varint(data, i)
        except ValueError:
            break
        field = tag >> 3
        wire = tag & 7
        if field not in _FDP_TOP_LEVEL_FIELDS or wire not in (0, 2):
            break
        i = j
        if wire == 0:
            try:
                _, i = _read_varint(data, i)
            except ValueError:
                return None
        else:
            try:
                length, k = _read_varint(data, i)
            except ValueError:
                break
            if k + length > n:
                break
            i = k + length
        if field == 1:
            saw_name = True
    return i if saw_name and i > start + 2 else None


def _find_anchors(data: bytes) -> list[int]:
    """Find offsets where a FileDescriptorProto's name field (``0x0A <len> "...proto"``) starts."""
    anchors: set[int] = set()
    needle = b".proto"
    pos = 0
    while True:
        p = data.find(needle, pos)
        if p < 0:
            break
        pos = p + 1
        end_name = p + len(needle)
        # The name bytes precede ``.proto``; the tag (0x0A) + length varint precede the name.
        for namelen in range(min(end_name, 120), 0, -1):
            s_name = end_name - namelen
            if s_name < 2:
                continue
            for vlen in (1, 2):
                t = s_name - 1 - vlen
                if t < 0 or data[t] != 0x0A:
                    continue
                try:
                    length, after = _read_varint(data, t + 1)
                except ValueError:
                    continue
                if length == namelen and after == s_name:
                    name = data[s_name:end_name]
                    if all(32 <= c < 127 for c in name):
                        anchors.add(t)
    return sorted(anchors)


def extract(binary_path: Path) -> dict[str, bytes]:
    """Return a mapping of ``.proto`` file name -> serialized FileDescriptorProto bytes."""
    data = binary_path.read_bytes()
    best: dict[str, tuple[int, bytes]] = {}
    for anchor in _find_anchors(data):
        end = _walk_extent(data, anchor)
        if end is None:
            continue
        blob = data[anchor:end]
        fdp = descriptor_pb2.FileDescriptorProto()
        try:
            fdp.ParseFromString(blob)
        except Exception:
            continue
        if not fdp.name.endswith(".proto"):
            continue
        if not (fdp.message_type or fdp.enum_type or fdp.dependency):
            continue
        # The extent walker can over- or under-shoot; keep the candidate whose re-serialized
        # size is closest to the slice (favours a clean, complete parse).
        score = abs(len(fdp.SerializeToString()) - len(blob))
        prev = best.get(fdp.name)
        if prev is None or score < prev[0]:
            best[fdp.name] = (score, blob)
    return {name: blob for name, (_, blob) in best.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path, help="Path to the agy binary")
    parser.add_argument("--out", type=Path, default=None, help="Directory to write <name>.fdp files")
    parser.add_argument("--grep", default=None, help="Print message/enum FQNs containing this substring")
    args = parser.parse_args()

    descriptors = extract(args.binary)
    print(f"recovered {len(descriptors)} FileDescriptorProtos")

    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        for name, blob in descriptors.items():
            (args.out / f"{name.replace('/', '__')}.fdp").write_bytes(blob)
        print(f"wrote descriptors to {args.out}")

    if args.grep is not None:
        needle = args.grep.lower()
        for fdp_bytes in descriptors.values():
            fdp = descriptor_pb2.FileDescriptorProto()
            fdp.ParseFromString(fdp_bytes)

            def _walk(prefix: str, messages, enums) -> None:
                for enum in enums:
                    fqn = f"{prefix}.{enum.name}"
                    if needle in fqn.lower():
                        values = ", ".join(f"{v.number}={v.name}" for v in enum.value)
                        print(f"ENUM {fqn} [{fdp.name}]: {values}")
                for message in messages:
                    fqn = f"{prefix}.{message.name}"
                    if needle in fqn.lower():
                        fields = ", ".join(f"f{f.number} {f.name}" for f in message.field)
                        print(f"MSG  {fqn} [{fdp.name}]: {fields}")
                    _walk(fqn, message.nested_type, message.enum_type)

            _walk(fdp.package, fdp.message_type, fdp.enum_type)


if __name__ == "__main__":
    main()
```

### decode_agy_conversation_db.py

```python
"""Decode an agy conversation SQLite ``.db`` into a readable transcript.

This is the reference implementation of the lightweight, self-contained protobuf walk
that the production transcript pipeline uses to read agy >= 1.0.4 conversations (agy 1.0.4,
2026-06-01, made SQLite "the CLI's conversation format"; before that, interactive agy wrote
a per-conversation JSONL transcript that the old streamer tailed). It does **not** depend on
the ``protobuf`` library or any shipped descriptors -- it walks the wire format directly,
keyed to the field map recovered by ``extract_agy_proto_schema.py`` (see ``dev/README.md``).

Schema (recovered field map -- re-verify against new agy releases):

    steps.step_payload = gemini_coder.Step:
        f1  type    enum CortexStepType
        f4  status  enum CortexStepStatus
        f5  metadata     CortexStepMetadata { f1 created_at{f1 sec,f2 nanos}; f3 source enum }
        f19 user_input        CortexStepUserInput        { f1 query; f2 user_response }
        f20 planner_response  CortexStepPlannerResponse  { f1 response; f3 thinking; f7 tool_calls[] }
        f10 code_action       CortexStepCodeAction
        f24 error_message     CortexStepErrorMessage     { f1 text (best-effort) }

    CortexStepType:   14=USER_INPUT 15=PLANNER_RESPONSE 5=CODE_ACTION 17=ERROR_MESSAGE
                      98=CONVERSATION_HISTORY (bookkeeping; dropped) 101=SYSTEM_MESSAGE ...
    CortexStepSource: 2=MODEL 3=USER_IMPLICIT 4=USER_EXPLICIT 5=SYSTEM 6=SYSTEM_SDK
    CortexStepStatus: 3=DONE 7=ERROR 8=GENERATING 2=RUNNING ...

Usage
-----
    uv run python libs/mngr_antigravity/dev/decode_agy_conversation_db.py <conversation.db>
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterator
from pathlib import Path

# gemini_coder.Step field numbers.
_STEP_TYPE = 1
_STEP_METADATA = 5
_STEP_USER_INPUT = 19
_STEP_PLANNER_RESPONSE = 20
_STEP_ERROR_MESSAGE = 24
# CortexStepMetadata.source.
_METADATA_SOURCE = 3
# CortexStepUserInput: the typed message lands in either query (f1) or user_response (f2).
_USER_INPUT_QUERY = 1
_USER_INPUT_RESPONSE = 2
# CortexStepPlannerResponse.
_PLANNER_RESPONSE_TEXT = 1
_PLANNER_THINKING = 3
_PLANNER_TOOL_CALLS = 7

# CortexStepType values we surface in a transcript (others are tool/browser steps).
_TYPE_USER_INPUT = 14
_TYPE_PLANNER_RESPONSE = 15
_TYPE_ERROR_MESSAGE = 17
_TYPE_CONVERSATION_HISTORY = 98  # bookkeeping replay of prior turns; dropped

_TYPE_LABELS = {
    14: "user",
    15: "assistant",
    5: "tool_result",
    17: "error",
    98: "conv_history",
    101: "system",
    21: "run_command",
    23: "checkpoint",
}


def _iter_fields(blob: bytes) -> Iterator[tuple[int, int, object]]:
    """Yield ``(field_number, wire_type, value)`` for a protobuf message.

    ``value`` is an ``int`` for varints and ``bytes`` for length-delimited fields (which may
    be a nested message or a string); 32/64-bit fixed fields yield their raw bytes.
    """
    i = 0
    n = len(blob)
    while i < n:
        tag = 0
        shift = 0
        while True:
            byte = blob[i]
            i += 1
            tag |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        field = tag >> 3
        wire = tag & 7
        if wire == 0:
            value = 0
            shift = 0
            while True:
                byte = blob[i]
                i += 1
                value |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            yield field, wire, value
        elif wire == 2:
            length = 0
            shift = 0
            while True:
                byte = blob[i]
                i += 1
                length |= (byte & 0x7F) << shift
                if not byte & 0x80:
                    break
                shift += 7
            yield field, wire, blob[i : i + length]
            i += length
        elif wire == 5:
            yield field, wire, blob[i : i + 4]
            i += 4
        elif wire == 1:
            yield field, wire, blob[i : i + 8]
            i += 8
        else:
            return


def _first(blob: bytes, field_number: int) -> object | None:
    for field, _wire, value in _iter_fields(blob):
        if field == field_number:
            return value
    return None


def _count(blob: bytes, field_number: int) -> int:
    return sum(1 for field, _wire, _value in _iter_fields(blob) if field == field_number)


def _text(value: object | None) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return ""


def decode_step(step_type: int, payload: bytes) -> tuple[str, str]:
    """Return ``(label, text)`` for one ``steps`` row's ``step_payload``."""
    label = _TYPE_LABELS.get(step_type, f"type{step_type}")
    if step_type == _TYPE_USER_INPUT:
        user_input = _first(payload, _STEP_USER_INPUT)
        if isinstance(user_input, bytes):
            return label, _text(_first(user_input, _USER_INPUT_QUERY)) or _text(
                _first(user_input, _USER_INPUT_RESPONSE)
            )
    elif step_type == _TYPE_PLANNER_RESPONSE:
        planner = _first(payload, _STEP_PLANNER_RESPONSE)
        if isinstance(planner, bytes):
            text = _text(_first(planner, _PLANNER_RESPONSE_TEXT))
            extras = []
            if _first(planner, _PLANNER_THINKING) is not None:
                extras.append("thinking")
            tool_calls = _count(planner, _PLANNER_TOOL_CALLS)
            if tool_calls:
                extras.append(f"{tool_calls} tool_calls")
            return label, text + (f"  [{', '.join(extras)}]" if extras else "")
    elif step_type == _TYPE_ERROR_MESSAGE:
        error = _first(payload, _STEP_ERROR_MESSAGE)
        if isinstance(error, bytes):
            return label, _text(_first(error, 1))
    return label, ""


def decode_conversation(db_path: Path) -> Iterator[tuple[int, str, int | None, str]]:
    """Yield ``(idx, label, source, text)`` for each step, dropping conversation-history rows.

    Opens the database read-only with immutable=1, which is safe here because this tool reads
    a completed, static snapshot (not a db agy is concurrently writing). The production
    streaming decoder must instead use mode=ro without immutable=1; see its stream_conversation
    docstring for why immutable=1 is unsafe against a live writer.
    """
    connection = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    try:
        rows = connection.execute("SELECT idx, step_type, step_payload FROM steps ORDER BY idx")
        for idx, step_type, payload in rows:
            if step_type == _TYPE_CONVERSATION_HISTORY:
                continue
            metadata = _first(payload, _STEP_METADATA)
            source = _first(metadata, _METADATA_SOURCE) if isinstance(metadata, bytes) else None
            label, text = decode_step(step_type, payload)
            yield idx, label, source if isinstance(source, int) else None, text
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", type=Path, help="Path to a conversation .db file")
    args = parser.parse_args()
    print(f"=== {args.db.name} ===")
    for idx, label, source, text in decode_conversation(args.db):
        print(f"  [{idx}] {label} (src={source}): {text[:160]!r}")


if __name__ == "__main__":
    main()
```

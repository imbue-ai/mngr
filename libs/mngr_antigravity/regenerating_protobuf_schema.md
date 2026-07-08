# agy conversation format: recovering the protobuf schema

How to recover agy's protobuf conversation schema, map it onto the records the transcript
decoder emits, and re-verify it after an agy release. This is a maintenance procedure, not
shipped code -- the production reader is
`imbue/mngr_antigravity/resources/decode_agy_transcript.py` and the extractor is
`scripts/extract_antigravity_proto_schema.py`.

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

## Recovering the schema (`scripts/extract_antigravity_proto_schema.py`)

Run the committed extractor:

```
uv run python scripts/extract_antigravity_proto_schema.py "$(which agy)" --out /tmp/agy_descriptors --grep CortexStepType
```

This scans the binary for embedded `FileDescriptorProto`s (anchored on the
`0x0A <len> "<name>.proto"` name field), validates them, and writes/greps them. On agy
1.0.8 it recovers ~163 descriptors. `--grep` prints matching message/enum layouts.

A few large descriptors registered via protobuf-go's legacy gzip path are not recovered
(e.g. `codeium_common.proto`, which defines `ChatToolCall`/`ModelUsageStats`). The decoder
does not need them for the message text -- only for full tool-call/usage detail.

## The recovered schema (agy 1.0.8 -- re-verify on each release)

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
  f24 error_message exa.cortex_pb.CortexStepErrorMessage { f3 error -> CortexErrorDetails { f1 user_error_message, f2 short_error, f3 full_error } }
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
| `CODE_ACTION` (5) | `tool_result` | none decoded yet (the converter emits the paired `tool_result` with empty output; agy records command output in step types the decoder does not map, and file-edit CODE_ACTION steps do not occur in practice -- a follow-up if needed) |
| `CONVERSATION_HISTORY` (98) | dropped | -- |

## Decoding a conversation

The production reader is `imbue/mngr_antigravity/resources/decode_agy_transcript.py` -- a
self-contained protobuf wire-walk keyed to the field map above (**no `protobuf` library and no
shipped descriptors required**; only Python's stdlib `sqlite3`). It is what
`stream_transcript.sh` runs: it reads new `steps` rows from each of an agent's conversation
`.db` files and emits one JSON record per step for `common_transcript.sh`.

It opens the live `.db` `mode=ro` **without** `immutable=1`, because agy may be writing it
concurrently; see its `stream_conversation` docstring for why `immutable=1` is unsafe against a
live writer (static-snapshot tooling that reads a completed db could use `immutable=1`).

To eyeball a single `.db` by hand, call the same code directly (offset `-1` = from the start;
it gates on terminal status, so still-generating tail steps are omitted until they settle):

```
uv run python -c "from pathlib import Path; from imbue.mngr_antigravity.resources.decode_agy_transcript import stream_conversation; lines,_ = stream_conversation(Path('<conversation>.db'), 'conv', -1); print('\n'.join(lines))"
```

## Redoing this after an agy release

agy ships ~weekly. Releases are normally additive (new fields and enum values), which the
number-keyed decoder tolerates automatically: unknown fields are skipped and unknown enum
values fall back to `STEP_TYPE_<n>`. The case it cannot catch on its own is agy *reusing* an
existing field number for a new meaning -- protobuf forbids that, but agy controls both ends --
so re-verify or update the field map after each release:

1. Run the release-marked verification test. It extracts the live binary's descriptors via
   `scripts/extract_antigravity_proto_schema.py` and asserts every field number / enum value the
   production decoder relies on still matches -- turning the eyeball diff into an exact check. It
   requires `agy` on PATH (a missing binary is a hard failure, not a skip):

   ```
   just test libs/mngr_antigravity/imbue/mngr_antigravity/resources/test_antigravity_proto_schema.py::test_decoder_field_map_matches_installed_antigravity_binary
   ```

   To inspect the raw layout by hand instead, run the extractor and diff the `gemini_coder.Step`
   / `CortexStepType` / `CortexStepSource` output against the tables above:
   `uv run python scripts/extract_antigravity_proto_schema.py "$(which agy)" --grep CortexStep`.
2. If anything moved, update the constants in `decode_agy_transcript.py`, then re-run the test.
3. Decode a fresh `.db` and confirm user/assistant text round-trips.

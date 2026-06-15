Fixed a macOS keychain barrier that blocked antigravity (`agy`) agents. agy embeds
Chromium, whose `os_crypt` stores its "Antigravity Safe Storage" key (which encrypts agy's
persisted conversation store) in the login keychain that macOS resolves at
`$HOME/Library/Keychains`. The per-agent `$HOME` relocation that isolates agy's config also
hid that directory, so agy found no keychain and macOS raised a modal "A keychain cannot be
found to store Antigravity Safe Storage" dialog -- which blocked agy until dismissed,
hanging any unattended run and popping on every fresh agent interactively.

Provisioning now symlinks the per-agent home's `Library/Keychains` to the user's real one
on macOS (Linux has no such keychain and Chromium falls back to its file-based store, so
nothing changes there). agy is already in the keychain item's ACL from interactive logins,
so it reads the key with no prompt. This mirrors the existing playwright-cache symlink --
another HOME-relative, machine-shared resource -- and the claude-style "straightforward on
Linux, keychain on macOS" split.

Also added the antigravity end-to-end release test (`test_antigravity_agent_e2e.py`) on the
shared agent release-lifecycle harness, which this fix unblocks.

Ported the antigravity transcript streamer to agy's new conversation store. agy 1.0.4
(2026-06-01) switched its interactive store from a per-conversation JSONL transcript (which
the old streamer tailed, and which agy no longer writes) to a protobuf SQLite `.db`, so the
streamer was capturing nothing on current agy. `stream_transcript.sh` is now a thin,
python3-guarded supervisor around a new self-contained decoder (`decode_agy_transcript.py`)
that reads new steps from each conversation `.db` and emits the same record shape the old
JSONL had, so the common-transcript converter is unchanged (it now also accepts agy's clean,
un-enveloped user text). The decoder needs no `protobuf` library or shipped schema -- it is a
small wire-walk keyed to the field map recovered from the binary's embedded descriptors;
`regenerating_protobuf_schema.md` documents that recovered schema and a repeatable process to
re-verify it after each (roughly weekly) agy release. Assistant tool calls (name + args) are decoded too,
so they surface on assistant messages. (Tool *results* are not yet captured as `tool_result`
events: agy records command output in step types the converter does not map, and file-edit
`CODE_ACTION` steps do not occur in practice -- a follow-up if needed.)

Added a release-marked test (`test_antigravity_proto_schema.py`) that mechanizes the
"re-verify the schema after each agy release" procedure from `regenerating_protobuf_schema.md`: it runs the
schema extractor against the installed `agy` binary and asserts every field number and enum
value the transcript decoder hard-codes still matches. It requires `agy` on PATH (a missing
binary is a hard failure, not a skip, since there is nothing to verify against without it).

Fixed ERROR_MESSAGE transcript decoding, which that verification surfaced: agy's
`CortexStepErrorMessage` carries no text directly -- the user-facing message lives in its
nested `error` field (a `CortexErrorDetails`), so the decoder, which read a non-existent
top-level text field, always produced empty content for error steps. It now descends into
`CortexErrorDetails.user_error_message` (falling back to `short_error` / `full_error`).

Lowered the antigravity full-lifecycle release test's wall-clock timeout from 1500s to 600s.
The 1500s was copied from sibling agent tests before this test had ever completed a run; a
healthy run measures ~25s. Also marked the test `flaky`: its post-resume "recall" step
occasionally hangs on agy's TUI message-submission signal (observed on agy 1.0.8).

Simplified the common-transcript converter's user-message handling to match agy's current
store: it now passes through the clean typed text agy records in `CortexStepUserInput.query`,
dropping the speculative `<USER_REQUEST>...</USER_REQUEST>` envelope stripping that existed
only for the retired agy-1.0.0 JSONL format.

Hardened the SQLite decoder against malformed/truncated protobuf so a single bad step can no
longer take down transcript capture. A `created_at` timestamp outside the platform range now
degrades to an empty timestamp instead of raising an uncaught error that aborted the entire
decode pass (which, since the offset never advanced, blacked out every conversation on every
cycle); such an out-of-range value comes from a corrupt or truncated payload, not from normal
agy releases, which are additive and keep the wire format valid. Truncated fixed-width
(32/64-bit) fields and unknown protobuf wire types are now detected as malformed and the step is
retried, matching the existing length-delimited handling, rather than silently yielding corrupt
data and advancing past the step. A corrupt per-conversation offset file now resets to the start
instead of crashing. Validated end-to-end by decoding real agy 1.0.8 conversation stores
(including the `ChatToolCall` name/args path the schema-verification test cannot reach).

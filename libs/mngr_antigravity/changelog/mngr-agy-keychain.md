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
`dev/README.md` documents that recovered schema and a repeatable process to re-verify it
after each (roughly weekly) agy release. Tool-call/code-action detail is not yet decoded.

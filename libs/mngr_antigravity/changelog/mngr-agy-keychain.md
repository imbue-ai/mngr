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

Documented a follow-up: the transcript streamer reads the per-conversation JSONL that agy
wrote through 1.0.3, but agy 1.0.4 (2026-06-01) switched its interactive conversation store
to a protobuf SQLite `.db`, so the streamer captures nothing on current agy. `dev/README.md`
records the recovered `.db` protobuf schema (from the binary's embedded descriptors) and a
repeatable process to re-verify it, as the basis for porting the streamer to read the `.db`.

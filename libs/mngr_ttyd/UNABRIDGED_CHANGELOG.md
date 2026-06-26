# Unabridged Changelog - mngr_ttyd

Full, unedited changelog entries for the `mngr_ttyd` project, consolidated nightly from individual files in `libs/mngr_ttyd/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-25

Fixed copy-paste in the browser terminal. The plugin now serves its own OSC 52-capable web client to the stock `ttyd` binary via `ttyd -I`, so copying text inside a tmux session running in the browser terminal reaches the system clipboard (the released `ttyd` 1.7.7 client has no OSC 52 handler). `mouse on` is kept, so mouse-wheel scroll and in-app mouse continue to work. The client is vendored gzip-compressed and decompressed onto each agent host during provisioning; if it is missing, `ttyd` cleanly falls back to its built-in client. Rebuild it with `scripts/build_patched_ttyd_client.sh`.

Added `scripts/repro_ttyd_tmux_copy_paste.sh`, a self-contained local reproduction that runs `ttyd` against a tmux session on a dedicated socket and exposes the relevant tmux and `ttyd` client options as environment-variable knobs, used to diagnose the copy-paste / mouse-wheel behavior.

Test branch combining the paired `mngr/fix-copy` changes (mngr-side and forever-claude-template-side) to manually verify the tmux copy/clipboard fixes end-to-end in the minds app.

The `mngr_ttyd` plugin now serves an OSC 52-capable ttyd web client so that a mouse-drag copy inside the browser terminal reaches the system clipboard.

## 2026-06-19

Trimmed the README to user-relevant content and tightened it for concision.

## 2026-06-18

The ttyd web-terminal attach now targets the agent's primary tmux window by name (`tmux.primary_window_name`, default `agent`, read from `MNGR_PRIMARY_WINDOW_NAME`) instead of the literal `:0` index, so attaching to an agent's terminal in the browser works regardless of the user's tmux `base-index` setting.

## 2026-06-12

Internal: routed the agent state-dir path construction through the shared `get_agent_state_dir_path` helper (now in `imbue.mngr.hosts.common`). No behavior change.

## 2026-06-08

Tests now isolate $HOME the same way as every other mngr plugin: the project
conftest calls `register_plugin_test_fixtures(globals())`, which brings in the
autouse `setup_test_mngr_env` fixture. Previously this plugin's tests did not
redirect $HOME, so running them on their own could read or write the real
`~/.mngr` / `~/.claude.json`. Internal test-infrastructure change only; no
user-facing behavior change.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# Ratchet count tightening

- Tightened the violation counts recorded in `test_ratchets.py` to their current exact values (via `uv run pytest --inline-snapshot=trim`), locking in previously-unrecorded reductions. No source-code or behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Switched `resources/ttyd_agent.sh` to use exact-session matching when attaching
to a named agent via URL arg. The previous `tmux attach -t "$_SESSION:0"` form could
silently route to a sibling session whose name starts with the requested one, e.g.
attaching by name `gemini` when `mngr-gemini` is gone but `mngr-gemini-foo` is alive
would land the browser ttyd window on the wrong agent. The script now passes
`=$_SESSION:0` so tmux refuses to misroute.

To prevent recurrences, adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule
(added in `imbue_common`) via `rc.check_bare_tmux_targets(_DIR, snapshot(0))` in
this project's `test_ratchets.py`. The ratchet flags new occurrences of
`tmux <subcmd> -t '<bare-name>'` -- targets without a leading `=` exact-match
prefix, which can silently route commands to a sibling session whose name shares
a prefix with the intended one. The adopting test starts at a baseline of zero
violations.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

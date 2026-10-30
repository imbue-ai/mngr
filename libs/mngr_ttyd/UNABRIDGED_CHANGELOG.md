# Unabridged Changelog - mngr_ttyd

Full, unedited changelog entries for the `mngr_ttyd` project, consolidated nightly from individual files in `libs/mngr_ttyd/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-29

Adds the repo-wide `test_prevent_httpx_import` ratchet, synced into every project's `test_ratchets.py`. It counts `import httpx` / `from httpx` statements, since the repo's HTTP clients now use `httpx2`; `httpx` is kept only where a third-party SDK built on it raises its errors.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

No behavior change. The ttyd pin-drift test no longer explains itself by saying a Dockerfile build `ARG` expands to empty on Modal, which stopped being true now that the Modal provider keeps every `ARG` in scope across cached layers. The test still asserts that the shipped Dockerfile's ttyd pin matches `TTYD_VERSION`, for the reason that still holds: a Dockerfile cannot read the constant.

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-09-29

The web terminal now fits itself to its frame whenever it reconnects. Before, a terminal resized while its connection was down (a laptop sleep, a network drop, a tunnel restart) came back at its old width and stayed there until the next resize: text was cut off at the old width, with empty space beside it. This is what truncated the minds chat's source view after its window was resized. The vendored web client is rebuilt with a new patch, `scripts/ttyd_refit_on_connect.patch`.

## 2026-09-21

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-09-10

Stopped re-downloading the `ttyd` binary on every `mngr create`.

When `ttyd` was missing, the plugin fetched a 1.36 MB binary from GitHub and then tried to
install it with `sudo`, which cannot succeed non-interactively. The failure was logged as a
warning and nothing recorded that it happened, so the next create downloaded it again --
2.3-4.5 seconds added to every single agent creation, forever, plus a 1.36 MB file orphaned
in `/tmp` per attempt.

The plugin now decides whether the install can succeed *before* downloading anything, and does
the whole thing in one host command instead of two:

- macOS hosts no longer download at all. The upstream release ships Linux binaries only, so
  what was being fetched was a Linux ELF that macOS could never execute. The plugin now says
  so and points at `brew install ttyd`.

- `sudo` is only used when `/usr/local/bin` is not already writable, and only when it works
  without a password. Hosts that have neither are told why, and skip the download.

- Running as root now installs correctly. It previously could not: the shell command
  short-circuited on its own `id -u` test before reaching `curl`, so `ttyd` was never
  installed on a root host and the command always reported failure.

- The download is cleaned up when an install fails, instead of being left in `/tmp`.

On a macOS host without `ttyd`, this takes the per-create cost of the check from 4.5s to 0.1s.

Added a test that fails if the ttyd version pinned in mngr's Dockerfile drifts from the one the
plugin installs.

ttyd 1.7.7 is pinned in two places: `TTYD_VERSION` in the plugin, and the download URL in
`libs/mngr/imbue/mngr/resources/Dockerfile`, which preinstalls ttyd into the image. They cannot
be collapsed into a single pin -- mngr_modal ships each Dockerfile instruction to Modal as its
own `dockerfile_commands` call, so a build ARG expands to empty in the RUN that would use it,
which is why the neighbouring restic and offload pins are inline literals too.

The test asserts the two agree, so bumping one without the other fails instead of silently
shipping an image whose ttyd differs from the one agents install for themselves. This is the
same approach already used for `CLAUDE_CODE_VERSION`.

## 2026-09-01

The vendored ttyd web client no longer focuses its terminal on every websocket (re)connect. Upstream's unconditional focus is right for a standalone full-page terminal but wrong when the client is embedded as one pane among many: a background pane reconnect-looping against a dead tmux session stole the host app's focus several times a second. Focus is now granted only by the embedding host, via a new `{type: "ttyd-focus"}` window message the client honors (or by clicking the terminal, which xterm handles natively). Ships as a second build patch (`scripts/ttyd_host_focus.patch`) beside the clipboard one, applied and verified by `build_patched_ttyd_client.sh`.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-11

The forever-claude-template repo is being renamed to default-workspace-template (with the `fct`/`FCT` shorthand expanded to `default_workspace_template`/`DEFAULT_WORKSPACE_TEMPLATE` forms).

References in this project (comments, identifiers, docs) are mechanically updated by `scripts/rename_template_repo.py`.

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

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

# SIGWINCH repaint on every tmux attach (client-attached hook)

Tracks [issue #2322](https://github.com/imbue-ai/mngr/issues/2322).

## Overview

- Today the post-attach `SIGWINCH` "redraw nudge" is sent **only** from `mngr connect`'s own SSH attach wrapper (`sigwinch_step` in `_build_ssh_activity_wrapper_script`, `libs/mngr/imbue/mngr/api/connect.py`). A raw `tmux attach`, the ttyd agent terminal, or a web-shell `tmux attach` never receives it.
- When a client attaches at a different size than the agent's session (created at `200x50`, `window-size=latest`), the agent's TUI is resized but can be left with a stale, unpainted frame -- especially under minds' `alternate-screen off`. This visually corrupts the pane and also breaks message sending (the paste-visibility `capture-pane` fuzzy-match fails on a garbled pane).
- The fix: move the nudge into a persistent tmux **`client-attached` hook** on the agent's session, so it fires on *every* attach (including `mngr connect`'s own). Then remove the now-redundant `sigwinch_step` from the connect path.
- **Decided: per-session hook set at creation** (a persistent `client-attached[98]` on the agent's primary window in `_build_start_agent_shell_command`), rather than a global `set-hook -g` in the host tmux config. This matches the existing onboarding `client-attached[99]` pattern, bakes the session name in as a plain shell arg (no `#{hook_session}` resolution needed), and keeps the wiring local to session creation.
- The hook body must run a **shipped resource script** (`run-shell "bash <path> <session>"`) rather than an inline pipeline. This is required, not just convenient: tmux performs `#{...}` format-expansion on `run-shell` arguments, which would corrupt the script's `-F '#{pane_pid}'` and `-F '#I'` formats. Putting the loop in a `.sh` file keeps all `#{...}` out of any string tmux parses.
- The session name is passed to the script as a plain shell argument, so no `#{hook_session}` / `#{session_name}` interpolation is needed in the hook string (mirrors how the session name is already baked into `build_post_attach_sigwinch_script`).

## Expected behavior

- Attaching to an agent session via a plain `tmux attach` (not `mngr connect`) sends `SIGWINCH` to the agent process **and its children** and triggers a clean repaint.
- The ttyd agent terminal and any web-shell `tmux attach` get the same repaint, because the hook is on the session, not the connect path.
- `mngr connect` still produces a clean repaint -- now via the hook that fires on its attach, not via `sigwinch_step`.
- The nudge is a no-op (skipped) when the agent's primary window is pinned to `window-size manual`: such a window never resizes on attach, so there is nothing to repaint and the deliberately-fixed dimensions are left untouched. (Preserves the current guard in `sigwinch_step`.)
- `window-size` is left at tmux's default `latest` for unpinned sessions, so the window still resizes to match the client on attach and on every later terminal resize (unchanged from today).
- Only **newly created** agent sessions get the hook; existing running sessions are unaffected until recreated. (Same limitation as the existing onboarding hook.)
- No behavior change for the SSH activity tracker, signal-file handling, or retry logic in `connect_to_agent` -- only the SIGWINCH step is removed from the wrapper.

## Implementation plan

### New resource script

- `libs/mngr/imbue/mngr/resources/sigwinch_panes.sh`
  - Takes the session name as `$1`.
  - Body is the existing pipeline from `build_post_attach_sigwinch_script`, run by bash (so `#{pane_pid}` / `#I` are passed to `tmux ... -F`, never parsed by tmux's hook/run-shell layer):
    - `tmux list-windows -t "=$1" -F '#I'` -> for each window, `tmux list-panes -t "=$1:$W" -F '#{pane_pid}'` -> `xargs ... sh -c 'kill -WINCH {} $(pgrep -P {})'`.
  - Includes the **manual-pin guard**: if the primary window's `window-size` is `manual`, exit without signaling. (Primary window name passed as `$2`, matching how `sigwinch_step` reads `-wv window-size` on the named window rather than `:0`.)
  - Uses `=`-prefixed exact-match targets, consistent with `TmuxSessionTarget`/`TmuxWindowTarget`.
  - Ship it in the wheel: it lives under the already-packaged `imbue` tree (`imbue/mngr/resources/`), so no `pyproject.toml` change is needed.

### Single source of truth for the pipeline

- Keep `build_post_attach_sigwinch_script(session_name)` in `connect.py` as the canonical generator, OR make `sigwinch_panes.sh` canonical and delete the Python builder. Decision in Open questions.
  - Preferred: make `sigwinch_panes.sh` the canonical implementation, since the hook is now the only production caller. Keep a thin Python helper only if a test still needs to invoke the pipeline directly.

### Install the script on every host

- The script must be present at session-creation time for **all** host types (local, docker, ssh, modal), since attach happens on all of them.
- Mirror `activity_watcher.sh`: install to `<host_dir>/commands/sigwinch_panes.sh` (mode `0755`).
  - Reuse `install_packaged_script_on_host(host, module=mngr_resources, filename="sigwinch_panes.sh", dest=<host_dir>/commands/sigwinch_panes.sh)` (`libs/mngr/imbue/mngr/hosts/host.py:304`).
  - Add this to the host-level provisioning that already runs for every host (alongside / near `_ensure_shared_shell_libs`), so the file exists before `start_agents` builds the session.

### Set the hook at session creation

- `_build_start_agent_shell_command` (`libs/mngr/imbue/mngr/hosts/host.py:3576`):
  - After the primary window is created and named, append a step setting a **persistent** `client-attached[98]` hook (a different slot than the onboarding `[99]`, and without the `set-hook -u` self-removal):
    - `tmux set-hook -t <quoted_exact_agent_window> client-attached[98] <hook_value>`
    - where `hook_value = shlex.quote('run-shell "bash <commands_dir>/sigwinch_panes.sh <session> <primary_window_name>"')` (paths/names via the existing quoting helpers; `host_dir` is already a parameter).
  - Set it unconditionally (not gated on `onboarding_text`), so every agent session gets it.

### Remove the redundant connect-path nudge

- `_build_ssh_activity_wrapper_script` (`libs/mngr/imbue/mngr/api/connect.py:69`):
  - Delete the `sigwinch_step` construction (lines ~101-119) and its interpolation into the returned wrapper (line ~133).
  - Drop the now-unused `primary_window_name` parameter if nothing else uses it; update the single caller in `connect_to_agent` (`connect.py:325`) accordingly.

### Tests

- `libs/mngr/imbue/mngr/hosts/host_test.py`: add a test asserting `_build_start_agent_shell_command` output contains `set-hook`, `client-attached[98]`, and `sigwinch_panes.sh` (mirroring the onboarding-hook tests at lines 996-1056). Assert the hook is present even when `onboarding_text` is `None`.
- `libs/mngr/imbue/mngr/api/test_connect.py`: keep a `@pytest.mark.tmux` regression test that the SIGWINCH pipeline reaches the pane process (now exercising `sigwinch_panes.sh` against a SIGWINCH-catcher session, replacing/adapting `test_post_attach_sigwinch_delivers_to_pane_process`).
- Add a test asserting the connect wrapper **no longer** contains a SIGWINCH step (guards against accidental reintroduction of the double-nudge).
- Changelog: `libs/mngr/changelog/mngr-sigwinch.md`.

## Implementation phases

1. **Ship the script + helper.** Add `sigwinch_panes.sh` (with manual-pin guard). Decide canonical-source question; adjust `build_post_attach_sigwinch_script` accordingly. Unit-test the pipeline against a catcher session.
2. **Install on hosts.** Wire `install_packaged_script_on_host` into host provisioning so `<host_dir>/commands/sigwinch_panes.sh` exists for local/docker/ssh/modal hosts. System still works; hook not yet set.
3. **Set the hook.** Add the persistent `client-attached[98]` hook in `_build_start_agent_shell_command`. Now both the hook and the old `sigwinch_step` fire on `mngr connect` (harmless double-nudge). Add the host_test.
4. **Remove the redundant step.** Delete `sigwinch_step` from `_build_ssh_activity_wrapper_script` and clean up the parameter. Add the "no SIGWINCH in wrapper" test.
5. **Verify + changelog.** Manually verify a plain `tmux attach` repaints (tmux send-keys/capture-pane), confirm `mngr connect` still repaints, add changelog, run full suite.

## Testing strategy

- **Unit (string-shape):** `_build_start_agent_shell_command` includes the persistent hook targeting the primary window and invoking the script; wrapper no longer includes any SIGWINCH step.
- **Integration (`@pytest.mark.tmux`):** create a session whose pane traps WINCH and writes a marker; run `sigwinch_panes.sh <session> <window>`; assert the marker appears. Add a manual-pin case: with `window-size manual`, assert the marker does **not** appear (guard holds).
- **Manual verification (not crystallized):** start a local agent, `tmux attach` from a differently-sized terminal, use `capture-pane` before/after to confirm a clean repaint; repeat via `mngr connect`.
- **Edge cases:** base-index != 0 (target by window name, not `:0`); session names with shell-special characters (exact-match `=` + quoting); multiple panes/windows; agent that is a grandchild of the pane shell (covered by `pgrep -P`).

## Open questions

- **Canonical source of the pipeline.** Make `sigwinch_panes.sh` the single source and delete `build_post_attach_sigwinch_script`, or keep the Python builder and have the `.sh` be a thin wrapper that calls it? Avoid duplicating the pipeline in two places.
- **Fire-ordering / race.** `sigwinch_step` used `(sleep 3; ...)` to let the window resize before nudging. Does `client-attached` reliably fire *after* the resize so no delay is needed, or should the hook body keep a short backgrounded delay? Verify against tmux behavior; prefer no sleep if safe.
- **Manual-pin guard location.** Keep the `window-size == manual` guard inside `sigwinch_panes.sh` (recommended), or drop it and accept a harmless no-op repaint on pinned windows?
- **Existing sessions.** Setting the hook only at creation means running sessions are not retrofitted. Acceptable (matches onboarding hook), or do we want a one-time backfill for already-running agents?
- **Install path coverage.** Confirm a single provisioning call installs `sigwinch_panes.sh` for *all* host types (local, docker, ssh, modal); `activity_watcher.sh` is installed via `ssh_host_setup`/docker paths -- verify local hosts are covered too.

---

## Assumptions made (template selection was skipped)

- Used the **Default** plan template (Overview / Expected behavior / Implementation plan / Implementation phases / Testing strategy / Open questions).
- Chose the **per-session persistent hook + shipped resource script** approach (confirmed by the user; closest match to the existing onboarding `client-attached[99]` pattern) rather than the global-config hook.
- Treated this `/blueprint` invocation as "produce and commit a plan" given the standing instruction to make a best guess and proceed without blocking on questions.

# SIGWINCH repaint on every tmux attach (client-attached hook)

Tracks [issue #2322](https://github.com/imbue-ai/mngr/issues/2322).

## Overview

- Today the post-attach `SIGWINCH` "redraw nudge" is sent **only** from `mngr connect`'s SSH attach wrapper (`sigwinch_step` in `_build_ssh_activity_wrapper_script`, `libs/mngr/imbue/mngr/api/connect.py`). A raw `tmux attach`, the ttyd agent terminal, or a web-shell `tmux attach` never receives it.
- When a client attaches at a different size than the agent's session (created at `200x50`, `window-size=latest`), the agent's TUI is resized but can be left with a stale, unpainted frame -- badly so under minds' `alternate-screen off`. This visually corrupts the pane and also breaks message sending (the paste-visibility `capture-pane` fuzzy-match fails on a garbled pane).
- Fix: move the nudge into a persistent tmux **`client-attached` hook** on the agent's session, so it fires on *every* attach (including `mngr connect`'s own), then remove the now-redundant `sigwinch_step` from the connect path.
- The hook is set **per-session at creation** as a persistent `client-attached[98]` hook on the agent's primary window, mirroring the existing onboarding `client-attached[99]` hook (`libs/mngr/imbue/mngr/hosts/host.py`). The session name is baked in as a plain shell arg, so no `#{hook_session}` resolution is needed.
- The hook body runs a **shipped resource script** (`sigwinch_panes.sh`) via `run-shell`, not an inline pipeline. This is required, not just convenient: tmux performs `#{...}` format-expansion on `run-shell` arguments, which would corrupt the script's `-F '#{pane_pid}'` / `-F '#I'` formats. Keeping the loop in a `.sh` file keeps all `#{...}` out of any string tmux parses.

## Expected behavior

- Attaching to an agent session via a plain `tmux attach` (not `mngr connect`) sends `SIGWINCH` to the agent process **and its children** and triggers a clean repaint.
- The ttyd agent terminal and any web-shell `tmux attach` get the same repaint, because the hook is on the session, not the connect path.
- `mngr connect` still produces a clean repaint -- now via the hook that fires on its attach, not via `sigwinch_step`.
- The attach is **never blocked**: the hook body runs fully detached (`run-shell -b` / self-backgrounded), so the deliberate ~3s delay before signaling does not stall the attaching client.
- The nudge is skipped (no-op) when the agent's primary window is pinned to `window-size manual`: such a window never resizes on attach, so there is nothing to repaint and the fixed dimensions are left untouched.
- `window-size` stays at tmux's default `latest` for unpinned sessions, so the window still resizes to match the client on attach and on every later terminal resize (unchanged).
- Every pane's children across **all windows** in the session are signaled (preserves today's pipeline behavior); the `manual` guard is read from the primary window.
- Only **newly created** agent sessions get the hook. Agents already running at upgrade time get no repaint on attach (including `mngr connect`) until they are recreated -- accepted gap, with no fallback in the connect path.
- No change to the SSH activity tracker, signal-file handling, or retry logic in `connect_to_agent` -- only the SIGWINCH step is removed from the wrapper.

## Implementation plan

### New resource script -- `libs/mngr/imbue/mngr/resources/sigwinch_panes.sh`

- Single source of truth for the nudge. Arguments: `$1` = session name, `$2` = primary window name.
- Manual-pin guard first: if the primary window's `window-size` is `manual` (`tmux show-options -t "=$1:$2" -wv window-size`), exit 0 without signaling.
- Otherwise the ~3s delay then the existing pipeline (run by bash, so `#{pane_pid}` / `#I` go to `tmux ... -F` and are never seen by tmux's hook layer):
  - `tmux list-windows -t "=$1" -F '#I'` -> for each window `W`, `tmux list-panes -t "=$1:$W" -F '#{pane_pid}'` -> `xargs -I{} sh -c 'kill -WINCH {} $(pgrep -P {})' 2>/dev/null`.
- Uses `=`-prefixed exact-match targets, matching `TmuxSessionTarget` / `TmuxWindowTarget` semantics.
- Self-backgrounds the delay+signal so the script returns immediately (belt-and-suspenders with `run-shell -b`).
- Ships in the wheel automatically: lives under the already-packaged `imbue` tree (`imbue/mngr/resources/`), so no `pyproject.toml` change.

### Delete the Python pipeline builder

- Remove `build_post_attach_sigwinch_script` from `libs/mngr/imbue/mngr/api/connect.py` (the hook + `.sh` are now the only callers).
- Update `libs/mngr/imbue/mngr/api/test_connect.py`, which currently imports it, to exercise `sigwinch_panes.sh` instead.

### Install the script on every host

- Add `sigwinch_panes.sh` to the host-portable shared-libs provisioning so it lands at `<host_dir>/commands/sigwinch_panes.sh` (mode `0755`).
- `_ensure_shared_shell_libs` (`libs/mngr/imbue/mngr/hosts/host.py:2635`) already runs during agent provisioning for all online host types (local, docker, ssh, modal) and uses `self.write_file` (host-portable, handles the executable bit). Install the script there (host-level commands dir; it does not need the agent-level copy).

### Set the hook at session creation

- In `_build_start_agent_shell_command` (`libs/mngr/imbue/mngr/hosts/host.py:3576`), after the primary window is created/named, append a step that sets a **persistent** `client-attached[98]` hook on the agent's primary window (no `set-hook -u` self-removal):
  - `tmux set-hook -t <quoted_exact_agent_window> client-attached[98] <hook_value>`
  - `hook_value = 'run-shell -b "bash <commands_dir>/sigwinch_panes.sh <session> <primary_window_name>"'`, assembled with the existing quoting helpers (`host_dir` is already a parameter; the step is `shlex.quote`d exactly like the onboarding hook).
- Set unconditionally (not gated on `onboarding_text`), so every new agent session gets it. Slot `[98]` avoids colliding with onboarding `[99]`.

### Remove the redundant connect-path nudge

- In `_build_ssh_activity_wrapper_script` (`libs/mngr/imbue/mngr/api/connect.py:69`): delete the `sigwinch_step` construction and its interpolation into the returned wrapper.
- Drop the now-unused `primary_window_name` parameter if nothing else uses it, and update the single caller in `connect_to_agent` (`connect.py:325`).

### Docs and changelog

- `libs/mngr/changelog/mngr-sigwinch.md`: changelog entry framed primarily as a **behavior change** -- agents now repaint on every attach (not just `mngr connect`) -- noting the pane corruption / failed-send symptom it resolves.
- Brief user-facing note that agents repaint on every attach, added to a tmux/conventions concepts doc (e.g. `libs/mngr/docs/conventions.md` or the tmux concepts page).

## Implementation phases

1. **Ship the script.** Add `sigwinch_panes.sh` (manual-pin guard + 3s delay + all-windows pipeline, self-backgrounded). Add the `@pytest.mark.tmux` integration test that runs it against a SIGWINCH-catcher session.
2. **Delete the Python builder.** Remove `build_post_attach_sigwinch_script`; repoint `test_connect.py` at the `.sh`.
3. **Install on hosts.** Wire the script into `_ensure_shared_shell_libs` so `<host_dir>/commands/sigwinch_panes.sh` exists for all host types. System still works; hook not yet set.
4. **Set the hook.** Add the persistent `client-attached[98]` hook in `_build_start_agent_shell_command`. Both the hook and the old `sigwinch_step` now fire on `mngr connect` (harmless double-nudge). Add the unit string-shape test.
5. **Remove the redundant step.** Delete `sigwinch_step` from the connect wrapper and clean up the parameter. Add a test asserting the wrapper no longer contains a SIGWINCH step.
6. **Docs + changelog.** Add the conventions note and the `mngr` changelog entry. Run the full suite.

## Testing strategy

- **Unit (string-shape), `host_test.py`:** `_build_start_agent_shell_command` output contains `set-hook`, `client-attached[98]`, `run-shell`, and `sigwinch_panes.sh`, targeting the primary window -- present even when `onboarding_text` is `None`. (Mirrors the onboarding-hook tests at `host_test.py:996-1056`.)
- **Unit (string-shape), `test_connect.py`:** the connect wrapper no longer contains any SIGWINCH step (guards against reintroducing the double-nudge).
- **Integration, `@pytest.mark.tmux`:** create a session whose pane traps `WINCH` and writes a marker; run `sigwinch_panes.sh <session> <window>`; assert the marker appears. Add a `window-size manual` case asserting the marker does **not** appear (guard holds). No acceptance/release marks.
- **No manual verification step** -- rely on the automated tests above.
- **Edge cases:** base-index != 0 (target by window name, not `:0`); session names with shell-special characters (`=` exact-match + quoting); multiple windows/panes; agent that is a child (not the foreground group leader) of the pane shell (covered by `pgrep -P`).

## Open questions

- Confirm during implementation that tmux fires `client-attached` *after* the window has resized to the new client (the chosen 3s self-backgrounded delay is the safety margin; verify it is sufficient and that `run-shell -b` does not itself reorder relative to the resize).
- Pin the exact destination for the user-facing note (conventions doc vs the tmux concepts page) when writing it; either is acceptable.

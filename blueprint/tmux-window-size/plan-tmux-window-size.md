# Plan: configurable tmux window size for agents (and `mngr robinhood`)

## Overview

- Add per-session control over the inner agent's tmux window **width**, **height**, and **resize behavior**, set once at session creation and never affecting other tmux sessions/windows.
- Motivation: `mngr robinhood`'s approximate live streaming reverse-maps the agent's tmux pane, so the agent's pane width is baked into the streamed text as hard line wraps. A wide, fixed pane removes most of that wrapping. This complements (does not replace) the streaming dedup fix already on this branch.
- The tmux session is created by mngr **core** (`_build_start_agent_shell_command` in `libs/mngr/imbue/mngr/hosts/host.py`, currently hardcoded `-x 200 -y 50`), so the capability is added there and exposed through `CreateAgentOptions`, persisted on agent config, and read by the session builder on every start. It is therefore provider-agnostic (local/docker/modal/remote) and survives restart/clone/migrate/snapshot.
- New CLI surface: `--tmux-width`, `--tmux-height`, and `--tmux-window-size` on both `mngr create` and `mngr robinhood`. Defaults differ by entry point: mngr core keeps today's behavior (`200x50`, window-size `latest`); `mngr robinhood` defaults to `2048x256`, window-size `manual` for every invocation.
- Out of scope: the mngr-backed Agent SDK surface (already configurable via mngr config).

## Expected behavior

- `mngr create` (and all existing creation paths) behave exactly as today when the new flags are omitted: window created at `200x50`, window-size `latest` (resizes to the attached client on `mngr connect`).
- `mngr create --tmux-width N --tmux-height M` creates the agent's tmux window at `N`x`M`. With the default `latest` mode, this is only the creation/headless size; attaching interactively still resizes the window to the human's terminal.
- `--tmux-window-size manual|latest|largest|smallest` sets the session's tmux resize policy:
  - `latest` (default for `mngr create`): today's behavior.
  - `manual`: the window is pinned to the configured width/height and does not auto-resize to attached clients.
  - `largest` / `smallest`: passed through to tmux as-is (only meaningful with attached clients).
  - An invalid value is rejected with an error; `mngr robinhood` exits with code 2 (consistent with its other bad-flag handling).
- In `manual` mode, `mngr connect` additionally skips its post-attach `resize-window -A` step, so even an interactive attach leaves the window pinned. This only affects manually-created `manual` agents; `latest`/`largest`/`smallest` keep the current attach-resize behavior.
- `manual` with no width/height given pins to the resolved defaults (`200x50` for `mngr create`, `2048x256` for `mngr robinhood`); the size and resize-mode options are independent.
- `mngr robinhood` (any invocation, streaming or not) creates its agent at `2048x256`, `manual` by default, which removes most baked-in line wrapping from the streamed output. Users can override with `--tmux-width` / `--tmux-height` / `--tmux-window-size`. These flags are consumed by the wrapper and not forwarded to the underlying `claude`.
- Width/height accept any positive integer (values ≤ 0 are rejected); there is no upper cap.
- The configured size/mode persist on agent config, so `mngr stop`/`start` and `clone`/`migrate`/`snapshot` reuse them.

## Changes

- **mngr core — agent options & config (`libs/mngr`):**
  - Add tmux sizing fields (width, height, window-size mode), grouped under a `tmux` namespace, to `CreateAgentOptions` and to the persisted agent config that the session builder reads. Unset means "use the core default".
  - Add a `window-size` mode type with the values `manual` / `latest` / `largest` / `smallest`, plus positive-integer width/height domain types (validation lives in the types).
  - Update the tmux session builder (`_build_start_agent_shell_command`) to emit the configured width/height in `new-session -x/-y` (falling back to `200x50`) and to set the session's tmux `window-size` to the configured mode (falling back to today's behavior / `latest`).
- **mngr core — connect (`libs/mngr/.../api/connect.py`):**
  - When the agent's window-size mode is `manual`, skip the post-attach resize (`build_post_attach_resize_script` / `resize-window -A`) so the window stays pinned.
- **mngr core — `create` CLI (`libs/mngr/.../cli/create.py`):**
  - Add `--tmux-width`, `--tmux-height`, `--tmux-window-size` options that populate the new `CreateAgentOptions` tmux fields; defaults leave the fields unset so core defaults apply.
- **robinhood — arg parsing & wiring (`libs/mngr_robinhood`):**
  - Add `--tmux-width`, `--tmux-height`, `--tmux-window-size` as wrapper-consumed value flags in `arg_partition.py`, with robinhood defaults `2048` / `256` / `manual`; carry them on `ArgPartition` (`data_types.py`); reject invalid values (exit 2).
  - In `orchestrator.py`, pass the resolved tmux fields into the `CreateAgentOptions` it builds for the spawned agent.
- **Tests:**
  - mngr core: session-builder emits correct `-x/-y` and `window-size`; defaults preserved when unset; `manual` suppresses the connect resize; width/height validation rejects ≤ 0 and the mode rejects invalid values.
  - robinhood: arg partition parses the three flags (and `--flag=value` form), applies the `2048x256`/`manual` defaults, rejects bad values, and threads the values into `CreateAgentOptions`.
- **Docs & changelog:**
  - Update `libs/mngr` and `libs/mngr_robinhood` READMEs to document the new flags and defaults.
  - Add per-PR changelog entries under both `libs/mngr/changelog/` and `libs/mngr_robinhood/changelog/`.

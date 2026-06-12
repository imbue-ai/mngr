# Plan: single discovery observer for the minds app

## Refined prompt

Consolidate discovery observation so only a single `mngr observe --discovery-only` runs: the one spawned by `mngr latchkey forward`. The `mngr forward` launched directly by the minds app should stop spawning its own observe and instead tail the discovery events file. This closes the loop on the bug worked around in PRs #1885 and #1888.

* Add a boolean `--observe-via-file` flag to `mngr forward`: skip spawning the `mngr observe` subprocess and instead tail the standard discovery events file (`<host_dir>/events/mngr/discovery/events.jsonl`); per-agent `mngr event ... --follow` subprocesses are unchanged. Mutually exclusive with `--no-observe`; allowed with both `--service` and `--forward-port`. The SIGHUP handler stays installed but is a no-op (debug log) in this mode.
* Implement the tailing in-process (no subprocess), reusing the emit-cached-snapshot + tail phases of `run_discovery_stream` (skipping its poll/write phases). On startup, emit the latest cached snapshot immediately; the live observer's first fresh snapshot corrects it shortly after. Handle the file being absent at startup (wait/poll for it to appear) and being rotated/truncated (reset offset and re-read).
* Revert latchkey's private `--events-dir` isolation: `mngr latchkey forward`'s observe writes to the standard discovery log again, removing `DiscoveryStreamConsumer.events_dir` and the `discovery-observe` dir + rmtree logic. Old on-disk `discovery-observe` dirs are left in place.
* Fully revert the PR-1885 mngr-side machinery: remove `MngrConfig.events_base_dir_override` and the observe-side override plumbing. `mngr observe --events-dir` still relocates the *full* observer's agent-state events, but combining `--events-dir` with `--discovery-only` is now a `click.UsageError`.
* Keep `--discovery-only` unlocked (no observe-lock enforcement).
* minds passes `--observe-via-file` in `start_mngr_forward` and removes its now-redundant SIGHUP bounce of `mngr forward`; provider changes refresh discovery solely via `LatchkeyForwardSupervisor.bounce()`.

## Overview

* Today two `mngr observe --discovery-only` processes run under minds: one spawned by `mngr latchkey forward` (for reverse tunnels) and one spawned by minds' own `mngr forward --service system_interface` (for web forwarding). PRs #1885/#1888 papered over the resulting cross-talk by isolating them onto separate event logs and adding provider-error retention; this plan removes the duplication outright so there is a single source of truth for "which hosts/agents exist".
* `mngr latchkey forward`'s observe becomes the sole discovery writer, writing to the **standard** discovery log (`<host_dir>/events/mngr/discovery/events.jsonl`). minds' `mngr forward` becomes a pure **reader** of that same log.
* New boolean `--observe-via-file` flag on `mngr forward` switches it from spawning `mngr observe` to tailing the discovery log in-process, reusing the existing snapshot+tail logic from `run_discovery_stream`. Per-agent `mngr event` streams are untouched.
* The per-env `--events-dir` isolation from PR #1885 (and its underlying `MngrConfig.events_base_dir_override`) is fully reverted: with one observer there is nothing to isolate from, so the extra plumbing is dead weight.
* Because both processes inherit the same environment (the detached latchkey forward copies minds' `os.environ`, and minds passes the same `MNGR_HOST_DIR` to `mngr forward`), both resolve the identical default discovery path with no shared path helper.

## Expected behavior

* Exactly one `mngr observe --discovery-only` process runs in a minds session (the one under `mngr latchkey forward`), instead of two.
* minds' workspace list, host/agent discovery, and system_interface forwarding behave identically to today from the user's perspective — the data now comes from tailing the shared log rather than from a private observe subprocess.
* On startup, minds' `mngr forward` populates instantly from the latest cached snapshot on disk, then is corrected by the live observer's first fresh snapshot seconds later (same fast-path behavior as `run_discovery_stream` today).
* If the discovery log does not exist yet when `mngr forward` starts (fresh machine, or latchkey forward still coming up), `mngr forward` waits for it to appear and starts forwarding once agents are discovered; it still binds its port and emits `listening` immediately (no regression to the startup handshake).
* If the discovery log is rotated/truncated while `mngr forward` is tailing it, the tailer recovers (re-reads from the new file) rather than stalling.
* Provider changes (enable/disable, sign-in/out, OAuth) refresh discovery solely by bouncing the latchkey supervisor's observe via SIGHUP; the new fresh snapshot is picked up automatically by the tailer. minds no longer SIGHUPs `mngr forward`.
* `mngr latchkey forward` again writes discovery events to the standard log; provider-error retention (PR #1888) still applies on the consumer side. Stale `discovery-observe/` directories from older versions remain on disk, inert.
* `mngr observe --discovery-only --events-dir X` is now a usage error (the flag never affected the discovery log in this mode; failing loudly avoids the pre-#1885 silent-ignore footgun). `mngr observe --events-dir X` (full observer) is unchanged.
* `mngr forward --observe-via-file` together with `--no-observe` is a usage error; `--observe-via-file` works with either `--service` or `--forward-port`.
* A user manually running their own `mngr observe --discovery-only` alongside minds is still permitted (no locking); snapshots written by either remain consistent and dedup handles overlap.

## Changes

### `libs/mngr_forward`
* Add an `--observe-via-file` boolean flag and corresponding `ForwardCliOptions` field to `mngr forward`.
* Validate flag combinations: reject `--observe-via-file` with `--no-observe`; allow it with `--service` or `--forward-port`.
* When `--observe-via-file` is set, drive discovery by tailing the standard discovery log in-process instead of constructing the observe-spawning `ForwardStreamManager`; per-agent `mngr event` streams still start for discovered agents.
* Extend `ForwardStreamManager` with a "tail file" discovery mode (alongside its existing subprocess mode) that feeds tailed lines into the same discovery-event handling path it already uses.
* Make the SIGHUP handler a no-op (debug log) when in tail-file mode, since there is no observe child to bounce or snapshot to re-take.
* Update `mngr forward` help/metadata to document the new flag and its relationship to `--no-observe`.

### `libs/mngr` (revert PR #1885 mngr-side machinery + add shared tailer)
* Add a public tail-only discovery helper in `mngr/api/discovery_events.py` (emit latest cached snapshot, then tail the file for appended lines, with file-absence wait and rotation/truncation recovery), and refactor `run_discovery_stream` to reuse it for its snapshot+tail phases.
* Remove `MngrConfig.events_base_dir_override` and all plumbing that reads/merges/loads it (`config/data_types.py`, `config/loader.py`).
* Simplify `get_discovery_events_dir` / `get_discovery_events_path` to derive solely from `default_host_dir`.
* Remove the `events_base_dir_override` threading in `cli/observe.py`; make `--events-dir` + `--discovery-only` a `click.UsageError`; `--events-dir` continues to relocate the full observer's agent-state events and lock.
* Update affected unit tests (`config/*_test.py`, `api/discovery_events_test.py`) for the removed field and the new usage error.

### `libs/mngr_latchkey`
* Remove `DiscoveryStreamConsumer.events_dir` and the `--events-dir` argument it added to the spawned `mngr observe` command; the consumer now spawns plain `mngr observe --discovery-only --quiet`.
* Remove the `discovery-observe` directory construction and `shutil.rmtree` cleanup in `cli.py`'s `_forward_command` (and the now-unused `shutil` import).
* Update the comment/docstrings that explained the isolation rationale.

### `apps/minds`
* Pass `--observe-via-file` from `start_mngr_forward` / `ForwardSubprocessConfig` so minds' `mngr forward` tails the shared discovery log instead of spawning its own observe.
* Remove minds' now-redundant SIGHUP bounce of `mngr forward`: drop `EnvelopeStreamConsumer.bounce_observe` and its call sites (`desktop_client/app.py`, `desktop_client/supertokens_routes.py`), so provider-change handlers bounce only the latchkey supervisor.
* Update related comments/docstrings that describe the old dual-observe wiring.

### Tests
* Unit-test the extracted tail-only helper: cached-snapshot emission, tailing appended lines, dedup, file-absence wait, and rotation/truncation recovery.
* Integration-test that `mngr forward --observe-via-file` picks up agents from a discovery log written by a separate process and spawns no `mngr observe` subprocess.
* Adjust existing latchkey/forward/minds tests that referenced `events_dir`, `events_base_dir_override`, the `discovery-observe` path, or `bounce_observe`.

### Changelog
* Add a per-PR changelog entry under each touched project: `libs/mngr/changelog/`, `libs/mngr_forward/changelog/`, `libs/mngr_latchkey/changelog/`, and `apps/minds/changelog/` (filename = branch name with slashes as dashes).

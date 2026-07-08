# Changelog - mngr_mapreduce

A concise, human-friendly summary of changes for the `mngr_mapreduce` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `mngr_mapreduce` framework, generalizing the test-fanout pattern previously baked into `mngr_tmr`. Recipes subclass `MapReduceRecipe` to plug in discovery, per-task prompts, the reducer prompt, and post-extraction hooks (`on_mapper_finalized`, `on_reducer_finalized`). The framework handles agent launching (with snapshot/host-pool support), polling, outputs-archive extraction, and report rendering/upload; it treats each agent's `outputs.tar.gz` as opaque.

### Changed

- Changed: `stop_agent_on_host` tolerates the `CleanupFailedGroup` that `Host.stop_agents` now raises when cleanup leaves a resource behind, so a best-effort stop in a `finally` logs and continues instead of masking the real result.
- Changed: The reducer agent now benefits from the same snapshot-based code-reuse optimization as mappers — when the run uses a snapshot, the reducer's host is pre-created so the agent's source is git-worktreed off the snapshot's `/code` instead of re-uploaded from the laptop.

### Fixed

- Fixed: Post-finalize `stop_agent_on_host` calls in `launch_and_poll_mappers` and `wait_for_reducer` now run on a new `AgentStopper` (in `agent_stopper.py`): a context-manager helper that spawns an `ObservableThread` per stop and drains in-flight stops for up to 60s on exit, instead of running synchronously on the polling loop's main thread. SSH `stop_agents` against an already-torn-down sandbox can block on the kernel's TCP retransmit (~16 minutes per call); the previous synchronous code path serialized the polling loop on those waits, leaving ~50 of 80 mappers unfinalized at the 4h GHA cap.
- Fixed: `sanitize_for_agent_name` now also strips trailing hyphens after the 40-char truncation, not just before it — previously a task slug whose 40th character was a hyphen (e.g. `test_create_modal_idle_mode_ssh_timeout_300`) produced an `AgentName`-invalid result.

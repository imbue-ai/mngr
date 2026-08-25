Robustness fixes from the 2026-07-14 partial workspace-destroy incident:

- State-store record writes are now atomic (temp file + rename). The `Volume.write_files` contract requires per-file atomic visibility, implemented in `LocalVolume` (via a new `atomic_write_bytes` helper) and `DockerVolume` (tar under temp names + a rename exec), so a concurrent reader can never observe a torn (empty / partial) host record -- the incident's root cause.

- Host-record reads now distinguish "unreadable" from "absent". Parse failures are retried (a torn read heals on re-read); after retries the default behavior still warns and treats the record as missing, while the new off-by-default `strict_host_record_parsing` config flag makes them raise `HostRecordUnreadableError` instead, which docker discovery deliberately re-raises rather than silently dropping the live container or degrading it to stale offline data.

- Discovery now heals the persisted agent store from each successful live agent listing (for providers that persist agent data: docker, lima). Agents created inside the host -- e.g. by an in-container bootstrap running `mngr create` -- previously never reached the store, so offline listings structurally missed them. Missing live agents are persisted and orphaned records removed; deletions are guarded by a non-blocking host-lock probe plus a post-probe re-read of the live listing, and a per-host cache keeps steady-state discovery read-only.

- The "Host {} still has {} agent(s) after destroy; leaving host alive" signal in `mngr destroy` is now logged at warning (was debug) and names the remaining agents -- in the incident it was the clearest evidence that a teardown had been left incomplete.

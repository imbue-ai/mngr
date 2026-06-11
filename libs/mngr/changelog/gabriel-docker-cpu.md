Stop `mngr event --follow` from polling the persisted files of an offline (stopped-but-not-destroyed) agent every second.

Previously, a follower whose target host was offline kept re-reading the agent's unchanging event files once per second through the read-only volume. For the Docker provider each such read is a separate `docker exec` into the shared state container, so a handful of stopped agents could drive the Docker engine to a large, wasted CPU load (observed: ~30 `docker exec`/sec into one state container, pushing it to ~90% CPU) even though no agent work was happening.

The follow loop already tracks online/offline status and re-checks it every 30s. It now:

- does not start per-source tail threads while the target is offline (a stopped agent cannot write events),

- skips the periodic source-directory rescan while offline, and

- on a live online -> offline transition, tears down the tail threads and leaves them down instead of restarting them against the volume.

When the host returns to RUNNING the loop resumes tailing automatically; existing event-id deduplication ensures nothing is emitted twice on resume. This affects every consumer of `mngr event --follow` (the CLI, `mngr forward`, and the minds desktop app), not just one.

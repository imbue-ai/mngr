The machines list now shows honest transitional states for cloud workspaces (imbue-ai/mngr-internal#547): a workspace whose stop upload is still in flight renders as "Stopping…" (and a backend-observed start as "Starting…") instead of an already-startable "Stopped" row whose Start button the server would refuse. Backend-observed STOPPING/STARTING host states flow through `MindLiveness` to the existing frontend badges, and the recovery card treats a mid-stop host as expectedly offline.

The ci and dev tiers' `deploy.toml` now declare a short workspace stop/start retention window (`[storage] stop_retention_seconds`: 60s on ci, 300s on dev), so the retention finalize — and the tests that wait for it — completes in minutes on those tiers instead of the default hour.

The workspace stop/start deploy doc now notes that box prep provides `zstd` (from apt) in addition to the pinned `age` + `s5cmd` transfer tooling.

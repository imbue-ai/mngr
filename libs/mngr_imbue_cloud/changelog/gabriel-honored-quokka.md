Imbue Cloud workspaces created before the container host_dir moved to `/home/user/.mngr` are readable again from a client configured for the new layout.

A container is baked with one host_dir layout and keeps it for life, but the provider config is account-wide, so a current-layout client addressed every older workspace at a directory it has never had. Those workspaces showed as `UNKNOWN` with "container is running on outer host but its inner data was unreadable", listed a stale set of agents, and failed `mngr exec` and `mngr start` with "Agent not found on host" -- all while the container was up and reachable.

- Discovery now probes the configured host_dir first and falls back to the other layouts mngr has shipped, picking whichever actually holds the host record. This runs both ways: a client resolving `/mngr` (still the `ImbueCloudProviderConfig` default -- only a minds-authored account block names `/home/user/.mngr`) can read a new-layout workspace too.

- Leasing a pool host uses the same candidate list when it rewrites the baked placeholder host name, and now records the layout it found. Previously that answer was discarded, so a fresh lease -- which has no discovery pass behind it -- built its host at the configured layout. On a mismatched client the pre-baked agent's `data.json` then read as missing and the adopt fast path silently fell back to a full create against a container that was already provisioned.

- The resolved location is recorded per host and reused for later operations, so `exec`, `start` and the minds SSH broker address the same directory discovery just read rather than the account-wide default.

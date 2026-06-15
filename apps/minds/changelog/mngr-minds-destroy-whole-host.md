Destroying a workspace now reliably tears down its entire host, fixing a bug where a destroy could report success in the UI while the underlying cloud instance kept running (and billing).

- Destroy always tears down the whole host (the workspace agent plus the per-host `system-services` agent), so the cloud instance is actually terminated. The previous single-agent fallback -- which could remove only the workspace agent and leave the host alive -- has been removed.

- The destroy no longer shells out to a slow `mngr list` to find the workspace's host: the host id is immutable and already known from in-memory discovery. If the host genuinely can't be determined, the destroy is refused with a clear error instead of doing a partial teardown.

- A workspace now stays visible (as "destroying", then "failed" if teardown didn't finish) until its host is confirmed gone, and is only removed from your account at that point. A failed or partial destroy no longer silently vanishes from the UI while the host keeps running.

- The AWS region picker now offers only the US datacenters (`us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`) by default. Each configured region adds a provider that `mngr list` queries every discovery cycle, and the non-US regions roughly doubled listing latency for little benefit.

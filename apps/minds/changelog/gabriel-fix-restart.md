Fixed a slow cold-start / wake-from-sleep load where opening a perfectly healthy imbue-cloud workspace would sit on "Loading workspace…" for tens of seconds and then needlessly restart the workspace's system services.

On a cold start the freshly-spawned `mngr forward` has not yet resolved the workspace's route, so probes fail (`UNRESOLVED` / 503) for the ~10s it takes discovery to warm up. That warm-up was being mistaken for an outage: the workspace was marked STUCK and the recovery flow auto-restarted it.

Two fixes:

- Suspect enrollment now ignores an `UNRESOLVED` backend failure outright. `UNRESOLVED` means the forward has no route for the agent at all -- a cold-start warm-up (which self-resolves) or a genuinely-gone agent (which a restart cannot revive) -- and a restart routes through the forward either way. A workspace that is present but unreachable does not land here: discovery retains its route, so the dial failure surfaces as `CONNECT_ERROR` / a 5xx, which still enrolls and still drives recovery.

- The recovery page's auto-dispatched restart now no-ops if the workspace has already recovered to HEALTHY by the time it fires (the host-health probe is slow, so the background probe loop can flip the workspace healthy while it is in flight). A manual restart is unaffected and always proceeds.

Also added diagnostic logging across the system-interface health / recovery path (backend-failure envelopes, suspect enrollment, probe results, the HEALTHY → STUCK transition with its failure-run duration, the recovery → HEALTHY transition, the STUCK redirect emission, and restart dispatch), and removed a stale recovery-gate FIXME now that the discovery-health watchdog backstops a persistently-stalled pipeline with its BLOCKED app-takeover.

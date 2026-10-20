Multi-relay sharing, phase 1 (blueprint/multi-relay), minds side.

`share.env` no longer carries a relay endpoint: the workspace's share gateway fetches its relay set from the connector's assignment endpoint, so server-side fleet changes never require re-injecting materials.

The latency-based relay region picker scores each region by its best endpoint (regions now expose several).

`minds env deploy` stops pinning `SHARE_DEFAULT_REGION` / `SHARE_RELAY_ENDPOINTS` into dev/ci sharing secrets -- the connector's `relays` table is the fleet's source of truth, and `just provision-dev-relay` now registers the relay it provisions.

New deployment test (`test_relay_fleet.py`): fleet registration + health, the share assignment contract, and relay failover on multi-relay regions (skipped on single-relay dev/ci envs).

Staging bring-up / next-deploy docs describe the two-relay-per-region flow.

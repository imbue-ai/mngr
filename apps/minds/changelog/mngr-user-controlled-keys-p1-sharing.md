Desktop-driven workspace sharing is now client-side for every row (Phase 1 of the user-controlled-keys plan, `blueprint/user-controlled-keys/`): enabling sharing on an imbue_cloud workspace uses the same connector `shares create` + materials-injection-over-the-user's-own-SSH path that local (docker/lima) workspaces already used, instead of the connector's server-side enable-sharing primitive. That server-side primitive now serves only web-created workspaces, which have no desktop client to inject from.

Cloud rows skip the relay-region latency measurement (the desktop's latency says nothing about the pool host's); the connector applies its default region for them.

Caveat during rollout: an older minds version enabling sharing on a workspace whose keys were later adopted (a future phase) would still call the server-side primitive and fail; upgrading minds resolves it.

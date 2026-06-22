An unauthenticated OVH provider now errors instead of silently reporting zero agents.

Previously, when no OVH credentials were resolvable anywhere (config, `OVH_*` env vars, `~/.ovh.conf`), the provider silently returned an empty listing (exit 0). It now raises the shared `ProviderNotAuthorizedError` at construction, so an enabled-but-unauthenticated OVH provider is reported consistently with the other cloud providers (one consistent error line in `mngr list`, contributing a non-zero exit) rather than vanishing.

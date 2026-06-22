An unauthenticated Vultr provider now errors instead of silently reporting zero agents.

Previously, with no API key configured, the Vultr provider printed an ad-hoc `WARNING: Vultr API key not configured, skipping VPS discovery` and returned an empty listing (exit 0). It now raises the shared `ProviderNotAuthorizedError` at construction, so an enabled-but-unauthenticated Vultr provider is reported consistently with the other cloud providers (one consistent error line in `mngr list`, contributing a non-zero exit) rather than vanishing. The bespoke warning print has been removed.

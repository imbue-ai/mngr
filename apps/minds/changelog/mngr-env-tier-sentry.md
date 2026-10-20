Every tier's `[secrets].services` now includes `sentry`: the error-reporting DSNs the remote services (the connector and the LiteLLM proxy) initialize sentry-sdk with, pushed from Vault as the stamped `sentry-<tier>-<deploy-id>` Modal Secret on each deploy. All-empty values are fine and simply leave reporting disabled, so tiers bring up in any order.

The `sentry-sdk` floor moves to 2.63.0 (the version that fixes the per-request FastAPI sync-endpoint wrapper leak, mngr-internal#493).

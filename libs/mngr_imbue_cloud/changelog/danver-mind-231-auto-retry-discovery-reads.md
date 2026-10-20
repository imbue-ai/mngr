Made the Imbue Cloud discovery reads (the `GET /hosts` and `GET /workspaces` listings behind `mngr list` and `mngr create`) automatically retry transient transport failures with bounded, jittered exponential backoff, instead of immediately failing the whole operation with "could not reach Imbue Cloud" / a "Could not create workspace" card (MIND-231; this error class hit 34+ distinct users in 90 days).

- The two listing calls are now routed through the connector client's existing transient-transport retry (`_send`), which rides out DNS failures, connect/read timeouts, and connection resets against the scale-to-zero connector: up to 3 attempts, exponential backoff (now with equal jitter), and a new 60-second total wall-clock cap that applies to all `_send`-routed connector calls.

- Only transient transport errors are retried. Auth failures (`ImbueCloudAuthError`), connector status errors (`ImbueCloudConnectorError`), and the old-connector `/workspaces` 404 fallback signal still fail fast with their types unchanged, and the token-refresh POST keeps its stricter connect-phase-only retry (a consumed refresh token must never be re-sent).

- After the retry budget is exhausted, the listings raise the new typed `ImbueCloudUnreachableError` (a subclass of `ImbueCloudConnectorError`), which the provider maps to the same `ProviderUnavailableError` and user-facing guidance as before -- and which gives the `mngr imbue_cloud hosts` CLI paths a clean error message instead of a raw httpx traceback.

- The retry policy is now implemented with tenacity (previously a hand-rolled loop), and tenacity is declared as a direct dependency of the plugin (it was already imported via a transitive dependency).

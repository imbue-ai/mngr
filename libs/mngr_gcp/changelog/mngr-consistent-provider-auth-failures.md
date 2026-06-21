Report an unauthenticated GCP provider consistently with the other cloud providers.

A missing/unresolvable ADC credential or project now raises the shared `ProviderNotAuthorizedError` (still a `ProviderUnavailableError`, so read paths treat it as unavailable). In `mngr list` this surfaces as one consistent error line and a non-zero exit.

ADC resolution is now bounded by the new `credential_timeout_seconds` setting (default 10s): `google.auth.default()` probes the GCE metadata server as a credential/project source, which can hang on a non-GCE host -- the timeout turns that into a fast, clear "not authenticated" failure.

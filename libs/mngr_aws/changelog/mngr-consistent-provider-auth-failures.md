Report an unauthenticated AWS provider consistently with the other cloud providers.

A missing/unresolvable AWS session now raises the shared `ProviderNotAuthorizedError` (still a `ProviderUnavailableError`, so read paths treat it as unavailable). In `mngr list` this surfaces as one consistent error line and a non-zero exit, instead of a one-off message.

Credential resolution is now bounded by the new `credential_timeout_seconds` setting (default 10s): boto3's default chain probes the EC2 instance metadata service (IMDS) when no other source resolves, which can hang on a non-AWS host -- the timeout turns that into a fast, clear "not authenticated" failure.

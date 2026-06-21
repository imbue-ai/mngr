Report an unauthenticated Azure provider consistently with the other cloud providers, and validate credentials eagerly.

A missing subscription or unusable credential now raises the shared `ProviderNotAuthorizedError` (still a `ProviderUnavailableError`, so read paths treat it as unavailable). In `mngr list` this surfaces as one consistent error line and a non-zero exit.

Azure previously validated only the subscription id: `DefaultAzureCredential` authenticates lazily, so an unauthenticated environment surfaced as a confusing API error on the first real call. The provider now eagerly requests a management-scope token at construction so the failure is reported up front. This resolution is bounded by the new `credential_timeout_seconds` setting (default 10s), since the credential probe may walk the IMDS managed-identity endpoint and hang on a non-Azure host.

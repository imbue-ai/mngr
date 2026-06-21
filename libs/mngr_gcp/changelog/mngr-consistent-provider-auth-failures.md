Report an unauthenticated GCP provider consistently with the other cloud providers.

A missing/unresolvable ADC credential or project now raises the shared `ProviderNotAuthorizedError` (still a `ProviderUnavailableError`, so read paths treat it as unavailable). In `mngr list` this surfaces as one consistent error line and a non-zero exit.

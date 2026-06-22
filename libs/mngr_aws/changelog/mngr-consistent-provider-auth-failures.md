Report an unauthenticated AWS provider consistently with the other cloud providers.

A missing/unresolvable AWS session now raises the shared `ProviderNotAuthorizedError` (still a `ProviderUnavailableError`, so read paths treat it as unavailable). In `mngr list` this surfaces as one consistent error line and a non-zero exit, instead of a one-off message.

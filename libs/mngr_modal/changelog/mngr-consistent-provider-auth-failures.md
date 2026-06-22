Report an unauthenticated Modal provider consistently with the other cloud providers.

A missing/invalid Modal token now raises the shared `ProviderNotAuthorizedError` from provider construction, and `ModalAuthError` is now a subclass of `ProviderNotAuthorizedError` (preserving its existing message and remediation). As a result, Modal auth failures are categorized the same way as the other cloud providers in `mngr list` -- one consistent error line and the granular provider-inaccessible exit code -- instead of an ad-hoc plugin error.

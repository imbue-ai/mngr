Hardened the OAuth sign-in flow against CSRF and authorization-code interception (CASA 3.2.1/3.2.2):

- `/auth/oauth/authorize` now accepts an optional `state` and reflects it into the provider authorize URL's query string, so the provider echoes it back on the callback for the client to verify. It also returns the provider's `pkce_code_verifier` (when one is minted) so the stateless connector can carry it back on the callback.

- `/auth/oauth/callback` now threads the client-returned `pkce_code_verifier` into the SuperTokens token exchange instead of hardcoding `None`.

- Enabled PKCE (`force_pkce=True`) for the Google provider, which supports and recommends PKCE alongside a confidential client secret. GitHub is intentionally left unchanged because GitHub OAuth Apps do not officially support PKCE; enabling it there would add no real protection while risking the flow.

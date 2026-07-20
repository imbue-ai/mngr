Hardened `mngr imbue_cloud auth oauth` against CSRF and authorization-code interception (CASA 3.2.1/3.2.2):

- The CLI now generates a cryptographically-random `state` for each browser sign-in and injects it into the authorize URL itself before opening the browser, then verifies (in constant time) that the provider echoed the exact value back on the callback before the code is ever exchanged. A missing or mismatched state aborts the flow. Owning the state entirely on the client side means sign-in keeps working against any connector version (no lockstep deploy required).

- The CLI now carries the PKCE `pkce_code_verifier` the connector returns from the authorize step (held in memory only, never logged) and passes it back on the callback so the token exchange is bound to this session's verifier. A connector that mints no verifier is handled transparently (the value stays `None`).

- The connector client's `auth_oauth_callback` gained a `pkce_code_verifier` argument. The `mngr imbue_cloud auth oauth <provider>` command signature is unchanged.

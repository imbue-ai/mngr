Hardened `mngr imbue_cloud auth oauth` against CSRF and authorization-code interception (CASA 3.2.1/3.2.2):

- The CLI now generates a cryptographically-random `state` for each browser sign-in, has the connector embed it in the authorize URL, and verifies (in constant time) that the provider echoed the exact value back on the callback before the code is ever exchanged. A missing or mismatched state aborts the flow.

- The CLI now carries the PKCE `pkce_code_verifier` the connector returns from the authorize step (held in memory only, never logged) and passes it back on the callback so the token exchange is bound to this session's verifier.

- The connector client's `auth_oauth_authorize` gained a required `state` argument, and `auth_oauth_callback` gained a `pkce_code_verifier` argument. The `mngr imbue_cloud auth oauth <provider>` command signature is unchanged.

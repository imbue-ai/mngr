Notion MCP sign-ins now return through the Minds-hosted OAuth callback page instead of latchkey's loopback callback.

Before any `notion-mcp` browser sign-in that registers a new OAuth client (a first sign-in, or "Add account"), `Latchkey.auth_browser` runs `latchkey auth prepare notion-mcp '{"redirectUri": "https://imbue-ai.github.io/oauth-callback/"}'` (latchkey >= 3.15) so the dynamically registered client is bound to that page, which forwards the authorization result to the loopback port latchkey encodes in `state`. A failed pin fails the sign-in with latchkey's own reason rather than falling back to a loopback sign-in. Re-signing in to a stored account (`--account`) is unchanged: latchkey reuses that account's own client and redirect URI.

The redirect-URI services live in `MINDS_OAUTH_REDIRECT_URI_BY_SERVICE` next to the Minds Google OAuth client preference, and `Latchkey.auth_prepare_redirect_uri` is the new counterpart of `auth_prepare`.

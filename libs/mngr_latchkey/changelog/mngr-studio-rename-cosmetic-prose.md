Comment and docstring prose that referred to the desktop app as "Minds" now
says "Imbue Studio": the gateway's Google OAuth notes (the OAuth client the app
provides and its consent screen) and the tests covering them, the discovery
module's tunnel notes, the WebDAV and permission-request extension docs, the
default-permissions docstring, the machine-store re-export note, the
cross-workspace verb metadata, the services-catalog generator's docstring, and
the account-scope prose in both `account_scopes.py` and `README.md`.

No behavior change. `minds-api-proxy`, `minds-workspaces` and
`MINDS_GOOGLE_OAUTH_SERVICES` are identifiers and keep their `minds` spelling
for good, per `apps/minds/style_guide.md`. The one name still to move is the
"Minds API" product name, which is an agent-facing wire path and needs a compat
alias, so it changes separately.

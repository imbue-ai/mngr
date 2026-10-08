Comment and docstring prose that referred to the desktop app as "Minds" now
says "Imbue Studio" -- the gateway's Google OAuth notes (the app-provided OAuth
client and its consent screen), the WebDAV and permission-request extension
docs, the default-permissions docstring, and the cross-workspace verb metadata.

No behavior change: every identifier keeps its spelling, per
`apps/minds/style_guide.md`. The `minds-api-proxy` extension, the
`minds-workspaces` API name, `MINDS_GOOGLE_OAUTH_SERVICES`, and the "Minds API"
product name are all untouched and are being handled separately.

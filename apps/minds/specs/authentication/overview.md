# Authentication

Component: the minds desktop client -- the bare-origin web UI served by
`minds run` (`apps/minds/imbue/minds/desktop_client/`) -- plus the
workspace-origin bridge served by the forward server (`libs/mngr_forward/`).

The features in this folder cover sign-in with a one-time login code, the
session that sign-in establishes, landing-page routing, the post-sign-in
destination, and the bridge that opens every workspace from one sign-in.
The Rules in `invariants.feature` bind all of them.

## Glossary

- **desktop client**: the local server started by `minds run`; the gateway
  through which the user reaches all their workspaces.
- **data directory**: the desktop client's local state directory. One
  installation = one data directory.
- **one-time code**: a secret minted at server start. The server prints a
  **login URL** (`http://localhost:<port>/login?one_time_code=<code>`) to its
  terminal; this is the only credential a user ever types or clicks.
- **session**: the signed-in state of a browser, established by a successful
  sign-in and carried by a signed cookie. The session is global: it is the one
  credential gating every page and every workspace.
- **workspace**: an agent environment listed on the landing page. Each
  workspace is served on its own origin, `<agent-id>.localhost:<forward-port>`.
- **discovery**: the background process that finds the user's workspaces after
  startup. "Initial discovery" is its first complete pass.
- **consent screen**: the one-time "Help improve Minds" error-reporting
  question, asked once per machine right after sign-in.
- **goto bridge**: the forward server's `/goto/<agent-id>/` route, which
  converts a valid bare-origin session into a workspace-origin session without
  user interaction.

## Out of scope

- Imbue-cloud account sign-in (email/password/OAuth, the `/auth/*` pages) --
  a separate account system layered on top of the local session. Only its
  funnel point `/post-login` is specified here, because it decides landing.
- The workspace-creation flow beyond "the create form is shown".
- The contents of landing-page workspace rows (liveness, colors, destroy
  status, remote-device tiles, locked-account banners).
- The Electron shell's own startup routing (welcome/restore/create window
  decision).
- The `SKIP_AUTH=1` environment variable, a development escape hatch that
  bypasses every session check; it is intentionally left unspecified.

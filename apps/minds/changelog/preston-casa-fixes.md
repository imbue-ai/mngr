Hardened the desktop client against several CASA Tier 2 findings:

- One-time login codes now expire 15 minutes after they are minted. An unused code past that window is rejected exactly like an invalid code (and marked expired so it cannot be reused). Pre-existing persisted codes with no creation timestamp are treated as expired. (CASA 1.1.2 / 1.3.1)

- Signing out now clears the local session state, not just the SuperTokens session: the sign-out responses expire the bare-origin `minds_session` cookie, and the Electron main process clears cookies and web storage from both the default session and the workspace-content partition on the authenticated -> unauthenticated boundary. (CASA 2.2.1 / 6.6.1)

- First-party desktop-client pages now carry `X-Content-Type-Options: nosniff` and a Content-Security-Policy. The headers are scoped to the bare-origin chrome pages and are deliberately not applied to proxied per-agent content on `<agent-id>.localhost` subdomains, which continue to control their own CSP. (CASA 5.1.7)

- The detached host-destroy command now shell-quotes its `--include` host-id filter as defense-in-depth, so a malformed host id cannot break out of the `bash -c` string. Behavior is unchanged for well-formed host ids. (CASA 5.1.9)

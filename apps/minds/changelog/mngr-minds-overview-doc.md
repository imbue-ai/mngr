Rewrote `docs/overview.md` into an accurate, top-level architecture overview of
minds and linked it from the README's "Learn more" section (it was previously
unreferenced there).

The prior overview had drifted from the code on several load-bearing points; the
new version is grounded in the current source, covering:

- the desktop client as a Flask app served by cheroot, which serves the
  bare-origin UI on `localhost:8420` and spawns the separate `mngr forward`
  plugin (default port 8421) that does the actual `<agent-id>.localhost`
  HTTP/WebSocket forwarding;

- the three independent local credentials (`minds_session`,
  `mngr_forward_session`, `MINDS_API_KEY`) versus SuperTokens as Imbue Cloud
  account identity;

- the workspace container model (the `system-services` primary agent, separate
  chat agents, bootstrap + supervisord, and the actual background services);

- the creation flow (`POST /api/v1/workspaces`, the `system-services@<host>`
  address, status phases);

- the six `LaunchMode` members (DOCKER, VULTR, LIMA, IMBUE_CLOUD, AWS, MODAL);

- client-config selection and the environment tiers; and

- global access via the Cloudflare tunnel, Cloudflare Access, and the
  remote-service connector.

Advanced the dev tier's pinned web-create template (`[web_workspaces].template_ref` in `envs/dev/deploy.toml`) to `mngr/final-web-details`, pairing the connector's `POST /hosts/claim` with pool slices baked from that default-workspace-template branch.

Added an "Enable web access" toggle (default off) to the create form's advanced view: when on, the workspace is brought up shared post-create so it is reachable from the hosted web client. imbue_cloud rows delegate to the connector's server-side enable-sharing primitive; local docker/lima rows run the desktop share flow with the owning account as the sole grantee. Requires a selected account (the create is refused otherwise).

Desktop-injected share materials now include `SHARE_CHROME_ORIGIN` (the connector origin, where the web chrome is served), so desktop-shared workspaces are embeddable and health-probeable from `/web` exactly like connector-shared ones.

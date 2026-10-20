The staging bring-up runbook's relay deploy step now passes the frps plugin secret via the `FRPS_AUTH_SECRET` environment variable with a secret-free `--plugin-auth-url` (`https://<connector>/frps/auth`), matching the new `share-relay` CLI where the secret is rendered into the plugin `addr`'s URL userinfo instead of the callback URL path (issue #616).

`next_deploy.md` gains the per-tier rollout checklist for the change: connector first, relay fleet redeploys, the comma-set secret rotation, and the eventual legacy-route cleanup.

Desktop-created shares now stamp the connector-reported chrome origin into the workspace's `share.env` (`SHARE_CHROME_ORIGIN`), instead of hardcoding the bare connector URL (issue #746). On tiers whose web chrome lives on a custom domain (`deploy.toml [origins].chrome_origin`, e.g. `https://minds.imbue-staging.com`), a desktop-shared workspace's `frame-ancestors` CSP previously only allowed the connector's modal.run origin, so the `/web` chrome showed blocked frames and its health probes failed.

When the connector reports no chrome origin (an older connector, or a tier with no hosted chrome configured), the previous behavior is preserved: the bare connector origin is stamped, which is where dev tiers path-serve the chrome.

Already-shared workspaces are not healed in place; they pick up the correct origin the next time sharing is re-enabled from an updated client (a grants-only edit deliberately never rewrites `share.env`).

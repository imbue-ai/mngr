Trimmed the README to user-relevant content and tightened it for concision.

Corrected the connector-URL precedence (there is no baked-in default; it comes from the `connector_url` config or the env var, else raises) and the `hosts release` argument (a host-db-id, not a lease-id).

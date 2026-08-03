Replace the Cloudflare-tunnel client + CLI with the self-hosted shares surface.

New `mngr imbue_cloud shares create|delete|status|list` commands and the matching connector-client methods (the one-time relay token is surfaced as a secret). Deletes the `mngr imbue_cloud tunnels` group, the tunnel/service/auth-policy client methods, `TunnelInfo`/`ServiceInfo`/`AuthPolicy`, and the `max_tunnels` / `max_services_per_tunnel` entitlements. The share quota surfaces through the connector's standard quota-exceeded error shape.

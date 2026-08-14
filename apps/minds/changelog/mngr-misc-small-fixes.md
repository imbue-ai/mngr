Desktop sharing improvements:

- The Share tab now shows per-step provisioning progress while a new share link comes up (link registered / TLS certificate issued / tunnel connected / end-to-end check) instead of a static "this can take a minute" message. The signals ride the existing readiness poll, which now also returns `cert_not_after` and `last_tunnel_login_at`; the tunnel step is detected by the login stamp changing during the wait.

- First-time shares of local (docker/lima) workspaces now pick the relay region by measured TCP connect latency from the user's machine instead of always landing on the tier's default region. Re-shares keep their existing region (the region is baked into the share's domain).

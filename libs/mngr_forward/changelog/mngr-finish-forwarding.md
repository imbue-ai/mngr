Service-per-origin local forwarding with host-id workspace coordinates.

Hostnames are now `[<service>.]host-<hex>.localhost:<port>`: the bare origin serves the configured shell service (or the fixed port in `--forward-port` mode), `<service>.` origins reach any agent-registered service directly, and deeper labels route to the same service (its own sub-origin space for multi-origin apps). Replaces the previous `agent-<hex>.localhost` single-origin scheme.

The `/goto/` bridge is keyed by host id, carries an optional `service` label chain so deep links land on the exact origin requested, and sets the session cookie with `Domain=host-<hex>.localhost` so one bridge hop authenticates the shell and every service origin.

Unregistered-but-plausible service origins serve the auto-retrying loading page instead of an error.

TLS/HTTP-2 mode mints per-SNI certificates on first handshake for nested `.localhost` names (a static SAN list cannot cover runtime-discovered host ids or service labels); certs for names longer than the 64-char CN limit are SAN-only.

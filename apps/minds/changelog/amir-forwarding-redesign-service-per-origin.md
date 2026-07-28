# Service-per-origin forwarding

The path-prefix muxing (`/service/<name>/` through the system interface) is
replaced by service-per-origin routing: locally every registered service is
served at `http://<name>.agent-<hex>.localhost:8421/` (any deeper subdomain
routes to the same service), and the workspace shell stays at the bare
`agent-<hex>.localhost:8421` origin. The shell service is renamed
`system_interface` -> `system-interface` (DNS-safe), and minds forwards that
service by default.

Sharing the workspace (the `system-interface` service) now fans out to every
registered service: each gets its own `<name>--<host>--<user>.<domain>`
hostname with the same email grants, so the shared shell's panels work for
recipients. Disabling the workspace share removes all of them.

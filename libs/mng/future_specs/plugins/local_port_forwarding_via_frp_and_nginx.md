# Local Port Forwarding via FRP and Nginx Spec [future]

This plugin exposes services running inside remote hosts to the user's local browser.

See [user-facing documentation](../../docs/core_plugins/local_port_forwarding_via_frp_and_nginx.md) for usage.

## Components

### Local computer (where mng runs)

- **frps** - The frp server. Listens on a configurable port (default: 8080) for frpc connections. Routes HTTP traffic based on `Host` header to the appropriate frpc proxy.
- **Auth endpoint** - A temporary HTTP server started by `mng auth`. Sets `mng_auth` cookie scoped to `*.mng.localhost`.

### Remote host

- **frpc** - Connects to frps via SSH tunnel. Registers services as named proxies with subdomain-based custom domains.
- **nginx** - Reverse proxy that enforces cookie auth and routes to local services.

## Architecture

The SSH tunnel ensures frpc on remote hosts can reach frps on the local computer. mng maintains this tunnel for all running remote hosts.

## Subdomain Structure

Pattern: `<service>.<agent>.<host>.mng.localhost:<frps-port>`

- **service** - User-chosen name passed to `forward-service add --name`
- **agent** - Agent name (defaults to `$MNG_AGENT_NAME` env var)
- **host** - Host name (from the provider)
- **frps-port** - Port frps listens on (default: 8080)

## forward-service Command

Injected into hosts at `$MNG_HOST_DIR/bin/forward-service`.

### Add a forward

```bash
forward-service add --name <service> --port <local-port> [--agent <agent>]
```

Steps:
1. Compute subdomain: `<service>.<agent>.<host>`
2. Write frpc proxy config to `/etc/mng/frpc/proxies.d/<service>.<agent>.toml`
3. Write nginx location config to `/etc/mng/nginx/forwarding.d/<service>.<agent>.conf`
4. Regenerate combined configs and reload frpc + nginx
5. Print the resulting URL to stdout

### List forwards

```bash
forward-service list [--agent <agent>]
```

Lists all config files in `proxies.d/` matching the agent filter.

### Remove a forward

```bash
forward-service remove --name <service> [--agent <agent>]
```

Removes the corresponding files from `proxies.d/` and `forwarding.d/`, then reloads.

## Configuration Directory Structure

On remote hosts:

```
/etc/mng/
├── nginx/
│   ├── nginx.conf              # Main config, includes conf.d/*.conf, forwarding.d/*.conf, plugins.d/*.conf
│   ├── conf.d/
│   │   └── security.conf       # Auth checking (validates mng_auth cookie or X-Mng-Auth header)
│   ├── forwarding.d/           # Per-service location blocks (managed by forward-service)
│   │   ├── web.alice.conf
│   │   └── api.alice.conf
│   └── plugins.d/              # Plugin-specific configs (e.g., activity tracking)
│
└── frpc/
    ├── frpc.toml               # Base config: serverAddr, serverPort, auth token
    └── proxies.d/              # Per-service proxy definitions (managed by forward-service)
        ├── web.alice.toml
        └── api.alice.toml
```

### frpc proxy config format

`/etc/mng/frpc/proxies.d/<service>.<agent>.toml`:
```toml
[[proxies]]
name = "<service>.<agent>.<host>"
type = "http"
localPort = <port>
customDomains = ["<service>.<agent>.<host>.mng.localhost"]
```

### nginx location config format

`/etc/mng/nginx/forwarding.d/<service>.<agent>.conf`:
```nginx
location / {
    # Auth is checked in security.conf via include
    proxy_pass http://127.0.0.1:<port>;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
}
```

### security.conf

```nginx
# Check for valid auth
set $auth_valid 0;
if ($cookie_mng_auth = "<expected-token>") {
    set $auth_valid 1;
}
if ($http_x_mng_auth = "<expected-token>") {
    set $auth_valid 1;
}
if ($auth_valid = 0) {
    return 401 "Unauthorized. Run 'mng auth' to authenticate.";
}
```

## Authentication Flow

### mng auth command

1. Generate a random nonce
2. Start temporary HTTP server on random port
3. Open browser to `http://mng.localhost:<frps-port>/_mng/auth?callback=http://localhost:<temp-port>&nonce=<nonce>`
4. Auth endpoint (served by frps or a sidecar):
   - Sets `mng_auth` cookie with value `<token>`, domain `.mng.localhost`, path `/`
   - Redirects to callback URL with nonce
5. Temp server receives callback, verifies nonce, exits successfully
6. Token is also saved to `~/.config/mng/auth_token` for programmatic use

### Programmatic access

```bash
curl -H "X-Mng-Auth: $(cat ~/.config/mng/auth_token)" <url>
```

## SSH Tunnel Management

We should use `autossh` to set up remote port forward: `-R <remote-port>:localhost:<frps-port>`

Thus, frpc can connect to frps via `localhost:<remote-port>`

When running basically any mng command, we should ensure the auto-ssh tunnel is up.

Specifically when running `mng create` or `mng start` for remote hosts, we should ensure that the auto-ssh tunnel made it out of the starting gate (see autossh docs).

## Trust

Untrusted hosts should not be allowed to set up port forwarding by default.

TODO: specify the exact set of permissions that are exposed by this plugin (eg. the ability to forward at all).

## Open Questions

- Should we support TCP/UDP forwarding (non-HTTP)? Would bypass nginx auth.
  - It's not really possible to restrict this from happening--seems like permissions are a bit all-or-nothing here (so, might as well allow it)
- Per-host vs global auth tokens?
  - Probably global to start with, would be pretty annoying otherwise
    - Though this interacts pretty badly with untrusted hosts...  TODO: figure out what to do here
- Cookie lifetime (session vs persistent)?
  - Persistent

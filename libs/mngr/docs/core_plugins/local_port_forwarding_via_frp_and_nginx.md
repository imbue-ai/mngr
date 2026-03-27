# Local Port Forwarding via FRP and Nginx [future]

This plugin exposes services running inside remote hosts to your local browser using [frp](https://github.com/fatedier/frp) (Fast Reverse Proxy) and nginx.

## Overview

When you create a remote host, services running inside it are isolated from your local network. This plugin provides a way to access those services through subdomain-based URLs:

```
<service>.<agent>.<host>.mngr.localhost:8080
```

For example:
- `api.alice.dev-box.mngr.localhost:8080` - An API exposed by agent "alice"
- `web.bob.dev-box.mngr.localhost:8080` - A web app from agent "bob" on the same host

## Forwarding a Service

Agents can forward services using the `forward-service` [future] command:

```bash
# Forward local port 3000 as "web"
forward-service add --name web --port 3000

# Forward with a custom agent name
forward-service add --name api --port 8000 --agent my-agent

# List current forwards
forward-service list

# Remove a forward
forward-service remove --name web
```

The command prints the resulting URL, which is immediately accessible after authentication.

## Authentication

All forwarded services require authentication. There are two ways to authenticate:

### Browser Access

Run `mngr auth` [future] to set an authentication cookie in your browser. After this, all `*.mngr.localhost` URLs will work automatically.

### Programmatic Access

For scripts and tools, use the `X-Mngr-Auth` header:

```bash
curl -H "X-Mngr-Auth: $(cat ~/.config/mngr/auth_token)" \
  http://api.alice.dev-box.mngr.localhost:8080/
```

## Requirements

This plugin requires:

- **frp** - Install via your package manager or download from [GitHub](https://github.com/fatedier/frp/releases)
- **nginx** - On remote hosts (installed automatically during provisioning)

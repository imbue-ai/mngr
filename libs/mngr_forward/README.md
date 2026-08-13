# mngr_forward

Auth + workspace-origin forwarding plugin for `mngr`.

`mngr forward` runs a local proxy that serves
`[<service>.]<host-id>.localhost:<port>/*` and byte-forwards each request to
the matching backend. The bare `host-<hex>.localhost` origin maps to the
configured backend (`--service NAME`, the default workflow, or a fixed remote
port via `--forward-port REMOTE_PORT`); `<service>.host-<hex>.localhost`
origins map to that agent-registered service, and deeper labels
(`sub.<service>.host-<hex>.localhost`) route to the same service -- they are
the service's own sub-origin space. Remote agents are reached via a per-host
SSH tunnel.

The plugin is opt-in:

```bash
mngr plugin enable forward
mngr forward --service system_interface
```

## Quick start (browser user)

```bash
mngr forward --service system_interface --open-browser
```

This listens on `127.0.0.1:8421`, prints a one-time login URL to stderr (or
emits a `login_url` JSONL event on stdout with `--format jsonl`), and streams
discovered agents and their events to stdout as a merged JSONL stream wrapped
in a `{stream, agent_id?, payload}` envelope. After the browser visits the
login URL, navigations to `host-<hex>.localhost:8421/` are byte-forwarded to
that host's resolved `system_interface` URL through an SSH tunnel, and
`<service>.host-<hex>.localhost:8421/` reaches any other registered service.
One session cookie (set with `Domain=host-<hex>.localhost` by the `/goto/`
bridge) covers the whole workspace-origin family.

## Reverse tunnels

`--reverse <remote-port>:<local-port>` (repeatable) auto-sets up reverse SSH
tunnels for every known agent on a remote host. The `<remote-port>` may be
`0` to ask sshd for a dynamic assignment; the actual bound port is reported
in a `forward.reverse_tunnel_established` envelope event.

## Manual mode

`--no-observe --forward-port REMOTE_PORT` runs `mngr list --format json` once
and forwards a fixed snapshot. `--no-observe` is invalid with `--service NAME`.

## Sub-process integration

Consumers (notably `minds run`) can spawn `mngr forward --format jsonl
--preauth-cookie <opaque-token>`, parse the envelope JSONL stream off stdout,
and pre-set the `mngr_forward_session` cookie in their browser session so the
OTP flow is bypassed.

For plain browsers (which cannot pre-set cookies programmatically), the
consumer can additionally pass `--browser-bridge-token <opaque-token>` and
302 an already-authenticated browser to
`/_bridge?token=<opaque-token>&next=<path>`; the plugin sets the bare-origin
session cookie and redirects onward -- no OTP consumed.

## Embedding (iframes)

Workspace origins are designed to be embeddable in an iframe by a trusted
host application (the minds chrome). Two pieces make this work:

- **Cookies**: on the TLS path (`--use-http2`) session cookies are
  `SameSite=None; Secure; Partitioned` so they are sent from inside a
  cross-site iframe. The plain-HTTP path keeps `SameSite=Lax` (the `None`
  attribute requires `Secure`), so embedding is unsupported without TLS.
- **frame-ancestors**: the proxy APPENDS a
  `Content-Security-Policy: frame-ancestors ...` header to every proxied
  workspace response. The default policy denies external embedding
  (`'self'` + the workspace's own origin family only); pass
  `--embedder-origin <scheme://host[:port]>` (repeatable) to allow specific
  embedders. This is a deliberate, narrow carve-out from the plugin's
  byte-forwarding purity: the proxy may *add* response headers (a service's
  own CSP still applies -- multiple CSP headers compose by intersection),
  but never touches bodies or existing headers.

**Breaking change note**: earlier versions sent no `frame-ancestors` header
at all, so any page could iframe a workspace origin. The default is now
deny-external; embedders must be allowlisted via `--embedder-origin`.

## Request identity headers

Every forwarded request (HTTP and WebSocket) carries `X-Share-Owner: true`. The
single authenticated user of a local forward is always the workspace owner, so
the proxy stamps that flag unconditionally and never sends `X-Share-Email`. Any
client-supplied copy of `X-Share-Owner` / `X-Share-Email` is dropped before the
value is set, so an agent-controlled backend page cannot forge its own
ownership or a caller email.

This is the same contract a shared workspace gets from its in-container
share-gateway (which additionally sends `X-Share-Email` for non-owner visitors),
so an in-workspace service reads request identity identically whether it is
reached locally or over the relay. See the default-workspace-template's
`system/services/share_gateway/README.md` for the full contract, including how
the owner's email is delivered (a file present only while shared), which never
travels as a per-request header.

## TLS trust for plain browsers

With `--use-http2` the proxy serves leaf certificates minted per startup from
a persistent local CA (stored under `$MNGR_HOST_DIR/plugin/forward/ca/`). Run

```bash
mngr forward --trust-ca
```

once to install that CA into your platform's trust stores (macOS login
keychain; Linux per-user NSS database used by Chrome), after which browsers
accept every workspace origin without certificate warnings. The Electron
minds app trusts the proxy programmatically and does not need this.

## Status

Experimental.

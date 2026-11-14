# mngr_forward

Auth + agent-origin forwarding plugin for `mngr`.

Origins are keyed by agent id (the canonical coordinate; legacy `host-<hex>`
origins from older persisted URLs are redirected to it).

`mngr forward` runs a local proxy that serves
`[<service>.]<agent-id>.localhost:<port>/*` and byte-forwards each request to
the matching backend. The bare `agent-<hex>.localhost` origin maps to the
configured backend (`--service NAME`, the default workflow, or a fixed remote
port via `--forward-port REMOTE_PORT`); `<service>.agent-<hex>.localhost`
origins map to that agent-registered service (an Imbue Studio workspace's chat is
one: a registered app served at its own `chat` origin), and deeper labels
(`sub.<service>.agent-<hex>.localhost`) route to the same service -- they are
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
login URL, navigations to `agent-<hex>.localhost:8421/` are byte-forwarded to
that host's resolved `system_interface` URL through an SSH tunnel, and
`<service>.agent-<hex>.localhost:8421/` reaches any other registered service.
One session cookie (set with `Domain=agent-<hex>.localhost` by the `/goto/`
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
host application (the Imbue Studio chrome). Two pieces make this work:

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

## Per-agent request headers

A host application can have the proxy stamp headers of its own onto every
request it forwards, with `--request-headers-file <path>`. The file is one
JSON object: each key is an agent id (`agent-<hex>`) or `"*"`, and each value
maps header names to string values:

```json
{
  "*": {"X-Example-Requester": "owner"},
  "agent-<hex>": {"X-Example-Requester": "owner:alice"}
}
```

On every proxied HTTP request and WebSocket handshake to an agent, the proxy
first deletes any inbound header whose name (case-insensitively) appears
anywhere in the file -- the union over every entry, so a page served by one
agent can never smuggle a header another agent's entry controls -- and then
sets the agent's own entry, else the `"*"` entry, else nothing. Header names
must be valid tokens and may not be request framing (`Host`,
`Content-Length`, `Transfer-Encoding`) or hop-by-hop headers (`Connection`,
`Upgrade`, ...); a file that names one is malformed as a whole.

The file is stat'ed per request and re-parsed when its mtime or size changes,
so the host application can rewrite it at any time (atomically, to avoid a
torn read); a malformed file is logged once per change and treated as empty.
Without the flag nothing is stripped or stamped. The proxy attaches no meaning
to the headers: what they carry is the host application's contract with the
services behind its agents.

## When the proxy answers a request itself

When a request to an agent origin cannot be handed to its backend, or the
backend's answer breaks off, the proxy answers it with one of its own statuses:

- **503**: no backend answered before its response headers -- the agent is not
  known yet, its origin has no route, the connection to its host could not be
  made, or nothing answered on the backend's port. Retrying later may work.
- **502**: the backend's response broke off after its headers.
- **504**: the connection to the backend could not be made in time, the backend
  accepted the request and never answered, or the proxy's own connection pool
  had no free slot.

A backend that itself answers with one of these statuses is forwarded
unchanged; only the proxy's own answers carry these two headers:

- `X-Mngr-Forward-Reason`: why the proxy answered, one of `DISCOVERY_PENDING`
  (discovery has not reported yet), `UNKNOWN_AGENT` (discovery has reported and
  knows no agent for the origin), `NO_ROUTE`, `NO_ROUTE_EVENTS_STOPPED` (no
  route while the agent's events stream, which delivers routes, is not
  running), `LOCAL_CONNECTION_FAILED` (this device could not build its end of
  the connection), `UNREACHABLE`, `NOT_LISTENING` (the agent's host refused the
  backend's port), `NOT_ANSWERING`, `CONNECTION_LOST`, `TIMED_OUT`,
  `POOL_EXHAUSTED`.
- `X-Mngr-Forward-Request-Sent`: `no` when the request never reached the
  backend (no route, no connection, a refused or timed-out dial, no pool slot,
  a refused channel to the backend's port), so the backend cannot have acted on
  it; `yes` when it was sent, in whole or in part, before the connection
  dropped or timed out, so the backend may have acted on it.

A caller that does not accept HTML gets the status, the headers and a short
plain-text body. A browser gets a page instead: for a 503, the "Loading
workspace" page, which re-attempts the origin every second and loads the
backend's answer once there is one; for a 502 or 504, a page saying the
connection dropped, with a Try again button and the plain-text body behind "See
details". A spent or invalid sign-in link to an agent origin
(`/_subdomain_auth`) gets a page that offers the path it was for.

The pages do not explain the reason. A framed page tells its parent window
instead, which can draw its own account over the frame:

```js
// on load, and again whenever a re-attempt answers with a different reason
{type: "mngr-forward:page", reason, status, details}
// when the page goes away (it loaded the backend's answer, or the frame moved on)
{type: "mngr-forward:page-closed"}
```

`status` is the page's HTTP status and `details` its plain-text body. The
`frame-ancestors` policy every proxied origin carries means the parent is the
agent's own origin family or an allowed embedder.

## TLS trust for plain browsers

With `--use-http2` the proxy serves leaf certificates minted per startup from
a persistent local CA (stored under `$MNGR_HOST_DIR/plugin/forward/ca/`). Run

```bash
mngr forward --trust-ca
```

once to install that CA into your platform's trust stores (macOS login
keychain; Linux per-user NSS database used by Chrome), after which browsers
accept every workspace origin without certificate warnings. The Imbue Studio
Electron app pins that CA programmatically (the backend hands it the
certificate's path) and does not need this.

## Status

Experimental.

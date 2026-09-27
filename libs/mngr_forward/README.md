# mngr_forward

Auth + agent-origin forwarding plugin for `mngr`.

Origins are keyed by agent id (the canonical coordinate; legacy `host-<hex>`
origins from older persisted URLs are redirected to it).

`mngr forward` runs a local proxy that serves
`[<service>.]<agent-id>.localhost:<port>/*` and byte-forwards each request to
the matching backend. The bare `agent-<hex>.localhost` origin maps to the
configured backend (`--service NAME`, the default workflow, or a fixed remote
port via `--forward-port REMOTE_PORT`); `<service>.agent-<hex>.localhost`
origins map to that agent-registered service (a minds workspace's chat is
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

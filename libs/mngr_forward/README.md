# mngr_forward

Auth + subdomain-forwarding plugin for `mngr`.

`mngr forward` runs a local proxy that serves `<agent-id>.localhost:<port>/*`
and byte-forwards each request to a service URL discovered for that agent
(`--service NAME`, the default workflow) or a fixed remote port
(`--forward-port REMOTE_PORT`, manual mode). Remote agents are reached via a
per-host SSH tunnel.

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
that agent's resolved `system_interface` URL through an SSH tunnel.

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

## Status

Experimental.

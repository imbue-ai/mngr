Fixed stale, misleading claims in `docs/overview.md` (ticket da-i0go), and the
same subdomain-port claim in `docs/workspace/getting_started.md`:

- Removed the phantom `global = true` key from the `runtime/applications.toml`
  description. `forward_port.py` only ever stores a service's `name` and `url`;
  there is no `global` flag, so nothing in a workspace toggles public exposure
  by editing that file.

- Rewrote the Cloudflare section to state loudly that nothing is publicly
  reachable by default. Provisioning a workspace's tunnel exposes no service on
  its own; exposing a service is an explicit, opt-in share that requires at
  least one email and installs a Cloudflare Access policy, so a shared service
  is always gated behind Cloudflare Access -- never anonymously public.

- Corrected the subdomain-access claim. Agent subdomains
  (`<agent-id>.localhost/...`) are served by the `mngr forward` child process
  over HTTPS, not by the desktop client on port 8420 (which is the desktop
  client's own bare-origin UI). The forwarding port is owned by the `mngr
  forward` plugin (its default is 8421) and reported back to the desktop
  client, so the docs describe the mechanism rather than asserting a fixed
  port.

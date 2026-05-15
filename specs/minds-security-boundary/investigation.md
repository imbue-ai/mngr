# Cross-mind boundary leak via absolute URLs + Lima auto port forwarding

## Incident summary

A user with two minds (`assistant`, then a freshly created `new-assistant` to
which they migrated a GTD/todo app via an external Claude session) observed
that submitting a todo from the new mind's UI silently wrote to the **old**
mind's data store. Investigation (transcript captured in the screenshots that
prompted this spec) found that:

- The GTD service inside `new-assistant` was started without
  `ROOT_PATH=/service/gtd`. FastAPI / Starlette therefore generated absolute
  URLs of the form `http://localhost:8082/items` in `<form action=...>`
  attributes (via `request.url_for(...)`).
- The `minds-workspace-server` proxy
  (`apps/system_interface/imbue/minds_workspace_server/proxy.py`) rewrites
  root-relative paths but does **not** rewrite or block absolute URLs that
  point at `localhost` / `127.0.0.1`. The regex
  `_ABSOLUTE_PATH_ATTR_PATTERN` only matches `href|src|action|formaction`
  values whose first character is `/`.
- The user's Electron renderer is a real browser context. Form submission on
  an absolute URL bypasses the per-mind subdomain proxy
  (`<agent-id>.localhost:<mngr_forward_port>`, served by the
  `mngr_forward` plugin) entirely and is sent to host `localhost:8082`.
- Both `assistant-host` and `new-assistant-host` Lima VMs had something
  binding `0.0.0.0:8082` inside the guest. Lima auto-forwards bound ports
  to the host's loopback; first-binder wins. The host's `localhost:8082`
  therefore pointed at the **old** mind's GTD backend.

End result: the only thing keeping minds apart in the steady state is the
subdomain-routing proxy, and any service that emits an absolute URL through
the workspace-server proxy punches a hole through that boundary.

## Why the existing isolation is insufficient

1. **The mind boundary is URL-shaped, not network-shaped.** It depends on
   every URL in user-visible HTML being routed through
   `<agent-id>.localhost:<mngr_forward_port>`. Any deviation (absolute URLs,
   raw `localhost` references in client-side JS, hardcoded ports in
   templates, etc.) drops the request onto the host's shared loopback.
2. **`libs/mngr_lima/imbue/mngr_lima/lima_yaml.py:71` sets
   `portForwards: []`** — this clears Lima's *explicit* rules but does not
   disable its default behavior of auto-forwarding any port that a guest
   process binds on `0.0.0.0`. Multiple concurrent minds therefore race for
   host ports; the first one to bind wins, the rest silently lose, and from
   that point on any cross-mind URL that hits a shared port flows to the
   wrong mind.
3. **Migration tooling has no awareness of these boundaries.** Asking an
   external Claude to "migrate this app" copies bytes; it does not rewrite
   absolute URLs cached in `runtime/` state, does not enforce that the
   migrated service inherits the new mind's `ROOT_PATH`, and does not warn
   about stale per-mind identifiers.

## Proposed hardening (defense in depth)

In rough order of leverage:

### 1. Strict `Content-Security-Policy` on every workspace-server response

`apps/system_interface/imbue/minds_workspace_server/service_dispatcher.py::_build_proxy_response`
should attach a CSP that pins the origin:

```
Content-Security-Policy: default-src 'self'; form-action 'self';
                         connect-src 'self'; frame-ancestors 'self'
```

Even if HTML contains a smuggled absolute URL, the browser refuses to
submit the form or open the connection. This is the single most effective
fix because it does not depend on enumerating every variation of "bad URL"
the proxy might miss.

### 2. Block host-bound guest ports in Lima/Docker provisioning

Services in `services.toml` and any user-installed app must bind
`127.0.0.1` (or a Unix socket) inside the guest. The `web_server` example
in `forever-claude-template/libs/web_server/src/web_server/runner.py:59`
already does the right thing (`uvicorn.run(app, host="127.0.0.1", ...)`);
the offending GTD service evidently did not.

Concrete steps:

- Add a startup guard in `bootstrap` that scans `runtime/applications.toml`
  registrations and refuses to mark a service ready if its declared URL is
  not `http://127.0.0.1:...` or `http://localhost:...`.
- For Lima specifically, set `portForwards` to an explicit ignore-all rule
  rather than `[]` (Lima treats `[]` as "use defaults"). The right form is
  a single entry that matches every guest port and sets
  `ignore: true`. See Lima docs on `portForwards`.
- For VPS Docker, ensure containers are run without `-p` for service
  ports (only the SSH port should be host-published).

### 3. Extend the proxy's HTML rewriter to handle absolute localhost URLs

Update `_ABSOLUTE_PATH_ATTR_PATTERN` (and the `rewrite_absolute_paths_in_html`
implementation) in
`apps/system_interface/imbue/minds_workspace_server/proxy.py` to also match

```
https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?(/[^"']*)?
```

and rewrite the matched value to `/service/<name>/<rest-of-path>` (or to a
known-broken sentinel that surfaces the bug loudly). This is a backstop for
services that bypass `ROOT_PATH` — the CSP fix above already prevents the
*exploit*; this one keeps the rendered UI from being visibly broken.

### 4. Make `ROOT_PATH` part of the service contract

`scripts/forward_port.py` already runs at service start. Extend it (or the
`app_watcher`) to require that the registering service has been launched
with `ROOT_PATH=/service/<name>` in env, and refuse to mark the service as
ready otherwise. A loud failure at registration time is much better than a
silent boundary violation at submit time.

### 5. Migration hygiene

Document — and ideally provide a `mngr migrate-app` primitive for — the
constraints around moving a service between minds:

- `runtime/` state must be scrubbed of absolute URLs and per-mind
  identifiers (agent id, hostname, container id).
- The migrated service inherits the new mind's `ROOT_PATH`, not the source
  mind's.
- The migration tool should diff the source's `services.toml` entry into
  the destination's and warn on any hardcoded `localhost:<port>` text.

## Acceptance criteria for the fix bundle

A regression test should exercise the failure path end to end:

- Stand up two minds A and B against the same host.
- Mount a service in each that emits a form with
  `action="http://localhost:<port>/submit"` (i.e. the GTD failure pattern).
- Verify that, with the CSP and rewriter changes, submitting the form from
  B's renderer never reaches A's service, regardless of which mind bound
  the host port first.

This belongs as an integration test alongside the existing workspace-server
proxy tests in
`apps/system_interface/imbue/minds_workspace_server/proxy_test.py`.

## Open questions

- Does the user's setup actually use Lima, or is the "Lima" mention in the
  screenshots an artifact of inspecting a Vultr pool host that happens to
  run Lima underneath? The fixes are independent of the provider, but the
  precise port-forwarding rule depends on which guest runtime is in play.
  The same shape of bug exists on `vps_docker` if any service is run with
  a `-p` mapping.
- Are there any internal callers that legitimately need to reach another
  service via absolute `http://localhost:*` URLs from a renderer context?
  If yes, CSP `connect-src 'self'` will break them and needs a designed
  exception (probably routed through the workspace-server proxy regardless).

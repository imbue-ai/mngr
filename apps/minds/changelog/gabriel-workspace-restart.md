Tiered system-interface restart for the minds recovery flow.

- When a workspace's system interface stops responding, minds shows a
  recovery page. While it is checking host health or a restart is in
  flight it shows a single "Loading workspace" state and refreshes itself
  until the workspace is back.
- The recovery page picks its tier from the workspace host's state and
  recovers with no clicks where it safely can. A running container gets a
  surgical system-interface restart (which does not interrupt your
  agents); a fully stopped container gets a full restart immediately
  (nothing is running, so there is nothing to interrupt). Only an
  ambiguous host state falls back to a confirmed "Restart workspace"
  button.
- The recovery page's pre-restart prompt and its post-failure state are
  now one identical "Workspace unresponsive" page: same heading, same
  body, a "Restart workspace" button, and a collapsed error detail that
  appears only when a restart actually failed (expandable, and it wraps
  instead of overflowing its container). The post-failure state no
  longer says "Restart failed" -- the automatic restart runs invisibly
  behind the "Loading workspace" state, so naming a failed attempt the
  user never saw was just confusing.
- The surgical restart cleanly stops and starts the system-services
  agent instead of poking its tmux window; the full restart bounces the
  whole workspace container.
- The recovery page's loading state is visually consistent with the
  forwarding plugin's "Loading workspace" loader, so the two pages a user
  may see during recovery look like one page.
- The sidebar workspace context menu gains a "Restart workspace…" entry
  (with a confirmation, since it interrupts every agent), and the home
  page gains a per-workspace restart button.
- Opening a workspace whose container has been stopped now routes to the
  recovery page (and serves the styled "Loading workspace" loader)
  instead of flashing a raw error.
- The recovery page's "Loading workspace" state no longer shows the
  explanatory "This page will reload automatically..." line -- it just
  shows the heading.
- The recovery page now auto-refreshes on a 1s cadence rather than
  1.5s, so its self-reload coincides with a completed rotation of the
  loading spinner instead of jumping the spinner back mid-rotation.
- The recovery page no longer flashes up for a workspace that is actually
  healthy. A workspace is now only treated as stuck after the background
  probe loop confirms it unreachable with a sustained run of failed HTTP
  probes; a single transient backend hiccup (such as a recycled SSE
  stream) merely starts active probing instead of triggering recovery.
- The forwarding plugin now reports every non-2xx backend response (it no
  longer pre-filters to specific status codes), so minds decides which
  ones matter: only connection-level failures and infrastructure 5xx
  (502/503/504) enroll an agent for active probing. Application errors
  (app 500s, ordinary 4xx) are ignored on the failure-envelope path and
  left for the background probe to adjudicate.
- Minds' HTTP calls through the forwarding plugin -- the
  workspace-readiness / health probes and the refresh-service broadcast
  POST -- now connect to the plugin over loopback and carry the agent's
  ``agent-<hex>.localhost`` vhost in the ``Host`` header, instead of
  putting the subdomain in the request URL. The plugin already routes on
  the ``Host`` header, so this makes those calls independent of
  ``*.localhost`` name resolution, which is not available on every host.
- Recovery diagnostics: the recovery page now runs a batched in-container
  probe (``tmux ls``, ``services.toml`` declaration parse, ``ss``/``curl``
  on the system-interface inner port) plus a plugin resolver-snapshot
  read, and surfaces the results inline. A collapsed Diagnostics
  ``<details>`` block carries the raw observations (host / SSH /
  services-agent state / services.toml / in-container probe / plugin
  resolver) and copyable SSH connection strings for the workspace host,
  with a page-level "Copy diagnostics" button. Probes only run on
  recovery-page load (RESTARTING refreshes skip probing); normal healthy
  operation generates no new probe traffic.
- New "Workspace misconfigured" recovery tier: when ``services.toml`` is
  missing ``[services.system_interface]`` (the only condition where no
  restart can possibly help), the recovery page renders dedicated copy
  explaining that a restart will not help and offers a secondary "Try
  restart anyway" affordance rather than auto-dispatching.
- Auto-escalate to host-restart when the SSH transport to a RUNNING host
  is down (the probe sentinel never returns). The page renders the
  shared "Workspace unresponsive" state, and the primary button is
  rebound to the host restart; bouncing a live container still requires
  explicit consent, so no auto-dispatch.
- The recovery probe runs over ``mngr exec`` with a 5s hard ceiling
  bounded by ``--no-start`` and ``--quiet``, so a wedged container
  cannot gate the recovery UI and a probe will never accidentally start
  a stopped host.
- On every non-HEALTHY -> HEALTHY tracker transition, the system
  interface health tracker now fires an on-recovery callback. Minds
  wires it to a loguru INFO line so the final recovery is visible in
  the log alongside the per-probe diagnostics line.
- Fix a race during sidebar-initiated workspace restarts where the
  recovery page would briefly redirect back to the workspace, then
  flip back to "Loading workspace" once the container actually went
  down. The background health probe loop now skips RESTARTING agents
  -- only the restart worker (which probes after its ``mngr stop``
  completes) can transition an in-flight restart to HEALTHY, so a
  probe of the still-alive pre-restart system interface can no longer
  prematurely declare recovery.
- Recovery-page diagnostics now show the raw ``mngr list`` invocation
  that fed every host-state field. The host-health endpoint surfaces:
  - The exact shell-quoted command (``mngr_list_command``), the raw
    ``stdout`` / ``stderr``, and the subprocess ``exit_code``. The
    diagnostics menu renders them verbatim, so the user can read the
    listing directly (which agents, which host states, which
    per-provider errors) instead of relying on minds' summarization,
    and can paste the command into a terminal to re-run it outside
    minds.
  - ``mngr_list_error``: a one-line summary of why ``mngr list`` did
    not exit cleanly -- whether the subprocess errored, the payload's
    per-provider ``errors`` array was non-empty, or the listing timed
    out. When set, the diagnostics menu surfaces it so the user can
    tell that the issue lives in a sibling workspace's host rather
    than their own.
  - ``plugin_resolver_has_services``: a self-describing boolean
    derived from the existing ``plugin_resolver_services`` map, named
    for what it means rather than asking the reader to compute it.
- The host-state ``mngr list`` is now scoped to this workspace's chat
  agent + system-services agent via a CEL ``id == ...`` include, and
  runs with ``--on-error continue`` so per-provider errors do not blank
  out the entire diagnostic. The recovery page therefore renders
  meaningful per-workspace data even when an unrelated host on the same
  provider is wedged.
- Quieter recovery-probe logs. The on-recovery INFO line now carries a
  compact summary of the cached probe (host state, ssh_dead,
  is_misconfigured, services-agent lifecycle, plugin discovery, probe
  inner port + curl status) instead of dumping the full
  ``HostHealthResponse`` JSON -- the JSON dump otherwise carried
  multi-KB ``mngr_list_*`` and ``probe.raw_stdout`` payloads with no
  programmatic consumer. The recovery probe's ``mngr exec`` subprocess
  also no longer emits a per-failure WARNING with its long
  base64-encoded inner script in the argv: probe failures (e.g. SSH
  transport down on a stopped host) are an expected diagnostic outcome
  already captured by the Layer-2 host-state INFO line via
  ``ssh_dead=True``. Restart-step and ``mngr list`` failures still emit
  the WARNING as before.
- A transient discovery loss (e.g. SSH dying inside a docker container)
  no longer kicks the user out of an open workspace window to the
  landing page. Electron now only navigates the content view to landing
  when the workspace was explicitly destroyed -- the chrome SSE
  ``workspaces`` payload includes a ``destroying_agent_ids`` list, and
  the desktop client remembers which agent ids it has ever seen
  destroying. When a workspace disappears from the live workspaces list,
  Electron checks that set; if the id is not there, the existing
  recovery flow handles the unresponsive workspace via the
  ``system_interface_status`` SSE event, with no nav.
- Minds now records the last-good per-host agent topology to a persistent
  ``last_good_agent_topology.json`` under the data directory, updated
  whenever discovery completely enumerates a host (its system-services
  agent is present). ``get_system_services_agent_id`` runs the same
  host-and-name search over the live snapshot first and falls back to this
  topology when live discovery has lost the host (the SSH-dead failure
  mode), so a restart can still address the system-services agent for
  ``mngr stop`` / ``mngr start``. Without this, a restart attempted while
  the docker provider could not enumerate agents would fail with "Could
  not locate the system-services agent for this workspace." A host whose
  enumeration is incomplete -- or that has dropped out of discovery
  entirely -- keeps its last complete record, so a partial or empty
  snapshot never erases a still-needed pairing (e.g. one wedged workspace
  among several healthy ones).
- Recovery diagnostics rewritten as a flat probe list. The host-health
  endpoint now returns ``probes: [{question, command, output, answer},
  ...]`` plus a derived ``dispatch_tier`` enum
  (``interface_unresponsive``/``host_offline``/``host_unresponsive``/``workspace_misconfigured``)
  instead of the
  prior natural-language fields (``reachable``, ``host_offline``,
  ``ssh_dead``, ``is_misconfigured``, ``host_state``,
  ``services_agent_state``, ``ssh_connections``, ``mngr_list_*``,
  ``plugin_resolver_*``). The recovery page renders each probe as a row
  with a check/x/? glyph and an expander showing the exact command and
  raw output, so the JSON object and the rendered view are kept simple
  and consistent. The page's restart-tier dispatch is now a single
  switch over ``dispatch_tier``. The cached probe-on-recovery INFO log
  and its ``_HostHealthCache`` holder were dropped along the way.
- The recovery page's "Loading workspace" state now hides the
  Diagnostics dropdown and clears the cached host-health payload, so a
  stale diagnostic from the previous tick does not linger on the page
  while a fresh check is in flight (the previous behavior was to leave
  the diagnostic visible after clicking "Restart workspace", which made
  the dropdown look like fresh data when it was already stale).
- The recovery page's restart-failed state now shows the failure error
  details and the diagnostics list together (in separate elements),
  instead of replacing the diagnostics with just the error. The page
  re-runs the host-health probe (with auto-dispatch off so it does not
  stack another restart attempt) so the user can see both the failure
  reason and the current probe answers at once.
- The post-restart startup-wait budget is now tier-aware. A surgical
  (in-place) restart still waits 15s, but a host restart -- which
  cold-boots the whole container -- now waits 30s before declaring the
  attempt failed. The previous shared 15s budget routinely bounced a
  still-booting workspace to the "Workspace unresponsive" page even
  though the container came up healthy moments later.
- A failed restart is no longer a dead end. The "Workspace unresponsive"
  page (restart-failed state) now polls in the background and, the moment
  the workspace's system interface answers again (the background health
  probe recovers it on its own -- e.g. a cold boot that finished just
  after the restart worker's wait elapsed), returns the user to the
  workspace automatically. Previously the page sat unresponsive until the
  user manually navigated away and back. The poll uses a lightweight
  redirect check, so the displayed failure reason and diagnostics stay
  put and the heavy host-health probe is not re-run on each tick.
- The auto-dispatched host restart (chosen only when the container is
  already fully stopped) now skips the redundant ``mngr stop --stop-host``
  step and cold-boots straight away, shaving a full ``mngr`` invocation
  off the recovery path. The manual "Restart workspace" button and the
  SSH-dead escalation still stop first, since they may target a
  still-running container.
- The "Is anything listening on the system-interface inner port?"
  diagnostic no longer depends on ``ss``. The agent container image ships
  no ``iproute2``, so the previous ``ss -ltnp`` probe always failed with a
  bare ``FileNotFoundError(2, 'No such file or directory')`` -- which read
  like the port was down when really the tool was simply absent. The probe
  now scans ``/proc/net/tcp{,6}`` in pure Python for a TCP_LISTEN socket on
  the inner port (decoding the listen address to ``ip:port``), so it works
  on the stock image and answers the question accurately.
- Every recovery-diagnostic row now shows a complete, copy-pasteable command
  whose stdout is exactly the output rendered beside it -- previously the
  command was the data-fetch call while the output was a value minds derived
  from it (e.g. command ``mngr list ... --format json`` but output
  ``RUNNING``), so the two did not correspond. Now:
  - The container-running and services-agent-registered rows pipe ``mngr
    list`` through ``jq -r`` to print exactly the extracted ``.host.state`` /
    ``.state`` (with a ``no host row`` / ``no agent row`` fallback line when
    the row is absent). The synthetic ``state=`` prefix is gone.
  - The in-container checks (services.toml declaration, inner-port LISTEN
    scan, local curl) are wrapped as ``mngr exec <services-agent-id>
    '<check>' --no-start --quiet`` so an operator can run them from the same
    place ``mngr`` lives, without opening a shell inside the container. Each
    inner check prints exactly the row's output: ``declared``/``MISSING`` for
    services.toml, decoded ``LISTEN ip:port`` lines (or ``(no LISTEN socket on
    port N)``) for the port scan, and the bare HTTP status code for curl.
  - The "can we run a command inside" row shows the real batched ``mngr
    exec`` and renders its verbatim stdout (the sentinel followed by the JSON
    payload).
  - The plugin-resolver row is the lone exception: its datum lives in minds'
    own memory (fed by the forward-plugin event stream) and has no in-container
    reproduction, so it stays a clearly-labelled internal observation.
- The workspace-readiness / health probes hit `/` and treat any 200 as
  "ready", deliberately decoupled from whatever application happens to be
  running inside the workspace. The probe makes no assumption about which
  app answers on the inner port or which routes it implements -- it only
  confirms that some web server is up and serving 200s for `GET /`.
- The recovery-page diagnostic that curls the inner web server inside the
  container targets `/`, for the same reason: it confirms a web server is
  answering on the inner port without coupling to any app-specific route.
  The diagnostic row reads "Does the inner web server answer GET / inside
  the container?" and its copy-pasteable `curl` command reflects the `/`
  path.
- The "Workspace unresponsive" page was restyled for a clearer hierarchy.
  The "Restart workspace" button is now the page's focal point -- a
  full-width primary button directly under the message -- rather than being
  sandwiched between the error and diagnostics dropdowns. The error and
  diagnostics disclosures are grouped together below the button under a
  muted "Troubleshooting" label, restyled from the heavy amber-filled boxes
  into quiet white cards with faint borders, a subtle shadow, and a chevron
  affordance (including on each diagnostic-question row). The troubleshooting
  block hides itself entirely whenever neither disclosure is showing, so the
  divider and label never appear over an empty section. Most users only ever
  need the button; the dropdowns are now visibly secondary, for the rare
  deep-debugging case.
- The Diagnostics menu regains a "Copy SSH command" button beside "Copy
  diagnostics". It copies a ready-to-run ``ssh -i <key> -p <port>
  <user>@<host>`` for the workspace host -- the same command mngr emits for
  the host. The per-host SSH command was previously surfaced in the
  diagnostics block but was dropped when the host-health response was
  narrowed to the flat probe list. It is now rendered server-side from the
  backend resolver's SSH info, so the host-health response stays narrow. The
  button is shown for every workspace (Docker, Lima, and remote hosts are all
  reached over SSH) and omitted only in the brief window before discovery has
  surfaced the host's SSH info.
- When the recovery page's ``mngr list`` host-state lookup does not exit
  cleanly (e.g. it times out, or a provider is unreachable) and so returns no
  row for this workspace, the "container running" and "system-services agent
  registered" diagnostic rows now show the failure reason (``mngr list
  failed: ...``) in place of a bare "no row", so the user can tell the
  listing failed rather than concluding the host or agent is genuinely
  absent. When the listing still returns this workspace's own row despite a
  non-clean exit, the real row is shown as before.
- Internal: the ``mngr`` subprocess helper that drives the restart steps and
  the host-health probe no longer converts launch failures (``OSError`` on
  fork/exec, ``ConcurrencyGroupError`` on group setup) into a return value.
  Those genuine exceptions now propagate with their normal traceback and are
  caught at each call site that knows how to surface them -- a restart step
  still marks the workspace "Restart failed" with the reason, and the
  host-health probe still threads the reason into its response. A process that
  actually ran (clean, timed out, or nonzero exit) stays a returned outcome, so
  the partial ``mngr list --on-error continue`` output is still used. No
  user-visible behavior change.

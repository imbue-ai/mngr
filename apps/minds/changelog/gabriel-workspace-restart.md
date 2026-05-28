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
- Minds now caches the workspace-agent → system-services-agent mapping
  to a persistent ``system_services_agent_cache.json`` under the data
  directory whenever discovery surfaces both agents on the same host.
  ``get_system_services_agent_id`` falls back to the cache when live
  discovery has lost the pair (the SSH-dead failure mode), so a restart
  can still address the system-services agent for ``mngr stop`` /
  ``mngr start``. Without this, a restart attempted while the docker
  provider could not enumerate agents would fail with "Could not locate
  the system-services agent for this workspace."
- Recovery diagnostics rewritten as a flat probe list. The host-health
  endpoint now returns ``probes: [{question, command, output, answer},
  ...]`` plus a derived ``dispatch_tier`` enum
  (``surgical``/``host``/``manual``/``misconfigured``) instead of the
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
- Persisted workspace cache. The landing-page tile and chrome workspaces
  list now keep rendering a workspace whose live discovery snapshot has
  transiently dropped it -- the SSH-dead docker-container failure mode.
  ``MngrCliBackendResolver`` writes each primary workspace agent it sees
  into ``workspaces_cache.json`` under the data directory and exposes
  ``list_known_or_cached_workspace_ids`` / a cache-fallback
  ``get_workspace_name``; the landing-page renderer and chrome SSE
  workspace-list builder consume the augmented list. ``list_known_workspace_ids``
  stays live-only so the destroying-records DONE/FAILED classification
  remains authoritative; entries are evicted via ``evict_cached_workspace``
  the moment a destroy transitions to DONE, in both the landing-page
  cleanup path and the chrome SSE poll. Without this, ``pkill sshd``
  inside a docker container made the workspace tile vanish from the home
  page even though the container was still up, leaving the user with no
  re-entry point to the recovery / restart flow.

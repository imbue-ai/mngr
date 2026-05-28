The forwarding plugin now reports a stopped workspace container as a
backend failure instead of flashing a raw error.

- An SSH-tunnel setup failure and a refused host-loopback dial (no SSH
  tunnel available) are both treated as backend failures: the plugin
  emits a `CONNECT_ERROR` `system_interface_backend_failure` envelope and
  serves the styled "Loading workspace" loader to HTML callers, the same
  as other unreachable-backend cases.
- This drives the minds health tracker toward STUCK and routes the user
  to the workspace recovery page when their container has been stopped.
- The "Loading workspace" loader no longer shows the explanatory "This
  page will reload automatically..." line -- it just shows the heading,
  vertically centered against the spinner.
- New `resolver_snapshot` envelope: the plugin emits the full per-agent
  service map on every mutation of that map -- both
  `update_services` (set/replace for one agent) and the destruction
  paths (`remove_known_agent` and `update_known_agents` when they drop
  an agent that had services), so consumer mirrors do not retain stale
  entries for destroyed agents. Minds mirrors the latest snapshot in
  its envelope-stream consumer and uses it on the recovery-diagnostics
  page to render whether the plugin has seen the agent's
  `system_interface` service (Q7 on the recovery checklist).
  Old minds against a new plugin transparently drops the new payload;
  new minds against an old plugin sees no `resolver_snapshot` and just
  renders Q7 as "no entry yet" -- same transient as a fresh plugin
  startup. No periodic flushes, no debouncing, no initial empty
  emission; the first envelope is sent on the first real services
  event.
- The plugin now also treats a 404 on a proxied `GET` as a backend
  failure (`NOT_FOUND_RESPONSE`). The system interface serves its SPA
  index for every unmatched `GET`, so it never 404s a page/route load --
  a 404 reaching the proxy means whatever holds the inner port is not
  the system interface (e.g. a different process has bound the port).
  This enrolls the agent as a probe suspect so the minds health loop can
  confirm and recover, instead of letting a wrong-process responder look
  healthy. Scoped to `GET` only; non-`GET` 404/405s are ordinary
  method/resource outcomes and do not enroll. Like the other reasons it
  is only a hint -- STUCK is still decided by minds' background probe.

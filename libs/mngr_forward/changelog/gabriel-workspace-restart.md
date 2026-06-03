The forwarding plugin now reports an unreachable backend as a backend
failure instead of flashing a raw error, and reports HTTP error responses
with a single generic reason that consumers interpret themselves.

- An SSH-tunnel setup failure and a refused host-loopback dial (no SSH
  tunnel available) are both treated as backend failures: the plugin
  emits a `CONNECT_ERROR` `system_interface_backend_failure` envelope and
  serves the styled "Loading workspace" loader to HTML callers, the same
  as other unreachable-backend cases. A consumer of the envelope stream
  can use this to drive its own recovery UI.
- The "Loading workspace" loader no longer shows the explanatory "This
  page will reload automatically..." line -- it just shows the heading,
  vertically centered against the spinner.
- HTTP error handling is simplified. The plugin no longer special-cases
  which status codes matter (it previously tagged only 502/503/504 as
  `FIVEXX_RESPONSE` and 404-on-`GET` as `NOT_FOUND_RESPONSE`). It now
  forwards every response unchanged and emits a single `ERROR_RESPONSE`
  reason -- carrying the `status_code` -- for any non-2xx response,
  leaving the policy decision (which statuses warrant action, and what
  action) entirely to the consumer.
- New `resolver_snapshot` envelope: the plugin emits the full per-agent
  service map on every mutation of that map -- both `update_services`
  (set/replace for one agent) and the destruction paths
  (`remove_known_agent` and `update_known_agents` when they drop an agent
  that had services) -- so a consumer's mirror does not retain stale
  entries for destroyed agents. The full map is sent on every change (no
  per-agent diff) so a late-attaching consumer only needs the most recent
  envelope to be in sync. No periodic flushes, no debouncing, no initial
  empty emission; the first envelope is sent on the first real services
  event. A consumer older than this change transparently drops the new
  payload; a consumer running against an older plugin simply sees no
  `resolver_snapshot` -- the same transient as a fresh plugin startup.

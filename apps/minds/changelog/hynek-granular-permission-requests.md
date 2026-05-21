- The minds desktop client has been adapted to the new latchkey
  permission-request shape: `LatchkeyPermissionRequestEvent` now carries
  `scope` (Detent schema) and `permissions` (the agent's requested list)
  instead of `service_name`. The previously-bundled
  `apps/minds/imbue/minds/desktop_client/latchkey/services.toml` has
  been deleted; the desktop client now lazily fetches the catalog from
  the gateway's `/permissions/available` endpoint (cached in process)
  to look up display names and the legal permission set. The grant
  dialog continues to render the display name ("Slack" etc.) and lets
  the user broaden or narrow the requested permission set.
- The minds desktop client now tolerates legacy response events on
  disk. Older versions wrote a ``service_name`` field on each
  ``RequestResponseEvent``; the current schema replaced it with
  ``scope``. Without a migration the historical events.jsonl emitted
  a pydantic-extras warning per legacy line at every minds startup
  and the corresponding request would not be marked resolved. The
  loader now drops ``service_name`` before validating, so historical
  responses load cleanly and their requests are correctly filtered
  out of the pending list. The dropped ``service_name`` is
  informational only -- pending-request filtering uses
  ``request_event_id`` -- so no functional information is lost.
- The streamed-permission-request handler now dedupes redeliveries by
  ``event_id``. The gateway re-emits every still-pending request on
  each stream reconnect (every couple of seconds when idle), but the
  handler used to append a fresh entry to the in-memory request inbox
  and emit an INFO log line + an SSE wake-up for every redelivery. The
  ``requests`` list therefore grew unbounded for as long as a request
  stayed pending, and the desktop log filled with duplicate ``Streamed
  latchkey permission request ...`` lines. The handler now checks the
  inbox for the incoming ``event_id`` first and no-ops on a match.
- Fixed a startup race where the minds desktop client could cache a
  stale latchkey gateway port and then fail every subsequent call
  with ``[Errno 111] Connection refused``. The race occurred because
  the supervisor restart and the gateway-client pre-warm previously
  ran on independent background threads at minds startup: the
  gateway client could observe the previous supervisor's record
  (still on disk, still alive) before the restart deleted that
  record and stamped the fresh port. Two fixes:
  - ``LatchkeyGatewayClient`` now self-heals from a stale cached
    gateway URL on connect-level transport failures
    (``httpx.ConnectError`` / ``httpx.ConnectTimeout``): the cached
    URL is invalidated and the next call re-resolves the port from
    the supervisor's on-disk record. Non-connect errors (read
    failures mid-stream, 5xx responses, etc.) continue to propagate
    without invalidation, since those usually indicate a problem at
    the gateway end rather than a stale local cache.
  - The supervisor restart and the gateway-client pre-warm now run
    sequentially on a single background thread, eliminating the
    race in the first place. App startup is unaffected: this still
    runs in a background thread, so the supervisor restart's 10s
    SIGTERM grace never blocks the foreground startup path.
- The latchkey permission dialog no longer pre-checks the catch-all
  ``any`` permission as an implicit default. ``any`` is still offered
  as the first checkbox so the user can opt into unrestricted access
  explicitly, but the initial check state is now the union of (a)
  permissions already granted for the scope on the agent's host and
  (b) the permissions the agent declared in the request event.
  Approving without modification therefore grants exactly that union
  (matching the user's mental model of "give the agent what it's
  asking for, on top of what it already has"). Previously, existing
  grants alone seeded the pre-check and the agent's new ask was
  ignored unless the user actively ticked it; under the new behavior
  an unmodified Approve actually delivers the requested permissions.

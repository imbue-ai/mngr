Workspace start no longer fails silently or with a generic timeout (imbue-ai/mngr-internal#547):

- The start poll surfaces the row's recorded `transition_error` as a `WorkspaceStartFailedError` within one poll cycle whenever a start lands back on `stopped` (or, against an old connector, bounces to `stopping`), instead of burning the full 1200s window; the timeout error itself now names the last observed status and last recorded transition error.

- The access token is fetched per poll probe through the refresh-near-expiry helper, so a token expiring mid-poll no longer aborts the wait with a bare `Unauthenticated (401)`.

- `mngr start` on a still-`stopping` workspace now waits for the stop's upload to verify (the connector refuses starts mid-stop) and requests the start the moment the row lands on `stopped`.

- A `stopping` workspace is now reported as host state STOPPING instead of STOPPED, so listings and UIs no longer render a mid-stop workspace as an already-startable stopped one.

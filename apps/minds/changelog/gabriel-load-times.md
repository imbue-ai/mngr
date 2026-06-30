Restore and extend diagnostic logging on the system-interface health / recovery path, to trace a recurring cold-start false-restart where a workspace is slow to load:

- Log each `system_interface_backend_failure` envelope with its `reason` and `status_code` (so a cold-start `CONNECT_ERROR` / `UNRESOLVED` warm-up can be told apart from a genuine `ERROR_RESPONSE` 5xx).

- Log each non-200 background health-probe tick (the HTTP status, or `transport-error` for a connection-level failure).

- Log every recovery host-health probe's answer and raw output alongside the dispatch tier (the supervisord state word, the listening sockets, and the in-container `curl /` result), so it is visible *why* a tier such as `INTERFACE_UNRESPONSIVE` was chosen.

Logging only; no behavior change.

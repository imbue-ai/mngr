Added diagnostic logging to the system-interface health / recovery path so a slow "Loading workspace…" load (and any unnecessary auto-restart of a healthy workspace) can be traced from the logs alone. The path was previously silent except for the final host-health probe verdict.

New log lines cover each edge of the flow:

- the `system_interface_backend_failure` envelope the plugin emits (with reason + status code), at DEBUG;

- enrolling a workspace as a probe suspect, at DEBUG;

- each non-200 background probe result (with the HTTP status, or a transport error), at DEBUG;

- the start of a probe-failure run and the HEALTHY → STUCK transition (with how long the workspace was continuously failing), at DEBUG / INFO;

- the recovery → HEALTHY transition, at INFO;

- emitting the STUCK recovery redirect once a post-onset discovery snapshot lifts the freshness suppression, at INFO;

- dispatching a recovery restart (system-interface or host tier), at INFO.

This is logging only — no behavior change.

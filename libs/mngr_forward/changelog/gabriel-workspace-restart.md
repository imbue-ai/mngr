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

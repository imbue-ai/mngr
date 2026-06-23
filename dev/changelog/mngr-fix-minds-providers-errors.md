- The `forward-system-interface` justfile recipe now resolves an agent's id with
  `mngr list --on-error continue`, so an unauthenticated/unreachable provider no
  longer aborts the lookup of a local agent.

- Added the design blueprint for robust provider-error handling across minds
  discovery and `mngr list` callers under
  `blueprint/robust-minds-list-provider-errors/`.

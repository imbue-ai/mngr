`AgentError` (and all of its subclasses, e.g. `NoCommandDefinedError`, `AgentNotFoundOnHostError`,
`SendMessageError`, `AgentStartError`) now inherit from `MngrError` instead of `BaseMngrError`.
The remaining `BaseMngrError`-only error types -- `PluginSpecifierError`,
`DiscoverySchemaChangedError`, `MalformedJsonlLineError`, `TolerantPathError`, and
`IssueSearchError` -- were moved the same way. This completes the consolidation of the error
hierarchy under a single user-facing parent class: every mngr error is now a `ClickException`,
so when one reaches the CLI it renders as a clean `Error: ...` message (plus any help text)
instead of a Python traceback, and `except MngrError` handlers treat them as the user-facing
errors they are.

The now-redundant `MngrError` mix-in on `AgentNotFoundError` and `DuplicateAgentNameError`
(which already reached `MngrError` via `AgentError`) was removed; both still behave identically.

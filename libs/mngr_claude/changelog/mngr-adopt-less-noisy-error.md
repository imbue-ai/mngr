`mngr create --adopt-session` now validates the session ID up front, before any host or worktree is created. Passing an unknown (or ambiguous) session ID fails fast with a clean `Error: ...` message instead of crashing mid-provisioning with a full "Unexpected error" traceback.

The "session not found" message is also concise now: it no longer enumerates every searched directory (which included one per local mngr agent, often hundreds of paths).

Internal: the existence/ambiguity check (`_resolve_adopt_session`) now also runs in the `on_before_create` hook, which executes outside `provision_agent`'s `ConcurrencyGroup`. Previously the only check happened in `on_after_provisioning` (inside that group), where the group's exit wrapped the `UserInputError` in a `ConcurrencyExceptionGroup` -- no longer a `ClickException` -- so it was reported as an unexpected error. The session source is always local, so the early result matches the provision-time resolution.

Regenerated the `mngr aws` / `mngr azure` CLI doc pages to cover the state-bucket setup these commands now perform (the providers' state-bucket feature is described in the `mngr_aws` / `mngr_azure` changelogs).

`TagLimitExceededError` now accepts an optional `actual` count (defaulting to omitted) and an optional `remediation` string appended to the message, so providers can surface actionable guidance (e.g. "run `mngr aws prepare`") when a host exhausts the provider's tag mirror.

Test-only: raised the per-test timeout on the tmux lifecycle tests `test_start_restart_running_agent` / `test_start_restart_stopped_agent` from the default 10s to 30s (they run several sequential tmux create/stop/restart operations that can exceed 10s on a loaded CI runner).

Added a shared `emit_operator_result` helper in `mngr.cli.output_helpers` that emits the machine-readable record of a provider `prepare` / `cleanup` result -- JSON writes the data object, JSONL writes a `<event_name>` event -- and is a no-op in human mode, since each provider renders its own command-specific human lines. Also extracted a `write_event_line` primitive that assembles the `{"event": <type>, ...payload}` JSONL shape shared by every JSONL emitter in that module (events, info, errors, operator results).

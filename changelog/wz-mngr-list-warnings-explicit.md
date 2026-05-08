`mngr list --format json` and `mngr list --format jsonl` now surface non-fatal provider warnings to stdout alongside errors, so programmatic consumers can see them without reading stderr:

- `--format json` gains a top-level `warnings` array (parallel to `errors`).
- `--format jsonl` emits `{"event": "warning", "source": ..., "type": ..., "message": ...}` lines as warnings occur.

Each warning is a typed `WarningInfo(source, type, message)` record with provider attribution: `source` identifies the emitting provider/subsystem and `type` is a stable identifier for the warning category (e.g. `VultrApiKeyMissing`, `VultrVpsReadFailed`).

The `list_agents()` API gains an `on_warning` callback (parallel to `on_error`) and a `result.warnings: list[WarningInfo]` field. Provider backends opt in by accepting an `on_warning` parameter on `discover_hosts_and_agents` and emitting `WarningInfo` records explicitly. The Vultr backend is wired in this PR; other backends accept the parameter as a pass-through and can opt in incrementally.

`-q` / `-v` / `-vv` continue to control stderr only; warnings always appear in the structured output regardless of console log level.

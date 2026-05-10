`mngr list --format json` and `mngr list --format jsonl` now surface non-fatal warnings (e.g. "Vultr API key not configured") to stdout alongside errors, so programmatic consumers can see them without reading stderr:

- `--format json` gains a top-level `warnings` array (parallel to `errors`).
- `--format jsonl` emits `{"event": "warning", "message": "..."}` lines as warnings occur.

`-q` / `-v` / `-vv` continue to control stderr only; warnings always appear in the structured output regardless of console log level. The `list_agents()` API gains an `on_warning` callback (parallel to `on_error`) and a `result.warnings: list[WarningInfo]` field.

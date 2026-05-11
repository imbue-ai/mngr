Provider error handling overhauled end-to-end: clean two-tier model, structured `result.warnings` channel, consistent CLI rendering, and a saner `--on-error` semantic.

**Severity model.** Two distinct categories with different treatment:

- **WARNING** (`ProviderUnavailableError` and subclasses): provider literally cannot run on this machine right now (binary missing, daemon down, credentials never configured, network unreachable). Surfaced via `result.warnings` and a one-line stderr summary; never affects exit code; other providers continue producing hosts.
- **ERROR** (`ProviderNotAuthorizedError` and other `ProviderError`): the provider was reached and produced a real failure that needs the user's attention. Surfaced via `result.errors` and per-error red stderr lines; gates exit code per `--on-error`.

**Hierarchy refactor (`mngr/errors.py`).** New `ProviderUnavailableError` subclasses for clearer typing:

- `ProviderBinaryMissingError` — required CLI binary not on PATH (used by `LimaNotInstalledError`)
- `ProviderDaemonNotRunningError` — daemon down (used by Docker)
- `ProviderCredentialsMissingError` — credentials never set (used by Vultr)
- `ProviderNetworkUnreachableError` — connector 5xx / transient (used by `ImbueCloudConnectorError`)

`ModalAuthError` moved from `PluginMngrError` to `ProviderNotAuthorizedError` so `mngr list` surfaces it consistently with other auth failures. `ImbueCloudAuthError` now multi-inherits `ProviderNotAuthorizedError`. `ImbueCloudConnectorError` now multi-inherits `ProviderNetworkUnreachableError`.

**`--on-error` semantic.** Now controls only the exit code; both modes produce identical stdout/stderr and identical `result.errors`/`result.warnings`:

- `abort` (default): exits 1 if `result.errors` is non-empty.
- `continue`: exits 0 always (callers inspect `result.errors` programmatically).

Warnings never affect exit code in either mode.

**CLI rendering (`mngr list`).**

- Default human format: one-line warning summary after the data table (`WARNING: N provider(s) unavailable: <names>. Use -v for details.`); per-error red lines for `result.errors`.
- `-v` / `-vv`: expands the summary to per-warning detail lines.
- `--quiet`: suppresses both warning summary and per-error lines (loguru console handler removed).
- `--format json`: top-level `warnings` array now always present alongside `errors`.
- `--format jsonl`: warnings stream as `{"event": "warning", "type": ..., "provider_name": ..., "message": ...}` lines via `on_warning` callback.

**User-facing behavior changes.**

- `mngr list` on a machine where Lima/Docker daemon/Vultr key isn't set up no longer aborts. It exits 0, shows the data the working providers produced, and emits a one-line warning summary. Previously: exit 1, raw error message, no data shown.
- `mngr list --on-error continue` now exits 0 even if errors were collected. Programmatic consumers should check `result.errors` (in `--format json`) rather than the exit code.
- `mngr list --quiet` now exits 0 when only warnings are present (previously hid the warning but still exited 1 from the abort path).

Modal auth failures and other "real" errors still exit 1 in `--on-error abort` mode and surface as red `ERROR: <provider>: <type>: <message>` lines on stderr.

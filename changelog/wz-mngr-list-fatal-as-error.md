Provider error handling for `mngr list` (and the shared discovery boundary) is now uniform: **every provider failure is an error**. There is no separate "warning" severity tier -- the `--on-error` flag is the single mechanism that expresses what the user wants done about failures.

**Typed exception hierarchy (kept).** Providers raise typed exceptions instead of `logger.warning(...) + return []`: `ProviderUnavailableError` (and subclasses `ProviderBinaryMissingError`, `ProviderDaemonNotRunningError`, `ProviderCredentialsMissingError`, `ProviderNetworkUnreachableError`), `ProviderNotAuthorizedError`, `ProviderDiscoveryError`. These remain useful for programmatic discrimination via `ErrorInfo.exception_type`, but they no longer drive a severity decision.

**No warning tier.** Removed `WarningInfo` / `ProviderWarningInfo`, `ListResult.warnings`, the `on_warning` callback, and the CLI's warning rendering. `ProviderUnavailableError` is no longer special-cased at the discovery boundary -- it flows through the same path as any other provider failure and lands on `result.errors`.

**`--on-error` semantic.** Controls only the exit code:
- `abort` (default): exits 1 if `result.errors` is non-empty.
- `continue`: exits 0; callers inspect `result.errors` programmatically.

**CLI rendering (`mngr list`).**
- `--format json`: top-level object is `{"agents": [...], "errors": [...]}` (no `warnings` key).
- `--format jsonl`: agent lines plus `{"event": "error", ...}` lines; no `event: warning` lines.
- stderr carries one red `ERROR: <provider>: <type>: <message>` line per `result.errors` entry, for every output format. `--quiet` suppresses them (loguru console handler removed).

**Shared discovery boundary (`api/discover.py`).** `discover_hosts_and_agents` -- used by `destroy`, `exec`, `snapshot`, etc. -- no longer soft-skips an unavailable provider with a `logger.warning`. The failure is wrapped in `ProviderDiscoveryError` and propagates, so a provider that can't be reached fails loudly instead of silently dropping its machines. (`api/gc.py` is intentionally unchanged: its `MngrError` soft-skip predates this work and has an orphan-detection-safety reason.)

**User-facing behavior changes.**
- `mngr list` on a machine where a configured provider can't run (Lima `limactl` missing, Docker daemon down, Vultr key unset, Modal not authorized) now exits 1 under the default `--on-error abort`, with the failure shown as an error. Previously it exited 0 with a soft warning. To opt out: disable the provider (`mngr config set --scope user providers.<name>.is_enabled false`) or pass `--on-error continue`.
- `mngr list --format json` output no longer contains a `warnings` array.
- `mngr destroy` / `mngr exec` / `mngr snapshot` against an unavailable provider now fail with a typed error instead of silently skipping that provider.

**Modal auth.** `ModalProviderBackend` initialization now raises `ModalAuthError` (a `ProviderNotAuthorizedError`) instead of a bare `MngrError` on `ModalProxyAuthError`, so the failure carries the proper `exception_type` and the canonical `--scope user` disable hint.

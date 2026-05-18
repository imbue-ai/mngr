Providers can now surface per-resource failures (e.g. "VPS 7 of 10 is unreachable") to `mngr list` without aborting the whole discovery.

- `discover_hosts_and_agents` on `BaseProviderInstance` accepts an optional `on_error: Callable[[ErrorInfo], None] | None` callback. When invoked from within a provider's per-resource catch site, the listing pipeline records the error on `result.errors` with full attribution (`ProviderErrorInfo` / `HostErrorInfo` / `AgentErrorInfo`).
- Per-resource errors render identically to whole-provider errors: in `--format json` they appear in the existing `errors` array; in `--format jsonl` as `event:error` lines; on stderr via `_render_errors_to_stderr`. No new fields, no new arrays.
- `--on-error abort` (default): per-resource errors cause exit 1 after discovery completes, the same as whole-provider errors. `--on-error continue`: exit 0.
- The shared `VpsDockerProvider._read_records_from_vps` is wired as the canonical case -- a VPS that fails outer SSH now produces a `ProviderErrorInfo` in `result.errors` instead of being silently swallowed by `logger.warning`. Vultr and OVH (which both subclass `VpsDockerProvider`) inherit this behavior. Other providers (Modal, ImbueCloud) accept the new parameter as a pass-through and can opt in incrementally.

`ErrorInfo`, `ProviderErrorInfo`, `HostErrorInfo`, and `AgentErrorInfo` move from `imbue.mngr.api.list` to `imbue.mngr.interfaces.data_types`; the originals remain importable from `imbue.mngr.api.list` for backward compatibility.

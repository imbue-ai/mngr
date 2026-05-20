Providers can now surface per-resource failures (e.g. "VPS 7 of 10 is unreachable") to `mngr list` without aborting the whole discovery.

- `discover_hosts_and_agents` on `ProviderInstanceInterface` accepts an optional `on_error: Callable[[ErrorInfo], None] | None` callback. When invoked from within a provider's per-resource catch site, the listing pipeline records the error on `result.errors` with full attribution (`ProviderErrorInfo` / `HostErrorInfo` / `AgentErrorInfo`).
- Per-resource errors render identically to whole-provider errors and reuse the existing surfaces: in `--format json` they appear in the existing `errors` array and the CLI also logs each entry to stderr as a warning; in `--format jsonl` they stream to stdout inline as `event:error` lines (no separate stderr render); and in the default human format the CLI logs each entry to stderr as a warning. No new fields, no new arrays.
- `--on-error` controls whether discovery aborts mid-flight or records-and-continues, not the final exit code. `mngr list` exits 1 whenever any errors were recorded (the same behavior in both modes).
- Provider sites wired to emit per-resource errors:
  - `VpsDockerProvider._read_records_from_vps` (inherited by Vultr and OVH): a VPS that fails outer SSH now produces a `ProviderErrorInfo` instead of being silently swallowed by `logger.warning`.
  - `ImbueCloudProvider._collect_listing_raw_via_outer`: a leased host whose outer SSH fails (auth, unreachable, or non-zero listing script) now produces a `HostErrorInfo`; the host still appears as a fallback stub.
  - `ModalProviderInstance._list_running_host_ids`: a running sandbox whose tags can't be parsed (so it would silently vanish from the listing) now produces a `ProviderErrorInfo`.

`ErrorInfo`, `ProviderErrorInfo`, `HostErrorInfo`, and `AgentErrorInfo` move from `imbue.mngr.api.list` to `imbue.mngr.interfaces.data_types`; the originals remain importable from `imbue.mngr.api.list` for backward compatibility.

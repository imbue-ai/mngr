Provider error handling in the discovery pipeline is now consistent and typed. Two-part change:

**Provider implementations** now raise typed errors instead of `logger.warning(...) + return []`:

- **Vultr**: missing API key in `_get_tagged_vps_ips` / `_find_host_record` raises `ProviderNotAuthorizedError`.
- **Lima**: `limactl` errors raise `ProviderUnavailableError`; the previous catch of `ProviderUnavailableError` (debug-logged only) is removed so it propagates.
- **Docker**: any `DockerException` from `discover_hosts` raises `ProviderUnavailableError`; `_docker_client` already wrapped daemon-down this way.
- **imbue_cloud**: `ImbueCloudConnectorError` from `client.list_hosts` (5xx, network, malformed response) is rewrapped as `ProviderUnavailableError`. `ImbueCloudAuthError` (401/403) propagates unchanged so misconfigured credentials surface as a hard error.

**Discovery boundary** (`api/list.py`, `api/discover.py`) now distinguishes "provider unavailable" from other errors:

- `ProviderUnavailableError` → log a warning, skip that provider gracefully, listing continues with the rest. This preserves the friendly UX for "machine doesn't have prerequisite" (binary missing, daemon down, network unreachable).
- All other `MngrError` subclasses → behave as before: under `--on-error continue` they appear as `ProviderErrorInfo` in `result.errors`; under `--on-error abort` the listing exits non-zero.

The split mirrors Modal's existing pattern: configuration errors (wrong/missing credentials, e.g. `ModalAuthError`, `ProviderNotAuthorizedError`) are loud; deployment facts (machine can't run this provider) are quiet.

Behavior changes worth flagging:

- `mngr list` with an enabled-but-misconfigured provider (e.g. Vultr API key not set) now exits non-zero under default `--on-error abort`. Either configure the provider, use `--on-error continue`, or disable the provider via `mngr config set --scope user providers.<name>.is_enabled false`.
- `mngr wait` and other commands going through `api/discover.py` no longer silently treat connector outages as "host not found"; they surface as the underlying error. This is more honest but is a contract change for callers that conflated the two.
- Lima/Docker on machines without `limactl` / Docker daemon running continue to work via soft-skip (no behavior change for these specific cases — the provider implementations now raise typed errors but the boundary catches them).

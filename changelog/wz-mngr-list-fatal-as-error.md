Fatal provider failures during `mngr list` now raise typed errors instead of being silently downgraded to `logger.warning(...) + return []`. Three sites changed:

- **Vultr**: missing API key in `_get_tagged_vps_ips` and `_find_host_record` now raises `ProviderNotAuthorizedError`.
- **Lima**: `limactl` unreachable (`LimaCommandError`/`OSError`) now raises `ProviderUnavailableError`. The previous swallow of `ProviderUnavailableError` (logged at debug only) is also dropped.
- **imbue_cloud**: `client.list_hosts(...)` failure in `_list_leased_hosts_cached` now propagates the `MngrError` instead of clearing the cache to `[]`.

These match Modal's existing pattern (`@handle_modal_auth_error`) so all providers behave consistently. Errors flow through the normal listing-pipeline boundary in `api/list.py`: under `--on-error continue` they appear as `ProviderErrorInfo` entries in `result.errors`; under `--on-error abort` the listing exits non-zero.

User-visible behavior change: users with an enabled-but-unconfigured Vultr/imbue_cloud/Lima provider running `mngr list` (default `--on-error abort`) will now see an error instead of a soft warning. To opt out, either configure the provider, switch to `--on-error continue`, or disable the provider:

```
mngr config set --scope user providers.<name>.is_enabled false
```

Part of the account-owned remote providers program (`specs/account-owned-providers/spec.md`); the entries `mngr-per-client-host-keys.md` and `mngr-fix-provider-storage.md` in this directory belong to the same PR.

- The slice provider keeps main's SSH-CA management access (no static pool-management root key) and only takes the per-host `_make_outer_for_vps_ip` signature from the branch.

- The provider config docstring names the `imbue_cloud_<user id>` account instance.

- `auth refresh` (and the transparent refresh before every authed call) reports an unreachable connector as `ImbueCloudUnreachableError` and any other connector status as `ImbueCloudConnectorError`; `ImbueCloudAuthError` now means the connector refused the refresh. minds signs an account out only on that class, so an outage during a refresh no longer retires the session; single-device signout still falls back to the stored token and drops the local files offline.

- `SyncDocument.metadata` is required: `sync documents push` sends only the fields set on the document and the server replaces the stored metadata whole, so an unset value would have wiped it.

- The leased host's key layout (`hosts/<host id>/` under the instance dir, holding `ssh_key` and its public half plus the host's own `known_hosts`) is published as `IMBUE_CLOUD_HOST_ACCESS_MANIFEST` (`primitives.py`, with `IMBUE_CLOUD_CLIENT_KEY_NAME` replacing the adoption module's `CLIENT_KEY_NAME`), which the provider's per-host state dir and the `hosts rotate` command's key-material lookup resolve through.

- Unit tests pin the refresh error classes end to end: a refused refresh (the connector's `200 {"status": "ERROR"}`) maps to `ImbueCloudAuthError` through `force_refresh` and reaches `discover_hosts`' caller unwrapped, while a `503 auth_upstream_unavailable` on the refresh maps to `ImbueCloudUnreachableError`.

- The account-document acceptance test carries its own two-minute timeout: its round trip through the real connector takes longer than the default ten-second per-test budget.

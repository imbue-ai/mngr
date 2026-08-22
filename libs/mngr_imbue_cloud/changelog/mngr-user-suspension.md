Account suspension support (issue #550):

- The connector client raises the new typed `ImbueCloudAccountSuspendedError` (carrying the server's message, which includes the support contact) when a structured 403 `code: account_suspended` is returned -- e.g. on the browser-login device-code exchange for a suspended account -- and the CLI error handler surfaces it as a structured JSON failure with `code: account_suspended`.

- New admin client methods for the operator CLI: `admin_revoke_sessions`, `admin_suspend_account`, `admin_unsuspend_account`, and `admin_stop_workspace`. `admin_get_account` now returns the new `AdminAccountInfo` wire model, which adds the operator-only `suspended_at` / `suspended_reason` fields.

- `mngr imbue_cloud auth signin` against a suspended account now shows the server's "account suspended -- contact support@imbue.com" message (the connector answers `ACCOUNT_SUSPENDED` with that message).

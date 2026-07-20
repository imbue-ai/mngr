Add account plan/quota surfaces and adopt the single-key bucket model:

- New `mngr imbue_cloud account show` (plan, entitlement values, live usage) and `account set-plan` commands, plus operator-side `admin account show / set-plan / set-quota` (email-addressed, `MINDS_PAID_ADMIN_KEY`-authenticated).

- `mngr imbue_cloud bucket roll-key <name>` replaces `bucket keys create` / `bucket keys destroy`: each bucket has exactly one key, and rolling returns fresh credentials with the same Access Key ID. `bucket keys list` remains.

- The connector's structured quota rejections surface as `ImbueCloudQuotaExceededError` (carrying entitlement, limit, and current usage) instead of a generic auth error.

Replace the binary paid-account gate with per-account plans and quota entitlements:

- New `plans` and `account_entitlements` tables (migration 014). Plan definitions ("explorer" / "ally") are written from deploy.toml on every deploy; each account gets a lazily-created entitlements row (pre-cutoff paid-listed accounts land on ally, everyone else on explorer) whose values are the adjustable source of truth.

- Every resource grant is quota-checked: pool-host leases (strict, via a per-user advisory lock), tunnels, services per tunnel, buckets, active synced workspace records, and LiteLLM key minting (refused outright at a $0 monthly budget). Rejections are structured 403s (`code: quota_exceeded` with entitlement, limit, current).

- Monthly LLM spend is enforced by LiteLLM user-level budgets (rolling `1mo`), upserted before any key is minted and on every plan/quota change; per-key budgets remain caller-controlled.

- R2 moves to a single key per bucket: `POST /buckets/{name}/roll-key` rolls the secret in place (same Access Key ID, policies untouched); the extra-keys endpoint is removed. An hourly sweep reads per-bucket storage via one GraphQL analytics query, flips over-quota accounts' keys to read-only in place, restores them when back under, and permanently enforces the single-key invariant. The Cloudflare token now also needs `Account Analytics: Read`.

- Public-exposure hardening: every forwarded service gets an Access Application (owner-verified-email fallback when no default policy is set), a failed Access creation rolls the add back, and identity-less auth policies are rejected.

- New endpoints: `GET /account` (plan + entitlements + live usage + available plans), `POST /account/plan` (same-plan no-op; ally requires a paid-listed email), and email-addressed `/admin/accounts/*` (show / set-plan / set-quota, `MINDS_PAID_ADMIN_KEY`-authenticated). The paid lists remain only as ally eligibility input.

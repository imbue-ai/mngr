# Plan: Per-user resource quotas and plans for imbue_cloud

## Overview

- Replace the binary "paid" gate with per-user, per-resource quotas ("entitlements"), enforced exclusively in the remote_service_connector — the single choke point every resource grant already flows through. The paid list survives only as the eligibility input for the "ally" plan.
- Introduce account plans: "explorer" (free alpha default; the analytics-access condition is disclosed pre-download, nothing to build) and "ally" (higher limits, selectable only with a paid-listed email). Plan definitions are git-owned (seeded from each tier's deploy.toml and overwritten on every deploy); a user's entitlements row is copied wholesale from the plan at assignment and is the operator-adjustable source of truth thereafter.
- Enforce each quota at its natural point: creation-time count checks for leases / tunnels / services / buckets / sync records, LiteLLM internal-user budgets (rolling monthly) for LLM spend, and an hourly connector cron that polls R2 usage and flips bucket-key token policies in place (readwrite ↔ read-only, same credentials) for storage.
- Simplify R2 to a single key per bucket (rolling the token secret in place replaces re-minting), and close the public-tunnel exposure hole outright (always-on Access Applications, no entitlement) instead of adding a "public tunnels" feature.
- Surface plan, quotas, and live usage to users (minds accounts page, `mngr imbue_cloud account show`, `GET /account`) and to operators (email-addressed `admin account` CLI + admin-key endpoints).

## Expected behavior

### Plans and entitlements

- Two plans exist at launch, with these quota values (explorer / ally): remote workspaces 2 / 10 · tunnels 50 / 50 · services per tunnel 10 / 10 · total bucket storage 50 GB / 500 GB · buckets 5 / 20 · monthly LLM spend $0 / $1000 · active synced workspaces 200 / 200. All quotas are finite (no NULL/unlimited); the same values are committed to all three tiers' deploy.toml.
- A user's entitlements row is created lazily on their first quota-relevant request: accounts whose SuperTokens `time_joined` predates a fixed ship-time cutoff get "ally" if their email is paid-listed (else "explorer"); accounts created after the cutoff always start as "explorer".
- Switching plans copies the new plan's values wholesale over the user's row (manual per-user bumps are lost); re-selecting the current plan is a no-op (bumps survive idempotent retries). Switching to "ally" errors with a clear reason unless the caller's email is paid-listed. No automatic demotion when an ally's email later leaves the paid list — that's an operator action.
- Quota rejections are HTTP 403 with structured detail — `{"code": "quota_exceeded", "entitlement": "<name>", "limit": N, "current": N}` — plus a human-readable message.
- Quotas are checked at grant time only: lowering a quota below current usage never revokes existing resources (except R2 storage and LLM spend, which have continuous enforcement by design).

### Per-resource enforcement

- **Remote workspaces**: `/hosts/lease` refuses when the user's existing lease count (leased rows, running or stopped — stopped slices still occupy a slot) is at quota. The check is strict under concurrency (per-user advisory lock inside the lease transaction). Release/rename/list only require ownership + auth, no quota.
- **LLM spend**: the connector provisions a LiteLLM internal user with `max_budget` = the user's monthly quota and rolling `budget_duration = "1mo"`; LiteLLM then enforces aggregate spend across all the user's keys at request time. Per-key budgets remain fully user-controlled (that's how users limit individual agents). `/keys/create` refuses with a quota error when the user's monthly budget is $0. A failed LiteLLM budget push fails the whole plan-assignment/quota-change operation, so DB and LiteLLM never diverge. Historical spend counting against a newly provisioned budget is accepted (operators can bump or reset manually).
- **Buckets and storage**: bucket creation checks the bucket-count quota (replacing the flat 50 cap). An hourly cron sums each user's total bucket bytes via the Cloudflare GraphQL analytics dataset; users over their storage quota get every bucket key's token policy flipped to read-only in place (S3 credentials unchanged — reads and restores keep working, writes fail), and flipped back automatically once under quota. Over-quota blocks nothing else. Key-rolling preserves policies, so rolling cannot bypass a downgrade.
- **Single key per bucket**: bucket creation mints the one key; re-provisioning rolls that token's secret in place (`POST /buckets/{name}/roll-key`, `mngr imbue_cloud bucket roll-key`); the extra-keys endpoints and `bucket keys create/destroy` CLI are removed. The hourly sweep permanently enforces the invariant — any bucket with more than one recorded key has the extras revoked (this doubles as the one-time cleanup of existing multi-key buckets, and revokes stale old credentials).
- **Tunnels and services**: `POST /tunnels` refuses at 50 tunnels (live Cloudflare count by user prefix); adding a service refuses at 10 services on that tunnel (works under both admin and agent auth). No periodic tunnel reconciliation — destroy/stop-sharing cascade cleanup is verified correct instead.
- **Sync**: pushing a workspace record refuses when it would create a new ACTIVE record beyond the quota (updates and tombstoning are always allowed).

### Public-exposure hardening (no entitlement)

- Every forwarded service gets a Cloudflare Access Application, unconditionally: when a tunnel has no default auth policy, the service falls back to an allow-only-the-owner's-verified-email policy.
- A failed Access-App creation rolls the service back (removes ingress + DNS) instead of leaving it up publicly.
- Auth-policy writes (tunnel default and per-service) reject policies with no identity constraint — every rule must name emails, email domains, or an IdP; empty rule lists are rejected. Access service tokens (non_identity policies) remain allowed. No repair pass for pre-existing services (none exist without Access Apps).

### Visibility

- `GET /account` (SuperTokens auth; triggers lazy row creation) returns the plan name, all entitlement values, and live usage — lease count (DB), tunnel count (Cloudflare), bucket count + total bytes (per-bucket REST usage calls, real-time), current-period LLM spend and reset date (LiteLLM), and active sync-record count (DB). Always computed live.
- The minds accounts page shows plan + usage/limits per signed-in account, plainly explained, with a plan selector listing all plans; picking an ineligible one errors with the reason. No client-side pre-gating of over-quota actions — server errors are the UX.
- `mngr imbue_cloud account show` exposes the same data on the CLI.
- Operators manage users by email: `mngr imbue_cloud admin account show <email>`, `set-plan <email> <plan>`, `set-quota <email> <name> <value>`, backed by `/admin/accounts/*` endpoints authenticated with the existing `MINDS_PAID_ADMIN_KEY`.

### Behavior changes and compatibility

- minds' "remote" create preset defaults the AI provider to SUBSCRIPTION instead of IMBUE_CLOUD. Old shipped clients that still send IMBUE_CLOUD hit the $0-quota error at key minting — a clear error during alpha is acceptable; the user retries with subscription.
- Tunnel creation and sync pushes happen after the workspace exists, so a user at those caps gets a working workspace whose sharing (or sync) failed with the quota error; creation is not rolled back.
- Existing multi-key buckets keep working until the sweep revokes all but the newest key per bucket.
- Read-only endpoints that were paid-gated (`GET /hosts`, `GET /buckets`, `GET /keys`, etc.) now require only authenticated ownership.
- Entitlements are keyed by full SuperTokens user_id with an indexed 16-hex prefix column, so both admin-auth paths (full id) and agent-auth paths (prefix from the tunnel name) can resolve the row.

## Changes

- **Neon DB (connector migrations)**: new `plans` and per-user entitlements tables with typed columns (one column per quota; storage in bytes), keyed by full user_id + indexed prefix; an enforcement-state column on `r2_keys` (intended vs currently-enforced access). Adding a future entitlement is a migration.
- **remote_service_connector**: replace every `require_paid_account` call with the specific entitlement check; add the lazy row-creation + plan-resolution logic (ship-time cutoff constant, paid-list lookup, LiteLLM budget provisioning); add `GET /account`, `POST /account/plan`, `POST /buckets/{name}/roll-key`, and admin-key `/admin/accounts/*` routes; add the structured 403 quota error; add the hourly R2 sweep cron (GraphQL usage query, policy flip/restore, single-key invariant); remove the extra-keys endpoints; harden the three public-exposure paths; add per-user advisory locking to the lease transaction.
- **modal_litellm**: pin the litellm version (currently unpinned "latest at image build").
- **mngr_imbue_cloud**: new `account` command group (show), `admin account` group (show/set-plan/set-quota, email-addressed), `bucket roll-key`; remove `bucket keys create/destroy`; update data types and the README for the new model.
- **minds**: accounts page shows per-account plan + usage/limits and the plan selector; "remote" preset defaults AI provider to SUBSCRIPTION; backup re-provisioning uses roll-key instead of minting extra keys.
- **deploy tooling**: `[plans]` blocks in each tier's deploy.toml, written (overwriting) to the plans table on every deploy; add `Account Analytics: Read` to the documented Cloudflare token permissions (manual dashboard edit per tier).
- **Tests**: unit/integration coverage via the existing mock-ops pattern, plus deployment-test coverage for the core quota paths (lease cap, $0 key refusal, plan switch) in `apps/minds/deployment_tests/`.
- **Out of scope**: abuse prevention (rate limits, email sends), splitting the workspace quota into running/stopped, tunnel GC/reconciliation UI, post-alpha plan tiers, any technical enforcement of the explorer analytics condition.

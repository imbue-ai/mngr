# Plan: minds new-workspace creation flow fixes

Three unrelated fixes to the minds new-workspace creation flow: post-login redirect, leased-host disassociation guard, and an imbue_cloud region preference.

## Overview

- **Fix 1 — post-login redirect.** After login the user always lands on the account-management page (`/accounts`); we want first-time users (zero workspaces) to land on the create screen instead. Route all sign-in paths through a single backend redirect that branches on workspace count.
- **Fix 2 — block leased-host disassociation.** Hosts leased from imbue_cloud must stay bound to their leasing account to prevent confusing "account mixing." Disassociation (and re-association to a different account) is blocked in both the UI and the backend; non-leased hosts are unaffected.
- **Fix 3 — imbue_cloud region preference.** Add a region *preference* (soft, used by minds) distinct from a region *requirement* (hard, available only to direct `mngr_imbue_cloud` users). The preference must never slow or break the fast path: it orders the pool by region but always falls back to any host in a single lease call.
- **Region selection is automatic and zero-latency for the user.** On create-page open, minds best-effort fetches `ifconfig.co/json`, maps the geo to the nearest OVH-US datacenter, and stores it as the preferred region for subsequent creates — throttled to ~once/hour and never blocking the page.
- **Scope.** Touches `apps/minds`, `libs/mngr_imbue_cloud`, and `apps/remote_service_connector`. `mngr_ovh` already supports per-bake datacenters (`--vps-datacenter`) and needs no change.

## Expected behavior

### Fix 1 — post-login redirect
- After completing sign-in (email/password or OAuth) or sign-up, a user with **zero workspaces** lands on `/` (which renders the create form).
- A user who **already has workspaces** continues to land on `/accounts` (unchanged for returning users).
- The "add another account" flow is a returning user with workspaces, so it continues to land on `/accounts`.
- Discovery still in progress is handled as today (the `/` landing already shows a "discovering…" state before deciding create-form vs list).

### Fix 2 — leased-host disassociation
- A workspace on a host leased from imbue_cloud (provider name prefixed `imbue_cloud_`) shows its bound account on the settings page with the **disassociate control disabled** and a short note that it is leased from imbue_cloud; no associate control is shown.
- Attempting to disassociate or re-associate a leased host via the backend returns **HTTP 403** with a clear message, even if the UI guard is bypassed.
- Non-leased workspaces (DOCKER / LIMA / CLOUD) keep full associate/disassociate behavior.

### Fix 3 — region preference / requirement
- `mngr create` against imbue_cloud accepts an optional **soft** `preferred_region`: the pool prefers an available host in that region but always returns a host if any exists (fast path preserved, one lease call).
- `mngr create` against imbue_cloud accepts an optional **hard** `region`: only a host in that region is returned; if none is available the lease fails. The requirement is enforced on both the fast path and the slow-path rebuild.
- An unknown hard `region` value fails early with a validation error listing the known OVH-US datacenters.
- minds only ever sends `preferred_region`, never the hard `region`.
- On create-page open, minds updates its stored preferred region from `ifconfig.co/json` geo, choosing the nearer of `US-EAST-VA` / `US-WEST-OR`; if the user is far from both, it still picks the nearer one.
- The geo fetch is fully non-blocking: it adds no latency to the create page, runs at most ~once per hour per process, and on failure or unusable data leaves any previously stored preference untouched.

## Changes

### Fix 1 — post-login redirect (`apps/minds`)
- Add a single backend post-login redirect endpoint in the desktop client that resolves the signed-in user's workspace count and 302s to `/` when zero, otherwise `/accounts`.
- Point all client-side sign-in/sign-up success handlers (`static/auth.js` — the email/password, OAuth, and signup paths) at that one endpoint instead of hardcoding `/` or `/accounts`.
- Reuse the existing workspace-count source used by the `/` landing (the backend resolver's known-workspace listing), including its in-progress-discovery handling.

### Fix 2 — leased-host disassociation guard (`apps/minds`)
- Surface a "is this workspace on a leased imbue_cloud host" flag to the settings page, derived from the discovered workspace's provider name prefix (`imbue_cloud_`).
- In the settings template, when the host is leased: render the account as bound, show the disassociate control disabled with an explanatory note, and omit the associate control.
- In the backend associate and disassociate routes, reject the operation with HTTP 403 when the target workspace is on a leased imbue_cloud host.

### Fix 3 — region requirement + preference

**Connector (`apps/remote_service_connector`)**
- Add a `region` column to `pool_hosts` (new migration), set when a host is baked; no backfill (all pool hosts will be rebaked).
- Extend the lease request with two optional fields: a hard `region` (adds an equality filter to the lease query) and a soft `preferred_region` (adds an `ORDER BY` that prefers a region match but never filters it out).
- Keep the lease a single query/round-trip so the fast path is unaffected.

**Pool provisioning (`libs/mngr_imbue_cloud` admin pool add)**
- Persist the bake `--region` (the OVH datacenter) into the new `pool_hosts.region` column at insert time.

**Client (`libs/mngr_imbue_cloud`)**
- Add hard `region` and soft `preferred_region` to the lease attributes/request model and the `-b` build-arg parser (e.g. `-b region=...`, `-b preferred_region=...`).
- Validate a hard `region` against the known OVH-US datacenter set, failing early on an unknown value.
- Preserve a hard `region` through the slow path's relaxed-attributes rebuild; have `preferred_region` influence ordering on both fast and slow paths.

**minds (`apps/minds`)**
- Add a region preference entry to the minds settings file (`~/.minds/config.toml`) with getter/setter on the config manager.
- Add a best-effort geo→region resolver: fetch `ifconfig.co/json`, compute nearest of the two OVH-US datacenters by great-circle distance from hardcoded approximate DC coordinates.
- On create-page open, trigger the resolver non-blocking, throttled by an in-process (non-persisted) last-fetch timestamp to ~once/hour; write only the resolved preferred region to settings; leave the prior value untouched on failure.
- When building the imbue_cloud `mngr create` command, pass the stored preferred region as `-b preferred_region=...` (never the hard `region`).

### Cross-cutting
- Add one changelog entry per touched project: `apps/minds`, `libs/mngr_imbue_cloud`, and `apps/remote_service_connector` (`<project>/changelog/mngr-create-fixes.md`).

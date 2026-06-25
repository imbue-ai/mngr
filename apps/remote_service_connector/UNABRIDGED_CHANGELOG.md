# Unabridged Changelog - remote_service_connector

Full, unedited changelog entries consolidated nightly from individual files in `apps/remote_service_connector/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-24

Updated the `cleanup_released_hosts.py` operator docstring for the new "split" Vault secret layout: OVH credentials are now sourced per key from `secrets/minds/<tier>/ovh/<KEY>` (value at `.data.data.value`) and the host-pool DSN from `secrets/minds/<tier>/neon/DATABASE_URL`, instead of the old flat single-entry layout.

## 2026-06-23

The host-lease and slice-teardown paths now pin SSH host keys instead of trust-on-first-use.

Added nullable `outer_host_public_key` / `container_host_public_key` columns to `pool_hosts` and `box_host_public_key` to `bare_metal_servers` (migration 011). `POST /hosts/lease` returns both pool-host keys and injects the user's key over SSH while strictly verifying each sshd against its recorded host key; a row missing its keys is not leasable (503, pointing at the one-time backfill). `GET /hosts` also returns the keys. Slice teardown and the reconcile sweep verify the bare-metal box against its recorded host key. The management SSH client no longer uses `AutoAddPolicy`.

## 2026-06-21

The hourly pool-host cleanup cron now also audits each bare-metal box's lima slices against the pool database, scoped to this deployment's own environment (via `MINDS_ENV_NAME`).

It logs two kinds of divergence: a slice stamped for this env that is present on a box but has no database row, and a database row whose VM has vanished from its box. The audit is alert-only -- it never auto-deletes (a row-less stamped slice is usually a bake mid-flight, and this cron runs independently of bakes, so deleting here could race a live bake). Actual orphan reaping stays with the bake-time reaper. Other environments' slices and legacy un-stamped slices are never inspected, so the audit is safe on a box shared by multiple dev environments.

## 2026-06-15

OVH bare-metal slices support:

- Added two `host_pool` migrations: `008_bare_metal_servers.sql` (a new table tracking rented OVH dedicated servers and their resumable lifecycle) and `009_pool_host_slice_columns.sql` (adds `backend_kind`, `bare_metal_server_id`, `lima_instance_name`, and `lima_disk_name` to `pool_hosts` so a pool host can be either a real OVH VPS or a lima-VM "slice"). Existing rows default to `backend_kind = 'ovh_vps'`; leasing is unchanged.

- The release path (`release_host`) and the cleanup sweep now branch on `backend_kind`: a real VPS is still cancelled in OVH, while a slice has its lima VM (and btrfs data disk) destroyed by SSHing the owning bare-metal box and running `limactl`. A slice whose VM cannot be destroyed keeps its row in `removing` so the slot is only freed once the VM is really gone.

- Added migration 010 (`bare_metal_servers`: `disk_gb`, `memory_per_slice_gb`, `cpu_overcommit_ratio`) so a box's per-slice sizing is stored rather than hardcoded. Admin-only columns; the connector does not read them.

## 2026-06-11

Replaced a direct RuntimeError raise in the app with a dedicated custom exception type.

## 2026-06-10

Raised the stale coverage floor from 45% to 80% to match the coverage CI already measures (~83%).

## 2026-06-08

Region-aware host leasing.

- New migration `007_pool_host_region.sql` adds a nullable `region` column to `pool_hosts` (the OVH datacenter the pool VPS was baked in). Rows baked before this migration carry NULL and act as non-preferred fallback until rebaked.
- `POST /hosts/lease` accepts two optional fields: a hard `region` (adds an equality filter, so only hosts in that datacenter are eligible) and a soft `preferred_region` (adds an `ORDER BY` that prefers a matching region while still falling back to any available host). Both are independent of the existing JSONB attribute filter, and the lease stays a single query so the fast path is unaffected.

- The `POST /hosts/lease` endpoint no longer accepts `preferred_region`. Leases
  are constrained only by the optional hard `region` field (equality match);
  when unset, the lease is region-agnostic.

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-06-03

Releasing a leased pool host now actually cleans it up instead of just marking it `released`. The release route runs a best-effort, idempotent chain inline: it flips the row to a new `removing` status, strips the per-lease OVH IAM tags (`minds_env`, `mngr-host-id`) while keeping `mngr-provider=ovh`, cancels the VPS in OVH (`deleteAtExpiration=true`, by service name), then deletes the DB row -- leaving the host recyclable for the next pool bake. The release returns 200 as soon as the row reaches `removing` (so OVH flakiness never blocks the caller) and treats an already-gone row as `already_released`. A new hourly Modal cron (`cleanup_removing_pool_hosts`) mops up any row left in `removing` by a crashed/timed-out release. OVH calls are made directly via the official `ovh` SDK (added to the image), and the connector now receives an `ovh-<tier>` Modal secret. The `cleanup_released_hosts.py` script was rewritten into a broad, dry-run-by-default operator runbook that tag-scans the OVH account and cleans every `mngr-provider` VPS (protecting those backing an `available`/`leased` row unless `--include-active`).

Fixed a pool-host teardown bug where released VPSes were never actually
cancelled (they kept running and billing) with no error surfaced anywhere.

Root cause: the bake wrote the mngr `host_id` into `pool_hosts.vps_instance_id`
instead of the OVH service name, so every connector OVH teardown call
(`vps_urn_for` / `set_delete_at_expiration`) targeted a nonexistent service and
404'd -- and the failure was swallowed into a warning while the release reported
success.

- `POST /hosts/{id}/release` is now **synchronous**: it strips the per-lease OVH
  tags, cancels the VPS, and deletes the row, and returns 200 only when every
  step succeeds. On failure it returns 5xx and leaves the row `removing` so the
  client (or the hourly sweep backstop) retries. `_finish_releasing_pool_host`
  no longer swallows OVH/DB errors -- a release that can't cancel the VPS reports
  failure instead of a false success. Added `PoolHostCleanupError` and mapped it
  plus `OvhApiError`/`OvhHttpError` in `raise_as_http`.
- `cleanup_released_hosts.py` now keys its active-row protection and its
  cleaned-host DB match on `vps_address` (the real OVH service name), not
  `vps_instance_id`. Previously the mismatch meant the runbook protected nothing
  and would have cancelled live leased/available hosts.
- New migration `006_fix_vps_instance_id.sql` backfills existing rows whose
  `vps_instance_id` still holds a `host-...` id.

Replaced the `PAID_ACCOUNT_SUFFIXES` env-var allowlist with two database tables (`paid_domains`, `paid_emails`) for tracking paid users. A caller is "paid" when their verified email has an active (`is_paid = true`) row matching the full email or its exact domain. The check is cached in-memory (configurable via `MINDS_PAID_LIST_CACHE_TTL_SECONDS`, default 60s, `0` disables) and fails closed on database errors. Added admin-key-authenticated CRUD endpoints (`/paid/domains/*`, `/paid/emails/*`) gated by `MINDS_PAID_ADMIN_KEY` (folded into the `supertokens` secret); these endpoints reject SuperTokens/tunnel tokens, and the key is rejected on all other routes. Removals are soft deletes (`is_paid = false`) so paid history is retained. Added migration `005_paid_lists.sql`.

Also added a configurable `scaledown_window` to the connector Modal function, driven by `MINDS_CONNECTOR_SCALEDOWN_WINDOW` (from the tier's `[scaledown_window].connector` in `deploy.toml`). `0` (default) keeps Modal's own default; dev tiers set it high (~10 min) so the no-warm-pool connector stays hot across a dev session.

## 2026-05-29

Added R2 bucket routes (`/buckets/*` and `/bucket-keys/*`), gated to paid
accounts. Supports creating a bucket with a default scoped key, listing /
inspecting / destroying buckets, and minting / listing / revoking additional
bucket-scoped keys (read-only or read-write).

Each key is an account-owned Cloudflare API token scoped to a single bucket; the
S3 Access Key ID is the token id and the Secret Access Key is the SHA-256 of the
token value (returned once, never stored). Only key metadata is persisted, in a
new `r2_keys` table (migration `004_r2_keys.sql`); buckets are listed straight
from the R2 API with an in-code owner-prefix re-check. Destroying a bucket
refuses if it is non-empty and otherwise cascades to revoke its keys.

Operator note: `CLOUDFLARE_API_TOKEN` must now be an account-owned (`cfat_`)
token with `Workers R2 Storage: Edit` + `Account API Tokens: Edit` added, and R2
must be enabled on the Cloudflare account. See the README for the full migration.

The connector's R2 bucket + bucket-key endpoints are now exercised end-to-end
by the minds workspace-creation flow (via `mngr imbue_cloud bucket ...`) to
provision per-workspace restic backup buckets.

(This integration PR adds no code in this project; it wires the existing
bucket endpoints into minds. The endpoints themselves are covered by the
`mngr-cloud-bucket` changelog entry.)

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# supertokens floor bump + ratchet count tightening

- Raised the `supertokens-python` floor from `>=0.27.0` to `>=0.31.3`. During the repo-wide `uv lock --upgrade`, the resolver would otherwise backtrack `supertokens-python` to 0.30.3 (an auth-library downgrade, which also caps `aiosmtplib<4`) in order to keep `packaging` at 26; the floor keeps it at the latest 0.31.3, leaving `aiosmtplib` at 5.x and `packaging` at 25 (immaterial).
- Tightened the violation counts recorded in `test_ratchets.py` to their current exact values (via `uv run pytest --inline-snapshot=trim`), locking in previously-unrecorded reductions. No source-code or behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

- `_authenticate_supertokens` now passes
  ``override_global_claim_validators=lambda *_: []`` to the SuperTokens
  session getter so the explicit ``if not is_verified: raise 401
  "Email not verified"`` check fires for unverified tokens instead of
  being shadowed by the SDK's generic ``Invalid token`` rejection. The
  matching ``_get_user_id_from_access_token`` helper also skips claim
  validation so flows like ``/auth/session/revoke`` (sign-out) work
  for unverified users -- they legitimately need to sign out of a
  session they never finished verifying. (F6)
- Connector exposes a new no-auth ``GET /health/liveness`` route
  returning ``{"status": "ok"}``. (F2)
- ``DELETE /tunnels/{name}`` and ``POST /hosts/{id}/release`` are now
  idempotent at the HTTP layer: a second call against an already-
  deleted tunnel or already-released host returns 200 with
  ``{"status": "already_deleted"}`` / ``{"status": "already_released"}``
  instead of 404. Clients retrying after a transient error no longer
  have to special-case 404. (F7, F30)
- Renamed `vps_ip` -> `vps_address` end-to-end: API models
  (`LeaseResult`, `LeasedHostInfo`, `LeaseHostResponse`), all Python
  call sites, AND the `pool_hosts.vps_ip` DB column. Migration ships
  as `apps/remote_service_connector/migrations/003_vps_address.sql`
  (idempotent rename). The field can hold a public IPv4 or a DNS
  hostname (e.g. OVH's `vps-eec8860b.vps.ovh.us`).
- Connector auth endpoints no longer 500 on `/auth/session/revoke`,
  `/auth/email/is-verified`, `/auth/email/send-verification`. The connector's
  twelve `async def` endpoints (plus the `_build_session_tokens` helper)
  have been converted to sync `def`, with the SuperTokens recipe imports
  switched from their `asyncio` modules to the `syncio` equivalents. The
  three broken endpoints were calling SuperTokens' `syncio.get_user` /
  `syncio.get_session_without_request_response` from inside an
  `async def`, where the syncio wrapper's `loop.run_until_complete` hit
  "RuntimeError: This event loop is already running" against the live
  FastAPI/uvicorn loop and produced bare 500s. The conversion makes the
  bug class structurally impossible (no event loop is running in
  FastAPI's threadpool workers) and also aligns the file with the
  monorepo style guide's prohibition on `async`/`asyncio`. Each
  newly-sync endpoint is wrapped in `with handle_endpoint_errors():` so
  error handling stays uniform across the file. The two OAuth callback
  endpoints still need to bridge to async-only methods on SuperTokens'
  `Provider` object (`get_authorisation_redirect_url`,
  `exchange_auth_code_for_oauth_tokens`, `get_user_info`); those three
  calls go through `supertokens_python.async_to_sync_wrapper.sync`, the
  same wrapper SuperTokens' own syncio modules use internally -- safe
  here because FastAPI runs sync def endpoints in a threadpool worker
  with no live event loop.

## 2026-05-06

- remote_service_connector: `add_service` is now idempotent. Updating the access list on a previously-shared service (which re-runs the full create-tunnel/add-service/set-auth chain) no longer fails with Cloudflare error 81053 ("DNS record already exists"). When a CNAME or ingress rule for the hostname is already in place pointing at the same tunnel, it is reused; if the per-service auth policy was already customized via `set_service_auth`, the tunnel default is no longer reapplied on top.

- Connector schema migration: `pool_hosts.version` is replaced with a flexible `attributes JSONB` column. `/hosts/lease` matches with `attributes @> request_attributes`. Backwards-compatible: legacy callers can still pass `version` as a top-level field; the connector folds it into attributes automatically.

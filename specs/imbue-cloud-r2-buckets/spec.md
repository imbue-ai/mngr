# Imbue Cloud R2 Bucket Support

## Overview

- Add **R2 bucket support** to the `mngr_imbue_cloud` provider so users can store files remotely on Cloudflare R2.
- Primary eventual use case is letting minds users back up their workspaces, but the feature is **totally generic** and lives at a lower level of abstraction (not coupled to minds or to backups).
- Each "unit of isolation" is a **whole separate bucket**, not a path prefix. R2 has no persistent, prefix-scoped credentials, so we sidestep that entirely: one bucket per host the user makes, each with its own bucket-scoped S3 credential.
- Users can mint **multiple S3 API keys per bucket**, each scoped read-only or read-write, and can create/destroy them via the CLI. This lets users "chop up" storage across agents (give different agents different keys/buckets, and keep data they shouldn't see out of reach).
- Keys are minted as **account-owned Cloudflare API tokens** scoped to a single bucket. We **track key metadata in our own DB** (so we can list/revoke), but never persist the secret/token value.
- All routes require a **paid account** (reuse the existing `require_paid_account` gate).
- New CLI surface: `mngr imbue_cloud bucket create|list|info|destroy` plus `mngr imbue_cloud bucket keys create|list|destroy`. The CLI emits credentials as JSON; there is **no `mngr create` auto-injection** in v1.

### Key design decisions (from Q&A)

- **No prefix scoping.** Separate bucket per host; each bucket gets its own scoped credential.
- **Bucket naming / ownership.** The server derives the real R2 name as `<user_id_prefix>--<slug>` where `user_id_prefix` is the 16-hex-char SuperTokens prefix already used for tunnel ownership. The user only ever passes/sees their **short** name; the full R2 name/URL is shown back in responses.
- **Listing is double-checked.** Buckets are listed via the R2 `name_contains` filter, then **re-verified in our code** to start with `<user_id_prefix>--` (mirrors the tunnel `startswith` ownership check) so a crafted bucket name cannot grant cross-user access.
- **Credential model.** Access Key ID = the Cloudflare token `id`; Secret Access Key = `sha256(token value)`. The token value is a secret: returned to the user once at creation and never persisted.
- **State.** Buckets are not tracked in our DB (listed from the R2 API). Keys **are** tracked in a new `r2_keys` table in the connector's existing Neon DB.
- **Single broadened Cloudflare token.** Reuse the existing `CLOUDFLARE_API_TOKEN`, widened to include R2 admin + "API Tokens Write" (rather than a separate secret). Existing deployed tiers must have this widened manually -- see Migration / rollout.
- **Cap.** A hard-coded limit (≈50) on **buckets per account** prevents unbounded resource creation (e.g. forgotten CI cleanup). Keys-per-bucket are unbounded in v1.

## Expected Behavior

### Buckets

- `mngr imbue_cloud bucket create <name> [--access read|readwrite] [--account ...] [--connector-url ...]`
  - Derives the full R2 name `<user_id_prefix>--<slug(name)>`, creates the bucket, and **mints one default key** (default `readwrite`, fixed alias `"default"`).
  - Emits JSON with the bucket info **and** the default key's credentials inline (S3 endpoint, full bucket name, access key id, secret) -- one round trip.
  - **Errors** if the derived bucket already exists for that user (not idempotent).
  - **Errors** if the user is at the per-account bucket cap.
  - **Errors** if the slugified name violates R2 rules (3-63 chars, lowercase alphanumeric + hyphens, no leading/trailing hyphen) after the prefix is prepended.
- `mngr imbue_cloud bucket list` -- lists all of the caller's buckets (full R2 name + S3 endpoint), filtered via `name_contains` then re-verified by prefix.
- `mngr imbue_cloud bucket info <name>` -- returns bucket metadata only (full R2 name, S3 endpoint). Keys come from `bucket keys list`.
- `mngr imbue_cloud bucket destroy <name>`
  - Refuses a **non-empty** bucket: relayed as `409` / `ImbueCloudBucketNotEmptyError` ("empty it first"). No `--force`.
  - On success, **cascades**: revokes all of the bucket's Cloudflare tokens and deletes their `r2_keys` rows.

### Keys

- `mngr imbue_cloud bucket keys create <bucket-name> [--alias ...] [--access read|readwrite]` (default `readwrite`)
  - Mints a bucket-scoped account-owned API token; records metadata in `r2_keys`; emits the new key's creds as JSON (access key id, secret, S3 endpoint, full bucket name, access).
- `mngr imbue_cloud bucket keys list [<bucket-name>]`
  - Default **account-wide** (all the caller's keys across buckets, bucket shown per row); optional bucket filter.
  - Reads from the DB; never shows secrets.
- `mngr imbue_cloud bucket keys destroy <access-key-id>`
  - The Access Key ID (= token id) is the user-facing handle. Revokes the Cloudflare token and deletes the DB row. Verifies ownership first.

### Cross-cutting

- All routes require a paid account; non-paid callers get `403` (existing `require_paid_account`).
- All new CLI commands accept `--account` (defaults to the active account) and `--connector-url`, like every existing subcommand.
- Errors are surfaced as structured JSON via the existing `handle_imbue_cloud_errors` decorator + `fail_with_json`.
- Ownership is always enforced server-side: a user can only see/operate on buckets and keys whose owner prefix matches their session.

## Implementation Plan

### Connector -- `apps/remote_service_connector/imbue/remote_service_connector/app.py`

This file is intentionally self-contained (stdlib + 3rd-party only, no monorepo imports) and uses local `Protocol` abstractions + plain classes. Follow that local style here (NOT the mngr style guide).

- **Request/response models** (pydantic `BaseModel`):
  - `CreateBucketRequest { name: str, access: str = "readwrite" }`
  - `BucketInfo { bucket_name: str, s3_endpoint: str }`
  - `CreateBucketResponse { bucket: BucketInfo, key: KeyMaterial }`
  - `KeyMaterial { access_key_id: str, secret_access_key: str, s3_endpoint: str, bucket_name: str, access: str }`
  - `CreateKeyRequest { alias: str | None = None, access: str = "readwrite" }`
  - `KeyInfo { access_key_id: str, bucket_name: str, access: str, alias: str | None, created_at: str }`
  - Validate `access` against `{"read", "readwrite"}`; validate `name` with a field validator that rejects names which (after prefixing) break R2 rules.
- **Naming helpers** (mirror the existing tunnel helpers):
  - `make_bucket_name(username, short_name) -> str` -> `f"{username}--{slug}"` with a `slugify` (lowercase, alphanumeric + single hyphens, collapse runs).
  - `verify_bucket_ownership(bucket_name, username)` -> raise on missing `f"{username}--"` prefix.
  - `_validate_r2_bucket_name(name)` -> enforce 3-63, lowercase alnum + hyphen, no leading/trailing hyphen.
  - Constant `_MAX_BUCKETS_PER_ACCOUNT = 50`.
  - `s3_endpoint()` -> `https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com`.
  - `derive_s3_secret(token_value) -> hashlib.sha256(token_value.encode()).hexdigest()`.
- **R2 ops abstraction** (new `R2Ops` Protocol + `HttpR2Ops` impl, parallel to `CloudflareOps`/`HttpCloudflareOps`):
  - `create_bucket(name)` -> `POST /accounts/{acct}/r2/buckets`
  - `list_buckets(name_contains)` -> `GET /accounts/{acct}/r2/buckets?name_contains=...` (paginate via cursor)
  - `get_bucket(name)` -> `GET /accounts/{acct}/r2/buckets/{name}` (None on 404)
  - `delete_bucket(name)` -> `DELETE /accounts/{acct}/r2/buckets/{name}` (raises a typed `R2BucketNotEmptyError` when CF reports non-empty)
  - `create_bucket_token(bucket_name, access, token_name)` -> `POST /accounts/{acct}/tokens` with an R2 bucket-scoped policy (resource `com.cloudflare.edge.r2.bucket.<ACCOUNT_ID>_default_<BUCKET_NAME>`) and the read vs write **permission group** id; returns `{id, value}`.
  - `delete_token(token_id)` -> `DELETE /accounts/{acct}/tokens/{token_id}`
  - Permission-group UUIDs ("Workers R2 Storage Bucket Item Read" / "...Write") are looked up once or hard-coded as constants (note in Open Questions).
- **Key store abstraction** (new `KeyStore` Protocol so DB-backed endpoints are unit-testable, parallel to `CloudflareOps`):
  - `add_key(access_key_id, owner, bucket_name, access, alias, created_at)`
  - `list_keys(owner, bucket_name: str | None) -> list[KeyRecord]`
  - `get_key(access_key_id) -> KeyRecord | None`
  - `delete_key(access_key_id)`
  - `delete_keys_for_bucket(owner, bucket_name) -> list[KeyRecord]` (returns revoked rows so the endpoint can revoke their CF tokens)
  - `count_buckets_owned(...)` is NOT needed (bucket count comes from R2 list).
  - Implementations: `PostgresKeyStore` (psycopg2 against `DATABASE_URL`, same DB as `pool_hosts`) and `InMemoryKeyStore` (test mock in `app_test.py`).
- **Errors**: `R2BucketNotEmptyError`, `R2BucketExistsError`, `R2BucketNotFoundError`, `R2BucketLimitError`; extend `raise_as_http` to map them to `409`/`404`/`409`/`409` respectively.
- **Endpoints** (all: `authenticate_request` -> `require_admin` -> `require_paid_account`):
  - `POST /buckets` -> create bucket (cap check via `list_buckets` + prefix re-verify; create bucket; mint default key; record; return `CreateBucketResponse`).
  - `GET /buckets` -> list buckets (filter + prefix re-verify).
  - `GET /buckets/{name}` -> bucket info (ownership check).
  - `DELETE /buckets/{name}` -> verify ownership; delete bucket (relay non-empty); cascade revoke tokens + delete key rows.
  - `POST /buckets/{name}/keys` -> verify ownership; mint token; record; return `KeyMaterial`.
  - `GET /buckets/keys` (account-wide) and `GET /buckets/{name}/keys` (per bucket) -> list `KeyInfo` from the store.
  - `DELETE /buckets/keys/{access_key_id}` -> verify the key's owner == caller; revoke token; delete row.
- **Context wiring**: extend `get_ctx()` (or add a sibling cached factory) to build `HttpR2Ops` + `PostgresKeyStore` from env.

### Connector -- migration

- `apps/remote_service_connector/migrations/004_r2_keys.sql`: create table `r2_keys`:
  - `access_key_id TEXT PRIMARY KEY` (= Cloudflare token id)
  - `owner_user_id TEXT NOT NULL` (the 16-hex username prefix)
  - `bucket_name TEXT NOT NULL` (full R2 name)
  - `access TEXT NOT NULL CHECK (access IN ('read','readwrite'))`
  - `alias TEXT`
  - `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`
  - Index on `(owner_user_id)` and `(owner_user_id, bucket_name)`.
  - **Never** stores the token value / secret.

### Plugin -- `libs/mngr_imbue_cloud`

- `primitives.py`:
  - `R2BucketShortName(NonEmptyStr)` (client-side light validation), `R2AccessKeyId(NonEmptyStr)`.
  - `R2BucketAccess(UpperCaseStrEnum)` with `READ` / `READWRITE` (serialize to `read`/`readwrite` at the wire boundary).
- `data_types.py`:
  - `R2BucketInfo { bucket_name: str, s3_endpoint: AnyUrl }`
  - `R2KeyMaterial { access_key_id: str, secret_access_key: SecretStr, s3_endpoint: AnyUrl, bucket_name: str, access: R2BucketAccess }`
  - `R2KeyInfo { access_key_id: str, bucket_name: str, access: R2BucketAccess, alias: str | None, created_at: str }`
  - `R2BucketCreateResult { bucket: R2BucketInfo, key: R2KeyMaterial }`
- `errors.py`: `ImbueCloudBucketError`, `ImbueCloudBucketNotEmptyError`, `ImbueCloudBucketExistsError`, `ImbueCloudBucketNotFoundError`, `ImbueCloudBucketLimitError` (all subclass `ImbueCloudError`).
- `client.py` (`ImbueCloudConnectorClient`): add `create_bucket`, `list_buckets`, `get_bucket_info`, `destroy_bucket`, `create_bucket_key`, `list_bucket_keys`, `destroy_bucket_key`. Map `409`/`404` bodies to the typed errors above (extend `_check` or add a small dispatch).
- `cli/buckets.py`: new `bucket` click group (`create` / `list` / `info` / `destroy`) + nested `keys` group (`create` / `list` / `destroy`). Reuse `make_connector_client`, `make_session_store`, `resolve_account_or_active`, `get_active_token`, `emit_json`, `handle_imbue_cloud_errors`, `--account` / `--connector-url`.
- `cli/root.py`: `imbue_cloud.add_command(bucket)`.

### Documentation + secrets

- `.minds/template/cloudflare.sh`: update the `CLOUDFLARE_API_TOKEN` comment to note it now also needs R2 admin + "API Tokens Write".
- `apps/remote_service_connector/README.md`: document the new `/buckets/*` routes and the broadened token requirement (with the migration note).
- `libs/mngr_imbue_cloud/README.md`: document the `bucket` command group.
- Changelog entries for both touched projects (`libs/mngr_imbue_cloud`, `apps/remote_service_connector`); add `dev` only if root-level files like `.minds/template` count as `dev`.

### Migration / rollout (manual, operator action)

- **Broaden `CLOUDFLARE_API_TOKEN`** for every already-deployed tier (dev/staging/production) to add R2 admin + "API Tokens Write" before these routes will work. This is a manual Cloudflare dashboard / Vault update; capture it in the PR description and deploy runbook.

## Implementation Phases

Each phase ends in a working (if incomplete) system.

1. **Connector R2 ops + bucket lifecycle (no keys).** Add models, naming helpers, `R2Ops`/`HttpR2Ops`, and `POST/GET/DELETE /buckets` (+ `GET /buckets/{name}`). Cap enforcement, ownership re-verify, non-empty relay. Mock `R2Ops` in tests. No key minting yet (bucket create returns bucket only, temporarily).
2. **Connector key store + key endpoints.** Add `migrations/004_r2_keys.sql`, `KeyStore`/`PostgresKeyStore`/`InMemoryKeyStore`, token minting, `/buckets/{name}/keys` + `/buckets/keys` + `DELETE /buckets/keys/{id}`. Wire `bucket create` to mint the default key inline and `bucket destroy` to cascade.
3. **Plugin data types + client.** Add primitives, data_types, errors, and `ImbueCloudConnectorClient` methods with typed error mapping.
4. **Plugin CLI.** Add `cli/buckets.py` and register it in `root.py`. Manually verify end-to-end against a real connector + real R2.
5. **Docs, secret template, changelogs, migration note.** Update READMEs, `cloudflare.sh`, and write changelog entries.

## Testing Strategy

- **Connector unit/integration tests** (`app_test.py`), matching the existing tunnel/LiteLLM approach:
  - Drive endpoints against a **mock `R2Ops`** + **`InMemoryKeyStore`** -- no live Cloudflare/R2 in CI.
  - Naming/ownership: `make_bucket_name` slugify edge cases; `_validate_r2_bucket_name` boundaries (2/3/63/64 chars, leading/trailing hyphen, uppercase); cross-user access attempt via a crafted name is rejected by the prefix re-check.
  - Cap: creating the 51st bucket returns the limit error.
  - Create returns bucket + default key; secret never appears in any list/info response; DB row never contains the secret.
  - Destroy: non-empty -> `409`; empty -> cascades (mock R2 token deletes + key rows gone).
  - Paid gate: non-paid caller -> `403` on every route.
  - Key ownership: deleting another user's key -> `403`/`404`.
  - `derive_s3_secret` matches the known `sha256(value)` contract.
- **Plugin unit tests**: client error mapping (`409`/`404` -> typed errors); data_type (de)serialization incl. `R2BucketAccess` <-> `read`/`readwrite`; CLI arg parsing / JSON output shape (using a fake/served connector or the existing test fixtures).
- **Migration test**: extend the existing "insert has required columns" style check so the `r2_keys` schema and the `add_key` INSERT can't drift.
- **Manual verification** (during development, not crystallized): real connector + real R2 -- create bucket, mint read-only + read-write keys, confirm S3 access honors the scope, destroy fails while non-empty, succeeds after emptying, tokens actually revoked in Cloudflare.
- **Edge cases**: duplicate bucket name; slug collapsing two distinct names to the same slug; very long user short name overflowing 63 chars after prefix; CF token-create failure mid-`bucket create` (bucket created, key failed) -- decide whether to roll back the bucket (see Open Questions).
- Run the full suite via `just test-offload`; iterate locally with `just test-quick`.

## Open Questions

- **R2 permission-group UUIDs.** The read vs read-write token policy needs Cloudflare's "Workers R2 Storage Bucket Item Read/Write" permission-group IDs. Hard-code them as constants, or look them up at runtime via `GET /accounts/{acct}/tokens/permission_groups` and cache? (Lean: hard-code with a comment; they're stable account-wide.)
- **Partial-failure rollback on `bucket create`.** If the bucket is created but the default-key token mint fails, do we delete the just-created bucket to keep create atomic, or leave the empty bucket and surface the error? (Lean: best-effort delete the bucket, then raise.)
- **Jurisdiction in the token resource string.** The bucket-scoped policy resource is `com.cloudflare.edge.r2.bucket.<ACCOUNT_ID>_<JURISDICTION>_<BUCKET_NAME>`. Since v1 uses the default jurisdiction, confirm the literal segment is `default` (verify against a live token before shipping).
- **`dev` changelog.** Confirm whether editing `.minds/template/cloudflare.sh` requires a `dev/changelog/<branch>.md` entry in addition to the two project entries.
- **Owner identity stored in `r2_keys`.** Plan stores the 16-hex username prefix (consistent with bucket ownership). Confirm we don't also want the full SuperTokens user id for auditing.

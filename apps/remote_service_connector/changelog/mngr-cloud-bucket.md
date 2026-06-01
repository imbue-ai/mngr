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

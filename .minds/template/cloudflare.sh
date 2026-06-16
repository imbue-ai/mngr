# Template for the `cloudflare-<env>` Modal secret.
#
# When adding or removing a variable here, mirror the change in every per-env
# file (e.g. .minds/production/cloudflare.sh). `scripts/push_modal_secrets.py`
# treats this file as the canonical list of expected keys and errors out if
# the target env file is missing any of them.
#
# Fill in values in a per-env copy, not here. Empty values are skipped on push
# (an empty `export KEY=` line declares the key but leaves it unset on Modal).

# Cloudflare API token. Must be an account-owned token (cfat_), NOT a
# user-owned token (cfut_), because the connector mints account-owned per-bucket
# R2 tokens on the user's behalf. Required permissions: Cloudflare Tunnel: Edit,
# DNS: Edit, Access: Apps and Policies: Edit, Access: Service Tokens: Edit,
# Workers KV Storage: Edit, Workers R2 Storage: Edit, Account API Tokens: Edit.
# R2 must also be enabled on the Cloudflare account for the bucket routes to work.
export CLOUDFLARE_API_TOKEN=

# Cloudflare account ID.
export CLOUDFLARE_ACCOUNT_ID=

# Cloudflare zone ID for DNS records.
export CLOUDFLARE_ZONE_ID=

# Base domain for service subdomains (e.g. example.com).
export CLOUDFLARE_DOMAIN=

# Optional: comma-separated list of Cloudflare identity provider UUIDs to
# allow on Access Applications (e.g. Google OAuth, one-time PIN). When unset,
# Cloudflare uses the account default.
export CLOUDFLARE_ALLOWED_IDPS=

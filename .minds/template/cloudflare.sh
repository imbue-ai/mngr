# Template for the `cloudflare-<env>` Modal secret.
#
# When adding or removing a variable here, mirror the change in every per-env
# file (e.g. .minds/production/cloudflare.sh). `scripts/push_modal_secrets.py`
# treats this file as the canonical list of expected keys and errors out if
# the target env file is missing any of them.
#
# Fill in values in a per-env copy, not here. Empty values are skipped on push
# (an empty `export KEY=` line declares the key but leaves it unset on Modal).

# Cloudflare API token with Tunnel Write and DNS Write permissions.
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

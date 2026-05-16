# Template for the `neon-admin` Vault entry at
# `secrets/kv/minds/<tier>/neon-admin`.
#
# These credentials are read by `minds env create` to provision per-dev-env
# Neon databases. They are NOT pushed to Modal -- the connector's runtime
# only needs the per-tier `DATABASE_URL` from the `neon` entry, not API
# tokens for creating new DBs.
#
# This file is the canonical schema for the keys that must be present in
# Vault at the above path. Fill the values in a *copy* of this file (not
# this file), push to Vault via `scripts/push_vault_from_file.py`, then
# shred the copy.

# Neon API token with create-database scope on the dev-tier Neon project.
# Create one at https://console.neon.tech under Account Settings > API Keys.
export NEON_API_TOKEN=

# Neon project ID for the dev-tier shared project. Visible in the Neon
# console URL when you're on the project dashboard (looks like
# `winter-frog-12345678`).
export NEON_PROJECT_ID=

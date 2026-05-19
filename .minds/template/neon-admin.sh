# Template for the `neon-admin` Vault entry at
# `secrets/kv/minds/<tier>/neon-admin`.
#
# These credentials are read by `minds env deploy` to provision per-dev-env
# Neon *projects* (one project per env, named `minds-<env>`, holding the
# env's `host_pool` and `litellm_cost` databases). They are NOT pushed to
# Modal -- the connector's runtime only needs the per-tier `DATABASE_URL`
# from the `neon` entry, not API tokens for creating new projects.
#
# This file is the canonical schema for the keys that must be present in
# Vault at the above path. Fill the values in a *copy* of this file (not
# this file), push to Vault via `scripts/push_vault_from_file.py`, then
# shred the copy.

# Neon API token with project-create scope on the dev-tier Neon org.
# Create one at https://console.neon.tech under Account Settings > API Keys.
# The token must be issued at the org level (not project-scoped) so it
# can create + delete the per-env projects.
export NEON_API_TOKEN=

# Neon organization id under which per-dev-env projects are created.
# Find it in the Neon console under Organization Settings; format is
# like `org-jolly-cell-77900540`.
export NEON_ORG_ID=

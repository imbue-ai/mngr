# Template for the `neon-admin` Vault entry at
# `secrets/kv/minds/<tier>/neon-admin`.
#
# These credentials are read by `minds env deploy` to operate Neon at
# the project level (create/delete per-dev-env projects in the dev
# tier; snapshot + restore the operator-managed project in shared
# tiers). They are NOT pushed to Modal -- the connector's runtime
# only needs the per-tier `DATABASE_URL` from the `neon` entry, not
# API tokens for managing projects or branches.
#
# This file is the canonical schema for the keys that must be present
# in Vault at the above path. Fill the values in a *copy* of this file
# (not this file), push to Vault via `scripts/push_vault_from_file.py`,
# then shred the copy.
#
# Per-tier required fields:
#
# +--------------+----------------+--------------+-------------------+
# | tier         | NEON_API_TOKEN | NEON_ORG_ID  | NEON_PROJECT_ID   |
# +==============+================+==============+===================+
# | dev          | required       | required     | unused            |
# | staging      | required       | unused       | required          |
# | production   | required       | unused       | required          |
# +--------------+----------------+--------------+-------------------+

# Neon API token. Required on every tier.
#
# - Dev tier: needs project-create + branch-create + restore scope on
#   the dev org so `minds env deploy` can create + tear down the
#   per-env Neon project. Issue the token at the org level (not
#   project-scoped) at https://console.neon.tech under Account
#   Settings > API Keys.
# - Shared tiers (staging / production): needs only branch-create +
#   restore scope on the operator-managed project named by
#   `NEON_PROJECT_ID`. A project-scoped token is sufficient and
#   preferable (least privilege).
export NEON_API_TOKEN=

# Neon organization id. Required only for the `dev` tier; ignored for
# shared tiers (they don't create projects -- the project already
# exists and is named by `NEON_PROJECT_ID` instead).
#
# Find it in the Neon console under Organization Settings; format is
# like `org-jolly-cell-77900540`. Leave empty on staging / production.
export NEON_ORG_ID=

# Neon project id. Required only for shared tiers (staging /
# production); ignored for the `dev` tier (which creates its project
# fresh per env and discovers the id from the create response).
#
# `minds env deploy` uses this to (a) take a pre-deploy snapshot
# branch off the project's default branch and (b) restore from that
# snapshot via `minds env recover` if anything goes wrong during the
# deploy. Without it, a failed shared-tier deploy can't be rolled
# back, so the deploy refuses to start.
#
# Find it in the Neon console under Project Settings > General;
# format is like `project-quiet-fog-42891337`. Leave empty on dev.
export NEON_PROJECT_ID=

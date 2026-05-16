# Template for the `vultr` Vault entry at `secrets/kv/minds/<tier>/vultr`.
#
# Shared dev-tier Vultr API key used by `minds env create` to tag and tear
# down per-dev-env VPS instances. NOT pushed to Modal -- only the
# pool-management SSH key (under `pool-ssh`) lives in the connector's
# runtime env.
#
# Fill the value in a *copy* of this file (not this file), push to Vault
# via `scripts/push_vault_from_file.py`, then shred the copy.

# Vultr API key with VPS create/destroy + tag scopes. Generate one at
# https://my.vultr.com/settings/#settingsapi.
export VULTR_API_KEY=

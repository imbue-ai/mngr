# Template for the `ovh` Vault entry at `secrets/kv/minds/<tier>/ovh`.
#
# Shared per-tier OVH API credentials used by `minds env deploy` /
# `minds env destroy` to tag and tear down OVH VPS instances belonging
# to a dev env (filtered by ``minds_env=<env-name>`` IAM tag). Also
# used by ``mngr imbue_cloud admin pool create`` when provisioning OVH
# pool hosts.
#
# These credentials are NOT pushed to Modal -- the connector's runtime
# never needs to mutate OVH directly (the pool ssh key under `pool-ssh`
# is what gets pushed). They are read only by the operator's machine
# during deploy/destroy.
#
# Fill the values in a *copy* of this file (not this file), push to
# Vault via `scripts/push_vault_from_file.py`, then shred the copy.

# OVH application key. Generate the AK/AS/CK trio for the relevant
# endpoint (usually ``ovh-us`` for our setup) via
# https://api.us.ovhcloud.com/createApp -- pick the script the README
# in libs/mngr_ovh references for the exact scopes the pool flows need
# (/vps, /order/cart, /v2/iam/resource, /me/api/credential, ...).
export OVH_APPLICATION_KEY=
export OVH_APPLICATION_SECRET=
export OVH_CONSUMER_KEY=

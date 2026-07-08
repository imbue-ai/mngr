# Template for the `ovh` Vault entry at `secrets/kv/minds/<tier>/ovh`.
#
# Shared per-tier OVH API credentials used by the operator's machine to
# order and manage the OVH bare-metal (dedicated) servers that Imbue
# Cloud carves lima-VM slices on (`mngr imbue_cloud admin server ...`).
# They are NOT pushed to Modal -- no deployed service makes OVH calls at
# runtime. Source them into your shell when running the bare-metal
# box-ordering flows.
#
# Fill the values in a *copy* of this file (not this file), push to
# Vault via `scripts/push_vault_from_file.py`, then shred the copy.

# OVH application key. Generate the AK/AS/CK trio for the relevant
# endpoint (usually ``ovh-us`` for our setup) via
# https://api.us.ovhcloud.com/createApp -- pick the script the README
# in libs/mngr_ovh references for the exact scopes the bare-metal
# ordering flows need (/order/cart, /dedicated/server, /me/api/credential, ...).
export OVH_APPLICATION_KEY=
export OVH_APPLICATION_SECRET=
export OVH_CONSUMER_KEY=

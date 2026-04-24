# Template for the `neon-<env>` Modal secret.
#
# When adding or removing a variable here, mirror the change in every per-env
# file (e.g. .minds/production/neon.sh). `scripts/push_modal_secrets.py`
# treats this file as the canonical list of expected keys and errors out if
# the target env file is missing any of them.
#
# Fill in values in a per-env copy, not here. Empty values are skipped on push
# (an empty `export KEY=` line declares the key but leaves it unset on Modal).

# Neon PostgreSQL connection string (pooled) for the host pool database.
# Used by the remote_service_connector at runtime to query/update pool_hosts.
export DATABASE_URL=

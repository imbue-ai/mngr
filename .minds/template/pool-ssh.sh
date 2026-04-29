# Template for the `pool-ssh-<env>` Modal secret.
#
# When adding or removing a variable here, mirror the change in every per-env
# file (e.g. .minds/production/pool-ssh.sh). `scripts/push_modal_secrets.py`
# treats this file as the canonical list of expected keys and errors out if
# the target env file is missing any of them.
#
# Fill in values in a per-env copy, not here. Empty values are skipped on push
# (an empty `export KEY=` line declares the key but leaves it unset on Modal).

# Ed25519 private key (PEM format) used by the remote_service_connector to
# SSH into pool hosts and inject user public keys during lease. Generate with:
#   ssh-keygen -t ed25519 -f pool_management_key -N ""
# Then paste the contents of pool_management_key (not .pub) here.
export POOL_SSH_PRIVATE_KEY=

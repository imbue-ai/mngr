# Template for the `litellm-<env>` Modal secret.
#
# When adding or removing a variable here, mirror the change in every per-env
# file (e.g. .minds/production/litellm.sh). `scripts/push_modal_secrets.py`
# treats this file as the canonical list of expected keys and errors out if
# the target env file is missing any of them.

# Anthropic API key for forwarding requests to the Anthropic API.
export ANTHROPIC_API_KEY=

# PostgreSQL connection string for litellm's cost tracking database.
export DATABASE_URL=

# Master key for litellm admin API (key generation, spend queries, etc.).
export LITELLM_MASTER_KEY=

# Template for the `litellm-connector-<env>` Modal secret.
#
# Contains only the LiteLLM vars needed by the remote service connector.
# Kept separate from litellm-<env> to avoid DATABASE_URL and
# ANTHROPIC_API_KEY collisions with the connector's own neon secret.

# Master key for litellm admin API (key generation, spend queries, etc.).
export LITELLM_MASTER_KEY=

# Public URL of the deployed litellm proxy.
export LITELLM_PROXY_URL=

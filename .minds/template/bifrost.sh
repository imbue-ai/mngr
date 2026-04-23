# Template for the `bifrost-<env>` Modal secret.
#
# When adding or removing a variable here, mirror the change in every per-env
# file (e.g. .minds/production/bifrost.sh). `scripts/push_modal_secrets.py`
# treats this file as the canonical list of expected keys and errors out if
# the target env file is missing any of them.
#
# Fill in values in a per-env copy, not here. Empty values are skipped on push
# (an empty `export KEY=` line declares the key but leaves it unset on Modal).

# Real Anthropic API key. Used by bifrost to forward agent requests to
# Anthropic. Agents never see this -- they only see their per-agent virtual
# key (sk-bf-*) which bifrost maps to this real key on forwarding.
export ANTHROPIC_API_KEY=

# AES key used by bifrost to encrypt virtual-key values at rest in the Neon
# config store. Must be stable across every deploy that shares the same
# database (rotating it invalidates every stored virtual key). Generate with
# e.g. `openssl rand -hex 32`.
export BIFROST_ENCRYPTION_KEY=

# Bearer token that protects bifrost's /api/* admin routes. The management
# Function uses this when proxying admin calls to localhost:8080; anyone
# reaching the inference Function's public URL also needs it to call /api/*.
# Generate with e.g. `openssl rand -hex 32`.
export BIFROST_ADMIN_TOKEN=

# --- Neon PostgreSQL: config store ---
# Holds bifrost governance data (virtual keys, budgets, rate limits, etc.).
# Kept in a separate database from the logs store to reduce load on any
# single Neon database.
export NEON_CONFIG_HOST=
export NEON_CONFIG_PORT=5432
export NEON_CONFIG_USER=
export NEON_CONFIG_PASSWORD=
export NEON_CONFIG_DB=

# --- Neon PostgreSQL: logs store ---
# Holds bifrost request logs (for analytics / histograms / cost breakdowns).
# Write-heavy; pointed at a different database from the config store.
export NEON_LOGS_HOST=
export NEON_LOGS_PORT=5432
export NEON_LOGS_USER=
export NEON_LOGS_PASSWORD=
export NEON_LOGS_DB=

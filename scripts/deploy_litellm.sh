#!/usr/bin/env bash
#
# Deploy the LiteLLM proxy Modal app for a given tier and Modal environment.
#
# Pulls the litellm Vault secret into Modal Secrets via
# scripts/push_modal_secrets.py, then deploys the Modal app pinned to the
# workspace named in the tier's deploy.toml and the Modal environment
# named on the command line.
#
# Usage:
#     scripts/deploy_litellm.sh <tier> <modal-env>
#
# Examples:
#     scripts/deploy_litellm.sh dev josh       # per-dev-env deploy
#     scripts/deploy_litellm.sh staging main   # tier deploy
#
# Both args are required; there's no default. For dev, <modal-env> is
# per-developer; for staging/production it's whatever stable env name
# the tier's operator picked (typically `main`).
#
# Requires:
#   - `vault login` against the HCP `admin` namespace
#   - `modal profile activate <name>` for the tier's Modal workspace

set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 <tier> <modal-env>" >&2
    exit 2
fi

tier="$1"
modal_env="$2"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
app_file="$repo_root/apps/modal_litellm/app.py"
deploy_toml="$repo_root/apps/minds/imbue/minds/config/envs/${tier}/deploy.toml"

if [[ ! -f "$app_file" ]]; then
    echo "error: app file not found: $app_file" >&2
    exit 1
fi
if [[ ! -f "$deploy_toml" ]]; then
    echo "error: deploy.toml not found for tier '${tier}': $deploy_toml" >&2
    exit 1
fi

modal_workspace=$(uv run python -c "
import sys, tomllib
with open('$deploy_toml', 'rb') as f:
    data = tomllib.load(f)
print(data.get('modal_workspace', ''))
")

if [[ -z "$modal_workspace" || "$modal_workspace" == "CHANGE_ME" ]]; then
    echo "error: '$deploy_toml' has no real modal_workspace set (got: '$modal_workspace')." >&2
    echo "       Edit the file before deploying." >&2
    exit 1
fi

echo "==> Pushing the litellm Vault secret to Modal env '${modal_env}' for tier '${tier}'..."
# litellm-proxy only consumes the `litellm-<tier>` Modal Secret; pushing
# everything else would force every other tier's Vault entries to be
# populated even when we're only iterating on the proxy. The secret has
# to land in the same Modal env the deploy below targets -- Modal
# Secrets are env-scoped.
uv run python "$repo_root/scripts/push_modal_secrets.py" "$tier" litellm --env "$modal_env"

# Modal won't auto-create the env on deploy; create it idempotently so
# operators don't have to remember a separate step on the first deploy
# of a new env.
echo "==> Ensuring Modal environment '${modal_env}' exists..."
if ! env_create_output=$(uv run modal environment create "$modal_env" 2>&1); then
    # Modal's "this env already exists" failure has shifted wording across
    # versions ("already exists", "same name or web label suffix as an
    # existing one"); both contain the substring "exist".
    if echo "$env_create_output" | grep -qi "exist"; then
        echo "    (already exists)"
    else
        echo "$env_create_output" >&2
        exit 1
    fi
fi

export MNGR_DEPLOY_ENV="$tier"

cd "$repo_root"

# Run the prisma schema push BEFORE the proxy deploy so the running
# proxy never sees a missing LiteLLM_VerificationToken / etc. table.
# The migrate_db function lives in the same app file, uses the same
# Modal Secret as the proxy (so DATABASE_URL is necessarily the same
# Postgres), and is idempotent -- re-running against an already-current
# schema is a quick no-op. `modal run` of an @app.function does not
# require a prior `modal deploy`, so this works on first-time tier
# bootstrap too.
echo "==> Pushing LiteLLM Prisma schema to DATABASE_URL via migrate_db Modal function..."
uv run modal run --env "$modal_env" "${app_file}::migrate_db"

echo "==> Deploying litellm-proxy-${tier} to workspace='${modal_workspace}', env='${modal_env}'..."
exec uv run modal deploy --name "litellm-proxy-${tier}" --env "$modal_env" "$app_file"

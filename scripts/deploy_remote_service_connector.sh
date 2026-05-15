#!/usr/bin/env bash
#
# Deploy the remote_service_connector Modal app for a given tier and
# Modal environment.
#
# Pulls the connector's required Vault secrets into Modal Secrets via
# scripts/push_modal_secrets.py, then deploys the Modal app pinned to
# the workspace named in the tier's deploy.toml and the Modal env
# named on the command line.
#
# Usage:
#     scripts/deploy_remote_service_connector.sh <tier> <modal-env>
#
# Examples:
#     scripts/deploy_remote_service_connector.sh dev josh
#     scripts/deploy_remote_service_connector.sh staging main
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
app_file="$repo_root/apps/remote_service_connector/imbue/remote_service_connector/app.py"
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

echo "==> Pushing the connector's Vault secrets to Modal env '${modal_env}' for tier '${tier}'..."
# The remote_service_connector binds these specific Modal Secrets in
# apps/remote_service_connector/.../app.py. Pushing the litellm secret
# is deferred to scripts/deploy_litellm.sh, which deploys a different
# Modal app. The secrets have to land in the same Modal env the deploy
# below targets -- Modal Secrets are env-scoped.
uv run python "$repo_root/scripts/push_modal_secrets.py" "$tier" \
    cloudflare \
    supertokens \
    neon \
    pool-ssh \
    litellm-connector \
    paid-accounts \
    --env "$modal_env"

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

echo "==> Deploying remote-service-connector-${tier} to workspace='${modal_workspace}', env='${modal_env}'..."
cd "$repo_root"
exec uv run modal deploy --name "remote-service-connector-${tier}" --env "$modal_env" "$app_file"

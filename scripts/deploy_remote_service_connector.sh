#!/usr/bin/env bash
#
# Deploy the remote_service_connector Modal app for a given tier.
#
# Pulls tier secrets from HCP Vault into Modal Secrets via
# scripts/push_modal_secrets.py, then deploys the Modal app pinned to
# the workspace named in the tier's deploy.toml.
#
# Usage:
#     scripts/deploy_remote_service_connector.sh <tier>
#
# Examples:
#     scripts/deploy_remote_service_connector.sh production
#     scripts/deploy_remote_service_connector.sh staging
#     scripts/deploy_remote_service_connector.sh dev
#
# Requires:
#   - `vault login` against the HCP `admin` namespace
#   - `modal token set` (or equivalent) for the tier's Modal workspace

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <tier>" >&2
    exit 2
fi

tier="$1"
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

# Read modal_workspace from deploy.toml via a one-liner Python invocation
# so we don't grow a shell TOML parser. Falls back to an empty string if
# the key is absent, which the loader will reject upstream.
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

echo "==> Pushing the connector's Vault secrets to Modal for tier '${tier}'..."
# The remote_service_connector binds these specific Modal Secrets in
# apps/remote_service_connector/.../app.py. Pushing the litellm secret
# is deferred to scripts/deploy_litellm.sh, which deploys a different
# Modal app.
uv run python "$repo_root/scripts/push_modal_secrets.py" "$tier" \
    cloudflare \
    supertokens \
    neon \
    pool-ssh \
    litellm-connector \
    paid-accounts

export MNGR_DEPLOY_ENV="$tier"

echo "==> Deploying remote-service-connector-${tier} to Modal workspace '${modal_workspace}'..."
cd "$repo_root"
exec uv run modal deploy --name "remote-service-connector-${tier}" "$app_file"

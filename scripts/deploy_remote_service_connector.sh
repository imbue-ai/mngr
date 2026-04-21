#!/usr/bin/env bash
#
# Deploy the remote_service_connector Modal app for a given environment.
#
# The environment name selects the Modal secrets that back the app:
# cloudflare-<env> and supertokens-<env>, plus a Secret.from_dict that bakes
# MNGR_DEPLOY_ENV into the container so runtime code can read it.
#
# Usage:
#     scripts/deploy_remote_service_connector.sh <env-name>
#
# Examples:
#     scripts/deploy_remote_service_connector.sh production
#     scripts/deploy_remote_service_connector.sh staging
#
# Secrets are managed separately with scripts/push_modal_secrets.py.

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <env-name>" >&2
    exit 2
fi

env_name="$1"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
app_file="$repo_root/apps/remote_service_connector/imbue/remote_service_connector/app.py"

if [[ ! -f "$app_file" ]]; then
    echo "error: app file not found: $app_file" >&2
    exit 1
fi

export MNGR_DEPLOY_ENV="$env_name"

echo "Deploying remote-service-connector-${env_name} with secrets:"
echo "  - cloudflare-${env_name}"
echo "  - supertokens-${env_name}"
echo ""

cd "$repo_root"
exec uv run modal deploy "$app_file"

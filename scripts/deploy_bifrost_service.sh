#!/usr/bin/env bash
#
# Deploy the bifrost_service Modal app for a given environment.
#
# The environment name selects the Modal secret that backs the app:
# bifrost-<env>, plus a Secret.from_dict that bakes MNGR_DEPLOY_ENV into
# the container so runtime code can read it. The management Function also
# reads supertokens-<env> for SuperTokens JWT validation (reused from the
# remote_service_connector deployment so credentials are not duplicated).
#
# Usage:
#     scripts/deploy_bifrost_service.sh <env-name>
#
# Examples:
#     scripts/deploy_bifrost_service.sh production
#     scripts/deploy_bifrost_service.sh staging
#
# Secrets are managed separately with scripts/push_modal_secrets.py.

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <env-name>" >&2
    exit 2
fi

env_name="$1"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
app_file="$repo_root/apps/bifrost_service/imbue/bifrost_service/app.py"

if [[ ! -f "$app_file" ]]; then
    echo "error: app file not found: $app_file" >&2
    exit 1
fi

export MNGR_DEPLOY_ENV="$env_name"

echo "Deploying bifrost-${env_name} with secrets:"
echo "  - bifrost-${env_name}"
echo "  - supertokens-${env_name} (management Function only)"
echo ""

cd "$repo_root"
exec uv run modal deploy "$app_file"

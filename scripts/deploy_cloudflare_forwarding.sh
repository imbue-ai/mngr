#!/usr/bin/env bash
#
# Deploy the cloudflare_forwarding Modal app for a given environment.
#
# The environment name selects the Modal secrets that back the app:
# cloudflare-<env> and supertokens-<env>, plus a Secret.from_dict that bakes
# MNGR_DEPLOY_ENV into the container so runtime code can read it.
#
# Usage:
#     scripts/deploy_cloudflare_forwarding.sh <env-name>
#
# Examples:
#     scripts/deploy_cloudflare_forwarding.sh production
#     scripts/deploy_cloudflare_forwarding.sh staging
#
# Secrets are managed separately with scripts/push_modal_secrets.py.

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <env-name>" >&2
    exit 2
fi

env_name="$1"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
app_file="$repo_root/apps/cloudflare_forwarding/imbue/cloudflare_forwarding/app.py"

if [[ ! -f "$app_file" ]]; then
    echo "error: app file not found: $app_file" >&2
    exit 1
fi

export MNGR_DEPLOY_ENV="$env_name"

echo "Deploying cloudflare-forwarding-${env_name} with secrets:"
echo "  - cloudflare-${env_name}"
echo "  - supertokens-${env_name}"
echo ""

cd "$repo_root"
exec uv run modal deploy "$app_file"

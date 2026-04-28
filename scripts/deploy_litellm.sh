#!/usr/bin/env bash
#
# Deploy the LiteLLM proxy Modal app for a given environment.
#
# Usage:
#     scripts/deploy_litellm.sh <env-name>
#
# Examples:
#     scripts/deploy_litellm.sh production
#
# Before deploying, push the litellm-<env> secret to Modal:
#     uv run python scripts/push_modal_secrets.py <env-name>

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <env-name>" >&2
    exit 2
fi

env_name="$1"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
app_file="$repo_root/apps/modal_litellm/app.py"

if [[ ! -f "$app_file" ]]; then
    echo "error: app file not found: $app_file" >&2
    exit 1
fi

export MNGR_DEPLOY_ENV="$env_name"

echo "Deploying litellm-proxy-${env_name} with secrets:"
echo "  - litellm-${env_name}"
echo ""

cd "$repo_root"
exec uv run modal deploy "$app_file"

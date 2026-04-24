#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load env vars from .env
source "$REPO_ROOT/.env"

export LITELLM_PROXY_PORT="${1:-4000}"

echo "Starting Anthropic proxy on port $LITELLM_PROXY_PORT..."
echo ""
echo "To use with claude -p, run:"
echo "  ANTHROPIC_BASE_URL=http://localhost:$LITELLM_PROXY_PORT claude -p 'hello'"
echo ""

uv run python "$SCRIPT_DIR/proxy.py"

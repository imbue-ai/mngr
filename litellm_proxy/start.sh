#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load env vars from .env
source "$REPO_ROOT/.env"

# litellm reads DATABASE_URL for its prisma connection
export DATABASE_URL="$LITELLM_DB_DIRECT"

# litellm's tool env has prisma -- put it on PATH so litellm can find it
LITELLM_TOOL_BIN="$HOME/.local/share/uv/tools/litellm/bin"
if [ -d "$LITELLM_TOOL_BIN" ]; then
    export PATH="$LITELLM_TOOL_BIN:$PATH"
fi

PORT="${1:-4000}"

echo "Starting LiteLLM proxy on port $PORT..."
echo ""
echo "To use with claude -p, run:"
echo "  ANTHROPIC_BASE_URL=http://localhost:$PORT/anthropic claude -p 'hello'"
echo ""
echo "To create a virtual key for cost tracking:"
echo "  curl -s -X POST http://localhost:$PORT/key/generate \\"
echo "    -H 'Authorization: Bearer \$LITELLM_MASTER_KEY' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"key_alias\": \"my-key\"}'"
echo ""

# Use litellm from uv tool (not uv run, which strips non-project deps)
litellm --port "$PORT" --config "$SCRIPT_DIR/config.yaml"

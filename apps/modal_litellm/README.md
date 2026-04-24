# LiteLLM Proxy on Modal

A serverless [LiteLLM](https://github.com/BerriAI/litellm) proxy deployed as a Modal ASGI function. Provides cost tracking via virtual keys for all Claude API usage routed through it.

## Architecture

- **Modal function** (`app.py`): Self-contained, no monorepo imports. Uses `@modal.asgi_app()` to serve LiteLLM's FastAPI app as a long-lived serverless function.
- **Database**: Neon PostgreSQL for cost tracking, key management, and spend logs.
- **Auth**: LiteLLM master key for admin operations; virtual keys for per-user/per-agent cost tracking.
- **Pass-through**: The `/anthropic/v1/messages` endpoint forwards requests to the real Anthropic API, so Claude Code can use it via `ANTHROPIC_BASE_URL`.

## Setup

### 1. Push secrets to Modal

```bash
uv run python scripts/push_modal_secrets.py production
```

This reads from `.minds/production/litellm.sh` and creates a `litellm-production` Modal secret with:
- `ANTHROPIC_API_KEY` -- for forwarding to Anthropic
- `DATABASE_URL` -- Neon PostgreSQL connection string
- `LITELLM_MASTER_KEY` -- admin API key

### 2. Deploy

```bash
scripts/deploy_litellm.sh production
```

This runs `modal deploy apps/modal_litellm/app.py` with `MNGR_DEPLOY_ENV=production`.

### 3. First-time DB migration

On the first cold start, LiteLLM runs ~118 Prisma migrations against the database. This takes ~14 minutes. Subsequent container starts take ~6 seconds.

The `min_containers=1` setting keeps one container alive to avoid cold starts.

### 4. Create a virtual key

```bash
PROXY_URL="https://<workspace>--litellm-proxy-production-litellm-app.modal.run"

curl -s -X POST "$PROXY_URL/key/generate" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key_alias": "my-agent"}'
```

### 5. Use with Claude Code

```bash
export ANTHROPIC_BASE_URL="https://<workspace>--litellm-proxy-production-litellm-app.modal.run/anthropic"
export ANTHROPIC_API_KEY="sk-your-virtual-key"

claude -p "hello"
```

## Local development

For local testing without Modal, use the `litellm_proxy/` directory at the repo root:

```bash
# One-time setup
uv tool install "litellm[proxy]" --with prisma

# Generate prisma client (one-time)
DATABASE_URL="..." ~/.local/share/uv/tools/litellm/bin/prisma generate \
  --schema ~/.local/share/uv/tools/litellm/lib/python3.12/site-packages/litellm/proxy/schema.prisma

# Start the proxy
./litellm_proxy/start.sh
```

See `litellm_proxy/start.sh` output for virtual key creation instructions.

## Supported models

- `claude-opus-4-7` (latest Opus)
- `claude-sonnet-4-6` (latest Sonnet)
- `claude-sonnet-4-20250514`
- `claude-opus-4-20250514`
- `claude-haiku-4-5-20251001`

## Checking spend

```bash
curl -s "$PROXY_URL/key/info?key=sk-your-virtual-key" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" | python3 -m json.tool
```

The `spend` field shows cumulative USD spend for that key.

## Troubleshooting

### ModuleNotFoundError for litellm modules

**Cause**: `uv run` syncs from `pyproject.toml` and strips litellm (not a project dependency) from the venv.

**Fix**: Use `uv tool install "litellm[proxy]"` for local development, or deploy on Modal where the image has litellm installed properly.

### Database URL empty / litellm can't connect

**Cause**: Unquoted URLs containing `&` in `.env` files -- bash interprets `&` as a background operator.

**Fix**: Quote all URLs: `export DATABASE_URL='postgresql://...?sslmode=require&channel_binding=require'`

### Port randomization

LiteLLM randomizes the port if the default (4000) is in use. Kill stale litellm processes before restarting.

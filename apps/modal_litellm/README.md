# LiteLLM Proxy on Modal

A serverless [LiteLLM](https://github.com/BerriAI/litellm) proxy deployed as a Modal ASGI function. Provides cost tracking via virtual keys for all Claude API usage routed through it.

## Architecture

- **Modal function** (`app.py`): Self-contained, no monorepo imports. Uses `@modal.asgi_app()` to serve LiteLLM's FastAPI app as a long-lived serverless function.
- **Database**: Neon PostgreSQL for cost tracking, key management, and spend logs.
- **Auth**: LiteLLM master key for admin operations; virtual keys for per-user/per-agent cost tracking.
- **Anthropic SDK compatible**: LiteLLM's native `POST /v1/messages` route accepts the Anthropic API request shape with a virtual key (`x-api-key` or `Authorization: Bearer sk-...`). Setting `ANTHROPIC_BASE_URL` to the proxy URL (no path suffix) routes the Anthropic SDK / Claude Code through the proxy with full cost tracking.

## Setup

### 1. Deploy (pushes secrets + runs `modal deploy`)

```bash
eval "$(uv run minds env activate production)"
uv run minds env deploy --yes-i-mean-production
```

`minds env deploy` reads `apps/minds/imbue/minds/config/envs/production/deploy.toml`
for the Modal workspace + the list of services to push from Vault,
creates the `litellm-production` Modal secret with:

- `ANTHROPIC_API_KEY` -- for forwarding to Anthropic
- `DATABASE_URL` -- Neon PostgreSQL connection string
- `LITELLM_MASTER_KEY` -- admin API key

and then runs `uv run modal deploy apps/modal_litellm/app.py` with
`MNGR_DEPLOY_ENV=production`. The `--yes-i-mean-production` flag is
the mandatory safety bar; substitute `--yes-i-mean-staging` (and
`activate staging`) for the staging tier.

### 3. First-time DB migration

On the first cold start, LiteLLM runs ~118 Prisma migrations against the database. This takes ~14 minutes. Subsequent container starts take ~6 seconds.

The `min_containers` setting keeps containers warm to avoid cold
starts. ``minds env deploy`` reads the value from the tier's
``apps/minds/imbue/minds/config/envs/<tier>/deploy.toml``
(``[min_containers].litellm_proxy``, default ``0``; staging and
production ship with ``1``) and threads it into ``modal deploy`` as
``MINDS_LITELLM_PROXY_MIN_CONTAINERS``. The value is read at module
load, which is when ``modal deploy`` serializes the function spec.
To override for a one-off deploy you control directly, export the env
var before running ``modal deploy`` by hand.

### 4. Create a virtual key

```bash
PROXY_URL="https://<workspace>--llm-production-proxy.modal.run"

curl -s -X POST "$PROXY_URL/key/generate" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key_alias": "my-agent"}'
```

### 5. Use with Claude Code

```bash
export ANTHROPIC_BASE_URL="https://<workspace>--llm-production-proxy.modal.run/"
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

The proxy registers each model with inline per-token pricing (mirrored from
litellm's `model_prices_and_context_window` map) so cost tracking is accurate
even on litellm versions whose bundled price map predates a model. The model
list lives in `apps/modal_litellm/app.py` (`LITELLM_CONFIG`) and is mirrored in
`litellm_proxy/config.yaml`; `config_drift_test.py` fails if the two diverge.

Opus (current price tier, $5 / $25 per 1M input / output):

- `claude-opus-4-8` (latest Opus)
- `claude-opus-4-7`
- `claude-opus-4-6`
- `claude-opus-4-5`

Opus (older, $15 / $75 per 1M):

- `claude-opus-4-1`
- `claude-opus-4-20250514` (Opus 4)

Sonnet ($3 / $15 per 1M):

- `claude-sonnet-4-6` (latest Sonnet)
- `claude-sonnet-4-5`
- `claude-sonnet-4-20250514` (Sonnet 4)

Haiku ($1 / $5 per 1M):

- `claude-haiku-4-5`
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

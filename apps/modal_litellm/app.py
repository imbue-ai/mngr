"""LiteLLM proxy deployed as a Modal serverless function.

This file is entirely self-contained -- it has NO imports from the monorepo.
Only stdlib, modal, pyyaml, and litellm (installed in the Modal image) are used.
This keeps deployment simple: ``modal deploy app.py`` ships just this file.

LiteLLM's native ``POST /v1/messages`` route accepts the Anthropic API
request shape, so the Anthropic SDK / Claude Code can talk to the proxy
by setting ``ANTHROPIC_BASE_URL`` to the proxy's root URL (no path
suffix). The SDK appends ``/v1/messages`` itself. All requests go
through LiteLLM's virtual key system for cost tracking.

Usage:
    # Push secrets to Modal + deploy in one shot:
    eval "$(uv run minds env activate production)"
    uv run minds env deploy --yes-i-mean-production

    # Use with claude -p (replace with your virtual key and Modal URL)
    ANTHROPIC_BASE_URL=https://<workspace>--llm-production-proxy.modal.run/ \\
    ANTHROPIC_API_KEY=sk-your-virtual-key \\
    claude -p "hello"
"""

import json
import os
import subprocess

import modal

_DEPLOY_ENV = os.environ.get("MNGR_DEPLOY_ENV", "production")

# Per-deploy timestamp baked into the deployed function spec. ``minds env
# deploy`` mints this at the start of every deploy and threads it through
# the ``modal deploy`` subprocess env. The deployed function pins to the
# matching ``<svc>-<tier>-<MINDS_DEPLOY_ID>`` Modal Secrets, so
# ``modal app rollback`` reverts the captured env and re-attaches to the
# previous deploy's secrets in one shot. Falls back to a sentinel value
# when unset so unit tests can import the module without raising; the
# resulting ``litellm-<tier>-MINDS_DEPLOY_ID_UNSET`` secret name doesn't
# exist in any Modal env so a real ``modal deploy`` invocation outside
# of ``minds env deploy`` will fail with "Secret not found" -- the
# safety property the timestamped-secret rollback model needs.
_MINDS_DEPLOY_ID = os.environ.get("MINDS_DEPLOY_ID", "MINDS_DEPLOY_ID_UNSET")

# Warm-pool size for the deployed function. ``minds env deploy`` reads
# the tier's ``[min_containers].litellm_proxy`` from its committed
# ``deploy.toml`` and threads the value here as
# ``MINDS_LITELLM_PROXY_MIN_CONTAINERS`` at ``modal deploy`` time --
# which is when this module is imported and the function spec is
# serialized. Defaults to 0 so a deploy that forgets to set the env
# var gets the cheapest possible warm pool (cold start on first hit).
_MIN_CONTAINERS = int(os.environ.get("MINDS_LITELLM_PROXY_MIN_CONTAINERS", "0"))

# Idle-before-scaledown window (seconds). ``minds env deploy`` threads the
# tier's ``[scaledown_window].litellm_proxy`` here as
# ``MINDS_LITELLM_PROXY_SCALEDOWN_WINDOW`` at ``modal deploy`` time. Dev tiers
# set this high (~10 min) so the no-warm-pool proxy stays hot across a dev
# session; staging / production leave it unset and rely on ``min_containers``.
# ``0`` (the default, and what the ci/test tier uses) means "don't pin it" --
# Modal uses its own default. Modal requires the value > 0, so 0 is normalized
# to ``None`` at the call site below.
_SCALEDOWN_WINDOW = int(os.environ.get("MINDS_LITELLM_PROXY_SCALEDOWN_WINDOW", "0"))

# Per-token USD pricing for each Anthropic model, mirrored verbatim from
# litellm's model_prices_and_context_window map. We register pricing inline
# (via litellm_params) rather than relying on litellm's bundled price map so
# cost tracking stays correct even on litellm versions whose bundled map
# predates a model (e.g. claude-opus-4-8 only landed in litellm's price map
# in the 1.88.0 pre-release line). MUST stay in sync with
# litellm_proxy/config.yaml -- config_drift_test.py enforces this.
_OPUS_PRICING = {
    "input_cost_per_token": 0.000005,
    "output_cost_per_token": 0.000025,
    "cache_creation_input_token_cost": 0.00000625,
    "cache_read_input_token_cost": 0.0000005,
}
# Opus 4.1 and the original Opus 4 (claude-opus-4-20250514) predate the Opus
# price drop and cost 3x the newer Opus models.
_OPUS_LEGACY_PRICING = {
    "input_cost_per_token": 0.000015,
    "output_cost_per_token": 0.000075,
    "cache_creation_input_token_cost": 0.00001875,
    "cache_read_input_token_cost": 0.0000015,
}
_SONNET_PRICING = {
    "input_cost_per_token": 0.000003,
    "output_cost_per_token": 0.000015,
    "cache_creation_input_token_cost": 0.00000375,
    "cache_read_input_token_cost": 0.0000003,
}
_HAIKU_PRICING = {
    "input_cost_per_token": 0.000001,
    "output_cost_per_token": 0.000005,
    "cache_creation_input_token_cost": 0.00000125,
    "cache_read_input_token_cost": 0.0000001,
}


def _model_entry(model_name: str, pricing: dict[str, float]) -> dict[str, object]:
    """Build a litellm model_list entry that forwards to the Anthropic API with inline pricing."""
    litellm_params: dict[str, object] = {
        "model": f"anthropic/{model_name}",
        "api_key": "os.environ/ANTHROPIC_API_KEY",
    }
    litellm_params.update(pricing)
    return {"model_name": model_name, "litellm_params": litellm_params}


LITELLM_CONFIG = {
    "model_list": [
        # Current Opus line.
        _model_entry("claude-opus-4-8", _OPUS_PRICING),
        _model_entry("claude-opus-4-7", _OPUS_PRICING),
        _model_entry("claude-opus-4-6", _OPUS_PRICING),
        _model_entry("claude-opus-4-5", _OPUS_PRICING),
        # Older Opus (higher price tier), still active on the Anthropic API.
        _model_entry("claude-opus-4-1", _OPUS_LEGACY_PRICING),
        _model_entry("claude-opus-4-20250514", _OPUS_LEGACY_PRICING),
        # Sonnet line.
        _model_entry("claude-sonnet-4-6", _SONNET_PRICING),
        _model_entry("claude-sonnet-4-5", _SONNET_PRICING),
        _model_entry("claude-sonnet-4-20250514", _SONNET_PRICING),
        # Haiku line (bare alias + dated id both routable).
        _model_entry("claude-haiku-4-5", _HAIKU_PRICING),
        _model_entry("claude-haiku-4-5-20251001", _HAIKU_PRICING),
    ],
    "general_settings": {
        "database_url": "os.environ/DATABASE_URL",
        "master_key": "os.environ/LITELLM_MASTER_KEY",
    },
    "litellm_settings": {
        "drop_params": True,
        "num_retries": 0,
    },
}


def _write_config_file() -> str:
    """Write the litellm config to a temp YAML file and return the path."""
    import yaml

    config_path = "/tmp/litellm_config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(LITELLM_CONFIG, f)
    return config_path


image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("litellm[proxy]", "prisma", "pyyaml")
    .run_commands(
        'python -c "import litellm.proxy; import os; print(os.path.dirname(litellm.proxy.__file__))" > /tmp/litellm_proxy_dir.txt',
        "prisma generate --schema $(cat /tmp/litellm_proxy_dir.txt)/schema.prisma",
    )
)

app = modal.App(name=f"llm-{_DEPLOY_ENV}", image=image)


@app.function(
    name="proxy",
    secrets=[
        modal.Secret.from_name(f"litellm-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}"),
        modal.Secret.from_dict({"MNGR_DEPLOY_ENV": _DEPLOY_ENV, "MINDS_DEPLOY_ID": _MINDS_DEPLOY_ID}),
    ],
    min_containers=_MIN_CONTAINERS,
    # Idle-before-scaledown window driven by ``_SCALEDOWN_WINDOW``. ``0``
    # (default / ci) -> ``None`` so Modal uses its own default; dev pins this
    # high so the no-warm-pool proxy stays hot across a dev session.
    scaledown_window=_SCALEDOWN_WINDOW or None,
    timeout=600,
)
@modal.asgi_app()
def litellm_app():
    config_path = _write_config_file()
    os.environ["CONFIG_FILE_PATH"] = config_path
    os.environ["WORKER_CONFIG"] = json.dumps(
        {
            "config": config_path,
        }
    )

    from litellm.proxy.proxy_server import app as fastapi_app

    return fastapi_app


@app.function(
    secrets=[modal.Secret.from_name(f"litellm-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}")],
    timeout=300,
)
def migrate_db() -> None:
    """Run `prisma db push` against DATABASE_URL to bring the LiteLLM schema current.

    Invoked by ``minds env deploy`` (via
    ``apps/minds/imbue/minds/envs/per_env_deploy.py::deploy_litellm_proxy``)
    before each ``modal deploy`` so the running proxy never sees a
    missing LiteLLM_VerificationToken / LiteLLM_BudgetTable / etc.

    Runs in the same image as the proxy itself, so prisma + the
    litellm[proxy] package (which ships the canonical schema.prisma)
    are already installed. Runs against the same `litellm-<tier>` Modal
    Secret the proxy consumes, so DATABASE_URL is necessarily the same
    Postgres the proxy will talk to at runtime.

    Idempotent: prisma db push only applies diffs, so re-running on an
    already-current database is a no-op (~1s wall-clock). The
    --accept-data-loss flag is safe here -- the schema is LiteLLM's,
    not ours, so any "loss" would be of stale columns that LiteLLM
    itself dropped in a version bump (we don't write to those tables
    out-of-band). --skip-generate skips client codegen since the image
    already did that at build time.
    """
    import litellm.proxy

    schema_path = os.path.join(os.path.dirname(litellm.proxy.__file__), "schema.prisma")
    subprocess.run(
        ["prisma", "db", "push", "--schema", schema_path, "--accept-data-loss", "--skip-generate"],
        check=True,
    )

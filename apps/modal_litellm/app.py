"""LiteLLM proxy deployed as a Modal serverless function.

This file is entirely self-contained -- it has NO imports from the monorepo.
Only stdlib, modal, pyyaml, and litellm (installed in the Modal image) are used.
This keeps deployment simple: ``modal deploy app.py`` ships just this file.

The proxy exposes the Anthropic pass-through endpoint so that Claude Code
can connect via ANTHROPIC_BASE_URL. All requests go through LiteLLM's
virtual key system for cost tracking.

Usage:
    # Push secrets to Modal + deploy in one shot:
    eval "$(uv run minds env activate production)"
    uv run minds env deploy --yes-i-mean-production

    # Use with claude -p (replace with your virtual key and Modal URL)
    ANTHROPIC_BASE_URL=https://<workspace>--litellm-proxy-production-litellm-app.modal.run/anthropic \\
    ANTHROPIC_API_KEY=sk-your-virtual-key \\
    claude -p "hello"
"""

import json
import os
import subprocess

import modal

_DEPLOY_ENV = os.environ.get("MNGR_DEPLOY_ENV", "production")

LITELLM_CONFIG = {
    "model_list": [
        {
            "model_name": "claude-opus-4-7",
            "litellm_params": {
                "model": "anthropic/claude-opus-4-7",
                "api_key": "os.environ/ANTHROPIC_API_KEY",
            },
        },
        {
            "model_name": "claude-sonnet-4-6",
            "litellm_params": {
                "model": "anthropic/claude-sonnet-4-6",
                "api_key": "os.environ/ANTHROPIC_API_KEY",
            },
        },
        {
            "model_name": "claude-sonnet-4-20250514",
            "litellm_params": {
                "model": "anthropic/claude-sonnet-4-20250514",
                "api_key": "os.environ/ANTHROPIC_API_KEY",
            },
        },
        {
            "model_name": "claude-opus-4-20250514",
            "litellm_params": {
                "model": "anthropic/claude-opus-4-20250514",
                "api_key": "os.environ/ANTHROPIC_API_KEY",
            },
        },
        {
            "model_name": "claude-haiku-4-5-20251001",
            "litellm_params": {
                "model": "anthropic/claude-haiku-4-5-20251001",
                "api_key": "os.environ/ANTHROPIC_API_KEY",
            },
        },
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

app = modal.App(name=f"litellm-proxy-{_DEPLOY_ENV}", image=image)


@app.function(
    secrets=[
        modal.Secret.from_name(f"litellm-{_DEPLOY_ENV}"),
        modal.Secret.from_dict({"MNGR_DEPLOY_ENV": _DEPLOY_ENV}),
    ],
    min_containers=1,
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
    secrets=[modal.Secret.from_name(f"litellm-{_DEPLOY_ENV}")],
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

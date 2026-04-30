"""LiteLLM proxy deployed as a Modal serverless function.

This file is entirely self-contained -- it has NO imports from the monorepo.
Only stdlib, modal, pyyaml, and litellm (installed in the Modal image) are used.
This keeps deployment simple: ``modal deploy app.py`` ships just this file.

The proxy exposes the Anthropic pass-through endpoint so that Claude Code
can connect via ANTHROPIC_BASE_URL. All requests go through LiteLLM's
virtual key system for cost tracking.

Usage:
    # One-time: push secrets to Modal
    uv run python scripts/push_modal_secrets.py production

    # Deploy
    MNGR_DEPLOY_ENV=production uv run modal deploy apps/modal_litellm/app.py

    # Use with claude -p (replace with your virtual key and Modal URL)
    ANTHROPIC_BASE_URL=https://<workspace>--litellm-proxy-production-litellm-app.modal.run/anthropic \\
    ANTHROPIC_API_KEY=sk-your-virtual-key \\
    claude -p "hello"
"""

import json
import os

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

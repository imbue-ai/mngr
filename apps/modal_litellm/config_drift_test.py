import importlib.util
from pathlib import Path
from typing import Any

import yaml

_THIS_DIR = Path(__file__).parent
_REPO_ROOT = _THIS_DIR.parents[1]
_LOCAL_CONFIG_PATH = _REPO_ROOT / "litellm_proxy" / "config.yaml"


def _load_deployed_model_list() -> list[dict[str, Any]]:
    """Load LITELLM_CONFIG['model_list'] from the Modal app module (app.py)."""
    app_path = _THIS_DIR / "app.py"
    spec = importlib.util.spec_from_file_location("modal_litellm_app_under_test", app_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LITELLM_CONFIG["model_list"]


def _load_local_model_list() -> list[dict[str, Any]]:
    """Load model_list from the local-dev litellm_proxy/config.yaml."""
    with _LOCAL_CONFIG_PATH.open("rb") as config_file:
        return yaml.safe_load(config_file)["model_list"]


def test_deployed_and_local_model_lists_match() -> None:
    """The Modal app and the local-dev litellm config must expose identical models + pricing.

    These are two representations of the same model list for two different
    consumers (the Modal-deployed proxy vs the local `litellm` CLI). Keeping
    them byte-for-byte in agreement here makes silent drift impossible.
    """
    deployed_model_list = _load_deployed_model_list()
    local_model_list = _load_local_model_list()
    assert deployed_model_list == local_model_list

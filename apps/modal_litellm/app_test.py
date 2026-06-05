from pathlib import Path

import app
import yaml
from inline_snapshot import snapshot


def test_write_config_file_round_trips_to_in_memory_litellm_config(
    litellm_proxy_config_path: str,
) -> None:
    """The file the proxy reads at startup must deserialize back to LITELLM_CONFIG.

    Guards that the whole config stays YAML-serializable (a non-serializable value
    would raise in the writer) and that it round-trips faithfully through the
    serializer LiteLLM uses -- so the proxy boots with exactly the intended config
    rather than failing on a live Modal deploy. Comparing against the in-memory
    constant also pins every field, including the litellm_settings block.
    """
    loaded_config = yaml.safe_load(Path(litellm_proxy_config_path).read_text())

    assert loaded_config == app.LITELLM_CONFIG


def test_litellm_config_routes_each_model_to_anthropic_with_api_key_reference(
    litellm_proxy_config_path: str,
) -> None:
    """Each exposed model must route to ``anthropic/<name>`` via the API-key env ref.

    Asserts, per model, that the public model_name and its litellm routing target
    agree (guarding a typo that would silently route to the wrong model) and that
    the api_key is the ``os.environ/ANTHROPIC_API_KEY`` reference LiteLLM resolves
    at runtime, not a literal credential. The exposed model-name set is pinned so
    an added or removed model surfaces as a reviewed diff.
    """
    loaded_config = yaml.safe_load(Path(litellm_proxy_config_path).read_text())

    model_names = [entry["model_name"] for entry in loaded_config["model_list"]]
    assert model_names == snapshot(
        [
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-sonnet-4-20250514",
            "claude-opus-4-20250514",
            "claude-haiku-4-5-20251001",
        ]
    )
    for entry in loaded_config["model_list"]:
        assert entry["litellm_params"]["model"] == f"anthropic/{entry['model_name']}"
        assert entry["litellm_params"]["api_key"] == "os.environ/ANTHROPIC_API_KEY"


def test_litellm_config_binds_database_and_master_key_to_env_references(
    litellm_proxy_config_path: str,
) -> None:
    """general_settings + litellm_settings must carry the right env refs and policy.

    Binds each secret to its role -- ``database_url`` and ``master_key`` must be
    the ``os.environ/<NAME>`` references LiteLLM resolves at runtime (not literal
    credentials) -- and pins the request-handling policy (``drop_params`` on,
    retries off) the proxy is meant to deploy with.
    """
    loaded_config = yaml.safe_load(Path(litellm_proxy_config_path).read_text())

    assert loaded_config["general_settings"]["database_url"] == "os.environ/DATABASE_URL"
    assert loaded_config["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    assert loaded_config["litellm_settings"]["drop_params"] is True
    assert loaded_config["litellm_settings"]["num_retries"] == 0

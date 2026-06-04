from pathlib import Path

import app


def test_litellm_config_model_names_match_anthropic_routing_suffix() -> None:
    """Each public model_name must equal the suffix of its anthropic routing string.

    Programmatic guard against a hand-maintained model_list entry whose public
    name and underlying ``anthropic/<model>`` routing target drift apart (e.g. a
    typo introduced when adding a model), which would silently route requests to
    the wrong model in the deployed proxy.
    """
    for entry in app.LITELLM_CONFIG["model_list"]:
        model_name = entry["model_name"]
        routing_target = entry["litellm_params"]["model"]
        assert routing_target == f"anthropic/{model_name}"
        assert entry["litellm_params"]["api_key"] == "os.environ/ANTHROPIC_API_KEY"


def test_write_config_file_emits_env_var_references_and_model_routing() -> None:
    """The written config file is what the proxy reads at startup.

    Calling the writer guards that the config stays serializable (a
    non-serializable value would raise here, only otherwise surfacing as a proxy
    container failing to boot on a live Modal deploy). The content assertions
    guard the secret-resolution contract: LiteLLM resolves secrets only from
    literal ``os.environ/<NAME>`` reference strings, so if serialization ever
    quoted, escaped, or transformed them the proxy would treat them as literal
    credentials. The per-model routing lines guard that every configured model
    is wired to its ``anthropic/<model>`` target in the emitted file.
    """
    config_path = app._write_config_file()

    written_config = Path(config_path).read_text()

    assert "os.environ/ANTHROPIC_API_KEY" in written_config
    assert "os.environ/DATABASE_URL" in written_config
    assert "os.environ/LITELLM_MASTER_KEY" in written_config
    for entry in app.LITELLM_CONFIG["model_list"]:
        assert f"anthropic/{entry['model_name']}" in written_config

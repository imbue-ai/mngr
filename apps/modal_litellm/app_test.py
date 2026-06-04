from pathlib import Path

import app

# The set of public model names the proxy is expected to expose. This is the
# reviewed source of truth: adding or removing a model must show up as a diff
# here, and each name is checked against the config the proxy actually writes.
_EXPECTED_MODEL_NAMES = (
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-sonnet-4-20250514",
    "claude-opus-4-20250514",
    "claude-haiku-4-5-20251001",
)


def test_write_config_file_exposes_each_expected_model_with_anthropic_routing() -> None:
    """Every expected model must appear in the written config wired to anthropic.

    Reads the file the proxy actually consumes at startup and asserts, per model,
    that both the public ``model_name: <name>`` entry and its
    ``model: anthropic/<name>`` routing target are present. This catches a
    hand-maintained model_list entry whose public name and routing target drift
    apart (e.g. a typo when adding a model), which would otherwise silently route
    requests to the wrong model. The entry count is pinned so a dropped or extra
    model is caught too.
    """
    config_path = app._write_config_file()

    written_config = Path(config_path).read_text()

    for model_name in _EXPECTED_MODEL_NAMES:
        assert f"model_name: {model_name}" in written_config
        assert f"model: anthropic/{model_name}" in written_config
    assert written_config.count("model_name:") == len(_EXPECTED_MODEL_NAMES)


def test_write_config_file_emits_env_var_references_for_secret_resolution() -> None:
    """The written config file is what the proxy reads at startup.

    Calling the writer guards that the config stays serializable (a
    non-serializable value would raise here, only otherwise surfacing as a proxy
    container failing to boot on a live Modal deploy). The content assertions
    guard the secret-resolution contract: LiteLLM resolves secrets only from
    literal ``os.environ/<NAME>`` reference strings, so if serialization ever
    quoted, escaped, or transformed them the proxy would treat them as literal
    credentials instead of env-var lookups.
    """
    config_path = app._write_config_file()

    written_config = Path(config_path).read_text()

    assert "os.environ/ANTHROPIC_API_KEY" in written_config
    assert "os.environ/DATABASE_URL" in written_config
    assert "os.environ/LITELLM_MASTER_KEY" in written_config

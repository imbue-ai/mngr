"""End-to-end test for changeling deployment and chat using the test-coder agent type.

Deploys a real changeling locally using the test-coder agent type (which uses
the matched-responses model instead of real LLMs), verifies the full pipeline
works, and cleans up. No API keys are required.

The matched-responses model's env-var-based customization (LLM_MATCHED_RESPONSE
and LLM_MATCHED_RESPONSES_FILE) is thoroughly unit-tested in
libs/llm_matched_responses/llm_matched_responses_test.py. This test focuses on
verifying the deployment pipeline and that the model is correctly installed and
configured in the changeling environment.
"""

from pathlib import Path

import pytest

from imbue.changelings.testing import extract_response
from imbue.changelings.testing import run_mng


@pytest.mark.release
@pytest.mark.timeout(120)
def test_deploy_test_coder_and_verify_matched_responses_model(deployed_test_coder: dict[str, object]) -> None:
    """Deploy a test-coder changeling and verify the matched-responses model works end-to-end.

    Verifies:
    1. The changeling was deployed successfully (via the fixture)
    2. The matched-responses model is installed and responds correctly via mng exec
    3. The chat settings are configured with model = "matched-responses"
    4. The env-var-based response override works in the agent environment
    """
    agent_name = str(deployed_test_coder["name"])

    # Verify the model returns the expected default response
    test_message = "Hello from end-to-end test"
    exec_result = run_mng("exec", agent_name, f'llm -m matched-responses "{test_message}"')
    assert exec_result.returncode == 0, (
        f"llm matched-responses failed:\nstdout: {exec_result.stdout}\nstderr: {exec_result.stderr}"
    )
    assert extract_response(exec_result) == f"Echo: {test_message}"

    # Verify the chat settings have the matched-responses model configured
    work_dir = str(deployed_test_coder["work_dir"])
    settings_path = Path(work_dir) / "changelings.toml"
    assert settings_path.exists(), f"Settings file not found at {settings_path}"
    settings_content = settings_path.read_text()
    assert 'model = "matched-responses"' in settings_content, (
        f"matched-responses model not configured in settings:\n{settings_content}"
    )

    # Verify the LLM_MATCHED_RESPONSE env var override works in the agent environment
    custom_response = "I am a test bot and this is my canned response."
    exec_result = run_mng(
        "exec",
        agent_name,
        f'LLM_MATCHED_RESPONSE="{custom_response}" llm -m matched-responses "anything"',
    )
    assert exec_result.returncode == 0, (
        f"llm matched-responses failed:\nstdout: {exec_result.stdout}\nstderr: {exec_result.stderr}"
    )
    assert extract_response(exec_result) == custom_response

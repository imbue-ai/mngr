"""Acceptance tests for the deploy CLI command.

These tests run the full deploy flow including mng create subprocess calls,
so they are too slow for the 10s integration test timeout. They run in the
acceptance test runner (90s timeout) instead.
"""

from pathlib import Path

import pytest

from imbue.changelings.cli.conftest import DEPLOY_TEST_RUNNER
from imbue.changelings.cli.conftest import create_git_repo_with_agent_type
from imbue.changelings.cli.conftest import data_dir_args
from imbue.changelings.cli.conftest import deploy_with_agent_type
from imbue.changelings.cli.conftest import deploy_with_git_url
from imbue.changelings.main import cli


@pytest.mark.acceptance
def test_deploy_cleans_up_temp_dir_after_deployment(tmp_path: Path) -> None:
    """Verify that no .tmp- directories remain after deployment (success or failure)."""
    repo_dir = create_git_repo_with_agent_type(tmp_path)
    data_dir = tmp_path / "changelings-data"

    deploy_with_git_url(tmp_path, str(repo_dir), name="my-bot", provider="local")

    if data_dir.exists():
        leftover = [p for p in data_dir.iterdir() if p.name.startswith(".tmp-")]
        assert leftover == []


@pytest.mark.acceptance
def test_deploy_shows_prompts(tmp_path: Path) -> None:
    """Verify all three prompts appear when deploying without flags."""
    repo_dir = create_git_repo_with_agent_type(tmp_path)

    result = DEPLOY_TEST_RUNNER.invoke(
        cli,
        ["deploy", str(repo_dir), *data_dir_args(tmp_path)],
        input="my-agent\n2\nN\n",
    )

    assert "What would you like to name this agent" in result.output
    assert "Where do you want to run" in result.output
    assert "launch its own agents" in result.output


@pytest.mark.acceptance
def test_deploy_displays_clone_url(tmp_path: Path) -> None:
    repo_dir = create_git_repo_with_agent_type(tmp_path)

    result = DEPLOY_TEST_RUNNER.invoke(
        cli,
        ["deploy", str(repo_dir), *data_dir_args(tmp_path)],
        input="test-bot\n1\nN\n",
    )

    assert "Cloning repository" in result.output


@pytest.mark.acceptance
def test_deploy_name_flag_skips_prompt(tmp_path: Path) -> None:
    """Verify that --name skips the name prompt."""
    repo_dir = create_git_repo_with_agent_type(tmp_path)

    result = deploy_with_git_url(tmp_path, str(repo_dir), name="my-custom-name")

    assert "What would you like to name this agent" not in result.output


@pytest.mark.acceptance
def test_deploy_provider_flag_skips_prompt(tmp_path: Path) -> None:
    """Verify that --provider skips the provider prompt."""
    repo_dir = create_git_repo_with_agent_type(tmp_path)

    result = DEPLOY_TEST_RUNNER.invoke(
        cli,
        ["deploy", str(repo_dir), "--provider", "local", "--no-self-deploy", *data_dir_args(tmp_path)],
        input="test-bot\n",
    )

    assert "Where do you want to run" not in result.output


@pytest.mark.acceptance
def test_deploy_self_deploy_flag_skips_prompt(tmp_path: Path) -> None:
    """Verify that --no-self-deploy skips the self-deploy prompt."""
    repo_dir = create_git_repo_with_agent_type(tmp_path)

    result = DEPLOY_TEST_RUNNER.invoke(
        cli,
        ["deploy", str(repo_dir), "--no-self-deploy", "--provider", "local", *data_dir_args(tmp_path)],
        input="test-bot\n",
    )

    assert "launch its own agents" not in result.output


@pytest.mark.acceptance
def test_deploy_all_flags_skip_all_prompts(tmp_path: Path) -> None:
    """Verify that providing all flags skips all interactive prompts."""
    repo_dir = create_git_repo_with_agent_type(tmp_path)

    result = deploy_with_git_url(tmp_path, str(repo_dir), name="bot")

    assert "What would you like to name this agent" not in result.output
    assert "Where do you want to run" not in result.output
    assert "launch its own agents" not in result.output


@pytest.mark.acceptance
def test_deploy_agent_type_shows_creating_message(tmp_path: Path) -> None:
    """Verify that --agent-type shows a 'Creating changeling repo' message instead of 'Cloning'."""
    result = deploy_with_agent_type(tmp_path)

    assert "Cloning repository" not in result.output
    assert "Deploying changeling from" in result.output


@pytest.mark.acceptance
def test_deploy_agent_type_defaults_name_to_agent_type(tmp_path: Path) -> None:
    """Verify that --agent-type defaults the agent name prompt to the agent type value."""
    result = deploy_with_agent_type(tmp_path, name=None, input_text="elena-code\n")

    assert "elena-code" in result.output


@pytest.mark.acceptance
def test_deploy_with_self_deploy_flag(tmp_path: Path) -> None:
    """Verify --self-deploy flag is accepted and skips the self-deploy prompt."""
    repo_dir = create_git_repo_with_agent_type(tmp_path)

    result = DEPLOY_TEST_RUNNER.invoke(
        cli,
        [
            "deploy",
            str(repo_dir),
            "--name",
            "my-bot",
            "--provider",
            "local",
            "--self-deploy",
            *data_dir_args(tmp_path),
        ],
    )

    assert "launch its own agents" not in result.output


@pytest.mark.acceptance
def test_deploy_provider_prompt_accepts_local_selection(
    tmp_path: Path,
) -> None:
    """Verify interactive provider selection with local (choice 1) proceeds to deployment."""
    repo_dir = create_git_repo_with_agent_type(tmp_path)

    result = DEPLOY_TEST_RUNNER.invoke(
        cli,
        ["deploy", str(repo_dir), *data_dir_args(tmp_path)],
        input="test-bot\n1\nN\n",
    )

    assert "Where do you want to run" in result.output
    assert "Deploying changeling from" in result.output


@pytest.mark.acceptance
def test_deploy_self_deploy_yes_via_interactive_input(tmp_path: Path) -> None:
    """Verify that interactive input 'y' for self-deploy is accepted."""
    repo_dir = create_git_repo_with_agent_type(tmp_path)

    result = DEPLOY_TEST_RUNNER.invoke(
        cli,
        ["deploy", str(repo_dir), "--provider", "local", *data_dir_args(tmp_path)],
        input="test-bot\ny\n",
    )

    assert "launch its own agents" in result.output

"""Tests for the create-template tutorial blocks."""

import json
import os

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


def _write_modal_big_template(e2e: E2eSession) -> None:
    """Add the modal-big template to local config so --template modal-big works."""
    cfg = ".$MNGR_ROOT_NAME/settings.local.toml"
    expect(
        e2e.run(
            f"echo '' >> {cfg} && echo '[create_templates.modal-big]' >> {cfg} && echo 'transfer = \"none\"' >> {cfg}",
            comment="define modal-big template (substituted with transfer=none for the local test)",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.tmux
def test_templates_setup_via_config_edit(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # templates are defined in your config (user, project, or local scope).
        # here's how to set one up using the config command:
        mngr config edit --scope project
        # in the editor, add something like:
        #   [create_templates.modal-big]
        #   provider = "modal"
        #   build_arg = ["cpu=4", "memory=16"]
        #   idle_timeout = "120"
        #   agent_args = ["--dangerously-skip-permissions"]
        # then use the template when creating agents:
        mngr create my-task --template modal-big
    """)
    expect(
        e2e.run(
            "EDITOR=/bin/true mngr config edit --scope project",
            comment="open the project config",
        )
    ).to_succeed()
    # Simulate the edits the tutorial describes: add the modal-big template to the
    # project config that `config edit --scope project` just created. transfer="none"
    # is substituted for the modal provider so the agent stays local for the test,
    # and is_allowed_in_pytest opts this fresh project config into the pytest run.
    project_cfg = ".$MNGR_ROOT_NAME/settings.toml"
    expect(
        e2e.run(
            f"echo 'is_allowed_in_pytest = true' >> {project_cfg}"
            f" && echo '[create_templates.modal-big]' >> {project_cfg}"
            f" && echo 'transfer = \"none\"' >> {project_cfg}",
            comment="add the modal-big template in the editor",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create my-task --template modal-big --type command --no-ensure-clean --no-connect -- sleep 100940",
            comment="use the template when creating agents",
        )
    ).to_succeed()


@pytest.mark.release
def test_create_with_undefined_template_fails(e2e: E2eSession) -> None:
    # Unhappy path for the same tutorial block: if you reference a template
    # without first defining it in config, create fails fast with a clear error
    # rather than silently falling back to defaults. This proves --template is
    # actually resolved against the config (and fails before any agent is started,
    # so no tmux/modal is needed).
    e2e.write_tutorial_block("""
        # templates are defined in your config (user, project, or local scope).
        # here's how to set one up using the config command:
        mngr config edit --scope project
        # in the editor, add something like:
        #   [create_templates.modal-big]
        #   provider = "modal"
        #   build_arg = ["cpu=4", "memory=16"]
        #   idle_timeout = "120"
        #   agent_args = ["--dangerously-skip-permissions"]
        # then use the template when creating agents:
        mngr create my-task --template modal-big
    """)
    result = e2e.run(
        "mngr create my-task --template modal-big --type command --no-ensure-clean --no-connect -- sleep 100943",
        comment="using a template that was never defined fails",
    )
    expect(result).to_fail()
    expect(result.stderr).to_contain("not found")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_template_short_form(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # short form
        mngr create my-task -t modal-big
    """)
    _write_modal_big_template(e2e)
    expect(
        e2e.run(
            "mngr create my-task -t modal-big --type command --no-ensure-clean --no-connect -- sleep 100941",
            comment="short form -t",
        )
    ).to_succeed()

    # Verify the agent was created and that the -t short form actually resolved the
    # modal-big template: its substituted `transfer = "none"` runs the agent in-place,
    # so its work_dir must equal the session cwd (not a generated worktree).
    pwd_result = e2e.run("pwd", comment="get the session cwd for comparison")
    expect(pwd_result).to_succeed()
    session_cwd = pwd_result.stdout.strip()

    list_result = e2e.run("mngr list --format json", comment="confirm the templated agent exists")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1, f"Expected exactly one 'my-task' agent, got: {agents}"
    work_dir = matching[0]["work_dir"]
    assert os.path.realpath(work_dir) == os.path.realpath(session_cwd), (
        "Expected template's transfer=none to run the agent in-place.\n"
        f"  work_dir: {work_dir}\n  session cwd: {session_cwd}"
    )


def _write_stacking_templates(e2e: E2eSession) -> None:
    """Define modal-big and with-tests templates with observable, stackable env vars.

    The two templates each set environment variables on the created agent so the
    stacking behavior can be verified concretely:
    - ``modal-big`` assigns ``env`` with ``STACKED_FROM_MODAL_BIG`` plus a shared
      ``OVERRIDDEN`` key.
    - ``with-tests`` uses ``env__extend`` so its ``STACKED_FROM_WITH_TESTS`` value
      is appended (rather than replacing modal-big's list), and re-sets the shared
      ``OVERRIDDEN`` key so the later template's value wins.
    """
    cfg = ".$MNGR_ROOT_NAME/settings.local.toml"
    expect(
        e2e.run(
            f"echo '' >> {cfg}"
            f" && echo '[create_templates.modal-big]' >> {cfg}"
            f" && echo 'transfer = \"none\"' >> {cfg}"
            f' && echo \'env = ["STACKED_FROM_MODAL_BIG=1", "OVERRIDDEN=modal_big"]\' >> {cfg}'
            f" && echo '[create_templates.with-tests]' >> {cfg}"
            f' && echo \'env__extend = ["STACKED_FROM_WITH_TESTS=1", "OVERRIDDEN=with_tests"]\' >> {cfg}',
            comment="define stackable modal-big and with-tests templates",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.tmux
def test_create_stack_templates(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stack multiple templates (later templates override earlier ones)
        mngr create my-task --template modal-big --template with-tests
    """)
    _write_stacking_templates(e2e)
    expect(
        e2e.run(
            "mngr create my-task --template modal-big --template with-tests --type command --no-ensure-clean --no-connect -- sleep 100942",
            comment="stack multiple templates",
        )
    ).to_succeed()
    # Inspect the running agent's environment to confirm the stacking semantics.
    env_result = e2e.run(
        "mngr exec my-task 'printenv'",
        comment="read the agent environment to verify both templates applied",
    )
    expect(env_result).to_succeed()
    # Both templates contributed: the agent has env vars from each.
    assert "STACKED_FROM_MODAL_BIG=1" in env_result.stdout, env_result.stdout
    assert "STACKED_FROM_WITH_TESTS=1" in env_result.stdout, env_result.stdout
    # Later template overrides earlier: with-tests wins for the shared key.
    assert "OVERRIDDEN=with_tests" in env_result.stdout, env_result.stdout
    assert "OVERRIDDEN=modal_big" not in env_result.stdout, env_result.stdout

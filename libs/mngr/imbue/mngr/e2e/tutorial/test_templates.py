"""Tests for the create-template tutorial blocks."""

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


def _write_with_tests_template(e2e: E2eSession) -> None:
    cfg = ".$MNGR_ROOT_NAME/settings.local.toml"
    expect(
        e2e.run(
            f"echo '' >> {cfg}"
            f" && echo '[create_templates.with-tests]' >> {cfg}"
            f" && echo 'ensure_clean = false' >> {cfg}",
            comment="define with-tests template stub",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
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
    # Simulate the in-editor edit by appending the template to the project
    # config that `config edit` just created. is_allowed_in_pytest opts this
    # newly created project config into the pytest run (it defaults to False,
    # so without it the config loader would refuse to load it during the test).
    project_cfg = ".$MNGR_ROOT_NAME/settings.toml"
    expect(
        e2e.run(
            f"echo '' >> {project_cfg}"
            f" && echo 'is_allowed_in_pytest = true' >> {project_cfg}"
            f" && echo '[create_templates.modal-big]' >> {project_cfg}"
            f" && echo 'transfer = \"none\"' >> {project_cfg}",
            comment="in the editor, add the modal-big template (transfer=none substituted for the local test)",
        )
    ).to_succeed()
    expect(
        e2e.run(
            "mngr create my-task --template modal-big --type command --no-ensure-clean --no-connect -- sleep 100940",
            comment="use the template when creating agents",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.tmux
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

    # The modal-big template is substituted with transfer=none for this local
    # test, so the template taking effect means the agent runs in-place: its work
    # directory is the session cwd rather than a generated worktree. Verify with
    # `mngr exec`, which targets the local agent directly and avoids cross-provider
    # discovery, so we assert the template's concrete effect, not just exit 0.
    session_pwd = e2e.run("pwd", comment="get the session cwd for comparison")
    expect(session_pwd).to_succeed()
    agent_pwd = e2e.run("mngr exec my-task pwd", comment="confirm the agent was created and runs in-place")
    expect(agent_pwd).to_succeed()
    # exec appends a status line after the command's output, so take the first line.
    agent_work_dir = agent_pwd.stdout.strip().splitlines()[0]
    assert os.path.realpath(agent_work_dir) == os.path.realpath(session_pwd.stdout.strip()), (
        "Expected the modal-big template (transfer=none) to run the agent in-place.\n"
        f"  agent pwd:   {agent_work_dir}\n"
        f"  session cwd: {session_pwd.stdout.strip()}"
    )


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_stack_templates(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
        # stack multiple templates (later templates override earlier ones)
        mngr create my-task --template modal-big --template with-tests
    """)
    _write_modal_big_template(e2e)
    _write_with_tests_template(e2e)
    expect(
        e2e.run(
            "mngr create my-task --template modal-big --template with-tests --type command --no-ensure-clean --no-connect -- sleep 100942",
            comment="stack multiple templates",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.timeout(60)
def test_create_stack_templates_with_unknown_template_fails(e2e: E2eSession) -> None:
    """Stacking an undefined template name fails fast with a clear error.

    This exercises the same --template stacking block as the happy-path test,
    but on the unhappy path: a name that is not defined in any config scope is
    rejected during template resolution (before any agent/host is created), so
    no tmux or remote provider is touched.
    """
    e2e.write_tutorial_block("""
        # stack multiple templates (later templates override earlier ones)
        mngr create my-task --template modal-big --template with-tests
    """)
    _write_modal_big_template(e2e)
    result = e2e.run(
        "mngr create my-task --template modal-big --template does-not-exist --type command --no-ensure-clean --no-connect -- sleep 100943",
        comment="stack a defined template with an undefined one",
    )
    expect(result).to_fail()
    # The error must name the offending template and not silently fall back to
    # only applying the templates that do exist.
    expect(result.stderr).to_contain("does-not-exist")
    expect(result.stderr).to_contain("not found")

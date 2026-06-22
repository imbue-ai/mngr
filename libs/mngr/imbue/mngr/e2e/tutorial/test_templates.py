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


def _write_worktree_template(e2e: E2eSession) -> None:
    """Add a template that sets transfer=git-worktree (the opposite of modal-big).

    Used to demonstrate template override ordering: stacked against modal-big
    (transfer=none), whichever template is listed last wins.
    """
    cfg = ".$MNGR_ROOT_NAME/settings.local.toml"
    expect(
        e2e.run(
            f"echo '' >> {cfg}"
            f" && echo '[create_templates.worktree]' >> {cfg}"
            f" && echo 'transfer = \"git-worktree\"' >> {cfg}",
            comment="define worktree template (transfer=git-worktree)",
        )
    ).to_succeed()


def _assert_agent_runs_in_place(e2e: E2eSession, agent_name: str) -> None:
    """Assert the agent's work dir is the session cwd, i.e. transfer=none took effect.

    `mngr exec` targets the local agent directly and avoids cross-provider
    discovery, so this asserts a template's concrete effect rather than just
    a zero exit code.
    """
    session_pwd = e2e.run("pwd", comment="get the session cwd for comparison")
    expect(session_pwd).to_succeed()
    agent_pwd = e2e.run(f"mngr exec {agent_name} pwd", comment="confirm the agent was created and runs in-place")
    expect(agent_pwd).to_succeed()
    # exec appends a status line after the command's output, so take the first line.
    agent_work_dir = agent_pwd.stdout.strip().splitlines()[0]
    assert os.path.realpath(agent_work_dir) == os.path.realpath(session_pwd.stdout.strip()), (
        "Expected the transfer=none template to run the agent in-place.\n"
        f"  agent pwd:   {agent_work_dir}\n"
        f"  session cwd: {session_pwd.stdout.strip()}"
    )


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
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
    # `config edit` creates the project config from a template when it does not
    # yet exist. Confirm that actually happened (not just exit 0) by reading the
    # file back: the freshly-created file carries the template's header comment.
    project_cfg = ".$MNGR_ROOT_NAME/settings.toml"
    created_cfg = e2e.run(f"cat {project_cfg}", comment="confirm config edit created the project config")
    expect(created_cfg).to_succeed()
    expect(created_cfg.stdout).to_contain("mngr configuration file")
    # Simulate the in-editor edit by appending the template to the project
    # config that `config edit` just created. is_allowed_in_pytest opts this
    # newly created project config into the pytest run (it defaults to False,
    # so without it the config loader would refuse to load it during the test).
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

    # Verify the template actually took effect, not just that create exited 0.
    # The modal-big template is substituted with transfer=none for this local
    # test, so applying it makes the agent run in-place: its work directory is
    # the session cwd rather than a generated worktree. `mngr exec` targets the
    # local agent directly, so we assert the template's concrete effect.
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
    # directory is the session cwd rather than a generated worktree.
    _assert_agent_runs_in_place(e2e, "my-task")


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

    # Both templates contribute non-overlapping keys, so stacking should apply
    # modal-big's transfer=none: the agent runs in-place (work dir == session
    # cwd) rather than in a generated worktree. Assert the concrete effect, not
    # just a zero exit code.
    _assert_agent_runs_in_place(e2e, "my-task")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_stack_templates_later_template_overrides_earlier(e2e: E2eSession) -> None:
    """Later templates override earlier ones when they set the same key.

    The happy-path stacking test uses templates with non-overlapping keys, so it
    cannot exercise the override ordering that the tutorial block calls out
    ("later templates override earlier ones"). Here both templates set the same
    key (``transfer``) with conflicting values, and we list modal-big
    (transfer=none) last so its value must win: the agent runs in-place. If
    ordering were reversed (earlier wins), the worktree template's
    transfer=git-worktree would take effect and the agent would not run in-place.
    """
    e2e.write_tutorial_block("""
        # stack multiple templates (later templates override earlier ones)
        mngr create my-task --template modal-big --template with-tests
    """)
    _write_modal_big_template(e2e)
    _write_worktree_template(e2e)
    expect(
        e2e.run(
            "mngr create my-task --template worktree --template modal-big --type command --no-ensure-clean --no-connect -- sleep 100944",
            comment="stack templates with conflicting transfer; the later (modal-big) must win",
        )
    ).to_succeed()
    _assert_agent_runs_in_place(e2e, "my-task")


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
    # The error should also surface the templates that *are* defined, proving
    # resolution distinguished the valid template (modal-big) from the bogus one
    # rather than failing for some unrelated reason.
    expect(result.stderr).to_contain("modal-big")
    # Resolution fails before any agent/host is created, so the failed create
    # must not leave a partially-created `my-task` agent behind. `mngr list
    # --ids` prints one id per agent; it should be empty here. Scope to the
    # local provider so this stays fast and avoids cross-provider discovery
    # (this test is intentionally not marked modal/tmux).
    list_result = e2e.run(
        "mngr list --provider local --ids",
        comment="confirm the failed create left no agent behind",
    )
    expect(list_result).to_succeed()
    assert list_result.stdout.strip() == "", (
        "Expected no agent to be created when template resolution fails, but "
        f"`mngr list --ids` returned: {list_result.stdout!r}"
    )

"""Tests for the create-template tutorial blocks."""

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
            f" && echo 'no_ensure_clean = true' >> {cfg},",
            comment="define with-tests template stub",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
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
    _write_modal_big_template(e2e)
    expect(
        e2e.run(
            "mngr create my-task --template modal-big --type command --no-ensure-clean --no-connect -- sleep 100940",
            comment="use the template when creating agents",
        )
    ).to_succeed()


@pytest.mark.release
@pytest.mark.modal
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


@pytest.mark.release
@pytest.mark.modal
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

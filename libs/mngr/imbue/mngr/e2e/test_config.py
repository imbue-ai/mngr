"""Tests for config and template behavior via the real CLI."""

import json
import os

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_create_with_template(e2e: E2eSession) -> None:
    e2e.write_tutorial_block("""
    # you can use templates to quickly apply a set of preconfigured options:
    echo '[create_templates.my_modal_template]' >> .mngr/settings.local.toml
    echo 'provider = "modal"' >> .mngr/settings.local.toml
    echo 'build_arg = ["cpu=4"]' >> .mngr/settings.local.toml
    mngr create my-task --template my_modal_template
    # templates are defined in your config (see the CONFIGURATION section for more) and can be stacked: --template modal --template codex
    # templates take exactly the same parameters as the create command
    # -t is short for --template. Many commands have a short form (see the "--help")
    """)
    # Write a template that sets transfer=none (so agent runs in-place). The
    # tutorial demonstrates a Modal template; we use a local transfer=none
    # template so the test stays entirely local (no remote provider needed).
    cfg = ".$MNGR_ROOT_NAME/settings.local.toml"
    expect(
        e2e.run(
            f"echo '' >> {cfg}"
            f" && echo '[create_templates.my_local_template]' >> {cfg}"
            f" && echo 'transfer = \"none\"' >> {cfg}",
            comment="Write a template that sets transfer=none",
        )
    ).to_succeed()

    # Create an agent using the template
    expect(
        e2e.run(
            "mngr create my-task --template my_local_template --type command --no-ensure-clean --no-connect -- sleep 100069",
            comment="Create agent using template",
        )
    ).to_succeed()

    # The template's transfer=none must place the agent in-place, i.e. its work
    # directory is exactly the session cwd rather than a generated worktree.
    pwd_result = e2e.run("pwd", comment="Get the session cwd for comparison")
    expect(pwd_result).to_succeed()
    session_cwd = pwd_result.stdout.strip()

    list_result = e2e.run("mngr list --format json", comment="Verify template settings applied")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    work_dir = matching[0]["work_dir"]
    assert os.path.realpath(work_dir) == os.path.realpath(session_cwd), (
        f"Expected the template (transfer=none) to run the agent in-place.\n"
        f"  work_dir: {work_dir}\n  session cwd: {session_cwd}"
    )

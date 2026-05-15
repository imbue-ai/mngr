"""Tests for config and template behavior via the real CLI."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
@pytest.mark.timeout(60)
def test_create_with_template(e2e: E2eSession) -> None:
    # Write a template that sets transfer=none (so agent runs in-place)
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

    # Verify the template was applied: with transfer="none", the agent should
    # run in-place (work_dir equals the source directory).
    list_result = e2e.run("mngr list --format json", comment="Verify template settings applied")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1

    pwd_result = e2e.run("pwd", comment="Get the source directory path")
    expect(pwd_result).to_succeed()
    source_dir = pwd_result.stdout.strip()
    assert matching[0]["work_dir"] == source_dir, (
        f"Expected in-place work_dir from template (transfer=none), got: {matching[0]['work_dir']}"
    )

    # With transfer="none", no new mngr/* branch should be created either.
    branch_result = e2e.run("git branch", comment="Verify no mngr/* branch was created")
    expect(branch_result).to_succeed()
    assert "mngr/my-task" not in branch_result.stdout

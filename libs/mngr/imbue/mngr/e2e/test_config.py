"""Tests for config and template behavior via the real CLI."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.modal
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
            "mngr create my-task --template my_local_template --command 'sleep 99999' --no-ensure-clean --no-connect",
            comment="Create agent using template",
        )
    ).to_succeed()

    # Verify the template was applied: work_dir should not contain "worktrees"
    list_result = e2e.run("mngr list --format json", comment="Verify template settings applied")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    assert "worktrees" not in matching[0]["work_dir"], (
        f"Expected in-place work_dir (no worktree) from template, got: {matching[0]['work_dir']}"
    )

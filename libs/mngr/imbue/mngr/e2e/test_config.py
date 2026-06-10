"""Tests for config and template behavior via the real CLI."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect


@pytest.mark.release
@pytest.mark.tmux
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

    # Verify the template was applied: work_dir should not contain "worktrees".
    # Scope discovery to the local provider so the test does not fan out to the
    # (slow, network-bound) Modal provider -- the template runs the agent
    # in-place locally, so there is nothing to learn from remote providers.
    list_result = e2e.run("mngr list --provider local --format json", comment="Verify template settings applied")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    work_dir = matching[0]["work_dir"]
    assert "worktrees" not in work_dir, f"Expected in-place work_dir (no worktree) from template, got: {work_dir}"

    # Confirm the agent is genuinely running in-place by observing its actual
    # runtime working directory (not just the metadata reported by `mngr list`).
    # With transfer=none the agent's cwd must be the repo itself, which is what
    # `work_dir` points at -- a worktree-based transfer would put it elsewhere.
    pwd_result = e2e.run("mngr exec my-task pwd", comment="Confirm the agent runs in-place in the repo")
    expect(pwd_result).to_succeed()
    expect(pwd_result.stdout).to_contain(work_dir)


@pytest.mark.release
def test_create_with_nonexistent_template(e2e: E2eSession) -> None:
    """Unhappy path: creating with an unknown template fails with a helpful error.

    Shares the templates tutorial block with ``test_create_with_template``: it
    defines one real template so the error path can report the set of available
    templates, then references a template name that was never configured.
    """
    # Write a real template so there is at least one available template to report.
    cfg = ".$MNGR_ROOT_NAME/settings.local.toml"
    expect(
        e2e.run(
            f"echo '' >> {cfg}"
            f" && echo '[create_templates.my_local_template]' >> {cfg}"
            f" && echo 'transfer = \"none\"' >> {cfg}",
            comment="Write a template that sets transfer=none",
        )
    ).to_succeed()

    # Reference a template that does not exist -- create must refuse before
    # provisioning any agent.
    result = e2e.run(
        "mngr create my-task --template does_not_exist --type command --no-ensure-clean --no-connect -- sleep 100069",
        comment="Create with a template name that was never configured",
    )
    expect(result).to_fail()
    # The error should name the missing template and list the configured ones,
    # so the user can correct the typo.
    expect(result.stderr).to_contain("does_not_exist")
    expect(result.stderr).to_contain("not found")
    expect(result.stderr).to_contain("my_local_template")

    # No agent should have been created by the failed command.
    list_result = e2e.run("mngr list --provider local --format json", comment="Confirm no agent was created")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    assert [a for a in agents if a["name"] == "my-task"] == []

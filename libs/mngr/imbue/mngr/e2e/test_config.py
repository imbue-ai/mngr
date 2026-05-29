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

    # Verify the template was applied: the default transfer mode for a same-host
    # git project is git-worktree (work_dir under a "worktrees" folder), so the
    # template's transfer=none is proven applied iff the agent runs in-place.
    list_result = e2e.run("mngr list --format json", comment="Verify template settings applied")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    matching = [a for a in agents if a["name"] == "my-task"]
    assert len(matching) == 1
    work_dir = matching[0]["work_dir"]
    assert "worktrees" not in work_dir, f"Expected in-place work_dir (no worktree) from template, got: {work_dir}"

    # Confirm in-place execution at runtime, not just in the recorded metadata:
    # the agent's actual working directory must match work_dir and be free of a
    # worktree path segment.
    pwd_result = e2e.run("mngr exec my-task pwd", comment="Verify agent runs in-place")
    expect(pwd_result).to_succeed()
    # `mngr exec` appends a trailing "Command succeeded on agent ..." status
    # line, so compare against the first non-empty output line (the pwd itself).
    pwd_lines = [line for line in pwd_result.stdout.splitlines() if line.strip()]
    assert pwd_lines and pwd_lines[0] == work_dir, (
        f"Expected agent's runtime cwd to match in-place work_dir {work_dir!r}, got output: {pwd_result.stdout!r}"
    )


@pytest.mark.release
def test_create_with_nonexistent_template(e2e: E2eSession) -> None:
    # Referencing an unknown template must fail fast with a clear error and
    # without leaving a half-created agent behind.
    create_result = e2e.run(
        "mngr create my-task --template no_such_template --type command --no-ensure-clean --no-connect -- sleep 100069",
        comment="Creating with an unknown template should fail",
    )
    expect(create_result).to_fail()
    expect(create_result.stderr).to_contain("not found")

    # No agent should have been created as a side effect of the failed command.
    list_result = e2e.run("mngr list --format json", comment="Verify no agent was created")
    expect(list_result).to_succeed()
    agents = json.loads(list_result.stdout)["agents"]
    assert not any(a["name"] == "my-task" for a in agents), f"Expected no agent to be created, got: {agents}"

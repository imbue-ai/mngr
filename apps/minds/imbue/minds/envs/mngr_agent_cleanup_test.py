import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.envs.mngr_agent_cleanup import MngrAgentCleanupError
from imbue.minds.envs.mngr_agent_cleanup import real_destroy_mngr_agents


@pytest.fixture
def _root_cg() -> Iterator[ConcurrencyGroup]:
    cg = ConcurrencyGroup(name="mngr-agent-cleanup-test-root")
    with cg:
        yield cg


def _make_fake_mngr_binary(tmp_path: Path) -> Path:
    """A fake ``mngr`` whose per-id exit code/output depends on the id arg.

    Invoked as ``mngr destroy -f <id>`` (so the id is ``$3``). It models
    three agents: one already gone (mngr's "not found" wording, non-zero
    exit), one that fails for a genuine reason, and any other id succeeds.
    """
    script = tmp_path / "mngr"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'agent_id="$3"\n'
        'case "$agent_id" in\n'
        '  gone-agent) echo "Agent gone-agent not found" >&2; exit 1 ;;\n'
        '  broken-agent) echo "docker error: container is stuck" >&2; exit 1 ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_destroy_all_succeed_is_noop(tmp_path: Path, _root_cg: ConcurrencyGroup) -> None:
    fake = _make_fake_mngr_binary(tmp_path)
    real_destroy_mngr_agents(["agent-a", "agent-b"], tmp_path, "minds-dev-", _root_cg, mngr_binary=str(fake))


def test_destroy_tolerates_already_gone_agent(tmp_path: Path, _root_cg: ConcurrencyGroup) -> None:
    # An agent that mngr reports as already gone is a no-op (manually
    # destroyed, or torn down as a host-mate of an earlier id).
    fake = _make_fake_mngr_binary(tmp_path)
    real_destroy_mngr_agents(["gone-agent"], tmp_path, "minds-dev-", _root_cg, mngr_binary=str(fake))


def test_real_failure_not_masked_by_a_gone_agent(tmp_path: Path, _root_cg: ConcurrencyGroup) -> None:
    # The regression this guards: a batch containing one already-gone agent
    # AND one genuinely-failing agent must NOT be swallowed as success just
    # because "not found" appears somewhere in the combined output.
    fake = _make_fake_mngr_binary(tmp_path)
    with pytest.raises(MngrAgentCleanupError) as exc_info:
        real_destroy_mngr_agents(
            ["gone-agent", "broken-agent"], tmp_path, "minds-dev-", _root_cg, mngr_binary=str(fake)
        )
    message = str(exc_info.value)
    assert "broken-agent" in message
    assert "container is stuck" in message
    # The env root must be reported as NOT removed so the operator re-runs.
    assert "NOT been removed" in message

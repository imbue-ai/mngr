"""Release test: full end-to-end lifecycle of a real mngr-managed pi agent.

Drives the real ``mngr`` CLI against the real ``pi`` binary and a real model through
the shared agent release lifecycle (create -> WAITING -> message -> RUNNING ->
transcript -> stop/start resume -> destroy).

pi exercises the richest end of the shared lifecycle: it observes the RUNNING marker,
forces a bash tool call (so the transcript carries a tool_result), and reports token
usage.

The git source includes a ``.agents/skills`` dir, which would trip pi 0.79+'s "Trust
project folder?" dialog; ``mngr create --yes`` makes the plugin pre-seed trust, so a
regression there would stall the first message and fail the lifecycle assertions.

Requires ``pi`` on PATH and ``ANTHROPIC_API_KEY`` in the environment; skipped otherwise.
"""

from pathlib import Path

import pytest

from imbue.mngr.agents.agent_release_testing import run_agent_release_lifecycle
from imbue.mngr_pi_coding.testing import PiReleaseProfile


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_pi_agent_full_lifecycle(tmp_path: Path) -> None:
    """Drive a real pi agent through the full mngr lifecycle and assert each stage produced its effect.

    Runs the shared agent release arc against the real ``mngr`` CLI, the real ``pi`` binary, and a
    real model: create -> WAITING, send a message -> RUNNING (observing the RUNNING marker), capture
    the transcript (which must carry a forced bash tool_result and reported token usage), stop/start
    to resume, adopt the preserved native pi session, and destroy. The assertions inside
    ``run_agent_release_lifecycle`` fail if any stage is a no-op -- e.g. the agent never reaches
    WAITING, the message yields no RUNNING transition, the transcript lacks the tool_result or usage,
    or resume/adopt loses the preserved session -- so passing proves pi's plumbing actually drove the
    lifecycle end to end.
    """
    run_agent_release_lifecycle(PiReleaseProfile(), tmp_path)

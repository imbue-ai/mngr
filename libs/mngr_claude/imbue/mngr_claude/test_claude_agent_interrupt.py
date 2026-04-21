"""Acceptance tests for ClaudeAgent.interrupt_current_turn against real tmux.

These tests spin up a real tmux session with a long-running shell process that
traps SIGINT and writes a marker to the pane. They verify end-to-end that:

1. When the agent is RUNNING, interrupt_current_turn delivers Ctrl-C through
   the actual tmux send-keys path (catching shell-quoting, target-spec, or
   host-command bugs that pure unit mocks can miss).
2. When the agent is WAITING, no Ctrl-C is delivered and the shell keeps
   running undisturbed (the no-op-when-idle contract).

A fake shell is used in place of real Claude because Claude Code itself is
already known to treat Ctrl-C as "abort turn, keep session alive". What these
tests are validating is the mngr-side plumbing, not Claude's interrupt
semantics.
"""

import shlex
from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import cleanup_tmux_session
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude.plugin_test import make_claude_agent

_INTERRUPT_MARKER = "__INTERRUPTED__"
# Shell that sets up a SIGINT trap printing the marker, then loops forever.
# Ctrl-C delivered via tmux should cause the marker to appear in the pane.
_VICTIM_SHELL = f"trap 'echo {_INTERRUPT_MARKER}' INT; echo READY; while true; do sleep 1; done"


class _FixedLifecycleClaudeAgent(ClaudeAgent):
    """ClaudeAgent variant whose lifecycle state is set directly, bypassing the real check.

    Used to isolate interrupt_current_turn's behavior from the real tmux/ps-based
    lifecycle detection (which would require a real Claude process to reach RUNNING).
    """

    fake_state: AgentLifecycleState = AgentLifecycleState.RUNNING

    def get_lifecycle_state(self) -> AgentLifecycleState:
        return self.fake_state


def _make_fixed_state_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    mngr_ctx: MngrContext,
    state: AgentLifecycleState,
) -> _FixedLifecycleClaudeAgent:
    """Build a ClaudeAgent that reports the given lifecycle state unconditionally."""
    base, _ = make_claude_agent(local_provider, tmp_path, mngr_ctx)
    # model_construct from the base agent's fields + the test-only fake_state override.
    return _FixedLifecycleClaudeAgent.model_construct(
        **base.__dict__,
        fake_state=state,
    )


def _start_victim_session(agent: ClaudeAgent) -> None:
    """Start a detached tmux session running the SIGINT-trapping shell."""
    agent.host.execute_idempotent_command(
        f"tmux new-session -d -s {shlex.quote(agent.session_name)} {shlex.quote(_VICTIM_SHELL)}",
        timeout_seconds=5.0,
    )
    wait_for(
        lambda: agent._check_pane_contains(agent.tmux_target, "READY"),
        timeout=5.0,
        error_message="Victim shell did not reach READY in tmux pane",
    )


@pytest.mark.acceptance
@pytest.mark.tmux
def test_interrupt_current_turn_delivers_ctrl_c_to_tmux_pane(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """When RUNNING, interrupt_current_turn must deliver SIGINT to the pane's foreground process."""
    agent = _make_fixed_state_agent(local_provider, tmp_path, temp_mngr_ctx, AgentLifecycleState.RUNNING)
    session_name = agent.session_name

    try:
        _start_victim_session(agent)

        agent.interrupt_current_turn()

        wait_for(
            lambda: agent._check_pane_contains(agent.tmux_target, _INTERRUPT_MARKER),
            timeout=5.0,
            error_message="SIGINT marker did not appear in tmux pane after interrupt_current_turn",
        )
    finally:
        cleanup_tmux_session(session_name)


@pytest.mark.acceptance
@pytest.mark.tmux
def test_interrupt_current_turn_does_not_deliver_ctrl_c_when_idle(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    """When state != RUNNING, the tmux pane must remain undisturbed (no SIGINT delivered)."""
    agent = _make_fixed_state_agent(local_provider, tmp_path, temp_mngr_ctx, AgentLifecycleState.WAITING)
    session_name = agent.session_name

    try:
        _start_victim_session(agent)

        agent.interrupt_current_turn()

        # Give any (erroneous) interrupt a chance to reach the pane. If the
        # no-op contract is upheld, the marker will never appear.
        with pytest.raises(TimeoutError):
            wait_for(
                lambda: agent._check_pane_contains(agent.tmux_target, _INTERRUPT_MARKER),
                timeout=1.5,
                error_message="(expected)",
            )
    finally:
        cleanup_tmux_session(session_name)

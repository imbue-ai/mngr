from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_claude.testing import AGENT_DESTROY_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import TURN_PROGRESS_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import append_project_settings
from imbue.mngr_claude.testing import capture_agent_pane
from imbue.mngr_claude.testing import commit_hook_payload_saver
from imbue.mngr_claude.testing import created_agent
from imbue.mngr_claude.testing import drift_note
from imbue.mngr_claude.testing import read_saved_payloads
from imbue.mngr_claude.testing import wait_for_agent_to_stop_running

_BYPASS_PERMISSIONS_FLAG: Final[str] = "--dangerously-skip-permissions"
# The mode that asks before a tool runs, set explicitly so a different default cannot skip the request.
_DEFAULT_PERMISSION_MODE: Final[str] = "default"
_PERMISSION_REQUEST_EVENT: Final[str] = "PermissionRequest"
_AUTO_ALLOW_CONSUMER: Final[str] = "claude_config.build_permission_auto_allow_hooks_config"


class _AutoAllowClaudeReleaseProfile(ClaudeReleaseProfile):
    """A claude release profile whose agent asks for tool permission, which mngr's auto-allow hook grants."""

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        ctx = super().setup(tmp_path)
        append_project_settings(ctx, "\n[agent_types.claude]\nauto_allow_permissions = true\n")
        return ctx

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        args = [arg for arg in super().create_extra_args(ctx) if arg != _BYPASS_PERMISSIONS_FLAG]
        return [*args, "--permission-mode", _DEFAULT_PERMISSION_MODE]


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_auto_allowed_agent_runs_a_tool_that_needs_permission(tmp_path: Path) -> None:
    """With auto_allow_permissions, an agent outside bypass mode runs a tool that needs permission, unattended."""
    profile = _AutoAllowClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    dump_dir = tmp_path / "permission-request-payloads"
    commit_hook_payload_saver(ctx.project_dir, dump_dir, (_PERMISSION_REQUEST_EVENT,), "*")
    tool_marker = tmp_path / "permitted-tool-ran"
    agent_name = f"claude-autoallow-{get_short_random_string()}"
    drift = drift_note(ctx, "the permission request hook")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        send_expecting_success(
            profile,
            ctx,
            agent_name,
            f"Use the Bash tool to run exactly: touch {shlex.quote(str(tool_marker))}. Then reply with just DONE.",
        )

        assert poll_until(
            lambda: any(payload.get("tool_name") == "Bash" for payload in read_saved_payloads(dump_dir)),
            timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
            poll_interval=1.0,
        ), (
            f"claude raised no {_PERMISSION_REQUEST_EVENT} hook event for the Bash tool outside bypass mode {drift}; "
            f"tool_names seen: {[p.get('tool_name') for p in read_saved_payloads(dump_dir)]}; "
            f"the Bash command ran anyway: {tool_marker.exists()}; "
            f"consumer: {_AUTO_ALLOW_CONSUMER}"
        )

        assert poll_until(tool_marker.exists, timeout=TURN_PROGRESS_TIMEOUT_SECONDS, poll_interval=1.0), (
            f"the Bash call that needed permission never ran, so claude did not take mngr's allow decision {drift}; "
            f"pane:\n{capture_agent_pane(ctx, agent_name)}\nconsumer: {_AUTO_ALLOW_CONSUMER}"
        )
        wait_for_agent_to_stop_running(
            profile,
            ctx,
            timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
            failure=f"the auto-allowed turn never ended {drift}; consumer: {_AUTO_ALLOW_CONSUMER}",
        )

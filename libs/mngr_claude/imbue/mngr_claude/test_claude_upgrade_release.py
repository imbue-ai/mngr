import subprocess
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import agent_state_dir
from imbue.mngr.agents.agent_release_testing import marker_path
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.hosts.background_tasks import BACKGROUND_TASK_DIR_NAME
from imbue.mngr.primitives import BackgroundTaskKind
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import read_background_task_records_in
from imbue.mngr_claude.testing import AGENT_DESTROY_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import BACKGROUND_SLEEP_PROMPT
from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import LocalLoginClaudeProfile
from imbue.mngr_claude.testing import RAW_TRANSCRIPT_RELPATH
from imbue.mngr_claude.testing import SESSION_RECORD_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import TURN_PROGRESS_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import created_agent
from imbue.mngr_claude.testing import read_session_records
from imbue.mngr_claude.testing import records_after_user_prompt
from imbue.mngr_claude.testing import session_message_text

_UPGRADE_TIMEOUT_SECONDS: Final[float] = 300.0
_RECALL_PROMPT: Final[str] = "What is the secret I asked you to remember? Do not use any tools. Reply with just it."


def _read_raw_records_from(ctx: AgentReleaseContext, first_record_idx: int) -> list[dict[str, Any]]:
    """The records of the agent's raw transcript from ``first_record_idx`` on."""
    raw_transcript = agent_state_dir(ctx.host_dir) / RAW_TRANSCRIPT_RELPATH
    return read_session_records(raw_transcript)[first_record_idx:] if raw_transcript.exists() else []


def _is_prompt_recorded(records: Sequence[Mapping[str, Any]], prompt: str) -> bool:
    return any(record.get("type") == "user" and prompt in session_message_text(record) for record in records)


def _reply_texts_to(records: Sequence[Mapping[str, Any]], prompt: str) -> list[str]:
    """The non-empty texts of the assistant records after the user record carrying ``prompt``."""
    texts = (
        session_message_text(record).strip()
        for record in records_after_user_prompt(records, prompt)
        if record.get("type") == "assistant"
    )
    return [text for text in texts if text != ""]


def _send_and_wait_for_turn_end(
    profile: ClaudeReleaseProfile, ctx: AgentReleaseContext, agent_name: str, prompt: str
) -> str:
    """Send ``prompt``, wait until its turn ended, and return the agent's reply from the raw transcript."""
    prior_record_count = len(_read_raw_records_from(ctx, 0))
    send_expecting_success(profile, ctx, agent_name, prompt)
    assert poll_until(
        lambda: _is_prompt_recorded(_read_raw_records_from(ctx, prior_record_count), prompt),
        timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
        poll_interval=1.0,
    ), f"the prompt {prompt!r} did not reach {RAW_TRANSCRIPT_RELPATH} within {TURN_PROGRESS_TIMEOUT_SECONDS:.0f}s"
    # The turn has begun, so a marker that is gone was cleared by its end.
    assert poll_until(
        lambda: not marker_path(ctx.host_dir).exists(), timeout=TURN_PROGRESS_TIMEOUT_SECONDS, poll_interval=1.0
    ), f"the turn {prompt!r} did not end within {TURN_PROGRESS_TIMEOUT_SECONDS:.0f}s: the `active` marker is still set"
    assert poll_until(
        lambda: bool(_reply_texts_to(_read_raw_records_from(ctx, prior_record_count), prompt)),
        timeout=SESSION_RECORD_TIMEOUT_SECONDS,
        poll_interval=1.0,
    ), (
        f"the turn {prompt!r} ended, but no reply to it reached {RAW_TRANSCRIPT_RELPATH} "
        f"within {SESSION_RECORD_TIMEOUT_SECONDS:.0f}s"
    )
    return " ".join(_reply_texts_to(_read_raw_records_from(ctx, prior_record_count), prompt))


def _list_supervisor_pids(state_dir: Path) -> list[str]:
    """The pids of the agent's claude_background_tasks.sh supervisors."""
    result = subprocess.run(
        ["pgrep", "-f", str(state_dir / "commands" / "claude_background_tasks.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.split()


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_upgrade_moves_an_agent_onto_new_hooks_and_keeps_its_conversation(tmp_path: Path) -> None:
    """An agent whose files predate background task records writes them after upgrade, and keeps its conversation."""
    profile = LocalLoginClaudeProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"claude-upgrade-{get_short_random_string()}"
    secret = f"SECRET-{get_short_random_string()}"
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        state_dir = agent_state_dir(ctx.host_dir)
        records_dir = state_dir / BACKGROUND_TASK_DIR_NAME
        _send_and_wait_for_turn_end(
            profile, ctx, agent_name, f"Remember this exact secret for later: {secret}. Reply with just OK."
        )

        # Remove the record writer, as on an agent created before it existed: backgrounded work leaves no record.
        (state_dir / "commands" / "mngr_background_task_lib.sh").unlink()
        _send_and_wait_for_turn_end(profile, ctx, agent_name, BACKGROUND_SLEEP_PROMPT)
        assert read_background_task_records_in(records_dir) == ()
        data_before = (state_dir / "data.json").read_bytes()
        supervisor_pids_before = _list_supervisor_pids(state_dir)
        assert len(supervisor_pids_before) == 1, supervisor_pids_before

        upgrade = profile.run_mngr(ctx, "upgrade", agent_name, timeout=_UPGRADE_TIMEOUT_SECONDS)

        assert upgrade.returncode == 0, f"upgrade failed:\n{upgrade.stdout}\n{upgrade.stderr}"
        assert f"upgraded {agent_name} (restarted)" in upgrade.stdout, upgrade.stdout
        assert (state_dir / "commands" / "mngr_background_task_lib.sh").is_file()
        assert (state_dir / "data.json").read_bytes() == data_before
        supervisor_pids_after = _list_supervisor_pids(state_dir)
        assert len(supervisor_pids_after) == 1 and supervisor_pids_after != supervisor_pids_before, (
            f"expected one supervisor started by the upgrade, found {supervisor_pids_after} "
            f"(before the upgrade: {supervisor_pids_before})"
        )

        assert secret in _send_and_wait_for_turn_end(profile, ctx, agent_name, _RECALL_PROMPT)

        _send_and_wait_for_turn_end(profile, ctx, agent_name, BACKGROUND_SLEEP_PROMPT)
        assert [record.kind for record in read_background_task_records_in(records_dir)] == [BackgroundTaskKind.SHELL]

        second_upgrade = profile.run_mngr(ctx, "upgrade", agent_name, timeout=_UPGRADE_TIMEOUT_SECONDS)

        assert second_upgrade.returncode == 0, f"upgrade failed:\n{second_upgrade.stdout}\n{second_upgrade.stderr}"
        assert f"current {agent_name}" in second_upgrade.stdout, second_upgrade.stdout

from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import RECALL_PROMPT
from imbue.mngr.agents.agent_release_testing import agent_state_dir
from imbue.mngr.agents.agent_release_testing import is_step_from
from imbue.mngr.agents.agent_release_testing import marker_path
from imbue.mngr.agents.agent_release_testing import read_common_records
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.agents.agent_release_testing import step_message
from imbue.mngr.agents.agent_release_testing import wait_for_records
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_pi_coding.testing import PiReleaseProfile

_CREATE_TIMEOUT_SECONDS: Final[float] = 600.0
_UPGRADE_TIMEOUT_SECONDS: Final[float] = 300.0
_TURN_TIMEOUT_SECONDS: Final[float] = 300.0
_DESTROY_TIMEOUT_SECONDS: Final[float] = 150.0


def _agent_texts_after_user_step(records: Sequence[Mapping[str, Any]], prompt: str) -> list[str]:
    """The messages of the agent steps after the last user step carrying ``prompt``."""
    user_step_indices = [
        idx for idx, record in enumerate(records) if is_step_from(record, "user") and prompt in step_message(record)
    ]
    if not user_step_indices:
        return []
    return [step_message(record) for record in records[user_step_indices[-1] + 1 :] if is_step_from(record, "agent")]


def _send_and_wait_for_reply(profile: PiReleaseProfile, ctx: AgentReleaseContext, agent_name: str, prompt: str) -> str:
    """Send ``prompt``, wait until the agent replied to it and its turn ended, and return the reply."""
    send_expecting_success(profile, ctx, agent_name, prompt)
    wait_for_records(
        ctx.host_dir,
        profile.common_transcript_subdir,
        lambda records: bool(_agent_texts_after_user_step(records, prompt)),
        timeout=_TURN_TIMEOUT_SECONDS,
        description=f"no reply to {prompt!r} reached the common transcript",
    )
    assert poll_until(
        lambda: not marker_path(ctx.host_dir).exists(), timeout=_TURN_TIMEOUT_SECONDS, poll_interval=1.0
    ), f"the turn {prompt!r} did not end within {_TURN_TIMEOUT_SECONDS:.0f}s: the `active` marker is still set"
    records = read_common_records(ctx.host_dir, profile.common_transcript_subdir)
    return " ".join(_agent_texts_after_user_step(records, prompt))


@pytest.mark.release
@pytest.mark.harness_internals(harness="pi-coding")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_upgrade_restarts_a_pi_agent_on_the_current_extension_and_keeps_its_conversation(tmp_path: Path) -> None:
    """An agent running an older mngr's extension restarts on this mngr's, and its extension then delivers a message the resumed session answers."""
    profile = PiReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"pi-upgrade-{get_short_random_string()}"
    secret = f"SECRET-{get_short_random_string()}"
    is_destroyed = False
    try:
        create = profile.run_mngr(
            ctx,
            "create",
            agent_name,
            profile.agent_type,
            "--no-connect",
            "--yes",
            *profile.create_extra_args(ctx),
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        assert create.returncode == 0, f"create failed:\n{create.stdout}\n{create.stderr}"
        _send_and_wait_for_reply(
            profile, ctx, agent_name, f"Remember this exact secret for later: {secret}. Reply with just OK."
        )

        # An older mngr's extension: it works like this mngr's, but its bytes differ.
        state_dir = agent_state_dir(ctx.host_dir)
        extension_path = state_dir / "commands" / "mngr_pi_lifecycle.ts"
        current_extension = extension_path.read_text()
        extension_path.write_text(current_extension + "\n// an older mngr's extension\n")
        data_before = (state_dir / "data.json").read_bytes()
        session_file_before = (state_dir / "pi_session_file").read_text()
        launched_before_ns = (state_dir / "pi_process_started").stat().st_mtime_ns

        upgrade = profile.run_mngr(ctx, "upgrade", agent_name, timeout=_UPGRADE_TIMEOUT_SECONDS)

        assert upgrade.returncode == 0, f"upgrade failed:\n{upgrade.stdout}\n{upgrade.stderr}"
        assert f"upgraded {agent_name} (restarted)" in upgrade.stdout, upgrade.stdout
        assert extension_path.read_text() == current_extension
        assert (state_dir / "data.json").read_bytes() == data_before
        assert (state_dir / "pi_process_started").stat().st_mtime_ns > launched_before_ns, (
            "upgrade did not relaunch pi"
        )
        assert (state_dir / "pi_session_started").exists()
        assert (state_dir / "pi_session_file").read_text() == session_file_before

        assert secret in _send_and_wait_for_reply(profile, ctx, agent_name, RECALL_PROMPT)

        second_upgrade = profile.run_mngr(ctx, "upgrade", agent_name, timeout=_UPGRADE_TIMEOUT_SECONDS)

        assert second_upgrade.returncode == 0, f"upgrade failed:\n{second_upgrade.stdout}\n{second_upgrade.stderr}"
        assert f"current {agent_name}" in second_upgrade.stdout, second_upgrade.stdout

        destroy = profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=_DESTROY_TIMEOUT_SECONDS)
        assert destroy.returncode == 0, f"destroy failed:\n{destroy.stdout}\n{destroy.stderr}"
        is_destroyed = True
    finally:
        try:
            if not is_destroyed:
                profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=_DESTROY_TIMEOUT_SECONDS)
        finally:
            if ctx.teardown is not None:
                ctx.teardown()

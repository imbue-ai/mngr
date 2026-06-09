"""Release test: full end-to-end flow for the opencode agent type.

Drives the real ``mngr`` CLI against the real ``opencode`` binary (no mocks):
create -> message -> RUNNING/WAITING lifecycle -> transcript -> resume across
stop/start -> destroy. Uses OpenCode's free OpenCode-Zen model so no API key is
required.

Release tests do NOT run in CI. Run manually::

    PYTEST_MAX_DURATION_SECONDS=1200 uv run pytest --no-cov --cov-fail-under=0 \\
        -n 0 -m release \\
        libs/mngr_opencode/imbue/mngr_opencode/test_opencode_agent.py

Requires ``opencode`` on PATH (an outbound network connection to OpenCode Zen is
used for the free model).
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import cleanup_tmux_session
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_mngr_subprocess

# A free model on the OpenCode Zen provider that needs no credentials. Pinned so
# the test does not depend on the developer's configured default model.
_FREE_MODEL = "opencode/deepseek-v4-flash-free"

_PROVISION_TIMEOUT_SECONDS = 240
_MESSAGE_TIMEOUT_SECONDS = 120
_RESPONSE_TIMEOUT_SECONDS = 240
_RUNNING_TIMEOUT_SECONDS = 90
_LIFECYCLE_TIMEOUT_SECONDS = 30

pytestmark = pytest.mark.skipif(
    shutil.which("opencode") is None,
    reason="Release test requires the `opencode` binary on PATH.",
)


def _build_subprocess_env(tmp_path: Path) -> dict[str, str]:
    """Build the subprocess env: seed the free model + disable remote providers.

    The autouse ``setup_test_mngr_env`` fixture has already redirected ``$HOME``
    to a tmp dir, so seeding ``~/.config/opencode/opencode.json`` writes into the
    sandbox (the agent inherits the model via ``sync_global_config``). Remote
    providers are disabled so ``mngr`` runs everything on the local host.
    """
    user_config = Path.home() / ".config" / "opencode" / "opencode.json"
    user_config.parent.mkdir(parents=True, exist_ok=True)
    user_config.write_text(json.dumps({"$schema": "https://opencode.ai/config.json", "model": _FREE_MODEL}))

    env = get_subprocess_test_env(root_name="mngr-opencode-release-test")
    project_config_dir = tmp_path / ".mngr-opencode-test"
    project_config_dir.mkdir(parents=True, exist_ok=True)
    (project_config_dir / "settings.local.toml").write_text(
        "is_allowed_in_pytest = true\n\n[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n"
    )
    env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)
    return env


def _find_agent_state_dir(env: dict[str, str]) -> Path:
    """Return the single agent state dir under the test's isolated host dir."""
    agents_dir = Path(env["MNGR_HOST_DIR"]) / "agents"
    candidates = [path for path in agents_dir.glob("*") if path.is_dir()]
    assert len(candidates) == 1, f"expected exactly one agent state dir under {agents_dir}, found {candidates}"
    return candidates[0]


def _read_common_transcript(agent_state_dir: Path) -> list[dict[str, Any]]:
    transcript_path = agent_state_dir / "events" / "opencode" / "common_transcript" / "events.jsonl"
    if not transcript_path.exists():
        return []
    return [json.loads(line) for line in transcript_path.read_text().splitlines() if line.strip()]


def _send_message(agent_name: str, message: str, env: dict[str, str]) -> None:
    run_mngr_subprocess("message", agent_name, "--message", message, env=env, timeout=float(_MESSAGE_TIMEOUT_SECONDS))


def _wait_for_idle(active_marker: Path) -> None:
    """Wait for the agent to finish its turn (the `active` marker is removed -> WAITING)."""
    wait_for(
        lambda: not active_marker.exists(),
        timeout=float(_RESPONSE_TIMEOUT_SECONDS),
        poll_interval=1.0,
        error_message="agent did not return to WAITING (active marker still present)",
    )


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(_PROVISION_TIMEOUT_SECONDS + _RUNNING_TIMEOUT_SECONDS + 4 * _RESPONSE_TIMEOUT_SECONDS + 120)
def test_opencode_agent_full_lifecycle(tmp_path: Path) -> None:
    """End-to-end: create, observe RUNNING->WAITING, capture transcript, resume across stop/start.

    Asserts the four parity features this branch adds, against the live
    ``opencode`` binary driven through the real ``mngr`` CLI:

    1. The agent reaches readiness and accepts a message (exercises the TUI ready
       banner + provisioning of the lifecycle plugin / transcript scripts).
    2. The ``active`` marker appears while the agent works (RUNNING) and is
       removed when it finishes (WAITING) -- this is exactly what
       ``BaseAgent.get_lifecycle_state`` reads.
    3. The common transcript captures the user prompt and the assistant reply.
    4. After ``mngr stop`` + ``mngr start``, the agent resumes the prior
       conversation and recalls a secret planted before the restart.
    """
    env = _build_subprocess_env(tmp_path)
    work_dir = tmp_path / "work"
    init_git_repo(work_dir, initial_commit=True)
    agent_name = f"oc-e2e-{get_short_random_string()}"
    session_name = f"{env['MNGR_PREFIX']}{agent_name}"

    run_mngr_subprocess(
        "create",
        agent_name,
        "opencode",
        "--no-connect",
        "--no-ensure-clean",
        "--yes",
        "--source",
        str(work_dir),
        env=env,
        timeout=float(_PROVISION_TIMEOUT_SECONDS),
    )

    try:
        agent_state_dir = _find_agent_state_dir(env)
        active_marker = agent_state_dir / "active"

        # (2) Lifecycle: a slow, multi-line response widens the RUNNING window so
        # the marker is reliably observable before it clears.
        _send_message(agent_name, "Count slowly from 1 to 20, one number per line, then say FINISHED.", env)
        wait_for(
            active_marker.exists,
            timeout=float(_RUNNING_TIMEOUT_SECONDS),
            poll_interval=0.2,
            error_message="active marker never appeared -> agent never reported RUNNING",
        )
        _wait_for_idle(active_marker)

        # (3) Transcript: the turn must be captured as a user + assistant message.
        def _has_user_and_assistant() -> bool:
            events = _read_common_transcript(agent_state_dir)
            has_user = any(e["type"] == "user_message" and "Count slowly" in e.get("content", "") for e in events)
            has_assistant = any(e["type"] == "assistant_message" and e.get("text") for e in events)
            return has_user and has_assistant

        wait_for(
            _has_user_and_assistant,
            timeout=float(_RESPONSE_TIMEOUT_SECONDS),
            poll_interval=2.0,
            error_message="common transcript did not capture the user prompt and assistant reply",
        )

        # (4) Resume: plant a secret, restart, and confirm the agent recalls it.
        secret = f"MARKER-{get_short_random_string()}"
        _send_message(agent_name, f"Remember this exact secret for later: {secret}. Reply with just OK.", env)
        _wait_for_idle(active_marker)

        run_mngr_subprocess("stop", agent_name, env=env, timeout=float(_LIFECYCLE_TIMEOUT_SECONDS))
        run_mngr_subprocess("start", agent_name, env=env, timeout=float(_PROVISION_TIMEOUT_SECONDS))

        _send_message(
            agent_name, "What was the exact secret I asked you to remember? Reply with just the secret.", env
        )

        def _recalled_secret() -> bool:
            return any(
                e["type"] == "assistant_message" and secret in e.get("text", "")
                for e in _read_common_transcript(agent_state_dir)
            )

        wait_for(
            _recalled_secret,
            timeout=float(_RESPONSE_TIMEOUT_SECONDS),
            poll_interval=2.0,
            error_message=f"agent did not recall the secret {secret!r} after stop/start -> resume failed",
        )
    finally:
        # Best-effort cleanup: run_mngr_subprocess only raises on timeout, not on
        # a nonzero exit, so a destroy failure won't mask the real test result.
        cleanup_tmux_session(session_name)
        run_mngr_subprocess("destroy", agent_name, "--force", env=env, timeout=float(_LIFECYCLE_TIMEOUT_SECONDS))

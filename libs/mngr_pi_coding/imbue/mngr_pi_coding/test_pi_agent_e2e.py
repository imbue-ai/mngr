"""Release test: full end-to-end lifecycle of a real mngr-managed pi agent.

Drives the actual mngr CLI (``create`` -> ``message`` -> ``stop`` -> ``start``
-> ``message`` -> ``destroy``) against a real ``pi`` binary and a real model,
exercising the four capabilities this plugin adds over the bare stub:

* **Readiness at create** -- ``mngr create`` only returns once the lifecycle
  extension's sentinel says pi can accept input.
* **RUNNING vs WAITING** -- the ``active`` marker is absent at rest, present
  while a turn runs, and absent again once it finishes.
* **Transcript capture** -- the common transcript is populated and
  ``mngr transcript`` renders it.
* **Resume across stop/start** -- after a stop/start the agent recalls a secret
  planted before the stop, proving the prior conversation was restored.

Release tests do not run in CI; run this manually with a real key::

    PYTEST_MAX_DURATION_SECONDS=900 ANTHROPIC_API_KEY=sk-ant-... \\
        uv run pytest --no-cov --cov-fail-under=0 -n 0 -m release \\
        libs/mngr_pi_coding/imbue/mngr_pi_coding/test_pi_agent_e2e.py
"""

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command

# A fast, cheap, tool-capable model keeps the real turns short.
_MODEL = "claude-haiku-4-5"

_CREATE_TIMEOUT_SECONDS = 600
_RESPONSE_TIMEOUT_SECONDS = 240
_DESTROY_TIMEOUT_SECONDS = 120

# The seed turn plants a UUID secret (so an accidental match against the model's
# own knowledge is effectively impossible) and forces a bash tool call, so the
# turn is long enough to reliably observe the RUNNING marker and so the
# transcript contains a tool_result.
_SEED_PROMPT_TEMPLATE = (
    "Remember this exact value for later: the secret answer is {secret}. "
    "Then use the bash tool to run exactly: echo SEEDED -- and finally reply with just ACK."
)
_RECALL_PROMPT = (
    "Earlier in this conversation I gave you a secret answer. "
    "Reply with just that secret answer, exactly as I gave it to you."
)


def _have_pi_credentials() -> bool:
    """Skip-guard: a real ``pi`` binary and ANTHROPIC_API_KEY are required."""
    return shutil.which("pi") is not None and bool(os.environ.get("ANTHROPIC_API_KEY"))


pytestmark = pytest.mark.skipif(
    not _have_pi_credentials(),
    reason="Release test requires ANTHROPIC_API_KEY in the environment and `pi` on PATH.",
)


@pytest.fixture
def subprocess_env(tmp_path: Path) -> dict[str, str]:
    """Env for ``uv run mngr`` subprocesses, isolated to this test.

    ``get_subprocess_test_env`` copies the os.environ that the autouse
    ``setup_test_mngr_env`` fixture already redirected to a temp HOME /
    MNGR_HOST_DIR / MNGR_PREFIX, and sets MNGR_ROOT_NAME so the repo's own
    ``.mngr`` config is not picked up. A project config is still required so
    that (a) the config loader's pytest guard is satisfied
    (``is_allowed_in_pytest``) and (b) the remote providers are disabled so
    ``mngr`` does not try to reach Modal/Docker. pi needs no trust file (it has
    no trust dialog), so unlike the claude release tests there is nothing
    else to seed.
    """
    env = get_subprocess_test_env(root_name="mngr-pi-release-test")
    project_config_dir = tmp_path / ".mngr-pi-test"
    project_config_dir.mkdir(parents=True, exist_ok=True)
    (project_config_dir / "settings.local.toml").write_text(
        "is_allowed_in_pytest = true\n\n[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n"
    )
    env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)
    return env


def _make_git_source(tmp_path: Path) -> Path:
    """A fresh git repo for ``mngr create --source`` (mngr requires a git source)."""
    work_dir = tmp_path / "pi-source"
    init_git_repo(work_dir, initial_commit=True)
    (work_dir / ".gitignore").write_text(".pi/\n")
    run_git_command(work_dir, "add", ".gitignore")
    run_git_command(work_dir, "commit", "-m", "add gitignore")
    return work_dir


def _run(
    args: list[str],
    env: dict[str, str],
    timeout: float = 120.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess, failing the test (with captured output) on non-zero exit."""
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed: {' '.join(args)}\n"
            f"  exit: {result.returncode}\n  stdout:\n{result.stdout}\n  stderr:\n{result.stderr}"
        )
    return result


def _agent_state_dir(env: dict[str, str]) -> Path:
    """The single agent's state dir under the isolated host dir."""
    agents_root = Path(env["MNGR_HOST_DIR"]) / "agents"
    agent_dirs = [p for p in agents_root.glob("*") if p.is_dir()] if agents_root.exists() else []
    assert len(agent_dirs) == 1, f"expected exactly one agent state dir, got {agent_dirs}"
    return agent_dirs[0]


def _marker_path(env: dict[str, str]) -> Path:
    return _agent_state_dir(env) / "active"


def _common_transcript_path(env: dict[str, str]) -> Path:
    return _agent_state_dir(env) / "events" / "pi-coding" / "common_transcript" / "events.jsonl"


def _wait_for_text_in_pane(session_name: str, expected: str, env: dict[str, str], timeout: float) -> None:
    """Poll ``tmux capture-pane`` (full scrollback) until ``expected`` appears."""
    last_capture: list[str] = [""]

    def _capture_if_match() -> str | None:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-9999"],
            capture_output=True,
            text=True,
            env=env,
        )
        last_capture[0] = result.stdout
        return result.stdout if expected in result.stdout else None

    capture, _, _ = poll_for_value(_capture_if_match, timeout=timeout, poll_interval=2.0)
    if capture is None:
        raise AssertionError(
            f"Did not see {expected!r} in tmux pane {session_name!r} within {timeout}s.\n"
            f"Last capture (tail):\n{last_capture[0][-2000:]}"
        )


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(_CREATE_TIMEOUT_SECONDS + 3 * _RESPONSE_TIMEOUT_SECONDS + _DESTROY_TIMEOUT_SECONDS + 120)
def test_pi_agent_full_lifecycle(tmp_path: Path, subprocess_env: dict[str, str]) -> None:
    env = subprocess_env
    source = _make_git_source(tmp_path)
    agent_name = f"pi-e2e-{get_short_random_string()}"
    secret = uuid.uuid4().hex
    session_name = f"{env['MNGR_PREFIX']}{agent_name}"

    # 1. Create interactively (no -p), so we can drive it with `mngr message`.
    #    Returning at all means the readiness sentinel fired.
    create_result = _run(
        [
            "uv",
            "run",
            "mngr",
            "create",
            agent_name,
            "pi-coding",
            "--no-connect",
            "--no-ensure-clean",
            "--yes",
            "--source",
            str(source),
            "--pass-env",
            "ANTHROPIC_API_KEY",
            "--",
            "--provider",
            "anthropic",
            "--model",
            _MODEL,
        ],
        env=env,
        timeout=float(_CREATE_TIMEOUT_SECONDS),
    )
    assert "Done." in create_result.stdout, f"stdout:\n{create_result.stdout}\nstderr:\n{create_result.stderr}"

    try:
        # 2. A freshly created agent is idle: the RUNNING/WAITING marker is absent.
        assert not _marker_path(env).exists(), "expected the agent to be WAITING (no active marker) right after create"

        # 3. Plant the secret. The marker must turn present (RUNNING) during the turn.
        _run(
            ["uv", "run", "mngr", "message", agent_name, "--message", _SEED_PROMPT_TEMPLATE.format(secret=secret)],
            env=env,
            timeout=120.0,
        )
        wait_for(
            _marker_path(env).exists,
            timeout=30.0,
            poll_interval=0.2,
            error_message="active marker never appeared (RUNNING) after sending a message",
        )

        # 4. Wait for the reply, then the marker must clear (WAITING).
        _wait_for_text_in_pane(session_name, "ACK", env=env, timeout=float(_RESPONSE_TIMEOUT_SECONDS))
        wait_for(
            lambda: not _marker_path(env).exists(),
            timeout=60.0,
            poll_interval=0.5,
            error_message="active marker never cleared (WAITING) after the turn finished",
        )

        # 5. The transcript captured the turn (user prompt, assistant reply, the bash tool result).
        records = [json.loads(line) for line in _common_transcript_path(env).read_text().splitlines() if line.strip()]
        record_types = [r["type"] for r in records]
        assert "user_message" in record_types, record_types
        assert "assistant_message" in record_types, record_types
        assert "tool_result" in record_types, record_types
        # And `mngr transcript` renders it (the secret was in the user prompt).
        transcript_out = _run(["uv", "run", "mngr", "transcript", agent_name], env=env, timeout=60.0).stdout
        assert secret in transcript_out, f"secret not found in mngr transcript output:\n{transcript_out[-2000:]}"

        # 6. Stop kills the tmux session but keeps state.
        _run(["uv", "run", "mngr", "stop", agent_name], env=env, timeout=120.0)
        assert (
            subprocess.run(["tmux", "has-session", "-t", session_name], env=env, capture_output=True).returncode != 0
        ), "tmux session should be gone after stop"

        # 7. Start resumes the prior session.
        _run(["uv", "run", "mngr", "start", agent_name], env=env, timeout=float(_CREATE_TIMEOUT_SECONDS))

        # 8. The resumed agent must recall the secret -> conversation context survived stop/start.
        _run(["uv", "run", "mngr", "message", agent_name, "--message", _RECALL_PROMPT], env=env, timeout=120.0)
        _wait_for_text_in_pane(session_name, secret, env=env, timeout=float(_RESPONSE_TIMEOUT_SECONDS))
    finally:
        _run(
            ["uv", "run", "mngr", "destroy", agent_name, "--force"],
            env=env,
            timeout=float(_DESTROY_TIMEOUT_SECONDS),
            check=False,
        )

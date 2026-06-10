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
* **Workspace trust** -- the source has a ``.agents/skills`` dir, so the worktree
  would hit pi's "Trust project folder?" dialog; the plugin pre-seeds trust, so the
  run (and its first message) succeeds rather than stalling at the dialog.
"""

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

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
    """A fresh git repo for ``mngr create --source`` (mngr requires a git source).

    Includes a ``.agents/skills/`` dir so the agent's worktree has pi "project
    trust inputs" and would otherwise stop at pi 0.79+'s "Trust project folder?"
    dialog. pi triggers that dialog (``hasProjectTrustInputs``) on a ``.pi`` config
    dir in the cwd, or a ``.agents/skills`` dir in the cwd or any ancestor -- NOT on
    CLAUDE.md/AGENTS.md, which are only loaded once trusted. ``mngr create --yes``
    (this test) makes the plugin pre-seed trust; if that handling regressed, the
    dialog would swallow the first message and the RUNNING-marker assertion below
    would fail.
    """
    work_dir = tmp_path / "pi-source"
    init_git_repo(work_dir, initial_commit=True)
    (work_dir / ".gitignore").write_text(".pi/\n")
    skills_dir = work_dir / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "example.md").write_text("# Example skill (gives the worktree pi trust inputs)\n")
    run_git_command(work_dir, "add", ".gitignore", ".agents")
    run_git_command(work_dir, "commit", "-m", "add gitignore and .agents/skills")
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

        # 4. The turn completing is signalled by the marker clearing (WAITING) --
        #    a real turn-completion gate, unlike matching a pane string (the
        #    prompt is echoed into the pane, so any reply token we asked for would
        #    match immediately). The assistant's actual reply is asserted via the
        #    transcript below and the secret recall in step 8.
        wait_for(
            lambda: not _marker_path(env).exists(),
            timeout=float(_RESPONSE_TIMEOUT_SECONDS),
            poll_interval=0.5,
            error_message="active marker never cleared (WAITING) after the turn finished",
        )

        # 5. The transcript captured the turn with the right record SHAPES -- the same
        #    contract the synthetic node-harness asserts, here validated against REAL pi
        #    events (so the harness's assumed event shapes can't silently drift).
        records = [json.loads(line) for line in _common_transcript_path(env).read_text().splitlines() if line.strip()]
        by_type: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            by_type.setdefault(str(record["type"]), []).append(record)
        for record in records:
            assert {"timestamp", "type", "event_id", "source"} <= set(record), record
            assert record["source"] == "pi-coding/common_transcript", record
        assert len({str(r["event_id"]) for r in records}) == len(records), "event_ids must be unique"
        assert "user_message" in by_type and "assistant_message" in by_type and "tool_result" in by_type, list(by_type)
        # The user prompt carried the secret.
        assert any(r["role"] == "user" and secret in str(r["content"]) for r in by_type["user_message"])
        # An assistant message has a bash tool call, a populated model, and a usage block.
        bash_calls = [c for r in by_type["assistant_message"] for c in r["tool_calls"] if c["tool_name"] == "bash"]
        assert bash_calls, by_type["assistant_message"]
        assert all(c.get("tool_call_id") and "input_preview" in c for c in bash_calls)
        assert all(r["model"] for r in by_type["assistant_message"])
        usage_keys = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"}
        assert all(usage_keys <= set(r["usage"]) for r in by_type["assistant_message"])
        # The bash tool_result holds the echo output, is not an error, and pairs to a real call id.
        call_ids = {c["tool_call_id"] for c in bash_calls}
        assert any(
            t["tool_name"] == "bash" and "SEEDED" in str(t["output"]) and t["is_error"] is False
            for t in by_type["tool_result"]
        ), by_type["tool_result"]
        assert any(t["tool_call_id"] in call_ids for t in by_type["tool_result"])
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

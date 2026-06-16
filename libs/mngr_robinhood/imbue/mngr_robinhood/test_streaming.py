"""Release tests for `mngr robinhood`'s approximate response streaming.

Verifies end-to-end that the tmux-based response stream (`stream_buffer`,
produced by the claude agent's `stream_snapshot.py` watcher) is surfaced through
robinhood's two streaming surfaces:

* `--output-format stream-json --include-partial-messages` emits claude-native
  `stream_event` / `text_delta` events as the response is produced, before the
  authoritative `assistant` message.
* `--stream-plain-text` streams the response text to stdout incrementally and
  does not duplicate it.

These are release tests; release tests do not run in CI. To run manually::

    PYTEST_MAX_DURATION_SECONDS=900 ANTHROPIC_API_KEY=sk-ant-... \\
        uv run pytest --no-cov --cov-fail-under=0 -n 0 -m release \\
        libs/mngr_robinhood/imbue/mngr_robinhood/test_streaming.py
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr.utils.testing import setup_claude_trust_config_for_subprocess

# A long prompt so the response streams over several poll intervals, giving the
# watcher time to produce multiple buffer snapshots.
_LONG_PROMPT = (
    "Write an incredibly long and detailed story, at least 20 sentences, about a "
    "lighthouse keeper named Eilert and the sea he watched over for forty years."
)

_RUN_TIMEOUT_SECONDS = 600.0


def _have_claude_credentials() -> bool:
    """Skip-guard: a real ``claude`` binary and ANTHROPIC_API_KEY are required."""
    return shutil.which("claude") is not None and bool(os.environ.get("ANTHROPIC_API_KEY"))


pytestmark = [
    pytest.mark.release,
    # robinhood drives a real claude agent in tmux, so these tests exercise the
    # tmux resource. The PATH wrapper installed by the resource guard blocks
    # (exit 127) any tmux invocation from a test lacking this mark, which would
    # make the robinhood subprocess fail (exit 2). Declaring it lets the real
    # tmux through; the subprocess inherits the resulting allow flag via its env.
    pytest.mark.tmux,
    # Driving a real agent over a long prompt far exceeds the project's default
    # 30s per-test timeout. Sibling live SDK tests use timeout(600); set this just
    # above the 600s subprocess run timeout so a hung agent surfaces robinhood's
    # own captured stdout/stderr (via _run_robinhood) rather than a bare
    # pytest-timeout traceback.
    pytest.mark.timeout(660),
    pytest.mark.skipif(
        not _have_claude_credentials(),
        reason="Release test requires ANTHROPIC_API_KEY in the environment and `claude` on PATH.",
    ),
]


@pytest.fixture
def streaming_work_dir(tmp_path: Path) -> Path:
    """A trusted git work dir robinhood can run in (settings.local.json gitignored)."""
    work_dir = tmp_path / "robinhood-stream-work"
    init_git_repo(work_dir, initial_commit=True)
    (work_dir / ".gitignore").write_text(".claude/settings.local.json\n")
    run_git_command(work_dir, "add", ".gitignore")
    run_git_command(work_dir, "commit", "-m", "add gitignore")
    return work_dir


@pytest.fixture
def streaming_env(streaming_work_dir: Path, tmp_path: Path) -> dict[str, str]:
    """Trust the work dir and disable remote providers for subprocess robinhood runs."""
    env = setup_claude_trust_config_for_subprocess(trusted_paths=[streaming_work_dir.resolve()])
    project_config_dir = tmp_path / ".mngr-stream-test"
    project_config_dir.mkdir(parents=True, exist_ok=True)
    (project_config_dir / "settings.local.toml").write_text(
        "is_allowed_in_pytest = true\n\n[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n"
    )
    env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)
    return env


def _run_robinhood(args: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        ["uv", "run", "mngr", "robinhood", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"robinhood failed (exit {result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def test_stream_json_emits_partials_before_final_assistant(
    streaming_work_dir: Path,
    streaming_env: dict[str, str],
) -> None:
    stdout = _run_robinhood(
        ["--output-format", "stream-json", "--include-partial-messages", _LONG_PROMPT],
        cwd=streaming_work_dir,
        env=streaming_env,
    )
    # json.loads returns Any, so events are loosely typed; this is test-only.
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]

    types_in_order = [event["type"] for event in events]
    assert "stream_event" in types_in_order, f"expected at least one partial stream_event; got {types_in_order}"
    assert "assistant" in types_in_order, f"expected a final assistant message; got {types_in_order}"

    # Partials must precede the first authoritative assistant message.
    first_partial = types_in_order.index("stream_event")
    first_assistant = types_in_order.index("assistant")
    assert first_partial < first_assistant, "partial text_delta events must precede the final assistant message"

    # Every partial carries a text_delta.
    partials = [event for event in events if event["type"] == "stream_event"]
    for partial in partials:
        assert partial["event"]["delta"]["type"] == "text_delta"


def test_stream_plain_text_streams_once_without_duplication(
    streaming_work_dir: Path,
    streaming_env: dict[str, str],
) -> None:
    stdout = _run_robinhood(
        ["--stream-plain-text", _LONG_PROMPT],
        cwd=streaming_work_dir,
        env=streaming_env,
    )
    assert stdout.strip() != "", "expected streamed plain-text output"
    # The keeper's name is mentioned once in the prompt; the streamed response
    # should not duplicate the whole body (the suppression-of-final-dump path).
    assert stdout.count("Eilert") >= 1
    # A crude duplication guard: the output should not contain a long repeated
    # half. If the body were emitted twice, the first and second halves would be
    # identical; assert they are not.
    half = len(stdout) // 2
    assert stdout[:half] != stdout[half : half * 2], "streamed output appears to be duplicated"

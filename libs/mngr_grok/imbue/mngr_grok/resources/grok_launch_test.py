import os
import shlex
import shutil
import subprocess
import uuid
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

import pytest
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.hosts.tmux import PANE_SESSION_NAME_SHELL_EXPRESSION
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import create_background_tasks_state_dir
from imbue.mngr.utils.testing import write_executable_script
from imbue.mngr_grok.grok_config import FORK_FROM_RELATIVE_PATH
from imbue.mngr_grok.grok_config import GROK_PLUGIN_STATE_RELATIVE_PATH
from imbue.mngr_grok.grok_config import LAUNCH_ENV_RELATIVE_PATH
from imbue.mngr_grok.grok_config import LAUNCH_SCRIPT_NAME
from imbue.mngr_grok.grok_config import ROLE_PROMPT_RELATIVE_PATH
from imbue.mngr_grok.grok_config import SESSION_ID_RELATIVE_PATH
from imbue.mngr_grok.grok_config import build_launch_command
from imbue.mngr_grok.grok_config import build_launch_env
from imbue.mngr_grok.grok_config import build_pinned_grok_environment
from imbue.mngr_grok.grok_config import compute_session_bucket_name
from imbue.mngr_grok.grok_config import get_grok_home
from imbue.mngr_grok.resources.grok_transcript_streamer import convert_lock_path
from imbue.mngr_grok.resources.testing import CONFIG_FIXTURES_DIR
from imbue.mngr_grok.resources.testing import read_complete_jsonl_records
from imbue.mngr_grok.testing import make_grok_session_dir

_LAUNCH_SCRIPT: Final[Path] = Path(__file__).parent / LAUNCH_SCRIPT_NAME
_ROLE_PROMPT_FIXTURE: Final[Path] = CONFIG_FIXTURES_DIR / "role_prompt_with_frontmatter.md"
_PANE_ID: Final[str] = "%71"
_PANE_SESSION_NAME: Final[str] = "mngr-grok-launch-test-session"
_LAUNCH_TIMEOUT_SECONDS: Final[float] = 8.0

# Records each invocation's argv, cwd, environment and the state entries present at its start, then exits with the next
# configured code.
_STANDIN_GROK_SCRIPT: Final[str] = """#!/usr/bin/env python3
import json
import os
import sys

record_path = os.environ["STANDIN_GROK_RECORD"]
state_dir = os.environ["MNGR_AGENT_STATE_DIR"]
session_started_path = os.path.join(state_dir, "grok_session_started")
invocation_index = 0
if os.path.exists(record_path):
    with open(record_path) as record_file:
        invocation_index = len(record_file.readlines())
record = {
    "argv0": os.path.basename(sys.argv[0]),
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "environment": dict(os.environ),
    "state_entries_at_launch": sorted(os.listdir(state_dir)),
    "plugin_entries_at_launch": sorted(os.listdir(os.path.join(state_dir, "plugin", "grok"))),
}
with open(record_path, "a") as record_file:
    record_file.write(json.dumps(record) + "\\n")
if os.environ.get("STANDIN_GROK_STARTS_SESSION") == "1":
    open(session_started_path, "w").close()
exit_codes = [int(code) for code in os.environ.get("STANDIN_GROK_EXIT_CODES", "0").split(",")]
sys.exit(exit_codes[invocation_index] if invocation_index < len(exit_codes) else 0)
"""

# Answers the pane's session-name query and records how it was asked.
_STANDIN_TMUX_SCRIPT: Final[str] = f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$@" > "$MNGR_AGENT_STATE_DIR/tmux_args"
printf '%s\\n' {_PANE_SESSION_NAME}
"""

_STANDIN_SUPERVISOR_SCRIPT: Final[str] = """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n%s\\n' "$1" "${GROK_AGENT-unset}" > "$MNGR_AGENT_STATE_DIR/supervisor_record.tmp"
mv "$MNGR_AGENT_STATE_DIR/supervisor_record.tmp" "$MNGR_AGENT_STATE_DIR/supervisor_record"
"""

_STALE_STATE_DIR_MARKERS: Final[tuple[str, ...]] = ("active", "permissions_waiting", "grok_session_started")
_STALE_PLUGIN_STATE_PATHS: Final[tuple[str, ...]] = (
    "background_tasks.json",
    "turns/0199aaaa-0000-7000-8000-000000000001",
    "subagents/0199aaaa-0000-7000-8000-000000000002",
    "pending_wake/0199aaaa-0000-7000-8000-000000000001",
)


class _LaunchFixture(FrozenModel):
    """Paths of one agent laid out for a launch-script run."""

    state_dir: Path = Field(description="The agent state dir ($MNGR_AGENT_STATE_DIR)")
    grok_home: Path = Field(description="The agent's GROK_HOME")
    work_dir: Path = Field(description="The agent's work dir")
    bin_dir: Path = Field(description="Directory holding the stand-in grok and tmux, first on PATH")
    record_path: Path = Field(description="JSONL file the stand-in grok appends one record to per invocation")
    plugin_dir: Path = Field(description="The agent's plugin/grok state dir")
    session_id: str = Field(description="The session id provision wrote to plugin/grok/session_id")


class _LaunchResult(FrozenModel):
    """Outcome of one run of the stored launch command."""

    returncode: int = Field(description="Exit status of the launch")
    stderr: str = Field(description="Standard error of the launch")
    invocations: tuple[dict[str, Any], ...] = Field(description="One record per stand-in grok invocation")


def _prepare_launch(
    tmp_path: Path,
    *,
    is_auth_shared: bool,
    model: str | None,
    reasoning_effort: str | None,
) -> _LaunchFixture:
    state_dir = create_background_tasks_state_dir(tmp_path / "state")
    grok_home = get_grok_home(state_dir)
    work_dir = tmp_path / "work dir"
    work_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_executable_script(bin_dir / "grok", _STANDIN_GROK_SCRIPT)
    write_executable_script(bin_dir / "tmux", _STANDIN_TMUX_SCRIPT)
    write_executable_script(state_dir / "commands" / "grok_background_tasks.sh", _STANDIN_SUPERVISOR_SCRIPT)
    shutil.copy(_LAUNCH_SCRIPT, state_dir / "commands" / LAUNCH_SCRIPT_NAME)
    session_id = str(uuid.uuid4())
    (state_dir / SESSION_ID_RELATIVE_PATH).parent.mkdir(parents=True, exist_ok=True)
    (state_dir / SESSION_ID_RELATIVE_PATH).write_text(session_id + "\n")
    (state_dir / LAUNCH_ENV_RELATIVE_PATH).write_text(
        build_launch_env(
            grok_home=grok_home,
            work_dir=work_dir,
            shared_user_auth_path=tmp_path / "user grok" / "auth.json" if is_auth_shared else None,
            model=model,
            reasoning_effort=reasoning_effort,
            is_privacy_pinned=True,
            is_isolated_from_claude_config=True,
        )
    )
    return _LaunchFixture(
        state_dir=state_dir,
        grok_home=grok_home,
        work_dir=work_dir,
        bin_dir=bin_dir,
        record_path=tmp_path / "grok_invocations.jsonl",
        plugin_dir=state_dir / GROK_PLUGIN_STATE_RELATIVE_PATH,
        session_id=session_id,
    )


def _prepare_default_launch(tmp_path: Path) -> _LaunchFixture:
    return _prepare_launch(tmp_path, is_auth_shared=True, model=None, reasoning_effort=None)


def _create_session_dir(launch: _LaunchFixture, session_id: str, is_summary_written: bool) -> Path:
    return make_grok_session_dir(
        launch.grok_home / "sessions",
        compute_session_bucket_name(str(launch.work_dir.resolve())),
        session_id,
        is_resumable=is_summary_written,
    )


def _run_launch(
    launch: _LaunchFixture,
    *,
    command: str,
    agent_args: Sequence[str],
    extra_environment: Mapping[str, str],
) -> _LaunchResult:
    stored_command = build_launch_command(
        command=CommandString(command), cli_args=(), agent_args=agent_args, command_override=None
    )
    environment = {
        "PATH": f"{launch.bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": os.environ["HOME"],
        "MNGR_AGENT_STATE_DIR": str(launch.state_dir),
        "TMUX_PANE": _PANE_ID,
        "STANDIN_GROK_RECORD": str(launch.record_path),
        **extra_environment,
    }
    completed = subprocess.run(
        ["bash", "-c", stored_command],
        env=environment,
        cwd=launch.state_dir,
        capture_output=True,
        text=True,
        timeout=_LAUNCH_TIMEOUT_SECONDS,
    )
    return _LaunchResult(
        returncode=completed.returncode,
        stderr=completed.stderr,
        invocations=tuple(read_complete_jsonl_records(launch.record_path)),
    )


def _run_default_launch(launch: _LaunchFixture) -> _LaunchResult:
    return _run_launch(launch, command="grok", agent_args=(), extra_environment={})


def test_first_launch_without_a_sessions_dir_starts_the_provisioned_session_id(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)

    result = _run_default_launch(launch)

    assert result.returncode == 0, result.stderr
    assert len(result.invocations) == 1
    assert result.invocations[0]["argv0"] == "grok"
    assert result.invocations[0]["argv"] == ["--session-id", launch.session_id]
    assert result.invocations[0]["cwd"] == str(launch.work_dir.resolve())


def test_launch_with_an_empty_sessions_dir_starts_the_provisioned_session_id(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)
    (launch.grok_home / "sessions").mkdir(parents=True)

    result = _run_default_launch(launch)

    assert result.returncode == 0, result.stderr
    assert [invocation["argv"] for invocation in result.invocations] == [["--session-id", launch.session_id]]


def test_launch_resumes_a_session_that_has_its_summary(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)
    _create_session_dir(launch, launch.session_id, is_summary_written=True)

    result = _run_default_launch(launch)

    assert result.returncode == 0, result.stderr
    assert [invocation["argv"] for invocation in result.invocations] == [["--resume", launch.session_id]]
    launch_events = (launch.state_dir / "events" / "logs" / "grok_launch" / "events.jsonl").read_text()
    assert f"Launching grok (resume) with session {launch.session_id}" in launch_events


def test_launch_does_not_resume_a_session_dir_without_a_summary(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)
    _create_session_dir(launch, launch.session_id, is_summary_written=False)

    result = _run_default_launch(launch)

    assert [invocation["argv"] for invocation in result.invocations] == [["--session-id", launch.session_id]]


def test_launch_resumes_a_session_whose_summary_is_in_another_bucket_than_copies_without_one(
    tmp_path: Path,
) -> None:
    launch = _prepare_default_launch(tmp_path)
    for bucket in ("%2Fa", "%2Fb", "%2Fc", "%2Fd"):
        make_grok_session_dir(launch.grok_home / "sessions", bucket, launch.session_id, is_resumable=False)
    _create_session_dir(launch, launch.session_id, is_summary_written=True)

    result = _run_default_launch(launch)

    assert [invocation["argv"] for invocation in result.invocations] == [["--resume", launch.session_id]]


def test_launch_forks_a_pending_clone_source_into_the_provisioned_session_id(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)
    fork_source_id = str(uuid.uuid4())
    _create_session_dir(launch, fork_source_id, is_summary_written=True)
    (launch.state_dir / FORK_FROM_RELATIVE_PATH).write_text(fork_source_id + "\n")

    result = _run_default_launch(launch)

    assert result.returncode == 0, result.stderr
    assert [invocation["argv"] for invocation in result.invocations] == [
        ["--resume", fork_source_id, "--fork-session", "--session-id", launch.session_id]
    ]
    assert (launch.state_dir / FORK_FROM_RELATIVE_PATH).exists()


def test_launch_resumes_a_fork_that_already_materialised_and_drops_the_pending_fork(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)
    fork_source_id = str(uuid.uuid4())
    _create_session_dir(launch, fork_source_id, is_summary_written=True)
    _create_session_dir(launch, launch.session_id, is_summary_written=True)
    (launch.state_dir / FORK_FROM_RELATIVE_PATH).write_text(fork_source_id + "\n")

    result = _run_default_launch(launch)

    assert [invocation["argv"] for invocation in result.invocations] == [["--resume", launch.session_id]]
    assert not (launch.state_dir / FORK_FROM_RELATIVE_PATH).exists()


def test_launch_fails_when_the_pending_fork_source_is_missing(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)
    missing_source_id = str(uuid.uuid4())
    (launch.state_dir / FORK_FROM_RELATIVE_PATH).write_text(missing_source_id + "\n")

    result = _run_default_launch(launch)

    assert result.returncode == 1
    assert result.invocations == ()
    assert f"the session to fork, {missing_source_id}, is not in" in result.stderr


def test_launch_without_a_provisioned_session_id_starts_a_fresh_one(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)
    (launch.state_dir / SESSION_ID_RELATIVE_PATH).unlink()

    result = _run_default_launch(launch)

    assert len(result.invocations) == 1
    argv = result.invocations[0]["argv"]
    assert argv[0] == "--session-id"
    assert uuid.UUID(argv[1]).version == 4


def _lay_out_new_launch(launch: _LaunchFixture) -> list[str]:
    return ["--session-id", launch.session_id]


def _lay_out_resume_launch(launch: _LaunchFixture) -> list[str]:
    _create_session_dir(launch, launch.session_id, is_summary_written=True)
    return ["--resume", launch.session_id]


def _lay_out_fork_launch(launch: _LaunchFixture) -> list[str]:
    fork_source_id = str(uuid.uuid4())
    _create_session_dir(launch, fork_source_id, is_summary_written=True)
    (launch.state_dir / FORK_FROM_RELATIVE_PATH).write_text(fork_source_id + "\n")
    return ["--resume", fork_source_id, "--fork-session", "--session-id", launch.session_id]


@pytest.mark.parametrize(
    "lay_out_first_attempt",
    [_lay_out_new_launch, _lay_out_resume_launch, _lay_out_fork_launch],
    ids=["new", "resume", "fork"],
)
def test_launch_falls_back_to_a_fresh_session_when_grok_fails_before_session_start(
    tmp_path: Path, lay_out_first_attempt: Callable[[_LaunchFixture], list[str]]
) -> None:
    launch = _prepare_launch(tmp_path, is_auth_shared=True, model="grok-4.7", reasoning_effort=None)
    first_attempt_session_args = lay_out_first_attempt(launch)

    result = _run_launch(launch, command="grok", agent_args=(), extra_environment={"STANDIN_GROK_EXIT_CODES": "1,0"})

    assert result.returncode == 0, result.stderr
    assert len(result.invocations) == 2
    assert result.invocations[0]["argv"] == ["-m", "grok-4.7", *first_attempt_session_args]
    retry_argv = result.invocations[1]["argv"]
    assert retry_argv[:-1] == ["-m", "grok-4.7", "--session-id"]
    assert retry_argv[-1] != launch.session_id
    assert uuid.UUID(retry_argv[-1]).version == 4
    assert "starting a fresh session" in result.stderr


def test_launch_reports_the_fallback_failure_when_both_attempts_fail(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)

    result = _run_launch(launch, command="grok", agent_args=(), extra_environment={"STANDIN_GROK_EXIT_CODES": "1,4"})

    assert len(result.invocations) == 2
    assert result.returncode == 4


def test_launch_does_not_fall_back_when_grok_fails_after_session_start(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)

    result = _run_launch(
        launch,
        command="grok",
        agent_args=(),
        extra_environment={"STANDIN_GROK_EXIT_CODES": "3", "STANDIN_GROK_STARTS_SESSION": "1"},
    )

    assert result.returncode == 3
    assert len(result.invocations) == 1


def test_launch_passes_a_frontmatter_role_prompt_as_one_rules_argument_without_trailing_newlines(
    tmp_path: Path,
) -> None:
    launch = _prepare_default_launch(tmp_path)
    role_text = _ROLE_PROMPT_FIXTURE.read_text()
    assert role_text.startswith("---\n")
    assert "\n\n\n" in role_text
    (launch.state_dir / ROLE_PROMPT_RELATIVE_PATH).write_text(role_text)

    result = _run_launch(launch, command="grok", agent_args=("--no-plan",), extra_environment={})

    argv = result.invocations[0]["argv"]
    assert argv == ["--no-plan", "--rules=" + role_text.rstrip("\n"), "--session-id", launch.session_id]


def test_launch_omits_rules_for_an_empty_role_file(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)
    (launch.state_dir / ROLE_PROMPT_RELATIVE_PATH).write_text("")

    result = _run_default_launch(launch)

    assert result.invocations[0]["argv"] == ["--session-id", launch.session_id]


def test_launch_passes_user_args_with_spaces_and_quotes_unchanged(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)
    agent_args = ("--allow", "Bash(ls *)", 'it\'s a "quoted" $HOME `word`')

    result = _run_launch(launch, command="grok", agent_args=agent_args, extra_environment={})

    assert result.invocations[0]["argv"] == [*agent_args, "--session-id", launch.session_id]
    stored_command = build_launch_command(
        command=CommandString("grok"), cli_args=(), agent_args=agent_args, command_override=None
    )
    assert "\n" not in stored_command
    assert shlex.split(stored_command) == [
        "bash",
        f"$MNGR_AGENT_STATE_DIR/commands/{LAUNCH_SCRIPT_NAME}",
        "grok",
        *agent_args,
    ]


def test_launch_runs_a_launcher_prefixed_command(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)

    result = _run_launch(launch, command="env FOO=launcher-value-8812 grok", agent_args=(), extra_environment={})

    invocation = result.invocations[0]
    assert invocation["argv0"] == "grok"
    assert invocation["environment"]["FOO"] == "launcher-value-8812"
    assert invocation["argv"] == ["--session-id", launch.session_id]


def test_launch_passes_the_configured_model_and_reasoning_effort(tmp_path: Path) -> None:
    launch = _prepare_launch(tmp_path, is_auth_shared=True, model="grok-4.7", reasoning_effort="high")

    result = _run_default_launch(launch)

    assert result.invocations[0]["argv"] == [
        "-m",
        "grok-4.7",
        "--reasoning-effort",
        "high",
        "--session-id",
        launch.session_id,
    ]


def test_launch_removes_inherited_overrides_and_sets_the_pinned_environment(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)
    inherited_overrides = {
        "GROK_AGENT": "1",
        "CLAUDECODE": "1",
        "GROK_CONFIG": "/inherited/config.toml",
        "GROK_CONFIG_PATH": "/inherited",
        "GROK_FOLDER_TRUST": "0",
        "GROK_HOME": "/inherited/home",
    }

    result = _run_launch(launch, command="grok", agent_args=(), extra_environment=inherited_overrides)

    environment = result.invocations[0]["environment"]
    for removed_name in ("GROK_AGENT", "CLAUDECODE", "GROK_CONFIG", "GROK_CONFIG_PATH", "GROK_FOLDER_TRUST"):
        assert removed_name not in environment
    pinned_environment = build_pinned_grok_environment(
        grok_home=launch.grok_home, is_privacy_pinned=True, is_isolated_from_claude_config=True
    )
    assert {name: environment.get(name) for name in pinned_environment} == pinned_environment
    assert environment["GROK_AUTH_PATH"] == str(tmp_path / "user grok" / "auth.json")
    assert environment["MNGR_AGENT_STATE_DIR"] == str(launch.state_dir)


def test_launch_keeps_an_auth_path_bound_in_the_agent_environment(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)

    result = _run_launch(
        launch, command="grok", agent_args=(), extra_environment={"GROK_AUTH_PATH": "/bound/account/auth.json"}
    )

    assert result.invocations[0]["environment"]["GROK_AUTH_PATH"] == "/bound/account/auth.json"


def test_launch_without_shared_auth_removes_an_inherited_auth_path(tmp_path: Path) -> None:
    launch = _prepare_launch(tmp_path, is_auth_shared=False, model=None, reasoning_effort=None)

    result = _run_launch(
        launch, command="grok", agent_args=(), extra_environment={"GROK_AUTH_PATH": "/bound/account/auth.json"}
    )

    assert "GROK_AUTH_PATH" not in result.invocations[0]["environment"]


def test_launch_resets_stale_state_before_grok_starts(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)
    for marker in _STALE_STATE_DIR_MARKERS:
        (launch.state_dir / marker).write_text("")
    for relative_path in _STALE_PLUGIN_STATE_PATHS:
        (launch.plugin_dir / relative_path).parent.mkdir(parents=True, exist_ok=True)
        (launch.plugin_dir / relative_path).write_text("stale")
    convert_lock_path(launch.state_dir).mkdir()

    result = _run_default_launch(launch)

    state_entries_at_launch = set(result.invocations[0]["state_entries_at_launch"])
    plugin_entries_at_launch = set(result.invocations[0]["plugin_entries_at_launch"])
    assert not state_entries_at_launch & {*_STALE_STATE_DIR_MARKERS, convert_lock_path(launch.state_dir).name}
    assert not plugin_entries_at_launch & {"background_tasks.json", "turns", "subagents", "pending_wake"}
    assert "grok_process_started" in state_entries_at_launch


def test_launch_starts_the_supervisor_with_the_pane_session_name_and_without_grok_agent(tmp_path: Path) -> None:
    launch = _prepare_default_launch(tmp_path)
    supervisor_record = launch.state_dir / "supervisor_record"

    _run_launch(launch, command="grok", agent_args=(), extra_environment={"GROK_AGENT": "1"})

    assert poll_until(supervisor_record.exists, timeout=5.0)
    assert supervisor_record.read_text().splitlines() == [_PANE_SESSION_NAME, "unset"]
    assert (launch.state_dir / "tmux_args").read_text().splitlines() == [
        "display-message",
        "-p",
        "-t",
        _PANE_ID,
        "#{session_name}",
    ]


def test_launch_script_reads_the_session_name_with_the_core_pane_expression() -> None:
    assert PANE_SESSION_NAME_SHELL_EXPRESSION in _LAUNCH_SCRIPT.read_text()

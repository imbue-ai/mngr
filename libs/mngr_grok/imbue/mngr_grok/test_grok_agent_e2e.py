"""Release tests: real mngr-managed grok agents, driven through the ``mngr`` CLI.

The agents run the pinned grok (``resources/test_fixtures/grok_version.txt``) on the stub model server, configured as
the agent type's custom model, with every proxy and xAI origin in their environment pointing at the stub;
trigger words in the prompts script the replies. With ``MNGR_GROK_REQUIRE_PINNED_BINARY=1`` (``release-tests.yml``
sets it) they never skip: ``mngr create --yes`` installs a missing grok at the pinned version, and a grok of another
version fails them. Elsewhere they skip unless the pinned grok is first on PATH.
"""

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pytest
import tomlkit

from imbue.imbue_common.primitives import PositiveFloat
from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import AgentReleaseProfile
from imbue.mngr.agents.agent_release_testing import agent_state_dir_by_name
from imbue.mngr.agents.agent_release_testing import run_agent_release_lifecycle
from imbue.mngr.agents.agent_release_testing import run_concurrent_message_delivery
from imbue.mngr.agents.agent_release_testing import run_message_delivery_journey
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import mngr_agent_cleanup
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr_grok.grok_config import GROK_PLUGIN_STATE_RELATIVE_PATH
from imbue.mngr_grok.grok_config import SESSION_ID_RELATIVE_PATH
from imbue.mngr_grok.grok_config import compute_session_bucket_name
from imbue.mngr_grok.grok_sessions import GROK_SESSIONS_RELATIVE_PATH
from imbue.mngr_grok.grok_sessions import ROOTS_RELATIVE_PATH
from imbue.mngr_grok.resources.grok_hook import ACTIVE_MARKER_FILENAME
from imbue.mngr_grok.resources.grok_hook import SUBMISSIONS_LOG_FILENAME
from imbue.mngr_grok.resources.grok_hook import normalized_prompt_tail
from imbue.mngr_grok.resources.testing import read_common_transcript_records
from imbue.mngr_grok.testing import FOREGROUND_SUBAGENT_TRIGGER
from imbue.mngr_grok.testing import GrokStubModelServer
from imbue.mngr_grok.testing import GrokStubScenario
from imbue.mngr_grok.testing import GrokStubServerConfig
from imbue.mngr_grok.testing import SLOW_REPLY_TRIGGER
from imbue.mngr_grok.testing import SUBAGENT_PROMPT
from imbue.mngr_grok.testing import TOOL_CALL_COMMAND
from imbue.mngr_grok.testing import TOOL_CALL_MARKER_FILENAME
from imbue.mngr_grok.testing import TOOL_CALL_MARKER_TEXT
from imbue.mngr_grok.testing import TOOL_CALL_TRIGGER
from imbue.mngr_grok.testing import build_stub_custom_model_settings_overrides
from imbue.mngr_grok.testing import find_pinned_grok_mismatch
from imbue.mngr_grok.testing import is_pinned_grok_required
from imbue.mngr_grok.testing import read_pinned_grok_version

_PROJECT_INSTRUCTIONS: Final[str] = "# Project instructions\n\nThis file makes grok ask whether to trust the folder.\n"
_CREATE_TIMEOUT_SECONDS: Final[float] = 600.0
_MESSAGE_TIMEOUT_SECONDS: Final[float] = 180.0
_LIFECYCLE_TIMEOUT_SECONDS: Final[float] = 150.0
_RESPONSE_TIMEOUT_SECONDS: Final[float] = 120.0


def _init_grok_source_repo(work_dir: Path) -> None:
    """A git repo holding an AGENTS.md, so the agent starts only if mngr recorded grok's folder trust."""
    init_git_repo(work_dir, initial_commit=True)
    (work_dir / "AGENTS.md").write_text(_PROJECT_INSTRUCTIONS)
    run_git_command(work_dir, "add", "AGENTS.md")
    run_git_command(work_dir, "commit", "-m", "Add AGENTS.md")


class _GrokReleaseProfile(AgentReleaseProfile):
    """The grok agent type on the stub model; ``setup`` starts the stub and the teardown stops it."""

    agent_type = "grok"
    common_transcript_subdir = "grok"
    forces_tool_call = True
    # Grok reports usage only on the turn's closing step, which the shared arc checks for before the turn ends.
    asserts_usage = False
    native_session_preserved_relpaths = (GROK_SESSIONS_RELATIVE_PATH.as_posix(),)
    clear_slash_command = "/new"

    def __init__(
        self,
        build_agent_environment: Callable[[str], dict[str, str]],
        stub_config: GrokStubServerConfig,
    ) -> None:
        self._build_agent_environment = build_agent_environment
        self.stub_server = GrokStubModelServer(config=stub_config)

    def unavailable_reason(self) -> str | None:
        return None if is_pinned_grok_required() else find_pinned_grok_mismatch(shutil.which("grok"))

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        env = get_subprocess_test_env(root_name="mngr-grok-release-test")
        work_dir = tmp_path / "grok-source"
        _init_grok_source_repo(work_dir)
        project_config_dir = tmp_path / ".mngr-grok-test"
        project_config_dir.mkdir(parents=True, exist_ok=True)
        pinned_version = read_pinned_grok_version()
        self.stub_server.start()
        is_settings_written = False
        try:
            settings = {
                "is_allowed_in_pytest": True,
                "providers": {"modal": {"is_enabled": False}, "docker": {"is_enabled": False}},
                "agent_types": {
                    "grok": {
                        "version": pinned_version,
                        "auto_allow_permissions": True,
                        "env": [
                            f"{name}={value}"
                            for name, value in self._build_agent_environment(self.stub_server.get_origin_url()).items()
                        ],
                        "settings_overrides": build_stub_custom_model_settings_overrides(
                            self.stub_server.get_api_base_url()
                        ),
                    }
                },
            }
            (project_config_dir / "settings.local.toml").write_text(tomlkit.dumps(settings))
            is_settings_written = True
        finally:
            if not is_settings_written:
                self.stub_server.stop()
        env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)
        return AgentReleaseContext(
            env=env, project_dir=work_dir, host_dir=Path(env["MNGR_HOST_DIR"]), teardown=self.stub_server.stop
        )

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        return ["--no-ensure-clean", "--source", str(ctx.project_dir)]

    def adopt_session_arg(self, preserved_dir: Path) -> str:
        return (preserved_dir / SESSION_ID_RELATIVE_PATH).read_text().strip()

    def prepare_adoption_project_dir(self, work_dir: Path) -> None:
        _init_grok_source_repo(work_dir)

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return run_mngr_subprocess(*args, env=dict(ctx.env), timeout=timeout)


def _read_agent_work_dir(state_dir: Path) -> Path:
    return Path(json.loads((state_dir / "data.json").read_text())["work_dir"])


def _session_bucket_dir(state_dir: Path) -> Path:
    """The grok session bucket of the agent's work dir in its grok home."""
    canonical_work_dir = os.path.realpath(_read_agent_work_dir(state_dir))
    return state_dir / GROK_SESSIONS_RELATIVE_PATH / compute_session_bucket_name(canonical_work_dir)


def _read_session_id(state_dir: Path) -> str:
    return (state_dir / SESSION_ID_RELATIVE_PATH).read_text().strip()


def _has_step_with(state_dir: Path, source: str, text: str) -> bool:
    return any(
        record.get("source") == source and text in str(record.get("message", ""))
        for record in read_common_transcript_records(state_dir)
    )


def _run_mngr_expecting_success(
    profile: _GrokReleaseProfile, ctx: AgentReleaseContext, *args: str, timeout: float
) -> subprocess.CompletedProcess[str]:
    result = profile.run_mngr(ctx, *args, timeout=timeout)
    assert result.returncode == 0, f"mngr {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
    return result


def _send(profile: _GrokReleaseProfile, ctx: AgentReleaseContext, agent_name: str, message: str) -> None:
    _run_mngr_expecting_success(
        profile, ctx, "message", agent_name, "--message", message, timeout=_MESSAGE_TIMEOUT_SECONDS
    )


def _wait_for_reply_with(state_dir: Path, text: str) -> None:
    wait_for(
        lambda: _has_step_with(state_dir, "agent", text),
        timeout=_RESPONSE_TIMEOUT_SECONDS,
        poll_interval=0.5,
        error_message=f"no agent step containing {text!r} reached the common transcript",
    )


def _wait_for_idle(state_dir: Path) -> None:
    wait_for(
        lambda: not (state_dir / ACTIVE_MARKER_FILENAME).exists(),
        timeout=_RESPONSE_TIMEOUT_SECONDS,
        poll_interval=0.2,
        error_message="the agent never reported WAITING",
    )


@contextmanager
def _running_grok_profile(
    build_agent_environment: Callable[[str], dict[str, str]],
    tmp_path: Path,
    stub_config: GrokStubServerConfig,
) -> Generator[tuple[_GrokReleaseProfile, AgentReleaseContext], None, None]:
    """A set-up profile and its context, torn down on exit; skips like the shared arcs when grok is absent.

    After a block that succeeded, it fails if the stub failed a request or grok reached past it.
    """
    profile = _GrokReleaseProfile(build_agent_environment, stub_config)
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    ctx = profile.setup(tmp_path)
    try:
        yield profile, ctx
    finally:
        if ctx.teardown is not None:
            ctx.teardown()
    profile.stub_server.check_no_egress_attempts_or_handler_errors()


@contextmanager
def _created_grok_agent(
    profile: _GrokReleaseProfile, ctx: AgentReleaseContext, agent_name: str, create_args: Sequence[str]
) -> Generator[Path, None, None]:
    """Create a grok agent with `create_args`, yield its state dir, and destroy it on exit."""
    with mngr_agent_cleanup(agent_name, env=dict(ctx.env)):
        _run_mngr_expecting_success(
            profile,
            ctx,
            "create",
            agent_name,
            profile.agent_type,
            "--no-connect",
            "--yes",
            *create_args,
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        yield agent_state_dir_by_name(ctx.host_dir, agent_name)


def _new_agent_name(label: str) -> str:
    return f"grok-{label}-{get_short_random_string()}"


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_grok_agent_full_lifecycle(
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]], tmp_path: Path
) -> None:
    """The shared create, message, stop/start resume, destroy and adopt arc, with a forced tool call."""
    profile = _GrokReleaseProfile(build_hermetic_grok_agent_environment, GrokStubServerConfig())

    run_agent_release_lifecycle(profile, tmp_path)

    profile.stub_server.check_no_egress_attempts_or_handler_errors()


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_grok_message_delivery_journey(
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]], tmp_path: Path
) -> None:
    """Idle, busy, rapid and long sends each confirm and land exactly once; a slash command sends without error."""
    profile = _GrokReleaseProfile(build_hermetic_grok_agent_environment, GrokStubServerConfig())

    run_message_delivery_journey(profile, tmp_path)

    profile.stub_server.check_no_egress_attempts_or_handler_errors()


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_grok_concurrent_message_delivery(
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]], tmp_path: Path
) -> None:
    """Two grok agents on one tmux server, messaged at once, each get only their own message."""
    profile = _GrokReleaseProfile(build_hermetic_grok_agent_environment, GrokStubServerConfig())

    run_concurrent_message_delivery(profile, tmp_path)

    profile.stub_server.check_no_egress_attempts_or_handler_errors()


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_grok_send_while_busy_is_confirmed_by_its_queued_row_while_the_turn_runs(
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]], tmp_path: Path
) -> None:
    run_id = get_short_random_string()
    busy_message = f"{SLOW_REPLY_TRIGGER} keep busy {run_id}"
    queued_message = f"Queued while busy {run_id}. Reply with just OK."
    stub_config = GrokStubServerConfig(slow_reply_seconds=PositiveFloat(40.0))

    with _running_grok_profile(build_hermetic_grok_agent_environment, tmp_path, stub_config) as (profile, ctx):
        agent_name = _new_agent_name("busy")
        with _created_grok_agent(profile, ctx, agent_name, profile.create_extra_args(ctx)) as state_dir:
            _send(profile, ctx, agent_name, busy_message)
            wait_for(
                lambda: (state_dir / ACTIVE_MARKER_FILENAME).exists(),
                timeout=_RESPONSE_TIMEOUT_SECONDS,
                error_message="the slow turn never started",
            )
            queued_send = run_mngr_subprocess(
                "message",
                agent_name,
                "--message",
                queued_message,
                env=dict(ctx.env),
                timeout=_MESSAGE_TIMEOUT_SECONDS,
                verbose=1,
            )
            is_active_after_queued_send = (state_dir / ACTIVE_MARKER_FILENAME).exists()
            assert queued_send.returncode == 0, queued_send.stdout + queued_send.stderr
            submitted_tails = [
                line.partition(" ")[2]
                for line in (state_dir / GROK_PLUGIN_STATE_RELATIVE_PATH / SUBMISSIONS_LOG_FILENAME)
                .read_text()
                .splitlines()
            ]
            wait_for(
                lambda: _has_step_with(state_dir, "user", queued_message),
                timeout=_RESPONSE_TIMEOUT_SECONDS,
                error_message="the queued message never ran",
            )

    assert "(confirmed by queued-row)" in queued_send.stdout + queued_send.stderr
    assert is_active_after_queued_send
    assert normalized_prompt_tail(queued_message) not in submitted_tails


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_grok_tool_call_runs_in_the_work_dir_and_reaches_the_common_transcript(
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]], tmp_path: Path
) -> None:
    with _running_grok_profile(build_hermetic_grok_agent_environment, tmp_path, GrokStubServerConfig()) as (
        profile,
        ctx,
    ):
        agent_name = _new_agent_name("tool")
        with _created_grok_agent(profile, ctx, agent_name, profile.create_extra_args(ctx)) as state_dir:
            _send(profile, ctx, agent_name, f"{TOOL_CALL_TRIGGER} write the marker {get_short_random_string()}")
            _wait_for_reply_with(state_dir, "tool finished")
            _wait_for_idle(state_dir)
            records = read_common_transcript_records(state_dir)
            work_dir = _read_agent_work_dir(state_dir)
            tool_marker_text = (work_dir / TOOL_CALL_MARKER_FILENAME).read_text().strip()

    tool_calls = [
        call for record in records if record.get("source") == "agent" for call in record.get("tool_calls", [])
    ]
    assert [(call["function_name"], call["arguments"]["command"]) for call in tool_calls] == [
        ("run_terminal_command", TOOL_CALL_COMMAND)
    ]
    observations = [result for record in records if record["type"] == "observation" for result in record["results"]]
    assert [result["source_call_id"] for result in observations] == [tool_calls[0]["tool_call_id"]]
    assert tool_marker_text == TOOL_CALL_MARKER_TEXT
    closing_steps = [record for record in records if record.get("extra", {}).get("finish_reason") == "end_turn"]
    assert len(closing_steps) == 1
    assert {"prompt_tokens", "completion_tokens", "cached_tokens"} <= set(closing_steps[0]["metrics"])


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_grok_subagent_is_tracked_while_it_runs_and_reaches_the_common_transcript(
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]], tmp_path: Path
) -> None:
    stub_config = GrokStubServerConfig(slow_reply_seconds=PositiveFloat(8.0))

    with _running_grok_profile(build_hermetic_grok_agent_environment, tmp_path, stub_config) as (profile, ctx):
        profile.stub_server.set_scenario(GrokStubScenario(is_child_reply_slow=True))
        agent_name = _new_agent_name("subagent")
        with _created_grok_agent(profile, ctx, agent_name, profile.create_extra_args(ctx)) as state_dir:
            subagents_dir = state_dir / GROK_PLUGIN_STATE_RELATIVE_PATH / "subagents"
            _send(profile, ctx, agent_name, f"{FOREGROUND_SUBAGENT_TRIGGER} delegate {get_short_random_string()}")
            wait_for(
                lambda: subagents_dir.is_dir() and any(subagents_dir.iterdir()),
                timeout=_RESPONSE_TIMEOUT_SECONDS,
                error_message="mngr never registered the running subagent",
            )
            _wait_for_reply_with(state_dir, "STUB_CHILD_REPLY")
            _wait_for_idle(state_dir)
            records = read_common_transcript_records(state_dir)
            remaining_subagents = list(subagents_dir.iterdir())

    assert remaining_subagents == []
    spawn_calls = [
        call
        for record in records
        if record.get("source") == "agent"
        for call in record.get("tool_calls", [])
        if call["function_name"] == "spawn_subagent"
    ]
    assert len(spawn_calls) == 1
    child_session_id = spawn_calls[0]["extra"]["subagent_session_id"]
    child_records = [record for record in records if record.get("extra", {}).get("session_id") == child_session_id]
    assert all(record["extra"]["is_sidechain"] for record in child_records)
    assert any(record.get("source") == "user" and SUBAGENT_PROMPT in record["message"] for record in child_records)
    assert any(record.get("source") == "agent" and "STUB_CHILD_REPLY" in record["message"] for record in child_records)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(600)
def test_grok_restart_after_new_resumes_the_new_session(
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]], tmp_path: Path
) -> None:
    run_id = get_short_random_string()
    first_secret = f"SECRET-first{run_id}"
    second_secret = f"SECRET-second{run_id}"

    with _running_grok_profile(build_hermetic_grok_agent_environment, tmp_path, GrokStubServerConfig()) as (
        profile,
        ctx,
    ):
        agent_name = _new_agent_name("new")
        with _created_grok_agent(profile, ctx, agent_name, profile.create_extra_args(ctx)) as state_dir:
            first_session_id = _read_session_id(state_dir)
            _send(
                profile, ctx, agent_name, f"Remember this exact secret for later: {first_secret}. Reply with just OK."
            )
            _wait_for_idle(state_dir)
            _send(profile, ctx, agent_name, "/new")
            wait_for(
                lambda: _read_session_id(state_dir) != first_session_id,
                timeout=_RESPONSE_TIMEOUT_SECONDS,
                error_message="/new never started a new grok session",
            )
            new_session_id = _read_session_id(state_dir)
            _send(
                profile, ctx, agent_name, f"Remember this exact secret for later: {second_secret}. Reply with just OK."
            )
            _wait_for_idle(state_dir)
            _run_mngr_expecting_success(profile, ctx, "stop", agent_name, timeout=_LIFECYCLE_TIMEOUT_SECONDS)
            _run_mngr_expecting_success(
                profile, ctx, "start", agent_name, "--no-connect", timeout=_CREATE_TIMEOUT_SECONDS
            )
            _send(profile, ctx, agent_name, profile.recall_prompt())
            _wait_for_reply_with(state_dir, "STUB_RECALL")
            recall_replies = [
                str(record["message"])
                for record in read_common_transcript_records(state_dir)
                if record.get("source") == "agent" and "STUB_RECALL" in str(record.get("message"))
            ]
            session_id_after_restart = _read_session_id(state_dir)
            roots = (state_dir / ROOTS_RELATIVE_PATH).read_text().split()

    assert session_id_after_restart == new_session_id
    assert roots == [first_session_id, new_session_id]
    assert recall_replies == [f"STUB_RECALL: {second_secret}"]


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_grok_clone_and_adoption_fork_the_session_into_their_own_buckets(
    build_hermetic_grok_agent_environment: Callable[[str], dict[str, str]], tmp_path: Path
) -> None:
    secret = f"SECRET-{get_short_random_string()}"

    with _running_grok_profile(build_hermetic_grok_agent_environment, tmp_path, GrokStubServerConfig()) as (
        profile,
        ctx,
    ):
        source_name = _new_agent_name("source")
        with _created_grok_agent(profile, ctx, source_name, profile.create_extra_args(ctx)) as source_state_dir:
            _send(profile, ctx, source_name, f"Remember this exact secret for later: {secret}. Reply with just OK.")
            _wait_for_idle(source_state_dir)
            source_session_id = _read_session_id(source_state_dir)
            source_bucket_dir = _session_bucket_dir(source_state_dir)

            clone_name = _new_agent_name("clone")
            with _created_grok_agent(profile, ctx, clone_name, ["--no-ensure-clean", "--from", source_name]) as (
                clone_state_dir
            ):
                _send(profile, ctx, clone_name, profile.recall_prompt())
                _wait_for_reply_with(clone_state_dir, secret)
                clone_session_id = _read_session_id(clone_state_dir)
                clone_bucket_dir = _session_bucket_dir(clone_state_dir)
                clone_session_dirs = sorted(path.name for path in clone_bucket_dir.iterdir() if path.is_dir())

            adopt_work_dir = tmp_path / "adopt-project"
            _init_grok_source_repo(adopt_work_dir)
            adopt_name = _new_agent_name("adopt")
            # The clone holds a copy of the source session under the same id, so the adoption names its directory.
            source_session_dir = source_bucket_dir / source_session_id
            adopt_args = ["--no-ensure-clean", "--source", str(adopt_work_dir), "--adopt", str(source_session_dir)]
            with _created_grok_agent(profile, ctx, adopt_name, adopt_args) as adopt_state_dir:
                _send(profile, ctx, adopt_name, profile.recall_prompt())
                _wait_for_reply_with(adopt_state_dir, secret)
                adopt_session_id = _read_session_id(adopt_state_dir)
                adopt_bucket_dir = _session_bucket_dir(adopt_state_dir)
                adopt_session_dirs = sorted(path.name for path in adopt_bucket_dir.iterdir() if path.is_dir())

    assert len({source_bucket_dir.name, clone_bucket_dir.name, adopt_bucket_dir.name}) == 3
    assert len({source_session_id, clone_session_id, adopt_session_id}) == 3
    assert clone_session_dirs == sorted([source_session_id, clone_session_id])
    assert adopt_session_dirs == sorted([source_session_id, adopt_session_id])

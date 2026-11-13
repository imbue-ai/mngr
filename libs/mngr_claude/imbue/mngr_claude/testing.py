import json
import os
import shlex
import shutil
import subprocess
import threading
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from typing import Final
from urllib.parse import urlsplit

import httpx2
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import AgentReleaseProfile
from imbue.mngr.agents.agent_release_testing import agent_state_dir
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.hosts.tmux import build_tmux_capture_pane_command
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr_claude.claude_config import get_agent_claude_config_dir
from imbue.mngr_claude.dialogs import get_pane_text_without_input_row_hints
from imbue.mngr_claude.dialogs import has_input_prompt_line

# Shell-mode input rows verbatim from `capture-pane -e` captures of the claude build
# `CLAUDE_CODE_VERSION` names.
# An empty box: a bare `!` submitted on a fresh agent.
STYLED_EMPTY_SHELL_ROW_WITH_TRY_HINT: Final[str] = (
    '!\xa0\x1b[7m\x1b[39mT\x1b[0;2mry\x1b[0m \x1b[2m"how\x1b[0m \x1b[2mdo\x1b[0m \x1b[2mI\x1b[0m '
    '\x1b[2mlog\x1b[0m \x1b[2man\x1b[0m \x1b[2merror?"\x1b[0m'
)
# A human typed the hint's words.
STYLED_TYPED_SHELL_ROW_READING_LIKE_THE_HINT: Final[str] = '!\xa0\x1b[39mTry "how do I log an error?"\x1b[7m \x1b[0m'

# Captured from the claude build `CLAUDE_CODE_VERSION` names, started with --dangerously-skip-permissions
# and no skipDangerousModePermissionPrompt in its settings. Esc on it exits Claude.
BYPASS_PERMISSIONS_WARNING_PANE: Final[str] = (
    "────────────────────────────────────────\n"
    "  WARNING: Claude Code running in Bypass Permissions mode\n"
    "\n"
    "  In Bypass Permissions mode, Claude Code will not ask for your approval before running potentially dangerous commands.\n"
    "  This mode should only be used in a sandboxed container/VM that has restricted internet access and can easily be restored if damaged.\n"
    "\n"
    "  By proceeding, you accept all responsibility for actions taken while running in Bypass Permissions mode.\n"
    "\n"
    "  https://code.claude.com/docs/en/security\n"
    "\n"
    "  ❯ No, exit\n"
    "    Yes, I accept\n"
    "\n"
    "  Enter to confirm · Esc to cancel\n"
)


def _build_styled_input_box_pane(rule_colour_sgr: str, styled_input_row: str, styled_footer: str) -> str:
    rule = rule_colour_sgr + "─" * 40
    return "\n".join((rule, styled_input_row, rule, styled_footer))


def build_styled_shell_mode_pane(styled_input_row: str) -> str:
    """The bottom of a shell-mode pane under `capture-pane -e`, with shortened rules and footer around the row."""
    return _build_styled_input_box_pane(
        rule_colour_sgr="\x1b[38;5;211m",
        styled_input_row=styled_input_row,
        styled_footer="\x1b[39m  \x1b[38;5;211m! for shell mode\x1b[39m",
    )


def build_styled_prompt_pane(styled_input_row: str) -> str:
    """The bottom of a normal-mode pane under `capture-pane -e`, with shortened rules and footer around the row."""
    return _build_styled_input_box_pane(
        rule_colour_sgr="\x1b[38;5;244m",
        styled_input_row=styled_input_row,
        styled_footer="\x1b[39m  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m",
    )


# claude's native session store, relative to the agent state dir: ``<this>/<encoded-cwd>/<session-id>.jsonl``.
_CLAUDE_PROJECTS_RELPATH: Final[str] = (get_agent_claude_config_dir(Path()) / "projects").as_posix()

# mngr's raw copy of claude's session JSONL, relative to the agent state dir.
RAW_TRANSCRIPT_RELPATH: Final[str] = "logs/claude_transcript/events.jsonl"

# Claude Code's alias for the current Haiku, the cheapest tier.
RELEASE_TEST_MODEL: Final[str] = "haiku"


def _init_claude_workspace(path: Path) -> None:
    """Init a git repo whose own .gitignore excludes .claude/settings.local.json, as mngr's preflight requires."""
    init_git_repo(path, initial_commit=False)
    (path / ".gitignore").write_text(".claude/settings.local.json\n")
    run_git_command(path, "add", ".gitignore")
    run_git_command(path, "commit", "-m", "Add .gitignore")


def is_queue_operation_carrying(record: Mapping[str, Any], operation: str, token: str) -> bool:
    """Whether a raw session record is a ``queue-operation`` of ``operation`` whose content carries ``token``."""
    return (
        record.get("type") == "queue-operation"
        and record.get("operation") == operation
        and token in str(record.get("content", ""))
    )


def is_stop_hook_summary(record: Mapping[str, Any]) -> bool:
    """Whether a raw session record is the ``system``/``stop_hook_summary`` record of a Stop hook pass."""
    return record.get("type") == "system" and record.get("subtype") == "stop_hook_summary"


class ClaudeReleaseProfile(AgentReleaseProfile):
    """The release tests' claude plumbing: a real haiku claude run under ``ANTHROPIC_API_KEY``."""

    agent_type = "claude"
    common_transcript_subdir = "claude"
    forces_tool_call = True
    asserts_usage = True
    clear_slash_command = "/clear"
    native_session_preserved_relpaths = (_CLAUDE_PROJECTS_RELPATH,)
    unknown_slash_command = "/mngr-invalid-command-probe"

    def count_injected_deliveries(self, host_dir: Path, token: str) -> int:
        """Count the raw transcript's ``queue-operation``/``remove`` records carrying ``token``."""
        return sum(
            1
            for events_path in host_dir.glob(f"agents/*/{RAW_TRANSCRIPT_RELPATH}")
            for record in read_session_records(events_path)
            if is_queue_operation_carrying(record, "remove", token)
        )

    def adopt_session_arg(self, preserved_dir: Path) -> str:
        # Shallow, so subagent transcripts at ``<sid>/subagents/*.jsonl`` are not matched.
        projects_root = preserved_dir / _CLAUDE_PROJECTS_RELPATH
        matches = list(projects_root.glob("*/*.jsonl"))
        assert len(matches) == 1, (
            f"expected exactly one preserved claude session JSONL under {projects_root}, found {matches}"
        )
        return str(matches[0])

    def unavailable_reason(self) -> str | None:
        if shutil.which("claude") is None or not os.environ.get("ANTHROPIC_API_KEY"):
            return "Release test requires ANTHROPIC_API_KEY in the environment and `claude` on PATH."
        return None

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        env = get_subprocess_test_env(root_name="mngr-claude-release-test")

        # Disable the remote providers, which would probe Modal/Docker and reject the autouse test prefix.
        project_config_dir = tmp_path / ".mngr-claude-test"
        project_config_dir.mkdir(parents=True, exist_ok=True)
        (project_config_dir / "settings.local.toml").write_text(
            "is_allowed_in_pytest = true\n\n[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n"
        )
        env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)

        work_dir = tmp_path / "claude-source"
        _init_claude_workspace(work_dir)
        return AgentReleaseContext(env=env, project_dir=work_dir, host_dir=Path(env["MNGR_HOST_DIR"]))

    def prepare_adoption_project_dir(self, work_dir: Path) -> None:
        _init_claude_workspace(work_dir)

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        # ``--dangerously-skip-permissions``: the forced bash tool call must not pause on a permission dialog.
        return [
            "--no-ensure-clean",
            "--source",
            str(ctx.project_dir),
            "--pass-env",
            "ANTHROPIC_API_KEY",
            "--",
            "--dangerously-skip-permissions",
            "--model",
            RELEASE_TEST_MODEL,
        ]

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return run_mngr_subprocess(*args, env=dict(ctx.env), timeout=timeout)


class _ProxiedClaudeReleaseProfile(ClaudeReleaseProfile):
    """A claude release profile whose agent calls the API through ``base_url``."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        ctx = super().setup(tmp_path)
        return AgentReleaseContext(
            env={**ctx.env, "ANTHROPIC_BASE_URL": self._base_url},
            project_dir=ctx.project_dir,
            host_dir=ctx.host_dir,
            teardown=ctx.teardown,
        )

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        args = list(super().create_extra_args(ctx))
        separator_index = args.index("--")
        return [*args[:separator_index], "--pass-env", "ANTHROPIC_BASE_URL", *args[separator_index:]]


_ANTHROPIC_API_BASE_URL: Final[str] = "https://api.anthropic.com"
_MESSAGES_API_PATH: Final[str] = "/v1/messages"
_UPSTREAM_TIMEOUT_SECONDS: Final[float] = 600.0
_UNRELAYED_HEADERS: Final[frozenset[str]] = frozenset(
    {"connection", "keep-alive", "transfer-encoding", "content-length", "host"}
)


class _RecordedApiRequest(FrozenModel):
    """One request the recording proxy relayed to the Anthropic API."""

    path: str = Field(description="The request target, e.g. ``/v1/messages?beta=true``")
    body: bytes = Field(description="The raw request body")


@contextmanager
def _recording_anthropic_proxy() -> Generator[tuple[str, list[_RecordedApiRequest]], None, None]:
    """Run a local proxy to the Anthropic API that records each request; yield its base URL and the requests."""
    recorded: list[_RecordedApiRequest] = []

    class _RelayHandler(BaseHTTPRequestHandler):
        """Relays each request to the Anthropic API and records its path and body."""

        def _relay(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            recorded.append(_RecordedApiRequest(path=self.path, body=body))
            headers = {k: v for k, v in self.headers.items() if k.lower() not in _UNRELAYED_HEADERS}
            with httpx2.stream(
                self.command,
                f"{_ANTHROPIC_API_BASE_URL}{self.path}",
                content=body,
                headers=headers,
                timeout=_UPSTREAM_TIMEOUT_SECONDS,
            ) as response:
                # Raw, so the bytes stay encoded as the relayed content-encoding header says.
                payload = b"".join(response.iter_raw())
            self.send_response(response.status_code)
            for key, value in response.headers.multi_items():
                if key.lower() not in _UNRELAYED_HEADERS:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _relay
        do_POST = _relay

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _RelayHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="anthropic-recording-proxy").start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", recorded
    finally:
        server.shutdown()
        server.server_close()


def _recorded_messages_request_bodies(recorded: Sequence[_RecordedApiRequest]) -> list[dict[str, Any]]:
    """The JSON bodies of the recorded ``/v1/messages`` requests, without ``count_tokens`` or any other endpoint's."""
    bodies = []
    for request in list(recorded):
        if urlsplit(request.path).path != _MESSAGES_API_PATH:
            continue
        body = json.loads(request.body)
        assert isinstance(body, dict), f"a {request.path} request body is not a JSON object: {request.body[:200]!r}"
        bodies.append(body)
    return bodies


AGENT_CREATE_TIMEOUT_SECONDS: Final[float] = 600.0
AGENT_DESTROY_TIMEOUT_SECONDS: Final[float] = 300.0
# How long a real haiku turn may take to reach its next observable point.
TURN_PROGRESS_TIMEOUT_SECONDS: Final[float] = 180.0
SESSION_RECORD_TIMEOUT_SECONDS: Final[float] = 90.0
_TURN_END_POLL_INTERVAL_SECONDS: Final[float] = 1.0


@contextmanager
def created_agent(
    profile: ClaudeReleaseProfile, ctx: AgentReleaseContext, agent_name: str, *, destroy_timeout: float
) -> Generator[None, None, None]:
    """Create the agent, and on exit force-destroy it and tear ``ctx`` down, including after a create that failed part-way."""
    try:
        create = profile.run_mngr(
            ctx,
            "create",
            agent_name,
            profile.agent_type,
            "--no-connect",
            "--yes",
            *profile.create_extra_args(ctx),
            timeout=AGENT_CREATE_TIMEOUT_SECONDS,
        )
        assert create.returncode == 0, f"create failed:\n{create.stdout}\n{create.stderr}"
        yield
    finally:
        try:
            profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=destroy_timeout)
        finally:
            if ctx.teardown is not None:
                ctx.teardown()


def drift_note(ctx: AgentReleaseContext, subject: str) -> str:
    """A failure-message suffix naming the installed claude, e.g. ``(claude version: <version>; a failure here means <subject> drifted)``."""
    result = subprocess.run(["claude", "--version"], capture_output=True, text=True, env=dict(ctx.env), timeout=60.0)
    claude_version = result.stdout.strip() or "unknown"
    return f"(claude version: {claude_version}; a failure here means {subject} drifted)"


def wait_for_agent_to_stop_running(
    profile: ClaudeReleaseProfile, ctx: AgentReleaseContext, *, timeout: float, failure: str
) -> None:
    """Wait until the agent stops reporting RUNNING, failing with ``failure`` if it still does after ``timeout``."""
    assert poll_until(
        lambda: not profile.is_reporting_running(ctx.host_dir),
        timeout=timeout,
        poll_interval=_TURN_END_POLL_INTERVAL_SECONDS,
    ), failure


NOTIFICATION_HOOK_EVENT: Final[str] = "Notification"
# The notification claude raises for an idle agent, which mngr's Notification hook also clears RUNNING on.
IDLE_PROMPT_NOTIFICATION_TYPE: Final[str] = "idle_prompt"


def wait_for_agent_to_stop_running_before_idle_notification(
    profile: ClaudeReleaseProfile,
    ctx: AgentReleaseContext,
    # Holds every ``Notification`` hook payload, as ``commit_hook_payload_saver`` saves them.
    payload_dump_dir: Path,
    *,
    after: str,
    drift: str,
    consumer: str,
) -> None:
    """Wait until the agent stops reporting RUNNING after ``after``; fail if claude's idle notification fired first."""
    wait_for_agent_to_stop_running(
        profile,
        ctx,
        timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
        failure=f"the agent still reports RUNNING {TURN_PROGRESS_TIMEOUT_SECONDS:.0f}s after {after} {drift}; "
        f"consumer: {consumer}",
    )
    assert poll_until(
        lambda: len(read_saved_payloads(payload_dump_dir)) == len(list(payload_dump_dir.glob("payload.*"))),
        timeout=SESSION_RECORD_TIMEOUT_SECONDS,
        poll_interval=0.2,
    ), f"a hook payload saved in {payload_dump_dir} never finished writing"
    notification_types = [payload.get("notification_type") for payload in read_saved_payloads(payload_dump_dir)]
    assert IDLE_PROMPT_NOTIFICATION_TYPE not in notification_types, (
        f"after {after} the agent left RUNNING only through claude's {IDLE_PROMPT_NOTIFICATION_TYPE} notification "
        f"{drift}; consumer: {consumer}"
    )


def wait_for_turn_to_end(
    profile: ClaudeReleaseProfile, ctx: AgentReleaseContext, prompt_text: str, drift: str
) -> None:
    """Wait until the turn ``prompt_text`` started has run its Stop hooks and the agent stops reporting RUNNING.

    A send can return before claude starts the turn, so RUNNING being clear does not show the turn ended.
    """
    session_file = wait_for_native_session_file(ctx)
    assert poll_until(
        lambda: any(
            is_stop_hook_summary(record)
            for record in records_after_user_prompt(read_session_records(session_file), prompt_text)
        ),
        timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
        poll_interval=_TURN_END_POLL_INTERVAL_SECONDS,
    ), (
        f"no stop_hook_summary record follows the user record carrying {prompt_text!r} {drift}; "
        "consumer: wait_for_stop_hook.sh, which reads it as the turn end"
    )
    wait_for_agent_to_stop_running(
        profile,
        ctx,
        timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
        failure=f"the agent still reports RUNNING after the turn {prompt_text!r} started ran its Stop hooks {drift}; "
        "consumer: wait_for_stop_hook.sh",
    )


def held_tool_command(start_marker: Path, release: Path) -> str:
    """A shell command that touches ``start_marker``, then waits until ``release`` exists.

    claude's Bash tool kills a command after its 2-minute default timeout.
    """
    return f"touch {shlex.quote(str(start_marker))} && until [ -e {shlex.quote(str(release))} ]; do sleep 1; done"


def start_held_tool_call(
    profile: ClaudeReleaseProfile, ctx: AgentReleaseContext, agent_name: str, tool_marker: Path
) -> Path:
    """Send a turn whose Bash call holds until the returned release path exists, and wait until that call runs."""
    release = tool_marker.with_name(f"{tool_marker.name}-release")
    send_expecting_success(
        profile,
        ctx,
        agent_name,
        f"Use the Bash tool to run exactly: {held_tool_command(tool_marker, release)}. Then reply with just DONE.",
    )
    assert poll_until(tool_marker.exists, timeout=TURN_PROGRESS_TIMEOUT_SECONDS, poll_interval=1.0), (
        "the turn never started its Bash call that holds it open until the test releases it"
    )
    return release


def append_project_settings(ctx: AgentReleaseContext, settings_toml: str) -> None:
    """Append ``settings_toml`` to the project ``settings.local.toml`` that ``ctx`` runs mngr with."""
    settings_path = Path(ctx.env["MNGR_PROJECT_CONFIG_DIR"]) / "settings.local.toml"
    with settings_path.open("a") as settings_file:
        settings_file.write(settings_toml)


_PROXIED_TURN_PROMPT: Final[str] = "Reply with just OK."


def record_one_proxied_turn(
    tmp_path: Path, settings_toml: str, drift_subject: str
) -> tuple[list[dict[str, Any]], str]:
    """Run one turn on a fresh agent that calls the API through a recording proxy.

    The agent runs with ``settings_toml`` appended to its project settings. Returns the turn's
    ``/v1/messages`` request bodies and a ``drift_note`` for ``drift_subject``.
    """
    with _recording_anthropic_proxy() as (base_url, recorded):
        profile = _ProxiedClaudeReleaseProfile(base_url)
        ctx = profile.setup(tmp_path)
        append_project_settings(ctx, settings_toml)
        drift = drift_note(ctx, drift_subject)
        agent_name = f"claude-proxied-{get_short_random_string()}"
        with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
            send_expecting_success(profile, ctx, agent_name, _PROXIED_TURN_PROMPT)
            wait_for_turn_to_end(profile, ctx, _PROXIED_TURN_PROMPT, drift)
    return _recorded_messages_request_bodies(recorded), drift


# Wider than the ``POST_SUBMIT_DIALOG_OBSERVE_SECONDS`` default, so a slow host's redraw after each key mngr presses
# into a dialog is waited for.
_WIDENED_DIALOG_OBSERVE_SECONDS: Final[float] = 4.0


def setup_ctx_that_answers_dialogs(profile: ClaudeReleaseProfile, tmp_path: Path) -> AgentReleaseContext:
    """Set up a release ctx whose agent answers every dialog mngr has a sensible answer for."""
    ctx = profile.setup(tmp_path)
    append_project_settings(
        ctx,
        f"\n[agent_types.claude]\n"
        f'sensibly_deal_with_dialogs = ["ALL_KNOWN_DIALOGS"]\n'
        f"post_submit_dialog_observe_seconds = {_WIDENED_DIALOG_OBSERVE_SECONDS}\n",
    )
    return ctx


_TMUX_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0


def _agent_pane_target(ctx: AgentReleaseContext, agent_name: str) -> TmuxWindowTarget:
    """The tmux target of the agent's primary window."""
    return TmuxWindowTarget(session_name=f"{ctx.env['MNGR_PREFIX']}{agent_name}")


def _capture_agent_pane_output(
    ctx: AgentReleaseContext, agent_name: str, *, is_scrollback_included: bool, is_styled: bool
) -> str:
    """The output of ``tmux capture-pane`` on the agent's primary tmux window pane."""
    capture_command = build_tmux_capture_pane_command(
        _agent_pane_target(ctx, agent_name), include_scrollback=is_scrollback_included, include_styles=is_styled
    )
    result = subprocess.run(
        shlex.split(capture_command),
        capture_output=True,
        text=True,
        check=False,
        timeout=_TMUX_COMMAND_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, f"`{capture_command}` failed: {result.stderr.strip()}"
    return result.stdout


def capture_agent_pane(ctx: AgentReleaseContext, agent_name: str) -> str:
    """Capture the agent's primary tmux window pane as plain text (colors stripped, like mngr)."""
    return _capture_agent_pane_output(ctx, agent_name, is_scrollback_included=False, is_styled=False)


def capture_agent_pane_with_scrollback(ctx: AgentReleaseContext, agent_name: str) -> str:
    """Capture the agent's primary tmux window pane as plain text, including the scrollback above the fold."""
    return _capture_agent_pane_output(ctx, agent_name, is_scrollback_included=True, is_styled=False)


def capture_agent_pane_without_input_row_hints(ctx: AgentReleaseContext, agent_name: str) -> str:
    """Capture the agent's primary tmux window pane as mngr reads it for dialogs: without the input row's hints."""
    return get_pane_text_without_input_row_hints(
        _capture_agent_pane_output(ctx, agent_name, is_scrollback_included=False, is_styled=True)
    )


def wait_for_agent_pane(
    ctx: AgentReleaseContext,
    agent_name: str,
    is_shown: Callable[[str], bool],
    *,
    capture: Callable[[AgentReleaseContext, str], str],
    timeout: float,
    failure: str,
) -> str:
    """Poll ``capture`` of the agent's pane until ``is_shown`` holds and return that pane, failing with the last one."""
    captured: list[str] = []

    def shown() -> bool:
        captured.append(capture(ctx, agent_name))
        return is_shown(captured[-1])

    assert poll_until(shown, timeout=timeout, poll_interval=1.0), f"{failure}; last pane:\n{captured[-1]}"
    return captured[-1]


_INPUT_PROMPT_TIMEOUT_SECONDS: Final[float] = 60.0


def wait_for_agent_prompt(ctx: AgentReleaseContext, agent_name: str, *, failure: str) -> str:
    """Poll the agent pane until claude draws its ``❯`` input prompt; return that pane.

    ``mngr create`` returns once the launch command has been sent, a beat before the TUI draws its prompt.
    """
    return wait_for_agent_pane(
        ctx,
        agent_name,
        has_input_prompt_line,
        capture=capture_agent_pane,
        timeout=_INPUT_PROMPT_TIMEOUT_SECONDS,
        failure=failure,
    )


def send_keys_to_agent_pane(ctx: AgentReleaseContext, agent_name: str, key: str, *, is_literal: bool) -> None:
    """Send ``key`` to the agent's primary tmux window pane, as literal text when ``is_literal``."""
    literal_flag = ["-l"] if is_literal else []
    target_arg = _agent_pane_target(ctx, agent_name).as_shell_arg()
    subprocess.run(
        ["tmux", "send-keys", "-t", *shlex.split(target_arg), *literal_flag, key],
        check=True,
        timeout=_TMUX_COMMAND_TIMEOUT_SECONDS,
    )


# The ``message.usage`` counters whose sum is a reply's prompt tokens.
PROMPT_TOKEN_USAGE_FIELDS: Final[tuple[str, ...]] = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def read_session_records(session_file: Path) -> list[dict[str, Any]]:
    """The JSON object records of a JSONL file, skipping any other line, such as a last line still being written."""
    records = []
    for line in session_file.read_text(errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def wait_for_session_record(
    session_file: Path, predicate: Callable[[dict[str, Any]], bool], *, failure: str
) -> dict[str, Any]:
    assert poll_until(
        lambda: any(predicate(record) for record in read_session_records(session_file)),
        timeout=SESSION_RECORD_TIMEOUT_SECONDS,
        poll_interval=2.0,
    ), failure
    return next(record for record in read_session_records(session_file) if predicate(record))


def content_text(content: Any) -> str:
    """Flatten a content value (str or block-array) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
    return ""


def session_message_text(record: Mapping[str, Any]) -> str:
    """Flatten a user/assistant record's message.content (str or block-array) to text."""
    return content_text((record.get("message") or {}).get("content"))


def records_after_user_prompt(records: Sequence[Mapping[str, Any]], prompt_text: str) -> list[Mapping[str, Any]]:
    """The records after the first user record carrying ``prompt_text``; empty while there is none."""
    for index, record in enumerate(records):
        if record.get("type") == "user" and prompt_text in session_message_text(record):
            return list(records[index + 1 :])
    return []


def wait_for_native_session_file(ctx: AgentReleaseContext) -> Path:
    """Wait for the agent's native session JSONL, named by its claude_session_id marker, and return it.

    Claude writes the file lazily on the first prompt, so call this after a send.
    """
    agent_dir = agent_state_dir(ctx.host_dir)
    session_id = (agent_dir / "claude_session_id").read_text().strip()
    assert session_id != "", "claude_session_id marker is empty"
    session_glob = f"{_CLAUDE_PROJECTS_RELPATH}/*/{session_id}.jsonl"
    assert poll_until(lambda: len(list(agent_dir.glob(session_glob))) == 1, timeout=60.0, poll_interval=2.0), (
        f"native session JSONL not at <config-dir>/projects/<encoded-cwd>/{session_id}.jsonl "
        f"{drift_note(ctx, 'the native session file layout')}; "
        "consumers: every probe's native-transcript path expression"
    )
    return next(agent_dir.glob(session_glob))


def commit_project_claude_settings(project_dir: Path, settings: Mapping[str, Any], commit_message: str) -> None:
    """Commit ``settings`` as the project's ``.claude/settings.json``."""
    (project_dir / ".claude").mkdir(exist_ok=True)
    (project_dir / ".claude" / "settings.json").write_text(json.dumps(settings))
    run_git_command(project_dir, "add", ".claude")
    run_git_command(project_dir, "commit", "-m", commit_message)


def build_payload_saving_command(dump_dir: Path) -> str:
    """A shell command that saves its stdin to a new ``payload.*`` file in ``dump_dir``."""
    return f'cat > "$(mktemp {shlex.quote(str(dump_dir))}/payload.XXXXXX)"'


def commit_hook_payload_saver(project_dir: Path, dump_dir: Path, events: Sequence[str], matcher: str | None) -> None:
    """Commit a project hook for each of ``events`` that saves each stdin payload to its own file in ``dump_dir``."""
    dump_dir.mkdir()
    hook: dict[str, Any] = {"hooks": [{"type": "command", "command": build_payload_saving_command(dump_dir)}]}
    if matcher is not None:
        hook["matcher"] = matcher
    commit_project_claude_settings(
        project_dir,
        {"hooks": {event: [hook] for event in events}},
        f"Add {', '.join(events)} hooks that save their stdin",
    )


def read_saved_payloads(dump_dir: Path) -> list[dict[str, Any]]:
    """The JSON object payloads saved in ``dump_dir``, skipping any still being written."""
    payloads = []
    for path in dump_dir.glob("payload.*"):
        try:
            payload = json.loads(path.read_text(errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads

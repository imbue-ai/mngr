"""Release test: full end-to-end codex agent flow against the real ``codex`` CLI.

Drives the real ``mngr`` CLI through the shared agent release lifecycle (create ->
WAITING -> message -> transcript -> stop/start resume -> destroy). The arc and
assertions live in ``imbue.mngr.agents.agent_release_testing``; this file supplies
codex's plumbing via an :class:`AgentReleaseProfile`.

codex's only real specifics over the other ports: a throwaway ``CODEX_HOME`` seeded
with a copy of the user's real ``~/.codex/auth.json`` (so it authenticates without
touching the real config), and putting the real ``codex`` binary on ``PATH``. ``mngr``
itself is run via ``uv run mngr`` from the checkout (like the sibling release tests), so
local changes are exercised. Host-dir and tmux-server isolation come for free from the
autouse ``setup_test_mngr_env`` fixture, same as every other test.

It is a ``release`` test (not run in CI) and requires the ``codex`` binary plus a
logged-in ``~/.codex/auth.json``; skipped otherwise. The model is pinned to a
ChatGPT-account-safe slug because codex's default ``*-codex`` model is rejected for
ChatGPT-account logins (see the lib README).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from contextlib import suppress
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import AgentReleaseProfile
from imbue.mngr.agents.agent_release_testing import agent_state_dir
from imbue.mngr.agents.agent_release_testing import is_step_from
from imbue.mngr.agents.agent_release_testing import marker_path
from imbue.mngr.agents.agent_release_testing import run_agent_release_lifecycle
from imbue.mngr.agents.agent_release_testing import run_message_delivery_journey
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.agents.agent_release_testing import wait_for_records
from imbue.mngr.hosts.background_tasks import TURN_END_FILE_NAME
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.app_server_client import connect_app_server_transport
from imbue.mngr_codex.codex_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_codex.codex_config import APP_SERVER_THREAD_FILENAME
from imbue.mngr_codex.codex_config import PERMISSIONS_WAITING_FILENAME
from imbue.mngr_codex.codex_config import get_codex_app_server_socket_path
from imbue.mngr_codex.codex_config import get_codex_home

# Resolved at import time, before the autouse ``setup_test_mngr_env`` fixture redirects
# $HOME / mutates PATH: the real home (auth source) and the real codex binary.
_REAL_HOME = Path.home()
_CODEX_BIN = shutil.which("codex") or next(
    (
        candidate
        for candidate in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex", str(_REAL_HOME / ".local/bin/codex"))
        if Path(candidate).exists()
    ),
    None,
)
_REAL_AUTH = _REAL_HOME / ".codex" / "auth.json"
_REAL_MODELS_CACHE = _REAL_HOME / ".codex" / "models_cache.json"

# ChatGPT-account-safe model. codex's compiled default is a ``*-codex`` slug a ChatGPT
# login rejects with a 400. Update if this account's available models change.
_CODEX_MODEL = "gpt-6.1-sol"

# Disable the remote-provider backends for every command: a purely local agent test,
# and leaving them on makes mngr probe Modal/Docker. ``-S`` is a per-command option, so
# it is injected right after the subcommand.
_PROVIDER_SETTINGS: tuple[str, ...] = (
    "-S",
    "providers.modal.is_enabled=false",
    "-S",
    "providers.docker.is_enabled=false",
)


class _CodexReleaseProfile(AgentReleaseProfile):
    agent_type = "codex"
    common_transcript_subdir = "codex"
    # codex forces the bash tool call (run unattended via approval_policy=never, set in
    # create_extra_args; its converter surfaces it as a nested assistant tool_call). It does
    # not report token usage, so that assertion is off (observing the RUNNING marker is universal).
    forces_tool_call = True
    asserts_usage = False
    # This is the store the adopt-from-preserved arc adopts: after destroy, a fresh agent
    # in a new worktree adopts the just-preserved session by id and must recall the
    # pre-destroy secret -- proving the rollout store resumes and the cwd rebind avoids the modal.
    native_session_preserved_relpaths = ("plugin/codex/home/sessions",)

    def adopt_session_arg(self, preserved_dir: Path) -> str:
        # The preserved tree records the root codex session id at ``codex_root_session``
        # (ROOT_SESSION_FILENAME); the plugin resolves that id against the preserved
        # store. Reading the file keeps the test independent of the rollout's date path.
        return (preserved_dir / "codex_root_session").read_text().strip()

    def unavailable_reason(self) -> str | None:
        if _CODEX_BIN is None or not _REAL_AUTH.exists():
            return "codex CLI not installed or ~/.codex/auth.json missing (not logged in)"
        return None

    def _bind_root_thread(self, client: CodexAppServerClient, agent_state_dir: Path) -> bool:
        """Bind the agent's root thread (persisted id, else the single loaded one); False if none."""
        thread_file = agent_state_dir / APP_SERVER_THREAD_FILENAME
        persisted = thread_file.read_text().strip() if thread_file.exists() else ""
        loaded = client.thread_loaded_list()
        if persisted and persisted in loaded:
            client.bind_thread(persisted)
            return True
        if persisted:
            try:
                client.thread_resume(persisted)
                return True
            except CodexAppServerError:
                return False
        if len(loaded) == 1:
            client.bind_thread(loaded[0])
            return True
        return False

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        assert _CODEX_BIN is not None
        # Inherits the autouse fixture's isolated MNGR_HOST_DIR, redirected $HOME, and private
        # tmux server (via copied os.environ); we only add codex's auth home and the real codex bin.
        env = get_subprocess_test_env(root_name="mngr-codex-release-test")

        user_codex_home = tmp_path / "user_codex"
        user_codex_home.mkdir()
        shutil.copy2(_REAL_AUTH, user_codex_home / "auth.json")
        (user_codex_home / "auth.json").chmod(0o600)
        if _REAL_MODELS_CACHE.exists():
            # Avoids codex's "model metadata not found" degradation on a fresh home.
            shutil.copy2(_REAL_MODELS_CACHE, user_codex_home / "models_cache.json")
        env["CODEX_HOME"] = str(user_codex_home)

        # Put the real codex binary on PATH (the autouse fixture redirects HOME).
        # Append, don't prepend: the resource guard prepends its tmux wrapper dir to PATH to
        # track tmux use, and prepending the real bin dir (which also holds the real tmux)
        # would shadow that wrapper and trip the guard's "marked tmux but never invoked" check.
        env["PATH"] = env.get("PATH", "") + os.pathsep + str(Path(_CODEX_BIN).parent)

        repo = tmp_path / "repo"
        init_git_repo(repo)
        return AgentReleaseContext(env=env, project_dir=repo, host_dir=Path(env["MNGR_HOST_DIR"]))

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        # Pass the work dir via --source (rather than the mngr cwd) so ``mngr`` can run from
        # the checkout under ``uv run`` -- matching the sibling release tests.
        # auto_allow_permissions sets approval_policy=never, so the forced bash tool call
        # runs without pausing on an approval prompt.
        return [
            "--no-ensure-clean",
            "--source",
            str(ctx.project_dir),
            "-S",
            f"agent_types.codex.model={_CODEX_MODEL}",
            "-S",
            "agent_types.codex.auto_allow_permissions=true",
        ]

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        # ``uv run mngr`` from the checkout (the default cwd), so local changes are exercised,
        # with the provider-disabling -S flags injected right after the subcommand.
        return run_mngr_subprocess(args[0], *_PROVIDER_SETTINGS, *args[1:], env=dict(ctx.env), timeout=timeout)


@contextmanager
def _created_codex_agent(
    profile: _CodexReleaseProfile, ctx: AgentReleaseContext, agent_name: str, *create_args: str
) -> Generator[None, None, None]:
    """Create a codex agent with ``create_args``; on exit force-destroy it and tear ``ctx`` down, even after a failed create."""
    try:
        create = profile.run_mngr(
            ctx, "create", agent_name, "codex", "--no-connect", "--yes", *create_args, timeout=600
        )
        assert create.returncode == 0, f"create failed:\n{create.stdout}\n{create.stderr}"
        yield
    finally:
        try:
            profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=150)
        finally:
            if ctx.teardown is not None:
                ctx.teardown()


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_codex_agent_full_lifecycle(tmp_path: Path) -> None:
    """Drive the real ``codex`` CLI through mngr's full agent release lifecycle and assert
    each stage genuinely happened (not merely that commands exited 0).

    Runs the shared arc in ``run_agent_release_lifecycle`` against the ``_CodexReleaseProfile``:
    create the agent, wait for it to reach WAITING, send a message, read back the transcript
    (which must contain the agent's reply and the forced bash tool call surfaced as a nested
    assistant tool_call), stop and restart to confirm the session resumes, then destroy. The
    final leg adopts the just-preserved native codex session by its root id in a fresh worktree
    and requires the new agent to recall a pre-destroy secret -- which only passes if the rollout
    store truly resumed, so it would fail if resume/adopt were a no-op.
    """
    run_agent_release_lifecycle(_CodexReleaseProfile(), tmp_path)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_codex_message_delivery_journey(tmp_path: Path) -> None:
    """Drive the app-server send pipeline through its racey delivery scenarios.

    codex delivers over the ``codex app-server`` daemon (idle -> ``turn/start``, busy ->
    ``turn/steer``), so this exercises that path: idle delivery, send-while-busy (parked
    into the running turn), rapid sequential sends, and a long buffer-pasted message --
    each delivered exactly once.
    """
    run_message_delivery_journey(_CodexReleaseProfile(), tmp_path)


@pytest.mark.release
@pytest.mark.harness_internals(harness="codex")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_codex_agent_with_unoffered_pinned_model_starts_on_account_default(tmp_path: Path) -> None:
    """A pin the account does not offer, with ``fall_back_to_account_default_model``, lands on the account default.

    The root conversation mngr establishes at create is read back from the live daemon: its model must be
    the one the account's own ``model/list`` marks default, not the unusable pin in config.toml.
    """
    profile = _CodexReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    ctx = profile.setup(tmp_path)
    agent_name = f"codex-fallback-{get_short_random_string()}"
    unoffered_model = f"gpt-unoffered-{get_short_random_string()}"
    with _created_codex_agent(
        profile,
        ctx,
        agent_name,
        "--no-ensure-clean",
        "--source",
        str(ctx.project_dir),
        "-S",
        f"agent_types.codex.model={unoffered_model}",
        "-S",
        "agent_types.codex.fall_back_to_account_default_model=true",
    ):
        agent_dirs = [path for path in (ctx.host_dir / "agents").glob("*") if path.is_dir()]
        assert len(agent_dirs) == 1
        root_thread_id = (agent_dirs[0] / APP_SERVER_THREAD_FILENAME).read_text().strip()
        socket_path = get_codex_app_server_socket_path(get_codex_home(agent_dirs[0]))
        client = CodexAppServerClient(transport=connect_app_server_transport(socket_path))
        try:
            client.initialize("mngr-codex-release-test", "0")
            account_default = next(
                offered.model for offered in client.model_list(include_hidden=True) if offered.is_default
            )
            root_thread = client.thread_resume(root_thread_id)
        finally:
            client.close()

        assert root_thread.model == account_default
        assert root_thread.model != unoffered_model


@pytest.mark.release
@pytest.mark.harness_internals(harness="codex")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_codex_agent_steps_carry_the_session_and_turn_ids_codex_reports(tmp_path: Path) -> None:
    """Every agent step carries, as ``extra.harness_session_id``, the root thread id codex's daemon reports.

    Pins the rollout's ``session_meta`` naming the session (which codex also sends in the
    ``x-codex-turn-metadata`` request header) and its ``turn_context`` naming each turn.
    """
    profile = _CodexReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    ctx = profile.setup(tmp_path)
    agent_name = f"codex-sessionid-{get_short_random_string()}"
    with _created_codex_agent(profile, ctx, agent_name, *profile.create_extra_args(ctx)):
        send_expecting_success(profile, ctx, agent_name, "Reply with just OK.")

        common_records = wait_for_records(
            ctx.host_dir,
            profile.common_transcript_subdir,
            lambda rs: any(is_step_from(r, "agent") for r in rs),
            timeout=180,
            description="no agent step reached the common transcript",
        )
        root_thread_id = (agent_state_dir(ctx.host_dir) / APP_SERVER_THREAD_FILENAME).read_text().strip()
        extras = [r.get("extra") or {} for r in common_records if is_step_from(r, "agent")]
        assert root_thread_id and all(extra.get("harness_session_id") == root_thread_id for extra in extras), (
            f"agent steps carry {extras}, not harness_session_id {root_thread_id!r}; the rollout's session_meta "
            "drifted (consumer: common_transcript_convert._session_meta_id)"
        )
        assert all(extra.get("harness_turn_id") for extra in extras), (
            f"agent steps carry {extras}, some without a harness_turn_id; the rollout's turn_context drifted "
            "(consumer: common_transcript_convert._stamp_inference_context)"
        )


_TURN_END_CONSUMER: Final[str] = (
    "stream_transcript.sh's _record_turn_end, which reads the rollout's task_complete (its error's codex_error_info) "
    "and turn_aborted (its reason) records"
)


def _published_agent(profile: _CodexReleaseProfile, ctx: AgentReleaseContext, agent_name: str) -> dict[str, Any]:
    listing = profile.run_mngr(ctx, "list", "--provider", "local", "--format", "json", timeout=120)
    assert listing.returncode == 0, f"mngr list failed:\n{listing.stdout}\n{listing.stderr}"
    (agent,) = [agent for agent in json.loads(listing.stdout)["agents"] if agent["name"] == agent_name]
    return agent


def _published_turn_end(profile: _CodexReleaseProfile, ctx: AgentReleaseContext, agent_name: str) -> dict[str, Any]:
    return _published_agent(profile, ctx, agent_name).get("turn_end") or {}


def _wait_for_turn_end(
    profile: _CodexReleaseProfile, ctx: AgentReleaseContext, agent_name: str, kind: str
) -> dict[str, Any]:
    poll_until(lambda: _published_turn_end(profile, ctx, agent_name).get("kind") == kind, timeout=180, poll_interval=2)
    turn_end = _published_turn_end(profile, ctx, agent_name)
    assert turn_end.get("kind") == kind, f"mngr list published turn_end {turn_end!r}, not {kind}; {_TURN_END_CONSUMER}"
    return turn_end


@contextmanager
def _root_thread_client(
    profile: _CodexReleaseProfile, ctx: AgentReleaseContext
) -> Generator[CodexAppServerClient, None, None]:
    """A connection to the agent's daemon, bound to its root thread."""
    state_dir = agent_state_dir(ctx.host_dir)
    client = CodexAppServerClient(
        transport=connect_app_server_transport(get_codex_app_server_socket_path(get_codex_home(state_dir)))
    )
    try:
        client.initialize("mngr-codex-release-test", "0")
        assert profile._bind_root_thread(client, state_dir)
        yield client
    finally:
        client.close()


def _running_turn_id(profile: _CodexReleaseProfile, ctx: AgentReleaseContext) -> str:
    """The id of the root thread's turn in progress, as codex's daemon reports it."""
    with _root_thread_client(profile, ctx) as client:
        turns = client.thread_read(include_turns=True)["thread"]["turns"]
    (turn_id,) = [turn["id"] for turn in turns if turn.get("status") == "inProgress"]
    return turn_id


def _interrupt_turn(profile: _CodexReleaseProfile, ctx: AgentReleaseContext, turn_id: str) -> None:
    with _root_thread_client(profile, ctx) as client:
        client.interrupt(turn_id)


_TURN_MARKER_CONSUMER: Final[str] = (
    "codex_turn_hook.sh, which writes the UserPromptSubmit payload's turn_id into `active`, leaves it at Stop and "
    "removes it at Interrupt, and stream_transcript.sh, which writes a task_started's turn_id into `active` and "
    "removes it at the rollout's end record of that turn_id"
)


@pytest.mark.release
@pytest.mark.harness_internals(harness="codex")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_codex_agent_publishes_a_completed_and_an_interrupted_turn_end(tmp_path: Path) -> None:
    """mngr list publishes turn_end COMPLETED after a reply and INTERRUPTED after the daemon interrupts a turn."""
    profile = _CodexReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    ctx = profile.setup(tmp_path)
    agent_name = f"codex-turn-end-{get_short_random_string()}"
    with _created_codex_agent(profile, ctx, agent_name, *profile.create_extra_args(ctx)):
        send_expecting_success(profile, ctx, agent_name, "Reply with just OK.")
        completed = _wait_for_turn_end(profile, ctx, agent_name, "COMPLETED")

        send_expecting_success(profile, ctx, agent_name, "Run `sleep 120` in the shell, wait for it, then reply DONE.")
        assert poll_until(lambda: profile.is_reporting_running(ctx.host_dir), timeout=120, poll_interval=1)
        marked_turn = marker_path(ctx.host_dir).read_text().strip()
        running = _published_agent(profile, ctx, agent_name)
        running_turn = _running_turn_id(profile, ctx)
        _interrupt_turn(profile, ctx, running_turn)
        interrupted = _wait_for_turn_end(profile, ctx, agent_name, "INTERRUPTED")
        after_interrupt = _published_agent(profile, ctx, agent_name)

    assert completed["native_type"] == ""
    assert (running["state"], running.get("turn_end")) == ("RUNNING", None), running
    assert marked_turn == running_turn, (
        f"`active` names {marked_turn!r}, not {running_turn!r}; {_TURN_MARKER_CONSUMER}"
    )
    assert after_interrupt["state"] == "WAITING", f"{after_interrupt['state']}; {_TURN_MARKER_CONSUMER}"
    assert interrupted["native_type"] == "interrupted", f"{interrupted!r}; {_TURN_END_CONSUMER}"


@pytest.mark.release
@pytest.mark.harness_internals(harness="codex")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_codex_agent_publishes_a_failed_turn_end_named_by_codex(tmp_path: Path) -> None:
    """A turn on a model the account does not offer publishes turn_end FAILED with codex's error name."""
    profile = _CodexReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    ctx = profile.setup(tmp_path)
    agent_name = f"codex-turn-failed-{get_short_random_string()}"
    unoffered_model = f"gpt-unoffered-{get_short_random_string()}"
    with _created_codex_agent(
        profile,
        ctx,
        agent_name,
        "--no-ensure-clean",
        "--source",
        str(ctx.project_dir),
        "-S",
        f"agent_types.codex.model={unoffered_model}",
    ):
        send_expecting_success(profile, ctx, agent_name, "Reply with just OK.")
        failed = _wait_for_turn_end(profile, ctx, agent_name, "FAILED")
        assert poll_until(lambda: not profile.is_reporting_running(ctx.host_dir), timeout=30, poll_interval=1)
        after_failure = _published_agent(profile, ctx, agent_name)

    assert failed["native_type"], f"{failed!r} names no codex_error_info; {_TURN_END_CONSUMER}"
    assert after_failure["state"] == "WAITING", f"{after_failure['state']}; {_TURN_MARKER_CONSUMER}"


def _streamed_rollout_records(state_dir: Path) -> list[dict[str, Any]]:
    """The rollout records the transcript streamer has copied so far."""
    events_path = state_dir / "logs" / "codex_transcript" / "events.jsonl"
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text().splitlines()]


def _is_tool_call_naming(record: Mapping[str, Any], text: str) -> bool:
    """Whether a rollout record is a tool call whose record contains ``text``."""
    payload = record.get("payload") or {}
    return (
        record.get("type") == "response_item"
        and str(payload.get("type", "")).endswith("_call")
        and text in json.dumps(payload)
    )


@pytest.mark.release
@pytest.mark.harness_internals(harness="codex")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_codex_agent_reads_running_through_a_turn_a_repo_stop_hook_keeps_going(tmp_path: Path) -> None:
    """A turn whose end a repo's own Stop hook blocks (exit 2) reads RUNNING until codex's continuation ends.

    Also pins codex running the continuation inside the same task: the rollout's only task_started and
    task_complete name the turn, and the continuation's tool call lies between them.
    """
    profile = _CodexReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    ctx = profile.setup(tmp_path)
    blocked_marker = tmp_path / "turn-end-blocked"
    release = tmp_path / "continuation-release"
    feedback = (
        f"Before you finish, run exactly this shell command and wait for it to exit: "
        f"until [ -e {shlex.quote(str(release))} ]; do sleep 1; done. Then reply with just AGAIN."
    )
    hook_script = tmp_path / "block_first_turn_end.sh"
    hook_script.write_text(
        "cat > /dev/null\n"
        f"[ -e {shlex.quote(str(blocked_marker))} ] && exit 0\n"
        f"touch {shlex.quote(str(blocked_marker))}\n"
        f"echo {shlex.quote(feedback)} >&2\n"
        "exit 2\n"
    )
    hook = {"type": "command", "command": f"bash {shlex.quote(str(hook_script))}"}
    (ctx.project_dir / ".codex").mkdir()
    (ctx.project_dir / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [hook]}]}}))
    run_git_command(ctx.project_dir, "add", ".codex")
    run_git_command(ctx.project_dir, "commit", "-m", "Add a Stop hook that blocks the first turn end")
    agent_name = f"codex-stop-block-{get_short_random_string()}"
    with _created_codex_agent(profile, ctx, agent_name, *profile.create_extra_args(ctx)):
        state_dir = agent_state_dir(ctx.host_dir)
        send_expecting_success(profile, ctx, agent_name, "Reply with just OK.")
        assert poll_until(
            lambda: any(_is_tool_call_naming(record, str(release)) for record in _streamed_rollout_records(state_dir)),
            timeout=180,
            poll_interval=1,
        ), f"the continuation never ran its held command (the repo's Stop hook blocked: {blocked_marker.exists()})"
        active = state_dir / ACTIVE_MARKER_FILENAME
        marked_turn = active.read_text().strip() if active.exists() else None
        during_continuation = _published_agent(profile, ctx, agent_name)
        release.touch()
        _wait_for_turn_end(profile, ctx, agent_name, "COMPLETED")
        poll_until(lambda: not profile.is_reporting_running(ctx.host_dir), timeout=30, poll_interval=0.5)
        after_continuation = _published_agent(profile, ctx, agent_name)
        records = _streamed_rollout_records(state_dir)

    assert blocked_marker.exists(), "the repo's Stop hook never ran"
    assert during_continuation["state"] == "RUNNING", (
        f"{during_continuation['state']} while codex ran the continuation of a turn a Stop hook blocked; "
        f"{_TURN_MARKER_CONSUMER}"
    )
    assert after_continuation["state"] == "WAITING", f"{after_continuation['state']}; {_TURN_MARKER_CONSUMER}"
    task_records = [
        (index, record["payload"]["type"], record["payload"].get("turn_id"))
        for index, record in enumerate(records)
        if record.get("type") == "event_msg" and record["payload"].get("type") in ("task_started", "task_complete")
    ]
    held_calls = [index for index, record in enumerate(records) if _is_tool_call_naming(record, str(release))]
    assert [(kind, turn_id) for _, kind, turn_id in task_records] == [
        ("task_started", marked_turn),
        ("task_complete", marked_turn),
    ], f"{task_records}, `active` named {marked_turn!r}; {_TURN_MARKER_CONSUMER}"
    assert held_calls and task_records[0][0] < held_calls[0] < task_records[1][0], (
        f"the continuation's tool call at {held_calls} is outside the task {task_records}; {_TURN_MARKER_CONSUMER}"
    )


@pytest.mark.release
@pytest.mark.harness_internals(harness="codex")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_codex_agent_turn_end_never_outlives_its_turn_and_survives_a_compaction(tmp_path: Path) -> None:
    """A fast-failing turn never shows the previous turn's end once idle, and a compaction keeps the last turn's end.

    The last compaction starts the moment a turn ends, so its records reach the streamer in the same pass as that end.
    """
    profile = _CodexReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    ctx = profile.setup(tmp_path)
    agent_name = f"codex-turn-end-{get_short_random_string()}"
    daemon_states_seen: set[str] = set()
    stale_samples: list[str] = []
    with _created_codex_agent(profile, ctx, agent_name, *profile.create_extra_args(ctx)):
        send_expecting_success(profile, ctx, agent_name, "Reply with just OK.")
        _wait_for_turn_end(profile, ctx, agent_name, "COMPLETED")
        state_dir = agent_state_dir(ctx.host_dir)
        turn_end_path = state_dir / TURN_END_FILE_NAME
        completed_line = turn_end_path.read_text()

        with _root_thread_client(profile, ctx) as client:
            client.settings_update(model=f"gpt-unoffered-{get_short_random_string()}")
            client.read_thread_status()
            client.submit("Reply with just OK.", get_short_random_string())

            def is_failure_recorded() -> bool:
                daemon_state = client.read_thread_status().status_type
                line = turn_end_path.read_text() if turn_end_path.exists() else ""
                if "active" in daemon_states_seen and daemon_state != "active" and line == completed_line:
                    stale_samples.append(f"{daemon_state}: {line.strip()}")
                daemon_states_seen.add(daemon_state)
                return " kind:FAILED" in line

            poll_until(is_failure_recorded, timeout=60, poll_interval=0.02)
        failed = _wait_for_turn_end(profile, ctx, agent_name, "FAILED")

        with _root_thread_client(profile, ctx) as client:
            client.settings_update(model=_CODEX_MODEL)
            compaction, compaction_marks = _run_compaction(client, state_dir)
        _wait_for_streamed_task_complete(state_dir, compaction["id"])
        after_compaction = _published_turn_end(profile, ctx, agent_name)

        with _root_thread_client(profile, ctx) as client:
            client.submit("Reply with just OK.", get_short_random_string())
            assert poll_until(
                lambda: client.read_thread_status().status_type == "active", timeout=60, poll_interval=0.01
            )
            assert poll_until(
                lambda: client.read_thread_status().status_type != "active", timeout=180, poll_interval=0.01
            )
            turn_then_compaction, turn_then_compaction_marks = _run_compaction(client, state_dir)
        _wait_for_streamed_task_complete(state_dir, turn_then_compaction["id"])
        after_turn_then_compaction = _published_turn_end(profile, ctx, agent_name)

    assert "active" in daemon_states_seen, f"never saw the failing turn run: {daemon_states_seen}"
    assert stale_samples == [], (
        f"the previous turn's end outlived the next turn: {stale_samples} "
        "(consumer: record_session_pointers.sh, which removes turn_end at codex's UserPromptSubmit)"
    )
    compaction_consumer = (
        "(consumer: stream_transcript.sh's _record_turn_end, which skips a task whose task_started lacks the "
        "turn_attribution the rollout's other task_started records carry)"
    )
    assert compaction.get("status") == "completed", compaction
    compaction_marker_consumer = (
        "(consumer: stream_transcript.sh's _replay_turn_markers, which writes a task_started's turn_id into `active` "
        "when its started_at is at or after MNGR_CODEX_LAUNCHED_AT)"
    )
    assert compaction["id"] in compaction_marks, f"{compaction_marks} {compaction_marker_consumer}"
    assert turn_then_compaction["id"] in turn_then_compaction_marks, (
        f"{turn_then_compaction_marks} {compaction_marker_consumer}"
    )
    assert after_compaction == failed, (
        f"a compaction replaced {failed!r} with {after_compaction!r} {compaction_consumer}"
    )
    assert turn_then_compaction.get("status") == "completed", turn_then_compaction
    assert after_turn_then_compaction.get("kind") == "COMPLETED", (
        f"a compaction started as a turn ended left {after_turn_then_compaction!r}, not that turn's COMPLETED "
        f"{compaction_consumer}"
    )


_BACKGROUND_TASK_CONSUMER: Final[str] = (
    "record_background_tasks.py, which reads codex's thread/loaded/list, thread/read (status, parentThreadId, "
    "threadSource) and thread/backgroundTerminals/list (itemId)"
)


def _wait_for_state(
    profile: _CodexReleaseProfile, ctx: AgentReleaseContext, agent_name: str, state: str, timeout: float
) -> tuple[dict[str, Any], list[str]]:
    """Poll until the published agent reads ``state``; return it, and every state read on the way."""
    states_read: list[str] = []

    def _agent_in_state() -> dict[str, Any] | None:
        agent = _published_agent(profile, ctx, agent_name)
        states_read.append(agent["state"])
        return agent if agent["state"] == state else None

    agent, _, _ = poll_for_value(_agent_in_state, timeout, poll_interval=1)
    assert agent is not None, f"mngr list never published {state}, only {states_read}; {_BACKGROUND_TASK_CONSUMER}"
    return agent, states_read


def _states_once_the_turn_opened(states_read: Sequence[str]) -> list[str]:
    return list(states_read[states_read.index("RUNNING") :]) if "RUNNING" in states_read else []


def _kinds_and_native_types(agent: dict[str, Any]) -> list[tuple[str, str]]:
    return [(task["kind"], task["native_type"]) for task in agent["background_tasks"]]


@pytest.mark.release
@pytest.mark.harness_internals(harness="codex")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_codex_agent_reads_running_background_while_a_terminal_or_a_subagent_outlives_its_turn(
    tmp_path: Path,
) -> None:
    """A terminal, then a subagent, left running past a turn read RUNNING_BACKGROUND from the turn's end until done."""
    profile = _CodexReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    ctx = profile.setup(tmp_path)
    agent_name = f"codex-background-{get_short_random_string()}"
    with _created_codex_agent(profile, ctx, agent_name, *profile.create_extra_args(ctx)):
        send_expecting_success(
            profile,
            ctx,
            agent_name,
            "Use your shell tool to start the command 'sleep 45' with a yield time of about 1 second so that it keeps "
            "running in the background, then end your turn right away with a one-line reply. Do not wait for it, "
            "poll it or stop it.",
        )
        terminal, states_to_terminal = _wait_for_state(profile, ctx, agent_name, "RUNNING_BACKGROUND", timeout=180)
        after_terminal, _ = _wait_for_state(profile, ctx, agent_name, "WAITING", timeout=120)

        send_expecting_success(
            profile,
            ctx,
            agent_name,
            "Use spawn_agent to start one subagent whose task is: run the shell command 'sleep 45' and wait for it to "
            "finish, then reply done. As soon as it is spawned, end your own turn with a one-line reply. Do not wait "
            "for the subagent and do not check on it.",
        )
        subagent, states_to_subagent = _wait_for_state(profile, ctx, agent_name, "RUNNING_BACKGROUND", timeout=180)
        after_subagent, _ = _wait_for_state(profile, ctx, agent_name, "WAITING", timeout=180)

    assert "WAITING" not in _states_once_the_turn_opened(states_to_terminal), (
        f"{states_to_terminal}; {_BACKGROUND_TASK_CONSUMER}"
    )
    assert "WAITING" not in _states_once_the_turn_opened(states_to_subagent), (
        f"{states_to_subagent}; {_BACKGROUND_TASK_CONSUMER}"
    )
    assert _kinds_and_native_types(terminal) == [("SHELL", "backgroundTerminals")], (
        f"{terminal['background_tasks']}; {_BACKGROUND_TASK_CONSUMER}"
    )
    assert _kinds_and_native_types(subagent) == [("SUBAGENT", "subagent")], (
        f"{subagent['background_tasks']}; {_BACKGROUND_TASK_CONSUMER}"
    )
    assert after_terminal["background_tasks"] == [] and after_subagent["background_tasks"] == []


def _run_compaction(client: CodexAppServerClient, state_dir: Path) -> tuple[dict[str, Any], set[str]]:
    """Start a compaction of the root thread; return its turn once it has ended, and the turns `active` named meanwhile."""
    turn_count = len(client.thread_read(include_turns=True)["thread"]["turns"])
    marked_turns: set[str] = set()
    client.thread_compact_start()

    def is_compaction_over() -> bool:
        with suppress(FileNotFoundError):
            marked_turns.add((state_dir / ACTIVE_MARKER_FILENAME).read_text().strip())
        turns = client.thread_read(include_turns=True)["thread"]["turns"]
        return len(turns) > turn_count and turns[-1].get("status") != "inProgress"

    assert poll_until(is_compaction_over, timeout=180, poll_interval=0.1)
    return client.thread_read(include_turns=True)["thread"]["turns"][-1], marked_turns


def _wait_for_streamed_task_complete(state_dir: Path, turn_id: str) -> None:
    events_path = state_dir / "logs" / "codex_transcript" / "events.jsonl"
    assert poll_until(
        lambda: any(
            '"type":"task_complete"' in line and turn_id in line for line in events_path.read_text().splitlines()
        ),
        timeout=60,
        poll_interval=1,
    )


_DIALOG_MARKER_CONSUMER: Final[str] = (
    "codex_turn_hook.sh, which touches `permissions_waiting` at PermissionRequest and at a PreToolUse whose "
    "tool_name is request_user_input, and removes it at PostToolUse"
)
_TURN_START_REQUEST_ID: Final[int] = 1_000_000


@pytest.mark.release
@pytest.mark.harness_internals(harness="codex")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_codex_agent_waits_on_a_command_approval_until_it_is_approved(tmp_path: Path) -> None:
    """A command codex asks to approve reads WAITING with reason PERMISSIONS until it is approved."""
    profile = _CodexReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    ctx = profile.setup(tmp_path)
    agent_name = f"codex-approval-{get_short_random_string()}"
    with _created_codex_agent(profile, ctx, agent_name, *profile.create_extra_args(ctx)):
        while_asked, is_cleared_by_the_answer, after_turn = _answer_the_dialog_of_a_turn(
            profile,
            ctx,
            agent_name,
            {
                "input": [{"type": "text", "text": "Run `mkdir -p approval-probe` in the shell, then reply DONE."}],
                "approvalPolicy": "untrusted",
            },
            "item/commandExecution/requestApproval",
            lambda params: {"decision": "accept"},
        )

    _assert_the_dialog_is_read_until_answered(while_asked, is_cleared_by_the_answer, after_turn)


@pytest.mark.release
@pytest.mark.harness_internals(harness="codex")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_codex_agent_waits_on_a_question_until_it_is_answered(tmp_path: Path) -> None:
    """A question codex asks in plan mode reads WAITING with reason PERMISSIONS until it is answered."""
    profile = _CodexReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)
    ctx = profile.setup(tmp_path)
    agent_name = f"codex-question-{get_short_random_string()}"
    prompt = (
        "Before planning anything, use your request_user_input tool to ask me one question: which color I "
        "prefer, red or blue. Then reply with just the color I chose."
    )
    with _created_codex_agent(profile, ctx, agent_name, *profile.create_extra_args(ctx)):
        while_asked, is_cleared_by_the_answer, after_turn = _answer_the_dialog_of_a_turn(
            profile,
            ctx,
            agent_name,
            {
                "input": [{"type": "text", "text": prompt}],
                "collaborationMode": {
                    "mode": "plan",
                    "settings": {"model": _CODEX_MODEL, "developer_instructions": None},
                },
            },
            "item/tool/requestUserInput",
            lambda params: {"answers": {question["id"]: {"answers": ["Blue"]} for question in params["questions"]}},
        )

    _assert_the_dialog_is_read_until_answered(while_asked, is_cleared_by_the_answer, after_turn)


def _answer_the_dialog_of_a_turn(
    profile: _CodexReleaseProfile,
    ctx: AgentReleaseContext,
    agent_name: str,
    turn_params: Mapping[str, Any],
    dialog_method: str,
    answer: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    """Start a root-thread turn with ``turn_params`` and answer the ``dialog_method`` request codex sends for it.

    Returns the agent ``mngr list`` publishes while the request waits, whether `permissions_waiting` went away
    while `active` still marked the turn, and the agent ``mngr list`` publishes once the turn's end reached `active`.
    """
    state_dir = agent_state_dir(ctx.host_dir)
    thread_id = (state_dir / APP_SERVER_THREAD_FILENAME).read_text().strip()
    cleared_while_active: list[bool] = []

    def observe_dialog_marker() -> None:
        if not cleared_while_active and not (state_dir / PERMISSIONS_WAITING_FILENAME).exists():
            cleared_while_active.append((state_dir / ACTIVE_MARKER_FILENAME).exists())

    def is_on_root_thread(frame: Mapping[str, Any]) -> bool:
        return (frame.get("params") or {}).get("threadId") == thread_id

    client = CodexAppServerClient(
        transport=connect_app_server_transport(get_codex_app_server_socket_path(get_codex_home(state_dir)))
    )
    try:
        client.initialize("mngr-codex-release-test", "0")
        # codex sends a turn's dialog requests to the connections that resumed its thread.
        client.thread_resume(thread_id)
        turn_start = {"threadId": thread_id, **turn_params}
        client.transport.send(
            json.dumps({"jsonrpc": "2.0", "id": _TURN_START_REQUEST_ID, "method": "turn/start", "params": turn_start})
        )
        dialog = _await_frame(
            client,
            lambda frame: frame.get("method") == dialog_method and "id" in frame and is_on_root_thread(frame),
            observe=lambda: None,
        )
        while_asked = _published_agent(profile, ctx, agent_name)
        client.transport.send(json.dumps({"jsonrpc": "2.0", "id": dialog["id"], "result": answer(dialog["params"])}))
        _await_frame(
            client,
            lambda frame: frame.get("method") == "turn/completed" and is_on_root_thread(frame),
            observe=observe_dialog_marker,
        )
    finally:
        client.close()
    poll_until(lambda: not (state_dir / ACTIVE_MARKER_FILENAME).exists(), timeout=30, poll_interval=0.2)
    return while_asked, cleared_while_active == [True], _published_agent(profile, ctx, agent_name)


def _await_frame(
    client: CodexAppServerClient, is_wanted: Callable[[Mapping[str, Any]], bool], observe: Callable[[], None]
) -> dict[str, Any]:
    """The first frame from the daemon that ``is_wanted`` accepts, calling ``observe`` before each read."""
    wanted: list[dict[str, Any]] = []

    def is_seen() -> bool:
        observe()
        with suppress(TimeoutError):
            while not wanted:
                frame = json.loads(client.transport.receive(0.0))
                if is_wanted(frame):
                    wanted.append(frame)
        return bool(wanted)

    assert poll_until(is_seen, timeout=180, poll_interval=0.02), "codex never sent the awaited frame"
    return wanted[0]


def _assert_the_dialog_is_read_until_answered(
    while_asked: Mapping[str, Any], is_cleared_by_the_answer: bool, after_turn: Mapping[str, Any]
) -> None:
    def reading(agent: Mapping[str, Any]) -> tuple[str, str | None]:
        return agent["state"], agent["plugin"].get("codex", {}).get("waiting_reason")

    assert reading(while_asked) == ("WAITING", "PERMISSIONS"), f"{reading(while_asked)}; {_DIALOG_MARKER_CONSUMER}"
    assert is_cleared_by_the_answer, f"`permissions_waiting` outlived the answer; {_DIALOG_MARKER_CONSUMER}"
    assert reading(after_turn) == ("WAITING", "END_OF_TURN"), f"{reading(after_turn)}; {_TURN_MARKER_CONSUMER}"

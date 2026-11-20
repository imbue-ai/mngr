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
import shutil
import subprocess
from collections.abc import Generator
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import AgentReleaseProfile
from imbue.mngr.agents.agent_release_testing import agent_state_dir
from imbue.mngr.agents.agent_release_testing import is_step_from
from imbue.mngr.agents.agent_release_testing import run_agent_release_lifecycle
from imbue.mngr.agents.agent_release_testing import run_message_delivery_journey
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.agents.agent_release_testing import wait_for_records
from imbue.mngr.hosts.background_tasks import TURN_END_FILE_NAME
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.app_server_client import connect_app_server_transport
from imbue.mngr_codex.codex_config import APP_SERVER_THREAD_FILENAME
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

    # codex has no ``active`` marker: RUNNING vs WAITING is read live from the daemon's
    # ``thread/status``. A turn in flight is ``active``; idle (or no daemon yet) is WAITING.
    def is_waiting_after_create(self, host_dir: Path) -> bool:
        return self._thread_status_type(host_dir) != "active"

    def is_reporting_running(self, host_dir: Path) -> bool:
        return self._thread_status_type(host_dir) == "active"

    def _thread_status_type(self, host_dir: Path) -> str | None:
        """The bound root thread's live status tag, or ``None`` when the daemon is unreachable.

        Mirrors the plugin's send-path bind (persisted root id, else the single loaded thread) and
        reads ``thread/status`` -- the same source the live lifecycle uses -- over a short-lived
        connection. Any failure (no socket yet, no bindable thread) reads as "not active".
        """
        agent_dirs = [path for path in (host_dir / "agents").glob("*") if path.is_dir()]
        if len(agent_dirs) != 1:
            return None
        socket_path = get_codex_app_server_socket_path(get_codex_home(agent_dirs[0]))
        if not socket_path.exists():
            return None
        try:
            client = CodexAppServerClient(transport=connect_app_server_transport(socket_path))
        except OSError:
            return None
        try:
            client.initialize("mngr-codex-release-test", "0")
            if not self._bind_root_thread(client, agent_dirs[0]):
                return None
            return client.read_thread_status().status_type
        except (CodexAppServerError, OSError):
            return None
        finally:
            client.close()

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


def _published_turn_end(profile: _CodexReleaseProfile, ctx: AgentReleaseContext, agent_name: str) -> dict[str, Any]:
    listing = profile.run_mngr(ctx, "list", "--provider", "local", "--format", "json", timeout=120)
    assert listing.returncode == 0, f"mngr list failed:\n{listing.stdout}\n{listing.stderr}"
    (agent,) = [agent for agent in json.loads(listing.stdout)["agents"] if agent["name"] == agent_name]
    return agent.get("turn_end") or {}


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


def _interrupt_running_turn(profile: _CodexReleaseProfile, ctx: AgentReleaseContext) -> None:
    """Send ``turn/interrupt`` to the daemon for the root thread's turn in progress."""
    with _root_thread_client(profile, ctx) as client:
        turns = client.thread_read(include_turns=True)["thread"]["turns"]
        (turn_id,) = [turn["id"] for turn in turns if turn.get("status") == "inProgress"]
        client.interrupt(turn_id)


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
        assert _published_turn_end(profile, ctx, agent_name) == {}
        _interrupt_running_turn(profile, ctx)
        interrupted = _wait_for_turn_end(profile, ctx, agent_name, "INTERRUPTED")

    assert completed["native_type"] == ""
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

    assert failed["native_type"], f"{failed!r} names no codex_error_info; {_TURN_END_CONSUMER}"


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
            compaction = _run_compaction(client)
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
            turn_then_compaction = _run_compaction(client)
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
    assert after_compaction == failed, (
        f"a compaction replaced {failed!r} with {after_compaction!r} {compaction_consumer}"
    )
    assert turn_then_compaction.get("status") == "completed", turn_then_compaction
    assert after_turn_then_compaction.get("kind") == "COMPLETED", (
        f"a compaction started as a turn ended left {after_turn_then_compaction!r}, not that turn's COMPLETED "
        f"{compaction_consumer}"
    )


def _run_compaction(client: CodexAppServerClient) -> dict[str, Any]:
    """Start a compaction of the root thread and return its turn once it has ended."""
    turn_count = len(client.thread_read(include_turns=True)["thread"]["turns"])
    client.thread_compact_start()

    def is_compaction_over() -> bool:
        turns = client.thread_read(include_turns=True)["thread"]["turns"]
        return len(turns) > turn_count and turns[-1].get("status") != "inProgress"

    assert poll_until(is_compaction_over, timeout=180, poll_interval=1)
    return client.thread_read(include_turns=True)["thread"]["turns"][-1]


def _wait_for_streamed_task_complete(state_dir: Path, turn_id: str) -> None:
    events_path = state_dir / "logs" / "codex_transcript" / "events.jsonl"
    assert poll_until(
        lambda: any(
            '"type":"task_complete"' in line and turn_id in line for line in events_path.read_text().splitlines()
        ),
        timeout=60,
        poll_interval=1,
    )

"""Release test: full end-to-end lifecycle of a real mngr-managed claude agent.

Drives the real ``mngr`` CLI against the real ``claude`` binary and a real model
through the shared agent release lifecycle (create -> WAITING -> message -> RUNNING
-> transcript -> stop/start resume -> destroy -> adopt-from-preserved -> recall).

Requires ``claude`` on PATH and ``ANTHROPIC_API_KEY`` in the environment; skipped
otherwise. Release-marked, so it does not run per PR.
"""

from __future__ import annotations

import os
import pwd
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import count_user_steps_containing
from imbue.mngr.agents.agent_release_testing import is_step_from
from imbue.mngr.agents.agent_release_testing import read_common_records
from imbue.mngr.agents.agent_release_testing import run_agent_release_lifecycle
from imbue.mngr.agents.agent_release_testing import run_concurrent_message_delivery
from imbue.mngr.agents.agent_release_testing import run_message_delivery_journey
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.agents.agent_release_testing import wait_for_user_message
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr_claude.dialogs import classify
from imbue.mngr_claude.plugin import has_input_prompt_line
from imbue.mngr_claude.testing import AGENT_DESTROY_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import RELEASE_TEST_MODEL
from imbue.mngr_claude.testing import SESSION_RECORD_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import TURN_PROGRESS_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import append_project_settings
from imbue.mngr_claude.testing import capture_agent_pane
from imbue.mngr_claude.testing import capture_agent_pane_with_scrollback
from imbue.mngr_claude.testing import commit_project_claude_settings
from imbue.mngr_claude.testing import created_agent
from imbue.mngr_claude.testing import drift_note
from imbue.mngr_claude.testing import held_tool_command
from imbue.mngr_claude.testing import is_queue_operation_carrying
from imbue.mngr_claude.testing import is_stop_hook_summary
from imbue.mngr_claude.testing import read_session_records
from imbue.mngr_claude.testing import session_message_text
from imbue.mngr_claude.testing import setup_ctx_that_answers_dialogs
from imbue.mngr_claude.testing import start_held_tool_call
from imbue.mngr_claude.testing import wait_for_agent_pane
from imbue.mngr_claude.testing import wait_for_agent_prompt
from imbue.mngr_claude.testing import wait_for_agent_to_stop_running
from imbue.mngr_claude.testing import wait_for_native_session_file
from imbue.mngr_claude.testing import wait_for_session_record


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_agent_full_lifecycle(tmp_path: Path) -> None:
    """Drive a real claude agent through the full shared release arc and assert it behaves.

    Runs create -> WAITING -> message -> RUNNING -> transcript -> stop/start resume ->
    destroy -> adopt-from-preserved -> recall against the real ``claude`` binary and a real
    (haiku) model. The load-bearing checks (in ``run_agent_release_lifecycle``) fail unless
    claude genuinely ran: it must reach WAITING, flip the RUNNING marker on a forced bash
    tool call, report token usage, and -- after the agent is destroyed -- a fresh agent in a
    new worktree that adopts the preserved session JSONL must recall the pre-destroy secret,
    proving the native session store resumes and cross-cwd re-filing works. A no-op or broken
    lifecycle (agent never runs, marker never flips, or adoption fails to resume) fails these
    assertions rather than passing silently.
    """
    run_agent_release_lifecycle(ClaudeReleaseProfile(), tmp_path)


@pytest.mark.witnesses(
    "message-delivery.stays-ready",
    partial="witnesses the ordinary/queued/slash instances: each of a sequence of sends is confirmed, so the agent stayed ready for the next; does not cover shell-mode (`!`) messages",
)
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.flaky
@pytest.mark.timeout(1500)
def test_claude_message_delivery_journey(tmp_path: Path) -> None:
    """Drive the evidence-confirmed send pipeline through its racey delivery scenarios.

    One real claude agent (haiku) walks idle delivery -> send-while-busy (queued
    input) -> rapid sequential sends -> a long buffer-pasted message -> /clear
    under the relaxed policy. Every ``mngr message`` exit 0 is load-bearing:
    strict confirmation succeeds only once the message's own content appears in
    claude's durable transcript (enqueue or user record), and the exactly-once
    assertions prove the pane-gated Enter retries never duplicate a message.
    """
    run_message_delivery_journey(ClaudeReleaseProfile(), tmp_path)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_concurrent_message_delivery(tmp_path: Path) -> None:
    """Two claude agents on one tmux server, messaged concurrently.

    Both sends must confirm and each message must land exactly once on its own
    agent -- concurrent sends must never cross-confirm against each other's
    submission evidence (the historical failure mode was exactly this kind of
    cross-talk, via latched tmux wait-for signals on a shared server).
    """
    run_concurrent_message_delivery(ClaudeReleaseProfile(), tmp_path)


# Bare ``/model`` opens Claude Code's interactive model picker: a numbered selector (a rule line
# followed by indented options, one highlighted with ``❯``) that blocks until a choice is made.
# The highlighted default is the agent's current model, so accepting it (Enter) is a benign no-op
# that just closes the picker.
_MODEL_PICKER_COMMAND: Final[str] = "/model"


@pytest.mark.witnesses(
    "message-delivery.stays-ready",
    partial="witnesses the `/model` (blocking-selector) instance: a normal message is still delivered after /model",
)
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_model_picker_does_not_leave_agent_stuck(tmp_path: Path) -> None:
    """Sending ``/model`` opens a blocking selector, which must not leave the agent stuck.

    Bare ``/model`` opens Claude Code's interactive model picker -- a numbered selector that blocks
    the input until a choice is made, and the real-world manifestation of the bug the dialog
    hardening addresses (a ``/model`` prompt silently blocking the client). Against a real haiku
    agent this asserts the user-facing outcome: the send exits 0, the picker is gone afterward (no
    blocking selector remains in the pane -- ``classify`` returns None), and
    a subsequent normal message is still delivered and processed. If the picker had wedged the
    agent, the follow-up message would never reach the transcript.

    Note on scope: mngr's send-confirmation retry already clears an Enter-dismissable selector like
    the picker, so this exercises the end-to-end "``/model`` does not wedge the agent" outcome
    rather than the dialog-clearing path specifically.
    """
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = setup_ctx_that_answers_dialogs(profile, tmp_path)
    agent_name = f"claude-modelpicker-{get_short_random_string()}"
    run_id = get_short_random_string()
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        send_expecting_success(profile, ctx, agent_name, _MODEL_PICKER_COMMAND)
        pane = capture_agent_pane(ctx, agent_name)
        # `classify` is what the send path itself uses to decide whether anything holds the
        # input, so asserting through it tests the same judgement the product makes.
        assert classify(pane) is None, (
            f"the /model picker was not dismissed; something still holds the pane's input:\n{pane}"
        )
        # The agent must still accept and process a normal message -- i.e. it is not wedged on the picker.
        token = f"AFTERMODEL-{run_id}"
        send_expecting_success(profile, ctx, agent_name, f"Remember this exact value: {token}. Reply with just OK.")
        wait_for_user_message(
            ctx.host_dir,
            profile.common_transcript_subdir,
            token,
            description="message sent after /model was not processed -- the agent may be stuck on the picker",
        )


# Transcript record contract

# The values Claude Code writes for a message absorbed into a running turn.
_ABSORBED_MID_TURN_REASON: Final[str] = "absorbed_mid_turn"
_QUEUED_COMMAND_ATTACHMENT_TYPE: Final[str] = "queued_command"
_QUEUED_PROMPT_MODE: Final[str] = "prompt"
# The queued_command origin kinds of a message the user typed; None is an older claude's missing origin.
_USER_ORIGIN_KINDS: Final[tuple[str | None, ...]] = (None, "human", "auto-continuation")

# The queue-operation (operation, reason) pairs mngr has decided about.
_KNOWN_QUEUE_OPERATIONS: Final[frozenset[tuple[str, str | None]]] = frozenset(
    {
        ("enqueue", None),
        ("dequeue", None),
        ("remove", None),
        ("remove", _ABSORBED_MID_TURN_REASON),
        # A background-task notification claude delivers itself; mngr leaves it out of the transcript.
        ("remove", "delivered_to_agent"),
        ("popAll", None),
    }
)


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_transcript_record_contract(tmp_path: Path) -> None:
    """Pin the native-transcript record shapes mngr consumes, against a live claude.

    Reads the RAW session JSONL with plain json parsing -- deliberately not
    through mngr's probes -- so when a Claude Code release reshapes a record,
    the failure names the drifted record and the mngr consumer to update,
    stamped with the claude version that broke it. Lenient to additions:
    only the fields mngr reads are asserted.
    """
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"claude-contract-{get_short_random_string()}"
    run_id = get_short_random_string()
    drift = drift_note(ctx, "the record shape")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        # An idle prompt lands as {"type":"user","message":{"content":...}}.
        idle_token = f"contract-idle-{run_id}"
        send_expecting_success(
            profile, ctx, agent_name, f"Remember this exact value: {idle_token}. Reply with just OK."
        )

        # The native session JSONL is <config-dir>/projects/<encoded-cwd>/<session-id>.jsonl,
        # named by the claude_session_id marker.
        session_file = wait_for_native_session_file(ctx)
        user_record = wait_for_session_record(
            session_file,
            lambda r: r.get("type") == "user" and idle_token in session_message_text(r),
            failure=f"no user record with message.content carrying the sent text {drift}; "
            "consumers: content probes, common-transcript converter",
        )
        # Records carry a top-level uuid (raw-streamer offset reconciliation).
        assert isinstance(user_record.get("uuid"), str) and user_record["uuid"] != "", (
            f"user record lost its top-level uuid {drift}; consumer: stream_transcript.sh offset reconciliation"
        )

        # The reply lands as {"type":"assistant","message":{"content":...}}.
        wait_for_session_record(
            session_file,
            lambda r: r.get("type") == "assistant" and session_message_text(r) != "",
            failure=f"no assistant record with message.content {drift}; "
            "consumers: common-transcript converter, transcript readers",
        )

        # A turn's Stop hook pass lands as
        # {"type":"system","subtype":"stop_hook_summary"} (mngr always installs Stop hooks).
        wait_for_session_record(
            session_file,
            is_stop_hook_summary,
            failure=f"no system/stop_hook_summary record after a completed turn {drift}; "
            "consumers: wait_for_stop_hook.sh queued-prompt check, wait_for_stop_hook.sh has_blocked_stop_hook",
        )

        # An unknown slash command lands as
        # {"type":"system","level":"warning","content":"Unknown command: ..."}.
        typo = f"/mngr-invalid-command-{run_id}"
        send_expecting_success(profile, ctx, agent_name, typo)
        wait_for_session_record(
            session_file,
            lambda r: r.get("type") == "system"
            and r.get("level") == "warning"
            and str(r.get("content", "")).startswith("Unknown command")
            and typo in str(r.get("content", "")),
            failure=f"no system/warning 'Unknown command' record for {typo!r} {drift}; "
            "consumer: _REJECTED_COMMAND_JQ_FILTER (rejection probe)",
        )


_STOP_HOOK_FEEDBACK_PREFIX: Final[str] = "Stop hook feedback:"


def _is_stop_hook_feedback(record: dict[str, Any]) -> bool:
    """Whether a raw session record is the isMeta user record carrying a blocking Stop hook's feedback."""
    return (
        record.get("type") == "user"
        and record.get("isMeta") is True
        and str((record.get("message") or {}).get("content")).startswith(_STOP_HOOK_FEEDBACK_PREFIX)
    )


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_stop_hook_block_keeps_agent_running_through_its_continuation(tmp_path: Path) -> None:
    """A turn end that another Stop hook blocks leaves the agent RUNNING until the continuation ends.

    Also pins the records wait_for_stop_hook.sh reads to tell a blocked turn end from a finished one.
    """
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    blocked_marker = tmp_path / "turn-end-blocked"
    continuation_marker = tmp_path / "continuation-running"
    continuation_release = tmp_path / "continuation-release"
    feedback = (
        "Before you stop, use the Bash tool to run exactly: "
        f"{held_tool_command(continuation_marker, continuation_release)}. Then reply with just DONE."
    )
    hook_relpath = ".claude/block_first_turn_end.sh"
    (ctx.project_dir / ".claude").mkdir()
    (ctx.project_dir / hook_relpath).write_text(
        "cat > /dev/null\n"
        f"[ -e {shlex.quote(str(blocked_marker))} ] && exit 0\n"
        f"touch {shlex.quote(str(blocked_marker))}\n"
        f"echo {shlex.quote(feedback)} >&2\n"
        "exit 2\n"
    )
    hook_command = f'bash "$CLAUDE_PROJECT_DIR/{hook_relpath}"'
    commit_project_claude_settings(
        ctx.project_dir,
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": hook_command}]}]}},
        "Add a Stop hook that blocks the first turn end",
    )
    agent_name = f"claude-stopblock-{get_short_random_string()}"
    drift = drift_note(ctx, "the stop hook records")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        send_expecting_success(profile, ctx, agent_name, "Reply with just OK.")
        assert poll_until(continuation_marker.exists, timeout=TURN_PROGRESS_TIMEOUT_SECONDS, poll_interval=1.0), (
            f"the blocked turn end never continued into the hook's tool call (the hook blocked: {blocked_marker.exists()})"
        )
        is_running_as_continuation_starts = profile.is_reporting_running(ctx.host_dir)

        session_file = wait_for_native_session_file(ctx)
        feedback_record = wait_for_session_record(
            session_file,
            _is_stop_hook_feedback,
            failure=f"the blocked turn end wrote no isMeta user record whose string content starts with "
            f"{_STOP_HOOK_FEEDBACK_PREFIX!r} {drift}; consumer: wait_for_stop_hook.sh has_blocked_stop_hook",
        )

        def is_feedback_followed_by_stop_hook_summary() -> bool:
            records = read_session_records(session_file)
            return any(is_stop_hook_summary(record) for record in records[records.index(feedback_record) + 1 :])

        assert poll_until(
            is_feedback_followed_by_stop_hook_summary, timeout=SESSION_RECORD_TIMEOUT_SECONDS, poll_interval=1.0
        ), (
            f"no stop_hook_summary follows the block's feedback record {drift}; "
            "consumer: wait_for_stop_hook.sh has_blocked_stop_hook, which reads the later summary as the turn end"
        )
        assert is_running_as_continuation_starts and profile.is_reporting_running(ctx.host_dir), (
            "the agent reported WAITING while the continuation of a blocked turn end was running; "
            "consumer: wait_for_stop_hook.sh has_blocked_stop_hook"
        )
        continuation_release.touch()
        wait_for_agent_to_stop_running(
            profile,
            ctx,
            timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
            failure="the agent still reports RUNNING after the continuation ended, so the unblocked turn end read as "
            "blocked; consumer: wait_for_stop_hook.sh has_blocked_stop_hook",
        )


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_mid_turn_message_lands_once_in_the_common_transcript(tmp_path: Path) -> None:
    """A message sent while a tool call runs reaches the common transcript as exactly one user step.

    Claude Code writes no user line for such a message, so this also pins the queue removal and
    ``queued_command`` attachment common_transcript_convert reads instead. It also pins the
    (operation, reason) pairs of the session's queue-operation records.
    """
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    tool_marker = tmp_path / "mid-turn-tool-running"
    agent_name = f"claude-midturn-{get_short_random_string()}"
    token = f"MIDTURN-{get_short_random_string()}"
    drift = drift_note(ctx, "the mid-turn message records")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        tool_release = start_held_tool_call(profile, ctx, agent_name, tool_marker)
        send_expecting_success(
            profile, ctx, agent_name, f"Also remember this exact value: {token}. Reply with just OK."
        )

        session_file = wait_for_native_session_file(ctx)
        wait_for_session_record(
            session_file,
            lambda r: is_queue_operation_carrying(r, "enqueue", token),
            failure=f"the message sent during a tool call wrote no queue-operation/enqueue record carrying it "
            f"{drift}; consumers: busy-send accept evidence, content probes",
        )
        tool_release.touch()
        removal = wait_for_session_record(
            session_file,
            lambda r: is_queue_operation_carrying(r, "remove", token),
            failure=f"the message sent during a tool call was never removed from the queue into the running turn "
            f"{drift}; consumers: common_transcript_convert mid-turn messages, release-test delivery counting",
        )
        command_uuid = removal.get("commandUuid")
        assert (
            removal.get("reason") == _ABSORBED_MID_TURN_REASON and isinstance(command_uuid, str) and command_uuid != ""
        ), (
            f"the mid-turn removal has reason={removal.get('reason')!r} and commandUuid={command_uuid!r}, not "
            f"reason {_ABSORBED_MID_TURN_REASON!r} with a commandUuid {drift}; "
            "consumer: common_transcript_convert._is_absorbed_mid_turn"
        )
        assert isinstance(removal.get("timestamp"), str) and removal["timestamp"] != "", (
            f"the {_ABSORBED_MID_TURN_REASON} removal has no timestamp {drift}; "
            "consumers: common_transcript_convert._is_absorbed_mid_turn, _mid_turn_user_record"
        )
        attachment_record = wait_for_session_record(
            session_file,
            lambda r: r.get("type") == "attachment"
            and isinstance(r.get("attachment"), dict)
            and r["attachment"].get("type") == _QUEUED_COMMAND_ATTACHMENT_TYPE
            and r["attachment"].get("source_uuid") == command_uuid,
            failure=f"no {_QUEUED_COMMAND_ATTACHMENT_TYPE} attachment has the removal's commandUuid as its source_uuid "
            f"{drift}; consumer: common_transcript_convert._mid_turn_user_record",
        )
        assert isinstance(attachment_record.get("uuid"), str) and attachment_record["uuid"] != "", (
            f"the {_QUEUED_COMMAND_ATTACHMENT_TYPE} attachment record has no top-level uuid {drift}; "
            "consumer: common_transcript_convert._mid_turn_user_record"
        )
        attachment = attachment_record["attachment"]
        assert attachment.get("commandMode") == _QUEUED_PROMPT_MODE and token in str(attachment.get("prompt")), (
            f"the {_QUEUED_COMMAND_ATTACHMENT_TYPE} attachment has commandMode={attachment.get('commandMode')!r} and "
            f"does not carry the message as its prompt {drift}; consumer: common_transcript_convert._mid_turn_user_record"
        )
        origin = attachment.get("origin")
        origin_kind = origin.get("kind") if isinstance(origin, dict) else None
        assert not attachment.get("isMeta") and origin_kind in _USER_ORIGIN_KINDS, (
            f"the {_QUEUED_COMMAND_ATTACHMENT_TYPE} attachment has isMeta={attachment.get('isMeta')!r} and origin kind "
            f"{origin_kind!r}, not the marks of a message the user typed (no isMeta, an origin kind in "
            f"{_USER_ORIGIN_KINDS}) {drift}; "
            "consumer: common_transcript_convert._mid_turn_user_record, which makes any other prompt a system step"
        )
        records = read_session_records(session_file)
        assert records.index(removal) < records.index(attachment_record), (
            f"the {_QUEUED_COMMAND_ATTACHMENT_TYPE} attachment is written before its {_ABSORBED_MID_TURN_REASON} "
            f"removal {drift}; "
            "consumer: common_transcript_convert._mid_turn_user_record, which needs the removal read first"
        )

        wait_for_user_message(
            ctx.host_dir,
            profile.common_transcript_subdir,
            token,
            description="the mid-turn message never reached the common transcript as a user step",
        )
        wait_for_agent_to_stop_running(
            profile,
            ctx,
            timeout=TURN_PROGRESS_TIMEOUT_SECONDS,
            failure=f"the turn that absorbed the mid-turn message never ended {drift}; consumer: wait_for_stop_hook.sh",
        )
        user_steps = count_user_steps_containing(
            read_common_records(ctx.host_dir, profile.common_transcript_subdir), token
        )
        assert user_steps == 1, f"the mid-turn message reached the common transcript {user_steps} times, not once"
        seen_operations = {
            (str(r.get("operation")), r.get("reason"))
            for r in read_session_records(session_file)
            if r.get("type") == "queue-operation"
        }
        unknown_operations = seen_operations - _KNOWN_QUEUE_OPERATIONS
        assert not unknown_operations, (
            f"unknown queue-operation (operation, reason) pairs {sorted(unknown_operations, key=str)} {drift}; "
            "consumers: accept-evidence probes, release-test delivery counting, "
            "wait_for_stop_hook.sh queued-prompt check, common_transcript_convert mid-turn messages, "
            "stream_transcript.sh interrupt heal's queued-prompt guard"
        )


# MIND-171: a `!`-prefixed message strands the agent in Claude's shell mode

# The stranded `!` send hangs for the full strict-confirmation window
# (ClaudeAgent.confirmation_timeout_seconds) before it fails, so the send
# timeout must sit comfortably above it.
_BANG_SEND_TIMEOUT_SECONDS: Final[float] = 150.0


def _login_home() -> Path:
    """The invoking user's real home, independent of a redirected ``HOME``.

    The release harness isolates ``HOME`` to a temp dir; a subscription (no
    API key) Claude login lives under the *real* home -- ``~/.claude`` and, on
    macOS, the login keychain at ``~/Library/Keychains`` -- so both the
    availability check and the ``mngr`` subprocesses must consult it to
    authenticate. Read from the password database so a patched ``HOME`` env var
    does not hide it.
    """
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _local_claude_login_present() -> bool:
    """Whether a local ``claude`` can authenticate without ``ANTHROPIC_API_KEY``.

    The rest of the release suite requires ``ANTHROPIC_API_KEY``, but this
    reproduction needs only a live claude TUI -- the strand is a TUI/tmux
    behaviour and a bare ``!`` never reaches the model. A developer logged into
    Claude locally (a subscription/OAuth login, as minds users are) can run it:
    the OAuth token lives in ``~/.claude/.credentials.json`` or, on macOS, the
    login keychain under the ``Claude Code-credentials`` service. Both are keyed
    to the real login home, not the harness's redirected ``HOME``.
    """
    home = _login_home()
    if (home / ".claude" / ".credentials.json").exists():
        return True
    if sys.platform == "darwin":
        probe = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials"],
            capture_output=True,
            check=False,
            env={**os.environ, "HOME": str(home)},
        )
        if probe.returncode == 0:
            return True
    return False


class _BangShellModeProfile(ClaudeReleaseProfile):
    """A Claude release profile that also runs on a locally-logged-in (no API key) machine.

    Reuses ``ClaudeReleaseProfile``'s workspace/config plumbing, but widens the
    availability gate to accept a local Claude login. With an ``ANTHROPIC_API_KEY``
    set it behaves exactly like the parent profile (isolated config dir, key passed
    into the agent), so the reproduction still runs in the key-based release
    pipeline. Without one it authenticates from the developer's existing local
    Claude login (a subscription/OAuth login, as minds users have):

    * The ``mngr`` subprocesses run under the *real* login home, since the harness
      isolates ``HOME`` to a temp dir and the macOS login keychain / ``~/.claude``
      credentials live under the real home.
    * The agent is provisioned in *shared* config-dir mode
      (``isolate_local_config_dir = false``) so it reuses that login directly. This
      is the mode mngr itself recommends for macOS subscription users (isolated
      mode copies the credentials into a separate keychain entry that goes stale).
      As a result the agent shares the developer's ``~/.claude`` while it runs; the
      bare ``!`` reproduction never reaches the model, so this leaves at most a
      short empty session behind. mngr's own state stays isolated via
      ``MNGR_HOST_DIR`` and the per-test tmux server.
    """

    def unavailable_reason(self) -> str | None:
        if shutil.which("claude") is None:
            return "Reproduction requires `claude` on PATH."
        if os.environ.get("ANTHROPIC_API_KEY") or _local_claude_login_present():
            return None
        return "Reproduction requires ANTHROPIC_API_KEY or a local Claude login (~/.claude credentials or macOS keychain)."

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        ctx = super().setup(tmp_path)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            append_project_settings(ctx, "\n[agent_types.claude]\nisolate_local_config_dir = false\n")
        return ctx

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        env = dict(ctx.env)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            # Point at the real login home so keychain / ~/.claude auth resolves.
            env["HOME"] = str(_login_home())
        return run_mngr_subprocess(*args, env=env, timeout=timeout)

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        args: list[str] = ["--no-ensure-clean", "--source", str(ctx.project_dir)]
        if os.environ.get("ANTHROPIC_API_KEY"):
            args += ["--pass-env", "ANTHROPIC_API_KEY"]
        args += ["--", "--dangerously-skip-permissions", "--model", RELEASE_TEST_MODEL]
        return args


# The continuation glyph Claude renders before a shell command's captured output
# (e.g. ``⎿  mngr-behaviors-probe``). Its presence in the pane means a command ran and
# produced output; its absence means no shell command ran.
_COMMAND_OUTPUT_MARKER: Final[str] = "⎿"


def _find_command_output_line(pane: str, token: str) -> str | None:
    """Return the pane line showing ``token`` as a shell command's OUTPUT, or None.

    Distinguishes the command's output from the echoed command line: the output line
    carries ``token`` but not the ``echo`` verb that only the typed command contains.
    """
    for line in pane.splitlines():
        if token in line and "echo" not in line:
            return line
    return None


@pytest.mark.witnesses("message-delivery.bare-bang-inert")
@pytest.mark.witnesses(
    "message-delivery.stays-ready",
    partial="witnesses only the lone-`!` instance: a normal message is still delivered after a bare `!`",
)
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_bang_prefix_does_not_strand_agent_in_shell_mode(tmp_path: Path) -> None:
    """A bare ``!`` is inert and leaves the agent ready: no command runs, no new turn.

    Witnesses ``message-delivery.bare-bang-inert`` (fully) and the
    ``message-delivery.stays-ready`` invariant (the lone-``!`` instance) in the
    ``libs/mngr_claude/behaviors`` corpus.

    mngr types a short message with ``tmux send-keys -l``, so a leading ``!`` is
    delivered as a literal keystroke and flips Claude Code into shell (bash)
    mode: the input row's column-0 ``❯`` prompt is replaced by ``!`` and the
    footer reads "! for shell mode". Submitting an empty shell line (a bare ``!``)
    is a no-op that stays in shell mode, so without the fix the pane can never
    leave shell mode on its own and the ``❯``-keyed readiness checks desync. The
    plugin recognizes this and leaves shell mode (a Backspace), restoring the prompt.

    This asserts all three observable claims of the scenario: the bare ``!`` runs
    no shell command (no command output appears in the conversation), the
    conversation gains no new turn (the common transcript is unchanged, and the
    later follow-up is the only turn), and the agent is ready for the next message
    (the ``❯`` prompt is back and a normal follow-up is delivered and recorded).
    """
    profile = _BangShellModeProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"claude-bang-{get_short_random_string()}"
    subdir = profile.common_transcript_subdir
    run_id = get_short_random_string()
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        wait_for_agent_prompt(ctx, agent_name, failure="the agent never drew the ❯ input prompt before the bare `!`")
        # Baseline: the freshly-created agent (no seed message) has no conversation turns yet.
        baseline_records = read_common_records(ctx.host_dir, subdir)

        # The send's exit code is intentionally not asserted: a fix may legitimately
        # reject `!` quickly rather than deliver it.
        profile.run_mngr(ctx, "message", agent_name, "--message", "!", timeout=_BANG_SEND_TIMEOUT_SECONDS)

        # The agent must be back at its input prompt, not wedged in shell mode.
        pane = capture_agent_pane_with_scrollback(ctx, agent_name)
        assert has_input_prompt_line(pane), (
            "after a bare `!` the agent is stranded in Claude shell mode -- "
            f"the ❯ input prompt is gone (footer typically reads '! for shell mode'):\n{pane}"
        )
        # No shell command runs: a command would render its output under the `⎿` marker.
        assert _COMMAND_OUTPUT_MARKER not in pane, (
            f"a bare `!` must run no shell command, but command output ('{_COMMAND_OUTPUT_MARKER}') "
            f"appears in the conversation:\n{pane}"
        )
        # The conversation gains no new turn: the common transcript is unchanged by the bare `!`.
        assert read_common_records(ctx.host_dir, subdir) == baseline_records, (
            "a bare `!` added a conversation turn: the common transcript changed after delivering `!`"
        )

        # The user can still interact: a normal follow-up is delivered and reaches the transcript.
        token = f"AFTERBANG-{run_id}"
        send_expecting_success(profile, ctx, agent_name, f"Remember this exact value: {token}. Reply with just OK.")
        wait_for_user_message(
            ctx.host_dir,
            subdir,
            token,
            description="follow-up message after a `!` send never reached the transcript -- "
            "the agent is desynced (stranded in shell mode)",
        )
        # The follow-up is the ONLY delivered turn: the bare `!` contributed no user turn of its own.
        final_records = read_common_records(ctx.host_dir, subdir)
        user_turn_count = sum(1 for r in final_records if is_step_from(r, "user"))
        assert user_turn_count == 1, (
            f"expected exactly one user turn (the follow-up); the bare `!` must add none, got {user_turn_count}"
        )


@pytest.mark.witnesses("message-delivery.runs-command")
@pytest.mark.witnesses(
    "message-delivery.stays-ready",
    partial="witnesses only the `!<command>` instance: the agent is ready (❯ prompt) after a bang command runs",
)
@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_bang_command_runs_and_leaves_agent_ready(tmp_path: Path) -> None:
    """A ``!<command>`` runs the command, its output appears, and the agent stays ready.

    Witnesses ``message-delivery.runs-command`` and the ``message-delivery.stays-ready``
    invariant (the ``!<command>`` instance) in the ``libs/mngr_claude/behaviors`` corpus.

    Delivering ``!echo mngr-behaviors-probe`` drives Claude Code's shell mode: the command
    runs in the pane and its stdout is rendered as the command's output (``⎿  mngr-behaviors-probe``),
    then the input box returns to normal mode on its own. A ``!`` command leaves no durable
    submission record, so the plugin confirms it under the relaxed policy (a strict send would
    hang and wrongly fail); the observable contract is the command's output in the conversation
    and the ``❯`` prompt afterward, which is what this asserts (via the pane the interactive
    client sees), not any mngr terminal internal.
    """
    profile = _BangShellModeProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"claude-bang-{get_short_random_string()}"
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        wait_for_agent_prompt(ctx, agent_name, failure="the agent never drew the ❯ input prompt before the `!echo`")

        # The send's exit code is not asserted; the observable contract is the command's output.
        profile.run_mngr(
            ctx, "message", agent_name, "--message", "!echo mngr-behaviors-probe", timeout=_BANG_SEND_TIMEOUT_SECONDS
        )

        # The command's OUTPUT (not just the echoed command) must appear in the conversation.
        wait_for_agent_pane(
            ctx,
            agent_name,
            lambda pane: _find_command_output_line(pane, "mngr-behaviors-probe") is not None,
            capture=capture_agent_pane_with_scrollback,
            timeout=90.0,
            failure="the `!echo mngr-behaviors-probe` command's output 'mngr-behaviors-probe' never appeared "
            "in the conversation as command output",
        )

        # After running the command, the agent is back at its input prompt, ready for the next message.
        pane = capture_agent_pane_with_scrollback(ctx, agent_name)
        assert has_input_prompt_line(pane), (
            f"after a `!echo` command the agent is not ready -- the ❯ input prompt is gone:\n{pane}"
        )

"""Release test: full end-to-end lifecycle of a real mngr-managed claude agent.

Drives the real ``mngr`` CLI against the real ``claude`` binary and a real model
through the shared agent release lifecycle (create -> WAITING -> message -> RUNNING
-> transcript -> stop/start resume -> destroy -> adopt-from-preserved -> recall). The
arc and assertions live in ``imbue.mngr.agents.agent_release_testing``; this file
supplies claude's plumbing via an :class:`AgentReleaseProfile`.

claude runs the same shared arc as every other port: it observes the RUNNING marker (its
UserPromptSubmit hook touches the ``active`` marker), forces a bash tool call, and -- with
``asserts_usage`` on -- reports token usage. Its plumbing differs from the sibling ports
only in:

* Repo-local ``.gitignore``. claude's preflight refuses to write hooks to
  ``.claude/settings.local.json`` unless the repository's *own* ``.gitignore``
  excludes it (a global rule is rejected, since remote hosts lack it).
  ``_init_claude_workspace`` seeds that rule for both the seed worktree and the fresh
  adoption worktree; the sibling ports don't need this.

* Custom-API-key approval. The plugin's ``approve_api_key_for_claude`` pre-approves the
  passed-in ``ANTHROPIC_API_KEY`` during provision, so claude doesn't block on its
  custom-key dialog (no sibling port has one). claude's other first-run dialogs
  (onboarding/effort) and work-dir trust are dismissed by the ``--yes`` the harness
  already passes for every agent -- not a claude specific -- so the test seeds no config.

* Post-``--`` args. ``--dangerously-skip-permissions`` lets the forced bash tool call
  run without a permission pause, ``--pass-env ANTHROPIC_API_KEY`` carries the key to the
  agent, and ``--model haiku`` pins the cheapest tier (the seed/recall turns don't need
  more).

* Adoption resolves by the preserved session JSONL's absolute path. claude has no
  root-session-id sidecar file (unlike codex); the preserved native store is the
  per-agent ``projects/<encoded-work-dir>/<session-id>.jsonl`` tree, and
  ``_resolve_adopt_session`` accepts a ``.jsonl`` path directly, so the path is both
  unambiguous and independent of the encoded-cwd subdir name.

Requires ``claude`` on PATH and ``ANTHROPIC_API_KEY`` in the environment; skipped
otherwise. Release-marked, so it does not run per PR.
"""

from __future__ import annotations

import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import AgentReleaseProfile
from imbue.mngr.agents.agent_release_testing import agent_state_dir
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
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr_claude.dialogs import classify
from imbue.mngr_claude.plugin import has_input_prompt_line

# claude's native resumable session store, relative to the agent state dir: the
# per-agent Claude config dir's session JSONLs (see ``_AGENT_CLAUDE_PROJECTS_RELPATH``
# / ``_claude_preserved_items`` in plugin.py). preserve_sessions_on_destroy copies
# this tree to preserved/, and adopt_session_arg resolves the JSONL out of it.
_CLAUDE_PROJECTS_RELPATH = "plugin/claude/anthropic/projects"

# Pin the cheapest tier: the seed/recall turns just plant and echo a secret, so a frontier
# model would only add cost and latency to the release run. ``haiku`` is Claude Code's alias
# for the current Haiku.
_MODEL = "haiku"


def _init_claude_workspace(path: Path) -> None:
    """Init a git repo whose own .gitignore excludes Claude's settings.local.json.

    mngr's claude preflight refuses to write hooks to .claude/settings.local.json
    unless the repository's *own* .gitignore excludes it (a global rule is rejected,
    since remote hosts lack it). Both the seed worktree and the fresh adoption
    worktree must carry that rule, so this replaces the bare init_git_repo for each.
    """
    init_git_repo(path, initial_commit=False)
    (path / ".gitignore").write_text(".claude/settings.local.json\n")
    run_git_command(path, "add", ".gitignore")
    run_git_command(path, "commit", "-m", "Add .gitignore")


def _is_queue_operation_carrying(record: dict[str, Any], operation: str, token: str) -> bool:
    """Whether a raw session record is a ``queue-operation`` of ``operation`` whose content carries ``token``."""
    return (
        record.get("type") == "queue-operation"
        and record.get("operation") == operation
        and token in str(record.get("content", ""))
    )


def _is_stop_hook_summary(record: dict[str, Any]) -> bool:
    """Whether a raw session record is the ``system``/``stop_hook_summary`` record of a Stop hook pass."""
    return record.get("type") == "system" and record.get("subtype") == "stop_hook_summary"


class _ClaudeReleaseProfile(AgentReleaseProfile):
    agent_type = "claude"
    common_transcript_subdir = "claude"
    # claude's forced seed turn runs a bash tool call and its converter emits per-message
    # token usage, so both gated assertions apply (observing the RUNNING marker is universal).
    forces_tool_call = True
    asserts_usage = True
    # /clear exercises the relaxed slash-command policy end to end (claude records
    # its effect durably as a session-id change, but the send must not depend on it).
    clear_slash_command = "/clear"
    # This is the store the adopt-from-preserved arc adopts: after destroy, a fresh agent
    # in a new worktree adopts the just-preserved session and must recall the pre-destroy
    # secret -- proving the store resumes and the cross-cwd re-filing works.
    native_session_preserved_relpaths = (_CLAUDE_PROJECTS_RELPATH,)

    # Exercises the rejection probe end to end: claude writes a structured
    # "Unknown command" warning the instant it rejects this, and the journey
    # asserts the resulting send_rejected_by_agent event -- the canary for
    # upstream changes to that record (see _REJECTED_COMMAND_JQ_FILTER).
    unknown_slash_command = "/mngr-invalid-command-probe"

    def count_injected_deliveries(self, host_dir: Path, token: str) -> int:
        """Count queue-removals of ``token`` in the raw transcript.

        Claude Code (2.1.21x) may deliver a queued message by removing it from
        the queue and injecting it into the running turn: the raw transcript
        gets a ``queue-operation``/``remove`` record carrying the message text,
        and no user record is ever written.
        """
        count = 0
        for events_path in host_dir.glob("agents/*/logs/claude_transcript/events.jsonl"):
            for line in events_path.read_text().splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _is_queue_operation_carrying(record, "remove", token):
                    count += 1
        return count

    def adopt_session_arg(self, preserved_dir: Path) -> str:
        # Return the absolute path of the single preserved session JSONL. The shallow
        # ``*/*.jsonl`` glob targets ``projects/<encoded-work-dir>/<session-id>.jsonl``
        # and excludes nested subagent transcripts at ``<sid>/subagents/*.jsonl``.
        # Passing the path (not a bare session id) keeps adoption unambiguous: the
        # resolver otherwise searches every live and preserved agent's projects/ dir.
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
        # ``mngr create --yes`` dismisses claude's first-run dialogs and trusts the work dir,
        # and the plugin's ``approve_api_key_for_claude`` pre-approves the key, so no seeded
        # ~/.claude.json is needed. The env carries the redirected HOME and the isolated
        # MNGR_HOST_DIR / tmux server from the autouse fixture.
        env = get_subprocess_test_env(root_name="mngr-claude-release-test")

        # Disable the remote providers for every command: a purely local agent test, and
        # leaving them on makes mngr probe Modal/Docker (and rejects the autouse test prefix).
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
        # The adoption worktree is also a claude source, so it needs the same
        # repo-local .gitignore rule the seed worktree carries (see _init_claude_workspace).
        _init_claude_workspace(work_dir)

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        # Pass the work dir via --source (so mngr runs from the checkout under ``uv run``)
        # and the API key into the agent. ``--dangerously-skip-permissions`` lets the
        # forced bash tool call run without pausing on a permission dialog.
        return [
            "--no-ensure-clean",
            "--source",
            str(ctx.project_dir),
            "--pass-env",
            "ANTHROPIC_API_KEY",
            "--",
            "--dangerously-skip-permissions",
            "--model",
            _MODEL,
        ]

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return run_mngr_subprocess(*args, env=dict(ctx.env), timeout=timeout)


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
    run_agent_release_lifecycle(_ClaudeReleaseProfile(), tmp_path)


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
    run_message_delivery_journey(_ClaudeReleaseProfile(), tmp_path)


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
    run_concurrent_message_delivery(_ClaudeReleaseProfile(), tmp_path)


# Create can provision a real claude.
_AGENT_CREATE_TIMEOUT_SECONDS: Final[float] = 600.0
_AGENT_DESTROY_TIMEOUT_SECONDS: Final[float] = 300.0


def _create_haiku_agent(profile: _ClaudeReleaseProfile, ctx: AgentReleaseContext, agent_name: str) -> None:
    create = profile.run_mngr(
        ctx,
        "create",
        agent_name,
        profile.agent_type,
        "--no-connect",
        "--yes",
        *profile.create_extra_args(ctx),
        timeout=_AGENT_CREATE_TIMEOUT_SECONDS,
    )
    assert create.returncode == 0, f"create failed:\n{create.stdout}\n{create.stderr}"


def _destroy_agent_and_tear_down(
    profile: _ClaudeReleaseProfile, ctx: AgentReleaseContext, agent_name: str, *, timeout: float
) -> None:
    """Force-destroy ``agent_name``, then run ``ctx``'s teardown even if the destroy raises."""
    try:
        profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=timeout)
    finally:
        if ctx.teardown is not None:
            ctx.teardown()


def _drift_note(ctx: AgentReleaseContext, subject: str) -> str:
    """A failure-message suffix naming the installed claude, e.g. ``(claude version: <version>; a failure here means <subject> drifted)``."""
    result = subprocess.run(["claude", "--version"], capture_output=True, text=True, env=dict(ctx.env), timeout=60.0)
    claude_version = result.stdout.strip() or "unknown"
    return f"(claude version: {claude_version}; a failure here means {subject} drifted)"


# Timeouts for the model-picker dialog tests. The send is a single relaxed slash command whose
# post-submit dialog check observes for a few seconds.
_MODEL_PICKER_SEND_TIMEOUT_SECONDS = 120.0
_MODEL_PICKER_DESTROY_TIMEOUT_SECONDS = 150.0

# Bare ``/model`` opens Claude Code's interactive model picker: a numbered selector (a rule line
# followed by indented options, one highlighted with ``❯``) that blocks until a choice is made.
# This is the load-bearing blocking dialog the hardening must auto-accept or surface as blocked.
# We use the bare command (not ``/model <name>``, which on current Claude versions switches
# directly with no dialog) because the picker is opened reliably and version-independently. The
# highlighted default is the agent's current model, so accepting it (Enter) is a benign no-op that
# just closes the picker.
_MODEL_PICKER_COMMAND = "/model"

# Bump the post-submit observe window above the 2s default so a real host that renders the picker a
# beat late is still caught -- and, incidentally, exercise the configurable
# post_submit_dialog_observe_seconds knob end to end against a live agent.
_MODEL_PICKER_OBSERVE_SECONDS = 4.0


def _setup_ctx_that_answers_dialogs(profile: _ClaudeReleaseProfile, tmp_path: Path) -> AgentReleaseContext:
    """Set up a release ctx whose agent answers every dialog mngr has a sensible answer for.

    Reuses the profile's standard setup, then appends an ``[agent_types.claude]`` section opting
    into that, with a slightly widened observe window.
    """
    ctx = profile.setup(tmp_path)
    settings_path = Path(ctx.env["MNGR_PROJECT_CONFIG_DIR"]) / "settings.local.toml"
    with settings_path.open("a") as settings_file:
        settings_file.write(
            f"\n[agent_types.claude]\n"
            f'sensibly_deal_with_dialogs = ["ALL_KNOWN_DIALOGS"]\n'
            f"post_submit_dialog_observe_seconds = {_MODEL_PICKER_OBSERVE_SECONDS}\n"
        )
    return ctx


def _capture_agent_pane(ctx: AgentReleaseContext, agent_name: str) -> str:
    """Capture the agent's primary tmux window pane as plain text (colors stripped, like mngr)."""
    session = ctx.env["MNGR_PREFIX"] + agent_name
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", f"={session}:0"], capture_output=True, text=True, check=False
    )
    return result.stdout


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
    rather than the dialog-clearing path specifically. That path -- a dialog present when the NEXT
    send starts, cleared at preflight or refused -- is covered deterministically by the plugin unit
    tests (see ``plugin_test.py``), which script a selector that persists.
    """
    profile = _ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = _setup_ctx_that_answers_dialogs(profile, tmp_path)
    agent_name = f"claude-modelpicker-{get_short_random_string()}"
    run_id = get_short_random_string()
    try:
        _create_haiku_agent(profile, ctx, agent_name)
        result = profile.run_mngr(
            ctx, "message", agent_name, "--message", _MODEL_PICKER_COMMAND, timeout=_MODEL_PICKER_SEND_TIMEOUT_SECONDS
        )
        assert result.returncode == 0, (
            f"expected /model to deliver and exit 0, got {result.returncode}:\n{result.stdout}\n{result.stderr}"
        )
        pane = _capture_agent_pane(ctx, agent_name)
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
    finally:
        _destroy_agent_and_tear_down(profile, ctx, agent_name, timeout=_MODEL_PICKER_DESTROY_TIMEOUT_SECONDS)


# Transcript record contract

_SESSION_RECORD_TIMEOUT_SECONDS: Final[float] = 90.0
_CONTRACT_BUSY_ATTEMPTS = 3

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


def _read_session_records(session_file: Path) -> list[dict[str, Any]]:
    records = []
    for line in session_file.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _wait_for_session_record(
    session_file: Path, predicate: Callable[[dict[str, Any]], bool], *, failure: str
) -> dict[str, Any]:
    found: list[dict[str, Any]] = []

    def has_match() -> bool:
        for record in _read_session_records(session_file):
            if predicate(record):
                found.append(record)
                return True
        return False

    assert poll_until(has_match, timeout=_SESSION_RECORD_TIMEOUT_SECONDS, poll_interval=2.0), failure
    return found[0]


def _contract_message_text(record: dict[str, Any]) -> str:
    """Flatten a user/assistant record's message.content (str or block-array) to text."""
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


def _wait_for_native_session_file(ctx: AgentReleaseContext) -> Path:
    """Wait for the agent's native session JSONL, named by its claude_session_id marker, and return it.

    Claude writes the file lazily on the first prompt, so call this after a send.
    """
    agent_dir = agent_state_dir(ctx.host_dir)
    session_id = (agent_dir / "claude_session_id").read_text().strip()
    assert session_id != "", "claude_session_id marker is empty"
    session_glob = f"{_CLAUDE_PROJECTS_RELPATH}/*/{session_id}.jsonl"
    assert poll_until(lambda: len(list(agent_dir.glob(session_glob))) == 1, timeout=60.0, poll_interval=2.0), (
        f"native session JSONL not at <config-dir>/projects/<encoded-cwd>/{session_id}.jsonl "
        f"{_drift_note(ctx, 'the native session file layout')}; "
        "consumers: every probe's native-transcript path expression"
    )
    return next(agent_dir.glob(session_glob))


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
    profile = _ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"claude-contract-{get_short_random_string()}"
    run_id = get_short_random_string()
    drift = _drift_note(ctx, "the record shape")
    destroyed = False

    try:
        _create_haiku_agent(profile, ctx, agent_name)

        # Contract 1: an idle prompt lands as {"type":"user","message":{"content":...}}.
        idle_token = f"contract-idle-{run_id}"
        send_expecting_success(
            profile, ctx, agent_name, f"Remember this exact value: {idle_token}. Reply with just OK."
        )

        # Contract 5: the native session JSONL is <config-dir>/projects/<encoded-cwd>/<session-id>.jsonl,
        # named by the claude_session_id marker.
        session_file = _wait_for_native_session_file(ctx)
        user_record = _wait_for_session_record(
            session_file,
            lambda r: r.get("type") == "user" and idle_token in _contract_message_text(r),
            failure=f"no user record with message.content carrying the sent text {drift}; "
            "consumers: content probes, common-transcript converter",
        )
        # Contract 6: records carry a top-level uuid (raw-streamer offset reconciliation).
        assert isinstance(user_record.get("uuid"), str) and user_record["uuid"] != "", (
            f"user record lost its top-level uuid {drift}; consumer: stream_transcript.sh offset reconciliation"
        )

        # Contract 4: the reply lands as {"type":"assistant","message":{"content":...}}.
        _wait_for_session_record(
            session_file,
            lambda r: r.get("type") == "assistant" and _contract_message_text(r) != "",
            failure=f"no assistant record with message.content {drift}; "
            "consumers: common-transcript converter, transcript readers",
        )

        # Contract 7: a turn's Stop hook pass lands as
        # {"type":"system","subtype":"stop_hook_summary"} (mngr always installs Stop hooks).
        _wait_for_session_record(
            session_file,
            _is_stop_hook_summary,
            failure=f"no system/stop_hook_summary record after a completed turn {drift}; "
            "consumers: wait_for_stop_hook.sh queued-prompt check, wait_for_stop_hook.sh has_blocked_stop_hook",
        )

        # Contract 2: a message sent while a turn runs lands as
        # {"type":"queue-operation","operation":"enqueue","content":...}.
        # Retried: the race is real (the running turn can finish first), and a
        # missed race must not read as record drift.
        enqueue_found = False
        for attempt in range(_CONTRACT_BUSY_ATTEMPTS):
            busy_token = f"contract-busy-{attempt}-{run_id}"
            # The starter turn must still be running when the next send lands,
            # or nothing enqueues. A bash sleep pins the turn open for a fixed
            # window, independent of model streaming speed.
            send_expecting_success(
                profile, ctx, agent_name, "Use the Bash tool to run exactly: sleep 30. Then reply with just DONE."
            )
            send_expecting_success(profile, ctx, agent_name, f"Also remember: {busy_token}. Reply with just OK.")

            def is_enqueue_with_token(record: dict[str, Any], token: str = busy_token) -> bool:
                return _is_queue_operation_carrying(record, "enqueue", token)

            if poll_until(
                lambda: any(is_enqueue_with_token(r) for r in _read_session_records(session_file)),
                timeout=30.0,
                poll_interval=2.0,
            ):
                enqueue_found = True
                break
        assert enqueue_found, (
            f"no queue-operation/enqueue record with content across {_CONTRACT_BUSY_ATTEMPTS} busy sends {drift}; "
            "consumers: busy-send accept evidence, content probes"
        )

        # Contract 2b: the queued message then either dequeues as a user record
        # or is removed from the queue and injected into the running turn (a
        # queue-operation/remove record carrying the text, no user record).
        # Both are single deliveries; anything else is drift.
        _wait_for_session_record(
            session_file,
            lambda r: (r.get("type") == "user" and busy_token in _contract_message_text(r))
            or _is_queue_operation_carrying(r, "remove", busy_token),
            failure=f"queued message neither dequeued as a user record nor removed-and-injected {drift}; "
            "consumers: release-test delivery counting, content probes",
        )

        # Contract 2c: the queue-operation vocabulary, as (operation, reason) pairs.
        seen_operations = {
            (str(r.get("operation")), r.get("reason"))
            for r in _read_session_records(session_file)
            if r.get("type") == "queue-operation"
        }
        unknown_operations = seen_operations - _KNOWN_QUEUE_OPERATIONS
        assert not unknown_operations, (
            f"unknown queue-operation (operation, reason) pairs {sorted(unknown_operations, key=str)} {drift}; "
            "consumers: accept-evidence probes, release-test delivery counting, "
            "wait_for_stop_hook.sh queued-prompt check, common_transcript_convert mid-turn messages, "
            "stream_transcript.sh interrupt heal's queued-prompt guard"
        )

        # Contract 3: an unknown slash command lands as
        # {"type":"system","level":"warning","content":"Unknown command: ..."}.
        typo = f"/mngr-invalid-command-{run_id}"
        send_expecting_success(profile, ctx, agent_name, typo)
        _wait_for_session_record(
            session_file,
            lambda r: r.get("type") == "system"
            and r.get("level") == "warning"
            and str(r.get("content", "")).startswith("Unknown command")
            and typo in str(r.get("content", "")),
            failure=f"no system/warning 'Unknown command' record for {typo!r} {drift}; "
            "consumer: _REJECTED_COMMAND_JQ_FILTER (rejection probe)",
        )

        destroy = profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=_AGENT_DESTROY_TIMEOUT_SECONDS)
        assert destroy.returncode == 0, f"destroy failed:\n{destroy.stdout}\n{destroy.stderr}"
        destroyed = True
    finally:
        try:
            if not destroyed:
                profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=_AGENT_DESTROY_TIMEOUT_SECONDS)
        finally:
            if ctx.teardown is not None:
                ctx.teardown()


# How long a real haiku turn may take to reach its next observable point.
_TURN_PROGRESS_TIMEOUT_SECONDS: Final[float] = 180.0
_STOP_HOOK_FEEDBACK_PREFIX: Final[str] = "Stop hook feedback:"


def _held_tool_command(start_marker: Path, release: Path) -> str:
    """A shell command that touches ``start_marker``, then waits until ``release`` exists.

    claude's Bash tool kills a command after its 2-minute default timeout, so a test creates
    ``release`` as soon as it has observed what it needs while the command runs.
    """
    return f"touch {shlex.quote(str(start_marker))} && until [ -e {shlex.quote(str(release))} ]; do sleep 1; done"


def _is_stop_hook_feedback(record: dict[str, Any]) -> bool:
    """Whether a raw session record is the isMeta user record carrying a blocking Stop hook's feedback."""
    return (
        record.get("type") == "user"
        and record.get("isMeta") is True
        and str((record.get("message") or {}).get("content")).startswith(_STOP_HOOK_FEEDBACK_PREFIX)
    )


def _wait_for_turn_to_end(profile: _ClaudeReleaseProfile, ctx: AgentReleaseContext, *, failure: str) -> None:
    assert poll_until(
        lambda: not profile.is_reporting_running(ctx.host_dir),
        timeout=_TURN_PROGRESS_TIMEOUT_SECONDS,
        poll_interval=2.0,
    ), failure


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_stop_hook_block_keeps_agent_running_through_its_continuation(tmp_path: Path) -> None:
    """A turn end that another Stop hook blocks leaves the agent RUNNING until the continuation ends.

    Also pins the records wait_for_stop_hook.sh reads to tell a blocked turn end from a finished one.
    """
    profile = _ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    blocked_marker = tmp_path / "turn-end-blocked"
    continuation_marker = tmp_path / "continuation-running"
    continuation_release = tmp_path / "continuation-release"
    feedback = (
        "Before you stop, use the Bash tool to run exactly: "
        f"{_held_tool_command(continuation_marker, continuation_release)}. Then reply with just DONE."
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
    settings = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": hook_command}]}]}}
    (ctx.project_dir / ".claude" / "settings.json").write_text(json.dumps(settings))
    run_git_command(ctx.project_dir, "add", ".claude")
    run_git_command(ctx.project_dir, "commit", "-m", "Add a Stop hook that blocks the first turn end")
    agent_name = f"claude-stopblock-{get_short_random_string()}"
    drift = _drift_note(ctx, "the stop hook records")
    try:
        _create_haiku_agent(profile, ctx, agent_name)
        send_expecting_success(profile, ctx, agent_name, "Reply with just OK.")
        assert poll_until(continuation_marker.exists, timeout=_TURN_PROGRESS_TIMEOUT_SECONDS, poll_interval=1.0), (
            f"the blocked turn end never continued into the hook's tool call (the hook blocked: {blocked_marker.exists()})"
        )
        is_running_as_continuation_starts = profile.is_reporting_running(ctx.host_dir)

        session_file = _wait_for_native_session_file(ctx)
        feedback_record = _wait_for_session_record(
            session_file,
            _is_stop_hook_feedback,
            failure=f"the blocked turn end wrote no isMeta user record whose string content starts with "
            f"{_STOP_HOOK_FEEDBACK_PREFIX!r} {drift}; consumer: wait_for_stop_hook.sh has_blocked_stop_hook",
        )

        def is_feedback_followed_by_stop_hook_summary() -> bool:
            records = _read_session_records(session_file)
            return any(_is_stop_hook_summary(record) for record in records[records.index(feedback_record) + 1 :])

        assert poll_until(
            is_feedback_followed_by_stop_hook_summary, timeout=_SESSION_RECORD_TIMEOUT_SECONDS, poll_interval=1.0
        ), (
            f"no stop_hook_summary follows the block's feedback record {drift}; "
            "consumer: wait_for_stop_hook.sh has_blocked_stop_hook, which reads the later summary as the turn end"
        )
        assert is_running_as_continuation_starts and profile.is_reporting_running(ctx.host_dir), (
            "the agent reported WAITING while the continuation of a blocked turn end was running; "
            "consumer: wait_for_stop_hook.sh has_blocked_stop_hook"
        )
        continuation_release.touch()
        _wait_for_turn_to_end(
            profile,
            ctx,
            failure="the agent still reports RUNNING after the continuation ended, so the unblocked turn end read "
            "as blocked; consumer: wait_for_stop_hook.sh has_blocked_stop_hook",
        )
    finally:
        _destroy_agent_and_tear_down(profile, ctx, agent_name, timeout=_AGENT_DESTROY_TIMEOUT_SECONDS)


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_mid_turn_message_lands_once_in_the_common_transcript(tmp_path: Path) -> None:
    """A message sent while a tool call runs reaches the common transcript as exactly one user step.

    Claude Code writes no user line for such a message, so this also pins the queue removal and
    ``queued_command`` attachment common_transcript_convert reads instead.
    """
    profile = _ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    tool_marker = tmp_path / "mid-turn-tool-running"
    tool_release = tmp_path / "mid-turn-tool-release"
    agent_name = f"claude-midturn-{get_short_random_string()}"
    token = f"MIDTURN-{get_short_random_string()}"
    drift = _drift_note(ctx, "the mid-turn message records")
    try:
        _create_haiku_agent(profile, ctx, agent_name)
        send_expecting_success(
            profile,
            ctx,
            agent_name,
            f"Use the Bash tool to run exactly: {_held_tool_command(tool_marker, tool_release)}. "
            "Then reply with just DONE.",
        )
        assert poll_until(tool_marker.exists, timeout=_TURN_PROGRESS_TIMEOUT_SECONDS, poll_interval=1.0), (
            "the starter turn never ran its Bash command, so no tool call was running for the mid-turn message"
        )
        send_expecting_success(
            profile, ctx, agent_name, f"Also remember this exact value: {token}. Reply with just OK."
        )

        session_file = _wait_for_native_session_file(ctx)
        _wait_for_session_record(
            session_file,
            lambda r: _is_queue_operation_carrying(r, "enqueue", token),
            failure=f"the message sent during a tool call wrote no queue-operation/enqueue record carrying it "
            f"{drift}; consumers: busy-send accept evidence, content probes",
        )
        tool_release.touch()
        removal = _wait_for_session_record(
            session_file,
            lambda r: _is_queue_operation_carrying(r, "remove", token),
            failure=f"the message sent during a tool call was never removed from the queue into the running turn "
            f"{drift}; consumer: common_transcript_convert mid-turn messages",
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
        attachment_record = _wait_for_session_record(
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
        records = _read_session_records(session_file)
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
        _wait_for_turn_to_end(profile, ctx, failure="the turn that absorbed the mid-turn message never ended")
        user_steps = count_user_steps_containing(
            read_common_records(ctx.host_dir, profile.common_transcript_subdir), token
        )
        assert user_steps == 1, f"the mid-turn message reached the common transcript {user_steps} times, not once"
    finally:
        _destroy_agent_and_tear_down(profile, ctx, agent_name, timeout=_AGENT_DESTROY_TIMEOUT_SECONDS)


# MIND-171: a `!`-prefixed message strands the agent in Claude's shell mode

# The stranded `!` send hangs for the full strict-confirmation window
# (ClaudeAgent.confirmation_timeout_seconds) before it fails, so the send
# timeout must sit comfortably above it.
_BANG_SEND_TIMEOUT_SECONDS = 150.0
_BANG_DESTROY_TIMEOUT_SECONDS = 150.0


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


class _BangShellModeProfile(_ClaudeReleaseProfile):
    """A Claude release profile that also runs on a locally-logged-in (no API key) machine.

    Reuses ``_ClaudeReleaseProfile``'s workspace/config plumbing, but widens the
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
            # No API key: reuse the developer's local login (see class docstring). Shared
            # config-dir mode is what mngr recommends for a macOS subscription.
            settings_path = Path(ctx.env["MNGR_PROJECT_CONFIG_DIR"]) / "settings.local.toml"
            with settings_path.open("a") as settings_file:
                settings_file.write("\n[agent_types.claude]\nisolate_local_config_dir = false\n")
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
        args += ["--", "--dangerously-skip-permissions", "--model", _MODEL]
        return args


def _wait_for_agent_prompt(ctx: AgentReleaseContext, agent_name: str, *, timeout: float = 60.0) -> str:
    """Poll the agent pane until Claude renders its ``❯`` input prompt; return that pane.

    ``mngr create`` returns once the launch command has been sent, a beat before the
    TUI finishes rendering, so a single immediate capture can catch the launch line
    instead of the prompt. Fails with the last pane on timeout.
    """
    captured: list[str] = []

    def ready() -> bool:
        pane = _capture_agent_pane(ctx, agent_name)
        captured.append(pane)
        return has_input_prompt_line(pane)

    assert poll_until(ready, timeout=timeout, poll_interval=1.0), (
        f"agent never rendered the ❯ input prompt within {timeout:.0f}s; last pane:\n{captured[-1] if captured else ''}"
    )
    return captured[-1]


# The continuation glyph Claude renders before a shell command's captured output
# (e.g. ``⎿  mngr-behaviors-probe``). Its presence in the pane means a command ran and
# produced output; its absence means no shell command ran.
_COMMAND_OUTPUT_MARKER = "⎿"


def _capture_agent_pane_with_scrollback(ctx: AgentReleaseContext, agent_name: str) -> str:
    """Capture the agent's pane including scrollback, so output above the fold is visible."""
    session = ctx.env["MNGR_PREFIX"] + agent_name
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-S", "-", "-t", f"={session}:0"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


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
    try:
        _create_haiku_agent(profile, ctx, agent_name)
        _wait_for_agent_prompt(ctx, agent_name)
        # Baseline: the freshly-created agent (no seed message) has no conversation turns yet.
        baseline_records = read_common_records(ctx.host_dir, subdir)

        # The send's exit code is intentionally not asserted: a fix may legitimately
        # reject `!` quickly rather than deliver it.
        profile.run_mngr(ctx, "message", agent_name, "--message", "!", timeout=_BANG_SEND_TIMEOUT_SECONDS)

        # The agent must be back at its input prompt, not wedged in shell mode.
        pane = _capture_agent_pane_with_scrollback(ctx, agent_name)
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
    finally:
        _destroy_agent_and_tear_down(profile, ctx, agent_name, timeout=_BANG_DESTROY_TIMEOUT_SECONDS)


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
    try:
        _create_haiku_agent(profile, ctx, agent_name)
        _wait_for_agent_prompt(ctx, agent_name)

        # The send's exit code is not asserted; the observable contract is the command's output.
        profile.run_mngr(
            ctx, "message", agent_name, "--message", "!echo mngr-behaviors-probe", timeout=_BANG_SEND_TIMEOUT_SECONDS
        )

        # The command's OUTPUT (not just the echoed command) must appear in the conversation.
        output_lines: list[str | None] = []

        def output_present() -> bool:
            pane = _capture_agent_pane_with_scrollback(ctx, agent_name)
            output_lines.append(_find_command_output_line(pane, "mngr-behaviors-probe"))
            return output_lines[-1] is not None

        assert poll_until(output_present, timeout=90.0, poll_interval=2.0), (
            "the `!echo mngr-behaviors-probe` command's output 'mngr-behaviors-probe' never appeared "
            f"in the conversation as command output:\n{_capture_agent_pane_with_scrollback(ctx, agent_name)}"
        )

        # After running the command, the agent is back at its input prompt, ready for the next message.
        pane = _capture_agent_pane_with_scrollback(ctx, agent_name)
        assert has_input_prompt_line(pane), (
            f"after a `!echo` command the agent is not ready -- the ❯ input prompt is gone:\n{pane}"
        )
    finally:
        _destroy_agent_and_tear_down(profile, ctx, agent_name, timeout=_BANG_DESTROY_TIMEOUT_SECONDS)

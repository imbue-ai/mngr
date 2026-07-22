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
otherwise. Release-marked, so it does not run in CI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import AgentReleaseProfile
from imbue.mngr.agents.agent_release_testing import run_agent_release_lifecycle
from imbue.mngr.agents.agent_release_testing import run_concurrent_message_delivery
from imbue.mngr.agents.agent_release_testing import run_message_delivery_journey
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr.utils.testing import run_mngr_subprocess

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
    # Exercises the rejection probe end to end: claude writes a structured
    # "Unknown command" warning the instant it rejects this, and the journey
    # asserts the resulting send_rejected_by_agent event -- the canary for
    # upstream changes to that record (see _REJECTED_COMMAND_JQ_FILTER).
    unknown_slash_command = "/zzz-mngr-release-probe"
    # This is the store the adopt-from-preserved arc adopts: after destroy, a fresh agent
    # in a new worktree adopts the just-preserved session and must recall the pre-destroy
    # secret -- proving the store resumes and the cross-cwd re-filing works.
    native_session_preserved_relpaths = (_CLAUDE_PROJECTS_RELPATH,)

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
                if (
                    record.get("type") == "queue-operation"
                    and record.get("operation") == "remove"
                    and token in str(record.get("content", ""))
                ):
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
        return AgentReleaseContext(env=env, workspace=work_dir, host_dir=Path(env["MNGR_HOST_DIR"]))

    def prepare_adoption_workspace(self, work_dir: Path) -> None:
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
            str(ctx.workspace),
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


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
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


# =============================================================================
# Transcript record contract
# =============================================================================

_CONTRACT_SEND_TIMEOUT_SECONDS = 180.0
_CONTRACT_RECORD_TIMEOUT_SECONDS = 90.0
_CONTRACT_BUSY_ATTEMPTS = 3


def _contract_read_records(session_file: Path) -> list[dict[str, Any]]:
    records = []
    for line in session_file.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _contract_wait_for_record(
    session_file: Path, predicate: Callable[[dict[str, Any]], bool], *, failure: str
) -> dict[str, Any]:
    found: list[dict[str, Any]] = []

    def has_match() -> bool:
        for record in _contract_read_records(session_file):
            if predicate(record):
                found.append(record)
                return True
        return False

    assert poll_until(has_match, timeout=_CONTRACT_RECORD_TIMEOUT_SECONDS), failure
    return found[0]


def _contract_message_text(record: dict[str, Any]) -> str:
    """Flatten a user/assistant record's message.content (str or block-array) to text."""
    content = record.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


@pytest.mark.release
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
    version_result = subprocess.run(
        ["claude", "--version"], capture_output=True, text=True, env=dict(ctx.env), timeout=60.0
    )
    claude_version = version_result.stdout.strip() or "unknown"
    drift = f"(claude version: {claude_version}; a failure here means the record shape drifted)"
    destroyed = False

    try:
        create = profile.run_mngr(
            ctx,
            "create",
            agent_name,
            profile.agent_type,
            "--no-connect",
            "--yes",
            *profile.create_extra_args(ctx),
            timeout=600.0,
        )
        assert create.returncode == 0, f"create failed:\n{create.stdout}\n{create.stderr}"

        agent_dirs = list(ctx.host_dir.glob("agents/*"))
        assert len(agent_dirs) == 1, f"expected exactly one agent dir, found {agent_dirs}"
        agent_dir = agent_dirs[0]
        session_id = (agent_dir / "claude_session_id").read_text().strip()
        assert session_id != "", "claude_session_id marker is empty"

        # Contract 1: an idle prompt lands as {"type":"user","message":{"content":...}}.
        idle_token = f"contract-idle-{run_id}"
        send = profile.run_mngr(
            ctx,
            "message",
            agent_name,
            "--message",
            f"Remember this exact value: {idle_token}. Reply with just OK.",
            timeout=_CONTRACT_SEND_TIMEOUT_SECONDS,
        )
        assert send.returncode == 0, f"idle send failed:\n{send.stdout}\n{send.stderr}"

        # Contract 5: the native session JSONL lives at
        # <config-dir>/projects/<encoded-cwd>/<session-id>.jsonl with the file
        # named by the claude_session_id marker. Every probe's native-path
        # expression depends on this layout. Claude writes the file lazily on
        # the first prompt, so this is checked after the idle send, with a poll.
        session_glob = f"plugin/claude/anthropic/projects/*/{session_id}.jsonl"
        assert poll_until(lambda: len(list(agent_dir.glob(session_glob))) == 1, timeout=60.0), (
            f"native session JSONL not at <config-dir>/projects/<encoded-cwd>/{session_id}.jsonl "
            f"{drift}; consumers: every probe's native-transcript path expression"
        )
        session_file = list(agent_dir.glob(session_glob))[0]
        user_record = _contract_wait_for_record(
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
        _contract_wait_for_record(
            session_file,
            lambda r: r.get("type") == "assistant" and _contract_message_text(r) != "",
            failure=f"no assistant record with message.content {drift}; "
            "consumers: common-transcript converter, transcript readers",
        )

        # Contract 2: a message sent while a turn runs lands as
        # {"type":"queue-operation","operation":"enqueue","content":...}.
        # Retried: the race is real (the running turn can finish first), and a
        # missed race must not read as record drift.
        enqueue_found = False
        for attempt in range(_CONTRACT_BUSY_ATTEMPTS):
            busy_token = f"contract-busy-{attempt}-{run_id}"
            # The starter must still be generating when the next send lands, or
            # nothing enqueues; a long counting task keeps the turn open far
            # longer than a short completion would.
            starter = profile.run_mngr(
                ctx,
                "message",
                agent_name,
                "--message",
                "Count from 1 to 200, one number per line. Do not use tools. End with DONE.",
                timeout=_CONTRACT_SEND_TIMEOUT_SECONDS,
            )
            assert starter.returncode == 0, f"busy-starter send failed:\n{starter.stdout}\n{starter.stderr}"
            queued = profile.run_mngr(
                ctx,
                "message",
                agent_name,
                "--message",
                f"Also remember: {busy_token}. Reply with just OK.",
                timeout=_CONTRACT_SEND_TIMEOUT_SECONDS,
            )
            assert queued.returncode == 0, f"queued send failed:\n{queued.stdout}\n{queued.stderr}"

            def is_enqueue_with_token(record: dict[str, Any], token: str = busy_token) -> bool:
                return (
                    record.get("type") == "queue-operation"
                    and record.get("operation") == "enqueue"
                    and token in str(record.get("content", ""))
                )

            if poll_until(
                lambda: any(is_enqueue_with_token(r) for r in _contract_read_records(session_file)),
                timeout=30.0,
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
        _contract_wait_for_record(
            session_file,
            lambda r: (r.get("type") == "user" and busy_token in _contract_message_text(r))
            or (
                r.get("type") == "queue-operation"
                and r.get("operation") == "remove"
                and busy_token in str(r.get("content", ""))
            ),
            failure=f"queued message neither dequeued as a user record nor removed-and-injected {drift}; "
            "consumers: release-test delivery counting, content probes",
        )

        # Contract 2c: the queue-operation vocabulary itself. A new operation
        # value means delivery-evidence semantics changed under mngr.
        known_operations = {"enqueue", "dequeue", "remove"}
        seen_operations = {
            str(r.get("operation")) for r in _contract_read_records(session_file) if r.get("type") == "queue-operation"
        }
        assert seen_operations <= known_operations, (
            f"unknown queue-operation values {sorted(seen_operations - known_operations)} {drift}; "
            "consumers: accept-evidence probes, release-test delivery counting"
        )

        # Contract 3: an unknown slash command lands as
        # {"type":"system","level":"warning","content":"Unknown command: ..."}.
        typo = f"/contract-zzz-{run_id}"
        typo_send = profile.run_mngr(
            ctx, "message", agent_name, "--message", typo, timeout=_CONTRACT_SEND_TIMEOUT_SECONDS
        )
        assert typo_send.returncode == 0, f"typo send failed:\n{typo_send.stdout}\n{typo_send.stderr}"
        _contract_wait_for_record(
            session_file,
            lambda r: r.get("type") == "system"
            and r.get("level") == "warning"
            and str(r.get("content", "")).startswith("Unknown command")
            and typo in str(r.get("content", "")),
            failure=f"no system/warning 'Unknown command' record for {typo!r} {drift}; "
            "consumer: _REJECTED_COMMAND_JQ_FILTER (rejection probe)",
        )

        destroy = profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=300.0)
        assert destroy.returncode == 0, f"destroy failed:\n{destroy.stdout}\n{destroy.stderr}"
        destroyed = True
    finally:
        try:
            if not destroyed:
                profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=300.0)
        finally:
            if ctx.teardown is not None:
                ctx.teardown()

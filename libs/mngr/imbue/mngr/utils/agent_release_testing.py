"""Shared harness for agent-plugin release (end-to-end) tests.

Every agent-type plugin ships a ``@pytest.mark.release`` test that drives the real
``mngr`` CLI against the real agent binary through the same lifecycle arc:

    create -> WAITING -> message -> turn runs -> transcript captured
           -> stop -> start -> recall a pre-restart secret -> destroy

The arc and its assertions are identical across agents; only the *plumbing* differs
(how the binary is launched and authenticated, how the workspace is seeded, which
flags ``create`` needs, how tmux is isolated). This module owns the arc and the
shared assertions; each plugin supplies an :class:`AgentReleaseProfile` that owns its
plumbing. A single ``run_agent_release_lifecycle(profile, tmp_path)`` call is then the
whole release test, so the parity the spec describes is enforced executably: every
agent is held to the same lifecycle and the same canonical-transcript contract.

The shared assertions are deliberately keyed on the **common transcript** (which every
agent emits, and which `imbue.mngr.agents.common_transcript_records` makes canonical)
rather than on the RUNNING marker: codex sets its marker only on ``UserPromptSubmit``
and clears it on ``Stop``, so polling the marker mid-turn is racy. Observing the
RUNNING marker is therefore an opt-in capability (``observes_running_marker``); the
WAITING-after-create check and transcript-conformance check are uniform.

These tests are not run in CI (release-marked); run a profile's test manually with the
real binary present, e.g.::

    uv run pytest -m release -p no:xdist --no-cov \\
        libs/mngr_opencode/imbue/mngr_opencode/test_opencode_agent.py
"""

from __future__ import annotations

import abc
import json
import subprocess
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Callable

import pytest

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string

# Generous defaults: real provisioning + a real model turn. A profile may widen any of
# these via its own @pytest.mark.timeout; these only bound the individual poll loops.
_CREATE_TIMEOUT_SECONDS = 600.0
_MESSAGE_TIMEOUT_SECONDS = 180.0
_RESPONSE_TIMEOUT_SECONDS = 300.0
_RUNNING_TIMEOUT_SECONDS = 90.0
_LIFECYCLE_TIMEOUT_SECONDS = 150.0


@dataclass(frozen=True)
class AgentReleaseContext:
    """Per-run plumbing produced by a profile's ``setup`` and consumed by the harness.

    ``env`` is how the harness invokes ``mngr`` (via ``profile.run_mngr``); ``workspace``
    is the git source/work dir (passed as ``--source`` or used as the ``mngr`` cwd, per
    profile); ``host_dir`` is the isolated ``MNGR_HOST_DIR`` the harness reads the agent's
    state (marker + transcript) out of. ``teardown`` runs in a ``finally`` after destroy --
    a profile uses it to tear down anything ``setup`` allocated (e.g. a private tmux
    server), and it must be safe to call even if setup half-failed.
    """

    env: Mapping[str, str]
    workspace: Path
    host_dir: Path
    teardown: Callable[[], None] = field(default=lambda: None)


class AgentReleaseProfile(abc.ABC):
    """Per-agent plumbing for the shared release lifecycle.

    Concrete profiles live in their own plugin's test module (so skip-guards stay
    per-binary and the packages do not couple). Subclasses set the class attributes and
    implement ``unavailable_reason``/``setup``/``create_extra_args``/``run_mngr``.
    """

    # The `mngr create <name> <agent_type>` type string and the common-transcript
    # subdir under <state>/events/<subdir>/common_transcript/events.jsonl.
    agent_type: str
    common_transcript_subdir: str

    # Capability flags -- which agent-specific assertions apply (mirrors the parity
    # matrix). Defaults suit a minimal port; richer agents opt in.
    observes_running_marker: bool = True
    forces_tool_call: bool = False
    asserts_usage: bool = False

    @abc.abstractmethod
    def unavailable_reason(self) -> str | None:
        """Return a skip reason if the agent can't run here (missing binary/creds), else None."""

    @abc.abstractmethod
    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        """Seed the workspace/auth/env and return the run context (incl. teardown)."""

    @abc.abstractmethod
    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        """Args appended after ``create <name> <agent_type> --no-connect --yes`` (source, model, post-`--`)."""

    @abc.abstractmethod
    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        """Invoke ``mngr`` with ``args`` for this agent (each agent launches mngr its own way)."""

    def seed_prompt(self, secret: str) -> str:
        """The first message: plant ``secret`` and (if forcing a tool) run a bash echo.

        Overridable; the default plants the secret verbatim so the recall turn can
        prove resume, and -- when ``forces_tool_call`` -- forces a tool call so the
        transcript contains a tool_result.
        """
        if self.forces_tool_call:
            return (
                f"Remember this exact value for later: the secret answer is {secret}. "
                "Then use the bash tool to run exactly: echo SEEDED -- and finally reply with just ACK."
            )
        return f"Remember this exact secret for later: {secret}. Reply with just OK."

    def recall_prompt(self) -> str:
        return "What was the exact secret I asked you to remember earlier? Reply with just the secret."


def _agent_state_dir(host_dir: Path) -> Path:
    candidates = [path for path in (host_dir / "agents").glob("*") if path.is_dir()]
    assert len(candidates) == 1, f"expected exactly one agent state dir under {host_dir}/agents, found {candidates}"
    return candidates[0]


def _marker_path(host_dir: Path) -> Path:
    return _agent_state_dir(host_dir) / "active"


def _read_common_records(host_dir: Path, subdir: str) -> list[dict[str, Any]]:
    path = _agent_state_dir(host_dir) / "events" / subdir / "common_transcript" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _wait_for_records(
    host_dir: Path,
    subdir: str,
    predicate: Callable[[list[dict[str, Any]]], bool],
    *,
    timeout: float,
    description: str,
) -> list[dict[str, Any]]:
    """Poll the common transcript until ``predicate`` holds; return the records (or fail)."""
    last: list[list[dict[str, Any]]] = [[]]

    def _ready() -> bool:
        last[0] = _read_common_records(host_dir, subdir)
        return predicate(last[0])

    if not poll_until(condition=_ready, timeout=timeout, poll_interval=2.0):
        raise AssertionError(
            f"{description} within {timeout}s. Last transcript:\n{json.dumps(last[0], indent=2)[:4000]}"
        )
    return last[0]


def _assert_records_conform(records: list[dict[str, Any]]) -> None:
    for record in records:
        error = validate_common_transcript_record(record)
        assert error is None, f"emitted record does not match the canonical schema ({error}): {record}"


def run_agent_release_lifecycle(profile: AgentReleaseProfile, tmp_path: Path) -> None:
    """Drive the full create -> lifecycle -> transcript -> resume -> destroy arc for ``profile``.

    Skips (rather than fails) when the agent binary/credentials are absent, so the test
    is a no-op in environments without them. Every assertion that is reasonable for all
    agents runs uniformly; capability flags gate the agent-specific ones.
    """
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    host_dir = ctx.host_dir
    subdir = profile.common_transcript_subdir
    agent_name = f"{profile.agent_type.replace('-', '')}-e2e-{get_short_random_string()}"
    secret = f"SECRET-{get_short_random_string()}"

    try:
        # 1. Create (no inline message), then assert the agent is idle: marker absent.
        create = profile.run_mngr(
            ctx,
            "create",
            agent_name,
            profile.agent_type,
            "--no-connect",
            "--yes",
            *profile.create_extra_args(ctx),
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        assert create.returncode == 0, f"create failed:\n{create.stdout}\n{create.stderr}"
        assert not _marker_path(host_dir).exists(), "expected WAITING (no active marker) right after create"

        # 2. Seed the secret. Optionally observe the RUNNING marker (skipped where racy).
        seed = profile.run_mngr(
            ctx, "message", agent_name, "--message", profile.seed_prompt(secret), timeout=_MESSAGE_TIMEOUT_SECONDS
        )
        assert seed.returncode == 0, f"seed message failed:\n{seed.stdout}\n{seed.stderr}"
        if profile.observes_running_marker:
            assert poll_until(
                condition=_marker_path(host_dir).exists, timeout=_RUNNING_TIMEOUT_SECONDS, poll_interval=0.2
            ), "active marker never appeared -> agent never reported RUNNING"

        # 3. The seed turn must be captured: user_message carries the secret, plus a reply.
        def _seed_captured(records: list[dict[str, Any]]) -> bool:
            has_user_secret = any(r["type"] == "user_message" and secret in str(r.get("content", "")) for r in records)
            has_assistant = any(r["type"] == "assistant_message" for r in records)
            has_tool = (not profile.forces_tool_call) or any(r["type"] == "tool_result" for r in records)
            return has_user_secret and has_assistant and has_tool

        records = _wait_for_records(
            host_dir,
            subdir,
            _seed_captured,
            timeout=_RESPONSE_TIMEOUT_SECONDS,
            description="seed turn was not captured",
        )

        # 4. The captured records must all match the canonical envelope, plus capability checks.
        _assert_records_conform(records)
        assert all(r["source"] == f"{subdir}/common_transcript" for r in records), records
        assert len({r["event_id"] for r in records}) == len(records), "event_ids must be unique"
        if profile.forces_tool_call:
            assert any(
                c.get("tool_name")
                for r in records
                if r["type"] == "assistant_message"
                for c in r.get("tool_calls", [])
            ), f"expected an assistant tool_call: {records}"
            assert any(r["type"] == "tool_result" for r in records), records
        if profile.asserts_usage:
            usage_keys = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"}
            assistant = [r for r in records if r["type"] == "assistant_message"]
            assert assistant and all(usage_keys <= set(r.get("usage") or {}) for r in assistant), assistant

        # 5. Stop then start -> the launch command must resume the prior conversation.
        stop = profile.run_mngr(ctx, "stop", agent_name, timeout=_LIFECYCLE_TIMEOUT_SECONDS)
        assert stop.returncode == 0, f"stop failed:\n{stop.stdout}\n{stop.stderr}"
        start = profile.run_mngr(ctx, "start", agent_name, "--no-connect", timeout=_CREATE_TIMEOUT_SECONDS)
        assert start.returncode == 0, f"start failed:\n{start.stdout}\n{start.stderr}"

        # 6. Ask for the secret; resume worked iff the model recalls it from pre-restart context.
        recall = profile.run_mngr(
            ctx, "message", agent_name, "--message", profile.recall_prompt(), timeout=_MESSAGE_TIMEOUT_SECONDS
        )
        assert recall.returncode == 0, f"recall message failed:\n{recall.stdout}\n{recall.stderr}"
        post = _wait_for_records(
            host_dir,
            subdir,
            lambda records: any(
                r["type"] == "assistant_message" and secret in str(r.get("text", "")) for r in records
            ),
            timeout=_RESPONSE_TIMEOUT_SECONDS,
            description=f"agent did not recall the secret {secret!r} after stop/start (resume failed)",
        )

        # 7. History survived the restart: the pre-restart seed user_message is still present
        #    (guards the opencode rebuild-on-idle raw-seeding; trivially holds for append-only emitters).
        assert any(r["type"] == "user_message" and secret in str(r.get("content", "")) for r in post), (
            "pre-restart turn was lost from the common transcript after stop/start"
        )
        _assert_records_conform(post)
    finally:
        try:
            profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=_LIFECYCLE_TIMEOUT_SECONDS)
        finally:
            ctx.teardown()

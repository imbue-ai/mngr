"""Shared harness for agent-plugin release (end-to-end) tests.

Every agent-type plugin ships a ``@pytest.mark.release`` test that drives the real
``mngr`` CLI against the real agent binary through the same lifecycle arc:

    create -> WAITING -> message -> turn runs -> transcript captured
           -> stop -> start -> recall a pre-restart secret -> destroy
           -> adopt the preserved session into a fresh agent -> recall again

The arc and its assertions are identical across agents; only the *plumbing* differs
(how the binary is launched and authenticated, how the workspace is seeded, which
flags ``create`` needs, how tmux is isolated). This module owns the arc and the
shared assertions; each plugin supplies an :class:`AgentReleaseProfile` that owns its
plumbing. A single ``run_agent_release_lifecycle(profile, tmp_path)`` call is then the
whole release test, so the parity the spec describes is enforced executably: every
agent is held to the same lifecycle and the same canonical-transcript contract.

The shared assertions are keyed on the **common transcript** (which every agent emits,
and which `imbue.mngr.agents.common_transcript_records` makes canonical). Every agent is
held to the same core arc -- it surfaces the RUNNING marker and resumes on stop/start and
adoption. Two capabilities still vary by agent: forcing a bash tool call (so the transcript
carries a tool_call nested on the assistant turn plus its tool_result) is gated by
``forces_tool_call`` because antigravity cannot satisfy it (it runs the command async and
ends the turn before the result settles, so a single forced-tool turn carries no
tool_result -- see its profile), and reporting per-message token usage is gated by
``asserts_usage`` since not every CLI exposes it.

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
from pathlib import Path
from typing import Any
from typing import Callable

import pytest
from pydantic import BaseModel
from pydantic import ConfigDict

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import init_git_repo

# Generous defaults: real provisioning + a real model turn. A profile may widen any of
# these via its own @pytest.mark.timeout; these only bound the individual poll loops.
_CREATE_TIMEOUT_SECONDS = 600.0
_MESSAGE_TIMEOUT_SECONDS = 180.0
_RESPONSE_TIMEOUT_SECONDS = 300.0
_RUNNING_TIMEOUT_SECONDS = 90.0
_LIFECYCLE_TIMEOUT_SECONDS = 150.0


class AgentReleaseContext(BaseModel):
    """Per-run plumbing produced by a profile's ``setup`` and consumed by the harness.

    ``env`` is how the harness invokes ``mngr`` (via ``profile.run_mngr``); ``workspace``
    is the git source/work dir (passed as ``--source`` or used as the ``mngr`` cwd, per
    profile); ``host_dir`` is the isolated ``MNGR_HOST_DIR`` the harness reads the agent's
    state (marker + transcript) out of. ``teardown`` runs in a ``finally`` after destroy --
    a profile uses it to tear down anything ``setup`` allocated (e.g. a private tmux
    server); it must be safe to call even if setup half-failed, and is omitted when the
    profile has nothing to tear down.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    env: Mapping[str, str]
    workspace: Path
    host_dir: Path
    teardown: Callable[[], None] | None = None


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

    # Capability flags. Observing the RUNNING marker is required of every agent, so it is
    # not a flag. ``forces_tool_call`` is gated because antigravity cannot satisfy it (its
    # tool result is captured only at the next turn boundary, so a single forced-tool turn
    # never carries a tool_result -- see its profile). ``asserts_usage`` is gated because
    # not every CLI reports per-message token usage (codex, opencode, antigravity do not).
    forces_tool_call: bool = False
    asserts_usage: bool = False
    # Paths (relative to the agent state dir, mirrored under the preserved dir) of the
    # agent's *native* resumable session store -- the files preserve-on-destroy copies
    # in addition to the transcripts (e.g. codex's `plugin/codex/home/sessions`). The arc
    # asserts each exists and is non-empty after destroy. Empty when the agent has no
    # native store worth preserving. This is also the store the adoption step adopts
    # (resolved via ``adopt_session_arg``).
    native_session_preserved_relpaths: Sequence[str] = ()

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
        """The first message: plant ``secret`` and (when ``forces_tool_call``) run a bash echo.

        Overridable; the default plants the secret verbatim so the recall turn can prove
        resume, and -- when ``forces_tool_call`` -- forces a tool call so the transcript
        carries a tool_result.
        """
        if self.forces_tool_call:
            return (
                f"Remember this exact value for later: the secret answer is {secret}. "
                "Then use the bash tool to run exactly: echo SEEDED -- and finally reply with just ACK."
            )
        return f"Remember this exact secret for later: {secret}. Reply with just OK."

    def recall_prompt(self) -> str:
        return "What was the exact secret I asked you to remember earlier? Reply with just the secret."

    @abc.abstractmethod
    def adopt_session_arg(self, preserved_dir: Path) -> str:
        """Return the value to hand the adopting create via ``--adopt``.

        Computed from the just-preserved agent dir (``preserved/<name>--<id>/``): either a
        session/conversation id the plugin can resolve, or an absolute path to the agent's
        native session file/dir under ``preserved_dir``.
        """

    def prepare_adoption_workspace(self, work_dir: Path) -> None:
        """Seed the fresh worktree the adopting agent is created against.

        A *distinct* dir from the original workspace, so adoption must rebind the native
        session's original-cwd binding rather than getting it for free. Defaults to an
        empty git repo; override to add agent-specific trust inputs.
        """
        init_git_repo(work_dir, initial_commit=True)


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


# Predicates over the current common-transcript records. Module-level (the call sites
# adapt them to poll_until's no-arg condition with a small lambda).
def _seed_turn_captured(records: list[dict[str, Any]], secret: str, forces_tool_call: bool) -> bool:
    has_user_secret = any(r["type"] == "user_message" and secret in str(r.get("content", "")) for r in records)
    has_assistant = any(r["type"] == "assistant_message" for r in records)
    has_tool = (not forces_tool_call) or any(r["type"] == "tool_result" for r in records)
    return has_user_secret and has_assistant and has_tool


def _assistant_recalled_secret(records: list[dict[str, Any]], secret: str) -> bool:
    return any(r["type"] == "assistant_message" and secret in str(r.get("text", "")) for r in records)


def _wait_for_records(
    host_dir: Path,
    subdir: str,
    predicate: Callable[[list[dict[str, Any]]], bool],
    *,
    timeout: float,
    description: str,
) -> list[dict[str, Any]]:
    """Poll the common transcript until ``predicate`` holds; return the records (or fail)."""
    found = poll_until(
        condition=lambda: predicate(_read_common_records(host_dir, subdir)), timeout=timeout, poll_interval=2.0
    )
    records = _read_common_records(host_dir, subdir)
    if not found:
        raise AssertionError(
            f"{description} within {timeout}s. Last transcript:\n{json.dumps(records, indent=2)[:4000]}"
        )
    return records


def _assert_records_conform(records: list[dict[str, Any]]) -> None:
    for record in records:
        error = validate_common_transcript_record(record)
        assert error is None, f"emitted record does not match the canonical schema ({error}): {record}"


def _preserved_agent_dir(host_dir: Path, agent_name: str) -> Path:
    """Return the unique ``preserved/<agent_name>--<id>/`` dir, asserting it exists.

    Mirrors the on-disk layout the preservation module writes (see
    ``imbue.mngr.api.preservation.get_preserved_agent_dir``); the id is unknown to the
    test, so it globs by the ``<name>--`` prefix and requires exactly one match.
    """
    preserved_root = host_dir / "preserved"
    matches = (
        [path for path in preserved_root.iterdir() if path.is_dir() and path.name.startswith(f"{agent_name}--")]
        if preserved_root.exists()
        else []
    )
    assert len(matches) == 1, (
        f"expected exactly one preserved dir for {agent_name} under {preserved_root}, found {matches}"
    )
    return matches[0]


def _assert_transcripts_preserved(
    host_dir: Path, agent_name: str, subdir: str, secret: str, native_session_relpaths: Sequence[str]
) -> None:
    """Assert destroy preserved the agent's transcripts + native session store to preserved/.

    The destroy that triggers this logs preservation failures as warnings and does not
    abort, so without this assertion a broken preservation path passes silently. Keying
    on the seeded ``secret`` proves real transcript *content* (not just an empty tree)
    survived to the preserved location. Both the common transcript (canonical, what the
    arc reads from the live state dir) and the raw transcript directory
    (``build_transcript_preserved_items``'s other half) are checked, plus each
    ``native_session_relpaths`` entry (the agent's own resumable session store).

    This asserts the bytes landed on disk; that they actually *resume* is exercised
    separately by the adopt-from-preserved arc step, which adopts this store into a fresh
    agent and asserts it recalls the pre-destroy secret. Resuming into a fresh agent (a new
    worktree) takes more than a byte copy: each plugin rebinds the session's recorded
    working directory (and resume pointer) to the new work_dir so its CLI resumes cleanly
    instead of stalling on a missing-directory prompt -- the per-agent rebind specifics live
    in each plugin's adoption path.
    """
    preserved_dir = _preserved_agent_dir(host_dir, agent_name)
    common = preserved_dir / "events" / subdir / "common_transcript" / "events.jsonl"
    assert common.exists(), (
        f"common transcript not preserved at {common}; preserved tree: {list(preserved_dir.rglob('*'))}"
    )
    common_text = common.read_text()
    assert secret in common_text, (
        f"preserved common transcript missing the seeded secret {secret!r}: {common_text[:2000]}"
    )
    raw_dir = preserved_dir / "logs" / f"{subdir}_transcript"
    assert raw_dir.is_dir() and any(raw_dir.iterdir()), (
        f"raw transcript dir not preserved (or empty) at {raw_dir}; preserved tree: {list(preserved_dir.rglob('*'))}"
    )
    for relpath in native_session_relpaths:
        native = preserved_dir / relpath
        non_empty = (native.is_file() and native.stat().st_size > 0) or (native.is_dir() and any(native.iterdir()))
        assert non_empty, (
            f"native session store not preserved (or empty) at {native}; "
            f"preserved tree: {list(preserved_dir.rglob('*'))}"
        )


def _adopt_preserved_and_recall(
    profile: AgentReleaseProfile,
    ctx: AgentReleaseContext,
    *,
    subdir: str,
    secret: str,
    preserved_dir: Path,
    tmp_path: Path,
) -> None:
    """Create a fresh agent that adopts the just-preserved session, then assert it recalls
    the pre-destroy secret.

    Runs after the source agent is destroyed (so its live state dir is gone and the only
    agent under ``agents/`` is this adopting one). The adopting agent is created against a
    *new* worktree -- exercising the per-agent original-cwd rebind -- with the resolved
    adopt argument passed via ``--adopt``. No secret is seeded: recall must succeed
    purely from the adopted session's restored context.
    """
    host_dir = ctx.host_dir
    adopt_work = tmp_path / "adopt_work"
    profile.prepare_adoption_workspace(adopt_work)
    # Reconstruct (rather than model_copy(update=...)) so the new context is re-validated:
    # same plumbing as the source run, but pointed at the fresh adoption worktree.
    adopt_ctx = AgentReleaseContext(env=ctx.env, workspace=adopt_work, host_dir=ctx.host_dir, teardown=ctx.teardown)
    adopt_name = f"{profile.agent_type.replace('-', '')}-adopt-{get_short_random_string()}"
    created = False
    try:
        create = profile.run_mngr(
            adopt_ctx,
            "create",
            adopt_name,
            profile.agent_type,
            "--no-connect",
            "--yes",
            "--adopt",
            profile.adopt_session_arg(preserved_dir),
            *profile.create_extra_args(adopt_ctx),
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        assert create.returncode == 0, f"adopt create failed:\n{create.stdout}\n{create.stderr}"
        created = True

        recall = profile.run_mngr(
            adopt_ctx, "message", adopt_name, "--message", profile.recall_prompt(), timeout=_MESSAGE_TIMEOUT_SECONDS
        )
        assert recall.returncode == 0, f"adopt recall message failed:\n{recall.stdout}\n{recall.stderr}"
        _wait_for_records(
            host_dir,
            subdir,
            lambda records: _assistant_recalled_secret(records, secret),
            timeout=_RESPONSE_TIMEOUT_SECONDS,
            description=f"adopting agent did not recall the secret {secret!r} from the preserved session",
        )
    finally:
        if created:
            profile.run_mngr(adopt_ctx, "destroy", adopt_name, "--force", timeout=_LIFECYCLE_TIMEOUT_SECONDS)


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
    destroyed = False

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

        # 2. Seed the secret and observe the RUNNING marker (every agent surfaces it).
        seed = profile.run_mngr(
            ctx, "message", agent_name, "--message", profile.seed_prompt(secret), timeout=_MESSAGE_TIMEOUT_SECONDS
        )
        assert seed.returncode == 0, f"seed message failed:\n{seed.stdout}\n{seed.stderr}"
        assert poll_until(
            condition=_marker_path(host_dir).exists, timeout=_RUNNING_TIMEOUT_SECONDS, poll_interval=0.2
        ), "active marker never appeared -> agent never reported RUNNING"

        # 3. The seed turn must be captured: user_message carries the secret, plus a reply
        #    (and a tool_result when the agent forces a tool call).
        records = _wait_for_records(
            host_dir,
            subdir,
            lambda records: _seed_turn_captured(records, secret, profile.forces_tool_call),
            timeout=_RESPONSE_TIMEOUT_SECONDS,
            description="seed turn was not captured",
        )

        # 4. The captured records must all match the canonical envelope, plus the forced-tool
        #    assertions (the call nested on the assistant turn, and its result) when applicable.
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
            lambda records: _assistant_recalled_secret(records, secret),
            timeout=_RESPONSE_TIMEOUT_SECONDS,
            description=f"agent did not recall the secret {secret!r} after stop/start (resume failed)",
        )

        # 7. History survived the restart: the pre-restart seed user_message is still present
        #    (guards the opencode rebuild-on-idle raw-seeding; trivially holds for append-only emitters).
        assert any(r["type"] == "user_message" and secret in str(r.get("content", "")) for r in post), (
            "pre-restart turn was lost from the common transcript after stop/start"
        )
        _assert_records_conform(post)

        # 8. Destroy must preserve the agent's transcripts to the local preserved/ dir before the
        #    state dir is deleted (the preserve-on-destroy feature). Done here in the try -- not as
        #    bare cleanup in the finally -- so the preservation assertion runs only on the success
        #    path and a swallowed preservation failure can no longer pass silently. The finally
        #    still force-destroys for the failure path (guarded by ``destroyed``).
        destroy = profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=_LIFECYCLE_TIMEOUT_SECONDS)
        assert destroy.returncode == 0, f"destroy failed:\n{destroy.stdout}\n{destroy.stderr}"
        destroyed = True
        _assert_transcripts_preserved(host_dir, agent_name, subdir, secret, profile.native_session_preserved_relpaths)

        # 9. Adopt the just-preserved session into a fresh agent (new worktree) and prove
        #    it recalls the pre-destroy secret -- that the preserved store *resumes*, not
        #    just that its bytes survived. Every agent must support this (its plugin
        #    implements the resolve + cwd-rebind path that ``--adopt`` triggers).
        _adopt_preserved_and_recall(
            profile,
            ctx,
            subdir=subdir,
            secret=secret,
            preserved_dir=_preserved_agent_dir(host_dir, agent_name),
            tmp_path=tmp_path,
        )
    finally:
        try:
            if not destroyed:
                profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=_LIFECYCLE_TIMEOUT_SECONDS)
        finally:
            if ctx.teardown is not None:
                ctx.teardown()

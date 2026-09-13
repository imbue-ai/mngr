"""Read a workspace's version history from its own git, from the minds hub.

A workspace is created from the default-workspace-template at a pinned ref (the
immutable ``original_minds_version`` label). Later upgrades are ``git pull``s
from the ``upstream`` remote (the ``update-self`` skill), which land as merge
commits on the workspace's primary branch. So the *current* version and the
*upgrade history* live in the workspace's git, not in any minds-side record.

Two things in that git can name the current version: the ``update-self:``
merge subject the skill writes (which outranks) and the nearest reachable
``minds-v*`` tag (absent in a clone built by fetching a single ref).

The hub reads them on demand by running ``git`` inside the (online) workspace
via ``mngr exec``. This is best-effort: an offline workspace, a workspace that
has neither marker nor tag, or any exec failure yields a ``None`` current
version and an empty history -- callers fall back to the
``original_minds_version`` label, the one version fact knowable offline.
"""

import json
import re
import shlex
from datetime import datetime
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.event_envelope import parse_iso_timestamp
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.primitives import AgentId

# Field separator used in the ``git log`` format string; a tab cannot appear in
# a commit hash or ISO timestamp, and we only keep the subject's first line.
_GIT_LOG_FIELD_SEPARATOR: str = "\t"

# Bounds the in-container git work. Reading refs + a bounded log is fast; a
# generous ceiling still surfaces a wedged exec instead of hanging the route.
_GIT_EXEC_TIMEOUT_SECONDS: float = 30.0

# The subject ``update-self`` writes when a run lands, verbatim from the skill
# (``.agents/skills/update-self/references/update-self-worker.md``). Matched in
# full: the template's own history carries other ``update-self:`` subjects on
# the workspace's first-parent path, and only the newest match is read.
_UPDATE_SELF_SUBJECT_PREFIX: Final[str] = "update-self: merge upstream template ("

# git commands run inside the workspace: the newest ``update-self`` marker, the
# nearest ``minds-v*`` tag, and the upgrade merges since creation.
_GIT_UPDATE_SELF_ARGS: tuple[str, ...] = (
    "git",
    "log",
    "--first-parent",
    f"--grep=^{_UPDATE_SELF_SUBJECT_PREFIX}",
    "-1",
    "--format=%s",
)
_GIT_DESCRIBE_ARGS: tuple[str, ...] = ("git", "describe", "--tags", "--match", "minds-v*", "--abbrev=0")
# Both version reads in one exec: the marker line (empty when there is none)
# and then the tag. ``describe`` fails loudly on a tagless clone, which is an
# ordinary state here, so its stderr is dropped and its exit code discarded --
# the command's stdout is the whole answer.
_GIT_CURRENT_VERSION_COMMAND: str = (
    f"{shlex.join(_GIT_UPDATE_SELF_ARGS)}; {shlex.join(_GIT_DESCRIBE_ARGS)} 2>/dev/null || true"
)
_GIT_MERGES_FORMAT: str = f"%H{_GIT_LOG_FIELD_SEPARATOR}%cI{_GIT_LOG_FIELD_SEPARATOR}%s"
_GIT_MERGES_ARGS: tuple[str, ...] = ("git", "log", "--merges", "--first-parent", f"--format={_GIT_MERGES_FORMAT}")

# Anchored on the subject: ``--grep`` also matches a body that merely quotes the marker.
_UPDATE_SELF_SUBJECT_RE: Final[re.Pattern[str]] = re.compile(
    rf"^{re.escape(_UPDATE_SELF_SUBJECT_PREFIX)}([^()]+)\)\s*$"
)


class UpgradeMerge(FrozenModel):
    """One upgrade merge commit on the workspace's primary branch."""

    commit_sha: str = Field(description="Full commit hash of the merge")
    committed_at: datetime | None = Field(description="Commit time (UTC), if parseable")
    summary: str = Field(description="First line of the merge commit message")


class WorkspaceGitVersion(FrozenModel):
    """Best-effort version facts read from a workspace's own git."""

    current_minds_version: str | None = Field(
        default=None,
        description="Ref named by the newest ``update-self:`` marker, else the nearest reachable "
        "``minds-v*`` tag; None when neither can be read",
    )
    upgrade_merges: tuple[UpgradeMerge, ...] = Field(
        default=(),
        description="Merge commits on the primary branch, newest first (the recorded upgrade history)",
    )


def parse_git_describe(stdout: str) -> str | None:
    """Return the tag named by ``git describe``, or None when there is none."""
    text = stdout.strip()
    return text or None


def parse_update_self_ref(stdout: str) -> str | None:
    """Return the ref named by an ``update-self:`` marker subject (as written, ``main`` included), or None."""
    match = _UPDATE_SELF_SUBJECT_RE.match(stdout.strip())
    return match.group(1).strip() or None if match is not None else None


def parse_current_version(stdout: str) -> str | None:
    """The version named by the combined marker + ``describe`` output, or None when neither line is there.

    The marker outranks the tag: a workspace that updated to ``main`` still
    describes the release it was created at. The two are told apart by shape:
    a marker line starts with the marker's subject prefix and a tag never does,
    so a marker the grep selected but the strict parse rejected is not mistaken
    for the tag.
    """
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    for line in lines:
        marker_ref = parse_update_self_ref(line)
        if marker_ref is not None:
            return marker_ref
    tag_line = next((line for line in lines if not line.startswith(_UPDATE_SELF_SUBJECT_PREFIX)), None)
    return parse_git_describe(tag_line) if tag_line is not None else None


def parse_upgrade_merges(stdout: str) -> tuple[UpgradeMerge, ...]:
    """Parse the tab-separated ``git log --merges`` output into typed records.

    Each line is ``<sha>\\t<iso-time>\\t<subject>``. Lines that don't carry at
    least a sha and a time field are skipped (defensive against unexpected git
    output); the subject may legitimately be empty.
    """
    merges: list[UpgradeMerge] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(_GIT_LOG_FIELD_SEPARATOR, 2)
        if len(parts) < 2:
            continue
        commit_sha = parts[0].strip()
        if not commit_sha:
            continue
        summary = parts[2] if len(parts) >= 3 else ""
        merges.append(
            UpgradeMerge(
                commit_sha=commit_sha,
                committed_at=parse_iso_timestamp(parts[1]),
                summary=summary,
            )
        )
    return tuple(merges)


def read_workspace_current_version(
    *,
    agent_id: AgentId,
    mngr_caller: MngrCaller,
) -> str | None:
    """Read the version a workspace's own git reports, in one ``mngr exec``.

    Separate from :func:`read_workspace_git_version` so callers that only need
    the version skip the history exec.
    """
    stdout = _exec_git_in_workspace(
        agent_id=agent_id, git_command=_GIT_CURRENT_VERSION_COMMAND, mngr_caller=mngr_caller
    )
    return parse_current_version(stdout) if stdout is not None else None


def read_workspace_git_version(
    *,
    agent_id: AgentId,
    mngr_caller: MngrCaller,
) -> WorkspaceGitVersion:
    """Read current version + upgrade history from a workspace's git via ``mngr exec``.

    Best-effort: any exec or git failure (offline workspace, no marker and no
    tags, etc.) is logged at debug and yields the empty/None defaults rather
    than raising, so the version route can always at least report
    ``original_minds_version``.
    """
    current_version = read_workspace_current_version(agent_id=agent_id, mngr_caller=mngr_caller)
    merges = _exec_git_merges(agent_id=agent_id, mngr_caller=mngr_caller)
    return WorkspaceGitVersion(current_minds_version=current_version, upgrade_merges=merges)


def _exec_git_in_workspace(
    *,
    agent_id: AgentId,
    git_command: str,
    mngr_caller: MngrCaller,
) -> str | None:
    """Run a git shell command inside the workspace via ``mngr exec``; return its stdout or None on failure.

    Runs through the shared warm-process ``mngr_caller``, which surfaces a
    launch/exec failure as a non-zero ``returncode`` (rather than raising), so
    the best-effort None fallback covers every failure mode.
    """
    # ``mngr exec`` takes the command as a single trailing COMMAND argument (its
    # CLI is ``mngr exec [AGENTS]... COMMAND``) and runs it in a shell, so the
    # git command is one shell string -- extra tokens would be parsed as
    # additional agent names and the whole call would error out.
    # --no-start: ``mngr exec`` auto-starts a stopped host by default, and a
    # best-effort version read must not cold-boot a container as a side effect.
    # ``--format json`` keeps the captured stdout clean: in its default (human)
    # format ``mngr exec`` appends a ``Command succeeded on agent <name>``
    # status line to stdout after the command's own output.
    result = mngr_caller.call(
        ["exec", "--no-start", str(agent_id), git_command, "--format", "json"],
        timeout=_GIT_EXEC_TIMEOUT_SECONDS,
    )
    if result.is_timed_out or result.returncode != 0:
        logger.debug(
            "{} in machine {} failed (timed_out={}, rc={})",
            git_command,
            agent_id,
            result.is_timed_out,
            result.returncode,
        )
        return None
    try:
        envelope = json.loads(result.stdout)
        return str(envelope["results"][0]["stdout"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        # Warning (not debug): a zero-exit exec whose ``--format json`` envelope
        # does not parse is a broken mngr output contract, not a normal
        # offline/no-tags fallback.
        logger.warning("{} in machine {} produced an unparseable exec envelope: {}", git_command, agent_id, e)
        return None


def _exec_git_merges(*, agent_id: AgentId, mngr_caller: MngrCaller) -> tuple[UpgradeMerge, ...]:
    stdout = _exec_git_in_workspace(
        agent_id=agent_id, git_command=shlex.join(_GIT_MERGES_ARGS), mngr_caller=mngr_caller
    )
    return parse_upgrade_merges(stdout) if stdout is not None else ()

"""Read a workspace's version history from its own git, from the minds hub.

A workspace is created from the default-workspace-template at a pinned ref (the
immutable ``original_minds_version`` label). Later upgrades are ``git pull``s
from the ``upstream`` remote (the ``update-self`` skill), which land as merge
commits on the workspace's primary branch. So the *current* version and the
*upgrade history* live in the workspace's git, not in any minds-side record.

Two things in that git can name the current version: the ``update-self:``
merge subject the skill writes (which outranks) and the nearest reachable
``minds-v*`` tag. The tag can be missing from a clone that holds the commit it
points at -- a workspace created from a published template carries the
template's whole history but none of its tags -- so a read that finds neither,
in a workspace whose tree came from the template and holds enough history for a
tag to describe it, adds minds' ``official`` remote, fetches the release tags
from it and describes again. That is the one thing the read writes into the
workspace.

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
from imbue.minds.desktop_client.backup_workspace_scripts import OFFICIAL_REMOTE_NAME
from imbue.minds.desktop_client.backup_workspace_scripts import OFFICIAL_REMOTE_URL
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.primitives import AgentId

# Field separator used in the ``git log`` format string; a tab cannot appear in
# a commit hash or ISO timestamp, and we only keep the subject's first line.
_GIT_LOG_FIELD_SEPARATOR: str = "\t"

# Bounds an in-container read of refs + a bounded log, which is fast; the
# ceiling is there to surface a wedged exec instead of hanging the route.
_GIT_EXEC_TIMEOUT_SECONDS: float = 30.0
# The version read gets its own, larger ceiling: it can include a tag fetch
# over the network (measured at 17 MB where it can succeed), and a read killed
# mid-fetch keeps nothing, so a ceiling that cannot cover the transfer turns a
# one-time cost into one paid again every sweep. Both bounds stay under the
# sweep's 300s interval so a read can never overlap its own next pass, which
# would have two fetches writing one workspace's refs.
_GIT_VERSION_EXEC_TIMEOUT_SECONDS: float = 150.0

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

# Only the release tags: nothing else the official template holds can name a
# version. It is still not a cheap fetch -- the pattern takes every release,
# including the ones newer than the workspace's base, whose commits the clone
# does not have: measured at 17 MB into a clone published from ``minds-v0.5.0``.
_OFFICIAL_TAG_REFSPEC: Final[str] = "refs/tags/minds-v*:refs/tags/minds-v*"
# The fetch's own ceiling, derived so it stays under the exec's by enough that the
# command still gets to print what it has. 17 MB inside it needs about 1.2 Mbit/s.
_GIT_TAG_FETCH_TIMEOUT_SECONDS: Final[int] = int(_GIT_VERSION_EXEC_TIMEOUT_SECONDS) - 30
# ``timeout`` is GNU-only and a host's shell may be a BSD userland's, so the
# bound is the perl form the style guide's portable-shell section prescribes,
# traps and all.
_TIME_BOUNDED_GIT_FETCH_TAGS_ARGS: tuple[str, ...] = (
    "perl",
    "-MTime::HiRes=alarm",
    "-e",
    "alarm shift; exec @ARGV or exit 127",
    str(_GIT_TAG_FETCH_TIMEOUT_SECONDS),
    "git",
    "fetch",
    "--quiet",
    OFFICIAL_REMOTE_NAME,
    _OFFICIAL_TAG_REFSPEC,
)
_GIT_DESCRIBE_INTO_TAG_COMMAND: str = f'tag="$({shlex.join(_GIT_DESCRIBE_ARGS)} 2>/dev/null)"'
# What the fetch needs to be able to work at all, beyond there being nothing to
# read: the workspace has to descend from the template, and hold enough history
# for a tag to describe HEAD.
#
# ``parent.toml`` is the template's own record of where it came from, which a
# published template keeps. It moved into ``system/config/`` at minds-v0.3.10
# and sat at the root before that, so both paths count. Without this, a
# workspace created from a user's own repo -- no marker, no tag, nothing to do
# with the template -- pulls the release tags too: measured at 70 MB of objects
# written into that repo, after which ``describe`` still has nothing to say.
#
# A shallow clone is the other dead end: the tags land and ``describe`` still
# cannot relate them to HEAD, because the history between is not there.
_GIT_FETCH_IS_WORTH_TRYING_CONDITION: str = (
    "{ [ -f system/config/parent.toml ] || [ -f parent.toml ]; } "
    '&& [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" != true ]'
)
# ``set-url`` first, which repoints a remote left pointing anywhere else (the
# fetch must reach the official template whatever the workspace was created
# from) and fails only when there is no such remote, which the ``add`` covers.
_GIT_ENSURE_OFFICIAL_REMOTE_COMMAND: str = (
    f"{shlex.join(('git', 'remote', 'set-url', OFFICIAL_REMOTE_NAME, OFFICIAL_REMOTE_URL))} >/dev/null 2>&1 "
    f"|| {shlex.join(('git', 'remote', 'add', OFFICIAL_REMOTE_NAME, OFFICIAL_REMOTE_URL))} >/dev/null 2>&1"
)
# Both version reads in one exec: the marker line (empty when there is none)
# and then the tag. Every git step is allowed to fail -- a tagless clone, a
# workspace with no network -- so the command ends on the ``printf`` that
# carries the whole answer. Silenced are the steps whose failure is an ordinary
# state of a workspace: a ``describe`` with no tag, a ``set-url`` with no such
# remote. The marker log and the fetch keep their stderr, the two whose failure
# is not.
#
# The marker outranks the tag, so a workspace that has one has nothing to gain
# from the fetch -- which is why the condition is both being empty, not the tag
# alone.
_GIT_CURRENT_VERSION_COMMAND: str = (
    f'marker="$({shlex.join(_GIT_UPDATE_SELF_ARGS)})"; '
    f"{_GIT_DESCRIBE_INTO_TAG_COMMAND}; "
    f'if [ -z "$marker" ] && [ -z "$tag" ] && {_GIT_FETCH_IS_WORTH_TRYING_CONDITION}; then '
    f"{_GIT_ENSURE_OFFICIAL_REMOTE_COMMAND}; "
    f"{shlex.join(_TIME_BOUNDED_GIT_FETCH_TAGS_ARGS)} >/dev/null; "
    f"{_GIT_DESCRIBE_INTO_TAG_COMMAND}; "
    "fi; "
    'printf \'%s\\n\' "$marker" "$tag"'
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
    the version skip the history exec. Not read-only: a template-derived
    workspace that reports no version has the release tags fetched into it first.
    """
    stdout = _exec_git_in_workspace(
        agent_id=agent_id,
        git_command=_GIT_CURRENT_VERSION_COMMAND,
        timeout_seconds=_GIT_VERSION_EXEC_TIMEOUT_SECONDS,
        mngr_caller=mngr_caller,
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
    timeout_seconds: float,
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
        timeout=timeout_seconds,
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
        command_result = json.loads(result.stdout)["results"][0]
        stdout = str(command_result["stdout"])
        stderr = str(command_result["stderr"]).strip()
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        # Warning (not debug): a zero-exit exec whose ``--format json`` envelope
        # does not parse is a broken mngr output contract, not a normal
        # offline/no-tags fallback.
        logger.warning("{} in machine {} produced an unparseable exec envelope: {}", git_command, agent_id, e)
        return None
    if stderr:
        # The command exits clean whatever git made of it, so this is the only
        # record of why a version read came back empty.
        logger.debug("{} in machine {} wrote to stderr: {}", git_command, agent_id, stderr)
    return stdout


def _exec_git_merges(*, agent_id: AgentId, mngr_caller: MngrCaller) -> tuple[UpgradeMerge, ...]:
    stdout = _exec_git_in_workspace(
        agent_id=agent_id,
        git_command=shlex.join(_GIT_MERGES_ARGS),
        timeout_seconds=_GIT_EXEC_TIMEOUT_SECONDS,
        mngr_caller=mngr_caller,
    )
    return parse_upgrade_merges(stdout) if stdout is not None else ()

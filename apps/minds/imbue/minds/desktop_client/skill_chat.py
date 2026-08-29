"""Spawn an in-workspace chat that drives a template skill, and probe for the skill first.

The app runs ``mngr create`` *inside* the workspace's container (via ``mngr exec``)
so the create resolves the template's ``chat`` create-template and lands in the
right work dir, exactly as the workspace's own UI creates chats, while the
coupling stays at the mngr CLI level. ``mngr exec`` runs its COMMAND through a
shell on the host, so the inner create is one ``shlex.join``-ed string and the
seed message cannot break out of its ``--message`` argument.

A workspace created from a template older than the skill would accept the inner
``mngr create`` and then hang on the unknown slash command, leaving a
half-created chat behind, so callers probe first and refuse rather than spawn a
chat that can only fail. The probe echoes a sentinel rather than relying on the
exit code, which would conflate "file absent" with "probe never ran".
"""

import secrets
import shlex
from enum import auto
from typing import Final

from loguru import logger

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.primitives import AgentId

# A single filesystem check inside an already-running container, so it should
# return near-instantly; a low ceiling makes an unreachable workspace fail fast.
_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0

# The inner create spawns a fresh chat agent (tmux window, claude process) on an
# existing host: no provisioning or git transfer, but slower than a plain message.
_SPAWN_TIMEOUT_SECONDS: Final[float] = 120.0

# Bare ``mngr`` resolves on the container's PATH (set up by ``mngr exec``'s
# source-env prefix); the desktop app's outer binary path does not exist there.
_CONTAINER_MNGR_BINARY: Final[str] = "mngr"

# Both labels make the system interface auto-open the chat's tab: shipped
# interfaces key on ``assist``, newer ones on the purpose-neutral ``auto_open``.
AUTO_OPEN_CHAT_LABELS: Final[tuple[str, ...]] = ("assist", "auto_open")


class SkillSupport(UpperCaseStrEnum):
    """Whether a workspace can host a chat driving a given template skill."""

    SUPPORTED = auto()
    """The workspace has the skill; spawning a chat will work."""
    UNSUPPORTED = auto()
    """The workspace is reachable but predates the skill."""
    UNREACHABLE = auto()
    """The probe could not run (host/workspace down); support is unknown."""


def _sentinel(skill_name: str, state: str) -> str:
    return f"MNGR_{skill_name.upper().replace('-', '_')}_SKILL_{state}"


def build_skill_support_probe_args(workspace_agent_id: AgentId, skill_name: str) -> list[str]:
    """Build the ``mngr`` CLI args that probe a workspace for ``skill_name``.

    Runs, in the workspace's work_dir (where ``mngr exec`` lands by default), a
    shell ``test`` for the skill's SKILL.md that echoes a present/absent sentinel.
    """
    skill_path = f".agents/skills/{skill_name}/SKILL.md"
    check = (
        f"if [ -f {shlex.quote(skill_path)} ]; "
        f"then echo {_sentinel(skill_name, 'PRESENT')}; else echo {_sentinel(skill_name, 'ABSENT')}; fi"
    )
    # --no-start: probes run eagerly (a modal opening, a dispatch), and a
    # support check must never cold-boot a container as a side effect.
    return ["exec", "--agent", str(workspace_agent_id), check, "--no-start"]


def check_skill_support(mngr_caller: MngrCaller, workspace_agent_id: AgentId, skill_name: str) -> SkillSupport:
    """Probe ``workspace_agent_id`` for ``skill_name`` and classify the result."""
    result = mngr_caller.call(
        build_skill_support_probe_args(workspace_agent_id, skill_name), timeout=_PROBE_TIMEOUT_SECONDS
    )
    if _sentinel(skill_name, "PRESENT") in result.stdout:
        return SkillSupport.SUPPORTED
    if _sentinel(skill_name, "ABSENT") in result.stdout:
        return SkillSupport.UNSUPPORTED
    logger.warning(
        "The {} skill probe for machine {} produced no sentinel (exit {}): {}",
        skill_name,
        workspace_agent_id,
        result.returncode,
        result.stderr.strip(),
    )
    return SkillSupport.UNREACHABLE


def generate_chat_name(skill_name: str) -> str:
    """A unique-enough chat name for one run of ``skill_name`` (``<skill>-<hex>``)."""
    return f"{skill_name}-{secrets.token_hex(3)}"


def build_skill_chat_mngr_args(workspace_agent_id: AgentId, *, chat_name: str, message: str) -> list[str]:
    """Build the ``mngr`` CLI args (sans the leading ``mngr``) that spawn a chat seeded with ``message``.

    An ``exec`` targeting the workspace agent by id (a bare id is a valid agent
    address) whose single COMMAND argument is the inner ``mngr create`` shell
    string. The chat is grouped with its workspace by living in the same
    container, so no grouping label is needed.
    """
    inner_parts = [_CONTAINER_MNGR_BINARY, "create", chat_name, "--template", "chat", "--transfer", "none"]
    inner_parts.append("--no-connect")
    for label in AUTO_OPEN_CHAT_LABELS:
        inner_parts += ["--label", f"{label}=true"]
    inner_parts += ["--message", message]
    # --no-start: the create is only reachable after the support probe succeeded
    # (host running), so this guards the stop race; a chat create must never
    # cold-boot a host either.
    return ["exec", "--agent", str(workspace_agent_id), shlex.join(inner_parts), "--no-start"]


def spawn_skill_chat(mngr_caller: MngrCaller, workspace_agent_id: AgentId, *, chat_name: str, message: str) -> bool:
    """Spawn the chat and wait for ``mngr create`` to finish; return whether it succeeded.

    Synchronous on purpose: the caller holds its "starting..." state until the
    chat actually exists rather than dismissing into a blank gap before the tab
    appears. A non-zero exit is logged and reported as ``False``.
    """
    args = build_skill_chat_mngr_args(workspace_agent_id, chat_name=chat_name, message=message)
    result = mngr_caller.call(args, timeout=_SPAWN_TIMEOUT_SECONDS)
    if result.returncode != 0:
        logger.error(
            "Spawning chat {} in machine {} exited {}: {}",
            chat_name,
            workspace_agent_id,
            result.returncode,
            result.stderr.strip(),
        )
        return False
    return True

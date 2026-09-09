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

**The account binding.** A chat has to be told which provider account it runs
on. The workspace keeps one config dir per signed-in account and leaves
``CLAUDE_CONFIG_DIR`` unset workspace-wide, so a chat created without a binding
resolves ``~/.claude``, which holds no credential: every turn comes back "Not
logged in". The workspace's own UI binds each chat it creates, and so must
this create -- the account only exists inside the container, so
:func:`resolve_account_binding` asks the template's own resolver for the
arguments rather than reconstructing the choice out here.
"""

import secrets
import shlex
from collections.abc import Sequence
from enum import auto
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.in_workspace_mngr import build_in_workspace_mngr_command
from imbue.minds.desktop_client.in_workspace_mngr import in_workspace_failure_detail
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.primitives import AgentId

# A single filesystem check inside an already-running container, so it should
# return near-instantly; a low ceiling makes an unreachable workspace fail fast.
_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0

# The inner create spawns a fresh chat agent (tmux window, claude process) on an
# existing host: no provisioning or git transfer, but slower than a plain message.
_SPAWN_TIMEOUT_SECONDS: Final[float] = 120.0

# The account probe runs the template's resolver under ``uv run``, so it pays a Python start.
_ACCOUNT_PROBE_TIMEOUT_SECONDS: Final[float] = 90.0

# The template script that turns the workspace's default account into ``mngr create``
# arguments. Its path is the contract; the package behind it is not.
_ACCOUNT_ARGS_SCRIPT: Final[str] = "system/scripts/default_account_args.py"

# The ``chat`` create-template makes a claude agent, so the binding this asks for is claude's.
_CHAT_HARNESS: Final[str] = "claude"

# Fence the resolver's own output, so an empty answer stays distinguishable from
# a probe that never ran.
ACCOUNT_ARGS_BEGIN_SENTINEL: Final[str] = "MNGR_ACCOUNT_ARGS_BEGIN"
ACCOUNT_ARGS_END_SENTINEL: Final[str] = "MNGR_ACCOUNT_ARGS_END"

# CLEANUP: drop this sentinel, the probe branch that echoes it, and the NOT_REQUIRED
# state it produces once no supported workspace predates the per-account config dirs
# minds-v0.5.0 introduced.
NO_ACCOUNT_STORE_SENTINEL: Final[str] = "MNGR_NO_ACCOUNT_STORE"

# The resolver exits 0 on every reason it has for declining and non-zero only when
# it broke, so its status is what tells "no account" from "the probe fell over".
ACCOUNT_ARGS_EXIT_SENTINEL: Final[str] = "MNGR_ACCOUNT_ARGS_EXIT="

# What the resolver emits per account: the flag, then its ``NAME=VALUE``.
_ACCOUNT_ARG_FLAG: Final[str] = "--env"

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


class AccountBindingState(UpperCaseStrEnum):
    """Whether a chat spawned in this workspace can be pointed at a signed-in account."""

    BOUND = auto()
    """The account resolved; its ``mngr create`` arguments are on the binding."""
    NOT_REQUIRED = auto()
    """The template has no account store, so its one shared config dir holds the credential."""
    UNAVAILABLE = auto()
    """The workspace keeps accounts but named none this chat could run on."""
    UNREACHABLE = auto()
    """The probe did not run, did not complete, or answered unreadably; the binding is unknown."""


class AccountBinding(FrozenModel):
    """How a chat about to be spawned must be bound to the user's provider account."""

    state: AccountBindingState = Field(description="Whether an account could be resolved, and why not")
    create_args: tuple[str, ...] = Field(
        default=(),
        description="``mngr create`` arguments that bind the chat; empty unless the state is BOUND",
    )


def build_account_binding_probe_args(workspace_agent_id: AgentId) -> list[str]:
    """Build the ``mngr`` CLI args that ask a workspace which account a new chat should run on.

    Runs the template's own resolver, in the workspace's work_dir (where ``mngr
    exec`` lands by default), and fences its output so an empty answer is still
    an answer. The resolver's exit status is echoed past the fence, keeping the
    fenced body its arguments alone. Templates predating the account store have
    no such script and say so with their own sentinel, because there a chat with
    no binding is the correctly authenticated one.
    """
    script = shlex.quote(_ACCOUNT_ARGS_SCRIPT)
    probe = (
        f"if [ -f {script} ]; then "
        f"echo {ACCOUNT_ARGS_BEGIN_SENTINEL}; "
        f"uv run python {script} {shlex.quote(_CHAT_HARNESS)}; "
        f"account_args_status=$?; "
        f"echo {ACCOUNT_ARGS_END_SENTINEL}; "
        f"echo {ACCOUNT_ARGS_EXIT_SENTINEL}$account_args_status; "
        f"else echo {NO_ACCOUNT_STORE_SENTINEL}; fi"
    )
    # --no-start, like every other probe here: resolving a binding must not cold-boot a container.
    return ["exec", "--agent", str(workspace_agent_id), probe, "--no-start"]


def _parse_account_args(stdout: str) -> tuple[str, ...] | None:
    """The resolver's arguments from a fenced probe answer, or None when it never answered.

    Nothing between the fences is a binding of zero arguments rather than a missing
    answer; whether that stands for a decline or a fall-over is the exit status's to say.
    """
    if ACCOUNT_ARGS_BEGIN_SENTINEL not in stdout or ACCOUNT_ARGS_END_SENTINEL not in stdout:
        return None
    body = stdout.split(ACCOUNT_ARGS_BEGIN_SENTINEL, 1)[1].split(ACCOUNT_ARGS_END_SENTINEL, 1)[0]
    return tuple(line.strip() for line in body.splitlines() if line.strip())


def _parse_resolver_exit_code(stdout: str) -> int | None:
    """The exit status the probe echoed for the resolver, or None when it echoed none."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(ACCOUNT_ARGS_EXIT_SENTINEL):
            code = stripped.removeprefix(ACCOUNT_ARGS_EXIT_SENTINEL)
            return int(code) if code.lstrip("-").isdigit() else None
    return None


def _is_well_formed_account_args(args: Sequence[str]) -> bool:
    """Whether ``args`` are the flag/``NAME=VALUE`` pairs the resolver documents."""
    if len(args) % 2 != 0:
        return False
    flags = args[::2]
    values = args[1::2]
    return all(flag == _ACCOUNT_ARG_FLAG for flag in flags) and all(
        "=" in value and not value.startswith("=") for value in values
    )


def resolve_account_binding(mngr_caller: MngrCaller, workspace_agent_id: AgentId) -> AccountBinding:
    """Ask ``workspace_agent_id`` which account a chat spawned in it should run on."""
    result = mngr_caller.call(
        build_account_binding_probe_args(workspace_agent_id), timeout=_ACCOUNT_PROBE_TIMEOUT_SECONDS
    )
    if NO_ACCOUNT_STORE_SENTINEL in result.stdout:
        return AccountBinding(state=AccountBindingState.NOT_REQUIRED)
    args = _parse_account_args(result.stdout)
    if args is None:
        logger.warning(
            "The account probe for machine {} produced no sentinel (exit {}): {}",
            workspace_agent_id,
            result.returncode,
            result.stderr.strip(),
        )
        return AccountBinding(state=AccountBindingState.UNREACHABLE)
    exit_code = _parse_resolver_exit_code(result.stdout)
    if exit_code != 0:
        logger.error(
            "The account resolver in machine {} did not complete (exit {}): {}",
            workspace_agent_id,
            exit_code,
            result.stderr.strip(),
        )
        return AccountBinding(state=AccountBindingState.UNREACHABLE)
    if not args:
        # The resolver declines silently on stdout; which of its reasons applies is only on stderr.
        logger.warning(
            "The account resolver in machine {} named no account: {}", workspace_agent_id, result.stderr.strip()
        )
        return AccountBinding(state=AccountBindingState.UNAVAILABLE)
    if not _is_well_formed_account_args(args):
        # An unreadable answer is a broken resolver, not a machine with nobody signed in;
        # sending the user to sign in would be sending them after a fix that does nothing.
        logger.error("The account resolver in machine {} answered {}, which is not readable", workspace_agent_id, args)
        return AccountBinding(state=AccountBindingState.UNREACHABLE)
    return AccountBinding(state=AccountBindingState.BOUND, create_args=args)


def generate_chat_name(skill_name: str) -> str:
    """A unique-enough chat name for one run of ``skill_name`` (``<skill>-<hex>``)."""
    return f"{skill_name}-{secrets.token_hex(3)}"


def build_skill_chat_mngr_args(
    workspace_agent_id: AgentId, *, chat_name: str, message: str, account_args: Sequence[str]
) -> list[str]:
    """Build the ``mngr`` CLI args (sans the leading ``mngr``) that spawn a chat seeded with ``message``.

    An ``exec`` targeting the workspace agent by id (a bare id is a valid agent
    address) whose single COMMAND argument is the inner ``mngr create`` shell
    string. The chat is grouped with its workspace by living in the same
    container, so no grouping label is needed.

    ``account_args`` come from :func:`resolve_account_binding` and bind the chat
    to a signed-in account; without them the chat runs on a config dir that
    holds no credential.
    """
    inner_parts = ["create", chat_name, "--template", "chat", "--transfer", "none"]
    inner_parts.append("--no-connect")
    for label in AUTO_OPEN_CHAT_LABELS:
        inner_parts += ["--label", f"{label}=true"]
    inner_parts += list(account_args)
    inner_parts += ["--message", message]
    # --no-start: the create is only reachable after the support probe succeeded
    # (host running), so this guards the stop race; a chat create must never
    # cold-boot a host either.
    return [
        "exec",
        "--agent",
        str(workspace_agent_id),
        build_in_workspace_mngr_command(inner_parts),
        "--no-start",
    ]


class SkillChatSpawn(FrozenModel):
    """Whether the inner ``mngr create`` landed, and what the workspace said when it did not."""

    is_started: bool = Field(description="Whether the chat now exists in the workspace")
    failure_detail: str = Field(
        default="",
        description=(
            "The workspace's own verdict on a failed spawn, for the caller to show; '' on success and "
            "when the workspace gave none. "
            "Bounded and stripped of the outer mngr's chatter, so it can be rendered as-is"
        ),
    )


def spawn_skill_chat(
    mngr_caller: MngrCaller,
    workspace_agent_id: AgentId,
    *,
    chat_name: str,
    message: str,
    account_args: Sequence[str],
) -> SkillChatSpawn:
    """Spawn the chat and wait for ``mngr create`` to finish; report how it went.

    Synchronous on purpose: the caller holds its "starting..." state until the
    chat actually exists rather than dismissing into a blank gap before the tab
    appears.

    A failure carries the workspace's verdict rather than only logging it: the
    refusals that stick are the ones retrying cannot fix.
    """
    args = build_skill_chat_mngr_args(
        workspace_agent_id, chat_name=chat_name, message=message, account_args=account_args
    )
    result = mngr_caller.call(args, timeout=_SPAWN_TIMEOUT_SECONDS)
    if result.returncode != 0:
        logger.error(
            "Spawning chat {} in machine {} exited {}: {}",
            chat_name,
            workspace_agent_id,
            result.returncode,
            result.stderr.strip(),
        )
        # When no result came back, the stderr is MngrCaller's own account of
        # that -- a timeout's quotes the whole argv, the seed message with it --
        # and is not something the workspace said, so there is no verdict to carry.
        detail = in_workspace_failure_detail(result.stderr) if result.is_mngr_output else ""
        return SkillChatSpawn(is_started=False, failure_detail=detail)
    return SkillChatSpawn(is_started=True)

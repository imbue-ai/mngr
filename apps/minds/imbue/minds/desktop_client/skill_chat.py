"""Launch an in-workspace chat that drives a template skill, checking the workspace has the skill first.

One ``mngr exec`` does the whole launch inside the workspace's container. It checks for the
skill's ``SKILL.md`` first: a workspace created from a template older than the skill would
accept the create and then hang on the unknown slash command, leaving a half-created chat
behind, so the launch refuses instead. It then asks the workspace's own chat app for the chat,
through the template's ``system/scripts/message_chat.py --create``
(:func:`~imbue.minds.desktop_client.chat_app.build_message_chat_command`, the same run that
messages a chat, plus ``--create``): the chat app mints the chat's id, binds the workspace's
default account and harness, labels it, seeds the message, and answers once its ``mngr
create`` has finished, exactly as a launcher-started chat is made. On a template whose script
predates the create mode (or has no script), the same shell runs a bare ``mngr create``
instead, which resolves the template's ``chat`` create-template and lands in the right work
dir, while the coupling stays at the mngr CLI level. ``mngr exec`` runs its COMMAND through a
shell on the host, so every inner command is ``shlex``-quoted and the seed message cannot
break out of its own argument. Each exec is expensive (it pays the outer mngr's host read and
an SSH hop), which is why the check, the chat app, and its fallback share one.

**Which account and harness the chat runs on** is the workspace's own decision,
not this app's. The workspace keeps its default provider account's harness and
binding in ``.mngr/settings.local.toml`` -- mngr's local config layer, which every
unqualified ``mngr create`` there resolves -- so the create this builds names no
account and no type and lands on whatever a launcher-started chat would. That file exists
on every workspace that writes it; the ones from minds-v0.5.0 through v0.5.2 keep
accounts but write no file, and for their one update the app falls back to asking
the template's own resolver (:func:`resolve_account_binding`) and splicing its
answer in, as it did before the file existed. That path costs two more execs.

The one setting the bare create adds, ``agent_types.claude.check_installation=false``,
is the lever for a workspace whose claude binary no longer matches the template's pin:
its in-container mngr refuses every claude create, including the update that would fix
it, and only a create arriving from outside can wave the check. The script's create
waves it on every create itself.
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
from imbue.minds.desktop_client.chat_app import ChatAppAnswer
from imbue.minds.desktop_client.chat_app import ChatAppVerdict
from imbue.minds.desktop_client.chat_app import build_chat_app_exec_args
from imbue.minds.desktop_client.chat_app import build_message_chat_command
from imbue.minds.desktop_client.chat_app import read_chat_app_answer
from imbue.minds.desktop_client.in_workspace_mngr import build_in_workspace_mngr_command
from imbue.minds.desktop_client.in_workspace_mngr import in_workspace_failure_detail
from imbue.minds.desktop_client.in_workspace_mngr import inner_stdout_from_exec_result
from imbue.minds.desktop_client.in_workspace_mngr import is_exec_cut_off_on_reached_host
from imbue.minds.utils.mngr_caller import MngrCaller

# The inner create spawns a fresh chat agent (tmux window, claude process) on an
# existing host: no provisioning or git transfer, but slower than a plain message.
_SPAWN_TIMEOUT_SECONDS: Final[float] = 120.0

# The same create asked of the chat app, which holds its answer until the create has
# finished. This ceiling has to sit ABOVE the two windows inside it -- the chat app's own
# 300s wait (``CHAT_CREATION_WAIT_TIMEOUT_SECONDS`` in the template's ``agent_manager.py``)
# and the 30s the script spends retrying a chat app that is not ready yet -- because the
# inner 504 is the only answer that can tell the user the chat is still coming. Cut the
# wait short here instead and the create runs on regardless, the chat's window opens moments
# later, and all the user was given is minds' own word that the spawn failed.
_SCRIPT_SPAWN_TIMEOUT_SECONDS: Final[float] = 360.0

# The launch runs the chat app's create and, behind a script with no create mode, the bare
# create, in one exec, so its ceiling has room for both.
_LAUNCH_TIMEOUT_SECONDS: Final[float] = _SCRIPT_SPAWN_TIMEOUT_SECONDS + _SPAWN_TIMEOUT_SECONDS

# The account probe runs the template's resolver under ``uv run``, so it pays a Python start.
_ACCOUNT_PROBE_TIMEOUT_SECONDS: Final[float] = 90.0

# mngr's local config layer, where the workspace keeps its default account's harness and
# binding as ``[commands.create]`` defaults. Relative to the workspace's work dir, where
# ``mngr exec`` lands.
_LOCAL_SETTINGS_PATH: Final[str] = ".mngr/settings.local.toml"

# CLEANUP: drop the account resolver below (``_ACCOUNT_ARGS_SCRIPT`` through
# ``resolve_account_binding``, the ``account_args`` the bare create takes,
# ``build_skill_chat_mngr_args``, and ``_spawn_with_bare_create``) together with the launch's
# branch that chooses between it and a bare create (``NEEDS_ACCOUNT_BINDING_SENTINEL``, its
# branch in ``_finish_bare_create_fallback``, and the ``_LOCAL_SETTINGS_PATH`` and
# ``_ACCOUNT_ARGS_SCRIPT`` tests in ``_build_bare_create_fallback_command``), once the release
# that ships the local settings writer (the first after minds-v0.5.2, which is still a resolver
# template) has been the minimum updatable template for one release cycle: every workspace
# then writes ``.mngr/settings.local.toml`` itself, so there is nothing left to ask and nothing
# left to fall back to.

# The template script that turns the workspace's default account into ``mngr create``
# arguments. Its path is the contract; the package behind it is not.
_ACCOUNT_ARGS_SCRIPT: Final[str] = "system/scripts/default_account_args.py"

# What the launch's fallback says instead of creating, on a workspace that keeps accounts
# but writes no create defaults: only the resolver can bind its chat.
NEEDS_ACCOUNT_BINDING_SENTINEL: Final[str] = "MNGR_NEEDS_ACCOUNT_BINDING"

# The ``chat`` create-template made a claude agent on the templates the resolver serves.
_CHAT_HARNESS: Final[str] = "claude"

# Fence the resolver's own output, so an empty answer stays distinguishable from
# a probe that never ran.
ACCOUNT_ARGS_BEGIN_SENTINEL: Final[str] = "MNGR_ACCOUNT_ARGS_BEGIN"
ACCOUNT_ARGS_END_SENTINEL: Final[str] = "MNGR_ACCOUNT_ARGS_END"

# A workspace whose template has no resolver script says so with this: one predating the
# per-account config dirs minds-v0.5.0 introduced, or a current one (the script is gone; its
# create defaults live in the local settings file) that has no account signed in and so no
# file either. Both get a bare create, and the current template's own gate refuses that one.
NO_ACCOUNT_STORE_SENTINEL: Final[str] = "MNGR_NO_ACCOUNT_STORE"

# The resolver exits 0 on every reason it has for declining and non-zero only when
# it broke, so its status is what tells "no account" from "the probe fell over".
ACCOUNT_ARGS_EXIT_SENTINEL: Final[str] = "MNGR_ACCOUNT_ARGS_EXIT="

# What the resolver emits per account: the flag, then its ``NAME=VALUE``.
_ACCOUNT_ARG_FLAG: Final[str] = "--env"

# Both labels make the workspace's chat app open the chat's window on the desktop: shipped
# interfaces key on ``assist``, newer ones on the purpose-neutral ``auto_open``.
AUTO_OPEN_CHAT_LABELS: Final[tuple[str, ...]] = ("assist", "auto_open")

# Puts the chat in the workspace's chat memory band, as its own UI does for the chats it
# creates. The chat app's create route sets it itself; the bare create has to.
USER_CREATED_LABEL: Final[str] = "user_created=true"

# Waves the claude version check for this create alone; see the module docstring.
SKIP_CLAUDE_INSTALLATION_CHECK_SETTING: Final[str] = "agent_types.claude.check_installation=false"


def skill_sentinel(skill_name: str, state: str) -> str:
    """The line the launch echoes for whether the workspace has ``skill_name`` (``state`` is PRESENT or ABSENT)."""
    return f"MNGR_{skill_name.upper().replace('-', '_')}_SKILL_{state}"


class AccountBindingState(UpperCaseStrEnum):
    """What the template's resolver said about the account a chat should run on."""

    BOUND = auto()
    """The account resolved; its ``mngr create`` arguments are on the binding."""
    NOT_REQUIRED = auto()
    """The template has no account store, so its one shared config dir holds the credential."""
    UNAVAILABLE = auto()
    """The workspace keeps accounts but named none this chat could run on."""
    UNREACHABLE = auto()
    """The probe did not run, did not complete, or answered unreadably; the binding is unknown."""


class AccountBinding(FrozenModel):
    """How the template's resolver would bind a chat about to be spawned."""

    state: AccountBindingState = Field(description="Whether an account could be resolved, and why not")
    create_args: tuple[str, ...] = Field(
        default=(),
        description="``mngr create`` arguments that bind the chat; empty unless the state is BOUND",
    )


def build_account_binding_probe_args(workspace_address: str) -> list[str]:
    """Build the ``mngr`` CLI args that ask a workspace's resolver which account a new chat should run on.

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
    # --no-start: resolving a binding must not cold-boot a container.
    return ["exec", "--agent", workspace_address, probe, "--no-start"]


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


def resolve_account_binding(mngr_caller: MngrCaller, workspace_address: str) -> AccountBinding:
    """Ask ``workspace_address``'s resolver which account a chat spawned in it should run on."""
    result = mngr_caller.call(
        build_account_binding_probe_args(workspace_address), timeout=_ACCOUNT_PROBE_TIMEOUT_SECONDS
    )
    if NO_ACCOUNT_STORE_SENTINEL in result.stdout:
        return AccountBinding(state=AccountBindingState.NOT_REQUIRED)
    args = _parse_account_args(result.stdout)
    if args is None:
        logger.warning(
            "The account probe for machine {} produced no sentinel (exit {}): {}",
            workspace_address,
            result.returncode,
            result.stderr.strip(),
        )
        return AccountBinding(state=AccountBindingState.UNREACHABLE)
    exit_code = _parse_resolver_exit_code(result.stdout)
    if exit_code != 0:
        logger.error(
            "The account resolver in machine {} did not complete (exit {}): {}",
            workspace_address,
            exit_code,
            result.stderr.strip(),
        )
        return AccountBinding(state=AccountBindingState.UNREACHABLE)
    if not args:
        # The resolver declines silently on stdout; which of its reasons applies is only on stderr.
        logger.warning(
            "The account resolver in machine {} named no account: {}", workspace_address, result.stderr.strip()
        )
        return AccountBinding(state=AccountBindingState.UNAVAILABLE)
    if not _is_well_formed_account_args(args):
        # An unreadable answer is a broken resolver; splicing it is how a chat ends up bound to nothing.
        logger.error("The account resolver in machine {} answered {}, which is not readable", workspace_address, args)
        return AccountBinding(state=AccountBindingState.UNREACHABLE)
    return AccountBinding(state=AccountBindingState.BOUND, create_args=args)


def generate_chat_name(skill_name: str) -> str:
    """A unique-enough chat name for one run of ``skill_name`` (``<skill>-<hex>``)."""
    return f"{skill_name}-{secrets.token_hex(3)}"


def build_create_chat_script_args(*, chat_name: str, message: str) -> list[str]:
    """The messaging script's arguments that ask the chat app for a chat named ``chat_name`` seeded with ``message``.

    The auto-open labels surface the chat's window; the route binds the account and marks
    the chat user-created itself. The script waits for the chat app to finish the create,
    so its answer comes once the chat exists or with the create's own failure.
    """
    script_args = ["--create", "--name", chat_name]
    for label in AUTO_OPEN_CHAT_LABELS:
        script_args += ["--label", f"{label}=true"]
    return [*script_args, "-m", message]


def build_bare_create_command(*, chat_name: str, message: str, account_args: Sequence[str]) -> str:
    """The in-workspace shell string that spawns a chat seeded with ``message`` with a bare ``mngr create``.

    The path for a template whose script cannot create a chat. The chat is grouped with its
    workspace by living in the same container, so no grouping label is needed. The create
    names no harness and no account: the workspace's own create defaults supply both.
    ``account_args`` are the resolver's arguments for a workspace that writes no create
    defaults (:func:`resolve_account_binding`); empty otherwise.
    """
    inner_parts = ["create", chat_name, "--template", "chat", "--transfer", "none"]
    inner_parts.append("--no-connect")
    for label in AUTO_OPEN_CHAT_LABELS:
        inner_parts += ["--label", f"{label}=true"]
    inner_parts += ["--label", USER_CREATED_LABEL]
    inner_parts += ["-S", SKIP_CLAUDE_INSTALLATION_CHECK_SETTING]
    inner_parts += list(account_args)
    inner_parts += ["--message", message]
    return build_in_workspace_mngr_command(inner_parts)


def _build_bare_create_fallback_command(*, chat_name: str, message: str) -> str:
    """The launch's fallback: a bare create, unless only the resolver can bind the chat.

    A workspace that writes its create defaults binds the chat itself, and one whose template
    has no resolver has one shared config dir that is the authenticated one; everywhere else
    the fallback says so instead of creating an unbound chat.
    """
    bare_create = build_bare_create_command(chat_name=chat_name, message=message, account_args=())
    return (
        f"if [ -f {shlex.quote(_LOCAL_SETTINGS_PATH)} ] || [ ! -f {shlex.quote(_ACCOUNT_ARGS_SCRIPT)} ]; "
        f"then {bare_create}; else echo {NEEDS_ACCOUNT_BINDING_SENTINEL}; fi"
    )


def build_skill_chat_launch_command(*, skill_name: str, chat_name: str, message: str) -> str:
    """The in-workspace shell string that launches a ``skill_name`` chat, or says the workspace lacks the skill.

    Runs in the workspace's work_dir, where ``mngr exec`` lands by default.
    """
    skill_path = f".agents/skills/{skill_name}/SKILL.md"
    chat_app_command = build_message_chat_command(
        build_create_chat_script_args(chat_name=chat_name, message=message),
        _build_bare_create_fallback_command(chat_name=chat_name, message=message),
    )
    return (
        f"if [ -f {shlex.quote(skill_path)} ]; then echo {skill_sentinel(skill_name, 'PRESENT')}; "
        f"{chat_app_command}; else echo {skill_sentinel(skill_name, 'ABSENT')}; fi"
    )


class SkillChatLaunchOutcome(UpperCaseStrEnum):
    """What one launch of a skill chat came to."""

    STARTED = auto()
    """The chat now exists in the workspace."""
    UNSUPPORTED = auto()
    """The workspace is reachable but predates the skill; nothing was created."""
    UNREACHABLE = auto()
    """The exec never reached the workspace (host down, stopped, or unreachable); nothing ran there."""
    SPAWN_FAILED = auto()
    """The workspace had the skill, but the create did not land, or the exec was cut off partway."""


class SkillChatLaunch(FrozenModel):
    """What a launch came to, and the workspace's own words when the create did not land."""

    outcome: SkillChatLaunchOutcome = Field(description="What the launch came to")
    failure_detail: str = Field(
        default="",
        description=(
            "The workspace's own verdict on a failed spawn, for the caller to show; '' for every other outcome "
            "and when the workspace gave none. "
            "Bounded and stripped of the outer mngr's chatter, so it can be rendered as-is"
        ),
    )


def launch_skill_chat(
    mngr_caller: MngrCaller,
    workspace_address: str,
    *,
    skill_name: str,
    chat_name: str,
    message: str,
) -> SkillChatLaunch:
    """Launch a ``skill_name`` chat named ``chat_name`` seeded with ``message``, and wait for its create to finish.

    Synchronous on purpose: the caller holds its "starting..." state until the chat
    actually exists rather than dismissing into a blank gap before the chat's window appears.
    A verdict from the chat app is final: a refusal names a chat already made or a workspace
    that cannot make one, and a second create would not change that. ``--no-start``: a launch
    never boots a stopped workspace; :data:`SkillChatLaunchOutcome.UNREACHABLE` is the caller's
    cue that it may start the workspace and launch again, since nothing ran.
    """
    command = build_skill_chat_launch_command(skill_name=skill_name, chat_name=chat_name, message=message)
    result = mngr_caller.call(build_chat_app_exec_args(workspace_address, command), timeout=_LAUNCH_TIMEOUT_SECONDS)
    inner_stdout = inner_stdout_from_exec_result(result.stdout)
    if skill_sentinel(skill_name, "ABSENT") in inner_stdout:
        return SkillChatLaunch(outcome=SkillChatLaunchOutcome.UNSUPPORTED)
    answer = read_chat_app_answer(result)
    if skill_sentinel(skill_name, "PRESENT") not in inner_stdout:
        # Only mngr's own account of failing before it reached the host says the exec never ran
        # the launch. A timeout, a warm process that died mid-call, or a failure on the reached
        # host may have run it in the workspace before it was cut off, so none of them is the
        # "nothing ran" a caller may retry after.
        if not result.is_mngr_output:
            logger.error(
                "Launching chat {} in machine {} got no answer from mngr: {}",
                chat_name,
                workspace_address,
                result.stderr,
            )
            return SkillChatLaunch(outcome=SkillChatLaunchOutcome.SPAWN_FAILED)
        if is_exec_cut_off_on_reached_host(result.stdout):
            logger.error(
                "Launching chat {} in machine {} was cut off after reaching it: {}",
                chat_name,
                workspace_address,
                answer.log_detail,
            )
            return SkillChatLaunch(outcome=SkillChatLaunchOutcome.SPAWN_FAILED, failure_detail=answer.detail)
        logger.warning(
            "Launching chat {} never reached machine {} (exit {}): {}",
            chat_name,
            workspace_address,
            result.returncode,
            answer.log_detail,
        )
        return SkillChatLaunch(outcome=SkillChatLaunchOutcome.UNREACHABLE)
    if answer.is_delivered:
        return SkillChatLaunch(outcome=SkillChatLaunchOutcome.STARTED)
    if answer.verdict is ChatAppVerdict.NO_VERDICT:
        return _finish_bare_create_fallback(
            mngr_caller, workspace_address, answer, chat_name=chat_name, message=message
        )
    logger.error(
        "Spawning chat {} in machine {} through its chat app failed (script exit {}, exec exit {}): {}",
        chat_name,
        workspace_address,
        answer.script_exit_code,
        answer.exec_returncode,
        answer.log_detail,
    )
    return SkillChatLaunch(outcome=SkillChatLaunchOutcome.SPAWN_FAILED, failure_detail=answer.detail)


def _finish_bare_create_fallback(
    mngr_caller: MngrCaller, workspace_address: str, answer: ChatAppAnswer, *, chat_name: str, message: str
) -> SkillChatLaunch:
    """What the launch's bare create came to, behind a script that gave no verdict on the create."""
    # Warning, with the script's own words: an argument the script rejected would otherwise
    # read as an old template.
    logger.warning(
        "Machine {} gave no verdict on creating chat {} through its chat app ({}); fell back to a bare create",
        workspace_address,
        chat_name,
        answer.detail,
    )
    fallback = answer.fallback
    if fallback is None or fallback.exit_code is None:
        logger.error("The bare create of chat {} in machine {} never reported back", chat_name, workspace_address)
        return SkillChatLaunch(outcome=SkillChatLaunchOutcome.SPAWN_FAILED)
    if NEEDS_ACCOUNT_BINDING_SENTINEL in fallback.stdout:
        binding = resolve_account_binding(mngr_caller, workspace_address)
        return _spawn_with_bare_create(
            mngr_caller, workspace_address, chat_name=chat_name, message=message, account_args=binding.create_args
        )
    if fallback.exit_code == 0:
        return SkillChatLaunch(outcome=SkillChatLaunchOutcome.STARTED)
    logger.error(
        "Spawning chat {} in machine {} exited {}: {}",
        chat_name,
        workspace_address,
        fallback.exit_code,
        fallback.stderr.strip(),
    )
    return SkillChatLaunch(
        outcome=SkillChatLaunchOutcome.SPAWN_FAILED, failure_detail=in_workspace_failure_detail(fallback.stderr)
    )


def build_skill_chat_mngr_args(
    workspace_address: str, *, chat_name: str, message: str, account_args: Sequence[str]
) -> list[str]:
    """The ``mngr`` args that run the bare create in its own ``mngr exec``, bound with the resolver's ``account_args``.

    ``--no-start``: the create follows a launch that found the host running, so this only
    guards the stop race; a chat create must never cold-boot a host.
    """
    return [
        "exec",
        "--agent",
        workspace_address,
        build_bare_create_command(chat_name=chat_name, message=message, account_args=account_args),
        "--no-start",
    ]


def _spawn_with_bare_create(
    mngr_caller: MngrCaller,
    workspace_address: str,
    *,
    chat_name: str,
    message: str,
    account_args: Sequence[str],
) -> SkillChatLaunch:
    """Spawn the chat with a bare ``mngr create`` in its own exec and wait for it to finish."""
    args = build_skill_chat_mngr_args(
        workspace_address, chat_name=chat_name, message=message, account_args=account_args
    )
    result = mngr_caller.call(args, timeout=_SPAWN_TIMEOUT_SECONDS)
    if result.returncode != 0:
        logger.error(
            "Spawning chat {} in machine {} exited {}: {}",
            chat_name,
            workspace_address,
            result.returncode,
            result.stderr.strip(),
        )
        # When no result came back, the stderr is MngrCaller's own account of
        # that -- a timeout's quotes the whole argv, the seed message with it --
        # and is not something the workspace said, so there is no verdict to carry.
        detail = in_workspace_failure_detail(result.stderr) if result.is_mngr_output else ""
        return SkillChatLaunch(outcome=SkillChatLaunchOutcome.SPAWN_FAILED, failure_detail=detail)
    return SkillChatLaunch(outcome=SkillChatLaunchOutcome.STARTED)

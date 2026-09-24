"""Spawn an in-workspace chat that drives a template skill, and probe for the skill first.

The app asks the workspace's own chat app for the chat, through the template's
``system/scripts/message_chat.py --create`` run *inside* the workspace's container
(:func:`~imbue.minds.desktop_client.chat_app.ask_chat_app`, the same run that
messages a chat, plus ``--create``): the chat app mints the chat's id, binds the
workspace's default account and harness, labels it, seeds the message, and answers
once its ``mngr create`` has finished, exactly as a launcher-started chat is made. On a template
whose script predates the create mode (or has no script), the app runs a bare
``mngr create`` inside the container instead, which resolves the template's
``chat`` create-template and lands in the right work dir, while the coupling
stays at the mngr CLI level. ``mngr exec`` runs its COMMAND through a shell on
the host, so either inner command is one ``shlex.join``-ed string and the seed
message cannot break out of its own argument.

A workspace created from a template older than the skill would accept the inner
``mngr create`` and then hang on the unknown slash command, leaving a
half-created chat behind, so callers probe first and refuse rather than spawn a
chat that can only fail. The probe echoes a sentinel rather than relying on the
exit code, which would conflate "file absent" with "probe never ran".

**Which account and harness the chat runs on** is the workspace's own decision,
not this app's. The workspace keeps its default provider account's harness and
binding in ``.mngr/settings.local.toml`` -- mngr's local config layer, which every
unqualified ``mngr create`` there resolves -- so the create this builds names no
account and no type and lands on whatever a launcher-started chat would. That file exists
on every workspace that writes it; the ones from minds-v0.5.0 through v0.5.2 keep
accounts but write no file, and for their one update the app falls back to asking
the template's own resolver (:func:`resolve_account_binding`) and splicing its
answer in, as it did before the file existed.

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
from imbue.minds.desktop_client.chat_app import ChatAppVerdict
from imbue.minds.desktop_client.chat_app import ask_chat_app
from imbue.minds.desktop_client.in_workspace_mngr import build_in_workspace_mngr_command
from imbue.minds.desktop_client.in_workspace_mngr import in_workspace_failure_detail
from imbue.minds.utils.mngr_caller import MngrCaller

# Two filesystem checks inside an already-running container, so it should
# return near-instantly; a low ceiling makes an unreachable workspace fail fast.
_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0

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

# The account probe runs the template's resolver under ``uv run``, so it pays a Python start.
_ACCOUNT_PROBE_TIMEOUT_SECONDS: Final[float] = 90.0

# mngr's local config layer, where the workspace keeps its default account's harness and
# binding as ``[commands.create]`` defaults. Relative to the workspace's work dir, where
# ``mngr exec`` lands.
_LOCAL_SETTINGS_PATH: Final[str] = ".mngr/settings.local.toml"

# Whether the workspace writes its create defaults, echoed by the skill probe so one exec
# answers both questions.
LOCAL_SETTINGS_PRESENT_SENTINEL: Final[str] = "MNGR_LOCAL_SETTINGS_PRESENT"
LOCAL_SETTINGS_ABSENT_SENTINEL: Final[str] = "MNGR_LOCAL_SETTINGS_ABSENT"

# CLEANUP: drop the account resolver below (``_ACCOUNT_ARGS_SCRIPT`` through
# ``resolve_account_binding``, ``resolve_legacy_account_args``, and the ``account_args``
# the create takes) together with the probe half that chooses between it and a bare
# create (the two sentinels above, ``_LOCAL_SETTINGS_PATH``, its ``test -f`` in
# ``build_skill_support_probe_args``, and ``SkillProbe.is_local_settings_present``), once
# the release that ships the local settings writer (the first after minds-v0.5.2, which
# is still a resolver template) has been the minimum updatable template for one release
# cycle: every workspace then writes ``.mngr/settings.local.toml`` itself, so there is
# nothing left to ask and nothing left to fall back to.

# The template script that turns the workspace's default account into ``mngr create``
# arguments. Its path is the contract; the package behind it is not.
_ACCOUNT_ARGS_SCRIPT: Final[str] = "system/scripts/default_account_args.py"

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


class SkillSupport(UpperCaseStrEnum):
    """Whether a workspace can host a chat driving a given template skill."""

    SUPPORTED = auto()
    """The workspace has the skill; spawning a chat will work."""
    UNSUPPORTED = auto()
    """The workspace is reachable but predates the skill."""
    UNREACHABLE = auto()
    """The probe could not run (host/workspace down); support is unknown."""


class SkillProbe(FrozenModel):
    """What one probe of a workspace answered: whether it can host the skill, and whether it writes its create defaults."""

    support: SkillSupport = Field(description="Whether the workspace can host a chat driving the skill")
    is_local_settings_present: bool = Field(
        default=False,
        description=(
            "Whether the workspace keeps its default account's create defaults in its local mngr "
            "settings, so a bare create resolves the account; False when the probe did not answer"
        ),
    )


def _sentinel(skill_name: str, state: str) -> str:
    return f"MNGR_{skill_name.upper().replace('-', '_')}_SKILL_{state}"


def build_skill_support_probe_args(workspace_address: str, skill_name: str) -> list[str]:
    """Build the ``mngr`` CLI args that probe a workspace for ``skill_name`` and its create defaults.

    Runs, in the workspace's work_dir (where ``mngr exec`` lands by default), a
    shell ``test`` for the skill's SKILL.md and one for the local settings file,
    each echoing a present/absent sentinel.
    """
    skill_path = f".agents/skills/{skill_name}/SKILL.md"
    check = (
        f"if [ -f {shlex.quote(skill_path)} ]; "
        f"then echo {_sentinel(skill_name, 'PRESENT')}; else echo {_sentinel(skill_name, 'ABSENT')}; fi; "
        f"if [ -f {shlex.quote(_LOCAL_SETTINGS_PATH)} ]; "
        f"then echo {LOCAL_SETTINGS_PRESENT_SENTINEL}; else echo {LOCAL_SETTINGS_ABSENT_SENTINEL}; fi"
    )
    # --no-start: probes run eagerly (a modal opening, a dispatch), and a
    # support check must never cold-boot a container as a side effect.
    return ["exec", "--agent", workspace_address, check, "--no-start"]


def probe_skill(mngr_caller: MngrCaller, workspace_address: str, skill_name: str) -> SkillProbe:
    """Probe ``workspace_address`` for ``skill_name`` and classify the result."""
    result = mngr_caller.call(
        build_skill_support_probe_args(workspace_address, skill_name), timeout=_PROBE_TIMEOUT_SECONDS
    )
    is_local_settings_present = LOCAL_SETTINGS_PRESENT_SENTINEL in result.stdout
    if _sentinel(skill_name, "PRESENT") in result.stdout:
        return SkillProbe(support=SkillSupport.SUPPORTED, is_local_settings_present=is_local_settings_present)
    if _sentinel(skill_name, "ABSENT") in result.stdout:
        return SkillProbe(support=SkillSupport.UNSUPPORTED, is_local_settings_present=is_local_settings_present)
    logger.warning(
        "The {} skill probe for machine {} produced no sentinel (exit {}): {}",
        skill_name,
        workspace_address,
        result.returncode,
        result.stderr.strip(),
    )
    return SkillProbe(support=SkillSupport.UNREACHABLE)


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
    # --no-start, like every other probe here: resolving a binding must not cold-boot a container.
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


def resolve_legacy_account_args(mngr_caller: MngrCaller, workspace_address: str, probe: SkillProbe) -> tuple[str, ...]:
    """The account arguments a create in ``workspace_address`` still needs from this app, if any.

    Nothing on a workspace that writes its create defaults: its own mngr binds the
    chat. On one that keeps accounts but writes no file (minds-v0.5.0 through v0.5.2)
    the template's resolver is asked, and only a resolved account is spliced in; a
    workspace that resolves none, or cannot be asked, gets a bare create, and what the
    workspace makes of that is the verdict the user sees.
    """
    if probe.is_local_settings_present:
        return ()
    return resolve_account_binding(mngr_caller, workspace_address).create_args


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


def build_skill_chat_mngr_args(
    workspace_address: str, *, chat_name: str, message: str, account_args: Sequence[str] = ()
) -> list[str]:
    """Build the ``mngr`` CLI args (sans the leading ``mngr``) that spawn a chat seeded with ``message`` with a bare ``mngr create``.

    The path for a template whose script cannot create a chat. An ``exec`` targeting the
    workspace agent's address whose single COMMAND argument is the inner ``mngr create``
    shell string. The chat is grouped with its workspace by living in the same container,
    so no grouping label is needed. The create names no harness and no account: the
    workspace's own create defaults supply both.

    ``account_args`` are the resolver's arguments for a workspace that writes no
    create defaults (:func:`resolve_legacy_account_args`); empty otherwise.
    """
    inner_parts = ["create", chat_name, "--template", "chat", "--transfer", "none"]
    inner_parts.append("--no-connect")
    for label in AUTO_OPEN_CHAT_LABELS:
        inner_parts += ["--label", f"{label}=true"]
    inner_parts += ["--label", USER_CREATED_LABEL]
    inner_parts += ["-S", SKIP_CLAUDE_INSTALLATION_CHECK_SETTING]
    inner_parts += list(account_args)
    inner_parts += ["--message", message]
    # --no-start: the create is only reachable after the support probe succeeded
    # (host running), so this guards the stop race; a chat create must never
    # cold-boot a host either.
    return [
        "exec",
        "--agent",
        workspace_address,
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
    workspace_address: str,
    *,
    chat_name: str,
    message: str,
    probe: SkillProbe,
) -> SkillChatSpawn:
    """Spawn the chat and wait for its create to finish; report how it went.

    Synchronous on purpose: the caller holds its "starting..." state until the
    chat actually exists rather than dismissing into a blank gap before the chat's window
    appears.

    The workspace's chat app is asked first, through the template's script. Only when the
    script gave no verdict (no script, or one from before its create mode) does the bare
    ``mngr create`` run, bound through the workspace's create defaults or, on a template
    that writes none, the resolver ``probe`` chose. A verdict from the
    script is final either way: a refusal names a chat already made or a workspace that
    cannot make one, and a second create would not change that.

    A failure carries the workspace's verdict rather than only logging it: the
    refusals that stick are the ones retrying cannot fix, and a workspace with no
    provider account signed in refuses in its own words.
    """
    answer = ask_chat_app(
        mngr_caller,
        workspace_address,
        build_create_chat_script_args(chat_name=chat_name, message=message),
        timeout=_SCRIPT_SPAWN_TIMEOUT_SECONDS,
    )
    if answer.is_delivered:
        return SkillChatSpawn(is_started=True)
    if answer.verdict is ChatAppVerdict.NO_VERDICT:
        # Warning, with the script's own words: an argument the script rejected would otherwise
        # read as an old template.
        logger.warning(
            "Machine {} gave no verdict on creating chat {} through its chat app ({}); spawning it with a bare create",
            workspace_address,
            chat_name,
            answer.detail,
        )
        account_args = resolve_legacy_account_args(mngr_caller, workspace_address, probe)
        return _spawn_with_bare_create(
            mngr_caller, workspace_address, chat_name=chat_name, message=message, account_args=account_args
        )
    logger.error(
        "Spawning chat {} in machine {} through its chat app failed (script exit {}, exec exit {}): {}",
        chat_name,
        workspace_address,
        answer.script_exit_code,
        answer.exec_returncode,
        answer.log_detail,
    )
    return SkillChatSpawn(is_started=False, failure_detail=answer.detail)


def _spawn_with_bare_create(
    mngr_caller: MngrCaller,
    workspace_address: str,
    *,
    chat_name: str,
    message: str,
    account_args: Sequence[str],
) -> SkillChatSpawn:
    """Spawn the chat with a bare ``mngr create`` inside the workspace and wait for it to finish."""
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
        return SkillChatSpawn(is_started=False, failure_detail=detail)
    return SkillChatSpawn(is_started=True)

"""``mngr_antigravity`` plugin -- registers the ``antigravity`` agent type for Google's Antigravity CLI (``agy``).

Antigravity replaced Gemini CLI on 2026-05-19; the legacy request path turns
off for paid-tier accounts on 2026-06-18. Despite the Gemini lineage the new
CLI is architecturally closer to Claude Code than to Gemini -- hook event
names and permission-dialog phrasing match Claude's surface. The structural
choices below reflect that: the process name is the Go binary ``agy``.

Hooks: mngr provisions a per-agent ``hooks.json`` (see
``build_antigravity_hooks_config``) into the agent state dir and points agy at
it with ``--add-dir`` (agy 1.0.3 loads and executes hooks discovered this way):

* An ``active`` marker (``PreInvocation`` touches it, ``Stop`` removes it).
  ``BaseAgent.get_lifecycle_state`` reads this marker to report RUNNING while
  the agent works and WAITING when it's idle; agy maintains no such marker on
  its own.

``auto_allow_permissions`` is handled by the ``--dangerously-skip-permissions``
CLI flag, NOT a hook: agy's documented ``PreToolUse`` ``{"decision": "allow"}``
output does not actually gate the ``run_command`` confirmation dialog (verified
live against agy 1.0.3 -- the hook runs but the dialog still appears).

The in-TUI ``/hooks`` command writes ``hooks.json`` to
``~/.gemini/antigravity-cli/``, which the execution engine never runs -- that
path is loaded only for the TUI's display, while hooks execute only from
``~/.gemini/config/hooks.json`` and per-workspace ``.agents/hooks.json``
(google-antigravity/antigravity-cli#49). mngr writes its own file under an
``--add-dir`` path and does not use the TUI.

Readiness is signalled by the ``InteractiveTuiAgent`` banner-poll: agy's hook
events (``PreToolUse``/``PostToolUse``/``PreInvocation``/``PostInvocation``/
``Stop``) are execution-loop events with no "input prompt drawn" analog. A
permission dialog can't be detected via hooks either -- none fires while the
agent is blocked at it, and the hook input carries no dialog state -- so the
agent exposes no permission-specific WAITING reason.

Transcript support: enabled by default. ``stream_transcript.sh`` tails agy's
per-conversation JSONL files at
``~/.gemini/antigravity-cli/brain/<conv_id>/.system_generated/logs/transcript.jsonl``,
filtered to conversation IDs that *this* agent worked on (discovered from the
per-agent conversation-ids file the ``PreInvocation`` capture hook maintains;
see ``CONVERSATION_IDS_FILENAME`` and ``capture_conversation_id.sh``).
``common_transcript.sh`` converts to the agent-agnostic schema that ``mngr
transcript`` reads.
"""

from __future__ import annotations

import importlib.resources
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import ClassVar
from typing import Final

import click
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.logging import log_span
from imbue.mngr import hookimpl
from imbue.mngr.agents.common_transcript import maybe_provision_common_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_raw_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_scripts_to_commands_dir
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import send_enter_best_effort
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.git_utils import find_git_common_dir
from imbue.mngr_antigravity import resources as _antigravity_resources
from imbue.mngr_antigravity.antigravity_config import CAPTURE_CONVERSATION_ID_SCRIPT_NAME
from imbue.mngr_antigravity.antigravity_config import CONVERSATION_IDS_FILENAME
from imbue.mngr_antigravity.antigravity_config import TRUSTED_WORKSPACES_KEY
from imbue.mngr_antigravity.antigravity_config import build_antigravity_hooks_config
from imbue.mngr_antigravity.antigravity_config import get_antigravity_user_settings_path
from imbue.mngr_antigravity.antigravity_config import merge_trusted_workspace
from imbue.mngr_antigravity.antigravity_config import read_antigravity_settings
from imbue.mngr_antigravity.antigravity_config import serialize_antigravity_hooks
from imbue.mngr_antigravity.antigravity_config import serialize_antigravity_settings

# Per-agent directory (under the agent state dir) whose ``.agents/hooks.json``
# holds mngr's hooks; see ``build_antigravity_hooks_config``. It lives in the
# durable state dir, but agy is pointed at it through a /tmp symlink (below):
# agy rejects any ``--add-dir`` path with a dot-prefixed segment, and the state
# dir is under ``~/.mngr/`` -- the same hidden-path rule the workspace symlink
# works around.
_AGY_HOOKS_DIR_NAME: Final[str] = "agy_hooks"

# Parent of the per-agent /tmp symlink that points at the (dotted) hooks dir.
# agy is given ``--add-dir <this>/<agent_id>`` -- a non-dotted path it accepts
# -- which resolves through the symlink to ``<state>/agy_hooks``. Mirrors
# ``_AGY_WORKSPACE_SYMLINK_PARENT``; recreated via ``ln -sfn`` each launch so
# /tmp wipes self-repair.
_AGY_HOOKS_SYMLINK_PARENT: Final[str] = "/tmp/mngr_antigravity_hooks"

# Top-level CLI flag exposed by `agy --help`; auto-approves every tool call.
# Same spelling as Claude Code's flag. Used (rather than a PreToolUse hook)
# for ``auto_allow_permissions`` because agy's documented hook allow-decision
# does not actually gate the run_command confirmation dialog -- see the
# ``auto_allow_permissions`` field comment and ``build_antigravity_hooks_config``.
_DANGEROUSLY_SKIP_PERMISSIONS_FLAG: Final[str] = "--dangerously-skip-permissions"

_COMMON_TRANSCRIPT_SCRIPT_NAME: Final[str] = "common_transcript.sh"
_RAW_TRANSCRIPT_SCRIPT_NAME: Final[str] = "stream_transcript.sh"

# Supervisor script provisioned into the agent's commands/ dir; owns the
# lifecycle of the raw streamer and (when enabled) the common-transcript
# converter. Mirrors the mngr_claude background-tasks pattern.
_BACKGROUND_TASKS_SCRIPT_NAME: Final[str] = "antigravity_background_tasks.sh"

# Relative path under $MNGR_AGENT_STATE_DIR for the agy --log-file. Keeping
# it under logs/ groups it with the other per-agent log artifacts. agy's
# internal log is no longer used for conversation-id discovery (the
# ``capture_conversation_id.sh`` hook records IDs directly; see
# ``CONVERSATION_IDS_FILENAME``), but a durable per-agent agy log is still
# worth keeping for debugging.
_AGY_LOG_FILE_RELATIVE_PATH: Final[str] = "logs/agy_cli.log"

# Parent directory for the per-agent symlinks that work around agy's
# refusal to treat hidden paths (anything with a dot-prefixed segment, like
# ``.mngr/...``) as a workspace. agy logs ``Failed to add workspace folder
# /path/.mngr/...: is hidden: ignore uri`` and falls back to the user's
# home directory as the project root, which means workspace-scoped tooling
# (file search, project_id, .agents/) operates against the wrong tree.
#
# Verified via google-forum bug report (no flag override exists) and
# confirmed live: launching agy with cwd set to a /tmp symlink that targets
# the dotted ``work_dir`` produces ``project: using project "/tmp/..."``
# (the symlink path, not the resolved target), and the workspace-add error
# disappears. The symlink is recreated on every ``assemble_command`` call
# via ``mkdir -p`` + ``ln -sfn`` so /tmp wipes self-repair on next launch.
_AGY_WORKSPACE_SYMLINK_PARENT: Final[str] = "/tmp/mngr_antigravity_workspaces"


def _load_antigravity_resource_script(filename: str) -> str:
    """Load a resource script from the mngr_antigravity resources package."""
    resource_files = importlib.resources.files(_antigravity_resources)
    return resource_files.joinpath(filename).read_text()


class AntigravityAgentConfig(AgentTypeConfig):
    """Config for the antigravity agent type."""

    command: CommandString = Field(
        default=CommandString("agy"),
        description="Command to run the antigravity agent. The Antigravity 2.0 desktop app "
        "ships its own `agy` shim that can shadow the CLI in PATH; if both are installed, "
        "remove the desktop app's `bin/agy` or override this field with the absolute path "
        "to the standalone Go binary.",
    )
    cli_args: tuple[str, ...] = Field(
        default=(),
        description="Additional CLI arguments to pass to the antigravity agent.",
    )
    # auto_allow_permissions adds agy's ``--dangerously-skip-permissions`` flag
    # (see ``assemble_command``). It is NOT a hook: agy's documented
    # ``PreToolUse`` ``{"decision": "allow"}`` output does not actually gate the
    # ``run_command`` confirmation dialog (verified live against agy 1.0.3), so
    # the flag is the only mechanism that reliably auto-approves.
    auto_allow_permissions: bool = Field(
        default=False,
        description="When True, auto-approve every tool call without prompting.",
    )
    # auto_dismiss_dialogs is the mngr_claude-style auto-trust knob. When
    # True (or when ``mngr_ctx.is_auto_approve`` is set, i.e. ``mngr create
    # --yes``), provisioning silently appends the work_dir to agy's
    # ``trustedWorkspaces`` without prompting. When False (default), the
    # provisioner asks the user via ``click.confirm`` before mutating the
    # global config, mirroring ``mngr_claude``'s ``auto_dismiss_dialogs``.
    # Why default off: the file is shared user state, so we should make
    # writing to it an explicit choice. Why dismiss-before-launch at all:
    # agy's first-launch trust dialog consumes the first keystroke
    # otherwise, breaking ``mngr message`` / ``--message`` flows -- the
    # same shape ``mngr_claude`` mitigates via its dismiss path.
    auto_dismiss_dialogs: bool = Field(
        default=False,
        description="When True, auto-trust the work_dir without prompting. "
        "When False (default), the user is prompted interactively.",
    )
    # emit_common_transcript gates the JSONL -> common-schema converter that
    # writes to ``events/antigravity/common_transcript/events.jsonl``. The raw
    # transcript at ``logs/antigravity_transcript/events.jsonl`` is always
    # captured (required by HasTranscriptMixin); only the common-format
    # converter is gated by this flag.
    emit_common_transcript: bool = Field(
        default=True,
        description="When True, emit a common-schema transcript that `mngr transcript` reads.",
    )


class AntigravityAgent(InteractiveTuiAgent[AntigravityAgentConfig], HasCommonTranscriptMixin):
    """Agent implementation for Google's Antigravity CLI (``agy``)."""

    # Stable substring of the footer hint that agy renders ONLY once the
    # input prompt is fully drawn and ready to receive keystrokes. Polled by
    # ``InteractiveTuiAgent.wait_for_ready_signal``.
    #
    # We deliberately do NOT key off the "Antigravity CLI <version>" splash
    # banner: agy renders an early "Welcome to the Antigravity CLI. You are
    # currently not signed in." line *before* OAuth completes, which also
    # contains the substring "Antigravity CLI" but does NOT mean the input
    # row is ready. If mngr starts pasting at that point, agy drops the
    # keystrokes on the floor (no input row yet to receive them) and
    # ``wait_for_paste_visible`` times out, surfacing as a noisy
    # ``mngr create --message`` timeout. The "? for shortcuts" footer string
    # appears only with the rendered input prompt, so it's a reliable
    # ready signal.
    TUI_READY_INDICATOR: ClassVar[str] = "? for shortcuts"

    def get_expected_process_name(self) -> str:
        # `agy` is a single-file Go binary; ps/tmux show the literal command name.
        return "agy"

    def _send_enter_and_validate(self, tmux_target: TmuxWindowTarget) -> None:
        # Antigravity has no ``UserPromptSubmit`` analog (so the tmux wait-for
        # hook trick Claude uses doesn't apply) and its input row has no
        # placeholder that hides while text is typed and reappears after
        # submission, so we can't poll for a cleared indicator either.
        # ``wait_for_paste_visible`` upstream already confirmed the message
        # landed in the pane before we get here, so a best-effort Enter is
        # the right strategy.
        send_enter_best_effort(self, tmux_target)

    @property
    def is_common_transcript_enabled(self) -> bool:
        return self.agent_config.emit_common_transcript

    def get_raw_transcript_scripts(self) -> Mapping[str, str]:
        """Return the antigravity raw-transcript streamer.

        Always provisioned per :class:`HasTranscriptMixin`: the raw bytes are
        the source of truth that the common-transcript converter and any
        future tooling read from.
        """
        return {_RAW_TRANSCRIPT_SCRIPT_NAME: _load_antigravity_resource_script(_RAW_TRANSCRIPT_SCRIPT_NAME)}

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        """Return the antigravity common-transcript converter."""
        return {_COMMON_TRANSCRIPT_SCRIPT_NAME: _load_antigravity_resource_script(_COMMON_TRANSCRIPT_SCRIPT_NAME)}

    def _get_agy_log_file_path(self) -> Path:
        """Path agy is told to write its --log-file to.

        Lives under the agent's state dir so it is per-agent and durable.
        The streamer reads this file to discover which conversation IDs
        belong to this agent.
        """
        return self._get_agent_dir() / _AGY_LOG_FILE_RELATIVE_PATH

    def _get_agy_hooks_dir(self) -> Path:
        """Durable per-agent dir holding ``.agents/hooks.json`` (the symlink target).

        Lives under the per-agent state dir so it survives restarts. agy is not
        pointed here directly -- this path is under ``~/.mngr/`` (dotted), which
        agy rejects -- but via the ``_get_agy_hooks_symlink_path`` /tmp symlink.
        """
        return self._get_agent_dir() / _AGY_HOOKS_DIR_NAME

    def _get_agy_hooks_symlink_path(self) -> str:
        """Non-dotted /tmp symlink path that agy receives as ``--add-dir``.

        Points at ``_get_agy_hooks_dir``; agy reads ``<symlink>/.agents/hooks.json``
        through it. Non-dotted so agy doesn't reject it as a hidden path (the
        state dir under ``~/.mngr/`` would be). Mirrors the workspace symlink.
        """
        return f"{_AGY_HOOKS_SYMLINK_PARENT}/{self.id}"

    def _get_agy_hooks_file_path(self) -> Path:
        """Path of the per-agent ``hooks.json`` agy reads from the ``--add-dir`` dir.

        agy looks for ``<workspace>/.agents/hooks.json``; with the hooks dir
        added via ``--add-dir`` that resolves to
        ``<state>/agy_hooks/.agents/hooks.json``.
        """
        return self._get_agy_hooks_dir() / ".agents" / "hooks.json"

    def _get_conversation_ids_file_path(self) -> Path:
        """Per-agent file recording the agy conversation IDs this agent worked on.

        Written by ``capture_conversation_id.sh`` (the ``PreInvocation`` capture
        hook); read on restart by ``assemble_command`` (last line -> resume that
        conversation) and by ``stream_transcript.sh`` (unique lines -> tail each
        conversation's transcript). Lives directly under the agent state dir so
        the hook's ``$MNGR_AGENT_STATE_DIR/{CONVERSATION_IDS_FILENAME}`` and this
        path resolve to the same file.
        """
        return self._get_agent_dir() / CONVERSATION_IDS_FILENAME

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Dismiss agy's startup dialogs, then install the background-tasks supervisor + transcript scripts.

        Dialog dismissal mirrors ``mngr_claude``'s
        ``interactively_dismiss_claude_dialogs``: in auto-approve mode
        (``mngr_ctx.is_auto_approve`` or ``auto_dismiss_dialogs=True``) the
        work_dir is pre-trusted silently; in interactive mode the user is
        prompted via ``click.confirm`` before mngr mutates the global
        ``~/.gemini/antigravity-cli/settings.json``; in non-interactive mode
        with neither auto-approve nor opt-in, we raise so the operator
        notices instead of falling back to agy's TUI dialog (which would
        consume the first keystroke of ``mngr message``).

        After dismissal, the per-agent ``hooks.json`` is installed (the
        ``active``-marker hooks, plus the auto-allow hook when configured),
        followed by the transcript scripts and the background-tasks
        supervisor under ``$MNGR_AGENT_STATE_DIR/commands/``.
        """
        self._interactively_dismiss_antigravity_dialogs(host, mngr_ctx)
        self._install_hooks(host)
        with mngr_ctx.concurrency_group.make_concurrency_group("antigravity_provisioning") as concurrency_group:
            provision_raw_transcript_scripts(
                self,
                host,
                self._get_agent_dir(),
                concurrency_group,
            )
            maybe_provision_common_transcript_scripts(
                self,
                host,
                self._get_agent_dir(),
                concurrency_group,
            )
            provision_scripts_to_commands_dir(
                host,
                self._get_agent_dir(),
                {
                    _BACKGROUND_TASKS_SCRIPT_NAME: _load_antigravity_resource_script(_BACKGROUND_TASKS_SCRIPT_NAME),
                    # Run by the PreInvocation capture hook to record the active
                    # conversation ID (see build_antigravity_hooks_config).
                    CAPTURE_CONVERSATION_ID_SCRIPT_NAME: _load_antigravity_resource_script(
                        CAPTURE_CONVERSATION_ID_SCRIPT_NAME
                    ),
                },
                concurrency_group,
            )

    def _install_hooks(self, host: OnlineHostInterface) -> None:
        """Write the per-agent ``hooks.json`` agy executes via ``--add-dir``.

        The file is mngr-owned and rewritten from scratch on every provision
        (no merge with user content needed -- it lives in the agent state dir,
        not the user's global config). ``host.write_text_file`` creates the
        intermediate ``agy_hooks/.agents/`` directories. The matching
        ``--add-dir`` arg is appended in ``assemble_command``.
        """
        hooks_config = build_antigravity_hooks_config()
        hooks_path = self._get_agy_hooks_file_path()
        with log_span("Installing antigravity hooks at {}", hooks_path):
            host.write_text_file(hooks_path, serialize_antigravity_hooks(hooks_config))

    def _find_git_source_path(self, concurrency_group: ConcurrencyGroup) -> Path | None:
        """Find the source repo root for this agent's ``work_dir``, if it's inside a git repo.

        Returns the parent of the git common dir (the source repo root), or
        ``None`` if ``work_dir`` is not inside a git repo. Mirrors
        ``mngr_claude``'s helper of the same name -- the source-path concept
        is what makes a single trust grant cover every worktree of the same
        repo (in Claude's per-project storage; for Antigravity it is the
        human-visible reference but doesn't change agy's exact-match check).
        """
        git_common_dir = find_git_common_dir(self.work_dir, concurrency_group)
        if git_common_dir is None:
            return None
        return git_common_dir.parent

    def _interactively_dismiss_antigravity_dialogs(self, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
        """Ensure agy's first-launch trust dialog won't intercept tmux input.

        Branches, matching ``mngr_claude``'s dismiss flow's user-visible
        posture while compensating for agy's exact-match trust check:

        * Effective workspace path (the agy-cwd symlink, see
          ``_get_agy_workspace_symlink_path``) already in
          ``trustedWorkspaces`` -> no-op (idempotent re-provision).
        * ``source_path`` already in ``trustedWorkspaces`` -> silently add
          the effective workspace path. The user has previously trusted
          the source repo (interactively or via opt-in); spawning another
          agent for the same repo shouldn't re-prompt.
        * ``auto_dismiss_dialogs=True`` or ``mngr_ctx.is_auto_approve``:
          silently add both ``source_path`` and the effective workspace
          path so future agents for the same source benefit from the
          silent-extend path above.
        * Interactive (``mngr_ctx.is_interactive``): prompt via
          ``click.confirm``. The prompt references ``source_path`` for
          stable wording across worktrees. On accept, add both.
        * Non-interactive without opt-in, or user declines: log an explicit
          ``logger.error`` and ``raise SystemExit(1)``.

        Note on "effective workspace path" vs ``work_dir``: agy is launched
        with cwd set to a /tmp symlink (the workaround for agy's hidden-
        path rejection of ``~/.mngr/worktrees/...``). agy treats the
        symlink path as its workspace identity and checks
        ``trustedWorkspaces`` against that path. So that's what we have to
        write -- writing ``work_dir`` would not silence the first-launch
        dialog. See ``_AGY_WORKSPACE_SYMLINK_PARENT``.

        Why ``SystemExit`` and not ``UserInputError``: ``provision_agent``
        wraps its body in a ``ConcurrencyExceptionGroup`` (see
        ``imbue.concurrency_group.concurrency_group.ConcurrencyGroup._exit``).
        Regular ``Exception`` raises get wrapped and surface as a noisy
        auto-diagnostics traceback; ``SystemExit`` is a ``BaseException``
        which the same ``_exit`` re-raises unwrapped (line 190-191),
        producing a clean exit.
        """
        # The "workspace path" agy will actually see and check is the /tmp
        # symlink, NOT self.work_dir. Pre-trusting work_dir doesn't silence
        # the dialog because agy never sees that path.
        effective_workspace_path = self._get_agy_workspace_symlink_path()
        settings_path = get_antigravity_user_settings_path()
        existing_settings = read_antigravity_settings(host, settings_path)
        self._check_existing_trustedworkspaces_shape(settings_path, existing_settings)
        existing_trusted: list[str] = list(existing_settings.get(TRUSTED_WORKSPACES_KEY, []))

        if effective_workspace_path in existing_trusted:
            logger.debug("Workspace {} already trusted in {}", effective_workspace_path, settings_path)
            return

        source_path = self._find_git_source_path(mngr_ctx.concurrency_group) or self.work_dir
        source_path_str = str(source_path)
        is_worktree_of_trusted_source = (
            source_path_str != effective_workspace_path and source_path_str in existing_trusted
        )

        if is_worktree_of_trusted_source:
            logger.debug(
                "Source {} is already trusted; silently extending trust to workspace {}",
                source_path_str,
                effective_workspace_path,
            )
            self._write_workspace_trust(host, settings_path, existing_settings, [effective_workspace_path])
            return

        if self.agent_config.auto_dismiss_dialogs or mngr_ctx.is_auto_approve:
            self._write_workspace_trust(
                host,
                settings_path,
                existing_settings,
                self._paths_to_add(effective_workspace_path, source_path_str, existing_trusted),
            )
            return

        if not mngr_ctx.is_interactive:
            logger.error(
                "Source directory {} is not trusted by the Antigravity CLI. "
                "agy's first-launch trust dialog would consume the first keystroke sent to "
                "the tmux pane and break `mngr message`. Re-run interactively to be prompted, "
                "re-run with `--yes`, or set `auto_dismiss_dialogs = true` on the antigravity "
                "agent type.",
                source_path,
            )
            raise SystemExit(1)

        if not self._prompt_user_to_trust_workspace(source_path, settings_path):
            logger.error(
                "User declined to trust {} in {}. Antigravity's first-launch trust dialog "
                "would block tmux input. Aborting agent creation.",
                source_path,
                settings_path,
            )
            raise SystemExit(1)
        self._write_workspace_trust(
            host,
            settings_path,
            existing_settings,
            self._paths_to_add(effective_workspace_path, source_path_str, existing_trusted),
        )

    @staticmethod
    def _paths_to_add(workspace_path: str, source_path_str: str, existing_trusted: list[str]) -> list[str]:
        """Return the paths to append to ``trustedWorkspaces``, deduped against existing entries.

        Includes (in order):

        * ``source_path_str`` -- when it differs from ``workspace_path`` and
          isn't already trusted, so future worktrees of the same source repo
          can take the silent-extend branch in
          ``_interactively_dismiss_antigravity_dialogs``.
        * ``workspace_path`` -- when it isn't already trusted; this is what
          agy's exact-match check needs to suppress the first-launch dialog.

        Each path is independently deduped, so the returned list may be empty,
        single-entry, or two-entry depending on what is already in
        ``existing_trusted``.
        """
        paths: list[str] = []
        if source_path_str != workspace_path and source_path_str not in existing_trusted:
            paths.append(source_path_str)
        if workspace_path not in existing_trusted:
            paths.append(workspace_path)
        return paths

    def _prompt_user_to_trust_workspace(self, source_path: Path, settings_path: Path) -> bool:
        """Prompt the user to trust the agent's source directory in Antigravity's settings.

        Returns True iff the user confirms. Pattern matches ``mngr_claude``'s
        ``_prompt_user_for_trust`` (`libs/mngr_claude/imbue/mngr_claude/plugin.py`):
        the message refers to the *source* directory (the git repo root, or
        the bare work_dir if not in a git repo) so the user sees a stable
        path across worktrees rather than the per-worktree transient path.
        Defaults to ``False`` so a stray Enter doesn't grant trust silently.
        Exposed as a method (rather than a module-level function) so tests
        can subclass and override without monkeypatching.
        """
        logger.info(
            "\nSource directory {} is not yet trusted by the Antigravity CLI.\n"
            "mngr needs to add a trust entry for this directory to {}\n"
            "so that agy's first-launch trust dialog doesn't intercept tmux input.\n",
            source_path,
            settings_path,
        )
        return click.confirm(
            f"Would you like to update {settings_path} to trust this directory?",
            default=False,
        )

    def _check_existing_trustedworkspaces_shape(
        self, settings_path: Path, existing_settings: Mapping[str, Any]
    ) -> None:
        """Hard-error if ``trustedWorkspaces`` exists but isn't a list.

        The ``@pure`` merge helper used to silently coerce non-list values
        into a fresh array containing only the new workspace, which could
        destroy entries an unknown future agy schema put there. Surfacing
        the schema break is safer than rewriting the file.
        """
        existing_trusted = existing_settings.get(TRUSTED_WORKSPACES_KEY)
        if existing_trusted is not None and not isinstance(existing_trusted, list):
            raise UserInputError(
                f"Antigravity settings at {settings_path} has a "
                f"non-list trustedWorkspaces value ({type(existing_trusted).__name__}); "
                f"refusing to overwrite. Inspect the file by hand and either fix the value "
                f"or remove the key, then re-run."
            )

    def _write_workspace_trust(
        self,
        host: OnlineHostInterface,
        settings_path: Path,
        existing_settings: Mapping[str, Any],
        paths_to_add: list[str],
    ) -> None:
        """Append each of ``paths_to_add`` to the user-tier settings' trust list and write it back.

        Iterates so already-trusted entries are skipped (each
        ``merge_trusted_workspace`` call is a no-op when the path is already
        present); writes the combined result once at the end. Passing an
        empty list is a no-op.
        """
        if not paths_to_add:
            return
        merged: Mapping[str, Any] = existing_settings
        actually_added: list[str] = []
        for path in paths_to_add:
            updated = merge_trusted_workspace(merged, path)
            if updated is not None:
                merged = updated
                actually_added.append(path)
        if not actually_added:
            logger.debug("All requested paths already trusted in {}; skipping write", settings_path)
            return
        with log_span("Pre-trusting workspace(s) {} in {}", actually_added, settings_path):
            host.write_text_file(settings_path, serialize_antigravity_settings(merged))

    def _build_background_tasks_command(self) -> str:
        """Shell snippet that launches the background-tasks supervisor.

        Identical structure to mngr_claude's: one backgrounded subshell that
        owns the lifecycle of every watcher (pidfile-deduped, restart-on-
        death). Re-running ``assemble_command`` (e.g. on agent restart) is
        therefore safe because the supervisor's pidfile check causes a
        duplicate launch to exit immediately.
        """
        script_path = f"$MNGR_AGENT_STATE_DIR/commands/{_BACKGROUND_TASKS_SCRIPT_NAME}"
        return f"( bash {script_path} {shlex.quote(self.session_name)} ) &"

    def _get_agy_workspace_symlink_path(self) -> str:
        """Per-agent symlink target that agy will treat as its workspace.

        Lives under ``/tmp/mngr_antigravity_workspaces/<agent_id>`` -- a
        non-dotted path, which is required because agy refuses to add any
        path with a dot-prefixed segment as a workspace (see the constant
        docstring above for the bug background). Per-agent so multiple
        antigravity agents don't share a workspace identity.
        """
        return f"{_AGY_WORKSPACE_SYMLINK_PARENT}/{self.id}"

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        """Build the full launch command.

        Composition (left to right):

        1. ``( bash background_tasks.sh <session> ) &`` -- backgrounded
           supervisor for the transcript streamer + converter.
        2. ``mkdir -p <state>/logs <ws_symlink_parent> <hooks_symlink_parent>
           <hooks>/.agents`` -- guarantees the agy ``--log-file`` directory,
           both symlink parents, and the hooks ``.agents`` dir exist before
           launch (the last so the hooks ``--add-dir`` resolves even on a
           restart before re-provision; ``provision`` writes the hooks.json
           into it).
        3. ``ln -sfn <work_dir> <ws_symlink>`` and
           ``ln -sfn <state>/agy_hooks <hooks_symlink>`` -- create / refresh
           the non-dotted /tmp symlinks for the workspace and the hooks dir.
           Both work around agy's rejection of dot-prefixed (hidden) paths;
           see ``_AGY_WORKSPACE_SYMLINK_PARENT`` / ``_AGY_HOOKS_SYMLINK_PARENT``.
        4. ``cd <ws_symlink>`` -- launches agy with cwd set to the workspace
           symlink, so agy's "project: using project ..." log line names the
           symlink path (not the resolved dotted target).
        5. ``{ <resume-prelude>; agy <user_args> --log-file <state>/logs/agy_cli.log
           --add-dir <hooks_symlink> [--dangerously-skip-permissions] "$@"; }`` --
           foreground process. The ``--add-dir`` makes agy load and execute the
           per-agent ``hooks.json`` (the active marker + the conversation-ID
           capture hook; see ``build_antigravity_hooks_config``). The
           ``--dangerously-skip-permissions`` flag is appended only when
           ``auto_allow_permissions`` is set.

        The resume-prelude resumes the most-recently-active conversation on
        restart. ``capture_conversation_id.sh`` records this agent's
        conversation IDs as it works (see ``CONVERSATION_IDS_FILENAME``); the
        last line is the current one. The stored command is replayed verbatim
        on every ``mngr start`` (``assemble_command`` runs only at create
        time), so the resume decision must be evaluated by the shell at launch
        -- not in Python here, where no conversation exists yet. We pass the
        flag via ``set --`` / ``"$@"`` rather than splitting an unquoted command
        substitution, so it survives both bash and zsh (the agent's login shell
        runs the command). The resume is guarded on the conversation's ``.db``
        store file still existing (agy writes it incrementally, so it survives
        the hard kill ``mngr stop`` performs -- unlike the ``.pb``, which is
        only written on a clean in-TUI exit); if it's gone we launch fresh
        rather than make agy print a "not found" warning. The whole step is a
        ``{ ...; }`` group gated on the ``cd`` succeeding.

        Bash precedence note: ``A & B && C && D && E`` parses as ``A &``
        followed by ``B && C && D && E``. The supervisor's subshell is
        therefore scoped to ``&``, while ``mkdir`` / ``ln`` / ``cd`` / the agy
        group form a foreground sequential chain.

        ``ln -sfn`` is idempotent: re-running on every launch (including
        restarts) updates the symlink in place; ``/tmp`` wipes self-repair
        on the next launch.

        The ``--log-file`` arg pipes agy's internal log to a per-agent path,
        kept for debugging (conversation-ID discovery no longer reads it; the
        capture hook records IDs directly).
        """
        log_file_path = self._get_agy_log_file_path()
        hooks_dir = self._get_agy_hooks_dir()
        hooks_agents_dir = self._get_agy_hooks_file_path().parent
        hooks_symlink_path = self._get_agy_hooks_symlink_path()
        # agy loads .agents/hooks.json from each --add-dir workspace and runs
        # the active-marker hooks. It must be the /tmp symlink, not hooks_dir
        # itself: agy rejects --add-dir paths with a dot-prefixed segment
        # (hooks_dir is under ~/.mngr/), so pointing it straight at hooks_dir
        # silently loads nothing. The symlink resolves to hooks_dir.
        extra_args: list[str] = [
            f"--log-file {shlex.quote(str(log_file_path))}",
            f"--add-dir {shlex.quote(hooks_symlink_path)}",
        ]
        # Auto-approval goes through the flag, not a hook (the hook allow-decision
        # does not gate run_command confirmations; see the config field comment).
        if self.agent_config.auto_allow_permissions:
            extra_args.append(_DANGEROUSLY_SKIP_PERMISSIONS_FLAG)
        base_command = super().assemble_command(host, agent_args, command_override, initial_message)
        background_cmd = self._build_background_tasks_command()

        symlink_path = self._get_agy_workspace_symlink_path()
        mkdir_cmd = (
            f"mkdir -p {shlex.quote(str(log_file_path.parent))} "
            f"{shlex.quote(_AGY_WORKSPACE_SYMLINK_PARENT)} {shlex.quote(_AGY_HOOKS_SYMLINK_PARENT)} "
            f"{shlex.quote(str(hooks_agents_dir))}"
        )
        ln_cmd = f"ln -sfn {shlex.quote(str(self.work_dir))} {shlex.quote(symlink_path)}"
        hooks_ln_cmd = f"ln -sfn {shlex.quote(str(hooks_dir))} {shlex.quote(hooks_symlink_path)}"
        cd_cmd = f"cd {shlex.quote(symlink_path)}"

        # Shell-evaluated at launch (the stored command is replayed on each
        # restart): resume the last-recorded conversation via `--conversation`
        # iff its `.db` store file still exists. We check the `.db` (not the
        # `.pb`): agy writes the conversation incrementally to
        # `conversations/<id>.db`, so it survives the hard process kill that
        # `mngr stop` performs and is what `agy --conversation` resumes from --
        # whereas `conversations/<id>.pb` is only written on a clean in-TUI
        # exit and is absent after a stop (verified live against agy 1.0.4).
        # `set --` / "$@" appends the flag without relying on
        # unquoted-substitution word splitting, so it behaves identically under
        # bash and zsh. The default store dir mirrors stream_transcript.sh's
        # ANTIGRAVITY_APP_DATA_DIR fallback.
        quoted_ids_file = shlex.quote(str(self._get_conversation_ids_file_path()))
        conv_store = "${ANTIGRAVITY_APP_DATA_DIR:-$HOME/.gemini/antigravity-cli}/conversations"
        resume_prelude = (
            f"__mngr_cid=$(tail -n 1 {quoted_ids_file} 2>/dev/null || true); set --; "
            f'if [ -n "$__mngr_cid" ] && [ -f "{conv_store}/$__mngr_cid.db" ]; then '
            'set -- --conversation "$__mngr_cid"; fi'
        )
        agy_invocation = f"{base_command} {' '.join(extra_args)}"

        return CommandString(
            f"{background_cmd} {mkdir_cmd} && {ln_cmd} && {hooks_ln_cmd} && {cd_cmd} "
            f'&& {{ {resume_prelude}; {agy_invocation} "$@" ; }}'
        )


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the antigravity agent type."""
    return ("antigravity", AntigravityAgent, AntigravityAgentConfig)

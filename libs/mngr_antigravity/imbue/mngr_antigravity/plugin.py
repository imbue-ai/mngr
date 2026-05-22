"""``mngr_antigravity`` plugin -- registers the ``antigravity`` agent type for Google's Antigravity CLI (``agy``).

Antigravity replaced Gemini CLI on 2026-05-19; the legacy request path turns
off for paid-tier accounts on 2026-06-18. Despite the Gemini lineage the new
CLI is architecturally closer to Claude Code than to Gemini -- hook event
names, ``--dangerously-skip-permissions`` flag spelling, and permission-
dialog phrasing all match Claude's surface. The structural choices below
reflect that: process name is the Go binary ``agy``; ``auto_allow_permissions``
is wired through Antigravity's documented ``--dangerously-skip-permissions``
flag rather than a permission hook, since the hook JSON schema is not yet
empirically validated against an authenticated session.

Capabilities deliberately scoped out of v0:

* No readiness sentinel hook -- ``InteractiveTuiAgent``'s banner-poll is the
  sole readiness signal. Live testing against ``agy`` 1.0.0 showed that
  hooks.json is loaded (``hooks_manager.go:45 loaded N named hooks``) but
  hook *execution* is gated behind the ``json-hooks-enabled`` experiment
  flag, which Google must enable per-account. Re-introduce when the
  experiment ships GA.

Transcript support: enabled by default. ``stream_transcript.sh`` tails agy's
per-conversation JSONL files at
``~/.gemini/antigravity-cli/brain/<conv_id>/.system_generated/logs/transcript.jsonl``,
filtered to conversation IDs that *this* agent created (discovered by
grepping agy's own ``--log-file``). ``common_transcript.sh`` converts to
the agent-agnostic schema that ``mngr transcript`` reads.
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
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.git_utils import find_git_common_dir
from imbue.mngr_antigravity import resources as _antigravity_resources
from imbue.mngr_antigravity.antigravity_config import TRUSTED_WORKSPACES_KEY
from imbue.mngr_antigravity.antigravity_config import get_antigravity_user_settings_path
from imbue.mngr_antigravity.antigravity_config import merge_trusted_workspace
from imbue.mngr_antigravity.antigravity_config import read_antigravity_settings
from imbue.mngr_antigravity.antigravity_config import serialize_antigravity_settings

# Top-level CLI flag exposed by `agy --help`; auto-approves every tool call.
# Same spelling as Claude Code's flag.
_DANGEROUSLY_SKIP_PERMISSIONS_FLAG: Final[str] = "--dangerously-skip-permissions"

_COMMON_TRANSCRIPT_SCRIPT_NAME: Final[str] = "common_transcript.sh"
_RAW_TRANSCRIPT_SCRIPT_NAME: Final[str] = "stream_transcript.sh"

# Supervisor script provisioned into the agent's commands/ dir; owns the
# lifecycle of the raw streamer and (when enabled) the common-transcript
# converter. Mirrors the mngr_claude background-tasks pattern.
_BACKGROUND_TASKS_SCRIPT_NAME: Final[str] = "antigravity_background_tasks.sh"

# Env var consumed by stream_transcript.sh to locate agy's --log-file. We
# also pass `--log-file <path>` to agy itself in ``assemble_command`` so
# the conversation-id discovery has something to grep against.
_AGY_LOG_FILE_ENV_VAR: Final[str] = "ANTIGRAVITY_AGY_LOG_FILE"

# Relative path under $MNGR_AGENT_STATE_DIR for the agy --log-file. Keeping
# it under logs/ groups it with the other per-agent log artifacts.
_AGY_LOG_FILE_RELATIVE_PATH: Final[str] = "logs/agy_cli.log"


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
    # auto_allow_permissions wires Antigravity's documented
    # ``--dangerously-skip-permissions`` CLI flag (same spelling Claude Code
    # uses). We deliberately route through the flag rather than a permission
    # hook because hook *execution* is currently gated behind the
    # ``json-hooks-enabled`` experiment that Google enables per-account.
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

    # Stable substring of the splash banner that the Antigravity TUI renders
    # once startup completes. Polled by ``InteractiveTuiAgent.wait_for_ready_signal``.
    # Captured live from `agy` 1.0.0; the full string is "Antigravity CLI <version>".
    TUI_READY_INDICATOR: ClassVar[str] = "Antigravity CLI"

    def get_expected_process_name(self) -> str:
        # `agy` is a single-file Go binary; ps/tmux show the literal command name.
        return "agy"

    def _send_enter_and_validate(self, tmux_target: str) -> None:
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

    def modify_env_vars(self, host: OnlineHostInterface, env_vars: dict[str, str]) -> None:
        """Expose the agy --log-file path to stream_transcript.sh.

        The streamer needs to grep agy's own log for ``Created conversation
        <uuid>`` lines to scope its watch to this agent's conversations.
        Setting the env var here keeps the script-side path consistent with
        the value we pass to ``agy --log-file`` in ``assemble_command``.
        """
        env_vars[_AGY_LOG_FILE_ENV_VAR] = str(self._get_agy_log_file_path())

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

        After dismissal, the transcript scripts and the background-tasks
        supervisor are installed under ``$MNGR_AGENT_STATE_DIR/commands/``.
        """
        self._interactively_dismiss_antigravity_dialogs(host, mngr_ctx)
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
                {_BACKGROUND_TASKS_SCRIPT_NAME: _load_antigravity_resource_script(_BACKGROUND_TASKS_SCRIPT_NAME)},
                concurrency_group,
            )

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

        * ``work_dir`` already in ``trustedWorkspaces`` -> no-op (idempotent
          re-provision).
        * ``source_path`` already in ``trustedWorkspaces`` -> silently add
          ``work_dir`` too. The user has previously trusted the source repo
          (interactively or via opt-in); spawning another worktree of the
          same repo shouldn't re-prompt, even though agy needs the worktree
          path written explicitly.
        * ``auto_dismiss_dialogs=True`` or ``mngr_ctx.is_auto_approve``:
          silently add both ``source_path`` and ``work_dir`` so future
          worktrees of the same source benefit from the silent-extend path
          above.
        * Interactive (``mngr_ctx.is_interactive``): prompt via
          ``click.confirm``. The prompt references ``source_path`` for stable
          wording across worktrees. On accept, add both ``source_path`` and
          ``work_dir``.
        * Non-interactive without opt-in, or user declines: log an explicit
          ``logger.error`` and ``raise SystemExit(1)``.

        Why ``SystemExit`` and not ``UserInputError``: ``provision_agent``
        wraps its body in a ``ConcurrencyExceptionGroup`` (see
        ``imbue.concurrency_group.concurrency_group.ConcurrencyGroup._exit``).
        Regular ``Exception`` raises get wrapped and surface as a noisy
        auto-diagnostics traceback; ``SystemExit`` is a ``BaseException``
        which the same ``_exit`` re-raises unwrapped (line 190-191),
        producing a clean exit.
        """
        workspace_path = str(self.work_dir)
        settings_path = get_antigravity_user_settings_path()
        existing_settings = read_antigravity_settings(host, settings_path)
        self._check_existing_trustedworkspaces_shape(settings_path, existing_settings)
        existing_trusted: list[str] = list(existing_settings.get(TRUSTED_WORKSPACES_KEY, []))

        if workspace_path in existing_trusted:
            logger.debug("Workspace {} already trusted in {}", workspace_path, settings_path)
            return

        source_path = self._find_git_source_path(mngr_ctx.concurrency_group) or self.work_dir
        source_path_str = str(source_path)
        is_worktree_of_trusted_source = source_path_str != workspace_path and source_path_str in existing_trusted

        if is_worktree_of_trusted_source:
            logger.debug(
                "Source {} is already trusted; silently extending trust to worktree {}",
                source_path_str,
                workspace_path,
            )
            self._write_workspace_trust(host, settings_path, existing_settings, [workspace_path])
            return

        if self.agent_config.auto_dismiss_dialogs or mngr_ctx.is_auto_approve:
            self._write_workspace_trust(
                host,
                settings_path,
                existing_settings,
                self._paths_to_add(workspace_path, source_path_str, existing_trusted),
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
            self._paths_to_add(workspace_path, source_path_str, existing_trusted),
        )

    @staticmethod
    def _paths_to_add(workspace_path: str, source_path_str: str, existing_trusted: list[str]) -> list[str]:
        """Return the paths to append to ``trustedWorkspaces``, deduped against existing entries.

        Always includes ``workspace_path`` (this is what agy's exact-match
        check needs to suppress the first-launch dialog) and additionally
        ``source_path_str`` when it differs and isn't already trusted (so
        future worktrees of the same source repo can take the silent-extend
        branch in ``_interactively_dismiss_antigravity_dialogs``).
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
        2. ``mkdir -p <state>/logs`` -- foreground step that guarantees the
           directory exists before agy attempts to open its ``--log-file``.
           The supervisor runs concurrently with agy, so we cannot rely on
           the supervisor (or any background watcher) creating this
           directory in time.
        3. ``agy <user_args> --log-file <state>/logs/agy_cli.log
           [--dangerously-skip-permissions]`` -- foreground process.

        Bash precedence note: ``A & B && C`` parses as ``A &`` followed by
        ``B && C``. The supervisor's subshell is therefore scoped to ``&``,
        while ``mkdir -p`` and ``agy`` form a foreground sequential chain.

        The ``--log-file`` arg pipes agy's internal log to a per-agent
        path; stream_transcript.sh greps it for ``Created conversation
        <uuid>`` to scope its watch to this agent.
        """
        log_file_path = self._get_agy_log_file_path()
        log_file_arg = f"--log-file {shlex.quote(str(log_file_path))}"
        extra_args: list[str] = [log_file_arg]
        if self.agent_config.auto_allow_permissions:
            extra_args.append(_DANGEROUSLY_SKIP_PERMISSIONS_FLAG)
        base_command = super().assemble_command(host, agent_args, command_override, initial_message)
        background_cmd = self._build_background_tasks_command()
        mkdir_cmd = f"mkdir -p {shlex.quote(str(log_file_path.parent))}"
        return CommandString(f"{background_cmd} {mkdir_cmd} && {base_command} {' '.join(extra_args)}")


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the antigravity agent type."""
    return ("antigravity", AntigravityAgent, AntigravityAgentConfig)

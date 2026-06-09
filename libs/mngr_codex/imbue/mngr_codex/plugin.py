"""``mngr_codex`` plugin -- registers the ``codex`` agent type for the OpenAI Codex CLI.

The Codex CLI (the Rust ``codex`` binary) is the closest CLI yet to Claude Code:
a Claude-style hook system (``UserPromptSubmit``/``Stop``/``SubagentStop``/...),
a first-class config-dir override env var, file-based auth, resume-by-id, and an
append-as-you-go session JSONL. So ``mngr_codex`` follows the ``mngr_claude``
shape, using ``mngr_antigravity`` only for the banner-poll readiness fallback.

Per-agent ``CODEX_HOME`` (the isolation lever)
----------------------------------------------
Codex resolves its whole config/auth/session/hook tree from ``CODEX_HOME``
(default ``~/.codex``). Pointing each agent at its own ``CODEX_HOME`` under the
agent state dir -- injected only on the codex process via ``env CODEX_HOME=...``
-- isolates the agent's config/permissions/transcripts while leaving the user's
real ``$HOME`` untouched. This is the preferred shape (cf. ``CLAUDE_CONFIG_DIR``);
no ``$HOME`` relocation, no workspace symlink (codex accepts the dotted
``~/.mngr/...`` cwd), and no heavy per-home caches to re-share.

The per-agent ``CODEX_HOME`` tree (mngr-owned files rewritten each provision;
see :mod:`imbue.mngr_codex.codex_config`)::

    config.toml              # model, sandbox, approval, credential-store pin, [notice], trust
    hooks.json               # the active-marker lifecycle hooks
    auth.json -> ~/.codex/auth.json   # symlink: shared login, write-through refresh
    .personality_migration   # empty NUX-skip marker
    sessions/.../rollout-*.jsonl      # codex-owned transcripts

Auth: codex writes ``auth.json`` in place (verified against source: ``O_TRUNC``,
no atomic rename) and its refresh path reloads-before-refreshing, so a per-agent
``auth.json`` *symlink* to the shared ``~/.codex/auth.json`` lets one login
authenticate every agent and propagates refreshes. ``cli_auth_credentials_store
= "file"`` is pinned in config.toml so codex never falls back to a keyring store
keyed by the (per-agent) ``CODEX_HOME`` path, which would defeat sharing.

Lifecycle marker: four hooks maintain the ``active`` marker that
``BaseAgent.get_lifecycle_state`` reads (RUNNING vs WAITING). Codex subagents run
*asynchronously* -- the root's ``Stop`` fires while subagents are still running,
their ``SubagentStop`` hooks arrive later with no ordering guarantee, and there
is no ``fullyIdle`` signal -- so the marker is recomputed under a lock from two
pieces of tracked state: a root-turn flag (``codex_root_active``) and one file
per in-flight subagent (under ``codex_subagents/``). ``UserPromptSubmit`` sets
the flag, ``Stop`` clears it, and ``SubagentStart``/``SubagentStop`` register and
deregister each subagent, so the marker stays RUNNING until the root turn **and**
every subagent are done. A recorded root ``session_id`` further guards the
``Stop`` clear against a nested/recursive ``codex`` process sharing the same
``CODEX_HOME``. See :func:`codex_config.build_codex_hooks_config`, the shared
``codex_marker_state.sh`` helper, and the ``set_active_marker.sh`` /
``clear_active_marker.sh`` / ``subagent_started.sh`` / ``subagent_stopped.sh``
resources.

Readiness: codex's ``SessionStart`` hook fires *lazily* (on the first prompt,
not at TUI launch -- openai/codex issue #15269), so there is no pre-input
sentinel; readiness falls back to the ``InteractiveTuiAgent`` banner poll on a
stable header string (``TUI_READY_INDICATOR``).

Hook trust: codex requires command hooks to be trusted before they run. mngr
passes ``--dangerously-bypass-hook-trust`` so its own lifecycle hooks run
without a per-hash trust dance. Because trusting the workspace also lets codex
load any repo-local ``.codex/hooks.json``, that bypass is consent-gated together
with workspace trust (see ``_ensure_source_repo_trusted``) -- mngr never runs an
agent on untrusted code, or bypasses codex's hook review, without the user's
say-so.

Resume: ``mngr stop``/``start`` resumes the prior conversation. There is no
``--session-id`` pin at fresh start, so the ``UserPromptSubmit`` hook records the
root ``session_id``; ``assemble_command`` reads it and shell-evaluates
``codex resume <id>`` (codex's rollout JSONL survives the hard kill ``mngr stop``
performs). Transcript scoping uses the captured rollout ``transcript_path``.
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

from imbue.imbue_common.logging import log_span
from imbue.mngr import hookimpl
from imbue.mngr.agents.common_transcript import maybe_provision_common_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_raw_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_scripts_to_commands_dir
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import send_enter_best_effort
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.common import symlink_on_host
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.git_utils import find_git_source_path
from imbue.mngr_codex import resources as _codex_resources
from imbue.mngr_codex.codex_config import BACKGROUND_TASKS_SCRIPT_NAME
from imbue.mngr_codex.codex_config import CLEAR_ACTIVE_MARKER_SCRIPT_NAME
from imbue.mngr_codex.codex_config import COMMON_TRANSCRIPT_SCRIPT_NAME
from imbue.mngr_codex.codex_config import MARKER_STATE_LIB_SCRIPT_NAME
from imbue.mngr_codex.codex_config import RAW_TRANSCRIPT_SCRIPT_NAME
from imbue.mngr_codex.codex_config import ROOT_SESSION_FILENAME
from imbue.mngr_codex.codex_config import SET_ACTIVE_MARKER_SCRIPT_NAME
from imbue.mngr_codex.codex_config import SUBAGENT_STARTED_SCRIPT_NAME
from imbue.mngr_codex.codex_config import SUBAGENT_STOPPED_SCRIPT_NAME
from imbue.mngr_codex.codex_config import build_codex_config
from imbue.mngr_codex.codex_config import build_codex_hooks_config
from imbue.mngr_codex.codex_config import get_codex_auth_path
from imbue.mngr_codex.codex_config import get_codex_config_path
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.mngr_codex.codex_config import get_codex_hooks_path
from imbue.mngr_codex.codex_config import get_codex_personality_migration_path
from imbue.mngr_codex.codex_config import is_project_trusted
from imbue.mngr_codex.codex_config import merge_project_trust
from imbue.mngr_codex.codex_config import read_codex_config
from imbue.mngr_codex.codex_config import serialize_codex_config
from imbue.mngr_codex.codex_config import serialize_codex_hooks

# Top-level codex flag: run enabled hooks without the per-hash trust review.
# Safe here because the per-agent CODEX_HOME is mngr-isolated and contains only
# mngr's own lifecycle hooks; the broader effect (repo-local .codex/hooks.json
# running unreviewed once the workspace is trusted) is consent-gated together
# with workspace trust in ``_ensure_source_repo_trusted``.
_DANGEROUSLY_BYPASS_HOOK_TRUST_FLAG: Final[str] = "--dangerously-bypass-hook-trust"

# codex approval policy that suppresses every interactive approval dialog while
# keeping the sandbox on (the right unattended default). Applied only when
# ``auto_allow_permissions`` is set; otherwise codex's trust-derived default
# (``on-request`` for a trusted project) stands.
_APPROVAL_POLICY_NEVER: Final[str] = "never"


def _load_codex_resource_script(filename: str) -> str:
    """Load a resource script from the mngr_codex resources package."""
    resource_files = importlib.resources.files(_codex_resources)
    return resource_files.joinpath(filename).read_text()


class CodexAgentConfig(AgentTypeConfig):
    """Config for the codex agent type."""

    command: CommandString = Field(
        default=CommandString("codex"),
        description="Command to run the OpenAI Codex CLI.",
    )
    cli_args: tuple[str, ...] = Field(
        default=(),
        description="Additional CLI arguments to pass to codex (rarely needed; most settings "
        "flow through the per-agent config.toml). Note: with conversation resume, these are "
        "appended after the `resume <id>` subcommand, so prefer config_overrides for anything "
        "the `resume` subcommand would reject.",
    )
    # model is intentionally not defaulted: codex picks the account's default,
    # and a ChatGPT-account login rejects some ``*-codex`` model slugs, so
    # forcing one could break the agent. Set this to a model your account
    # supports (e.g. "gpt-5.5") if codex's default fails (see the README).
    model: str | None = Field(
        default=None,
        description="Model slug to pin in the per-agent config.toml (e.g. 'gpt-5.5'). None leaves "
        "codex's own default in force. A ChatGPT-account login rejects some *-codex model slugs.",
    )
    model_reasoning_effort: str | None = Field(
        default=None,
        description="Reasoning effort to pin (none|minimal|low|medium|high|xhigh). None leaves the default.",
    )
    sandbox_mode: str | None = Field(
        default="workspace-write",
        description="codex sandbox policy (read-only|workspace-write|danger-full-access). "
        "None leaves codex's default. Written to the per-agent config.toml.",
    )
    # auto_allow_permissions sets ``approval_policy = "never"`` in the per-agent
    # config.toml, which suppresses every approval dialog while keeping the
    # sandbox on. Unlike antigravity (whose hook allow-decision does not gate the
    # dialog), codex honors ``approval_policy`` directly, so no skip-all flag is
    # needed. Sandbox isolation is governed separately by ``sandbox_mode``.
    auto_allow_permissions: bool = Field(
        default=False,
        description="When True, set approval_policy='never' so codex never prompts for tool "
        "approval (the sandbox set by sandbox_mode still applies).",
    )
    # config_overrides mirrors mngr_claude's settings_overrides / antigravity's:
    # a free-form blob merged last (shallow) into the per-agent config.toml.
    # Covers anything not surfaced as a typed knob (extra [notice] keys, a
    # [profiles.*] table, model_provider, etc.).
    config_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Key-value overrides merged last into the per-agent config.toml. "
        'Example: {"model_provider": "openai", "approval_policy": "on-request"}.',
    )
    # auto_dismiss_dialogs is the mngr_claude-style auto-consent knob. When True
    # (or under ``mngr create --yes``), provisioning silently records workspace
    # trust + the hook-bypass consent without prompting. When False (default),
    # the user is prompted via click.confirm before mngr mutates the global
    # config or runs codex with hook review bypassed.
    auto_dismiss_dialogs: bool = Field(
        default=False,
        description="When True, trust the source repo and allow the codex hook-review bypass "
        "without prompting. When False (default), the user is prompted interactively.",
    )
    # emit_common_transcript gates the rollout -> common-schema converter. The
    # raw transcript is always captured (HasTranscriptMixin); only the common
    # converter is gated.
    emit_common_transcript: bool = Field(
        default=True,
        description="When True, emit a common-schema transcript that `mngr transcript` reads.",
    )


class CodexAgent(InteractiveTuiAgent[CodexAgentConfig], HasCommonTranscriptMixin):
    """Agent implementation for the OpenAI Codex CLI (``codex``)."""

    # Stable substring of codex's header box, which renders together with the
    # input composer once the TUI is ready to receive keystrokes (verified live
    # against codex 0.138.0). codex has no pre-input readiness hook -- its
    # ``SessionStart`` fires lazily on the first prompt (openai/codex #15269) --
    # so this banner poll is the readiness signal, as with antigravity. There is
    # no OAuth splash delay (auth is a file), so unlike agy the header box is a
    # safe indicator: it appears only with the rendered, ready composer.
    TUI_READY_INDICATOR: ClassVar[str] = "/model to change"

    def get_expected_process_name(self) -> str:
        # The codex CLI is a single Rust binary; ps/tmux show the literal name.
        return "codex"

    def _send_enter_and_validate(self, tmux_target: TmuxWindowTarget) -> None:
        # codex submits the composer on Enter. Upstream ``wait_for_paste_visible``
        # already confirmed the message landed in the pane before we get here, so
        # a best-effort Enter is the right strategy (as with antigravity).
        send_enter_best_effort(self, tmux_target)

    @property
    def is_common_transcript_enabled(self) -> bool:
        return self.agent_config.emit_common_transcript

    def get_raw_transcript_scripts(self) -> Mapping[str, str]:
        """Return the codex raw-transcript streamer (always provisioned)."""
        return {RAW_TRANSCRIPT_SCRIPT_NAME: _load_codex_resource_script(RAW_TRANSCRIPT_SCRIPT_NAME)}

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        """Return the codex common-transcript converter."""
        return {COMMON_TRANSCRIPT_SCRIPT_NAME: _load_codex_resource_script(COMMON_TRANSCRIPT_SCRIPT_NAME)}

    def _get_codex_home(self) -> Path:
        """Per-agent ``CODEX_HOME`` (under the agent state dir)."""
        return get_codex_home(self._get_agent_dir())

    def _get_root_session_file_path(self) -> Path:
        """Per-agent file recording the root codex ``session_id`` (for resume + marker gating).

        Written by ``set_active_marker.sh`` at a turn boundary; read here in
        ``assemble_command`` to resume via ``codex resume <id>``. Lives directly
        under the agent state dir so the hook's
        ``$MNGR_AGENT_STATE_DIR/{ROOT_SESSION_FILENAME}`` and this path resolve to
        the same file.
        """
        return self._get_agent_dir() / ROOT_SESSION_FILENAME

    def _resolve_user_codex_home(self, host: OnlineHostInterface) -> Path:
        """Resolve the user's real ``CODEX_HOME`` over the host shell.

        Honors a ``CODEX_HOME`` override and falls back to ``$HOME/.codex``, read
        from the host shell (not ``Path.home()``) so the auth source is correct
        on remote hosts. This is the shared ``auth.json`` the per-agent token
        symlinks to.
        """
        result = host.execute_idempotent_command('printf %s "${CODEX_HOME:-$HOME/.codex}"', timeout_seconds=10.0)
        resolved = result.stdout.strip()
        if not result.success or not resolved:
            logger.error(
                "Could not resolve the user's CODEX_HOME for codex provisioning "
                "(exit_success={}, stdout={!r}); cannot locate the shared auth.json.",
                result.success,
                result.stdout,
            )
            raise SystemExit(1)
        return Path(resolved)

    def _resolve_canonical_path(self, host: OnlineHostInterface, path: Path) -> str:
        """Resolve ``path`` to its canonical absolute form over the host shell.

        codex canonicalizes the cwd (resolving symlinks) before its project-trust
        lookup, so the trust key we seed must be canonical too (e.g. macOS
        ``/tmp`` -> ``/private/tmp``). Resolved on the host so it is correct
        remotely. Falls back to the input path string if resolution fails (the
        literal path is also one of codex's lookup keys).
        """
        quoted = shlex.quote(str(path))
        result = host.execute_idempotent_command(
            f"cd {quoted} 2>/dev/null && pwd -P || printf %s {quoted}", timeout_seconds=10.0
        )
        resolved = result.stdout.strip()
        return resolved or str(path)

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Build the per-agent ``CODEX_HOME`` tree and install the transcript scripts.

        Steps:

        1. Resolve the user's real ``CODEX_HOME`` (the shared-auth source) and the
           canonical work-dir path (the trust key codex matches).
        2. Ensure the source repo is trusted (consent-gated; also gates the
           hook-review bypass) -- a clean ``SystemExit`` if consent is unavailable.
        3. Build the per-agent ``CODEX_HOME`` (config.toml, hooks.json, the
           auth.json symlink, the NUX-skip marker).
        4. Install the transcript scripts + background supervisor under
           ``$MNGR_AGENT_STATE_DIR/commands/``.
        """
        user_codex_home = self._resolve_user_codex_home(host)
        canonical_work_dir = self._resolve_canonical_path(host, self.work_dir)
        self._ensure_source_repo_trusted(host, user_codex_home, mngr_ctx)
        self._provision_codex_home(host, user_codex_home, canonical_work_dir)
        with mngr_ctx.concurrency_group.make_concurrency_group("codex_provisioning") as concurrency_group:
            provision_raw_transcript_scripts(self, host, self._get_agent_dir(), concurrency_group)
            maybe_provision_common_transcript_scripts(self, host, self._get_agent_dir(), concurrency_group)
            provision_scripts_to_commands_dir(
                host,
                self._get_agent_dir(),
                {
                    BACKGROUND_TASKS_SCRIPT_NAME: _load_codex_resource_script(BACKGROUND_TASKS_SCRIPT_NAME),
                    # Shared helper sourced by the four lifecycle hooks: marker
                    # state paths, the mkdir-based lock, and the recompute.
                    MARKER_STATE_LIB_SCRIPT_NAME: _load_codex_resource_script(MARKER_STATE_LIB_SCRIPT_NAME),
                    # UserPromptSubmit hook: set the root-turn flag, record the
                    # root session id + transcript path (see build_codex_hooks_config).
                    SET_ACTIVE_MARKER_SCRIPT_NAME: _load_codex_resource_script(SET_ACTIVE_MARKER_SCRIPT_NAME),
                    # Stop hook: clear the root-turn flag and recompute the marker
                    # (in-flight subagents keep it present).
                    CLEAR_ACTIVE_MARKER_SCRIPT_NAME: _load_codex_resource_script(CLEAR_ACTIVE_MARKER_SCRIPT_NAME),
                    # SubagentStart/Stop hooks: track in-flight subagents so the
                    # marker stays RUNNING while async subagents are still working.
                    SUBAGENT_STARTED_SCRIPT_NAME: _load_codex_resource_script(SUBAGENT_STARTED_SCRIPT_NAME),
                    SUBAGENT_STOPPED_SCRIPT_NAME: _load_codex_resource_script(SUBAGENT_STOPPED_SCRIPT_NAME),
                },
                concurrency_group,
            )

    def _provision_codex_home(self, host: OnlineHostInterface, user_codex_home: Path, canonical_work_dir: str) -> None:
        """Write the mngr-owned per-agent ``CODEX_HOME`` tree (idempotent each provision).

        Provisions the auth.json symlink, config.toml (model/sandbox/approval +
        the credential-store pin + the trusted work-dir + notice suppressors +
        overrides), hooks.json, and the personality-migration NUX-skip marker.
        ``host.write_text_file`` creates intermediate dirs; codex-owned
        ``sessions/`` is left intact across re-provision.
        """
        codex_home = self._get_codex_home()
        self._provision_auth_symlink(host, user_codex_home, codex_home)

        approval_policy = _APPROVAL_POLICY_NEVER if self.agent_config.auto_allow_permissions else None
        config = build_codex_config(
            model=self.agent_config.model,
            model_reasoning_effort=self.agent_config.model_reasoning_effort,
            sandbox_mode=self.agent_config.sandbox_mode,
            approval_policy=approval_policy,
            trusted_projects=[canonical_work_dir],
            config_overrides=self.agent_config.config_overrides,
        )
        config_path = get_codex_config_path(codex_home)
        with log_span("Writing per-agent codex config to {}", config_path):
            host.write_text_file(config_path, serialize_codex_config(config))

        hooks_path = get_codex_hooks_path(codex_home)
        with log_span("Installing codex hooks at {}", hooks_path):
            host.write_text_file(hooks_path, serialize_codex_hooks(build_codex_hooks_config()))

        # Empty marker: codex skips the personality-migration prompt when it exists.
        host.write_text_file(get_codex_personality_migration_path(codex_home), "")

    def _provision_auth_symlink(self, host: OnlineHostInterface, user_codex_home: Path, codex_home: Path) -> None:
        """Symlink the per-agent ``auth.json`` to the shared user ``auth.json``.

        Always create the symlink, even when the shared file does not exist yet
        (a dangling symlink). codex writes ``auth.json`` in place (verified
        against source -- ``O_TRUNC``, no atomic rename), so the first agent's
        login writes *through* the symlink to the shared path, authenticating
        every agent and propagating refreshes (codex's refresh reloads the file
        first, so concurrent agents don't clobber each other). The
        ``cli_auth_credentials_store = "file"`` pin in config.toml keeps codex on
        the file store rather than a ``CODEX_HOME``-keyed keyring entry that would
        defeat sharing.
        """
        symlink_on_host(
            host,
            get_codex_auth_path(user_codex_home),
            get_codex_auth_path(codex_home),
            ensure_source_parent=True,
        )

    def _find_git_source_path(self, mngr_ctx: MngrContext) -> Path | None:
        """Find the source repo root for this agent's ``work_dir`` (or None if not in a repo).

        Delegates to the shared core helper (also used by mngr_claude/antigravity).
        The source-repo root is the durable thing trust is persisted against, so a
        single grant covers every worktree of the same repo. Kept as a method so
        tests can override without monkeypatching.
        """
        return find_git_source_path(self.work_dir, mngr_ctx.concurrency_group)

    def _ensure_source_repo_trusted(
        self, host: OnlineHostInterface, user_codex_home: Path, mngr_ctx: MngrContext
    ) -> None:
        """Ensure the source repo is trusted, persisting durable trust to the user's global config.

        This single consent covers two things that are enabled together by
        trusting the workspace:

        * codex's first-launch folder-trust dialog (seeded per-agent in
          ``_provision_codex_home`` via ``[projects."<work_dir>"] trusted``), and
        * the ``--dangerously-bypass-hook-trust`` the launch command passes so
          mngr's lifecycle hooks run -- which, on a trusted workspace, also lets
          codex load any repo-local ``.codex/hooks.json`` unreviewed.

        Gating mirrors mngr_claude/antigravity: source already trusted in the
        user's global ``config.toml`` -> no-op (consent previously given);
        ``auto_dismiss_dialogs`` or ``mngr_ctx.is_auto_approve`` -> silent;
        interactive -> ``click.confirm`` (default False); non-interactive without
        opt-in, or declined -> ``SystemExit(1)``. We never run an agent on
        untrusted code, or bypass codex's hook review, without the user's say-so.

        ``SystemExit`` (not ``UserInputError``) for the same reason as the other
        plugins: ``provision_agent`` wraps its body in a ``ConcurrencyExceptionGroup``
        that re-raises ``BaseException`` unwrapped but turns ``Exception`` into a
        noisy auto-diagnostics traceback.
        """
        user_config_path = get_codex_config_path(user_codex_home)
        existing_config = read_codex_config(host, user_config_path)

        source_path = self._find_git_source_path(mngr_ctx) or self.work_dir
        canonical_source = self._resolve_canonical_path(host, source_path)
        if is_project_trusted(existing_config, canonical_source):
            logger.debug("Source {} already trusted in {}", canonical_source, user_config_path)
            return

        if not (self.agent_config.auto_dismiss_dialogs or mngr_ctx.is_auto_approve):
            if not mngr_ctx.is_interactive:
                logger.error(
                    "Source directory {} is not trusted by the Codex CLI. mngr will not silently "
                    "run a codex agent on untrusted code (which also bypasses codex's hook review). "
                    "Re-run interactively to be prompted, re-run with `--yes`, or set "
                    "`auto_dismiss_dialogs = true` on the codex agent type.",
                    canonical_source,
                )
                raise SystemExit(1)
            if not self._prompt_user_to_trust_workspace(Path(canonical_source), user_config_path):
                logger.error("User declined to trust {}. Aborting agent creation.", canonical_source)
                raise SystemExit(1)

        merged = merge_project_trust(existing_config, canonical_source)
        if merged is not None:
            with log_span("Persisting trusted source repo {} in {}", canonical_source, user_config_path):
                host.write_text_file(user_config_path, serialize_codex_config(merged))

    def _prompt_user_to_trust_workspace(self, source_path: Path, config_path: Path) -> bool:
        """Prompt to trust the source repo (and allow the codex hook-review bypass).

        Refers to the *source* directory (git repo root, or the bare work_dir)
        so the user sees a stable path across worktrees. Defaults to False so a
        stray Enter never grants trust. Exposed as a method so tests can override
        without monkeypatching.
        """
        logger.info(
            "\nSource directory {} is not yet trusted by the Codex CLI.\n"
            "To run a codex agent here, mngr needs to:\n"
            "  - add a trust entry for this directory to {}, and\n"
            "  - run codex with `--dangerously-bypass-hook-trust` so mngr's lifecycle hooks\n"
            "    work (this also lets codex run any repo-local .codex/hooks.json unreviewed).\n",
            source_path,
            config_path,
        )
        return click.confirm(
            f"Trust {source_path} and allow mngr to run codex with its hook review bypassed?",
            default=False,
        )

    def _build_background_tasks_command(self) -> str:
        """Shell snippet that launches the backgrounded transcript supervisor.

        One backgrounded subshell owns the streamer + converter lifecycle
        (pidfile-deduped, restart-on-death), so replaying the command on restart
        is safe. Mirrors mngr_claude/antigravity.
        """
        script_path = f"$MNGR_AGENT_STATE_DIR/commands/{BACKGROUND_TASKS_SCRIPT_NAME}"
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

        1. ``( bash codex_background_tasks.sh <session> ) &`` -- backgrounded
           transcript supervisor (scoped to ``&`` so the foreground process is
           codex itself, which liveness/readiness detection keys off).
        2. ``mkdir -p <CODEX_HOME>`` -- ensure the config dir exists.
        3. ``cd <work_dir>`` -- codex's cwd becomes the (trusted) work dir; codex
           accepts the dotted ``~/.mngr/...`` path, so no symlink workaround.
        4. ``{ <resume-prelude>; env CODEX_HOME=<home> codex
           --dangerously-bypass-hook-trust "$@" <cli/agent args>; }`` -- codex in
           the foreground under the per-agent ``CODEX_HOME`` (injected only on the
           codex process). The bypass flag goes before the subcommand so it
           applies whether the prelude selected ``resume <id>`` or a fresh start.

        The resume-prelude reads the root ``session_id`` from
        ``codex_root_session`` (written by the ``UserPromptSubmit`` hook) and sets
        ``$@`` to ``resume <id>`` so a restart continues the conversation; empty
        otherwise. It is shell-evaluated here because the stored command is
        replayed on every ``mngr start``. codex's rollout JSONL is written
        append-and-flush per line, so it survives the hard kill ``mngr stop``
        performs and ``codex resume`` reconstructs history from it.

        Bash precedence: ``A & B && C`` parses as ``A &`` then ``B && C``, so the
        supervisor subshell is backgrounded while ``mkdir`` / ``cd`` / the codex
        group form the foreground chain.
        """
        codex_home = self._get_codex_home()
        base = str(command_override) if command_override is not None else str(self.agent_config.command)

        extra_args = list(self.agent_config.cli_args) + [shlex.quote(arg) for arg in agent_args]
        extra_str = (" " + " ".join(extra_args)) if extra_args else ""

        background_cmd = self._build_background_tasks_command()
        mkdir_cmd = f"mkdir -p {shlex.quote(str(codex_home))}"
        cd_cmd = f"cd {shlex.quote(str(self.work_dir))}"
        home_prefix = f"env CODEX_HOME={shlex.quote(str(codex_home))}"

        # Resume the root conversation via `codex resume <id>`, shell-evaluated
        # because the stored command is replayed on each restart. `set --` / "$@"
        # appends the subcommand without unquoted word-splitting, so it works
        # under both bash and zsh; an empty id leaves "$@" empty (a fresh start).
        quoted_root_file = shlex.quote(str(self._get_root_session_file_path()))
        resume_prelude = (
            f"__mngr_sid=$(cat {quoted_root_file} 2>/dev/null || true); set --; "
            'if [ -n "$__mngr_sid" ]; then set -- resume "$__mngr_sid"; fi'
        )
        codex_invocation = f"{home_prefix} {base} {_DANGEROUSLY_BYPASS_HOOK_TRUST_FLAG}"

        return CommandString(
            f'{background_cmd} {mkdir_cmd} && {cd_cmd} && {{ {resume_prelude}; {codex_invocation} "$@"{extra_str} ; }}'
        )


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the codex agent type."""
    return ("codex", CodexAgent, CodexAgentConfig)

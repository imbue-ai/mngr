"""``mngr_antigravity`` plugin -- registers the ``antigravity`` agent type for Google's Antigravity CLI (``agy``).

Antigravity replaced Gemini CLI on 2026-05-19; the legacy request path turns
off for paid-tier accounts on 2026-06-18. Despite the Gemini lineage the new
CLI is architecturally closer to Claude Code than to Gemini -- hook event
names and permission-dialog phrasing match Claude's surface. The process name
is the Go binary ``agy``.

Per-agent ``$HOME`` (the core mechanism)
----------------------------------------
``agy`` resolves its entire config/permission/auth/session tree from
``$HOME/.gemini`` and has **no** config-dir override env var and no
per-workspace settings/permission loading (``--add-dir`` does not load
settings/permissions/model from the added dir). The only lever that yields a
per-agent ``settings.json`` -- and therefore per-agent permissions, per-agent
model, and isolated transcripts/conversations -- is a **per-agent ``$HOME``**.

So ``provision`` always builds a per-agent ``$HOME`` tree under the agent state
dir and ``assemble_command`` always launches ``agy`` under it
(``env HOME=<home> agy ...``). This is unconditional: there is no
"isolated vs non-isolated" branch. Whether an agent is locked down or open is
purely *data* -- whether ``settings_overrides`` carries a ``permissions`` block
-- not a structural fork. Hooks, onboarding, trust, and auth therefore have
exactly one code path.

The per-agent ``$HOME`` tree (rooted at
``<agent_state_dir>/plugin/antigravity/home/``, which is the ``$HOME`` for the
agy process; mngr-owned files rewritten idempotently each ``provision``)::

    .gemini/
      antigravity-cli/
        settings.json
        cache/onboarding.json
        antigravity-oauth-token
      config/hooks.json

where ``settings.json`` is a copy of the user's settings (when
``sync_home_settings``) plus the workspace trust, ``settings_overrides``, and
the mngr-owned lifecycle ``statusLine`` (applied last so it wins);
``cache/onboarding.json`` is the NUX seed that skips the first-run theme/ToS
flow; ``antigravity-oauth-token`` is a symlink to the user's shared file token
(auth) -- created even when that token doesn't exist yet, so the first agent's
login writes *through* it to the shared path and authenticates every agent (agy
writes the token in place; copy mode is available for full isolation); and
``config/hooks.json`` holds the conversation-id capture hook (agy executes it
from there directly -- no ``--add-dir``).

Lifecycle: agy invokes a configured ``statusLine`` command on every agent-state
change (JSON payload on stdin), and ``statusline.sh`` is the single source of
truth (see ``build_antigravity_statusline_settings``). It maintains an
``active`` marker that ``BaseAgent.get_lifecycle_state`` reads to report RUNNING
while the agent works and WAITING when idle (agy maintains no such marker on its
own), records the root conversation for resume, and fires the tmux
message-submission signal. agy's top-level ``agent_state`` already aggregates
subagent activity (stays ``working`` while a subagent runs), so this single
state check captures the whole-turn busy/idle invariant on its own.

Hooks: a single ``PreInvocation`` handler captures every conversation id (incl.
subagents', which ``statusLine`` does not surface) for transcript scoping (see
``build_antigravity_hooks_config``). Because the per-agent ``$HOME`` is
unconditional, agy executes it from ``$HOME/.gemini/config/hooks.json`` directly
-- no ``--add-dir`` symlink workaround.

Permissions: routed through the per-agent ``settings.json`` (a ``permissions``
block in ``settings_overrides``) and/or ``--dangerously-skip-permissions``
(``auto_allow_permissions``). NOT via a hook: agy's documented ``PreToolUse``
``{"decision": "allow"}`` output does not gate the ``run_command`` confirmation
dialog (verified live against agy 1.0.3 -- the hook runs but the dialog still
appears).

Readiness is signalled by the ``InteractiveTuiAgent`` banner-poll: it gates
"input row drawn and able to receive a paste", which the ``statusLine``
``agent_state`` does not (that is about the agent loop and can be ``idle`` before
the input row renders). A permission dialog can't be detected via hooks either
-- none fires while the agent is blocked at it -- so the agent exposes no
permission-specific WAITING reason.

Transcript support: enabled by default. ``stream_transcript.sh`` tails agy's
per-conversation JSONL files under ``$ANTIGRAVITY_APP_DATA_DIR`` (pointed at the
per-agent home's ``antigravity-cli`` dir via ``modify_env_vars``), filtered to
conversation IDs that *this* agent worked on (discovered from the per-agent
conversation-ids file the ``PreInvocation`` capture hook maintains; see
``CONVERSATION_IDS_FILENAME`` and ``capture_conversation_id.sh``).
``common_transcript.sh`` converts to the agent-agnostic schema that ``mngr
transcript`` reads.
"""

from __future__ import annotations

import importlib.resources
import shlex
from collections.abc import Mapping
from collections.abc import Sequence
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
from imbue.mngr.agents.tui_utils import send_enter_via_tmux_wait_for_hook
from imbue.mngr.api.preservation import PreservedItem
from imbue.mngr.api.preservation import build_transcript_preserved_items
from imbue.mngr.api.preservation import flag_gated_items
from imbue.mngr.api.preservation import preserve_agent_state
from imbue.mngr.api.preservation import preserve_host_agents_on_destroy
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import copy_on_host
from imbue.mngr.hosts.common import symlink_on_host
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.utils.git_utils import find_git_source_path
from imbue.mngr_antigravity import resources as _antigravity_resources
from imbue.mngr_antigravity.antigravity_config import CAPTURE_CONVERSATION_ID_SCRIPT_NAME
from imbue.mngr_antigravity.antigravity_config import CONVERSATIONS_DIR_RELATIVE_TO_HOME
from imbue.mngr_antigravity.antigravity_config import CONVERSATION_IDS_FILENAME
from imbue.mngr_antigravity.antigravity_config import ROOT_CONVERSATION_FILENAME
from imbue.mngr_antigravity.antigravity_config import STATUSLINE_SCRIPT_NAME
from imbue.mngr_antigravity.antigravity_config import TRUSTED_WORKSPACES_KEY
from imbue.mngr_antigravity.antigravity_config import USER_STATUSLINE_COMMAND_FILENAME
from imbue.mngr_antigravity.antigravity_config import build_antigravity_hooks_config
from imbue.mngr_antigravity.antigravity_config import build_antigravity_statusline_settings
from imbue.mngr_antigravity.antigravity_config import build_isolated_settings
from imbue.mngr_antigravity.antigravity_config import build_onboarding_seed
from imbue.mngr_antigravity.antigravity_config import extract_statusline_command
from imbue.mngr_antigravity.antigravity_config import get_antigravity_cli_dir
from imbue.mngr_antigravity.antigravity_config import get_antigravity_hooks_config_path
from imbue.mngr_antigravity.antigravity_config import get_antigravity_oauth_token_path
from imbue.mngr_antigravity.antigravity_config import get_antigravity_onboarding_cache_path
from imbue.mngr_antigravity.antigravity_config import get_antigravity_settings_path
from imbue.mngr_antigravity.antigravity_config import merge_trusted_workspace
from imbue.mngr_antigravity.antigravity_config import read_antigravity_settings
from imbue.mngr_antigravity.antigravity_config import serialize_antigravity_hooks
from imbue.mngr_antigravity.antigravity_config import serialize_antigravity_settings

# Top-level CLI flag exposed by `agy --help`; auto-approves every tool call.
# Same spelling as Claude Code's flag. Used (rather than a PreToolUse hook)
# for ``auto_allow_permissions`` because agy's documented hook allow-decision
# does not actually gate the run_command confirmation dialog -- see the
# ``auto_allow_permissions`` field comment and ``build_antigravity_hooks_config``.
_DANGEROUSLY_SKIP_PERMISSIONS_FLAG: Final[str] = "--dangerously-skip-permissions"

_COMMON_TRANSCRIPT_SCRIPT_NAME: Final[str] = "common_transcript.sh"
# The python converter common_transcript.sh invokes (python3
# <dir>/common_transcript_convert.py); provisioned alongside the .sh.
_COMMON_TRANSCRIPT_CONVERT_SCRIPT_NAME: Final[str] = "common_transcript_convert.py"
_RAW_TRANSCRIPT_SCRIPT_NAME: Final[str] = "stream_transcript.sh"
# The python3 decoder stream_transcript.sh invokes to read agy's SQLite conversation
# store (agy >= 1.0.4); provisioned alongside the streamer into the commands/ dir.
_TRANSCRIPT_DECODER_SCRIPT_NAME: Final[str] = "decode_agy_transcript.py"

# Supervisor script provisioned into the agent's commands/ dir; owns the
# lifecycle of the raw streamer and (when enabled) the common-transcript
# converter. Mirrors the mngr_claude background-tasks pattern.
_BACKGROUND_TASKS_SCRIPT_NAME: Final[str] = "antigravity_background_tasks.sh"

# Env var consumed by stream_transcript.sh to locate agy's per-conversation
# ``brain/<conv_id>/.system_generated/logs/transcript.jsonl`` files. We point it
# at the per-agent home's ``antigravity-cli`` dir so the streamer (which runs in
# the supervisor subshell on the *real* HOME) finds the relocated transcripts.
# This var is a no-op for agy itself (absent from the binary) -- HOME relocation,
# not this var, is what isolates agy; this only steers the streamer script.
# (Conversation-id discovery uses the capture-hook file, not a log grep; see
# ``CONVERSATION_IDS_FILENAME`` and ``capture_conversation_id.sh``.)
_ANTIGRAVITY_APP_DATA_DIR_ENV_VAR: Final[str] = "ANTIGRAVITY_APP_DATA_DIR"

# Relative path under $MNGR_AGENT_STATE_DIR for the agy --log-file. Keeping
# it under logs/ groups it with the other per-agent log artifacts. This is a
# debugging log; conversation-id discovery uses the capture-hook file (see
# ``CONVERSATION_IDS_FILENAME``), not this log.
_AGY_LOG_FILE_RELATIVE_PATH: Final[str] = "logs/agy_cli.log"

# Per-agent $HOME for the agy process, under the agent state dir. agy resolves
# ``GeminiDir = $HOME/.gemini`` from it (works under this dotted ``~/.mngr/...``
# path -- agy only rejects dot-prefixed *workspace*/``--add-dir`` paths, not its
# own config dir). Mirrors mngr_claude's per-agent ``get_claude_config_dir``.
_AGY_HOME_RELATIVE_PATH: Final[tuple[str, ...]] = ("plugin", "antigravity", "home")

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
# (HOME isolation does not change this: the work_dir is still a hidden path
# agy refuses as a *workspace*, even though it accepts a hidden config dir.)
_AGY_WORKSPACE_SYMLINK_PARENT: Final[str] = "/tmp/mngr_antigravity_workspaces"

# OS-specific subpath (under ``$HOME``) of agy's ms-playwright-go cache. agy
# downloads heavy playwright + browser binaries there on first real use; a fully
# isolated per-agent ``$HOME`` would make every agent re-download them, so each
# per-agent home's cache is symlinked to the user's real host cache to share the
# download. macOS uses ``Library/Caches``, Linux ``.cache``; the choice is made
# from the host's ``uname`` (resolved in ``provision``) so it is correct for
# remote hosts too.
_PLAYWRIGHT_CACHE_SUBPATH_MACOS: Final[tuple[str, ...]] = ("Library", "Caches", "ms-playwright-go")
_PLAYWRIGHT_CACHE_SUBPATH_LINUX: Final[tuple[str, ...]] = (".cache", "ms-playwright-go")
_DARWIN_UNAME: Final[str] = "Darwin"

# macOS keychain directory (under ``$HOME``). agy embeds Chromium, whose
# ``os_crypt`` keeps its "Antigravity Safe Storage" key -- the key that encrypts
# agy's persisted conversation store -- in the login keychain, which macOS
# resolves at ``$HOME/Library/Keychains``. Under a relocated per-agent ``$HOME``
# that directory is absent, so os_crypt finds no keychain and macOS raises a
# *modal* "A keychain cannot be found to store Antigravity Safe Storage" dialog.
# That dialog blocks agy's main thread until a human dismisses it, so an
# unattended run (e.g. the release test, or any non-interactive create) hangs
# and never persists a turn -- and even interactively it is the popup users hit
# on every fresh agent. Symlinking the per-agent home's ``Library/Keychains`` to
# the user's real one restores keychain discovery: agy finds the existing Safe
# Storage item (it is already in that item's ACL from the user's interactive
# logins, so no access prompt) and proceeds silently. macOS-only -- Linux has no
# such keychain (Chromium falls back to its file-based "basic" store with no
# prompt), exactly the claude-style "straightforward on Linux, keychain on
# macOS" split. Mirrors ``_provision_playwright_cache`` -- another HOME-relative,
# machine-shared resource symlinked into the per-agent home.
_MACOS_KEYCHAINS_SUBPATH: Final[tuple[str, ...]] = ("Library", "Keychains")


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
    # settings_overrides mirrors mngr_claude's field of the same name: a
    # free-form blob merged last into the per-agent settings.json. Avoids a
    # structured schema that could drift from agy's native format, and naturally
    # covers ``permissions`` ({allow, deny, ask}; precedence Deny > Ask > Allow),
    # ``toolPermission`` (e.g. "proceed-in-sandbox"), ``model`` (an ``agy
    # models`` display name, e.g. "Gemini 3.5 Flash (Medium)"), etc.
    #
    # File/url targets in ``permissions`` must be canonical (macOS /tmp ->
    # /private/tmp); a wrong target fails open to Ask rather than erroring.
    # Combined with ``auto_allow_permissions``, skip-permissions wins (it
    # auto-approves everything), so a ``permissions`` policy is then moot -- no
    # warning, matching mngr_claude.
    settings_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Key-value overrides merged last into the per-agent settings.json. "
        "Common keys: permissions ({allow, deny, ask}), toolPermission, model (a display "
        'name like "Gemini 3.5 Flash (Medium)"). Example: '
        '{"permissions": {"allow": ["command(git)"], "deny": ["command(rm -rf)"]}, "model": "..."}.',
    )
    # sync_home_settings mirrors mngr_claude's flag: a *data-source* choice
    # inside the one settings builder, never a second code path. When True
    # (default, claude-parity), the per-agent settings.json starts from a copy
    # of the user's real ~/.gemini/antigravity-cli/settings.json; settings_overrides
    # layer on top. When False, the base is an empty dict. This copies only the
    # *global* settings.json scope (in practice theme/telemetry/trust); the
    # user's model, permission grants, and behavioral policies live in other agy
    # scopes (config/config.json userSettings, per-project config/projects/<uuid>.json)
    # that are intentionally NOT read -- importing the user's grants would weaken
    # per-agent isolation. Per-agent model/permissions come from settings_overrides.
    sync_home_settings: bool = Field(
        default=True,
        description="Whether to base the per-agent settings.json on a copy of the user's real "
        "~/.gemini/antigravity-cli/settings.json (True, default) or start from an empty base (False).",
    )
    # symlink_oauth_token mirrors mngr_claude's symlink-vs-copy credential
    # choice. The per-agent home needs agy's file token to authenticate. With
    # the default (symlink), the per-agent token is a symlink to the shared
    # ~/.gemini/antigravity-cli/antigravity-oauth-token -- created even when that
    # shared token doesn't exist yet. Because agy writes the token in place,
    # the first agent's login writes *through* the symlink to the shared path,
    # auto-authenticating every other agent and propagating refreshes ("log in
    # once in any agent"). Copy mode (False) gives full isolation but no
    # sharing/propagation and only works if the shared token already exists.
    symlink_oauth_token: bool = Field(
        default=True,
        description="Symlink (True, default) each per-agent antigravity-oauth-token to the shared "
        "~/.gemini one, so one agent's login writes through to the shared token and authenticates "
        "all agents (and propagates refreshes). Copy (False) for full isolation (no sharing).",
    )
    # auto_allow_permissions adds agy's ``--dangerously-skip-permissions`` flag
    # (see ``assemble_command``). It is NOT a hook: agy's documented
    # ``PreToolUse`` ``{"decision": "allow"}`` output does not actually gate the
    # ``run_command`` confirmation dialog (verified live against agy 1.0.3), so
    # the flag is the only mechanism that reliably auto-approves. When combined
    # with a ``permissions`` policy in ``settings_overrides``, skip-permissions
    # wins (the policy is moot); no warning, matching mngr_claude.
    auto_allow_permissions: bool = Field(
        default=False,
        description="When True, auto-approve every tool call without prompting "
        "(adds --dangerously-skip-permissions, which overrides any settings_overrides permissions policy).",
    )
    # auto_dismiss_dialogs is the mngr_claude-style auto-trust knob. When
    # True (or when ``mngr_ctx.is_auto_approve`` is set, i.e. ``mngr create
    # --yes``), provisioning silently records the source repo in agy's global
    # ``trustedWorkspaces`` without prompting. When False (default), the
    # provisioner asks the user via ``click.confirm`` before mutating the
    # global config, mirroring ``mngr_claude``'s ``auto_dismiss_dialogs``.
    # Why default off: the global file is shared user state, and we should never
    # silently let an agent run on untrusted code -- trusting the repo is an
    # explicit choice. Why gate at all: the per-agent settings.json trusts the
    # agent's workspace so the running agy doesn't show its dialog, but granting
    # that trust must still be a deliberate acknowledgment.
    auto_dismiss_dialogs: bool = Field(
        default=False,
        description="When True, auto-trust the source repo without prompting. "
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
    preserve_on_destroy: bool = Field(
        default=True,
        description="When destroying this agent, first copy its transcripts and resumable session "
        "store to <local_host_dir>/preserved/ so they survive. Set to False to discard them.",
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
        # agy's ``statusLine`` command fires ``tmux wait-for -S`` on the
        # per-session channel whenever the agent enters a busy state -- i.e. once
        # it starts processing the just-submitted message (see statusline.sh).
        # Wait for that, exactly as Claude waits for its UserPromptSubmit hook.
        # agy waits on the statusLine busy-signal alone -- no acceptance marker is
        # supplied (agy records none), so the hook signal is the sole confirmation,
        # which covers the normal and queue-while-busy cases. (Known edge: a model
        # that *refuses* the prompt -- e.g. quota exhausted -- never enters a busy
        # state, so this times out even though the prompt was enqueued.)
        send_enter_via_tmux_wait_for_hook(
            self,
            tmux_target,
            wait_channel=f"mngr-submit-{self.session_name}",
            timeout_seconds=self.enter_submission_timeout_seconds,
            accept_marker_command=None,
        )

    @property
    def is_common_transcript_enabled(self) -> bool:
        return self.agent_config.emit_common_transcript

    def get_raw_transcript_scripts(self) -> Mapping[str, str]:
        """Return the antigravity raw-transcript streamer and its SQLite decoder.

        Always provisioned per :class:`HasTranscriptMixin`: the raw records are
        the source of truth that the common-transcript converter and any future
        tooling read from. ``stream_transcript.sh`` is the supervisor (python3
        guard + poll loop); ``decode_agy_transcript.py`` does the actual work of
        reading new steps from agy's per-conversation SQLite ``.db`` and emitting
        the JSON records (agy >= 1.0.4 replaced the JSONL transcript the streamer
        used to tail; see the module docstrings and ``regenerating_protobuf_schema.md``).
        """
        return {
            _RAW_TRANSCRIPT_SCRIPT_NAME: _load_antigravity_resource_script(_RAW_TRANSCRIPT_SCRIPT_NAME),
            _TRANSCRIPT_DECODER_SCRIPT_NAME: _load_antigravity_resource_script(_TRANSCRIPT_DECODER_SCRIPT_NAME),
        }

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        """Return the antigravity common-transcript converter shell script plus the
        python module it invokes."""
        return {
            name: _load_antigravity_resource_script(name)
            for name in (_COMMON_TRANSCRIPT_SCRIPT_NAME, _COMMON_TRANSCRIPT_CONVERT_SCRIPT_NAME)
        }

    def _get_agy_log_file_path(self) -> Path:
        """Path agy is told to write its --log-file to.

        Lives under the agent's state dir so it is per-agent and durable.
        The streamer reads this file to discover which conversation IDs
        belong to this agent.
        """
        return self._get_agent_dir() / _AGY_LOG_FILE_RELATIVE_PATH

    def _get_agy_home_dir(self) -> Path:
        """Per-agent ``$HOME`` for the agy process (under the agent state dir).

        agy resolves its whole config/permission/auth/session tree from
        ``<this>/.gemini``. Relocating ``$HOME`` here is what gives each agent
        its own permissions, model, and isolated transcripts. Mirrors
        ``mngr_claude``'s per-agent ``get_claude_config_dir``.
        """
        return self._get_agent_dir().joinpath(*_AGY_HOME_RELATIVE_PATH)

    def _resolve_host_home_and_os(self, host: OnlineHostInterface) -> tuple[Path, str]:
        """Resolve the host user's real ``$HOME`` and ``uname`` in one round-trip.

        Read from the host shell (not local ``Path.home()`` / ``platform.system()``)
        so the user's real ``~/.gemini`` (settings/token source) and the OS-specific
        playwright cache subpath are correct on remote hosts too.

        On the (essentially never) chance the query fails, exit cleanly via
        ``SystemExit`` rather than a plain ``Exception``: provision runs inside
        ``provision_agent``'s ``ConcurrencyExceptionGroup``, which wraps
        ``Exception`` subclasses into a noisy auto-diagnostics traceback but
        re-raises ``BaseException`` (``SystemExit``) unwrapped (same reason
        ``_ensure_source_repo_trusted`` uses ``SystemExit``).
        """
        result = host.execute_idempotent_command('printf \'%s\\n%s\' "$HOME" "$(uname -s)"', timeout_seconds=10.0)
        lines = result.stdout.splitlines()
        home = lines[0].strip() if lines else ""
        host_uname = lines[1].strip() if len(lines) > 1 else ""
        if not result.success or not home or not host_uname:
            logger.error(
                "Could not resolve the host's $HOME / uname for antigravity provisioning "
                "(exit_success={}, stdout={!r}). Cannot build the per-agent home tree.",
                result.success,
                result.stdout,
            )
            raise SystemExit(1)
        return Path(home), host_uname

    def _playwright_cache_subpath(self, host_uname: str) -> tuple[str, ...]:
        """OS-specific subpath of agy's ms-playwright-go cache, from the host's ``uname``."""
        return _PLAYWRIGHT_CACHE_SUBPATH_MACOS if host_uname == _DARWIN_UNAME else _PLAYWRIGHT_CACHE_SUBPATH_LINUX

    def modify_env_vars(self, host: OnlineHostInterface, env_vars: dict[str, str]) -> None:
        """Expose the per-agent app-data dir to the transcript streamer.

        ``ANTIGRAVITY_APP_DATA_DIR`` points stream_transcript.sh at the per-agent
        home's ``antigravity-cli`` dir, where the relocated agy writes
        ``brain/<conv_id>/.system_generated/logs/transcript.jsonl`` -- the
        streamer runs in the supervisor subshell on the real HOME, so it needs
        this to find the relocated transcripts. The var is a no-op for agy
        itself (absent from the binary); HOME relocation is what isolates agy.
        (Conversation-id discovery uses the capture-hook file, not a log grep;
        see ``_get_conversation_ids_file_path`` and ``CONVERSATION_IDS_FILENAME``.)
        """
        env_vars[_ANTIGRAVITY_APP_DATA_DIR_ENV_VAR] = str(get_antigravity_cli_dir(self._get_agy_home_dir()))

    def _get_conversation_ids_file_path(self) -> Path:
        """Per-agent file recording every agy conversation ID this agent worked on.

        Written by ``capture_conversation_id.sh`` (the ``PreInvocation`` capture
        hook); read by ``stream_transcript.sh`` (unique lines -> tail each
        conversation's transcript, including subagents'). This is the *set* of
        conversations for transcript scoping; the agent's main conversation for
        resume is tracked separately in ``root_conversation`` (see
        ``_get_root_conversation_file_path``). Lives directly under the agent
        state dir so the hook's ``$MNGR_AGENT_STATE_DIR/{CONVERSATION_IDS_FILENAME}``
        and this path resolve to the same file.
        """
        return self._get_agent_dir() / CONVERSATION_IDS_FILENAME

    def _get_root_conversation_file_path(self) -> Path:
        """Per-agent file recording the *main* (root) agy conversation ID.

        Written by ``statusline.sh`` (the lifecycle ``statusLine`` command) with
        the ``conversation_id`` from agy's payload, which always reports the root
        (never a subagent, even while one runs). Read on restart by
        ``assemble_command`` to resume the main conversation via
        ``agy --conversation``. This is the single source of truth for "the
        agent's current conversation", unaffected by the subagent ids that also
        land in ``CONVERSATION_IDS_FILENAME``. Lives directly under the agent
        state dir so the script's ``$MNGR_AGENT_STATE_DIR/{ROOT_CONVERSATION_FILENAME}``
        and this path resolve to the same file.
        """
        return self._get_agent_dir() / ROOT_CONVERSATION_FILENAME

    def on_destroy(self, host: OnlineHostInterface) -> None:
        """Preserve transcripts and conversation-id history before the state dir is deleted."""
        if self.agent_config.preserve_on_destroy:
            preserve_agent_state(_antigravity_preserved_items(), self, host)

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Build the per-agent ``$HOME`` tree and install the transcript/supervisor scripts.

        Steps:

        1. Resolve the host user's real ``$HOME`` (the copy/auth source).
        2. Ensure the agent's source repo is trusted (see
           ``_ensure_source_repo_trusted``): consent-gated write of the durable
           source-repo path into the user's *global* settings.json, or a clean
           ``SystemExit`` if consent is unavailable -- we never silently run an
           agent on untrusted code.
        3. Build the per-agent ``$HOME`` tree (``_provision_agy_home``):
           settings.json (copy of the user's settings + workspace trust +
           overrides + the mngr-owned lifecycle statusLine), the onboarding NUX
           seed, the conversation-id capture hook, the oauth token symlink/copy,
           the shared playwright-cache symlink, and -- on macOS -- the
           ``Library/Keychains`` symlink (restores keychain discovery under the
           relocated ``$HOME`` so agy's os_crypt never raises a blocking dialog).
        4. Install the transcript scripts and the background-tasks supervisor
           under ``$MNGR_AGENT_STATE_DIR/commands/``.
        """
        host_home, host_uname = self._resolve_host_home_and_os(host)
        self._ensure_source_repo_trusted(host, host_home, mngr_ctx)
        self._provision_agy_home(host, host_home, host_uname)
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
                    # Run by agy's statusLine command on every agent-state change:
                    # maintains the active marker (RUNNING/WAITING), records the
                    # root conversation, and fires the message-submission signal
                    # (see build_antigravity_statusline_settings).
                    STATUSLINE_SCRIPT_NAME: _load_antigravity_resource_script(STATUSLINE_SCRIPT_NAME),
                    # Run by the PreInvocation capture hook to record every
                    # conversation ID (incl. subagents') for transcript scoping
                    # (see build_antigravity_hooks_config).
                    CAPTURE_CONVERSATION_ID_SCRIPT_NAME: _load_antigravity_resource_script(
                        CAPTURE_CONVERSATION_ID_SCRIPT_NAME
                    ),
                },
                concurrency_group,
            )

    def _provision_agy_home(self, host: OnlineHostInterface, host_home: Path, host_uname: str) -> None:
        """Write the mngr-owned per-agent ``$HOME`` tree (idempotent each provision).

        Provisions the oauth token, settings.json (including the mngr-owned
        lifecycle ``statusLine``), the onboarding NUX seed, the conversation-id
        capture hook, the shared playwright-cache symlink, and -- on macOS -- the
        ``Library/Keychains`` symlink that restores keychain discovery under the
        relocated ``$HOME`` (see ``_provision_macos_keychain``).
        ``host.write_text_file`` creates intermediate directories. agy-owned
        session dirs (brain/, conversations/) are left intact across re-provision.
        """
        agy_home = self._get_agy_home_dir()
        self._provision_oauth_token(host, host_home, agy_home)
        self._provision_playwright_cache(host, host_home, host_uname, agy_home)
        self._provision_macos_keychain(host, host_home, host_uname, agy_home)
        base_settings: dict[str, Any] = {}
        if self.agent_config.sync_home_settings:
            user_settings_path = get_antigravity_settings_path(host_home)
            base_settings = read_antigravity_settings(host, user_settings_path)
            # Validate the copied base independently of _ensure_source_repo_trusted's
            # check (which reads the same file earlier) so the per-agent build never
            # silently coerces a corrupt user trustedWorkspaces regardless of call order.
            self._check_existing_trustedworkspaces_shape(user_settings_path, base_settings)
        per_agent_settings = build_isolated_settings(
            base_settings,
            self.agent_config.settings_overrides,
            [self._get_agy_workspace_symlink_path()],
        )
        # The agy statusLine must be mngr's: RUNNING/WAITING detection and
        # message-submission confirmation both depend on statusline.sh running, and
        # agy allows only one statusLine command. A user's own statusLine (in the
        # synced base settings or settings_overrides -- both already merged into
        # per_agent_settings here) is therefore not the agy statusLine, but it is
        # *composed* rather than discarded: record its command so statusline.sh runs
        # it (with the same payload) and emits only its output as the status row (mngr
        # itself renders nothing). A statusLine we can't run as a command (an unknown
        # shape) is dropped with a warning. Then inject mngr's statusLine LAST so it wins.
        self._provision_user_statusline_command(host, per_agent_settings.get("statusLine"))
        per_agent_settings.update(build_antigravity_statusline_settings())
        settings_path = get_antigravity_settings_path(agy_home)
        with log_span("Writing per-agent antigravity settings to {}", settings_path):
            host.write_text_file(settings_path, serialize_antigravity_settings(per_agent_settings))

        onboarding_path = get_antigravity_onboarding_cache_path(agy_home)
        host.write_text_file(onboarding_path, serialize_antigravity_settings(build_onboarding_seed()))

        hooks_path = get_antigravity_hooks_config_path(agy_home)
        with log_span("Installing antigravity hooks at {}", hooks_path):
            host.write_text_file(hooks_path, serialize_antigravity_hooks(build_antigravity_hooks_config()))

    def _provision_user_statusline_command(self, host: OnlineHostInterface, user_statusline: Any) -> None:
        """Record a user's own statusLine command for statusline.sh to compose, or clear a stale one.

        ``user_statusline`` is whatever ``statusLine`` the merged per-agent settings
        carry (from the synced base settings or ``settings_overrides``), or ``None``.
        When it is a runnable ``{"type": "command", "command": <str>}`` block, its
        command is written to the per-agent ``user_statusline_command`` file;
        ``statusline.sh`` runs it (with the same payload) and emits only its output
        as the status row, so the user's rendering survives verbatim (mngr's own use
        is lifecycle-only and renders nothing). A statusLine present but not runnable
        as a command is dropped with a warning (mngr's statusLine must be the agy one
        regardless).
        Any stale file from a prior provision is removed so a config that no longer
        has a user statusLine stops composing one.
        """
        command_file = self._get_user_statusline_command_file_path()
        composable_command = extract_statusline_command(user_statusline)
        if composable_command is not None:
            host.write_text_file(command_file, composable_command)
            return
        if user_statusline is not None:
            logger.warning(
                "Antigravity agent {} has a user-provided statusLine ({!r}) that mngr cannot compose "
                "with its lifecycle statusLine: only a {{'type': 'command', 'command': <str>}} block is "
                "runnable. Dropping it (mngr's statusLine drives RUNNING/WAITING and message-submission "
                "confirmation, so it must be the agy statusLine).",
                self.name,
                user_statusline,
            )
        host.execute_idempotent_command(f"rm -f {shlex.quote(str(command_file))}", timeout_seconds=5.0)

    def _get_user_statusline_command_file_path(self) -> Path:
        """Per-agent file holding the user's own statusLine command (for compose).

        Written by ``_provision_user_statusline_command``; read by ``statusline.sh``
        at ``$MNGR_AGENT_STATE_DIR/{USER_STATUSLINE_COMMAND_FILENAME}``. Lives
        directly under the agent state dir so the script's expansion and this path
        resolve to the same file.
        """
        return self._get_agent_dir() / USER_STATUSLINE_COMMAND_FILENAME

    def _provision_oauth_token(self, host: OnlineHostInterface, host_home: Path, agy_home: Path) -> None:
        """Point the per-agent oauth token at the shared host token (symlink), or copy it.

        agy is keyring-first, file-fallback (``ChainedAuth``) and writes the
        token file at login; on Linux (mngr's runtime) there is no OS keyring so
        the file is the native store. On macOS the keyring is the login keychain;
        ``_provision_macos_keychain`` symlinks it back into the relocated
        ``$HOME`` so it stays reachable (otherwise agy hits a blocking "keychain
        cannot be found" dialog), but this token file remains the portable seed
        that authenticates a fresh agent and shares logins/refreshes across
        agents regardless of platform.

        **Symlink mode (default).** Always create the per-agent
        ``antigravity-oauth-token`` as a symlink to the user's *shared*
        ``~/.gemini/antigravity-cli/antigravity-oauth-token`` -- even when that
        shared token does not exist yet (a dangling symlink). agy writes the
        token **in place** (verified empirically -- it does NOT use temp-file +
        atomic rename), so the first agent's login writes *through* the symlink
        to the shared path, which:

        * authenticates every agent whose token symlinks to that shared path
          (so you log in once in any agent and the rest are auto-authed), and
        * propagates token refreshes the same way (the symlink survives, refresh
          writes reach the shared file) -- resolving the spec's open
          "refresh clobbering" risk.

        The shared parent dir is created so the write-through target exists. This
        is the mechanism on both Linux and macOS (it does not depend on the
        keychain).

        **Copy mode** (``symlink_oauth_token=False``, full isolation, no
        propagation): copy the shared token in only if it exists; otherwise skip
        and let agy run its login flow on first launch (matching
        ``mngr_claude``'s ``_provision_local_credentials``, which skips seeding
        rather than blocking agent creation).
        """
        source = get_antigravity_oauth_token_path(host_home)
        dest = get_antigravity_oauth_token_path(agy_home)
        if self.agent_config.symlink_oauth_token:
            # Make the shared (source) parent so a write-through login resolves.
            symlink_on_host(host, source, dest, ensure_source_parent=True)
            return
        if not copy_on_host(host, source, dest):
            logger.info(
                "No shared Antigravity oauth token at {} to copy (symlink_oauth_token=False); the agent "
                "will run agy's login flow on first launch.",
                source,
            )

    def _provision_playwright_cache(
        self, host: OnlineHostInterface, host_home: Path, host_uname: str, agy_home: Path
    ) -> None:
        """Symlink the per-agent home's ms-playwright-go cache to the user's real host cache.

        agy downloads heavy playwright + browser binaries into
        ``$HOME/<os-cache>/ms-playwright-go`` on first real use; a fully isolated
        per-agent ``$HOME`` would make every agent re-download them. Symlinking the
        per-agent cache to the user's real host cache shares the download (agy
        creates/reads it through the symlink, like the oauth token). Done at
        provision time -- the per-agent ``$HOME`` is durable (under the agent state
        dir), so unlike the ``/tmp`` workspace symlink this needn't be recreated
        each launch. The OS-specific subpath comes from the host's ``uname``, so it
        is correct on remote hosts too.
        """
        subpath = self._playwright_cache_subpath(host_uname)
        symlink_on_host(
            host,
            host_home.joinpath(*subpath),
            agy_home.joinpath(*subpath),
            ensure_source_parent=True,
        )

    def _provision_macos_keychain(
        self, host: OnlineHostInterface, host_home: Path, host_uname: str, agy_home: Path
    ) -> None:
        """Symlink the per-agent home's ``Library/Keychains`` to the user's real one (macOS only).

        agy embeds Chromium, whose ``os_crypt`` stores the "Antigravity Safe
        Storage" key (which encrypts agy's persisted conversation store) in the
        login keychain that macOS resolves at ``$HOME/Library/Keychains``. The
        per-agent ``$HOME`` relocation that isolates agy's config also hides that
        directory, so os_crypt finds no keychain and macOS raises a *modal* "A
        keychain cannot be found to store Antigravity Safe Storage" dialog that
        blocks agy until dismissed -- hanging any unattended run, and popping on
        every fresh agent interactively. Symlinking the directory to the user's
        real one restores discovery; agy is already in the Safe Storage item's
        ACL (from interactive logins), so it reads the key with no access prompt.
        Per-item ACLs still gate every other secret, so this grants agy nothing
        it did not already have interactively.

        macOS-only (gated on the host's ``uname``, so it is correct for remote
        hosts too): on Linux there is no such keychain and Chromium falls back to
        its file-based "basic" store without prompting, so nothing is provisioned
        -- the claude-style "straightforward on Linux, keychain on macOS" split.
        Unlike the oauth-token and playwright-cache symlinks, the source
        (``~/Library/Keychains``) always exists on a real macOS user, so
        ``ensure_source_parent`` is left off -- we never fabricate an empty
        keychain dir in the user's real home.
        """
        if host_uname != _DARWIN_UNAME:
            return
        symlink_on_host(
            host,
            host_home.joinpath(*_MACOS_KEYCHAINS_SUBPATH),
            agy_home.joinpath(*_MACOS_KEYCHAINS_SUBPATH),
        )

    def _find_git_source_path(self, concurrency_group: ConcurrencyGroup) -> Path | None:
        """Find the source repo root for this agent's ``work_dir``, if it's inside a git repo.

        Returns the parent of the git common dir (the source repo root), or
        ``None`` if ``work_dir`` is not inside a git repo. Delegates to the
        shared core helper ``imbue.mngr.utils.git_utils.find_git_source_path``
        (also used by ``mngr_claude``) -- the source-path concept is what makes a
        single trust grant cover every worktree of the same repo: it is the
        durable thing we persist in the global settings. Kept as a method so
        tests can subclass and override it without monkeypatching.
        """
        return find_git_source_path(self.work_dir, concurrency_group)

    def _ensure_source_repo_trusted(self, host: OnlineHostInterface, host_home: Path, mngr_ctx: MngrContext) -> None:
        """Ensure the agent's source repo is trusted, persisting it to the global settings.

        agy does not distinguish a durable project from a transient git
        worktree -- mngr must. Trust splits by *what* is being persisted:

        * **Durable source-repo path -> global settings.json (here).** The git
          source-repo root (the parent repo for a worktree, or the work_dir for
          a standalone project) is the durable thing worth persisting: once
          trusted, later agents/worktrees of the same repo skip the consent
          prompt. This is what this method records.
        * **Transient per-agent workspace path -> per-agent settings.json
          (``_provision_agy_home``).** The agy-cwd ``/tmp`` symlink the running
          (isolated) agy exact-matches goes only into the per-agent file, which
          is deleted with the agent -- never into the global file, which would
          accumulate dead transient paths.

        Consent gating mirrors ``mngr_claude``: source already trusted -> no-op;
        ``auto_dismiss_dialogs`` or ``mngr_ctx.is_auto_approve`` -> silent;
        interactive -> ``click.confirm``; non-interactive without opt-in, or a
        declined prompt -> ``SystemExit(1)``. We never silently grant trust:
        even though the per-agent settings.json is what suppresses the running
        agy's dialog, granting that trust must be a deliberate acknowledgment so
        an agent never runs on untrusted code without the user's say-so.

        Why ``SystemExit`` and not ``UserInputError``: ``provision_agent`` wraps
        its body in a ``ConcurrencyExceptionGroup`` (see
        ``imbue.concurrency_group.concurrency_group.ConcurrencyGroup._exit``).
        Regular ``Exception`` raises get wrapped and surface as a noisy
        auto-diagnostics traceback; ``SystemExit`` is a ``BaseException`` which
        the same ``_exit`` re-raises unwrapped, producing a clean exit.
        """
        settings_path = get_antigravity_settings_path(host_home)
        existing_settings = read_antigravity_settings(host, settings_path)
        self._check_existing_trustedworkspaces_shape(settings_path, existing_settings)
        existing_trusted: list[str] = list(existing_settings.get(TRUSTED_WORKSPACES_KEY, []))

        source_path = self._find_git_source_path(mngr_ctx.concurrency_group) or self.work_dir
        source_path_str = str(source_path)
        if source_path_str in existing_trusted:
            logger.debug("Source {} already trusted in {}", source_path_str, settings_path)
            return

        if not (self.agent_config.auto_dismiss_dialogs or mngr_ctx.is_auto_approve):
            if not mngr_ctx.is_interactive:
                logger.error(
                    "Source directory {} is not trusted by the Antigravity CLI. mngr will not "
                    "silently run an agent on untrusted code. Re-run interactively to be prompted, "
                    "re-run with `--yes`, or set `auto_dismiss_dialogs = true` on the antigravity "
                    "agent type.",
                    source_path,
                )
                raise SystemExit(1)
            if not self._prompt_user_to_trust_workspace(source_path, settings_path):
                logger.error(
                    "User declined to trust {} in {}. Aborting agent creation.",
                    source_path,
                    settings_path,
                )
                raise SystemExit(1)

        self._write_workspace_trust(host, settings_path, existing_settings, [source_path_str])

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
            "so that agents for this repo are not run on untrusted code.\n",
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
        """Append each of ``paths_to_add`` to the global settings' trust list and write it back.

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
        with log_span("Persisting trusted source repo(s) {} in {}", actually_added, settings_path):
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
        2. ``mkdir -p <state>/logs <ws_symlink_parent>`` -- guarantees the agy
           ``--log-file`` directory and the workspace-symlink parent exist
           before launch.
        3. ``ln -sfn <work_dir> <ws_symlink>`` -- create / refresh the
           non-dotted ``/tmp`` workspace symlink (works around agy's rejection
           of dot-prefixed (hidden) paths as workspaces; see
           ``_AGY_WORKSPACE_SYMLINK_PARENT``).
        4. ``cd <ws_symlink>`` -- launches agy with cwd set to the workspace
           symlink, so agy's "project: using project ..." log line names the
           symlink path (not the resolved dotted target).
        5. ``{ <resume-prelude>; env HOME=<home> agy <user_args>
           --log-file <state>/logs/agy_cli.log [--dangerously-skip-permissions]
           "$@"; }`` -- foreground process under the per-agent ``$HOME``.
           ``HOME`` is injected only on the agy process (the unambiguous ``env``
           prefix), so the backgrounded supervisor subshell and tmux keep the
           real HOME. agy loads and executes the per-agent ``hooks.json`` (the
           conversation-ID capture hook; see ``build_antigravity_hooks_config``)
           directly from ``$HOME/.gemini/config/hooks.json`` under the relocated
           home -- no ``--add-dir`` needed, and the lifecycle ``statusLine`` runs
           from the per-agent ``settings.json``. The ``--dangerously-skip-permissions`` flag is
           appended only when ``auto_allow_permissions`` is set; the model and
           any permissions policy flow through the per-agent ``settings.json``,
           not the CLI.

        The resume-prelude resumes the agent's main (root) conversation via
        ``agy --conversation`` on restart, reading the id from
        ``root_conversation`` (see ``_get_root_conversation_file_path``); it is
        shell-evaluated at launch because the stored command is replayed on every
        ``mngr start`` (see the inline comment on its construction below).

        Bash precedence note: ``A & B && C && ...`` parses as ``A &`` followed
        by ``B && C && ...``. The supervisor's subshell is therefore scoped to
        ``&``, while ``mkdir`` / ``ln`` / ``cd`` / the agy group form a
        foreground sequential chain. ``ln -sfn`` is idempotent: re-running on
        every launch updates the symlink in place; ``/tmp`` wipes self-repair.
        (The per-agent ``$HOME`` tree -- settings, oauth-token symlink, and the
        playwright-cache symlink -- is durable, so it is built once at
        ``provision`` time, not here.)

        The ``--log-file`` arg writes agy's internal log to a per-agent path
        for debugging. (Resume reads ``root_conversation`` and transcript
        scoping reads ``CONVERSATION_IDS_FILENAME`` -- both hook-written files,
        not this log.)
        """
        log_file_path = self._get_agy_log_file_path()
        agy_home = self._get_agy_home_dir()
        extra_args: list[str] = [f"--log-file {shlex.quote(str(log_file_path))}"]
        # Auto-approval goes through the flag, not a hook (the hook allow-decision
        # does not gate run_command confirmations; see the config field comment).
        # A finer-grained policy instead lives in the per-agent settings.json
        # ``permissions`` block (settings_overrides).
        if self.agent_config.auto_allow_permissions:
            extra_args.append(_DANGEROUSLY_SKIP_PERMISSIONS_FLAG)
        base_command = super().assemble_command(host, agent_args, command_override, initial_message)
        background_cmd = self._build_background_tasks_command()

        symlink_path = self._get_agy_workspace_symlink_path()
        mkdir_cmd = f"mkdir -p {shlex.quote(str(log_file_path.parent))} {shlex.quote(_AGY_WORKSPACE_SYMLINK_PARENT)}"
        ln_cmd = f"ln -sfn {shlex.quote(str(self.work_dir))} {shlex.quote(symlink_path)}"
        cd_cmd = f"cd {shlex.quote(symlink_path)}"
        home_prefix = f"env HOME={shlex.quote(str(agy_home))}"

        # Resume the agent's main conversation via `agy --conversation`,
        # evaluated here in the shell because the stored command is replayed on
        # each restart. The id comes from `root_conversation` -- the conversation
        # that opened the most recent turn, i.e. the root agent's -- NOT the
        # conversation-ids file, whose last line can be a subagent (subagents
        # share the capture hook). agy resumes from its own incrementally-written
        # store (which survives the hard kill `mngr stop` performs) and, if the
        # conversation was pruned, warns and starts fresh on its own -- so we
        # pass the flag whenever an id is recorded and don't stat the store
        # ourselves (which would couple us to agy's on-disk layout). `set --` /
        # "$@" appends the flag without unquoted-substitution word splitting,
        # so it works under both bash and zsh.
        quoted_root_file = shlex.quote(str(self._get_root_conversation_file_path()))
        resume_prelude = (
            f"__mngr_cid=$(cat {quoted_root_file} 2>/dev/null || true); set --; "
            'if [ -n "$__mngr_cid" ]; then set -- --conversation "$__mngr_cid"; fi'
        )
        agy_invocation = f"{base_command} {' '.join(extra_args)}"

        return CommandString(
            f"{background_cmd} {mkdir_cmd} && {ln_cmd} && {cd_cmd} "
            f'&& {{ {resume_prelude}; {home_prefix} {agy_invocation} "$@" ; }}'
        )


def _antigravity_preserved_items() -> list[PreservedItem]:
    """Return the files to preserve from an antigravity agent's state directory.

    The raw and common transcripts plus the conversation-id history: the root
    conversation (for resume) and the full conversation-ids list (root plus
    subagents).

    Also agy's native resumable conversation store -- the per-conversation
    SQLite ``<conv_id>.db`` files that ``agy --conversation`` resumes from. We
    preserve the ``conversations/`` subdir specifically, which excludes the agy
    oauth token, ``settings.json``, and the macOS keychain symlink (all siblings
    elsewhere in the per-agent ``home`` tree, which is otherwise not preserved).

    Known limitation: on macOS the ``.db`` is encrypted by Chromium os_crypt
    with the "Antigravity Safe Storage" key in the login keychain, so a
    macOS-created store is not portable to a different machine/user (it is
    readable when preserved on the same machine, and Linux uses a portable
    file-based store).
    """
    conversations_relpath = (Path(*_AGY_HOME_RELATIVE_PATH) / CONVERSATIONS_DIR_RELATIVE_TO_HOME).as_posix()
    return [
        *build_transcript_preserved_items("antigravity"),
        PreservedItem(rel_path=ROOT_CONVERSATION_FILENAME, kind=FileType.FILE),
        PreservedItem(rel_path=CONVERSATION_IDS_FILENAME, kind=FileType.FILE),
        PreservedItem(rel_path=conversations_relpath, kind=FileType.DIRECTORY),
    ]


def _antigravity_items_to_preserve_for_discovered_agent(ref: DiscoveredAgent) -> Sequence[PreservedItem] | None:
    """Return the items to preserve for a discovered (offline) antigravity agent, or None to skip it."""
    return flag_gated_items(ref, "preserve_on_destroy", _antigravity_preserved_items())


@hookimpl
def on_before_host_destroy(host: HostInterface, mngr_ctx: MngrContext) -> None:
    """Preserve antigravity transcripts from the host's volume before it is destroyed.

    Mirrors ``AntigravityAgent.on_destroy`` for the offline path, where a host is
    destroyed without per-agent ``on_destroy`` calls but agent state still lives
    on the host's persisted volume.
    """
    preserve_host_agents_on_destroy(
        host, mngr_ctx, AgentTypeName("antigravity"), _antigravity_items_to_preserve_for_discovered_agent
    )


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the antigravity agent type."""
    return ("antigravity", AntigravityAgent, AntigravityAgentConfig)


@hookimpl
def register_agent_aliases() -> dict[str, str]:
    """Register ``agy`` as a short alias for the ``antigravity`` agent type."""
    return {"agy": "antigravity"}

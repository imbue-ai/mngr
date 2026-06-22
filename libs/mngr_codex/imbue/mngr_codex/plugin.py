"""``mngr_codex`` plugin -- registers the ``codex`` agent type for the OpenAI Codex CLI.

The Codex CLI (the Rust ``codex`` binary) has a hook system
(``UserPromptSubmit``/``Stop``/``SubagentStop``/...), a first-class config-dir
override env var, file-based auth, resume-by-id, and an append-as-you-go session
JSONL.

Per-agent ``CODEX_HOME`` (the isolation lever)
----------------------------------------------
Codex resolves its whole config/auth/session/hook tree from ``CODEX_HOME``
(default ``~/.codex``). Pointing each agent at its own ``CODEX_HOME`` under the
agent state dir -- injected only on the codex process via ``env CODEX_HOME=...``
-- isolates the agent's config/permissions/transcripts while leaving the user's
real ``$HOME`` untouched. codex accepts the dotted ``~/.mngr/...`` cwd, so there
is no workspace symlink either.

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
sentinel; readiness uses the ``InteractiveTuiAgent`` banner poll on a stable
header string (``TUI_READY_INDICATOR``).

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
import json
import os
import shlex
from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto
from pathlib import Path
from typing import Any
from typing import Callable
from typing import ClassVar
from typing import Final

import click
from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.logging import log_span
from imbue.mngr import hookimpl
from imbue.mngr.agents.common_transcript import maybe_provision_common_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_raw_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_scripts_to_commands_dir
from imbue.mngr.agents.installation import ensure_cli_installed
from imbue.mngr.agents.installation import verify_pinned_cli_version
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import send_enter_via_tmux_wait_for_hook
from imbue.mngr.api.preservation import PreservedItem
from imbue.mngr.api.preservation import adopt_sessions
from imbue.mngr.api.preservation import build_transcript_preserved_items
from imbue.mngr.api.preservation import dedupe_by_resolved_path
from imbue.mngr.api.preservation import flag_gated_items
from imbue.mngr.api.preservation import iter_agent_session_paths
from imbue.mngr.api.preservation import preserve_agent_state
from imbue.mngr.api.preservation import preserve_host_agents_on_destroy
from imbue.mngr.api.preservation import require_unique_match
from imbue.mngr.api.preservation import run_adopt_session_preflight
from imbue.mngr.api.preservation import transfer_cloned_agent_session_store
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import classify_waiting_reason
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.common import symlink_on_host
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import CliBackedAgentMixin
from imbue.mngr.interfaces.agent import HasAutoInstallMixin
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.agent import HasPermissionPolicyMixin
from imbue.mngr.interfaces.agent import HasSessionAdoptionMixin
from imbue.mngr.interfaces.agent import HasSessionPreservationMixin
from imbue.mngr.interfaces.agent import HasUnattendedModeMixin
from imbue.mngr.interfaces.agent import HasVersionManagementMixin
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.plugins.hookspecs import OnBeforeCreateArgs
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import WaitingReason
from imbue.mngr.utils.git_utils import find_git_source_path
from imbue.mngr_codex import resources as _codex_resources
from imbue.mngr_codex.codex_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_codex.codex_config import BACKGROUND_TASKS_SCRIPT_NAME
from imbue.mngr_codex.codex_config import CLEAR_ACTIVE_MARKER_SCRIPT_NAME
from imbue.mngr_codex.codex_config import COMMON_TRANSCRIPT_CONVERT_SCRIPT_NAME
from imbue.mngr_codex.codex_config import COMMON_TRANSCRIPT_SCRIPT_NAME
from imbue.mngr_codex.codex_config import MARKER_LOCK_DIRNAME
from imbue.mngr_codex.codex_config import MARKER_STATE_LIB_SCRIPT_NAME
from imbue.mngr_codex.codex_config import PERMISSIONS_WAITING_FILENAME
from imbue.mngr_codex.codex_config import RAW_TRANSCRIPT_SCRIPT_NAME
from imbue.mngr_codex.codex_config import ROOT_ACTIVE_FILENAME
from imbue.mngr_codex.codex_config import ROOT_SESSION_FILENAME
from imbue.mngr_codex.codex_config import SESSIONS_RELATIVE_PATH
from imbue.mngr_codex.codex_config import SET_ACTIVE_MARKER_SCRIPT_NAME
from imbue.mngr_codex.codex_config import SUBAGENTS_DIRNAME
from imbue.mngr_codex.codex_config import SUBAGENT_STARTED_SCRIPT_NAME
from imbue.mngr_codex.codex_config import SUBAGENT_STOPPED_SCRIPT_NAME
from imbue.mngr_codex.codex_config import SUBMIT_WAIT_CHANNEL_PREFIX
from imbue.mngr_codex.codex_config import build_codex_config
from imbue.mngr_codex.codex_config import build_codex_hooks_config
from imbue.mngr_codex.codex_config import extract_latest_codex_version
from imbue.mngr_codex.codex_config import get_codex_auth_path
from imbue.mngr_codex.codex_config import get_codex_config_path
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.mngr_codex.codex_config import get_codex_hooks_path
from imbue.mngr_codex.codex_config import get_codex_personality_migration_path
from imbue.mngr_codex.codex_config import get_codex_version_cache_path
from imbue.mngr_codex.codex_config import is_codex_update_available
from imbue.mngr_codex.codex_config import is_project_trusted
from imbue.mngr_codex.codex_config import merge_project_trust
from imbue.mngr_codex.codex_config import parse_codex_cli_version
from imbue.mngr_codex.codex_config import read_codex_config
from imbue.mngr_codex.codex_config import rewrite_rollout_record_cwd
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

# Sentinel that separates the two payloads of the single-round-trip version probe
# (``codex --version`` output, then codex's version.json). Chosen to never collide
# with a version string or JSON content.
_VERSION_SPLIT_SENTINEL: Final[str] = "__MNGR_CODEX_VERSION_SPLIT__"


class CodexUpdatePolicy(UpperCaseStrEnum):
    """How mngr acts on an outdated codex CLI at provision (see ``CodexAgentConfig.update_policy``).

    The network-free version check always runs regardless of policy; only the action
    taken when codex is outdated differs.
    """

    # Upgrade silently (run ``codex update``, no prompt).
    AUTO = auto()
    # Prompt to update on an attended local run, otherwise just notify.
    ASK = auto()
    # Never update; only log a non-blocking notice.
    NEVER = auto()


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
    # sandbox on. (codex's ``never`` is the "never *ask for* approval" value --
    # it auto-proceeds without prompting -- not "never allow".) codex honors
    # ``approval_policy`` directly, so no separate skip-all flag is needed. Sandbox
    # isolation is governed separately by ``sandbox_mode``.
    auto_allow_permissions: bool = Field(
        default=False,
        description="When True, set approval_policy='never' so codex never prompts for tool "
        "approval (the sandbox set by sandbox_mode still applies).",
    )
    check_installation: bool = Field(
        default=True,
        description="Check whether codex is installed and install it if missing "
        "(if False, assume it is already present).",
    )
    # config_overrides is a free-form blob merged last (shallow) into the
    # per-agent config.toml. Covers anything not surfaced as a typed knob (extra
    # [notice] keys, a [profiles.*] table, model_provider, etc.).
    config_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Key-value overrides merged last into the per-agent config.toml. "
        'Example: {"model_provider": "openai", "approval_policy": "on-request"}.',
    )
    # auto_dismiss_dialogs is the auto-consent knob. When True (or under
    # ``mngr create --yes``), provisioning silently records workspace trust + the
    # hook-bypass consent without prompting. When False (default), the user is
    # prompted via click.confirm before mngr mutates the global config or runs
    # codex with hook review bypassed.
    auto_dismiss_dialogs: bool = Field(
        default=False,
        description="When True, trust the source repo and allow the codex hook-review bypass "
        "without prompting. When False (default), the user is prompted interactively.",
    )
    # update_policy governs how mngr handles an outdated codex CLI at provision. mngr
    # always runs a network-free check (comparing ``codex --version`` to the latest
    # version codex itself recorded in ~/.codex/version.json) -- it is the well-behaved
    # replacement for codex's own ``check_for_update_on_startup``, which mngr disables
    # because its blocking "Update available!" prompt would intercept the first pasted
    # message. The check is best-effort: any probe/parse failure is swallowed and never
    # blocks provisioning. Only the *action* taken when codex is outdated is governed by
    # this policy: ``auto`` upgrades, ``ask`` prompts (attended) or notifies, ``never``
    # only notifies. ``codex update`` self-detects the install method (brew/npm/
    # standalone), so mngr needs no per-method logic.
    update_policy: CodexUpdatePolicy = Field(
        default=CodexUpdatePolicy.ASK,
        description="How mngr handles an outdated codex CLI at provision (it always runs a "
        "network-free version check, best-effort -- failures never block provisioning). "
        "`AUTO`: run `codex update` with no prompt. `ASK` (default): prompt to update on an "
        "attended local run (interactive tty + local host, not `--yes`), otherwise just log a "
        "non-blocking notice (unattended remote/deploy hosts, or any non-interactive run). "
        "`NEVER`: only log a non-blocking notice, never update. Updating mutates the user's "
        "*global* codex install. mngr always disables codex's own blocking startup update prompt.",
    )
    version: str | None = Field(
        default=None,
        description="Pin the codex CLI version to install (e.g., '0.139.0'). When set, installation runs "
        "`npm i -g @openai/codex@<version>` and provisioning verifies the installed codex matches, erroring "
        "on a mismatch. When None (the default), installs the latest version. A pin also suppresses the "
        "provision-time update check (`update_policy` is ignored), since updating would defeat the pin.",
    )
    # emit_common_transcript gates the rollout -> common-schema converter. The
    # raw transcript is always captured (HasTranscriptMixin); only the common
    # converter is gated.
    emit_common_transcript: bool = Field(
        default=True,
        description="When True, emit a common-schema transcript that `mngr transcript` reads.",
    )
    preserve_on_destroy: bool = Field(
        default=True,
        description="When destroying this agent, first copy its transcripts and resumable session "
        "store to <local_host_dir>/preserved/ so they survive. Set to False to discard them.",
    )


class CodexAgent(
    InteractiveTuiAgent[CodexAgentConfig],
    CliBackedAgentMixin,
    HasCommonTranscriptMixin,
    HasSessionPreservationMixin,
    HasSessionAdoptionMixin,
    HasUnattendedModeMixin,
    HasPermissionPolicyMixin,
    HasVersionManagementMixin,
    HasAutoInstallMixin,
):
    """Agent implementation for the OpenAI Codex CLI (``codex``).

    Future direction -- an app-server-backed variant:
    This agent drives the codex **TUI** via ``tmux send-keys`` (paste + Enter) with a
    banner-poll readiness check (see ``TUI_READY_INDICATOR``). That works but is fragile
    (screen-scraping) and codex's ``SessionStart`` fires lazily, so there is no clean
    pre-input readiness signal. codex's **app-server** is a much cleaner surface to drive
    programmatically -- a JSON-RPC protocol over a socket (``initialize`` -> ``thread/start``
    -> ``turn/start``), with the ``initialize`` response / ``thread.started`` event as an
    unambiguous readiness signal, and ``turn.*`` / ``item.*`` events to drive the
    RUNNING/WAITING marker and transcript directly. A TUI can still attach as a *viewer*
    via ``codex --remote unix://<sock>``. Hooks/subagents/sandbox/approval are all
    engine-level (``codex-core``), so they fire identically either way. Invocation (verified
    against codex 0.138.0): ``codex app-server --listen unix://<sock>`` works with the
    brew/npm install; avoid the ``codex remote-control start`` / ``app-server daemon``
    wrapper (needs codex's standalone installer at a fixed path).

    Why the TUI agent exists first, and is not merely a stopgap: on a ChatGPT-subscription
    login the backend gates some ``*-codex`` models on the **client identity** (the
    ``originator``, derived from the app-server's ``initialize`` ``clientInfo.name``). The
    first-party TUI presents as ``codex-tui`` and is entitled to them; a programmatic
    app-server client identifying honestly as mngr is not. An app-server variant must
    **identify honestly** -- do NOT set ``clientInfo.name = "codex-tui"`` to bypass the gate
    (codex treats these names as a trust boundary; the override env var is literally
    ``CODEX_INTERNAL_ORIGINATOR_OVERRIDE``, and spoofing falls under OpenAI's
    "circumvent restrictions" clause). For the gated ``*-codex`` models in app-server mode,
    authenticate with an **API key** (the documented path for programmatic workflows). So
    the TUI agent remains the legitimate way to use ``*-codex`` models on a ChatGPT login,
    and the app-server variant is a complement for cases where the identity gap is
    acceptable -- not a replacement.
    """

    # Stable substring of codex's header box, which renders together with the
    # input composer once the TUI is ready to receive keystrokes (verified live
    # against codex 0.138.0). codex has no pre-input readiness hook -- its
    # ``SessionStart`` fires lazily on the first prompt (openai/codex #15269) --
    # so this banner poll is the readiness signal. There is no OAuth splash delay
    # (auth is a file), so the header box is a safe indicator: it appears only
    # with the rendered, ready composer.
    TUI_READY_INDICATOR: ClassVar[str] = "/model to change"

    def get_expected_process_name(self) -> str:
        # The codex CLI is a single Rust binary; ps/tmux show the literal name.
        return "codex"

    def get_lifecycle_state(self) -> AgentLifecycleState:
        """Get lifecycle state, accounting for the codex ``permissions_waiting`` marker.

        The ``PermissionRequest`` hook touches a ``permissions_waiting`` file while
        codex is blocked on a tool-approval dialog (verified live against codex
        0.139.0: the marker is present for the whole time the dialog is open and is
        cleared on ``PostToolUse``). The base state reads only the ``active`` marker,
        which stays present during a dialog (the root turn has not stopped), so on
        its own it would report RUNNING. Promote RUNNING -> WAITING while the agent
        is blocked, since it cannot progress without user intervention. The promotion
        rule itself lives in ``_resolve_lifecycle_state_for_permission`` so it can be
        unit-tested without a live tmux pane.
        """
        base_state = super().get_lifecycle_state()
        is_blocked_on_permission = self._check_file_exists(self._get_agent_dir() / PERMISSIONS_WAITING_FILENAME)
        return _resolve_lifecycle_state_for_permission(base_state, is_blocked_on_permission)

    def _send_enter_and_validate(self, tmux_target: TmuxWindowTarget) -> None:
        # codex's UserPromptSubmit hook (set_active_marker.sh) fires
        # ``tmux wait-for -S mngr-submit-<session>`` *after* it sets the ``active``
        # marker, so waiting on that channel both confirms the message was
        # submitted and guarantees the agent reads as RUNNING by the time this
        # returns -- closing the race where a caller checks lifecycle state before
        # the turn registers. No queue-log fallback (claude's misfire workaround):
        # codex's raw transcript is the rollout JSONL, not the enqueue-event log
        # that fallback greps, and the foreground-registered waiter already avoids
        # the signal-vs-waiter race.
        send_enter_via_tmux_wait_for_hook(
            self,
            tmux_target,
            wait_channel=f"{SUBMIT_WAIT_CHANNEL_PREFIX}{self.session_name}",
            timeout_seconds=self.enter_submission_timeout_seconds,
        )

    @property
    def is_common_transcript_enabled(self) -> bool:
        return self.agent_config.emit_common_transcript

    def get_raw_transcript_scripts(self) -> Mapping[str, str]:
        """Return the codex raw-transcript streamer (always provisioned)."""
        return {RAW_TRANSCRIPT_SCRIPT_NAME: _load_codex_resource_script(RAW_TRANSCRIPT_SCRIPT_NAME)}

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        """Return the codex common-transcript converter shell script and its python module."""
        return {
            name: _load_codex_resource_script(name)
            for name in (COMMON_TRANSCRIPT_SCRIPT_NAME, COMMON_TRANSCRIPT_CONVERT_SCRIPT_NAME)
        }

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

    def preserve_session_state(self, host: OnlineHostInterface) -> None:
        preserve_agent_state(_codex_preserved_items(), self, host)

    def is_unattended_enabled(self) -> bool:
        return self.agent_config.auto_allow_permissions

    def get_permission_policy(self) -> Mapping[str, Any]:
        # codex's per-resource policy is its sandbox mode plus any approval_policy override.
        policy: dict[str, Any] = {"sandbox_mode": self.agent_config.sandbox_mode}
        if "approval_policy" in self.agent_config.config_overrides:
            policy["approval_policy"] = self.agent_config.config_overrides["approval_policy"]
        return policy

    def reconcile_installed_version(self, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
        # With a pinned version, verify the installed codex matches and error on a mismatch --
        # and skip the update check entirely, since prompting to update would defeat the pin.
        if self.agent_config.version is not None:
            self._verify_pinned_codex_version(host)
            return
        # Otherwise codex follows an update policy (ask / auto / never) rather than pinning a
        # version: a network-free check of the installed codex against its own recorded latest,
        # then the update_policy action. Best-effort and never fatal -- an outdated codex still runs.
        self._maybe_check_for_codex_update(host, self._resolve_user_codex_home(host), mngr_ctx)

    def _verify_pinned_codex_version(self, host: OnlineHostInterface) -> None:
        """Verify the installed codex matches ``config.version``, erroring on a mismatch.

        Called only when a version is pinned. A mismatch means the wrong codex is on
        PATH (e.g. a pre-existing global install that ``check_installation`` left in
        place), which the user must resolve -- re-install the pinned version or update
        the pin. Delegates to the shared verifier so codex matches the pin the same
        (scheme-agnostic) way as the other agents.
        """
        pinned_version = self.agent_config.version
        if pinned_version is None:
            return
        verify_pinned_cli_version(
            host,
            command=str(self.agent_config.command),
            binary_name=self.get_install_binary_name(),
            pinned_version=pinned_version,
        )

    def get_install_binary_name(self) -> str:
        return "codex"

    def get_install_command(self) -> str:
        version = self.agent_config.version
        package = f"@openai/codex@{version}" if version is not None else "@openai/codex"
        return f"npm i -g {shlex.quote(package)}"

    def on_destroy(self, host: OnlineHostInterface) -> None:
        """Preserve transcripts and session-id history before the state dir is deleted."""
        if self.agent_config.preserve_on_destroy:
            self.preserve_session_state(host)

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
            raise PluginMngrError(
                "Could not resolve the user's CODEX_HOME for codex provisioning "
                f"(exit_success={result.success}, stdout={result.stdout!r}); cannot locate the shared auth.json."
            )
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
        3. Surface (and, if opted in, apply) a codex CLI update -- best-effort and
           never fatal (an outdated codex still runs).
        4. Build the per-agent ``CODEX_HOME`` (config.toml, hooks.json, the
           auth.json symlink, the NUX-skip marker).
        5. Install the transcript scripts + background supervisor under
           ``$MNGR_AGENT_STATE_DIR/commands/``.
        """
        if self.agent_config.check_installation:
            ensure_cli_installed(host, mngr_ctx, self.get_install_binary_name(), self.get_install_command())
        user_codex_home = self._resolve_user_codex_home(host)
        canonical_work_dir = self._resolve_canonical_path(host, self.work_dir)
        self._ensure_source_repo_trusted(host, user_codex_home, mngr_ctx)
        self.reconcile_installed_version(host, mngr_ctx)
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

        approval_policy = _APPROVAL_POLICY_NEVER if self.is_unattended_enabled() else None
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

        Delegates to the shared core helper. The source-repo root is the durable
        thing trust is persisted against, so a single grant covers every worktree
        of the same repo. Kept as a method so tests can override without
        monkeypatching.
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

        Gating: source already trusted in the user's global ``config.toml`` ->
        no-op (consent previously given); ``auto_dismiss_dialogs`` or
        ``mngr_ctx.is_auto_approve`` -> silent; interactive -> ``click.confirm``
        (default False); non-interactive without opt-in, or declined ->
        ``SystemExit(1)``. We never run an agent on untrusted code, or bypass
        codex's hook review, without the user's say-so.

        ``SystemExit`` (not ``UserInputError``) because ``provision_agent`` wraps
        its body in a ``ConcurrencyExceptionGroup`` that re-raises
        ``BaseException`` unwrapped but turns ``Exception`` into a noisy
        auto-diagnostics traceback.
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

    def _maybe_check_for_codex_update(
        self, host: OnlineHostInterface, user_codex_home: Path, mngr_ctx: MngrContext
    ) -> None:
        """Surface (and optionally apply) a codex CLI update at provision.

        mngr disables codex's own ``check_for_update_on_startup`` (its blocking
        "Update available!" prompt would intercept the first pasted message), so this
        is the well-behaved replacement: a network-free check (codex's own
        ``version.json`` vs ``codex --version``) that always runs, plus -- when
        outdated -- the action chosen by ``update_policy``: an automatic ``codex
        update`` (``AUTO``), an interactive prompt on an attended local run else a
        notice (``ASK``), or just a non-blocking notice (``NEVER``). Updating is
        optional -- an outdated codex still runs -- so, unlike workspace trust, a
        declined, never, or non-interactive case never aborts provisioning, and any
        probe/parse failure is swallowed (debug-logged) so the check never blocks.
        """
        installed, latest = self._read_codex_versions(host, user_codex_home)
        if installed is None or latest is None:
            logger.debug(
                "Could not determine codex version (installed={!r}, latest={!r}); skipping update check.",
                installed,
                latest,
            )
            return
        if not is_codex_update_available(installed, latest):
            logger.debug("codex CLI is up to date (installed {}).", installed)
            return
        self._handle_codex_update_available(host, installed, latest, mngr_ctx)

    def _read_codex_versions(self, host: OnlineHostInterface, user_codex_home: Path) -> tuple[str | None, str | None]:
        """Resolve ``(installed, latest)`` codex versions over the host in one round-trip.

        ``installed`` comes from ``codex --version``; ``latest`` from the
        ``latest_version`` codex itself recorded in ``<user_codex_home>/version.json``
        (no network call). Either is None when it cannot be determined (codex not
        installed, no cache yet, an unparseable value), and the caller then skips the
        check. Exposed as a method so tests can inject versions without a real codex.
        """
        base = str(self.agent_config.command)
        quoted_cache = shlex.quote(str(get_codex_version_cache_path(user_codex_home)))
        # One command: the installed version, a sentinel, then the version cache
        # (empty if absent). ``2>/dev/null`` hides a missing-codex error and
        # ``cat ... || true`` keeps a missing cache non-fatal, so the probe still
        # exits 0 and we fall through to "could not determine" rather than failing.
        probe = (
            f"{base} --version 2>/dev/null; "
            f"printf '%s\\n' {shlex.quote(_VERSION_SPLIT_SENTINEL)}; "
            f"cat {quoted_cache} 2>/dev/null || true"
        )
        result = host.execute_idempotent_command(probe, timeout_seconds=30.0)
        if not result.success:
            logger.debug("codex version probe failed (stderr={!r}); skipping update check.", result.stderr)
            return None, None
        version_text, _, cache_text = result.stdout.partition(_VERSION_SPLIT_SENTINEL)
        return parse_codex_cli_version(version_text), self._parse_latest_codex_version(cache_text)

    def _parse_latest_codex_version(self, cache_text: str) -> str | None:
        """Parse the ``latest_version`` out of codex's ``version.json`` text, or None.

        A blank cache (file absent) yields None silently -- the normal fresh-install
        case. Malformed JSON is surfaced at warning level (it is codex-managed machine
        state, so corruption is abnormal) and then skipped.
        """
        stripped = cache_text.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.warning("codex version cache is not valid JSON ({}); skipping update check.", exc)
            return None
        if not isinstance(parsed, Mapping):
            return None
        return extract_latest_codex_version(parsed)

    def _handle_codex_update_available(
        self, host: OnlineHostInterface, installed: str, latest: str, mngr_ctx: MngrContext
    ) -> None:
        """Apply or surface an available codex update, per ``update_policy``.

        ``AUTO`` -> run ``codex update`` with no prompt. ``ASK`` -> prompt to update
        only on an *attended* run (a local host driven from an interactive terminal,
        and not ``--yes``); in every other case (``--yes``, non-interactive, or an
        *unattended* remote/deploy host) just log a non-blocking notice. ``NEVER`` ->
        only the notice. We never mutate the *global* codex install at provision
        without ``AUTO`` or an interactive yes.

        Unattended is keyed off ``not host.is_local`` -- mirroring the claude plugin's
        ``is_unattended`` -- so provisioning a *remote* codex agent from a local tty
        never prompts (and never silently upgrades the remote's global install), even
        though the local terminal is interactive. (``--yes`` clears blocking
        prerequisites like trust, but an optional global upgrade is heavier, so it is
        gated on ``AUTO`` alone, not on auto-approve.)
        """
        # Attended = a local host driven from an interactive terminal. A remote/deploy
        # host is unattended even when the local stdout is a tty, so it never prompts.
        is_attended = mngr_ctx.is_interactive and host.is_local
        policy = self.agent_config.update_policy
        should_update = policy is CodexUpdatePolicy.AUTO
        if policy is CodexUpdatePolicy.ASK and is_attended and not mngr_ctx.is_auto_approve:
            should_update = self._prompt_user_to_update_codex(installed, latest)
        if should_update:
            self._run_codex_update(host, installed, latest)
            return
        logger.warning(
            "A newer codex CLI is available ({} -> {}). Run `codex update` to upgrade, or set "
            '`update_policy = "AUTO"` on the codex agent type to have mngr do it. (mngr disables '
            "codex's own blocking startup update prompt, so it won't interrupt the agent.)",
            installed,
            latest,
        )

    def _prompt_user_to_update_codex(self, installed: str, latest: str) -> bool:
        """Prompt to run ``codex update`` now. Defaults to False (no stray upgrade).

        Exposed as a method so tests can override without driving click.confirm.
        """
        logger.info(
            "\nA newer codex CLI is available: you're on {}, latest is {}.\n"
            "`codex update` self-detects your install method (brew/npm/standalone) and upgrades.\n",
            installed,
            latest,
        )
        return click.confirm(f"Update codex now ({installed} -> {latest})?", default=False)

    def _run_codex_update(self, host: OnlineHostInterface, installed: str, latest: str) -> None:
        """Run ``codex update`` over the host (best-effort; never fatal).

        ``codex update`` self-detects the install method and shells out to the right
        updater (``brew upgrade --cask codex`` for brew, ``npm i -g`` for npm, the curl
        installer for standalone); for an install it cannot update it prints its own
        "update manually" guidance and exits non-zero, which we surface as a warning. A
        failed update must not abort agent creation -- the (outdated) codex still works.
        Exposed as a method so tests can override it without invoking codex.
        """
        update_command = f"{self.agent_config.command} update"
        with log_span("Updating codex CLI {} -> {} via `codex update`", installed, latest):
            result = host.execute_idempotent_command(update_command, timeout_seconds=600.0)
        if result.success:
            logger.info("codex update completed (was {}, latest {}).", installed, latest)
        else:
            logger.warning(
                "`codex update` did not complete (stderr={!r}); continuing with codex {}. "
                "You may need to update manually (e.g. `brew upgrade --cask codex`).",
                result.stderr.strip(),
                installed,
            )

    def _build_background_tasks_command(self) -> str:
        """Shell snippet that launches the backgrounded transcript supervisor.

        One backgrounded subshell owns the streamer + converter lifecycle
        (pidfile-deduped, restart-on-death), so replaying the command on restart
        is safe.
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
        4. ``{ <reset-marker-state>; <resume-prelude>; env CODEX_HOME=<home> codex
           --dangerously-bypass-hook-trust "$@" <cli/agent args>; }`` -- codex in
           the foreground under the per-agent ``CODEX_HOME`` (injected only on the
           codex process). The reset clears stale lifecycle-marker state left by a
           SIGKILL-mid-turn ``mngr stop`` (see the inline comment). The bypass flag
           goes before the subcommand so it applies whether the prelude selected
           ``resume <id>`` or a fresh start.

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

        # Reset the lifecycle-marker state on every launch. `mngr stop` SIGKILLs the
        # codex process, so if it was mid-turn (or had async subagents in flight)
        # the `active` marker, `codex_root_active` flag, per-subagent files, and a
        # held lock can persist. A resumed agent is idle (WAITING) until a new turn
        # begins -- and the killed subagents' SubagentStop hooks will never arrive --
        # so clear that stale state at start; the hooks rebuild it from the next turn.
        # `codex_root_session` / `codex_transcript_path` are intentionally kept (the
        # resume prelude reads the session id; both are re-recorded on the first
        # post-resume prompt). `|| true` so a stray failure can't block the launch.
        state = "$MNGR_AGENT_STATE_DIR"
        reset_marker_cmd = (
            f'rm -rf "{state}/{ACTIVE_MARKER_FILENAME}" "{state}/{ROOT_ACTIVE_FILENAME}" '
            f'"{state}/{SUBAGENTS_DIRNAME}" "{state}/{MARKER_LOCK_DIRNAME}" 2>/dev/null || true'
        )

        return CommandString(
            f"{background_cmd} {mkdir_cmd} && {cd_cmd} "
            f'&& {{ {reset_marker_cmd}; {resume_prelude}; {codex_invocation} "$@"{extra_str} ; }}'
        )

    def on_after_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Adopt an existing codex session so the new agent resumes its conversation."""
        self.adopt_session(host, options, mngr_ctx)

    def adopt_session(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Adopt existing codex conversation(s) into this newly provisioned agent.

        Two sources, combined via the shared ``adopt_sessions`` orchestrator:

        - ``--adopt`` (``options.adopt_session``, the tuple of values passed to the
          command-global ``multiple=True`` flag): each value (a codex session id or an
          absolute rollout ``.jsonl`` path) is resolved to a
          ``(session_id, source_sessions_dir)`` (see ``_resolve_adopt_session``) and its
          source ``sessions/`` tree is copied into this agent's ``CODEX_HOME/sessions``.
          Rollouts are date-nested, so multiple values coexist; each rollout's cwd is
          rebound to this agent's work dir.

        - ``--from <agent>`` (``options.source_agent_state_location``): a generic clone that
          copies the source workspace but *not* the source agent's state dir. The source's
          native session store is transferred in, and its most-recent rollout rebound.

        The session actually resumed -- via the resume pointer (``codex_root_session``) that
        ``assemble_command``'s prelude reads -- is the ``--from`` clone's when given, else the
        last ``--adopt`` value; any others are left available for codex's own session
        switcher. With neither option set, nothing is adopted (fresh start). Rebinding a
        rollout rewrites its recorded cwd to this agent's work dir so ``codex resume`` does
        not pop the working-directory modal.
        """
        adopt_sessions(
            options.adopt_session,
            options.source_agent_state_location,
            copy_explicit=lambda arg: self._copy_explicit_codex_session(host, arg, mngr_ctx),
            copy_clone=lambda location: self._copy_cloned_codex_session(host, location),
            resume=lambda session_id: self._write_codex_resume_pointer(host, session_id),
        )

    def _copy_explicit_codex_session(self, host: OnlineHostInterface, adopt_arg: str, mngr_ctx: MngrContext) -> str:
        """Resolve an explicit ``--adopt`` value, copy its ``sessions/`` tree in, rebind its cwd.

        Additive: each call copies one resolved source ``sessions/`` tree into this agent's
        ``CODEX_HOME/sessions``. Rollouts are date-nested, so multiple ``--adopt`` values
        coexist. Returns the resolved session id; the orchestrator decides which is resumed.
        """
        user_codex_home = self._resolve_user_codex_home(host)
        session_id, source_sessions_dir = _resolve_adopt_session(adopt_arg, mngr_ctx, user_codex_home)
        dest_sessions_dir = self._get_codex_home() / "sessions"
        with log_span("Adopting codex session {}", session_id):
            host.copy_directory(host, source_sessions_dir, dest_sessions_dir)
            self._rebind_adopted_rollout_cwd(host, dest_sessions_dir, session_id)
        logger.info("Adopted codex session {} into agent {}", session_id, self.id)
        return session_id

    def _copy_cloned_codex_session(self, host: OnlineHostInterface, source_location: HostLocation) -> str | None:
        """Transfer the cloned source agent's native session store in, rebind its latest rollout.

        Transfers the source's native session store (the same relpath the agent preserves
        and scans) into this agent's state dir, and rebinds the source's most-recent rollout
        cwd. Returns the discovered session id, or ``None`` when there is nothing to resume.

        The clone's session id is read from the *source* store (which holds only the source
        agent's own sessions), not the merged destination store: any ``--adopt`` sessions the
        orchestrator copied in first were rewritten by their cwd-rebind (bumping their mtime to
        "now"), so an ``ls -t`` over the destination could rank one of them ahead of the clone's
        older transferred rollout and resume the wrong session.

        Warns and returns ``None`` when the source has no session store, or the store holds no
        rollout: a ``--from`` clone is fundamentally a workspace clone, so carrying the session
        forward is a bonus -- an empty source falls back to a fresh start (or the last
        ``--adopt``), not a hard failure.
        """
        source_sessions_dir = source_location.path / _AGENT_SESSIONS_RELPATH
        if not source_location.host.path_exists(source_sessions_dir):
            logger.warning("clone source agent {} has no codex session store to resume", source_location.path)
            return None
        session_id = self._find_latest_session_id(source_location.host, source_sessions_dir)
        if session_id is None:
            logger.warning("no rollout found in codex session store at {}", source_sessions_dir)
            return None
        transfer_cloned_agent_session_store(host, self._get_agent_dir(), source_location, _AGENT_SESSIONS_RELPATH)
        dest_sessions_dir = self._get_codex_home() / "sessions"
        with log_span("Adopting cloned codex session {}", session_id):
            self._rebind_adopted_rollout_cwd(host, dest_sessions_dir, session_id)
        logger.info("Adopted cloned codex session {} into agent {}", session_id, self.id)
        return session_id

    def _write_codex_resume_pointer(self, host: OnlineHostInterface, session_id: str) -> None:
        """Write ``session_id`` to ``codex_root_session`` so the launch prelude resumes it.

        The resume pointer ``assemble_command``'s prelude reads. Called once by the
        ``adopt_sessions`` orchestrator on the single session it selects to resume.
        """
        host.write_text_file(self._get_root_session_file_path(), session_id)

    def _find_latest_session_id(self, host: OnlineHostInterface, sessions_dir: Path) -> str | None:
        """Return the session id of the most-recent rollout under ``sessions_dir``, or None.

        Codex files rollouts under ``sessions/YYYY/MM/DD/`` as
        ``rollout-<timestamp>-<id>.jsonl``; ``find ... | xargs -r ls -t`` walks the date
        nesting and picks the newest by mtime, and its trailing UUID is the id codex
        resumes by. Resolved over the host shell (a recursive ``find``, not a globstar
        glob) so it works remotely and under any shell. ``|| true`` keeps an empty store
        non-fatal; ``xargs -r`` (``--no-run-if-empty``) is required because GNU xargs would
        otherwise run ``ls -t`` with no args (listing the cwd) when ``find`` matches nothing.
        """
        quoted_dir = shlex.quote(str(sessions_dir))
        result = host.execute_idempotent_command(
            f"find {quoted_dir} -type f -name 'rollout-*.jsonl' -print0 2>/dev/null "
            "| xargs -0 -r ls -t 2>/dev/null | head -n1 || true",
            timeout_seconds=10.0,
        )
        latest = result.stdout.strip()
        if not latest:
            return None
        return _session_id_from_rollout_path(Path(latest))

    def _rebind_adopted_rollout_cwd(self, host: OnlineHostInterface, sessions_dir: Path, session_id: str) -> None:
        """Rewrite the recorded cwd in the adopted rollout to this agent's work dir.

        Codex resumes by id and compares the rollout's recorded cwd against the actual
        cwd; a mismatch (always, when adopting into a fresh worktree) pops the "Choose
        working directory to resume this session" modal. Rewriting every ``payload.cwd``
        in the adopted rollout removes the mismatch. The work dir is resolved through
        symlinks on the host so it matches the path codex canonicalizes its cwd to.

        Codex writes exactly one rollout file per session id, so a single read/rewrite/
        write suffices (no per-file upload loop).
        """
        new_cwd = self._resolve_canonical_path(host, self.work_dir)
        rollout_path = self._find_adopted_rollout_path(host, sessions_dir, session_id)
        if rollout_path is None:
            logger.warning(
                "Adopted codex session {} has no rollout file under {}; the resume modal may appear.",
                session_id,
                sessions_dir,
            )
            return
        original = host.read_text_file(rollout_path)
        host.write_text_file(rollout_path, self._rewrite_rollout_text_cwd(original, new_cwd, rollout_path))

    def _rewrite_rollout_text_cwd(self, rollout_text: str, new_cwd: str, rollout_path: Path) -> str:
        """Rewrite every recorded ``payload.cwd`` in a rollout JSONL to ``new_cwd``.

        Parses each JSONL line, applies the pure per-record rewrite, and rejoins
        (preserving a trailing newline). A malformed line is passed through unchanged
        but logged at warning level: the rollout is codex-owned state, so we never drop
        content we cannot parse, but surface the corruption rather than swallow it.
        """
        has_trailing_newline = rollout_text.endswith("\n")
        rewritten_lines: list[str] = []
        for line in rollout_text.splitlines():
            if not line.strip():
                rewritten_lines.append(line)
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("Skipping unparseable line in adopted rollout {}: {}", rollout_path, exc)
                rewritten_lines.append(line)
                continue
            if isinstance(record, Mapping):
                rewritten_lines.append(json.dumps(rewrite_rollout_record_cwd(record, new_cwd)))
            else:
                rewritten_lines.append(line)
        result = "\n".join(rewritten_lines)
        if has_trailing_newline and result:
            result += "\n"
        return result

    def _find_adopted_rollout_path(
        self, host: OnlineHostInterface, sessions_dir: Path, session_id: str
    ) -> Path | None:
        """Return the copied rollout JSONL path for ``session_id``, or None if absent.

        Codex files rollouts under ``sessions/YYYY/MM/DD/`` and embeds the id in the
        filename (``rollout-<timestamp>-<id>.jsonl``), so a recursive name glob finds it
        regardless of date nesting. Resolved over the host shell so it works remotely. A
        session id maps to a single rollout file; the first match is returned.
        """
        quoted_dir = shlex.quote(str(sessions_dir))
        pattern = shlex.quote(f"rollout-*-{session_id}.jsonl")
        result = host.execute_idempotent_command(
            f"find {quoted_dir} -type f -name {pattern} 2>/dev/null || true", timeout_seconds=10.0
        )
        for line in result.stdout.splitlines():
            if line.strip():
                return Path(line.strip())
        return None


# Per-agent codex sessions store, as a rel-path under the agent state dir. Both live
# local mngr agents (``agents/<id>/...``) and preserved agents
# (``preserved/<name>--<id>/...``) mirror this layout, so an adopt argument can be
# resolved against either (matching ``SESSIONS_RELATIVE_PATH``).
_AGENT_SESSIONS_RELPATH: Final[Path] = Path(SESSIONS_RELATIVE_PATH)


def _mngr_session_dirs(mngr_ctx: MngrContext) -> list[Path]:
    """Return the per-agent codex ``sessions`` directories on the local host.

    Scans both live local mngr agents (``<host_dir>/agents/<id>/...``) and preserved
    agents (``<host_dir>/preserved/<name>--<id>/...``; see ``preserve_session_state``),
    each of which stores its rollout JSONLs under
    ``plugin/codex/home/sessions/``.

    Only the local host dir is scanned: an adopted session's files are copied onto the
    destination host from a path that must already be reachable as a local source, so
    remote agents' session dirs are not searched here (mirrors the claude plugin).
    """
    local_host_dir = Path(mngr_ctx.config.default_host_dir).expanduser()
    return iter_agent_session_paths(local_host_dir, _AGENT_SESSIONS_RELPATH)


def _resolve_adopt_session(adopt_session_arg: str, mngr_ctx: MngrContext, user_codex_home: Path) -> tuple[str, Path]:
    """Resolve a codex adopt argument to a ``(session_id, source_sessions_dir)`` pair.

    Accepts either:

    - An absolute path to a rollout ``.jsonl`` file (its ``<uuid>`` is the session id;
      the returned source dir is the ``sessions/`` root so the whole date-nested tree
      copies, matching how codex files rollouts).
    - A bare session id, searched (across *all of*) the user-native store
      (``<user_codex_home>/sessions``), every live local mngr agent's per-agent
      ``sessions/`` dir, and every preserved agent's ``sessions/`` dir. A rollout
      filename embeds the id as ``rollout-<timestamp>-<id>.jsonl``, so the id is
      matched by globbing ``**/rollout-*-<id>.jsonl``. An id matching in more than one
      store is rejected as ambiguous (the user must pass the full ``.jsonl`` path).

    Returns ``(session_id, source_sessions_dir)`` where ``source_sessions_dir`` is the
    ``sessions/`` root to copy into the new agent's ``CODEX_HOME/sessions``.
    """
    if adopt_session_arg.endswith(".jsonl"):
        rollout_file = Path(adopt_session_arg).resolve()
        if not rollout_file.exists():
            raise UserInputError(f"Session file not found: {rollout_file}")
        return _session_id_from_rollout_path(rollout_file), _sessions_root_for_rollout(rollout_file)

    # Search the user-native store plus every live and preserved local mngr agent (all
    # of them -- an id matching in multiple stores is treated as ambiguous below, not
    # resolved by search order). Dedupe by resolved path (the user store can coincide
    # with a scanned agent dir) while preserving candidate ordering.
    candidate_dirs: list[Path] = [user_codex_home / "sessions", *_mngr_session_dirs(mngr_ctx)]
    deduped_dirs = dedupe_by_resolved_path(candidate_dirs)

    matched_dirs: list[Path] = []
    for sessions_dir in deduped_dirs:
        if sessions_dir.is_dir() and any(sessions_dir.glob(f"**/rollout-*-{adopt_session_arg}.jsonl")):
            matched_dirs.append(sessions_dir)

    matched_dir = require_unique_match(
        matched_dirs,
        not_found_message=(
            f"Codex session {adopt_session_arg} not found. Check that the session id is correct, "
            "or pass an absolute path to the rollout .jsonl file. (Searched the user's "
            "~/.codex/sessions, every live mngr codex agent, and every preserved one.)"
        ),
        ambiguous_message=(
            f"Codex session {adopt_session_arg} found in multiple session stores; "
            "pass the absolute path to the rollout .jsonl file to specify which one:"
        ),
    )
    return adopt_session_arg, matched_dir


def _session_id_from_rollout_path(rollout_file: Path) -> str:
    """Extract the codex session id (the trailing UUID) from a rollout filename.

    Codex names rollouts ``rollout-<ISO-timestamp>-<uuid>.jsonl``; the id codex
    resumes by is that ``<uuid>``. A UUID has four ``-`` separators, so the id is the
    last five ``-``-joined fields of the stem.
    """
    parts = rollout_file.stem.split("-")
    if len(parts) < 5:
        raise UserInputError(
            f"Rollout filename {rollout_file.name!r} does not embed a session id "
            "(expected rollout-<timestamp>-<uuid>.jsonl)."
        )
    return "-".join(parts[-5:])


def _sessions_root_for_rollout(rollout_file: Path) -> Path:
    """Return the ``sessions/`` root above a rollout file (its ``YYYY/MM/DD`` ancestors).

    Codex files rollouts under ``sessions/YYYY/MM/DD/``; the whole ``sessions/`` tree
    is the unit copied into the new agent so codex's date-nested scan finds the
    adopted rollout. Falls back to the rollout's own parent if a ``sessions`` ancestor
    is not present (e.g. a flat layout).
    """
    for ancestor in rollout_file.parents:
        if ancestor.name == "sessions":
            return ancestor
    return rollout_file.parent


def _codex_preserved_items() -> list[PreservedItem]:
    """Return the files to preserve from a codex agent's state directory.

    The raw and common transcripts, the root session-id history (used to resume
    the conversation), and codex's native resumable rollout store. The native
    JSONLs are preserved by targeting ``CODEX_HOME/sessions`` specifically, which
    excludes the auth-token symlink and config that sit as siblings in CODEX_HOME.
    """
    return [
        *build_transcript_preserved_items("codex"),
        PreservedItem(rel_path=ROOT_SESSION_FILENAME, kind=FileType.FILE),
        PreservedItem(rel_path=SESSIONS_RELATIVE_PATH, kind=FileType.DIRECTORY),
    ]


def _codex_items_to_preserve_for_discovered_agent(ref: DiscoveredAgent) -> Sequence[PreservedItem] | None:
    """Return the items to preserve for a discovered (offline) codex agent, or None to skip it."""
    return flag_gated_items(ref, "preserve_on_destroy", _codex_preserved_items())


@hookimpl
def on_before_host_destroy(host: HostInterface, mngr_ctx: MngrContext) -> None:
    """Preserve codex transcripts from the host's volume before it is destroyed.

    Mirrors ``CodexAgent.on_destroy`` for the offline path, where a host is
    destroyed without per-agent ``on_destroy`` calls but agent state still lives
    on the host's persisted volume.
    """
    preserve_host_agents_on_destroy(
        host, mngr_ctx, AgentTypeName("codex"), _codex_items_to_preserve_for_discovered_agent
    )


def _user_native_codex_home() -> Path:
    """Resolve the user's real ``CODEX_HOME`` on the local machine.

    Honors a ``CODEX_HOME`` override, else ``$HOME/.codex`` -- the same precedence the
    plugin uses over the host shell and the release test seeds. Used by
    ``on_before_create``, which runs before any host exists and whose source is always
    local, so a local-process resolution matches the later host-shell resolution.
    """
    override = os.environ.get("CODEX_HOME")
    return Path(override) if override else Path.home() / ".codex"


@hookimpl
def on_before_create(args: OnBeforeCreateArgs, mngr_ctx: MngrContext) -> OnBeforeCreateArgs | None:
    """Codex-specific fail-fast pre-resolution of ``--adopt`` session ids (see ``run_adopt_session_preflight``)."""
    user_codex_home = _user_native_codex_home()
    run_adopt_session_preflight(
        args.agent_options.agent_type,
        args.agent_options.adopt_session,
        mngr_ctx,
        CodexAgent,
        resolve_one=lambda session_arg: _resolve_adopt_session(session_arg, mngr_ctx, user_codex_home),
    )
    return None


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the codex agent type."""
    return ("codex", CodexAgent, CodexAgentConfig)


def _resolve_lifecycle_state_for_permission(
    base_state: AgentLifecycleState, is_blocked_on_permission: bool
) -> AgentLifecycleState:
    """Layer the ``permissions_waiting`` signal onto the base lifecycle state.

    Promotes RUNNING -> WAITING while codex is blocked on a tool-approval dialog
    (the base state, which reads only the ``active`` marker, would otherwise report
    RUNNING since the root turn has not stopped). Every non-RUNNING base state
    passes through unchanged. Kept pure (no agent/host) so ``get_lifecycle_state``'s
    promotion rule is unit-testable without standing up a tmux pane.

    Defers the gating decision to the shared ``classify_waiting_reason``: a RUNNING
    base state means the ``active`` marker is present and the process is alive, so
    the classifier's ``is_active`` gate is satisfied and a PERMISSIONS verdict is
    what promotes RUNNING to WAITING. Sharing that one function keeps this promotion
    and the ``waiting_reason`` field generator from drifting apart.
    """
    if base_state != AgentLifecycleState.RUNNING:
        return base_state
    reason = classify_waiting_reason(is_active=True, is_blocked_on_permission=is_blocked_on_permission)
    return AgentLifecycleState.WAITING if reason is WaitingReason.PERMISSIONS else base_state


def _waiting_reason(agent: AgentInterface, host: OnlineHostInterface) -> WaitingReason | None:
    """Return why the agent is waiting based on marker files, or None.

    Reads the agent state directory's marker files directly rather than calling
    get_lifecycle_state() (which runs tmux/ps SSH commands), then delegates the
    decision to the shared ``classify_waiting_reason`` so this and the lifecycle
    promotion stay in lockstep. The markers are maintained by the codex lifecycle
    hooks (see build_codex_hooks_config). ``permissions_waiting`` is only read when
    ``active`` is present, both to short-circuit the idle case and because the
    classifier ignores the permission signal when the agent is not in a turn.

    Known limitation: when a dialog is cancelled (Esc / "No"), codex 0.139.0 fires
    no terminal hook for the turn (verified live), so both the ``active`` and
    ``permissions_waiting`` markers persist until the next turn's Stop. During that
    window this returns PERMISSIONS even though the dialog is closed; the lifecycle
    state stays WAITING (correct), only this reason sub-field is briefly off, and it
    self-heals at the next Stop. See the README "Known limitation" note.
    """
    agent_dir = get_agent_state_dir_path(host.host_dir, agent.id)
    is_active = host.path_exists(agent_dir / ACTIVE_MARKER_FILENAME)
    is_blocked_on_permission = is_active and host.path_exists(agent_dir / PERMISSIONS_WAITING_FILENAME)
    return classify_waiting_reason(is_active, is_blocked_on_permission)


@hookimpl
def agent_field_generators() -> tuple[str, dict[str, Callable[[AgentInterface, OnlineHostInterface], Any]]] | None:
    """Expose codex-specific agent fields for listing."""
    return ("codex", {"waiting_reason": _waiting_reason})

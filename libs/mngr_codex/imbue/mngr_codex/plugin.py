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
    hooks.json               # the session-pointer recorder hook (guards live in the repo's .codex/hooks.json)
    auth.json -> ~/.codex/auth.json   # symlink: shared login, write-through refresh
    .personality_migration   # empty NUX-skip marker
    sessions/.../rollout-*.jsonl      # codex-owned transcripts

Auth: codex writes ``auth.json`` in place (verified against source: ``O_TRUNC``,
no atomic rename) and its refresh path reloads-before-refreshing, so a per-agent
``auth.json`` *symlink* to the shared ``~/.codex/auth.json`` lets one login
authenticate every agent and propagates refreshes. ``cli_auth_credentials_store
= "file"`` is pinned in config.toml so codex never falls back to a keyring store
keyed by the (per-agent) ``CODEX_HOME`` path, which would defeat sharing.

Lifecycle: ``CodexAgent.get_lifecycle_state`` reads the daemon's live
``thread/status`` as the SOLE RUNNING-vs-WAITING source (a turn in flight ->
RUNNING; parked on an approval/input it cannot self-clear -> WAITING); process
presence (tmux/ps) answers only alive/dead (STOPPED/DONE/REPLACED). There is no
lifecycle marker and no marker fallback -- the daemon is authoritative, and its
death takes the ``--remote`` window down so a dead daemon reads STOPPED. The one
mngr hook, ``UserPromptSubmit`` -> ``record_session_pointers.sh``, records only
the rollout session id + transcript path (non-lifecycle pointers the
adopt/preserve and raw-transcript machinery need); see
:func:`codex_config.build_codex_hooks_config`.

Readiness: a successful ``initialize`` handshake with the ``codex app-server``
daemon over its unix socket -- an unambiguous programmatic signal, unlike the
lazy ``SessionStart`` hook (openai/codex issue #15269) the old glyph poll had to
work around. See ``wait_for_ready_signal`` / ``_probe_app_server_ready``.

Hook trust: codex requires command hooks to be trusted before they run. mngr
passes ``--dangerously-bypass-hook-trust`` so its own hooks run
without a per-hash trust dance. Because trusting the workspace also lets codex
load any repo-local ``.codex/hooks.json``, that bypass is consent-gated together
with workspace trust (see ``_ensure_source_repo_trusted``) -- mngr never runs an
agent on untrusted code, or bypasses codex's hook review, without the user's
say-so.

Resume: ``mngr stop``/``start`` resumes the prior conversation over the daemon
(the client's ``thread/resume`` on the persisted app-server thread id; codex's
rollout JSONL survives the hard kill ``mngr stop`` performs). The
``UserPromptSubmit`` hook (``record_session_pointers.sh``) records the root
rollout ``session_id`` for the adopt/preserve machinery and the rollout
``transcript_path`` for transcript scoping.
"""

from __future__ import annotations

import importlib.metadata
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
from typing import Final
from uuid import uuid4

import click
from loguru import logger
from pydantic import Field
from websockets.exceptions import WebSocketException

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mngr import hookimpl
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.common_transcript import maybe_provision_common_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_raw_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_scripts_to_commands_dir
from imbue.mngr.agents.installation import ensure_cli_installed
from imbue.mngr.agents.installation import verify_pinned_cli_version
from imbue.mngr.agents.output_styles import read_output_style_files
from imbue.mngr.agents.output_styles import resolve_output_style
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
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import MessageDeliveredButBlockedError
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import classify_waiting_reason
from imbue.mngr.hosts.common import symlink_on_host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import CliBackedAgentMixin
from imbue.mngr.interfaces.agent import HasAutoInstallMixin
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.agent import HasPermissionPolicyMixin
from imbue.mngr.interfaces.agent import HasSessionAdoptionMixin
from imbue.mngr.interfaces.agent import HasSessionPreservationMixin
from imbue.mngr.interfaces.agent import HasUnattendedModeMixin
from imbue.mngr.interfaces.agent import HasVersionManagementMixin
from imbue.mngr.interfaces.agent import InteractiveAgentMixin
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
from imbue.mngr.primitives import LifecycleProbeResult
from imbue.mngr.primitives import OutputStyleName
from imbue.mngr.primitives import SystemPromptText
from imbue.mngr.primitives import WaitingReason
from imbue.mngr.utils.git_utils import find_git_source_path
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_codex import resources as _codex_resources
from imbue.mngr_codex.app_server_client import AppServerTransport
from imbue.mngr_codex.app_server_client import CodexAppServerClient
from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.app_server_client import Disposition
from imbue.mngr_codex.app_server_client import DispositionKind
from imbue.mngr_codex.app_server_client import ThreadStatusSnapshot
from imbue.mngr_codex.app_server_client import connect_app_server_transport
from imbue.mngr_codex.codex_config import APP_SERVER_THREAD_FILENAME
from imbue.mngr_codex.codex_config import BACKGROUND_TASKS_SCRIPT_NAME
from imbue.mngr_codex.codex_config import COMMON_TRANSCRIPT_CONVERT_SCRIPT_NAME
from imbue.mngr_codex.codex_config import COMMON_TRANSCRIPT_SCRIPT_NAME
from imbue.mngr_codex.codex_config import DEVELOPER_INSTRUCTIONS_SEPARATOR
from imbue.mngr_codex.codex_config import PROCESS_STARTED_MARKER_FILENAME
from imbue.mngr_codex.codex_config import RAW_TRANSCRIPT_SCRIPT_NAME
from imbue.mngr_codex.codex_config import RECORD_SESSION_POINTERS_SCRIPT_NAME
from imbue.mngr_codex.codex_config import ROOT_SESSION_FILENAME
from imbue.mngr_codex.codex_config import RUST_LOG_VALUE
from imbue.mngr_codex.codex_config import SESSIONS_RELATIVE_PATH
from imbue.mngr_codex.codex_config import TRANSCRIPT_PATH_FILENAME
from imbue.mngr_codex.codex_config import build_codex_config
from imbue.mngr_codex.codex_config import build_codex_hooks_config
from imbue.mngr_codex.codex_config import extract_latest_codex_version
from imbue.mngr_codex.codex_config import get_codex_app_server_socket_path
from imbue.mngr_codex.codex_config import get_codex_auth_path
from imbue.mngr_codex.codex_config import get_codex_config_path
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.mngr_codex.codex_config import get_codex_hooks_path
from imbue.mngr_codex.codex_config import get_codex_personality_migration_path
from imbue.mngr_codex.codex_config import get_codex_tui_log_dir
from imbue.mngr_codex.codex_config import get_codex_version_cache_path
from imbue.mngr_codex.codex_config import get_shared_output_styles_dir
from imbue.mngr_codex.codex_config import is_codex_update_available
from imbue.mngr_codex.codex_config import is_project_trusted
from imbue.mngr_codex.codex_config import merge_project_trust
from imbue.mngr_codex.codex_config import parse_codex_cli_version
from imbue.mngr_codex.codex_config import read_codex_config
from imbue.mngr_codex.codex_config import rewrite_rollout_record_cwd
from imbue.mngr_codex.codex_config import serialize_codex_config
from imbue.mngr_codex.codex_config import serialize_codex_hooks

# codex approval policy that suppresses every interactive approval dialog while
# keeping the sandbox on (the right unattended default). Applied only when
# ``auto_allow_permissions`` is set; otherwise codex's trust-derived default
# (``on-request`` for a trusted project) stands.
_APPROVAL_POLICY_NEVER: Final[str] = "never"

# Sentinel that separates the two payloads of the single-round-trip version probe
# (``codex --version`` output, then codex's version.json). Chosen to never collide
# with a version string or JSON content.
_VERSION_SPLIT_SENTINEL: Final[str] = "__MNGR_CODEX_VERSION_SPLIT__"

# The hidden sidecar tmux window (in the agent's own session) that runs the
# ``codex app-server`` daemon as its FOREGROUND process. Because it is a window
# process in the agent's session, ``tmux kill-session`` (mngr stop/destroy) reaps
# the daemon with everything else -- no orphaned daemons, no leaked sockets.
_APP_SERVER_WINDOW_NAME: Final[str] = "codex-app-server"

# Top-level flags the DAEMON needs so its hooks actually run. codex gates hooks behind a
# feature flag that is OFF by default (``--enable hooks`` == ``-c features.hooks=true``), and
# even enabled, a command hook stays "in review" (never fires) until trusted --
# ``--dangerously-bypass-hook-trust`` runs enabled hooks without that persisted trust. Turns run
# in the daemon, so this is where the hooks fire; without both flags the transcript marker and the
# PreToolUse safety guards silently never run (verified live: a daemon launched with both fires
# SessionStart / UserPromptSubmit / PreToolUse; a bare ``codex app-server`` fires nothing). Both
# are top-level flags, placed before the ``app-server`` subcommand.
_DAEMON_HOOK_FLAGS: Final[str] = "--dangerously-bypass-hook-trust --enable hooks"

# The primary window waits (bounded) for the daemon's unix socket to appear before
# launching ``codex --remote`` against it, so the two windows' start order does not
# matter. 100 polls * 0.1s = a 10s ceiling (the daemon is up in ~1-2s).
_SOCKET_WAIT_POLLS: Final[int] = 100
_SOCKET_WAIT_INTERVAL_SECONDS: Final[str] = "0.1"

# ``clientInfo`` sent on ``initialize`` (echoed back in the daemon's ``userAgent``).
# mngr identifies honestly as itself (never as ``codex-tui``).
_APP_SERVER_CLIENT_NAME: Final[str] = "mngr"

# Prefix for the ``clientUserMessageId`` mngr mints on every send. The daemon echoes
# it back on the committed ``userMessage`` item, so the system_interface ledger can
# reconcile delivery per id; the CLI just needs a unique, mngr-attributable id.
_CLIENT_ID_PREFIX: Final[str] = "mngr-cid-"


def _mint_client_id() -> str:
    """Mint a unique ``clientUserMessageId`` for one send (contract A4 join key)."""
    return f"{_CLIENT_ID_PREFIX}{uuid4().hex}"


# Items injected to MATERIALIZE the root thread's rollout at create WITHOUT a model turn
# (``thread/inject_items``). ``thread/start`` alone leaves the thread unmaterialized (no rollout
# on disk, so ``codex resume <id>`` cannot cold-load it), and the daemon rejects an empty items
# list; a single empty ``environmentContext`` item writes the rollout the way codex seeds a
# session itself, adding no visible user-message bubble so the conversation stays blank (verified
# live against codex 0.147).
_ROOT_MATERIALIZE_ITEMS: Final[tuple[Mapping[str, Any], ...]] = ({"type": "environmentContext", "text": ""},)

# How long to wait for the rollout to appear on disk after ``inject_items`` materializes it, before
# recording its path in ``codex_transcript_path``. The daemon writes it within a few ms; this is only
# a guard against the find racing the write.
_ROLLOUT_MATERIALIZE_TIMEOUT_SECONDS: Final[float] = 5.0

# The ``ThreadStatus`` tag that means a turn is in flight (the thread is doing work or is
# parked awaiting the user mid-turn). Its complement, ``idle``, means the turn is done and
# the agent is waiting for the next message. This is the event-sourced replacement for the
# ``active`` marker's RUNNING-vs-WAITING split.
_THREAD_STATUS_ACTIVE: Final[str] = "active"

# Lifecycle states in which the agent's process is present (its primary window is alive).
# Only for these does the daemon's live status re-derive the RUNNING/WAITING split; the
# other states (STOPPED/DONE/REPLACED) are pure process-presence facts from tmux/ps and
# are kept verbatim so ``mngr ls`` never misreports a dead or replaced agent as running.
_PROCESS_PRESENT_STATES: Final[frozenset[AgentLifecycleState]] = frozenset(
    {AgentLifecycleState.RUNNING, AgentLifecycleState.WAITING}
)


class _LiveActivity(FrozenModel):
    """The two lifecycle signals the daemon's live thread status yields.

    ``is_active`` -- a turn is in flight (RUNNING); its absence is WAITING (END_OF_TURN).
    ``is_blocked`` -- the turn is parked awaiting an approval/input the agent cannot
    self-clear (promotes RUNNING -> WAITING with reason PERMISSIONS). These are the
    event-sourced replacements for the ``active`` / ``permissions_waiting`` markers.
    """

    is_active: bool
    is_blocked: bool


def _app_server_client_version() -> str:
    """Return the plugin version for the ``initialize`` ``clientInfo`` (cosmetic)."""
    try:
        return importlib.metadata.version("imbue-mngr-codex")
    except importlib.metadata.PackageNotFoundError:
        return "0"


# How long the readiness poll waits between connect+initialize attempts.
_READY_POLL_INTERVAL_SECONDS: Final[float] = 0.25

# The stable header of codex's "Hooks need review" screen. The ``codex resume <id> --remote`` TUI
# stops on it and ignores ``--dangerously-bypass-hook-trust`` (a codex limitation of the resume
# path), so until it is cleared the daemon's hooks stay untrusted and NEVER fire -- on typed OR
# programmatic turns (verified live). ``_clear_hook_trust_prompt`` selects "Trust all and continue".
_HOOK_TRUST_PROMPT_MARKER: Final[str] = "Hooks need review"

# How long to wait for the ``--remote`` TUI to render the hook-trust prompt after it launches
# (the primary window resumes only after mngr has established the root, then codex renders in ~1-2s).
# A no-show within this window means there is nothing to clear (already trusted, or not that screen).
_HOOK_TRUST_PROMPT_TIMEOUT_SECONDS: Final[float] = 15.0


def _probe_app_server_ready(socket_path: Path) -> bool:
    """Return whether the daemon accepts a WebSocket connect + ``initialize`` handshake.

    A not-yet-listening socket or a failed handshake returns ``False`` (the caller
    retries until the readiness deadline) rather than raising.
    """
    try:
        transport = connect_app_server_transport(socket_path)
    except (OSError, WebSocketException) as exc:
        logger.debug("codex app-server not yet reachable at {}: {}", socket_path, exc)
        return False
    try:
        client = CodexAppServerClient(transport=transport)
        client.initialize(_APP_SERVER_CLIENT_NAME, _app_server_client_version())
        return True
    except (OSError, WebSocketException, CodexAppServerError) as exc:
        logger.debug("codex app-server handshake not ready at {}: {}", socket_path, exc)
        return False
    finally:
        transport.close()


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

    # --- role behaviour, set by a create template and applied by this harness ---
    #
    # Both are harness-neutral *intent*: a role states them once and each harness applies
    # them its own way. They live on the harness subclasses rather than AgentTypeConfig so
    # a harness that cannot honour them has no field to route to -- the create then fails
    # naming the template, instead of launching an agent that quietly ignores its role.
    output_style: OutputStyleName | None = Field(
        default=None,
        description="Name of an output style to launch with, matched against the `name:` "
        "frontmatter of a file in the work dir's output-style directory. Scalar: the last "
        "template in the stack to set it wins.",
    )
    append_system_prompt: tuple[SystemPromptText, ...] = Field(
        default=(),
        description="Blocks to append to the agent's system prompt, in stack order. Aggregate: "
        "write `append_system_prompt__extend = [...]` in a template so stacked roles each "
        "contribute a block instead of the last one replacing the rest.",
    )

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
    # auto_dismiss_dialogs_at_startup is the auto-consent knob. When True (or under
    # ``mngr create --yes``), provisioning silently records workspace trust + the
    # hook-bypass consent without prompting. When False (default), the user is
    # prompted via click.confirm before mngr mutates the global config or runs
    # codex with hook review bypassed.
    auto_dismiss_dialogs_at_startup: bool = Field(
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
    BaseAgent[CodexAgentConfig],
    InteractiveAgentMixin,
    CliBackedAgentMixin,
    HasCommonTranscriptMixin,
    HasSessionPreservationMixin,
    HasSessionAdoptionMixin,
    HasUnattendedModeMixin,
    HasPermissionPolicyMixin,
    HasVersionManagementMixin,
    HasAutoInstallMixin,
):
    """Agent implementation for the OpenAI Codex CLI (``codex``), driven over the app-server.

    Instead of screen-scraping the TUI with ``tmux send-keys``, this agent drives the
    stock ``codex app-server`` daemon over its WebSocket JSON-RPC surface (see
    :class:`~imbue.mngr_codex.app_server_client.CodexAppServerClient`). ``assemble_command``
    launches the daemon as a hidden sidecar window and a visible ``codex --remote`` TUI in
    the primary window; readiness (``wait_for_ready_signal``) is a successful ``initialize``
    handshake; and ``send_message`` delivers a message as a ``turn/start`` (idle) or parks
    it as a ``turn/steer`` (busy) over a short-lived per-call connection. Hooks / subagents
    / sandbox / approval are engine-level (``codex-core``), so they fire identically whether
    a human types in the TUI or mngr drives the daemon.

    ``clientInfo.name`` is ``"mngr"`` -- mngr identifies honestly and never spoofs
    ``codex-tui`` (codex treats these names as a trust boundary; the override env var is
    literally ``CODEX_INTERNAL_ORIGINATOR_OVERRIDE``). For the ``*-codex`` models gated to
    the first-party client on a ChatGPT login, authenticate with an API key (the documented
    path for programmatic workflows).

    Lifecycle/activity are event-sourced: ``get_lifecycle_state`` / ``probe_lifecycle`` /
    ``is_running`` / ``waiting_reason`` read the daemon's live ``thread/status`` (a turn is in
    flight -> RUNNING; parked on an approval/input -> WAITING; idle -> WAITING/END_OF_TURN) as
    the SOLE RUNNING-vs-WAITING source -- there is no ``active`` / ``permissions_waiting`` marker
    and no marker fallback. Process presence (STOPPED / DONE / REPLACED) comes from tmux/ps, so a
    dead or replaced window is reported correctly; the ``--remote`` window's life is tied to the
    daemon, so a dead daemon reads STOPPED. When the daemon cannot be reached (a remote host, a
    not-yet-ready or already-stopped daemon, a transient failure), the split degrades to WAITING
    rather than consulting any marker.

    This class subclasses ``BaseAgent`` directly (not ``SendKeysAgent`` / ``InteractiveTuiAgent``)
    because codex is driven over the app-server and sends nothing through the send-keys /
    evidence-probe / ready-glyph pipeline: ``send_message`` and ``wait_for_ready_signal`` are
    overridden below to talk JSON-RPC to the daemon, matching how ``pi`` / ``opencode`` subclass
    ``BaseAgent`` and override their own transport.
    """

    def get_expected_process_name(self) -> str:
        # The codex CLI is a single Rust binary; ps/tmux show the literal name.
        return "codex"

    def get_lifecycle_state(self) -> AgentLifecycleState:
        """Get lifecycle state, sourcing RUNNING/WAITING from the daemon's live thread status.

        Process presence comes from the base tmux/ps probe: STOPPED / DONE / REPLACED are
        pure "is the window's process there?" facts and pass through unchanged (so a dead or
        replaced agent is never misreported as running). When the process IS present, the
        RUNNING-vs-WAITING split is derived SOLELY from the daemon:

        * a turn in flight -> RUNNING; promoted to WAITING when the turn is parked awaiting an
          approval/input the agent cannot self-clear (``thread/status`` ``active`` with
          ``waitingOnApproval`` / ``waitingOnUserInput``);
        * an idle thread -> WAITING (END_OF_TURN).

        There is no marker fallback: when the daemon cannot be read (a remote host, a
        not-yet-ready or already-stopped daemon, a transient failure) the split degrades to
        WAITING -- the conservative "not actively running" default, honest since a live turn
        cannot be confirmed. The permission promotion stays in
        ``_resolve_lifecycle_state_for_permission`` so it is unit-testable without a live pane.
        """
        base_probe = self._base_probe_lifecycle()
        if base_probe.state not in _PROCESS_PRESENT_STATES:
            return base_probe.state
        activity = self._resolve_live_activity()
        if activity is None:
            return AgentLifecycleState.WAITING
        live_state = AgentLifecycleState.RUNNING if activity.is_active else AgentLifecycleState.WAITING
        return _resolve_lifecycle_state_for_permission(live_state, activity.is_blocked)

    def _base_probe_lifecycle(self) -> LifecycleProbeResult:
        """The tmux/ps process-presence probe (STOPPED/DONE/REPLACED + pid), before live re-sourcing.

        A thin wrapper over ``BaseAgent.probe_lifecycle`` so the process-presence half can be
        driven in unit tests without a live tmux pane, independently of the daemon status seam.
        """
        return super().probe_lifecycle()

    def probe_lifecycle(self) -> LifecycleProbeResult:
        """Probe lifecycle state + pid, sourcing the RUNNING/WAITING split from live status.

        The base probe supplies process presence and the pid; when the process is present its
        RUNNING/WAITING split is replaced with the daemon's live activity (a turn in flight ->
        RUNNING, idle -> WAITING), keeping this consistent with ``get_lifecycle_state``. The
        pid is preserved from the base probe. There is no marker fallback: when the daemon
        cannot be read the split degrades to WAITING (pid preserved). The permission promotion
        is applied only by ``get_lifecycle_state`` (matching the base contract, where
        ``probe_lifecycle`` reports the raw split and the promotion layers on top).
        """
        base_probe = self._base_probe_lifecycle()
        if base_probe.state not in _PROCESS_PRESENT_STATES:
            return base_probe
        activity = self._resolve_live_activity()
        if activity is None:
            return LifecycleProbeResult(state=AgentLifecycleState.WAITING, pid=base_probe.pid)
        live_state = AgentLifecycleState.RUNNING if activity.is_active else AgentLifecycleState.WAITING
        return LifecycleProbeResult(state=live_state, pid=base_probe.pid)

    def is_blocked_on_dialog(self) -> bool:
        """Whether codex is parked on an approval/input it cannot self-clear.

        Sourced from the daemon's live ``thread/status`` (``waitingOnApproval`` /
        ``waitingOnUserInput``), not from a ``permissions_waiting`` marker file: this
        harness has no lifecycle markers, the daemon is authoritative. Best-effort per
        the interface contract -- an unreadable daemon yields False (nothing detected),
        matching the WAITING degrade the lifecycle path already takes.
        """
        activity = self._resolve_live_activity()
        return activity is not None and activity.is_blocked

    def compute_waiting_reason(self) -> WaitingReason | None:
        """Return why the agent is waiting (or None if actively running), from live status.

        Sources ``is_active`` / ``is_blocked`` the same way as ``get_lifecycle_state`` -- the
        daemon's live thread status, the SOLE source -- then delegates the verdict to the shared
        ``classify_waiting_reason`` so this and the lifecycle promotion cannot drift. When the
        daemon cannot be read there is no marker fallback: the reason degrades to END_OF_TURN,
        matching ``get_lifecycle_state``'s WAITING degrade (an unconfirmable turn is treated as
        not running).
        """
        activity = self._resolve_live_activity()
        if activity is None:
            return classify_waiting_reason(is_active=False, is_blocked_on_permission=False)
        return classify_waiting_reason(activity.is_active, activity.is_blocked)

    def _resolve_live_activity(self) -> _LiveActivity | None:
        """Read the daemon's live thread status into ``(is_active, is_blocked)``, or None.

        Returns None when the daemon cannot be queried (a remote host whose unix socket the
        mngr process cannot reach, a not-yet-ready or already-stopped daemon, or any transient
        transport failure); callers then degrade to WAITING (there is no marker fallback). A
        live read maps ``thread/status`` ``active`` -> ``is_active`` and its
        ``waitingOnApproval`` / ``waitingOnUserInput`` flags -> ``is_blocked``.
        """
        snapshot = self._read_live_thread_status()
        if snapshot is None:
            return None
        return _LiveActivity(
            is_active=snapshot.status_type == _THREAD_STATUS_ACTIVE,
            is_blocked=snapshot.is_blocked_on_input,
        )

    def _read_live_thread_status(self) -> ThreadStatusSnapshot | None:
        """Query the daemon for this agent's root-thread status, or None if it cannot be read.

        Uses a short-lived connection through the same ``_connect_app_server_transport`` seam as
        ``send_message`` (so tests inject a scripted transport). Binds the agent's ROOT thread
        without ever starting a fresh one -- a status probe must not create a conversation -- and
        returns None when no root thread is bound/loadable. Every failure path (remote socket
        unreachable, handshake error, transport hiccup, no thread) yields None so a lifecycle
        probe never raises; the caller then degrades to WAITING (no marker fallback).
        """
        if not self.host.is_local:
            return None
        try:
            transport = self._connect_app_server_transport()
        except SendMessageError as exc:
            logger.debug("codex app-server not reachable for a status probe of {}: {}", self.name, exc)
            return None
        client = CodexAppServerClient(transport=transport)
        try:
            client.initialize(_APP_SERVER_CLIENT_NAME, _app_server_client_version())
            if not self._bind_existing_thread(client):
                return None
            return client.read_thread_status()
        except (CodexAppServerError, OSError, WebSocketException) as exc:
            logger.debug("codex app-server status probe of {} failed: {}", self.name, exc)
            return None
        finally:
            client.close()

    def _bind_existing_thread(self, client: CodexAppServerClient) -> bool:
        """Bind the agent's root thread WITHOUT starting one; return whether a thread was bound.

        Mirrors ``_bind_thread_for_send`` but omits the fresh-``thread/start`` branch: a status
        probe must be side-effect-light and must never create a conversation. Prefers the persisted
        root id (already loaded, else reloaded from its rollout via ``thread/resume``); with no
        persisted id, adopts a single loaded thread (e.g. the visible ``--remote`` TUI's). Returns
        False when there is no unambiguous root to read (the caller then degrades to WAITING).
        """
        persisted_thread_id = self._read_persisted_thread_id()
        loaded_thread_ids = client.thread_loaded_list()
        if persisted_thread_id is not None and persisted_thread_id in loaded_thread_ids:
            client.bind_thread(persisted_thread_id)
            return True
        if persisted_thread_id is not None:
            return self._try_resume_thread(client, persisted_thread_id)
        if len(loaded_thread_ids) == 1:
            client.bind_thread(loaded_thread_ids[0])
            return True
        return False

    def wait_for_ready_signal(
        self, is_readiness_awaited: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        """Launch the agent, then (when awaited) block until the app-server is ready.

        Readiness is a successful JSON-RPC handshake with the daemon: a WebSocket
        connect plus ``initialize`` on the per-agent socket. This replaces the
        readiness glyph poll the send-keys TUI drive used (the daemon is a clean,
        unambiguous readiness signal -- no lazy-``SessionStart`` race). The
        ``start_action`` brings up the session (daemon + ``--remote`` windows); the
        primary window independently waits for the socket, so ordering is irrelevant.

        Callers pass ``is_readiness_awaited=True`` on create and ``False`` on
        start/resume. The signature is unchanged from ``AgentInterface``. On create,
        once the daemon is ready, mngr establishes+materializes+persists the ONE root
        conversation (see :meth:`_establish_root_conversation`) -- the app-server
        analogue of the session file a TUI agent gets for free at launch.
        """
        start_action()
        if is_readiness_awaited:
            self._wait_for_app_server_ready(timeout)
            self._establish_root_conversation()
            self._clear_hook_trust_prompt()

    def _clear_hook_trust_prompt(self) -> None:
        """Clear codex's one-time "Hooks need review" prompt on the ``--remote`` TUI so hooks run.

        The visible ``codex resume <id> --remote`` TUI stops on a "Hooks need review" screen and
        ignores ``--dangerously-bypass-hook-trust`` (a codex limitation of the resume path). Turns
        run in the daemon, but until this screen is cleared the daemon's hooks stay UNTRUSTED and
        never fire -- so the PreToolUse workspace safety guards (and the session-pointer recorder)
        are inert on every turn, typed or programmatic (verified live). Selecting "Trust all and
        continue" clears it AND persists the trust under ``CODEX_HOME``, so this is a one-time action
        at create: ``start`` / ``connect`` never see the screen again. It is consent-consistent with
        the workspace trust the user already granted (which is what gates the bypass flag), so mngr
        does not re-prompt. Best-effort: no screen within the timeout (already trusted, or the TUI
        never rendered it) is a no-op, and a re-check right before the keypress keeps a stray "2"
        from ever landing in the composer as a message.
        """
        if not self.host.is_local:
            return
        try:
            wait_for(
                self._is_on_hook_trust_prompt,
                timeout=_HOOK_TRUST_PROMPT_TIMEOUT_SECONDS,
                poll_interval=_READY_POLL_INTERVAL_SECONDS,
                error_message="codex hook-trust prompt did not appear",
            )
        except TimeoutError:
            return
        if not self._is_on_hook_trust_prompt():
            return
        self._send_hook_trust_keypress()

    def _is_on_hook_trust_prompt(self) -> bool:
        """Whether the agent's primary (``--remote`` TUI) window is showing the hook-trust screen."""
        pane = self.capture_pane_content()
        return pane is not None and _HOOK_TRUST_PROMPT_MARKER in pane

    def _send_hook_trust_keypress(self) -> None:
        """Select "Trust all and continue" on the hook-trust screen (a test seam).

        ``2`` selects the "Trust all and continue" option, ``Enter`` confirms (verified live: this
        clears the screen and makes every hook fire on subsequent programmatic and typed turns).
        """
        target_arg = self.tmux_target.as_shell_arg()
        result = self.host.execute_stateful_command(f"tmux send-keys -t {target_arg} 2 Enter")
        if result.success:
            logger.info("cleared codex hook-trust prompt for {} (hooks now trusted for this agent)", self.name)
        else:
            logger.warning(
                "could not clear codex hook-trust prompt for {} ({}); the safety hooks may stay "
                "untrusted until a human selects 'Trust all' in the terminal",
                self.name,
                result.stderr or result.stdout,
            )

    def _establish_root_conversation(self) -> None:
        """Establish, materialize, and persist this agent's ONE root conversation (create only).

        The TUI form gets a session file the moment it launches; the app-server form must
        reproduce that. After the daemon is ready, mngr ``thread/start``s a thread, materializes
        its rollout with ``thread/inject_items`` (no model turn), and persists the returned id as
        the root -- so a durable conversation exists before any client connects,
        ``assemble_command``'s ``codex resume <id> --remote`` attaches the visible TUI to it, and
        every send binds it. This is the app-server analogue of pi opening its session / claude
        writing ``claude_session_id``; the id is written from HERE (where mngr learns it), not from
        the ``UserPromptSubmit`` hook (which never fires on a programmatic turn).

        Skipped when a root is already persisted (an ``--adopt`` / ``--from`` clone wrote
        ``codex_root_session`` in ``on_after_provisioning`` -- that adopted session IS the root,
        resumed as-is), and on a remote host whose unix socket the mngr process cannot reach (the
        primary window then falls back to a fresh ``codex --remote``; establishment is a local-host
        convenience, not a correctness requirement). A failure to establish is logged and
        swallowed rather than aborting create: the primary window's bounded wait for the id file
        then times out and it launches a fresh ``codex --remote`` (degraded, not broken).
        """
        if not self.host.is_local:
            return
        adopted_root = self._read_root_session_id()
        if adopted_root is not None:
            # An `--adopt` / `--from` clone already established the root: `adopt_session`
            # (on_after_provisioning) copied its rollout store and wrote `codex_root_session`. Do NOT
            # start a second conversation over it -- but DO finish the pointers establishment would
            # otherwise write, which adopt does not: bind the send/status path to the adopted thread
            # (``codex_app_server_thread``) and record its rollout for the transcript streamer
            # (``codex_transcript_path``). Without these the adopted agent's sends open a stray thread
            # and its transcript stays empty (the ``UserPromptSubmit`` hook won't write them on a
            # programmatic turn), so it never recalls its resumed history.
            self._write_persisted_thread_id(adopted_root)
            self._persist_transcript_path(adopted_root)
            return
        try:
            client = self._open_app_server_client()
        except SendMessageError as exc:
            logger.warning("could not establish codex root conversation for {} (handshake): {}", self.name, exc)
            return
        try:
            info = client.thread_start(cwd=str(self.work_dir))
            client.inject_items(_ROOT_MATERIALIZE_ITEMS)
            self._persist_root_conversation(info.thread_id)
            self._persist_transcript_path(info.thread_id)
            logger.info("established codex root conversation {} for agent {}", info.thread_id, self.name)
        except (CodexAppServerError, OSError, WebSocketException) as exc:
            logger.warning("could not establish codex root conversation for {}: {}", self.name, exc)
        finally:
            client.close()

    def _persist_transcript_path(self, session_id: str) -> None:
        """Record the root rollout's absolute path in ``codex_transcript_path`` for the streamer.

        ``stream_transcript.sh`` tails the file named here to build the raw + common transcripts (what
        ``mngr transcript`` and the Minds web chat read). On a turn TYPED into the TUI, codex's
        ``UserPromptSubmit`` hook (``record_session_pointers.sh``) records it -- but that hook never
        fires on mngr's programmatic ``turn/start`` turns, so without this the transcript stays empty
        for every web/CLI-driven send. mngr records it itself from the rollout ``inject_items`` just
        materialized; codex appends to that same rollout across restarts, so the one write stays valid.
        """
        sessions_dir = self._get_codex_home() / "sessions"
        try:
            wait_for(
                lambda: self._find_adopted_rollout_path(self.host, sessions_dir, session_id) is not None,
                timeout=_ROLLOUT_MATERIALIZE_TIMEOUT_SECONDS,
                poll_interval=_READY_POLL_INTERVAL_SECONDS,
                error_message=f"codex rollout for {session_id} did not materialize",
            )
        except TimeoutError:
            logger.warning(
                "could not find the materialized rollout for {} to record its transcript path; "
                "the transcript may stay empty until the first TUI-typed turn",
                session_id,
            )
            return
        rollout_path = self._find_adopted_rollout_path(self.host, sessions_dir, session_id)
        if rollout_path is None:
            return
        self.host.write_text_file(self._get_agent_dir() / TRANSCRIPT_PATH_FILENAME, str(rollout_path))

    def _read_root_session_id(self) -> str | None:
        """Return the persisted root ``codex_root_session`` id for this agent, or ``None``."""
        try:
            content = self.host.read_text_file(self._get_root_session_file_path())
        except FileNotFoundError:
            return None
        stripped = content.strip()
        return stripped or None

    def _persist_root_conversation(self, thread_id: str) -> None:
        """Persist ``thread_id`` as this agent's root, to BOTH pointer files.

        ``codex_root_session`` is what ``assemble_command``'s ``codex resume <id> --remote`` and
        the adopt/preserve machinery read; ``codex_app_server_thread`` is what the send/status bind
        path reads. The app-server thread id equals the rollout ``session_id``, so both hold the
        same value.
        """
        self.host.write_text_file(self._get_root_session_file_path(), thread_id)
        self._write_persisted_thread_id(thread_id)

    def _wait_for_app_server_ready(self, timeout: float | None) -> None:
        """Poll until the daemon is ready, or raise ``AgentStartError`` on timeout.

        On a local host readiness connects to the unix socket directly and completes
        the ``initialize`` handshake -- the strongest possible readiness signal. On a
        remote host (where the mngr process cannot reach the agent host's unix socket)
        it falls back to confirming the socket is present over the host shell.
        """
        timeout_seconds = timeout if timeout is not None else self.get_ready_timeout_seconds()
        socket_path = get_codex_app_server_socket_path(self._get_codex_home())
        try:
            wait_for(
                lambda: self._is_app_server_ready(socket_path),
                timeout=timeout_seconds,
                poll_interval=_READY_POLL_INTERVAL_SECONDS,
                error_message=f"codex app-server did not become ready within {timeout_seconds:.0f}s",
            )
        except TimeoutError as exc:
            reason = f"codex app-server did not become ready within {timeout_seconds:.0f}s (socket {socket_path})"
            raise AgentStartError(str(self.name), reason) from exc

    def _is_app_server_ready(self, socket_path: Path) -> bool:
        """Return whether the daemon is ready (WS handshake locally, socket presence remotely)."""
        if self.host.is_local:
            return _probe_app_server_ready(socket_path)
        return self._is_app_server_socket_present(socket_path)

    def _is_app_server_socket_present(self, socket_path: Path) -> bool:
        """Return whether the daemon's unix socket exists (checked over the host shell)."""
        result = self.host.execute_idempotent_command(
            f"test -S {shlex.quote(str(socket_path))} && echo ready", timeout_seconds=5.0
        )
        return result.success and result.stdout.strip() == "ready"

    def send_message(self, message: str) -> None:
        """Deliver ``message`` to the codex agent over the app-server (unchanged public contract).

        Replaces the send-keys paste+Enter+evidence-probe path. Over a short-lived
        per-call WebSocket connection to the daemon, this:

        1. connects + ``initialize`` (the handshake);
        2. resolves + binds the agent's root thread and reads its LIVE status so the
           idle-vs-busy decision is correct on a fresh connection (see
           :meth:`_bind_thread_for_send`);
        3. ``submit``s the message -- ``turn/start`` when idle (delivered as a new user
           turn) or ``turn/steer`` when a turn is running (parked into it) -- and closes.

        The stateless CLI agent object cannot hold a long-lived connection, and codex
        keeps the thread (and any running turn) loaded after the starting connection
        closes (verified live), so a short-lived connection is both sufficient and
        correct. A ``turn/steer`` on a fresh connection correctly parks into the running
        turn; a blind ``turn/start`` while busy would open a SECOND concurrent turn,
        which is why the live status is read first.

        Exceptions preserve the CLI's exit-code contract (mapped in
        ``libs/mngr/imbue/mngr/api/message.py``): a message that is not accepted raises
        :class:`SendMessageError`; a message that is delivered but leaves codex blocked
        on an approval/input it cannot self-clear raises
        :class:`MessageDeliveredButBlockedError`. The per-agent ``message.lock`` still
        serializes concurrent sends (its removal is a later, ledger-side phase).
        """
        with self._message_lock(), log_span("Sending message to codex agent {} (length={})", self.name, len(message)):
            client = self._open_app_server_client()
            try:
                self._bind_thread_for_send(client)
                disposition = self._submit_over_app_server(client, message)
                self._raise_if_delivered_but_blocked(client, disposition)
            finally:
                client.close()

    def _connect_app_server_transport(self) -> AppServerTransport:
        """Open a WebSocket transport to this agent's daemon socket (a test seam).

        Overridden in tests to inject a scripted in-memory transport, so ``send_message``
        is exercised end-to-end without a live daemon.
        """
        socket_path = get_codex_app_server_socket_path(self._get_codex_home())
        try:
            return connect_app_server_transport(socket_path)
        except (OSError, WebSocketException) as exc:
            raise SendMessageError(str(self.name), f"could not connect to the codex app-server: {exc}") from exc

    def _open_app_server_client(self) -> CodexAppServerClient:
        """Connect a client and complete the ``initialize`` handshake, or raise ``SendMessageError``."""
        transport = self._connect_app_server_transport()
        client = CodexAppServerClient(transport=transport)
        try:
            client.initialize(_APP_SERVER_CLIENT_NAME, _app_server_client_version())
        except (CodexAppServerError, OSError, WebSocketException) as exc:
            client.close()
            raise SendMessageError(str(self.name), f"codex app-server handshake failed: {exc}") from exc
        return client

    def _bind_thread_for_send(self, client: CodexAppServerClient) -> None:
        """Bind the agent's root thread and seed the client's active-turn state.

        The daemon may hold several loaded threads (the root plus any sub-agents), so the
        root is pinned by the persisted thread id when present; otherwise a single loaded
        thread is adopted (e.g. one the visible ``--remote`` TUI started), and with none
        loaded a fresh thread is started. After binding an existing thread the LIVE status
        is read so a running turn is steered (parked) rather than a second turn opened.
        """
        try:
            persisted_thread_id = self._read_persisted_thread_id()
            loaded_thread_ids = client.thread_loaded_list()
            if persisted_thread_id is not None and persisted_thread_id in loaded_thread_ids:
                client.bind_thread(persisted_thread_id)
            elif persisted_thread_id is not None:
                if not self._try_resume_thread(client, persisted_thread_id):
                    self._start_and_persist_thread(client)
                    return
            elif len(loaded_thread_ids) == 1:
                client.bind_thread(loaded_thread_ids[0])
                self._write_persisted_thread_id(loaded_thread_ids[0])
            else:
                self._start_and_persist_thread(client)
                return
            client.read_thread_status()
        except CodexAppServerError as exc:
            raise SendMessageError(str(self.name), f"could not bind the codex thread: {exc}") from exc

    def _try_resume_thread(self, client: CodexAppServerClient, thread_id: str) -> bool:
        """Reload a persisted-but-unloaded thread from its rollout; return whether it worked.

        The thread is not currently loaded (e.g. after a daemon restart), so it is
        reloaded from the on-disk rollout via ``thread/resume``. A brand-new thread with
        no persisted rollout yet errors (``-32600``); the caller then starts fresh.
        """
        try:
            client.thread_resume(thread_id, cwd=str(self.work_dir))
            return True
        except CodexAppServerError as exc:
            logger.debug("codex thread {} could not be resumed ({}); starting fresh", thread_id, exc)
            return False

    def _start_and_persist_thread(self, client: CodexAppServerClient) -> None:
        """Start a fresh thread (idle) and persist its id as this agent's root thread."""
        info = client.thread_start(cwd=str(self.work_dir))
        self._write_persisted_thread_id(info.thread_id)

    def _submit_over_app_server(self, client: CodexAppServerClient, message: str) -> Disposition:
        """Submit ``message`` (mngr-minted client id); raise ``SendMessageError`` if not accepted."""
        client_id = _mint_client_id()
        try:
            return client.submit(message, client_id)
        except (CodexAppServerError, OSError, WebSocketException) as exc:
            raise SendMessageError(str(self.name), f"codex app-server did not accept the message: {exc}") from exc

    def _raise_if_delivered_but_blocked(self, client: CodexAppServerClient, disposition: Disposition) -> None:
        """Raise ``MessageDeliveredButBlockedError`` if a delivered message left codex blocked.

        Only a freshly-started turn can be immediately blocked on this send's behalf; a
        parked steer joins an already-running turn, whose blocking is that turn's concern.
        A message that is delivered but leaves codex parked on an approval/input it cannot
        self-clear (``thread/status`` ``active`` with ``waitingOnApproval`` /
        ``waitingOnUserInput``) is "delivered but blocked", which the CLI maps to a
        dedicated exit code -- distinct from a send that never landed. The status read is
        best-effort: a failure to read does not un-deliver the message.
        """
        if disposition.kind is not DispositionKind.STARTED:
            return
        try:
            snapshot = client.read_thread_status()
        except CodexAppServerError as exc:
            logger.debug("could not read codex thread status after send: {}", exc)
            return
        if snapshot.is_blocked_on_input:
            raise MessageDeliveredButBlockedError(
                str(self.name),
                "message delivered, but codex is now blocked awaiting an approval or input mngr cannot clear",
            )

    def _read_persisted_thread_id(self) -> str | None:
        """Return the persisted root app-server thread id for this agent, or ``None``."""
        try:
            content = self.host.read_text_file(self._get_agent_dir() / APP_SERVER_THREAD_FILENAME)
        except FileNotFoundError:
            return None
        stripped = content.strip()
        return stripped or None

    def _write_persisted_thread_id(self, thread_id: str) -> None:
        """Persist ``thread_id`` as this agent's root app-server thread id."""
        self.host.write_text_file(self._get_agent_dir() / APP_SERVER_THREAD_FILENAME, thread_id)

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
        """Per-agent file recording the root codex rollout ``session_id`` (for adopt/preserve).

        Written by ``record_session_pointers.sh`` at each turn boundary and by the
        adopt orchestrator's resume pointer; read by the adopt/preserve machinery to
        resolve which conversation a later agent resumes. Lives directly under the
        agent state dir so the hook's ``$MNGR_AGENT_STATE_DIR/{ROOT_SESSION_FILENAME}``
        and this path resolve to the same file.
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
                    # UserPromptSubmit hook: record the rollout session id + transcript
                    # path (non-lifecycle pointers; see build_codex_hooks_config).
                    RECORD_SESSION_POINTERS_SCRIPT_NAME: _load_codex_resource_script(
                        RECORD_SESSION_POINTERS_SCRIPT_NAME
                    ),
                },
                concurrency_group,
            )

    def _build_developer_instructions(self, host: OnlineHostInterface) -> str | None:
        """Join this agent type's system-prompt additions into one blob, or None if there are none.

        The ``append_system_prompt`` blocks come first, in stack order, and the style body
        last. Codex has no output-style concept, so ``output_style`` reaches it as instruction
        text rather than a named setting: the style file's body is used **verbatim**,
        frontmatter block included, so a style reads identically whichever agent type runs
        it. Placing it last means it is the nearest instruction to the model, matching how a
        harness with a real output-style setting applies the style over the prompt.

        The style directory read here is ``.agents/output-styles`` -- the source of truth
        where styles are authored. (Claude validates against its own ``.claude/output-styles``
        instead, since that is the path it will read; the two are the same files.)
        """
        blocks: list[str] = [str(prompt) for prompt in self.agent_config.append_system_prompt]
        if self.agent_config.output_style is not None:
            styles_dir = get_shared_output_styles_dir(Path(self.work_dir))
            # Raises UserInputError, listing what is available, when the name has no match.
            blocks.append(
                resolve_output_style(self.agent_config.output_style, read_output_style_files(host, styles_dir))
            )
        if not blocks:
            return None
        return DEVELOPER_INSTRUCTIONS_SEPARATOR.join(blocks)

    def _provision_codex_home(
        self,
        host: OnlineHostInterface,
        user_codex_home: Path,
        canonical_work_dir: str,
    ) -> None:
        """Write the mngr-owned per-agent ``CODEX_HOME`` tree (idempotent each provision).

        Provisions the auth.json symlink, config.toml (model/sandbox/approval +
        the credential-store pin + the trusted work-dir + notice suppressors +
        overrides + the ``developer_instructions`` role/harness prompt), hooks.json,
        and the personality-migration NUX-skip marker. ``host.write_text_file`` creates
        intermediate dirs; codex-owned ``sessions/`` is left intact across re-provision.
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
            log_dir=str(get_codex_tui_log_dir(codex_home)),
            developer_instructions=self._build_developer_instructions(host),
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
        no-op (consent previously given); ``auto_dismiss_dialogs_at_startup`` or
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

        if not (self.agent_config.auto_dismiss_dialogs_at_startup or mngr_ctx.is_auto_approve):
            if not mngr_ctx.is_interactive:
                logger.error(
                    "Source directory {} is not trusted by the Codex CLI. mngr will not silently "
                    "run a codex agent on untrusted code (which also bypasses codex's hook review). "
                    "Re-run interactively to be prompted, re-run with `--yes`, or set "
                    "`auto_dismiss_dialogs_at_startup = true` on the codex agent type.",
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
        """Build the full launch command (the app-server drive).

        The primary window runs the visible ``codex --remote`` TUI; a hidden sidecar
        window in the same session runs the ``codex app-server`` daemon it connects to.
        Composition of the primary-window command (left to right):

        1. ``( bash codex_background_tasks.sh <session> ) &`` -- backgrounded
           transcript supervisor (scoped to ``&`` so the foreground process is codex).
        2. ``mkdir -p <TUI-log dir>`` + ``cd <work_dir>`` -- ensure dirs, and codex's
           cwd becomes the (trusted) work dir.
        3. ``{ <stamp-process-start>; rm -f <sock>; <spawn daemon window>; <wait for
           sock>; env CODEX_HOME=<home> codex --remote unix://<sock>; }`` -- clean the
           stale socket, spawn the daemon as the FOREGROUND process of a detached sidecar
           window (so ``tmux kill-session`` reaps it -- no orphan daemons, no leaked
           sockets), wait (bounded) for the socket, then run the visible ``--remote`` TUI
           in the foreground (what ``connect``/open-terminal attaches to, what
           ``capture_pane_content`` reads, and what process detection matches -- still
           ``codex``).

        The daemon window runs ``codex app-server --listen unix://<sock>`` under the
        per-agent ``CODEX_HOME``; ``codex --remote`` exits when that daemon dies (lost
        connection), so the primary window's liveness tracks the daemon's -- daemon
        death takes the TUI window down and mngr reports the agent stopped (no
        auto-respawn; ``mngr start`` relaunches both). This is what lets process
        presence alone answer STOPPED without any marker.

        No lifecycle-marker reset is needed (the lifecycle no longer reads any marker;
        RUNNING vs WAITING comes from the daemon's ``thread/status``). The process-start
        stamp is retained (the system_interface activity tracker still uses its mtime as a
        stale-tail gate). The primary window resumes the persisted root conversation with
        ``codex resume <root_session_id> --remote`` (self-healing: fresh ``codex --remote`` when
        there is no persisted root), after a bounded wait for mngr to write ``codex_root_session``.

        Bash precedence: ``A & B && C`` parses as ``A &`` then ``B && C``, so the
        supervisor subshell is backgrounded while ``mkdir`` / ``cd`` / the codex group
        form the foreground chain.
        """
        codex_home = self._get_codex_home()
        base = str(command_override) if command_override is not None else str(self.agent_config.command)
        socket_path = get_codex_app_server_socket_path(codex_home)
        socket_url = shlex.quote(f"unix://{socket_path}")
        quoted_socket = shlex.quote(str(socket_path))

        # cli_args + agent_args are threaded to the engine (the daemon) as top-level
        # codex flags, before the ``app-server`` subcommand.
        extra_args = list(self.agent_config.cli_args) + [shlex.quote(arg) for arg in agent_args]
        extra_str = (" " + " ".join(extra_args)) if extra_args else ""

        background_cmd = self._build_background_tasks_command()
        # Make the TUI-log dir too, so codex's file layer can open the heartbeat log.
        mkdir_cmd = f"mkdir -p {shlex.quote(str(get_codex_tui_log_dir(codex_home)))}"
        cd_cmd = f"cd {shlex.quote(str(self.work_dir))}"
        home_env = f"env CODEX_HOME={shlex.quote(str(codex_home))}"
        # RUST_LOG=...,codex_otel=info makes codex-core (in the daemon) write
        # `codex.sse_event` delta lines into the TUI log (log_dir is set in config.toml);
        # the system_interface still tails those during the migration.
        daemon_env = f"{home_env} RUST_LOG={shlex.quote(RUST_LOG_VALUE)}"

        # Stamp the process-start boundary on every launch/resume (activity-tracker mtime gate).
        state = "$MNGR_AGENT_STATE_DIR"
        process_started_cmd = f'touch "{state}/{PROCESS_STARTED_MARKER_FILENAME}" 2>/dev/null || true'

        # Remove any stale socket a prior run left behind before the daemon binds.
        rm_socket_cmd = f"rm -f {quoted_socket}"

        # The daemon runs as the foreground process of a detached sidecar window in the
        # SAME session, so `tmux kill-session` reaps it. `exec` replaces the wrapper shell
        # so the window's process IS codex (clean death -> the window closes).
        #
        # The daemon is where TURNS run, so it is where codex fires its hooks (the
        # UserPromptSubmit marker writer that records the live rollout path, the PreToolUse
        # policy hooks, ...). Those hooks are invoked as `bash "$MNGR_AGENT_STATE_DIR/..."`
        # and `bash "$MNGR_AGENT_WORK_DIR/..."`, so they need the agent env. The primary
        # window gets that env because mngr prefixes its launch command with the same source
        # step; this detached sidecar window is spawned by `tmux new-window` with a bare
        # environment, so without this prefix every hook resolves to a nonexistent path and
        # dies (exit 127) -- which silently breaks the transcript marker (empty web chat) and
        # the safety hooks. Source the host + agent env here so the daemon and its hook
        # children inherit it (the source paths are absolute, so a bare window can run them).
        source_env_prefix = host.build_source_env_prefix(self)
        # Capture the daemon's own stdout/stderr to a per-agent log so a daemon that dies on
        # launch (bad env, codex startup error) leaves a diagnosable trace instead of a window
        # that silently closes. Absolute path (computed here), since the bare sidecar window has no
        # agent env until ``source_env_prefix`` runs -- and the redirect must also catch a failure
        # in that prefix or the ``cd``.
        daemon_log = shlex.quote(str(self._get_agent_dir() / "app_server.log"))
        daemon_cmd = (
            f"{{ {source_env_prefix}{cd_cmd} && exec {daemon_env} {base}{extra_str} "
            f"{_DAEMON_HOOK_FLAGS} app-server --listen {socket_url} ; }} > {daemon_log} 2>&1"
        )
        spawn_daemon_cmd = (
            f"tmux new-window -d -t {shlex.quote(self.session_name)} "
            f"-n {shlex.quote(_APP_SERVER_WINDOW_NAME)} {shlex.quote(daemon_cmd)}"
        )

        # Wait (bounded) for the daemon's socket so window start-order is irrelevant.
        wait_for_socket_cmd = (
            f"__mngr_i=0; while [ ! -S {quoted_socket} ] && "
            f'[ "$__mngr_i" -lt {_SOCKET_WAIT_POLLS} ]; do sleep {_SOCKET_WAIT_INTERVAL_SECONDS}; '
            "__mngr_i=$((__mngr_i+1)); done"
        )
        # Wait (bounded) for mngr to persist the root session id before the TUI launches, so the
        # visible window attaches to the ONE root conversation. On create, ``wait_for_ready_signal``
        # writes ``codex_root_session`` after the daemon is ready (which happens in parallel with
        # this window's wait -- no deadlock, the handshake uses the sidecar daemon's socket). On
        # resume the file already exists, so this passes immediately. If it never appears (a
        # degraded create where establishment failed, or a remote host), the wait times out and the
        # TUI launches fresh -- self-healing, never a hang.
        quoted_root_file = shlex.quote(str(self._get_root_session_file_path()))
        wait_for_root_id_cmd = (
            f"__mngr_j=0; while [ ! -s {quoted_root_file} ] && "
            f'[ "$__mngr_j" -lt {_SOCKET_WAIT_POLLS} ]; do sleep {_SOCKET_WAIT_INTERVAL_SECONDS}; '
            "__mngr_j=$((__mngr_j+1)); done"
        )
        # The visible TUI, in the foreground of the primary window: a self-healing
        # ``codex resume <root_session_id> --remote`` that attaches to the persisted root thread
        # (the daemon cold-loads it from its rollout), falling back to a fresh ``codex --remote``
        # only when there is genuinely no persisted root. Mirrors pi's ``pi --session <file>`` /
        # claude's ``--resume`` chain, so ``stop`` -> ``start`` returns to the same conversation.
        #
        # ``--dangerously-bypass-hook-trust`` is what makes mngr's hooks actually run: codex parks
        # newly-discovered command hooks as UNTRUSTED ("in review") until a human approves them in
        # the TUI, and an untrusted hook never fires -- not for a typed turn and not for a
        # programmatic (web/CLI) ``turn/start``. The daemon holds the hook engine, and this flag on
        # the ``--remote`` client that drives it clears the review gate so the workspace's safety
        # guards (PreToolUse: pipe/rebase blockers, the OOM + git-identity rewrite, the tk
        # discipline guards) run on EVERY turn regardless of who sent it. Consent-gated: the flag
        # only takes effect on a workspace the user has trusted (``[projects] trust_level``).
        remote_base = f"{home_env} {base}"
        remote_tail = f"--remote {socket_url} --dangerously-bypass-hook-trust"
        remote_cmd = (
            f"__mngr_sid=$(cat {quoted_root_file} 2>/dev/null || true); "
            f'if [ -n "$__mngr_sid" ]; then {remote_base} resume "$__mngr_sid" {remote_tail}; '
            f"else {remote_base} {remote_tail}; fi"
        )

        return CommandString(
            f"{background_cmd} {mkdir_cmd} && {cd_cmd} "
            f"&& {{ {process_started_cmd}; {rm_socket_cmd}; "
            f"{spawn_daemon_cmd}; {wait_for_socket_cmd}; {wait_for_root_id_cmd}; {remote_cmd} ; }}"
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
    """Layer the daemon's blocked-on-input signal onto the base lifecycle state.

    Promotes RUNNING -> WAITING while codex's turn is parked awaiting an approval/input
    it cannot self-clear (``is_blocked_on_permission`` from ``thread/status``), which
    would otherwise read RUNNING since the turn is still in flight. Every non-RUNNING
    base state passes through unchanged. Kept pure (no agent/host) so
    ``get_lifecycle_state``'s promotion rule is unit-testable without standing up a
    tmux pane.

    Defers the gating decision to the shared ``classify_waiting_reason``: a RUNNING
    base state means a turn is in flight, so the classifier's ``is_active`` gate is
    satisfied and a PERMISSIONS verdict is what promotes RUNNING to WAITING. Sharing
    that one function keeps this promotion and the ``waiting_reason`` field generator
    from drifting apart.
    """
    if base_state != AgentLifecycleState.RUNNING:
        return base_state
    reason = classify_waiting_reason(is_active=True, is_blocked_on_permission=is_blocked_on_permission)
    return AgentLifecycleState.WAITING if reason is WaitingReason.PERMISSIONS else base_state


def _waiting_reason(agent: AgentInterface, host: OnlineHostInterface) -> WaitingReason | None:
    """Return why the agent is waiting (or None if actively running), from live thread status.

    Delegates to :meth:`CodexAgent.compute_waiting_reason`, which reads the daemon's live
    ``thread/status`` (a turn in flight -> None; a turn parked on an approval/input -> PERMISSIONS;
    idle -> END_OF_TURN) as the SOLE source. The shared ``classify_waiting_reason`` produces the
    verdict, so this field and the ``get_lifecycle_state`` RUNNING -> WAITING promotion cannot drift.

    Registered only for the codex type, so ``agent`` is always a :class:`CodexAgent`; the
    isinstance guard narrows the type (a foreign agent, which the registration prevents, yields
    ``None`` rather than a marker read off ``host``).
    """
    return agent.compute_waiting_reason() if isinstance(agent, CodexAgent) else None


@hookimpl
def agent_field_generators() -> tuple[str, dict[str, Callable[[AgentInterface, OnlineHostInterface], Any]]] | None:
    """Expose codex-specific agent fields for listing."""
    return ("codex", {"waiting_reason": _waiting_reason})

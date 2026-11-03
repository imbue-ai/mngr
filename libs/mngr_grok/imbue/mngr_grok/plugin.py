import importlib.resources
import json
import re
import shlex
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import ClassVar
from typing import Final
from typing import NoReturn
from typing import assert_never

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.primitives import NonEmptyStr
from imbue.imbue_common.pure import pure
from imbue.mngr import hookimpl
from imbue.mngr.agents.common_transcript import maybe_provision_common_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_raw_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_scripts_to_commands_dir
from imbue.mngr.agents.installation import VersionMismatchPolicy
from imbue.mngr.agents.installation import ensure_cli_installed
from imbue.mngr.agents.installation import verify_pinned_cli_version
from imbue.mngr.agents.output_styles import read_output_style_files
from imbue.mngr.agents.output_styles import resolve_output_style
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import EnterRetryGate
from imbue.mngr.agents.tui_utils import PANE_TARGET_SHELL_VARIABLE
from imbue.mngr.agents.tui_utils import SubmissionConfirmationPolicy
from imbue.mngr.agents.tui_utils import SubmissionEvidenceProbe
from imbue.mngr.agents.tui_utils import build_changed_token_probe
from imbue.mngr.agents.tui_utils import build_normalized_message_probe
from imbue.mngr.agents.tui_utils import is_shell_command_message
from imbue.mngr.agents.update_policy import AgentUpdatePolicy
from imbue.mngr.agents.update_policy import is_self_update_disabled
from imbue.mngr.api.preservation import PreservedItem
from imbue.mngr.api.preservation import adopt_sessions
from imbue.mngr.api.preservation import flag_gated_items
from imbue.mngr.api.preservation import preserve_agent_state
from imbue.mngr.api.preservation import preserve_host_agents_on_destroy
from imbue.mngr.api.preservation import run_adopt_session_preflight
from imbue.mngr.api.providers import get_local_host
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.field_markers import SettingsPatchField
from imbue.mngr.errors import AgentInstallationError
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.errors import SendFailureKind
from imbue.mngr.errors import SendMessageError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import classify_waiting_reason
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.hosts.tmux import build_tmux_capture_pane_command
from imbue.mngr.hosts.tmux import quote_tmux_text_argument
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
from imbue.mngr.plugin_catalog import GROK_BUILD_VERSION_LINE_PATTERN
from imbue.mngr.plugins.hookspecs import OnBeforeCreateArgs
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import OutputStyleName
from imbue.mngr.primitives import SystemPromptText
from imbue.mngr.primitives import WaitingReason
from imbue.mngr.utils.agent_skills import split_skill_frontmatter
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_grok import resources as grok_resources
from imbue.mngr_grok.grok_config import GROK_PLUGIN_STATE_RELATIVE_PATH
from imbue.mngr_grok.grok_config import GrokRootTurn
from imbue.mngr_grok.grok_config import LAUNCH_ENV_RELATIVE_PATH
from imbue.mngr_grok.grok_config import LAUNCH_SCRIPT_NAME
from imbue.mngr_grok.grok_config import RENDERED_MODEL_SETTINGS_RELATIVE_PATH
from imbue.mngr_grok.grok_config import ROLE_PROMPT_RELATIVE_PATH
from imbue.mngr_grok.grok_config import ROOT_TURNS_RELATIVE_PATH
from imbue.mngr_grok.grok_config import RenderedGrokConfig
from imbue.mngr_grok.grok_config import SESSION_ID_RELATIVE_PATH
from imbue.mngr_grok.grok_config import build_config_toml
from imbue.mngr_grok.grok_config import build_hooks_json
from imbue.mngr_grok.grok_config import build_launch_command
from imbue.mngr_grok.grok_config import build_launch_env
from imbue.mngr_grok.grok_config import build_requirements_toml
from imbue.mngr_grok.grok_config import fold_settings_overrides_onto_empty_config
from imbue.mngr_grok.grok_config import get_grok_home
from imbue.mngr_grok.grok_config import parse_grok_config_text
from imbue.mngr_grok.grok_config import parse_rendered_model_settings
from imbue.mngr_grok.grok_config import parse_root_turn
from imbue.mngr_grok.grok_config import render_rendered_model_settings
from imbue.mngr_grok.grok_config import render_toml
from imbue.mngr_grok.grok_screen import GROK_ENTER_RETRY_CONDITION
from imbue.mngr_grok.grok_screen import GrokGateScreen
from imbue.mngr_grok.grok_screen import build_queued_message_count_command
from imbue.mngr_grok.grok_screen import composer_holds_text
from imbue.mngr_grok.grok_screen import detect_gate_screen
from imbue.mngr_grok.grok_screen import has_cancel_hint
from imbue.mngr_grok.grok_screen import has_press_again_hint
from imbue.mngr_grok.grok_screen import has_send_now_hint
from imbue.mngr_grok.grok_screen import is_always_approve_option_selected
from imbue.mngr_grok.grok_screen import is_idle_and_empty
from imbue.mngr_grok.grok_screen import is_idle_holding_text
from imbue.mngr_grok.grok_screen import is_ready_for_paste
from imbue.mngr_grok.grok_screen import read_composer_text
from imbue.mngr_grok.grok_sessions import GROK_SESSIONS_RELATIVE_PATH
from imbue.mngr_grok.grok_sessions import build_grok_preserved_items
from imbue.mngr_grok.grok_sessions import copy_adopted_grok_session
from imbue.mngr_grok.grok_sessions import copy_cloned_grok_session
from imbue.mngr_grok.grok_sessions import get_local_user_grok_home
from imbue.mngr_grok.grok_sessions import record_grok_fork_source
from imbue.mngr_grok.grok_sessions import resolve_canonical_work_dir
from imbue.mngr_grok.grok_sessions import resolve_grok_adopt_session
from imbue.mngr_grok.resources.grok_hook import ACTIVE_MARKER_FILENAME
from imbue.mngr_grok.resources.grok_hook import PERMISSIONS_WAITING_FILENAME
from imbue.mngr_grok.resources.grok_hook import SESSION_STARTED_FILENAME
from imbue.mngr_grok.resources.grok_hook import SUBMISSIONS_LOG_FILENAME
from imbue.mngr_grok.resources.grok_hook import TURN_DROPPED_FLAG
from imbue.mngr_grok.trust import ensure_grok_folder_trust
from imbue.mngr_grok.trust import prompt_user_to_trust_grok_folder

_GROK_AGENT_TYPE_NAME: Final[str] = "grok"
_GROK_BINARY_NAME: Final[str] = "grok"
_GROK_NPM_PACKAGE: Final[str] = "@xai-official/grok"
_GROK_DISABLE_AUTOUPDATER_ENV_VAR: Final[str] = "GROK_DISABLE_AUTOUPDATER"
_GROK_BUILD_VERSION_LINE_RE: Final[re.Pattern[str]] = re.compile(GROK_BUILD_VERSION_LINE_PATTERN, re.MULTILINE)

_BACKGROUND_TASKS_SCRIPT_NAME: Final[str] = "grok_background_tasks.sh"
RAW_TRANSCRIPT_SCRIPT_NAMES: Final[tuple[str, ...]] = ("stream_transcript.sh", "grok_transcript_streamer.py")
COMMON_TRANSCRIPT_SCRIPT_NAMES: Final[tuple[str, ...]] = ("common_transcript.sh", "common_transcript_convert.py")
_HOOK_HANDLER_SCRIPT_NAME: Final[str] = "grok_hook.py"
_HOOKS_DIRNAME: Final[str] = "hooks"
_HOOKS_CONFIG_FILENAME: Final[str] = "mngr.json"
_CONFIG_FILENAME: Final[str] = "config.toml"
_REQUIREMENTS_FILENAME: Final[str] = "requirements.toml"
_PRIVATE_FILE_MODE: Final[str] = "0600"
_EXECUTABLE_FILE_MODE: Final[str] = "0755"

_SUBMISSIONS_LOG_RELATIVE_PATH: Final[Path] = GROK_PLUGIN_STATE_RELATIVE_PATH / SUBMISSIONS_LOG_FILENAME
_LAST_INTERRUPT_RELATIVE_PATH: Final[Path] = GROK_PLUGIN_STATE_RELATIVE_PATH / "last_interrupt"

# The work dir's directory of output-style files.
_OUTPUT_STYLES_RELATIVE_PATH: Final[Path] = Path(".agents", "output-styles")
_ROLE_BLOCK_SEPARATOR: Final[str] = "\n\n"
# The role reaches grok as one argv string, which Linux caps at 128 KiB.
_ROLE_PROMPT_MAX_BYTES: Final[int] = 100_000
_ROLE_DESCRIPTION_MAX_LENGTH: Final[int] = 80

_HOST_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0
_PANE_PROBE_TIMEOUT_SECONDS: Final[float] = 10.0
_READY_POLL_INTERVAL_SECONDS: Final[float] = 0.1
# Two Ctrl+C presses within about a second quit grok.
_INTERRUPT_MIN_INTERVAL_SECONDS: Final[float] = 1.5
_INTERRUPT_SPACING_POLL_INTERVAL_SECONDS: Final[float] = 0.05
# grok ends or drops a cancelled turn, and empties its idle composer at the Ctrl+C that clears it, within a second.
_CANCEL_SETTLE_TIMEOUT_SECONDS: Final[float] = 5.0
_CANCEL_SETTLE_POLL_INTERVAL_SECONDS: Final[float] = 0.1

# A Ctrl chord as tmux names it: `C-c`, `C-Enter`, `^c`.
_CTRL_CHORD_RE: Final[re.Pattern[str]] = re.compile(r"\A(?:C-|\^)(?P<key>[^ ]+)\Z", re.IGNORECASE)
_INTERRUPT_CTRL_KEY: Final[str] = "c"
_ALWAYS_APPROVE_TOGGLE_CTRL_KEY: Final[str] = "o"
_QUIT_CTRL_KEY: Final[str] = "q"
_SEND_NOW_CTRL_KEYS: Final[frozenset[str]] = frozenset({"enter", "kpenter"})
# Shift+Tab as tmux names it (lowercased).
_MODE_CYCLE_KEYS: Final[frozenset[str]] = frozenset({"btab", "s-tab"})
# Keys that choose "always approve" on a permission card, as tmux names them (lowercased).
_PERMISSION_CARD_ALWAYS_APPROVE_KEYS: Final[frozenset[str]] = frozenset({"1", "kp1"})
# Enter as tmux names it (lowercased); `C-m` is Enter's control code.
_ENTER_KEYS: Final[frozenset[str]] = frozenset({"enter", "kpenter", "c-m", "^m"})

_UNREADABLE_COMPOSER_TEXT: Final[str] = "(text grok shows in its composer)"
_DROPPED_TURN_LEFT_RECORDED: Final[str] = (
    "the agent reads RUNNING until grok's idle notification ends the turn, about a minute later"
)
_TRUST_DIALOG_ADVICE_OPENING: Final[str] = (
    "grok is asking whether to trust the agent's work dir, although mngr records grok's folder trust when it "
    "provisions the agent"
)
_LOGIN_ADVICE_OPENING: Final[str] = (
    "grok has no usable credential: run `grok login` (`grok login --device-auth` on a remote host), or set "
    "GROK_AUTH_PATH or XAI_API_KEY in the agent's environment"
)
_CONSENT_ADVICE_OPENING: Final[str] = "grok shows a notice from xAI that has to be accepted once"

# The labelled lines the cancel probes print: `turn:<promptId>` for the turn, then `pane:` before the pane.
_TURN_RECORD_LABEL: Final[str] = "turn:"
_PANE_LABEL: Final[str] = "pane:"

# The record separators the agent-definition listing puts before each file's path and before its content.
_DEFINITION_PATH_SEPARATOR: Final[str] = "\x1e"
_DEFINITION_CONTENT_SEPARATOR: Final[str] = "\x1f"


class GrokAgentConfig(AgentTypeConfig):
    """Config for the grok agent type."""

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
        default=CommandString("grok"),
        description="Command to run the Grok Build CLI.",
    )
    model: str | None = Field(
        default=None,
        description="Model to launch grok with (`-m`). None leaves the account's default model in force.",
    )
    reasoning_effort: str | None = Field(
        default=None,
        description="Reasoning effort to launch grok with (`--reasoning-effort`). None leaves grok's default.",
    )
    auto_allow_permissions: bool = Field(
        default=False,
        description='When True, grok approves every tool call without prompting (`[ui] permission_mode = "always-approve"`). '
        'When False (default), the per-agent config.toml sets `permission_mode = "ask"` explicitly.',
    )
    auto_dismiss_dialogs_at_startup: bool = Field(
        default=False,
        description="When True, record grok's folder trust for the work dir without asking. When False (default), "
        "an existing grant in the user's trust store is copied, `--yes` trusts without asking, a terminal is asked, "
        "and otherwise the create fails.",
    )
    settings_overrides: Annotated[dict[str, Any], SettingsPatchField()] = Field(
        default_factory=dict,
        description="A patch folded last onto the per-agent config.toml; layers accumulate. A bare key assigns "
        "(narrowing-checked); to merge onto mngr's rendered value instead, declare the key's dotted path in a "
        'top-level `__mngr_merge` map with the value "extend". See mngr\'s config README.',
    )
    share_auth: bool = Field(
        default=True,
        description="When True (default), point GROK_AUTH_PATH at the user's grok credential file, so one "
        "login authenticates every agent. When False, no stored login reaches the agent, which then needs "
        "XAI_API_KEY in its environment.",
    )
    isolate_from_claude_config: bool = Field(
        default=True,
        description="When True (default), grok imports none of the user's Claude Code or Cursor "
        "configuration (hooks, skills, rules, agents, MCP servers, sessions). When False, grok keeps it "
        "except the Claude Code and Cursor hooks and the plugins in the user's Claude `enabledPlugins`.",
    )
    pin_privacy_settings: bool = Field(
        default=True,
        description="When True (default), turn off grok's telemetry, trace upload, feedback, campaigns "
        "and codebase upload in the per-agent configuration and environment.",
    )
    memory: bool = Field(
        default=False,
        description="Whether grok's memory feature is enabled. The value is written explicitly to the "
        "per-agent config.toml, so no remote or managed default can turn it on; when enabled, "
        "preservation on destroy also keeps the memory store.",
    )
    check_installation: bool = Field(
        default=True,
        description="Check whether grok is installed, install it if missing, and check that `grok --version` "
        "reports xAI's Grok Build (if False, assume it is already present).",
    )
    version: str | None = Field(
        default=None,
        description="Pin the grok CLI version to install (e.g., '1.0.62'). When set, installation runs "
        "`npm install -g @xai-official/grok@<version>` and provisioning verifies the installed grok "
        "matches (see `version_mismatch`). When None (the default), installs the latest version.",
    )
    version_mismatch: VersionMismatchPolicy = Field(
        default=VersionMismatchPolicy.ERROR,
        description="What provisioning does when the installed grok does not report the pinned `version`. "
        "`ERROR` (default): fail the create. `WARN`: log a warning and continue on the installed binary.",
    )
    update_policy: AgentUpdatePolicy | None = Field(
        default=None,
        description="How to handle grok's own auto-updater. NEVER sets GROK_DISABLE_AUTOUPDATER=1 and "
        "turns the updater off in the per-agent config.toml, so the installed build stays put; AUTO "
        "leaves it on. ASK has no interactive flow for grok and behaves like AUTO. When unset (the "
        "default), resolves to NEVER.",
    )
    emit_common_transcript: bool = Field(
        default=True,
        description="When True, emit a common-schema transcript that `mngr transcript` reads.",
    )
    preserve_on_destroy: bool = Field(
        default=True,
        description="When destroying this agent, first copy its transcripts and resumable session "
        "store to <local_host_dir>/preserved/ so they survive. Set to False to discard them.",
    )


class GrokProvisioningError(PluginMngrError):
    """Raised when provisioning cannot read what a grok agent's config is built from, or prepare its grok home."""


class GrokInputBlockedError(SendMessageError):
    """Raised when grok shows a screen that holds its keyboard in place of the session composer."""

    def __init__(
        self, agent_name: str, gate_screen: GrokGateScreen, is_auth_shared: bool, is_at_startup: bool
    ) -> None:
        self.gate_screen = gate_screen
        super().__init__(
            agent_name,
            _describe_grok_gate_screen(gate_screen, agent_name, is_auth_shared, is_at_startup),
            SendFailureKind.INPUT_BLOCKED,
        )


class GrokKeyRefusedError(SendMessageError):
    """Raised when a key press could change grok's settings, cut a turn short or quit grok."""

    def __init__(self, agent_name: str, key: str, refusal_reason: str) -> None:
        self.key = key
        super().__init__(agent_name, f"refused to press {key}: {refusal_reason}", SendFailureKind.INPUT_BLOCKED)


class GrokCancelUnsettledError(SendMessageError):
    """Raised when grok dropped the turn mngr's Ctrl+C cancelled and mngr could not clear its composer or end the turn."""

    def __init__(self, agent_name: str, reason: str) -> None:
        super().__init__(agent_name, f"pressed Ctrl+C to cancel the turn, but {reason}", SendFailureKind.UNKNOWN)


class GrokCancelReading(FrozenModel):
    """The cancelled turn's record and the agent's pane, read in one host command after the Ctrl+C."""

    cancelled_turn: GrokRootTurn = Field(description="The turn as mngr's records showed it when Ctrl+C was pressed")
    recorded_prompt_id: str = Field(description="The promptId its `turns/` file records now; empty once it is gone")
    pane: str = Field(description="The agent's pane; empty when the capture failed")


class GrokHostPaths(FrozenModel):
    """What provisioning reads about the agent's host, resolved over the host shell with the agent's environment."""

    home_dir: Path = Field(description="$HOME on the host")
    user_grok_home: Path = Field(description="The user's own grok home, `${GROK_HOME:-$HOME/.grok}`")
    user_auth_path: Path = Field(
        description="The user's grok credential file, `${GROK_AUTH_PATH:-${GROK_HOME:-$HOME/.grok}/auth.json}`"
    )
    agent_definition_text_by_path: dict[str, str] = Field(
        description="The Claude and legacy grok agent definitions on the host, by path"
    )


@pure
def _describe_grok_gate_screen(
    gate_screen: GrokGateScreen, agent_name: str, is_auth_shared: bool, is_at_startup: bool
) -> str:
    """What the screen is and how to clear it; at startup, advice that holds whether or not the agent was kept."""
    match gate_screen:
        case GrokGateScreen.TRUST_DIALOG if is_at_startup:
            return f"{_TRUST_DIALOG_ADVICE_OPENING}; report the problem with the work dir's layout"
        case GrokGateScreen.TRUST_DIALOG:
            return f"{_TRUST_DIALOG_ADVICE_OPENING}; answer the dialog in the agent's terminal and report the problem"
        case GrokGateScreen.LOGIN if is_auth_shared and is_at_startup:
            return f"{_LOGIN_ADVICE_OPENING}, then run the same `mngr create` again"
        case GrokGateScreen.LOGIN if is_auth_shared:
            return f"{_LOGIN_ADVICE_OPENING}, then run `mngr create {agent_name} --reuse --update`"
        case GrokGateScreen.LOGIN:
            return (
                "grok has no usable credential, and with share_auth = false no stored login reaches the agent: "
                "pass XAI_API_KEY to the agent when you create it (`--pass-env XAI_API_KEY`)"
            )
        case GrokGateScreen.CONSENT if is_auth_shared and is_at_startup:
            return (
                f"{_CONSENT_ADVICE_OPENING}: run `grok` yourself and accept it, then run the same `mngr create` again "
                "so the agent copies the answer"
            )
        case GrokGateScreen.CONSENT if is_at_startup:
            return (
                f"{_CONSENT_ADVICE_OPENING}, and with share_auth = false mngr does not copy your answer into the "
                "agent; report the problem"
            )
        case GrokGateScreen.CONSENT if is_auth_shared:
            return (
                f"{_CONSENT_ADVICE_OPENING}: run `grok` yourself and accept it, then run "
                f"`mngr create {agent_name} --reuse --update` so the agent copies the answer, or accept it in the "
                "agent's terminal"
            )
        case GrokGateScreen.CONSENT:
            return f"{_CONSENT_ADVICE_OPENING}: accept it in the agent's terminal"
        case GrokGateScreen.PAYWALL:
            return "grok reports that the account has no subscription that includes Grok Build"
        case GrokGateScreen.PERMISSION_CARD if is_at_startup:
            return "grok is waiting for an answer to a permission request while it starts; report the problem"
        case GrokGateScreen.PERMISSION_CARD:
            return "grok is waiting for an answer to a permission request; answer it in the agent's terminal"
        case _ as unreachable:
            assert_never(unreachable)


@pure
def describe_grok_role(role_text: str) -> str:
    """A short name for a role prompt in a log line: its quoted first non-blank line, shortened when long."""
    first_line = next((line.strip() for line in role_text.splitlines() if line.strip()), "")
    if first_line == "":
        return "no role"
    if len(first_line) <= _ROLE_DESCRIPTION_MAX_LENGTH:
        return repr(first_line)
    return repr(first_line[:_ROLE_DESCRIPTION_MAX_LENGTH] + "…")


@pure
def build_submissions_log_evidence_probe(env_prefix: str, message: str) -> SubmissionEvidenceProbe:
    """Probe that confirms once `submissions.log` gains a user prompt whose tail ends with the message's.

    Text already in grok's composer goes out ahead of the message as one prompt, so the logged tail of a short
    message carries that text's last characters first.
    """
    count_command = (
        f'{env_prefix}{{ log_path="$MNGR_AGENT_STATE_DIR/{_SUBMISSIONS_LOG_RELATIVE_PATH.as_posix()}"; '
        'if [ -f "$log_path" ]; then '
        f"awk -v tail={shlex.quote(build_normalized_message_probe(message))} "
        '\'$2 == tail || (tail != "" && substr($2, length($2) - length(tail) + 1) == tail) { count++ } '
        'END { print count + 0 }\' "$log_path"; '
        "else echo 0; fi; }"
    )
    return SubmissionEvidenceProbe(
        name="submissions-log",
        baseline_command=count_command,
        poll_command=(
            f'count="$( {count_command} )"; if [ "${{count:-0}}" -gt "${{base:-0}}" ]; then echo submitted; fi'
        ),
    )


@pure
def build_queued_row_evidence_probe(pane_capture_command: str, message: str) -> SubmissionEvidenceProbe:
    """Probe that confirms once the pane shows one more queued row that can show the message."""
    count_command = build_queued_message_count_command(message)
    capture_assignment = f'raw_pane="$( {pane_capture_command} )"'
    return SubmissionEvidenceProbe(
        name="queued-row",
        baseline_command=f"{capture_assignment}; {count_command}",
        poll_command=(
            f'{capture_assignment}; count="$( {count_command} )"; '
            'if [ "${count:-0}" -gt "${base:-0}" ]; then echo queued; fi'
        ),
    )


@pure
def build_session_id_evidence_probe(env_prefix: str) -> SubmissionEvidenceProbe:
    """Probe that confirms once mngr's hook records another root session, as when `/new` starts one."""
    return build_changed_token_probe(
        "session-id", f'{env_prefix}cat "$MNGR_AGENT_STATE_DIR/{SESSION_ID_RELATIVE_PATH.as_posix()}" 2>/dev/null'
    )


@pure
def _build_permission_wait_capture_command(marker_path: Path, pane_capture_command: str) -> str:
    """A host command printing the pane while `marker_path` exists (nothing if the capture fails), failing otherwise."""
    return (
        f"if [ -e {shlex.quote(str(marker_path))} ]; then {pane_capture_command} 2>/dev/null || true; else exit 1; fi"
    )


@pure
def _is_permission_wait_shown(pane_content: str) -> bool:
    """Whether a pane captured while `permissions_waiting` exists shows something other than the session composer.

    The marker outlives the permission card while the approved tool runs, and the composer is back by then.
    """
    return not is_ready_for_paste(pane_content)


@pure
def _get_ctrl_chord_key(key: str) -> str | None:
    match = _CTRL_CHORD_RE.match(key)
    return None if match is None else match.group("key").lower()


@pure
def is_grok_interrupt_key(key: str) -> bool:
    return _get_ctrl_chord_key(key) == _INTERRUPT_CTRL_KEY


@pure
def _find_gate_screen_key_refusal(gate_screen: GrokGateScreen, key: str, pane_content: str) -> str | None:
    match gate_screen:
        case GrokGateScreen.PERMISSION_CARD:
            if key.lower() in _PERMISSION_CARD_ALWAYS_APPROVE_KEYS:
                return "on a permission card it turns on always-approve for every tool"
            if key.lower() in _ENTER_KEYS and is_always_approve_option_selected(pane_content):
                return (
                    "the permission card's always-approve option is selected, so it turns that mode on for every tool"
                )
            return None
        case GrokGateScreen.TRUST_DIALOG | GrokGateScreen.LOGIN | GrokGateScreen.CONSENT | GrokGateScreen.PAYWALL:
            return f"grok shows its {gate_screen.lower().replace('_', ' ')} screen, where keys act on the screen"
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _find_interrupt_refusal(pane_content: str, is_cancellable_turn_in_flight: bool) -> str | None:
    if has_press_again_hint(pane_content):
        return "grok is waiting for a second Ctrl+C to quit"
    if not is_cancellable_turn_in_flight:
        return "grok is idle (no top-level turn is running), where Ctrl+C arms quit instead of cancelling a turn"
    if not has_cancel_hint(pane_content):
        return "grok's footer shows no Ctrl+c:cancel hint, so the turn may have ended and Ctrl+C would arm quit"
    if composer_holds_text(pane_content):
        return "grok's composer holds text, which Ctrl+C clears instead of cancelling the turn"
    return None


@pure
def find_grok_key_refusal(key: str, pane_content: str, is_cancellable_turn_in_flight: bool) -> str | None:
    """Why mngr must not press `key` into grok now, or None when it may.

    `is_cancellable_turn_in_flight` says whether a top-level turn other than a pending-wake placeholder is in flight.
    """
    ctrl_key = _get_ctrl_chord_key(key)
    if ctrl_key == _ALWAYS_APPROVE_TOGGLE_CTRL_KEY:
        return "Ctrl+O turns grok's always-approve mode on and saves it to the agent's config"
    if key.lower() in _MODE_CYCLE_KEYS:
        return (
            "Shift+Tab cycles grok's permission mode, which can turn always-approve on and save it to the agent's "
            "config"
        )
    if ctrl_key == _QUIT_CTRL_KEY:
        return "Ctrl+Q quits grok (at its second press); `mngr stop` stops the agent"
    if ctrl_key in _SEND_NOW_CTRL_KEYS:
        return "Ctrl+Enter cuts the running turn short to send a queued message"
    gate_screen = detect_gate_screen(pane_content)
    gate_refusal = None if gate_screen is None else _find_gate_screen_key_refusal(gate_screen, key, pane_content)
    if gate_refusal is not None:
        return gate_refusal
    if key.lower() in _ENTER_KEYS and has_send_now_hint(pane_content):
        return "grok's footer shows Enter:send now, where Enter cuts the running turn short to send a queued message"
    if ctrl_key == _INTERRUPT_CTRL_KEY:
        return _find_interrupt_refusal(pane_content, is_cancellable_turn_in_flight)
    return None


@pure
def _build_turn_record_print_command(turn_path: Path) -> str:
    """Prints `turn:<promptId>` for a turn's file, `turn:` once it is gone."""
    return f"printf '{_TURN_RECORD_LABEL}%s\\n' \"$(head -n 1 {shlex.quote(str(turn_path))} 2>/dev/null)\""


@pure
def _read_labelled_values(lines: Sequence[str], label: str) -> list[str]:
    return [line.strip().removeprefix(label) for line in lines if line.strip().startswith(label)]


@pure
def build_cancel_settle_probe_command(
    agent_state_dir: Path, cancelled_turn: GrokRootTurn, pane_capture_command: str
) -> str:
    """One host command printing `turn:<promptId>` for what the turn's file records (`turn:` once it is gone).

    `pane:` and the pane follow.
    """
    turn_read = _build_turn_record_print_command(
        agent_state_dir / ROOT_TURNS_RELATIVE_PATH / cancelled_turn.session_id
    )
    return "; ".join([turn_read, f"echo {_PANE_LABEL}", f"{pane_capture_command} 2>/dev/null || true"])


@pure
def parse_cancel_settle_probe_output(cancelled_turn: GrokRootTurn, stdout: str) -> GrokCancelReading | None:
    """The reading build_cancel_settle_probe_command printed, or None when its output is cut short."""
    lines = stdout.split("\n")
    pane_label_idx = next((idx for idx, line in enumerate(lines) if line.strip() == _PANE_LABEL), None)
    if pane_label_idx is None:
        return None
    recorded_prompt_ids = _read_labelled_values(lines[:pane_label_idx], _TURN_RECORD_LABEL)
    if len(recorded_prompt_ids) != 1:
        return None
    return GrokCancelReading(
        cancelled_turn=cancelled_turn,
        recorded_prompt_id=recorded_prompt_ids[0],
        pane="\n".join(lines[pane_label_idx + 1 :]),
    )


@pure
def is_cancelled_turn_still_recorded(reading: GrokCancelReading) -> bool:
    """Whether the turn's file still records the promptId it was cancelled at."""
    return reading.recorded_prompt_id == reading.cancelled_turn.prompt_id


@pure
def _is_idle_composer_shown(pane: str) -> bool:
    return is_idle_and_empty(pane) or is_idle_holding_text(pane)


@pure
def is_cancel_ended_or_idle(reading: GrokCancelReading) -> bool:
    """Whether grok reported the end of the cancelled turn, or shows an idle composer, empty or holding text."""
    return not is_cancelled_turn_still_recorded(reading) or _is_idle_composer_shown(reading.pane)


@pure
def is_cancelled_turn_dropped(reading: GrokCancelReading) -> bool:
    """Whether the cancelled turn is still recorded while grok shows an idle composer, empty or holding text."""
    return is_cancelled_turn_still_recorded(reading) and _is_idle_composer_shown(reading.pane)


@pure
def build_end_dropped_turn_command(env_prefix: str, agent_state_dir: Path, dropped_turn: GrokRootTurn) -> str:
    """One host command ending the turn through the hook handler's TURN_DROPPED_FLAG, then reading the turn's file.

    It ends by printing `turn:<promptId>` for what the file records, `turn:` once it is gone.
    """
    hook_path = shlex.quote(str(get_grok_home(agent_state_dir) / _HOOKS_DIRNAME / _HOOK_HANDLER_SCRIPT_NAME))
    hook_run = (
        f"{hook_path} {TURN_DROPPED_FLAG} {shlex.quote(dropped_turn.session_id)} {shlex.quote(dropped_turn.prompt_id)}"
    )
    record_print = _build_turn_record_print_command(
        agent_state_dir / ROOT_TURNS_RELATIVE_PATH / dropped_turn.session_id
    )
    return f"{env_prefix}{{ {hook_run}; {record_print}; }}"


@pure
def build_grok_host_paths_command(env_prefix: str, is_agent_definition_listed: bool) -> str:
    """One host command reading what GrokHostPaths holds.

    It prints $HOME, the user's grok home and the credential path one per line, then the agent definitions.
    """
    definition_listing = (
        'for definition in "$HOME/.claude/agents/"*.md "$HOME/.grok/agents/"*.md; do '
        'if [ -f "$definition" ]; then printf \'\\036%s\\037\' "$definition"; cat "$definition"; fi; done'
        if is_agent_definition_listed
        else ":"
    )
    return (
        'if [ -n "${GROK_AGENT:-}" ]; then unset GROK_HOME GROK_AUTH_PATH; fi; '
        + env_prefix
        + '{ printf \'%s\\n%s\\n%s\\n\' "$HOME" "${GROK_HOME:-$HOME/.grok}" '
        + '"${GROK_AUTH_PATH:-${GROK_HOME:-$HOME/.grok}/auth.json}"; '
        + definition_listing
        + "; }"
    )


@pure
def _parse_agent_definition_listing(listing: str) -> dict[str, str]:
    text_by_path: dict[str, str] = {}
    for entry in listing.split(_DEFINITION_PATH_SEPARATOR)[1:]:
        path, _, text = entry.partition(_DEFINITION_CONTENT_SEPARATOR)
        text_by_path[path] = text
    return text_by_path


@pure
def parse_grok_host_paths(command_stdout: str) -> GrokHostPaths:
    lines = command_stdout.split("\n", 3)
    if len(lines) < 3:
        raise GrokProvisioningError(f"Could not read the host's paths for grok: unexpected output {command_stdout!r}")
    home_dir = Path(lines[0])
    user_grok_home = Path(lines[1])
    user_auth_path = Path(lines[2])
    if not user_grok_home.is_absolute():
        raise GrokProvisioningError(
            f"The user's grok home resolves to the relative path {lines[1]!r}; grok keeps no trust store there. "
            "Set GROK_HOME to an absolute path."
        )
    if not home_dir.is_absolute() or not user_auth_path.is_absolute():
        raise GrokProvisioningError(
            f"The host's $HOME ({lines[0]!r}) and grok credential path ({lines[2]!r}) must be absolute"
        )
    return GrokHostPaths(
        home_dir=home_dir,
        user_grok_home=user_grok_home,
        user_auth_path=user_auth_path,
        agent_definition_text_by_path=_parse_agent_definition_listing(lines[3] if len(lines) == 4 else ""),
    )


@pure
def collect_agent_definition_names(agent_definition_text_by_path: Mapping[str, str]) -> list[str]:
    """The names a definition can be selected by: its file stem and its frontmatter `name`, when it has one.

    The frontmatter is read as leniently as Claude Code reads it: a definition's description is often not valid YAML.
    """
    names: set[str] = set()
    for path, text in agent_definition_text_by_path.items():
        names.add(Path(path).stem)
        frontmatter_name = split_skill_frontmatter(text).fields.get("name", "")
        if frontmatter_name:
            names.add(frontmatter_name)
    return sorted(names)


@pure
def parse_claude_enabled_plugin_keys(settings_text: str, settings_path: Path) -> tuple[str, ...]:
    """The keys of `enabledPlugins` in a Claude Code settings.json, e.g. `name@marketplace`."""
    if not settings_text.strip():
        return ()
    try:
        settings = json.loads(settings_text)
    except json.JSONDecodeError as e:
        raise GrokProvisioningError(f"{settings_path} is not valid JSON ({e}); fix it, then re-run") from e
    if not isinstance(settings, dict):
        raise GrokProvisioningError(f"{settings_path} is not a JSON object; fix it, then re-run")
    enabled_plugins = settings.get("enabledPlugins", {})
    if not isinstance(enabled_plugins, dict):
        raise GrokProvisioningError(f"{settings_path} has an `enabledPlugins` that is not an object")
    return tuple(sorted(str(plugin_key) for plugin_key in enabled_plugins))


def read_carried_over_privacy_banner_ack(existing_config: Mapping[str, Any]) -> datetime | None:
    """The `[privacy] privacy_banner_acked` time of an existing per-agent config.toml, when mngr can read it."""
    privacy_table = existing_config.get("privacy", {})
    acked_value = privacy_table.get("privacy_banner_acked") if isinstance(privacy_table, Mapping) else None
    if isinstance(acked_value, datetime):
        acked_at = acked_value
    elif isinstance(acked_value, str):
        try:
            acked_at = datetime.fromisoformat(acked_value)
        except ValueError:
            logger.debug("Not carrying over the unreadable [privacy] privacy_banner_acked {!r}", acked_value)
            return None
    else:
        return None
    return acked_at if acked_at.tzinfo is not None else acked_at.replace(tzinfo=timezone.utc)


def _load_grok_resource_text(filename: str) -> str:
    return importlib.resources.files(grok_resources).joinpath(filename).read_text()


def _read_optional_text_file(host: OnlineHostInterface, path: Path) -> str:
    try:
        return host.read_text_file(path)
    except FileNotFoundError:
        return ""


class GrokAgent(
    InteractiveTuiAgent[GrokAgentConfig],
    CliBackedAgentMixin,
    HasCommonTranscriptMixin,
    HasSessionPreservationMixin,
    HasSessionAdoptionMixin,
    HasUnattendedModeMixin,
    HasPermissionPolicyMixin,
    HasVersionManagementMixin,
    HasAutoInstallMixin,
):
    """Agent implementation for xAI's Grok Build CLI (``grok``)."""

    PASTE_PLACEHOLDER_PREFIXES: ClassVar[tuple[str, ...]] = ("[Pasted: ",)

    cancel_settle_timeout_seconds: float = Field(
        default=_CANCEL_SETTLE_TIMEOUT_SECONDS,
        description="How long a cancelling Ctrl+C waits for grok to end or drop the turn, and for the composer to "
        "empty once mngr clears the prompt grok put back",
    )

    # Launched through grok_launch.sh and the configured command, but the process is always grok.
    fixed_process_name: ClassVar[str | None] = _GROK_BINARY_NAME

    def get_tui_ready_indicator(self) -> Callable[[str], bool]:
        return is_ready_for_paste

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        return build_launch_command(
            self.agent_config.command, self.agent_config.cli_args, agent_args, command_override
        )

    def modify_env_vars(self, host: OnlineHostInterface, env_vars: dict[str, str]) -> None:
        if is_self_update_disabled(self.agent_config.update_policy, is_unattended=not host.is_local):
            env_vars.setdefault(_GROK_DISABLE_AUTOUPDATER_ENV_VAR, "1")

    def wait_for_ready_signal(
        self, is_readiness_awaited: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        """Start grok, then wait for its SessionStart hook and an idle, empty composer; fail fast on a gate screen."""
        with log_span("Starting grok agent {}", self.name):
            super().wait_for_ready_signal(is_readiness_awaited=False, start_action=start_action, timeout=timeout)
        if not is_readiness_awaited:
            return
        ready_timeout = timeout if timeout is not None else self.mngr_ctx.config.agent_ready_timeout
        deadline = time.monotonic() + ready_timeout
        target_arg = self._send_target_arg(self.tmux_target)
        session_started_path = self._get_agent_dir() / SESSION_STARTED_FILENAME
        with log_span("Waiting for grok's SessionStart hook (timeout={}s)", ready_timeout):
            is_session_started = poll_until(
                lambda: self._is_file_present_past_gate_screens(target_arg, session_started_path),
                timeout=ready_timeout,
                poll_interval=_READY_POLL_INTERVAL_SECONDS,
            )
        if not is_session_started:
            self._raise_not_ready(target_arg, f"grok's SessionStart hook did not run within {ready_timeout}s")
        with log_span("Waiting for grok's composer to be idle and empty"):
            is_idle = poll_until(
                lambda: is_idle_and_empty(self._capture_pane_past_gate_screens(target_arg)),
                timeout=max(deadline - time.monotonic(), 0.0),
                poll_interval=_READY_POLL_INTERVAL_SECONDS,
            )
        if not is_idle:
            self._raise_not_ready(target_arg, f"grok's composer was not idle and empty within {ready_timeout}s")

    def _capture_pane_past_gate_screens(self, target_arg: str) -> str:
        pane_content = self._capture_pane_content(target_arg) or ""
        gate_screen = detect_gate_screen(pane_content)
        if gate_screen is not None:
            raise GrokInputBlockedError(
                str(self.name), gate_screen, is_auth_shared=self.agent_config.share_auth, is_at_startup=True
            )
        return pane_content

    def _is_file_present_past_gate_screens(self, target_arg: str, path: Path) -> bool:
        self._capture_pane_past_gate_screens(target_arg)
        return self._check_file_exists(path)

    def _raise_not_ready(self, target_arg: str, reason: str) -> NoReturn:
        logger.error(
            "grok agent {} is not ready; pane content:\n{}", self.name, self._capture_pane_content(target_arg)
        )
        raise AgentStartError(str(self.name), f"{reason} (the pane content is in the log)")

    def _preflight_send_message(self, tmux_target: TmuxWindowTarget | str) -> str | None:
        pane_content = self._capture_pane_content(tmux_target)
        gate_screen = detect_gate_screen(pane_content or "")
        if gate_screen is not None:
            raise GrokInputBlockedError(
                str(self.name), gate_screen, is_auth_shared=self.agent_config.share_auth, is_at_startup=False
            )
        if _is_permission_wait_shown(pane_content or "") and self._check_file_exists(
            self._get_agent_dir() / PERMISSIONS_WAITING_FILENAME
        ):
            raise GrokInputBlockedError(
                str(self.name),
                GrokGateScreen.PERMISSION_CARD,
                is_auth_shared=self.agent_config.share_auth,
                is_at_startup=False,
            )
        return pane_content

    def _read_root_turns(self) -> list[GrokRootTurn]:
        """The top-level turns mngr's hook handler records as in flight, by session id."""
        root_turns: list[GrokRootTurn] = []
        for entry in self.host.list_directory(self._get_agent_dir() / ROOT_TURNS_RELATIVE_PATH):
            entry_path = Path(entry.path)
            if entry.file_type != FileType.FILE or entry_path.name.startswith("."):
                continue
            # A turn that ends between the listing and the read is no longer in flight.
            try:
                turn_text = self.host.read_text_file(entry_path)
            except FileNotFoundError:
                continue
            root_turns.append(parse_root_turn(entry_path.name, turn_text))
        return sorted(root_turns, key=lambda root_turn: root_turn.session_id)

    def _detect_preexisting_input_text(self, pane_content: str) -> str | None:
        if not composer_holds_text(pane_content):
            return None
        return read_composer_text(pane_content) or _UNREADABLE_COMPOSER_TEXT

    def _determine_confirmation_policy(self, message: str) -> SubmissionConfirmationPolicy:
        """Confirm a shell-mode `!` message under the relaxed policy, like a slash command: it fires no grok hook."""
        if is_shell_command_message(message):
            return SubmissionConfirmationPolicy.RELAXED
        return super()._determine_confirmation_policy(message)

    def _build_enter_retry_gate(self, message: str) -> EnterRetryGate | None:
        return EnterRetryGate(condition_command=NonEmptyStr(GROK_ENTER_RETRY_CONDITION))

    def _build_submission_evidence_probes(
        self, message: str, policy: SubmissionConfirmationPolicy
    ) -> Sequence[SubmissionEvidenceProbe]:
        env_prefix = self.host.build_source_env_prefix(self)
        pane_capture_command = build_tmux_capture_pane_command(f'"${PANE_TARGET_SHELL_VARIABLE}"') + " 2>/dev/null"
        message_probes = [
            build_submissions_log_evidence_probe(env_prefix, message),
            build_queued_row_evidence_probe(pane_capture_command, message),
        ]
        match policy:
            case SubmissionConfirmationPolicy.STRICT:
                return message_probes
            case SubmissionConfirmationPolicy.RELAXED:
                return [*message_probes, build_session_id_evidence_probe(env_prefix)]
            case _ as unreachable:
                assert_never(unreachable)

    def press_key_chord(self, key: str) -> None:
        """Press one tmux key into grok, refusing keys that change settings, cut a turn short or quit grok.

        Ctrl+C is pressed only into a top-level turn that a fresh capture shows cancellable with an empty composer. When
        it is the only cancellable top-level turn mngr records, mngr waits for grok to end it, and ends in mngr's records
        a turn grok drops instead.
        """
        with self._message_lock(), log_span("Pressing key chord {} to agent {}", key, self.name):
            is_interrupt = is_grok_interrupt_key(key)
            if is_interrupt:
                self._wait_out_interrupt_spacing(self._read_last_interrupt_time())
            target_arg = self._send_target_arg(self.tmux_target)
            cancellable_turns = (
                tuple(root_turn for root_turn in self._read_root_turns() if not root_turn.is_pending_wake)
                if is_interrupt
                else ()
            )
            # The turn files lag grok's own turn end, so the pane, captured last, decides.
            refusal_reason = find_grok_key_refusal(
                key, self._capture_pane_content(target_arg) or "", len(cancellable_turns) > 0
            )
            if refusal_reason is not None:
                raise GrokKeyRefusedError(str(self.name), key, refusal_reason)
            self._press_into_pane(target_arg, key)
            if is_interrupt:
                interrupt_time = self._record_interrupt()
                # The pane shows one session; with turns in several, mngr cannot tell which one the Ctrl+C cancelled.
                if len(cancellable_turns) == 1:
                    self._settle_cancelled_turn(target_arg, cancellable_turns[0], interrupt_time)

    def _press_into_pane(self, target_arg: str, key: str) -> None:
        self._clear_pane_modes(target_arg)
        self._run_send_command(
            f"tmux send-keys -t {target_arg} {quote_tmux_text_argument(key)}", f"tmux send-keys {key}"
        )

    def _record_interrupt(self) -> float:
        interrupt_time = time.time()
        self.host.write_text_file(self._get_agent_dir() / _LAST_INTERRUPT_RELATIVE_PATH, f"{interrupt_time}\n")
        return interrupt_time

    def _read_last_interrupt_time(self) -> float | None:
        record_path = self._get_agent_dir() / _LAST_INTERRUPT_RELATIVE_PATH
        try:
            record_text = self.host.read_text_file(record_path)
        except FileNotFoundError:
            return None
        try:
            return float(record_text.strip())
        except ValueError:
            logger.warning("Ignoring the unreadable last Ctrl+C record {}: {!r}", record_path, record_text)
            return None

    def _wait_out_interrupt_spacing(self, last_interrupt_time: float | None) -> None:
        if last_interrupt_time is None:
            return
        remaining_seconds = min(
            _INTERRUPT_MIN_INTERVAL_SECONDS - (time.time() - last_interrupt_time), _INTERRUPT_MIN_INTERVAL_SECONDS
        )
        if remaining_seconds <= 0:
            return
        resume_at = time.monotonic() + remaining_seconds
        with log_span("Spacing this Ctrl+C {:.2f}s after the previous one to agent {}", remaining_seconds, self.name):
            poll_until(
                lambda: time.monotonic() >= resume_at,
                timeout=remaining_seconds + _INTERRUPT_MIN_INTERVAL_SECONDS,
                poll_interval=_INTERRUPT_SPACING_POLL_INTERVAL_SECONDS,
            )

    def _settle_cancelled_turn(self, target_arg: str, cancelled_turn: GrokRootTurn, interrupt_time: float) -> None:
        """Wait for grok to end the cancelled turn, and end it in mngr's records when grok drops it instead.

        grok drops a turn cancelled before its first reply: it goes idle without reporting the end, with the prompt
        already back in its composer, which is cleared first. A turn grok first shows idle and empty for is ended as
        dropped at the deadline unless grok reports its end, and nothing typed meanwhile is cleared. A turn grok still
        runs at the deadline is left for grok to end.
        """
        probe_command = build_cancel_settle_probe_command(
            self._get_agent_dir(), cancelled_turn, build_tmux_capture_pane_command(target_arg)
        )
        with log_span("Waiting for grok to end the turn cancelled in agent {}", self.name):
            first_reading, _, elapsed_seconds = poll_for_value(
                lambda: self._read_cancel_when(probe_command, cancelled_turn, is_cancel_ended_or_idle),
                timeout=self.cancel_settle_timeout_seconds,
                poll_interval=_CANCEL_SETTLE_POLL_INTERVAL_SECONDS,
            )
            if first_reading is None:
                self._log_cancelled_turn_left_to_grok()
                return
            if not is_cancelled_turn_still_recorded(first_reading):
                return
            if is_idle_holding_text(first_reading.pane):
                self._log_dropped_turn(cancelled_turn)
                self._clear_restored_prompt(target_arg, interrupt_time)
                self._end_dropped_turn(cancelled_turn)
                return
            ended_reading, _, _ = poll_for_value(
                lambda: self._read_cancel_when(
                    probe_command, cancelled_turn, lambda reading: not is_cancelled_turn_still_recorded(reading)
                ),
                timeout=max(0.0, self.cancel_settle_timeout_seconds - elapsed_seconds),
                poll_interval=_CANCEL_SETTLE_POLL_INTERVAL_SECONDS,
            )
        if ended_reading is not None:
            return
        reading = self._read_cancel(probe_command, cancelled_turn)
        if reading is not None and not is_cancelled_turn_still_recorded(reading):
            return
        if reading is None or not is_cancelled_turn_dropped(reading):
            self._log_cancelled_turn_left_to_grok()
            return
        self._log_dropped_turn(cancelled_turn)
        self._end_dropped_turn(cancelled_turn)

    def _log_cancelled_turn_left_to_grok(self) -> None:
        logger.info(
            "Left the turn cancelled in agent {} for grok to end: grok had not ended it {:g}s after the Ctrl+C",
            self.name,
            self.cancel_settle_timeout_seconds,
        )

    def _log_dropped_turn(self, dropped_turn: GrokRootTurn) -> None:
        logger.info(
            "Ending the cancelled turn {} of agent {} in mngr's records: grok dropped it without reporting its end",
            dropped_turn.prompt_id,
            self.name,
        )

    def _read_cancel(self, probe_command: str, cancelled_turn: GrokRootTurn) -> GrokCancelReading | None:
        """The cancelled turn's record and the pane, or None when the probe fails or its output is cut short."""
        result = self.host.execute_idempotent_command(probe_command, timeout_seconds=_PANE_PROBE_TIMEOUT_SECONDS)
        reading = parse_cancel_settle_probe_output(cancelled_turn, result.stdout) if result.success else None
        if reading is None:
            logger.trace(
                "Could not read the cancelled turn of agent {} (success={}, stderr={!r})",
                self.name,
                result.success,
                result.stderr,
            )
        return reading

    def _read_cancel_when(
        self, probe_command: str, cancelled_turn: GrokRootTurn, condition: Callable[[GrokCancelReading], bool]
    ) -> GrokCancelReading | None:
        """The reading _read_cancel takes, when it meets `condition`."""
        reading = self._read_cancel(probe_command, cancelled_turn)
        return reading if reading is not None and condition(reading) else None

    def _clear_restored_prompt(self, target_arg: str, interrupt_time: float) -> None:
        """Clear what grok put back in its idle composer with one spaced Ctrl+C, and wait for the composer to empty."""
        self._wait_out_interrupt_spacing(interrupt_time)
        pane = self._capture_pane_content(target_arg) or ""
        if is_idle_and_empty(pane):
            return
        if not is_idle_holding_text(pane):
            raise GrokCancelUnsettledError(
                str(self.name),
                "grok dropped it and put its prompt back in its input box, then showed no idle input box when mngr came "
                "to clear it, so mngr cleared nothing and did not end the turn in mngr's records",
            )
        self._press_into_pane(target_arg, "C-c")
        self._record_interrupt()
        is_cleared = poll_until(
            lambda: is_idle_and_empty(self._capture_pane_content(target_arg) or ""),
            timeout=self.cancel_settle_timeout_seconds,
            poll_interval=_CANCEL_SETTLE_POLL_INTERVAL_SECONDS,
        )
        if not is_cleared:
            raise GrokCancelUnsettledError(
                str(self.name),
                "grok dropped it and put its prompt back in its input box, which still held it "
                f"{self.cancel_settle_timeout_seconds:g}s after the Ctrl+C that clears it; "
                f"{_DROPPED_TURN_LEFT_RECORDED}",
            )

    def _end_dropped_turn(self, dropped_turn: GrokRootTurn) -> None:
        """End a turn grok dropped in mngr's records, under the hook handler's state lock, which recomputes `active`."""
        result = self.host.execute_idempotent_command(
            build_end_dropped_turn_command(
                self.host.build_source_env_prefix(self), self._get_agent_dir(), dropped_turn
            ),
            timeout_seconds=_HOST_COMMAND_TIMEOUT_SECONDS,
        )
        recorded_prompt_ids = _read_labelled_values(result.stdout.split("\n"), _TURN_RECORD_LABEL)
        is_turn_ended = (
            result.success and len(recorded_prompt_ids) > 0 and recorded_prompt_ids[-1] != dropped_turn.prompt_id
        )
        if not is_turn_ended:
            raise GrokCancelUnsettledError(
                str(self.name),
                "grok dropped it without reporting its end, and mngr's hook handler did not end it in mngr's records "
                f"(see $MNGR_AGENT_STATE_DIR/logs/grok_hooks.log; stderr={result.stderr!r}); "
                f"{_DROPPED_TURN_LEFT_RECORDED}",
            )

    def is_blocked_on_dialog(self) -> bool:
        """Whether mngr's hook recorded a permission request and the pane does not show the session composer."""
        pane_capture_command = build_tmux_capture_pane_command(self._send_target_arg(self.tmux_target))
        result = self.host.execute_idempotent_command(
            _build_permission_wait_capture_command(
                self._get_agent_dir() / PERMISSIONS_WAITING_FILENAME, pane_capture_command
            ),
            timeout_seconds=_PANE_PROBE_TIMEOUT_SECONDS,
        )
        return result.success and _is_permission_wait_shown(result.stdout)

    @property
    def is_common_transcript_enabled(self) -> bool:
        return self.agent_config.emit_common_transcript

    def get_raw_transcript_scripts(self) -> Mapping[str, str]:
        return {name: _load_grok_resource_text(name) for name in RAW_TRANSCRIPT_SCRIPT_NAMES}

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        return {name: _load_grok_resource_text(name) for name in COMMON_TRANSCRIPT_SCRIPT_NAMES}

    def is_unattended_enabled(self) -> bool:
        return self.agent_config.auto_allow_permissions

    def get_permission_policy(self) -> Mapping[str, Any]:
        policy = fold_settings_overrides_onto_empty_config(self.agent_config.settings_overrides).get("permission", {})
        return policy if isinstance(policy, Mapping) else {}

    def get_install_binary_name(self) -> str:
        return _GROK_BINARY_NAME

    def get_install_command(self) -> str:
        version = self.agent_config.version
        package = f"{_GROK_NPM_PACKAGE}@{version}" if version is not None else _GROK_NPM_PACKAGE
        npm_install = f"env -u GROK_HOME npm install -g {shlex.quote(package)}"
        # The package's postinstall exits 0 even when it fails to place the native binary.
        return f'{npm_install} && "$(npm prefix -g)/bin/{_GROK_BINARY_NAME}" --version >/dev/null'

    def reconcile_installed_version(self, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
        if self.agent_config.version is None:
            return
        verify_pinned_cli_version(
            host,
            binary_name=self.get_install_binary_name(),
            pinned_version=self.agent_config.version,
            mismatch_policy=self.agent_config.version_mismatch,
        )

    def provision(self, host: OnlineHostInterface, options: CreateAgentOptions, mngr_ctx: MngrContext) -> None:
        """Install and check grok, settle folder trust, render the per-agent grok home and install the scripts."""
        if self.agent_config.check_installation:
            ensure_cli_installed(host, mngr_ctx, self.get_install_binary_name(), self.get_install_command())
            self._verify_installed_grok_is_grok_build(host)
        self.reconcile_installed_version(host, mngr_ctx)
        env_prefix = host.build_source_env_prefix(self)
        host_paths = self._resolve_grok_host_paths(host, env_prefix)
        ensure_grok_folder_trust(
            host,
            work_dir=Path(self.work_dir),
            env_prefix=env_prefix,
            user_grok_home=host_paths.user_grok_home,
            agent_grok_home=get_grok_home(self._get_agent_dir()),
            is_auto_dismiss_dialogs_at_startup=self.agent_config.auto_dismiss_dialogs_at_startup,
            is_auto_approve=mngr_ctx.is_auto_approve,
            is_interactive=mngr_ctx.is_interactive,
            prompt_user_to_trust=self._prompt_user_to_trust_grok_folder,
        )
        self._provision_grok_state(host, host_paths, mngr_ctx)
        with mngr_ctx.concurrency_group.make_concurrency_group("grok_provisioning") as concurrency_group:
            provision_raw_transcript_scripts(self, host, self._get_agent_dir(), concurrency_group)
            maybe_provision_common_transcript_scripts(self, host, self._get_agent_dir(), concurrency_group)
            provision_scripts_to_commands_dir(
                host,
                self._get_agent_dir(),
                {
                    LAUNCH_SCRIPT_NAME: _load_grok_resource_text(LAUNCH_SCRIPT_NAME),
                    _BACKGROUND_TASKS_SCRIPT_NAME: _load_grok_resource_text(_BACKGROUND_TASKS_SCRIPT_NAME),
                },
                concurrency_group,
            )

    def _verify_installed_grok_is_grok_build(self, host: OnlineHostInterface) -> None:
        """Raise when `grok --version` prints something other than Grok Build's version line."""
        binary_name = self.get_install_binary_name()
        result = host.execute_idempotent_command(
            f"{shlex.quote(binary_name)} --version", timeout_seconds=_HOST_COMMAND_TIMEOUT_SECONDS
        )
        version_output = f"{result.stdout}\n{result.stderr}".strip() if result.success else ""
        if version_output == "":
            logger.debug("Could not read `{} --version`; skipping the Grok Build check", binary_name)
            return
        if _GROK_BUILD_VERSION_LINE_RE.search(version_output) is not None:
            return
        raise AgentInstallationError(
            f"`{binary_name} --version` reported {version_output!r}, which is not xAI's Grok Build (it prints "
            f"`grok X.Y.Z (<build hash>)`). Another program named {binary_name}, such as the community grok-cli, "
            "comes first on the host's PATH: uninstall it, or set `check_installation = false` when `command` "
            "already runs Grok Build."
        )

    def _prompt_user_to_trust_grok_folder(self, trust_key: Path, user_store_path: Path) -> bool:
        return prompt_user_to_trust_grok_folder(trust_key, user_store_path)

    def _resolve_grok_host_paths(self, host: OnlineHostInterface, env_prefix: str) -> GrokHostPaths:
        command = build_grok_host_paths_command(
            env_prefix, is_agent_definition_listed=self.agent_config.isolate_from_claude_config
        )
        result = host.execute_idempotent_command(command, timeout_seconds=_HOST_COMMAND_TIMEOUT_SECONDS)
        if not result.success:
            raise GrokProvisioningError(
                f"Could not read the host's paths for grok (stdout={result.stdout!r}, stderr={result.stderr!r})"
            )
        return parse_grok_host_paths(result.stdout)

    def _build_role_prompt_text(self, host: OnlineHostInterface) -> str:
        blocks = [str(block) for block in self.agent_config.append_system_prompt]
        if self.agent_config.output_style is not None:
            styles_dir = Path(self.work_dir) / _OUTPUT_STYLES_RELATIVE_PATH
            blocks.append(
                resolve_output_style(self.agent_config.output_style, read_output_style_files(host, styles_dir))
            )
        role_text = _ROLE_BLOCK_SEPARATOR.join(blocks)
        role_size = len(role_text.encode("utf-8"))
        if role_size > _ROLE_PROMPT_MAX_BYTES:
            raise UserInputError(
                f"The role for grok agent {self.name} (its append_system_prompt blocks and output style) is "
                f"{role_size} bytes, over grok's limit of {_ROLE_PROMPT_MAX_BYTES}; shorten it."
            )
        return role_text if role_text.strip() else ""

    def _build_grok_config(
        self, host: OnlineHostInterface, host_paths: GrokHostPaths, mngr_ctx: MngrContext
    ) -> RenderedGrokConfig:
        isolation = self.agent_config.isolate_from_claude_config
        existing_config_text = _read_optional_text_file(host, get_grok_home(self._get_agent_dir()) / _CONFIG_FILENAME)
        rendered_model_settings_path = self._get_agent_dir() / RENDERED_MODEL_SETTINGS_RELATIVE_PATH
        previously_rendered_model_settings = parse_rendered_model_settings(
            _read_optional_text_file(host, rendered_model_settings_path), str(rendered_model_settings_path)
        )
        user_config_text = (
            _read_optional_text_file(host, host_paths.user_auth_path.parent / _CONFIG_FILENAME)
            if self.agent_config.share_auth
            else ""
        )
        claude_settings_path = host_paths.home_dir / ".claude" / "settings.json"
        enabled_claude_plugin_keys = (
            ()
            if isolation
            else parse_claude_enabled_plugin_keys(
                _read_optional_text_file(host, claude_settings_path), claude_settings_path
            )
        )
        existing_config = parse_grok_config_text(existing_config_text, "The agent's grok config.toml")
        return build_config_toml(
            model=self.agent_config.model,
            reasoning_effort=self.agent_config.reasoning_effort,
            is_auto_allow_permissions=self.agent_config.auto_allow_permissions,
            is_self_update_disabled=is_self_update_disabled(
                self.agent_config.update_policy, is_unattended=not host.is_local
            ),
            is_privacy_pinned=self.agent_config.pin_privacy_settings,
            is_memory_enabled=self.agent_config.memory,
            is_isolated_from_claude_config=isolation,
            subagent_names_to_disable=collect_agent_definition_names(host_paths.agent_definition_text_by_path),
            enabled_claude_plugin_keys=enabled_claude_plugin_keys,
            privacy_banner_acked_at=read_carried_over_privacy_banner_ack(existing_config)
            or datetime.now(timezone.utc),
            existing_agent_config_text=existing_config_text,
            user_config_text=user_config_text,
            settings_overrides=self.agent_config.settings_overrides,
            is_settings_narrowing_allowed=mngr_ctx.config.allow_settings_key_assignment_narrowing,
            previously_rendered_model_settings=previously_rendered_model_settings,
        )

    def _provision_grok_state(
        self, host: OnlineHostInterface, host_paths: GrokHostPaths, mngr_ctx: MngrContext
    ) -> None:
        """Render the per-agent grok home and the launch state under plugin/grok; reruns leave the same files."""
        agent_dir = self._get_agent_dir()
        grok_home = get_grok_home(agent_dir)
        rendered_config = self._build_grok_config(host, host_paths, mngr_ctx)
        requirements_text = build_requirements_toml(self.agent_config.pin_privacy_settings)
        role_text = self._build_role_prompt_text(host)
        recorded_session_id = _read_optional_text_file(host, agent_dir / SESSION_ID_RELATIVE_PATH).strip()

        # One host command makes the directories and removes the files this provision does not write.
        stale_paths = [
            path
            for path, is_stale in (
                (grok_home / _REQUIREMENTS_FILENAME, requirements_text == ""),
                (agent_dir / ROLE_PROMPT_RELATIVE_PATH, role_text == ""),
            )
            if is_stale
        ]
        directories = (
            grok_home / _HOOKS_DIRNAME,
            agent_dir / GROK_SESSIONS_RELATIVE_PATH,
            agent_dir / GROK_PLUGIN_STATE_RELATIVE_PATH,
        )
        prepare_command = "mkdir -p " + " ".join(shlex.quote(str(directory)) for directory in directories)
        if stale_paths:
            prepare_command += " && rm -f " + " ".join(shlex.quote(str(path)) for path in stale_paths)
        result = host.execute_idempotent_command(prepare_command, timeout_seconds=_HOST_COMMAND_TIMEOUT_SECONDS)
        if not result.success:
            raise GrokProvisioningError(f"Could not prepare {grok_home} on the host (stderr={result.stderr!r})")

        with log_span("Writing the grok home of agent {} at {}", self.name, grok_home):
            host.write_text_file(
                grok_home / _CONFIG_FILENAME, render_toml(rendered_config.body), mode=_PRIVATE_FILE_MODE
            )
            host.write_text_file(
                agent_dir / RENDERED_MODEL_SETTINGS_RELATIVE_PATH,
                render_rendered_model_settings(rendered_config.own_model_settings),
            )
            if requirements_text:
                host.write_text_file(grok_home / _REQUIREMENTS_FILENAME, requirements_text)
            host.write_text_file(
                grok_home / _HOOKS_DIRNAME / _HOOKS_CONFIG_FILENAME, json.dumps(build_hooks_json(), indent=2) + "\n"
            )
            host.write_file(
                grok_home / _HOOKS_DIRNAME / _HOOK_HANDLER_SCRIPT_NAME,
                _load_grok_resource_text(_HOOK_HANDLER_SCRIPT_NAME).encode(),
                mode=_EXECUTABLE_FILE_MODE,
            )
            host.write_text_file(
                agent_dir / LAUNCH_ENV_RELATIVE_PATH,
                build_launch_env(
                    grok_home=grok_home,
                    work_dir=Path(self.work_dir),
                    shared_user_auth_path=host_paths.user_auth_path if self.agent_config.share_auth else None,
                    model=self.agent_config.model,
                    reasoning_effort=self.agent_config.reasoning_effort,
                    is_privacy_pinned=self.agent_config.pin_privacy_settings,
                    is_isolated_from_claude_config=self.agent_config.isolate_from_claude_config,
                ),
            )
            if role_text:
                host.write_text_file(agent_dir / ROLE_PROMPT_RELATIVE_PATH, role_text)
            if not recorded_session_id:
                host.write_text_file(agent_dir / SESSION_ID_RELATIVE_PATH, f"{self.id.get_uuid()}\n")

    def on_after_provisioning(
        self, host: OnlineHostInterface, options: CreateAgentOptions, mngr_ctx: MngrContext
    ) -> None:
        self.adopt_session(host, options, mngr_ctx)

    def adopt_session(self, host: OnlineHostInterface, options: CreateAgentOptions, mngr_ctx: MngrContext) -> None:
        """Copy the `--adopt` sessions and the `--from` source's session in; the first launch forks the resumed one."""
        if not options.adopt_session and options.source_agent_state_location is None:
            return
        canonical_work_dir = resolve_canonical_work_dir(host, Path(self.work_dir))
        adopt_sessions(
            options.adopt_session,
            options.source_agent_state_location,
            copy_explicit=lambda adopt_arg: self._copy_adopted_session(host, mngr_ctx, canonical_work_dir, adopt_arg),
            copy_clone=lambda source_location: self._copy_cloned_session(host, canonical_work_dir, source_location),
            resume=lambda session_id: record_grok_fork_source(host, self._get_agent_dir(), session_id),
        )

    def _copy_adopted_session(
        self, host: OnlineHostInterface, mngr_ctx: MngrContext, canonical_work_dir: str, adopt_arg: str
    ) -> str:
        source = resolve_grok_adopt_session(adopt_arg, mngr_ctx, get_local_user_grok_home())
        session_id = copy_adopted_grok_session(
            host, get_local_host(mngr_ctx), self._get_agent_dir(), canonical_work_dir, source
        )
        logger.info("Adopted grok session {} into agent {}", session_id, self.name)
        own_role_text = _read_optional_text_file(host, self._get_agent_dir() / ROLE_PROMPT_RELATIVE_PATH)
        if own_role_text:
            logger.info(
                "Agent {} continues grok session {} with the role that session started with; its own role ({}) "
                "applies to the sessions it starts later",
                self.name,
                session_id,
                describe_grok_role(own_role_text),
            )
        return session_id

    def _copy_cloned_session(
        self, host: OnlineHostInterface, canonical_work_dir: str, source_location: HostLocation
    ) -> str | None:
        session_id = copy_cloned_grok_session(host, self._get_agent_dir(), canonical_work_dir, source_location)
        if session_id is None:
            return None
        logger.info("Cloned grok session {} from {} into agent {}", session_id, source_location.path, self.name)
        source_role_text = _read_optional_text_file(
            source_location.host, source_location.path / ROLE_PROMPT_RELATIVE_PATH
        )
        own_role_text = _read_optional_text_file(host, self._get_agent_dir() / ROLE_PROMPT_RELATIVE_PATH)
        if source_role_text != own_role_text:
            logger.info(
                "Agent {} continues grok session {} with the source's role ({}); its own role ({}) applies to the "
                "sessions it starts later",
                self.name,
                session_id,
                describe_grok_role(source_role_text),
                describe_grok_role(own_role_text),
            )
        return session_id

    def preserve_session_state(self, host: OnlineHostInterface) -> None:
        preserve_agent_state(build_grok_preserved_items(is_memory_enabled=self.agent_config.memory), self, host)

    def on_destroy(self, host: OnlineHostInterface) -> None:
        if self.agent_config.preserve_on_destroy:
            self.preserve_session_state(host)


def _grok_items_to_preserve_for_discovered_agent(ref: DiscoveredAgent) -> Sequence[PreservedItem] | None:
    agent_config = ref.certified_data.get("agent_config", {})
    is_memory_enabled = isinstance(agent_config, Mapping) and agent_config.get("memory") is True
    return flag_gated_items(
        ref, "preserve_on_destroy", build_grok_preserved_items(is_memory_enabled=is_memory_enabled)
    )


def _waiting_reason(agent: AgentInterface, host: OnlineHostInterface) -> WaitingReason | None:
    if not isinstance(agent, GrokAgent):
        return None
    is_active = host.path_exists(get_agent_state_dir_path(host.host_dir, agent.id) / ACTIVE_MARKER_FILENAME)
    return classify_waiting_reason(is_active, is_active and agent.is_blocked_on_dialog())


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the grok agent type."""
    return (_GROK_AGENT_TYPE_NAME, GrokAgent, GrokAgentConfig)


@hookimpl
def agent_field_generators() -> tuple[str, dict[str, Callable[[AgentInterface, OnlineHostInterface], Any]]] | None:
    """Expose why a grok agent is waiting."""
    return (_GROK_AGENT_TYPE_NAME, {"waiting_reason": _waiting_reason})


@hookimpl
def on_before_create(args: OnBeforeCreateArgs, mngr_ctx: MngrContext) -> OnBeforeCreateArgs | None:
    """Resolve every `--adopt` grok session before any host or work dir is built."""
    run_adopt_session_preflight(
        args.agent_options.agent_type,
        args.agent_options.adopt_session,
        mngr_ctx,
        GrokAgent,
        lambda adopt_arg: resolve_grok_adopt_session(adopt_arg, mngr_ctx, get_local_user_grok_home()),
    )
    return None


@hookimpl
def on_before_host_destroy(host: HostInterface, mngr_ctx: MngrContext) -> None:
    """Preserve grok agents' transcripts and sessions from the volume of a host destroyed without its agents."""
    preserve_host_agents_on_destroy(
        host, mngr_ctx, AgentTypeName(_GROK_AGENT_TYPE_NAME), _grok_items_to_preserve_for_discovered_agent
    )

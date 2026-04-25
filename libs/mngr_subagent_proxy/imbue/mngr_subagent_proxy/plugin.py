from __future__ import annotations

import importlib.resources
import json
import shlex
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_claude.claude_config import SESSION_GUARD
from imbue.mngr_claude.claude_config import merge_hooks_config
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_subagent_proxy import hookimpl
from imbue.mngr_subagent_proxy import resources as _subagent_proxy_resources

_AGENT_DEFINITION: Final[str] = "mngr-proxy.agent.md"

_SPAWN_MODULE: Final[str] = "imbue.mngr_subagent_proxy.hooks.spawn"
_REWRITE_MODULE: Final[str] = "imbue.mngr_subagent_proxy.hooks.rewrite"
_REAP_MODULE: Final[str] = "imbue.mngr_subagent_proxy.hooks.reap"


def _load_resource(filename: str) -> str:
    """Load a text resource from the subagent-proxy resources package."""
    resource_files = importlib.resources.files(_subagent_proxy_resources)
    return resource_files.joinpath(filename).read_text()


def _python_hook_command(module: str) -> str:
    """Build the shell-command form Claude Code expects, delegating to a Python module."""
    return SESSION_GUARD + f"exec uv run python -m {module}"


def build_subagent_proxy_hooks_config() -> dict[str, Any]:
    """Build the hooks configuration that routes Claude subagents through mngr.

    - PreToolUse (Agent): spawn the mngr proxy subagent instead of Claude's
      native nested Agent loop.
    - PostToolUse (Agent): rewrite the proxy's result before Claude sees it.
    - SessionStart: reap orphaned proxy subagents from prior sessions.
    """
    spawn_cmd = _python_hook_command(_SPAWN_MODULE)
    rewrite_cmd = _python_hook_command(_REWRITE_MODULE)
    reap_cmd = _python_hook_command(_REAP_MODULE)
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Agent",
                    "hooks": [
                        {
                            "type": "command",
                            "command": spawn_cmd,
                            "timeout": 15,
                        },
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "Agent",
                    "hooks": [
                        {
                            "type": "command",
                            "command": rewrite_cmd,
                            "timeout": 15,
                        },
                    ],
                }
            ],
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": reap_cmd,
                        },
                    ],
                }
            ],
        }
    }


def _write_proxy_agent_definition(host: OnlineHostInterface, work_dir: Path) -> None:
    """Write the mngr-proxy subagent definition under the agent's .claude/agents/."""
    agents_dir = work_dir / ".claude" / "agents"
    host.execute_idempotent_command(f"mkdir -p {shlex.quote(str(agents_dir))}", timeout_seconds=5.0)
    content = _load_resource(_AGENT_DEFINITION)
    host.write_text_file(agents_dir / "mngr-proxy.md", content)


def _merge_subagent_proxy_hooks(host: OnlineHostInterface, work_dir: Path) -> None:
    """Merge the subagent-proxy hooks into the agent's .claude/settings.local.json."""
    settings_path = work_dir / ".claude" / "settings.local.json"
    existing_settings: dict[str, Any] = {}
    try:
        content = host.read_text_file(settings_path)
        existing_settings = json.loads(content)
    except FileNotFoundError:
        pass

    hooks_config = build_subagent_proxy_hooks_config()
    merged = merge_hooks_config(existing_settings, hooks_config)
    if merged is None:
        logger.debug("Subagent-proxy hooks already configured in {}", settings_path)
        return
    host.write_text_file(settings_path, json.dumps(merged, indent=2) + "\n")


_SUBAGENT_NAME_INFIX: Final[str] = "--subagent-"


def _is_subagent_proxy_child(agent: AgentInterface) -> bool:
    """Return True if this agent was spawned by the subagent-proxy hook.

    We mint proxy-child names as ``<parent>--subagent-<slug>-<tid>``; use
    that pattern as the signal. Conservative: the user could theoretically
    name a top-level agent with ``--subagent-`` in it and hit this by
    accident, in which case provisioning would refuse to proceed if the
    agent inherits user-configured Stop/SubagentStop hooks (see
    ``_check_subagent_hooks_compat``) and would strip any other
    non-mngr hooks from the spawned subagent's settings.
    """
    return _SUBAGENT_NAME_INFIX in str(agent.name)


class UnsupportedSubagentHookError(NotImplementedError):
    """A spawned subagent inherits Stop/SubagentStop hooks we don't know how to translate.

    Top-level-vs-subagent hook semantics differ (e.g. parent ``Stop`` hooks
    often re-prompt the agent and would prevent the subagent from ever
    ending its turn; a user's ``SubagentStop`` hook might be the one that
    actually wants to fire when the mngr subagent completes its work).
    Rather than guess wrong, refuse to proceed and make the user decide.
    """


# Substrings that mark a hook command as mngr-managed (readiness,
# credential sync, subagent-proxy, wait pipeline). Anything whose command
# doesn't contain one of these is treated as a user-configured hook --
# i.e. a regular hook whose top-level-vs-subagent semantics we don't
# know how to reason about, and which gets stripped from the spawned
# subagent's settings.
_MNGR_MANAGED_HOOK_MARKERS: Final[tuple[str, ...]] = (
    "$MNGR_AGENT_STATE_DIR",
    "imbue.mngr_subagent_proxy.hooks.",
    "sync_keychain_credentials.py",
    "wait_for_stop_hook.sh",
)


def _is_known_safe_hook(hook_entry: dict[str, Any]) -> bool:
    """Return True if every command in the hook entry is recognized as safe."""
    inner = hook_entry.get("hooks")
    if not isinstance(inner, list) or not inner:
        return False
    for cmd_entry in inner:
        if not isinstance(cmd_entry, dict):
            return False
        command = cmd_entry.get("command")
        if not isinstance(command, str):
            return False
        if not any(marker in command for marker in _MNGR_MANAGED_HOOK_MARKERS):
            return False
    return True


def _check_subagent_hooks_compat(host: OnlineHostInterface, agent: AgentInterface) -> None:
    """Refuse to provision a subagent-proxy child whose inherited Stop/SubagentStop hooks
    need custom translation between top-level and subagent semantics.

    We recognize the baseline mngr_claude readiness hook and let it through;
    anything else is a user-configured hook whose intended scope we don't
    know (should it run once per subagent turn? once per outer turn? not at
    all?). Fail loudly rather than silently strip or silently duplicate.
    """
    settings_path = agent.work_dir / ".claude" / "settings.local.json"
    try:
        content = host.read_text_file(settings_path)
    except FileNotFoundError:
        return
    try:
        settings: dict[str, Any] = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Could not parse settings.local.json at {}; assuming no stop hooks", settings_path)
        return
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event_name in ("Stop", "SubagentStop"):
        entries = hooks.get(event_name)
        if not isinstance(entries, list):
            continue
        unsafe = [e for e in entries if isinstance(e, dict) and not _is_known_safe_hook(e)]
        if unsafe:
            raise UnsupportedSubagentHookError(
                f"Spawned mngr subagent {agent.name!r} inherits {len(unsafe)} "
                f"{event_name} hook(s) whose top-level-vs-subagent semantics "
                f"are ambiguous. mngr_subagent_proxy does not yet know how "
                f"to translate these between the parent's scope and a "
                f"spawned subagent's scope. Review each hook: if it should "
                f"fire per subagent turn, install it there directly; if it "
                f"should only fire at the outer end_turn, narrow its "
                f"matcher. Offending settings path: {settings_path}"
            )


@hookimpl(trylast=True)
def on_after_provisioning(agent: AgentInterface, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
    """Install subagent-proxy hooks on Claude agents.

    Runs trylast so mngr_claude's provisioning (which writes the base
    settings.local.json) has already completed. For agents we recognize
    as our own spawned proxy-children, refuse to proceed if they inherit
    any Stop / SubagentStop hooks whose semantics differ between top-level
    and subagent contexts -- the user has to decide how those should apply.
    """
    del mngr_ctx  # unused
    if not isinstance(agent.agent_config, ClaudeAgentConfig):
        return

    _write_proxy_agent_definition(host, agent.work_dir)
    _merge_subagent_proxy_hooks(host, agent.work_dir)

    if _is_subagent_proxy_child(agent):
        _check_subagent_hooks_compat(host, agent)
        _strip_user_hooks_from_subagent(host, agent.work_dir)


def _strip_user_hooks_from_subagent(host: OnlineHostInterface, work_dir: Path) -> None:
    """Strip non-mngr user-configured hooks from the spawned subagent's settings.

    A spawned mngr subagent inherits the full settings.local.json from
    the source repo at create time, which typically includes whatever
    hooks the user has configured on the parent (PreToolUse filters,
    PostToolUse notifications, etc.). Their top-level-vs-subagent
    semantics are ambiguous, so drop everything that isn't recognized
    as mngr-managed. The Stop/SubagentStop compat check already
    rejected unsafe ones before we got here, so anything still present
    in those events must be mngr baseline; the loop below simply
    filters again for uniformity.
    """
    settings_path = work_dir / ".claude" / "settings.local.json"
    try:
        content = host.read_text_file(settings_path)
    except FileNotFoundError:
        return
    try:
        settings: dict[str, Any] = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Could not parse settings.local.json at {}; not stripping user hooks", settings_path)
        return
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return

    stripped_any = False
    for event_name in list(hooks.keys()):
        entries = hooks.get(event_name)
        if not isinstance(entries, list):
            continue
        filtered = [e for e in entries if isinstance(e, dict) and _is_known_safe_hook(e)]
        if len(filtered) != len(entries):
            stripped_any = True
        if filtered:
            hooks[event_name] = filtered
        else:
            hooks.pop(event_name)

    if not stripped_any:
        return
    logger.info("Stripped user-configured hooks from spawned subagent settings at {}", settings_path)
    host.write_text_file(settings_path, json.dumps(settings, indent=2) + "\n")

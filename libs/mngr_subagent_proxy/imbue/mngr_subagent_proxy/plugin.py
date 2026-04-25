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
    accident, but it's exceedingly unlikely and the consequence is just
    "Stop hooks are skipped" -- not a correctness hazard.
    """
    return _SUBAGENT_NAME_INFIX in str(agent.name)


def _strip_stop_hooks(host: OnlineHostInterface, work_dir: Path) -> None:
    """Remove Stop and SubagentStop hooks from the agent's settings.local.json.

    Native Claude Code Task subagents never fire Stop hooks (the parent's
    Stop hook runs at the outer end_turn, not per nested subagent). Our
    mngr-managed proxy subagents should match that behavior, because
    user-configured Stop hooks (e.g. imbue-code-guardian's
    stop_hook_orchestrator.sh) often re-prompt the agent with
    ``"Stop hook feedback: ..."``, which in a spawned subagent context
    means the subagent never actually ends its turn and
    ``subagent_wait`` hangs indefinitely.
    """
    settings_path = work_dir / ".claude" / "settings.local.json"
    try:
        content = host.read_text_file(settings_path)
    except FileNotFoundError:
        return
    try:
        settings: dict[str, Any] = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Could not parse settings.local.json at {}; not stripping Stop hooks", settings_path)
        return
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    removed: list[str] = []
    for event_name in ("Stop", "SubagentStop"):
        if event_name in hooks:
            hooks.pop(event_name)
            removed.append(event_name)
    if not removed:
        return
    logger.info("Stripped {} hooks from spawned subagent {}", removed, settings_path)
    host.write_text_file(settings_path, json.dumps(settings, indent=2) + "\n")


@hookimpl(trylast=True)
def on_after_provisioning(agent: AgentInterface, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
    """Install subagent-proxy hooks on Claude agents.

    Runs trylast so mngr_claude's provisioning (which writes the base
    settings.local.json) has already completed. For agents we recognize
    as our own spawned proxy-children, strip Stop / SubagentStop hooks
    so the subagent matches native-Task end-of-turn semantics rather
    than getting pulled back into a user-configured post-turn ritual.
    """
    del mngr_ctx  # unused
    if not isinstance(agent.agent_config, ClaudeAgentConfig):
        return

    _write_proxy_agent_definition(host, agent.work_dir)
    _merge_subagent_proxy_hooks(host, agent.work_dir)

    if _is_subagent_proxy_child(agent):
        _strip_stop_hooks(host, agent.work_dir)

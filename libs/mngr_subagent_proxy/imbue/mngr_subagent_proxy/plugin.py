from __future__ import annotations

import importlib.resources
import json
import shlex
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_claude.claude_config import merge_hooks_config
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_subagent_proxy import hookimpl
from imbue.mngr_subagent_proxy import resources as _subagent_proxy_resources

# Guard prefix for hook commands: exit gracefully if this is not the main Claude
# session (e.g. a reviewer sub-agent that resumed a session). Mirrors the guard
# used by mngr_claude so the hooks stay inert in nested sessions.
_SESSION_GUARD: Final[str] = '[ -z "$MAIN_CLAUDE_SESSION_ID" ] && exit 0; '

_SPAWN_SCRIPT: Final[str] = "spawn_proxy_subagent.sh"
_REWRITE_SCRIPT: Final[str] = "rewrite_subagent_result.sh"
_REAP_SCRIPT: Final[str] = "reap_orphan_subagents.sh"
_AGENT_DEFINITION: Final[str] = "mngr-proxy.agent.md"

_SUBAGENT_SCRIPTS: Final[tuple[str, ...]] = (_SPAWN_SCRIPT, _REWRITE_SCRIPT, _REAP_SCRIPT)


def _load_resource(filename: str) -> str:
    """Load a text resource from the subagent-proxy resources package."""
    resource_files = importlib.resources.files(_subagent_proxy_resources)
    return resource_files.joinpath(filename).read_text()


def build_subagent_proxy_hooks_config() -> dict[str, Any]:
    """Build the hooks configuration that routes Claude subagents through mngr.

    - PreToolUse (Agent): spawn the mngr proxy subagent instead of Claude's
      native nested Agent loop.
    - PostToolUse (Agent): rewrite the proxy's result before Claude sees it.
    - SessionStart: reap orphaned proxy subagents from prior sessions.
    """
    spawn_cmd = _SESSION_GUARD + f'bash "$MNGR_AGENT_STATE_DIR/commands/{_SPAWN_SCRIPT}"'
    rewrite_cmd = _SESSION_GUARD + f'bash "$MNGR_AGENT_STATE_DIR/commands/{_REWRITE_SCRIPT}"'
    reap_cmd = _SESSION_GUARD + f'bash "$MNGR_AGENT_STATE_DIR/commands/{_REAP_SCRIPT}"'
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


def _provision_subagent_proxy_scripts(host: OnlineHostInterface, agent_state_dir: Path) -> None:
    """Write the subagent-proxy hook scripts to $MNGR_AGENT_STATE_DIR/commands/."""
    commands_dir = agent_state_dir / "commands"
    host.execute_idempotent_command(f"mkdir -p {shlex.quote(str(commands_dir))}", timeout_seconds=5.0)
    for script_name in _SUBAGENT_SCRIPTS:
        script_content = _load_resource(script_name)
        host.write_file(commands_dir / script_name, script_content.encode(), "0755")


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


@hookimpl(trylast=True)
def on_after_provisioning(agent: AgentInterface, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
    """Install subagent-proxy hooks and helper scripts on Claude agents.

    Runs trylast so mngr_claude's provisioning (which writes the base
    settings.local.json and command scripts) has already completed.
    """
    del mngr_ctx  # unused
    if not isinstance(agent.agent_config, ClaudeAgentConfig):
        return

    agent_state_dir = get_agent_state_dir_path(host.host_dir, agent.id)
    _provision_subagent_proxy_scripts(host, agent_state_dir)
    _write_proxy_agent_definition(host, agent.work_dir)
    _merge_subagent_proxy_hooks(host, agent.work_dir)

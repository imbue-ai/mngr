from __future__ import annotations

import importlib.resources
import json
import os
import shlex
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.host_dir import read_default_host_dir
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.host import get_agent_state_dir_path
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentName
from imbue.mngr_claude.claude_config import SESSION_GUARD
from imbue.mngr_claude.claude_config import build_permission_auto_allow_hooks_config
from imbue.mngr_claude.claude_config import get_user_claude_config_dir
from imbue.mngr_claude.claude_config import merge_hooks_config
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_subagent_proxy import hookimpl
from imbue.mngr_subagent_proxy import resources as _subagent_proxy_resources
from imbue.mngr_subagent_proxy._stop_hook_guard import MNGR_MANAGED_HOOK_MARKERS
from imbue.mngr_subagent_proxy._stop_hook_guard import PROXY_CHILD_GUARD_PREFIX
from imbue.mngr_subagent_proxy._stop_hook_guard import guard_user_stop_hooks_against_proxy_children
from imbue.mngr_subagent_proxy._stop_hook_guard import iter_user_stop_hook_commands
from imbue.mngr_subagent_proxy.data_types import SubagentProxyMode
from imbue.mngr_subagent_proxy.data_types import SubagentProxyPluginConfig
from imbue.mngr_subagent_proxy.hooks.destroy_detached import DestroyAgentDetachedCallable
from imbue.mngr_subagent_proxy.hooks.destroy_detached import destroy_agent_detached

SUBAGENT_PROXY_CHILD_AGENT_TYPE: Final[str] = "mngr-proxy-child"

# Plugin name used to look up our config from MngrContext. Matches the
# entry-point name in pyproject.toml ([project.entry-points.mngr]).
SUBAGENT_PROXY_PLUGIN_NAME: Final[str] = "subagent_proxy"

register_plugin_config(SUBAGENT_PROXY_PLUGIN_NAME, SubagentProxyPluginConfig)


class SubagentProxyChildConfig(ClaudeAgentConfig):
    """Claude config for agents spawned by the subagent-proxy hook.

    Differs from ``ClaudeAgentConfig`` in one default:
    ``sync_home_settings=False`` -- mngr_claude does not copy the user's
    ``~/.claude/{plugins,skills,agents,commands}/`` into the child's
    per-agent config dir.

    Plugin-installed Stop hooks are also auto-guarded by the
    on_after_provisioning hookimpl below (env-conditional wrap on
    ``MNGR_SUBAGENT_PROXY_CHILD``), so the user-installed Stop-hook
    orchestrator does not re-prompt the spawned subagent into
    autofix/verify cycles.
    """

    sync_home_settings: bool = Field(
        default=False,
        description="Override: spawned subagents do not inherit user-installed Claude Code plugins via filesystem sync.",
    )


class UnguardedProjectStopHookError(MngrError):
    """Project-level ``.claude/settings.json`` has un-guarded user Stop hooks.

    Raised at provisioning time when a Claude agent's project settings
    file contains Stop or SubagentStop commands that would fire inside
    spawned proxy subagents (which inherit the worktree). Wrapping
    settings.json automatically would dirty a git-tracked file, so we
    refuse rather than silently mutate; the user has to either add the
    env-conditional guard themselves or set the override to bypass.
    """


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface], type[AgentTypeConfig]]:
    """Register the mngr-proxy-child agent type.

    Same agent class (ClaudeAgent) as the ``claude`` type; only the config
    differs (no inherited user plugins).
    """
    return (SUBAGENT_PROXY_CHILD_AGENT_TYPE, ClaudeAgent, SubagentProxyChildConfig)


_AGENT_DEFINITION: Final[str] = "mngr-proxy.agent.md"
_MNGR_SUBAGENTS_SKILL: Final[str] = "mngr-subagents.skill.md"

_SPAWN_MODULE: Final[str] = "imbue.mngr_subagent_proxy.hooks.spawn"
_CLEANUP_MODULE: Final[str] = "imbue.mngr_subagent_proxy.hooks.cleanup"
_REAP_MODULE: Final[str] = "imbue.mngr_subagent_proxy.hooks.reap"
_DENY_MODULE: Final[str] = "imbue.mngr_subagent_proxy.hooks.deny"


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
    cleanup_cmd = _python_hook_command(_CLEANUP_MODULE)
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
                            "command": cleanup_cmd,
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


def build_subagent_proxy_deny_hooks_config() -> dict[str, Any]:
    """Build the deny-mode hooks config: a single PreToolUse:Agent hook.

    No PostToolUse, no SessionStart reaper -- the deny hook never spawns
    a subagent, so there is nothing to clean up after a Task call and
    nothing to reap on session start. The hook just denies the Task
    tool with a copy-pasteable ``mngr create`` invocation in the deny
    reason; Claude (the calling agent) is expected to run those
    commands itself via Bash.
    """
    deny_cmd = _python_hook_command(_DENY_MODULE)
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Agent",
                    "hooks": [
                        {
                            "type": "command",
                            "command": deny_cmd,
                            "timeout": 15,
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


def _write_mngr_subagents_skill(host: OnlineHostInterface, work_dir: Path) -> None:
    """Write the ``mngr-subagents`` Claude skill under the agent's .claude/skills/.

    Used in DENY mode to give Claude the full context for delegating to
    mngr-managed subagents. The deny hook's ``permissionDecisionReason``
    is intentionally short -- a one-liner pointing at this skill plus
    the per-Task-call wait-script -- so the verbose protocol is loaded
    on demand by Claude rather than crowding every Task transcript.
    """
    skill_dir = work_dir / ".claude" / "skills" / "mngr-subagents"
    host.execute_idempotent_command(f"mkdir -p {shlex.quote(str(skill_dir))}", timeout_seconds=5.0)
    content = _load_resource(_MNGR_SUBAGENTS_SKILL)
    host.write_text_file(skill_dir / "SKILL.md", content)


def _merge_subagent_proxy_deny_hooks(host: OnlineHostInterface, work_dir: Path) -> None:
    """Merge the deny-mode hook into the agent's .claude/settings.local.json.

    Deny mode installs a single PreToolUse:Agent hook that denies Task
    calls with copy-pasteable mngr instructions. It does NOT install
    PostToolUse / SessionStart hooks (no spawned children to clean
    up), does NOT walk the user's plugin hooks dirs to install Stop-hook
    guards (no proxy children to guard against), and does NOT check the
    project ``settings.json`` for un-guarded Stop hooks (same reason).
    The surface is deliberately much smaller than PROXY mode.
    """
    settings_path = work_dir / ".claude" / "settings.local.json"
    existing_settings: dict[str, Any] = {}
    try:
        content = host.read_text_file(settings_path)
        existing_settings = json.loads(content)
    except FileNotFoundError:
        pass

    hooks_config = build_subagent_proxy_deny_hooks_config()
    merged = merge_hooks_config(existing_settings, hooks_config)
    if merged is None:
        logger.debug("Subagent-proxy deny hook already configured in {}", settings_path)
        return
    host.write_text_file(settings_path, json.dumps(merged, indent=2) + "\n")


def _merge_subagent_proxy_hooks(host: OnlineHostInterface, work_dir: Path) -> None:
    """Merge the subagent-proxy hooks into the agent's .claude/settings.local.json.

    Also rewrites every existing user-defined Stop/SubagentStop command
    in that file to no-op when MNGR_SUBAGENT_PROXY_CHILD=1 is set in the
    env. This is what stops user-installed Stop hooks (imbue-code-guardian's
    stop_hook_orchestrator.sh, project-specific cleanup hooks, etc.) from
    re-prompting spawned subagents into autofix/verify cycles.

    The wrap is env-conditional so it is safe for the parent agent too:
    the parent's MNGR_SUBAGENT_PROXY_CHILD is unset, the guard falls
    through, and the original command runs normally. Only the spawned
    proxy children, which we explicitly set the env var on at create
    time, see the no-op.
    """
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
        merged = existing_settings

    guard_changed = guard_user_stop_hooks_against_proxy_children(merged)

    if merged is not existing_settings or guard_changed:
        host.write_text_file(settings_path, json.dumps(merged, indent=2) + "\n")
    else:
        logger.debug("Subagent-proxy hooks already configured in {}", settings_path)

    # NOTE: project ``.claude/settings.json`` is git-tracked, so we
    # deliberately do NOT mutate it -- a wrap there would dirty the
    # working tree and (perversely) trigger user-installed
    # "uncommitted-changes" Stop hooks like imbue-code-guardian's
    # stop_hook_orchestrator.sh against the parent agent.
    #
    # Plugin-installed hooks.json files are the more impactful target.
    # Claude Code reads from the per-agent plugin cache, not the user
    # marketplace dir, so we walk both: the source-of-truth marketplace
    # files plus every per-agent cache under the host's agents tree.
    for plugin_hooks in _discover_plugin_hooks_files():
        _guard_stop_hooks_in_file(host, plugin_hooks)


def _guard_stop_hooks_in_file(host: OnlineHostInterface, path: Path) -> None:
    """Apply the proxy-child guard to every Stop/SubagentStop command in a JSON hooks file.

    Reads the file, walks its ``hooks`` dict (matching the schema both
    settings.json and plugin hooks.json files use), wraps each non-mngr
    Stop/SubagentStop command with the proxy-child guard, and writes the
    file back. No-op if the file is missing, malformed, or already fully
    guarded.
    """
    try:
        content = host.read_text_file(path)
    except FileNotFoundError:
        return
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Could not parse {}; skipping Stop-hook guard pass", path)
        return
    if not isinstance(data, dict):
        return
    if not guard_user_stop_hooks_against_proxy_children(data):
        return
    logger.info("Wrapped Stop hooks in {} with MNGR_SUBAGENT_PROXY_CHILD guard", path)
    host.write_text_file(path, json.dumps(data, indent=2) + "\n")


def _discover_plugin_hooks_files() -> list[Path]:
    """Return every plugin hooks.json that Claude Code might load.

    Walks the user's marketplace install (source of truth) plus every
    per-agent plugin cache under all known mngr host dirs. Claude Code
    reads from the per-agent cache at session start, so the marketplace
    file alone is not enough -- the wrap has to land in the cache too.
    """
    candidates: list[Path] = []
    user_plugins = get_user_claude_config_dir() / "plugins"
    if user_plugins.is_dir():
        try:
            candidates.extend(user_plugins.rglob("hooks/hooks.json"))
        except OSError as e:
            logger.warning("Could not enumerate user plugin hooks under {}: {}", user_plugins, e)

    # Per-agent plugin caches live under <host_dir>/agents/<id>/plugin/claude/anthropic/plugins/.
    # `read_default_host_dir` resolves MNGR_HOST_DIR (explicit override) or
    # falls back to ~/.mngr -- so we honor user-customized host dirs.
    host_agents_root = read_default_host_dir() / "agents"
    if host_agents_root.is_dir():
        try:
            candidates.extend(host_agents_root.glob("*/plugin/claude/anthropic/plugins/**/hooks/hooks.json"))
        except OSError as e:
            logger.warning("Could not enumerate per-agent plugin hooks under {}: {}", host_agents_root, e)

    return sorted(set(candidates))


def _is_subagent_proxy_child(agent: AgentInterface) -> bool:
    """Return True if this agent was spawned by the subagent-proxy hook.

    Proxy-spawned subagents register the ``mngr-proxy-child`` agent type
    (whose config is a SubagentProxyChildConfig), so the isinstance check
    is the authoritative signal.
    """
    return isinstance(agent.agent_config, SubagentProxyChildConfig)


class UnsupportedSubagentHookError(NotImplementedError):
    """A spawned subagent inherits Stop/SubagentStop hooks we don't know how to translate.

    Top-level-vs-subagent hook semantics differ (e.g. parent ``Stop`` hooks
    often re-prompt the agent and would prevent the subagent from ever
    ending its turn; a user's ``SubagentStop`` hook might be the one that
    actually wants to fire when the mngr subagent completes its work).
    Rather than guess wrong, refuse to proceed and make the user decide.
    """


# Marker tuple lives in _stop_hook_guard.py so reap.py can use it without
# importing this module. Re-imported above and re-exported here for
# backwards-compat with the local-shadow patterns just below.


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
        if not any(marker in command for marker in MNGR_MANAGED_HOOK_MARKERS):
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


_OPT_OUT_PROJECT_STOP_CHECK_ENV: Final[str] = "MNGR_SUBAGENT_PROXY_ALLOW_UNGUARDED_PROJECT_STOP_HOOKS"


def _check_project_settings_stop_hooks_guarded(host: OnlineHostInterface, work_dir: Path) -> None:
    """Refuse to provision when ``.claude/settings.json`` has un-guarded Stop hooks.

    settings.json is git-tracked, so the plugin doesn't auto-wrap it
    (would dirty the worktree). Instead, raise loudly so the user
    notices and adds the guard manually -- otherwise these hooks fire
    inside spawned proxy subagents and turn them into runaway autofix
    loops.

    Bypass with the ``MNGR_SUBAGENT_PROXY_ALLOW_UNGUARDED_PROJECT_STOP_HOOKS``
    env var (``=1``) when you know what you're doing. Intended as a
    temporary escape hatch.
    """
    if os.environ.get(_OPT_OUT_PROJECT_STOP_CHECK_ENV, "") == "1":
        return
    settings_path = work_dir / ".claude" / "settings.json"
    try:
        content = host.read_text_file(settings_path)
    except FileNotFoundError:
        return
    try:
        settings: dict[str, Any] = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Could not parse {}; skipping project stop-hook check", settings_path)
        return
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    offenders: list[str] = []
    for event_name, _cmd_entry, command in iter_user_stop_hook_commands(hooks):
        if any(marker in command for marker in MNGR_MANAGED_HOOK_MARKERS):
            continue
        if PROXY_CHILD_GUARD_PREFIX in command:
            continue
        offenders.append(f"{event_name}: {command[:80]}")
    if not offenders:
        return
    listing = "\n  - ".join(offenders)
    raise UnguardedProjectStopHookError(
        f"{settings_path} has {len(offenders)} un-guarded user Stop / SubagentStop hook(s). "
        f"These would fire inside spawned mngr-proxy subagents and likely cause runaway loops. "
        f"Either prepend the env-conditional guard to each command:\n"
        f"\n"
        f'  [ -n "$MNGR_SUBAGENT_PROXY_CHILD" ] && exit 0; <original>\n'
        f"\n"
        f"...or set {_OPT_OUT_PROJECT_STOP_CHECK_ENV}=1 in the env to bypass this check "
        f"(temporary escape hatch; see mngr_subagent_proxy README).\n"
        f"\n"
        f"Offending commands:\n  - {listing}"
    )


def _resolve_plugin_mode(mngr_ctx: MngrContext | None) -> SubagentProxyMode:
    """Resolve the plugin's mode from mngr_ctx, falling back to PROXY.

    ``mngr_ctx`` is None in unit tests (which pass it explicitly to keep
    the hookimpl signature satisfied without standing up a full MngrContext).
    Treat that case as "use defaults" -- equivalent to a user who never
    configured the plugin.
    """
    if mngr_ctx is None:
        return SubagentProxyPluginConfig().mode
    config = mngr_ctx.get_plugin_config(SUBAGENT_PROXY_PLUGIN_NAME, SubagentProxyPluginConfig)
    return config.mode


@hookimpl(trylast=True)
def on_after_provisioning(agent: AgentInterface, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
    """Install subagent-proxy hooks on Claude agents.

    Runs trylast so mngr_claude's provisioning (which writes the base
    settings.local.json) has already completed. For agents we recognize
    as our own spawned proxy-children, refuse to proceed if they inherit
    any Stop / SubagentStop hooks whose semantics differ between top-level
    and subagent contexts -- the user has to decide how those should apply.

    Behavior depends on ``SubagentProxyPluginConfig.mode``:
    - ``PROXY`` (default): install spawn / cleanup / SessionStart hooks,
      write the mngr-proxy agent definition, guard project Stop hooks.
    - ``DENY``: install only the deny hook. None of the other plumbing
      runs (no PostToolUse, no SessionStart reaper, no Stop-hook guard,
      no project settings.json check). The deny hook itself never
      directly invokes ``mngr create`` -- only the wait-script it
      generates does, and only when Claude runs it via Bash -- so the
      cascade-destroy / reaper machinery has nothing to track.
    """
    if not isinstance(agent.agent_config, ClaudeAgentConfig):
        return

    mode = _resolve_plugin_mode(mngr_ctx)
    if mode == SubagentProxyMode.DENY:
        _merge_subagent_proxy_deny_hooks(host, agent.work_dir)
        _write_mngr_subagents_skill(host, agent.work_dir)
        return

    _check_project_settings_stop_hooks_guarded(host, agent.work_dir)
    _write_proxy_agent_definition(host, agent.work_dir)
    _merge_subagent_proxy_hooks(host, agent.work_dir)

    if _is_subagent_proxy_child(agent):
        _check_subagent_hooks_compat(host, agent)
        _strip_user_hooks_from_subagent(host, agent.work_dir)
        _install_proxy_child_auto_allow(host, agent.work_dir)


def _install_proxy_child_auto_allow(host: OnlineHostInterface, work_dir: Path) -> None:
    """Auto-allow all permission dialogs in spawned mngr-proxy-child agents.

    Why: a proxy child is a Haiku dispatcher constrained to running our
    wait-script and fake_tool. When that child itself spawns a nested
    Agent call (depth 2+), our PreToolUse hook proxies it as another
    Bash invocation -- but the child's settings.local.json has no
    auto-allow for Bash, so the nested Bash call triggers a permission
    dialog INSIDE the child's Claude session. The parent's
    subagent_wait then surfaces NEED_PERMISSION, but the only way to
    resolve it is `mngr connect <child-name>` in another terminal,
    which makes nested subagents effectively unusable.

    Auto-allowing permissions in proxy children is safe: the child is
    structurally restricted (single Bash tool, agent definition limits
    commands) and is never user-driven. Top-level (non-child) agents
    are unaffected -- they keep whatever permissions config the user
    set.
    """
    settings_path = work_dir / ".claude" / "settings.local.json"
    existing_settings: dict[str, Any] = {}
    try:
        content = host.read_text_file(settings_path)
        existing_settings = json.loads(content)
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        logger.warning("Could not parse settings.local.json at {}; skipping auto-allow merge", settings_path)
        return
    auto_allow_config = build_permission_auto_allow_hooks_config()
    merged = merge_hooks_config(existing_settings, auto_allow_config)
    if merged is None:
        return
    host.write_text_file(settings_path, json.dumps(merged, indent=2) + "\n")


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


_SUBAGENT_MAP_DIRNAME: Final[str] = "subagent_map"
_CASCADE_LOG_NAME: Final[str] = "subagent_cascade_destroy.log"


def _read_subagent_map_targets(state_dir: Path) -> list[str]:
    """Return target_name values from every subagent_map entry under state_dir.

    Best-effort: malformed entries are skipped, missing dir returns [].
    """
    map_dir = state_dir / _SUBAGENT_MAP_DIRNAME
    if not map_dir.is_dir():
        return []
    targets: list[str] = []
    try:
        entries = list(map_dir.iterdir())
    except OSError as e:
        logger.warning("cascade-destroy: failed to list {}: {}", map_dir, e)
        return []
    for entry in entries:
        if entry.suffix != ".json":
            continue
        try:
            payload = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("cascade-destroy: skipping malformed {}: {}", entry, e)
            continue
        if not isinstance(payload, dict):
            continue
        target = payload.get("target_name")
        if isinstance(target, str) and target:
            targets.append(target)
    return targets


def cascade_destroy_recorded_children(
    state_dir: Path,
    agent_name: AgentName,
    destroy_callable: DestroyAgentDetachedCallable,
) -> None:
    """Read recorded children from ``state_dir`` and fan out detached destroys.

    Best-effort: errors are logged, never raised -- failing the parent's
    destroy because a child cleanup failed would leave both stuck.
    """
    targets = _read_subagent_map_targets(state_dir)
    if not targets:
        return
    log_path = state_dir / _CASCADE_LOG_NAME
    logger.info(
        "cascade-destroy: parent {} being destroyed; spawning detached destroy for {} child agent(s)",
        agent_name,
        len(targets),
    )
    for target in targets:
        destroy_callable(target, log_path)


@hookimpl
def on_before_agent_destroy(agent: AgentInterface, host: OnlineHostInterface) -> None:
    """Cascade-destroy any proxy children of a Claude agent before its state dir is wiped.

    Closes the gap where the PostToolUse:Agent hook never fires (parent
    Ctrl+C'd, crashed, or force-destroyed mid-Task) and the SessionStart
    reaper can't catch the orphans because the parent's
    ``$MNGR_AGENT_STATE_DIR/subagent_map/`` is about to disappear.
    """
    if not isinstance(agent.agent_config, ClaudeAgentConfig):
        return
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)
    cascade_destroy_recorded_children(state_dir, agent.name, destroy_agent_detached)

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

from imbue.mngr.api.git import GitignoreStatus
from imbue.mngr.api.git import check_path_gitignore_status
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.host_dir import read_default_host_dir
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.common import get_agents_root_dir
from imbue.mngr.hosts.host import read_json_dict_via_host
from imbue.mngr.hosts.host import write_json_dict_via_host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentName
from imbue.mngr_claude.claude_config import SESSION_GUARD
from imbue.mngr_claude.claude_config import build_permission_auto_allow_hooks_config
from imbue.mngr_claude.claude_config import get_agent_hook_settings_path
from imbue.mngr_claude.claude_config import get_user_claude_config_dir
from imbue.mngr_claude.claude_config import merge_hooks_config
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_claude_subagent_proxy import hookimpl
from imbue.mngr_claude_subagent_proxy import resources as _subagent_proxy_resources
from imbue.mngr_claude_subagent_proxy._stop_hook_guard import MNGR_MANAGED_HOOK_MARKERS
from imbue.mngr_claude_subagent_proxy._stop_hook_guard import PROXY_CHILD_GUARD_PREFIX
from imbue.mngr_claude_subagent_proxy._stop_hook_guard import guard_user_stop_hooks_against_proxy_children
from imbue.mngr_claude_subagent_proxy._stop_hook_guard import guarded_settings_text
from imbue.mngr_claude_subagent_proxy._stop_hook_guard import is_well_formed_command_entry
from imbue.mngr_claude_subagent_proxy._stop_hook_guard import iter_user_stop_hook_commands
from imbue.mngr_claude_subagent_proxy.data_types import SubagentProxyMode
from imbue.mngr_claude_subagent_proxy.data_types import SubagentProxyPluginConfig
from imbue.mngr_claude_subagent_proxy.hooks.destroy_detached import DestroyAgentDetachedCallable
from imbue.mngr_claude_subagent_proxy.hooks.destroy_detached import destroy_agent_detached

SUBAGENT_PROXY_CHILD_AGENT_TYPE: Final[str] = "mngr-proxy-child"

# Plugin-config registry key used to look up our config from MngrContext.
# This is the key users put in their settings.toml under
# `[plugins.claude_subagent_proxy]`, and must match what
# register_plugin_config() below registers. Aligned with the pyproject.toml
# entry-point key and the package directory name so the three names stay
# in sync.
CLAUDE_SUBAGENT_PROXY_PLUGIN_NAME: Final[str] = "claude_subagent_proxy"

register_plugin_config(CLAUDE_SUBAGENT_PROXY_PLUGIN_NAME, SubagentProxyPluginConfig)


class SubagentProxyChildConfig(ClaudeAgentConfig):
    """Claude config for agents spawned by the subagent-proxy hook.

    Differs from ``ClaudeAgentConfig`` in one default:
    ``sync_home_settings=False`` -- mngr_claude does not copy the user's
    ``~/.claude/{plugins,skills,agents,commands}/`` into the child's
    per-agent config dir.

    Plugin-installed Stop hooks are also auto-guarded by the
    on_after_provisioning hookimpl below (env-conditional wrap on
    ``MNGR_CLAUDE_SUBAGENT_PROXY_CHILD``), so the user-installed Stop-hook
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


class UnignoredProxyArtifactError(MngrError):
    """A subagent-proxy provisioning artifact path is not gitignored.

    Raised at provisioning time when the plugin would write its agent
    definition (PROXY mode) or DENY-mode skill into a git-tracked worktree
    where the target path is not gitignored -- the file would surface as an
    unstaged change and trip clean-tree stop hooks. Like mngr_claude's
    settings.local.json guard, we refuse rather than dirty the worktree; the
    user must either gitignore the path or disable the plugin.
    """


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface], type[AgentTypeConfig]]:
    """Register the mngr-proxy-child agent type.

    Same agent class (ClaudeAgent) as the ``claude`` type; only the config
    differs (no inherited user plugins).
    """
    return (SUBAGENT_PROXY_CHILD_AGENT_TYPE, ClaudeAgent, SubagentProxyChildConfig)


_AGENT_DEFINITION: Final[str] = "mngr-proxy.agent.md"
_PROXY_SKILL_RESOURCE: Final[str] = "mngr-proxy.skill.md"

# Where the plugin writes its provisioning artifacts, relative to the agent
# worktree's ``.claude/`` directory. Both live under a ``mngr-proxy/``
# subdirectory (rather than flat in ``agents/`` / ``skills/``) so a single
# ``.claude/agents/mngr-proxy/`` or ``.claude/skills/mngr-proxy/`` line in
# ``.gitignore`` covers them. Claude Code scans ``.claude/agents/``
# recursively and identifies a subagent by its frontmatter ``name:`` field
# (``mngr-proxy``), not its filename or directory, so the agent definition is
# still discovered from the subdirectory. A skill, by contrast, is named by
# its directory and its entry file must be exactly ``SKILL.md``.
_PROXY_AGENT_SUBPATH: Final[Path] = Path("agents") / "mngr-proxy" / "proxy.md"
_PROXY_SKILL_SUBPATH: Final[Path] = Path("skills") / "mngr-proxy" / "SKILL.md"

_SPAWN_MODULE: Final[str] = "imbue.mngr_claude_subagent_proxy.hooks.spawn"
_CLEANUP_MODULE: Final[str] = "imbue.mngr_claude_subagent_proxy.hooks.cleanup"
_REAP_MODULE: Final[str] = "imbue.mngr_claude_subagent_proxy.hooks.reap"
_DENY_MODULE: Final[str] = "imbue.mngr_claude_subagent_proxy.hooks.deny"
_GUARD_STOP_HOOKS_MODULE: Final[str] = "imbue.mngr_claude_subagent_proxy.hooks.guard_stop_hooks"


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
    - SessionStart: label-driven reaper (shared with DENY mode) plus
      PROXY-only Stop-hook guarding of the per-agent plugin cache.
    """
    spawn_cmd = _python_hook_command(_SPAWN_MODULE)
    cleanup_cmd = _python_hook_command(_CLEANUP_MODULE)
    reap_cmd = _python_hook_command(_REAP_MODULE)
    guard_cmd = _python_hook_command(_GUARD_STOP_HOOKS_MODULE)
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
                        {"type": "command", "command": reap_cmd},
                        {"type": "command", "command": guard_cmd},
                    ],
                }
            ],
        }
    }


def build_subagent_proxy_deny_hooks_config() -> dict[str, Any]:
    """Build the deny-mode hooks config: PreToolUse:Agent deny + SessionStart reap.

    - PreToolUse (Agent): emit a short skill-pointer ``permissionDecisionReason``
      that directs Claude at the ``mngr-proxy`` skill (installed under
      ``.claude/skills/`` by ``_write_proxy_skill``); the
      ``mngr create`` / ``subagent_wait`` protocol lives in that skill,
      not in the deny reason itself.
    - SessionStart: same shared label-driven reaper that PROXY mode uses
      (``hooks/reap.py``). Both spawn paths attach
      ``mngr_claude_subagent_proxy_parent_id=${MNGR_AGENT_ID}`` to every
      child, so the same query identifies orphans in either mode.

    No PostToolUse cleanup -- the deny hook never runs ``mngr create``
    itself, so there is no per-Task-call state on the parent to clean up.
    No PROXY-only Stop-hook guarding -- DENY-spawned children are plain
    claude agents and do not get the ``MNGR_CLAUDE_SUBAGENT_PROXY_CHILD``
    env var, so the guard predicate would never fire anyway.
    """
    deny_cmd = _python_hook_command(_DENY_MODULE)
    reap_cmd = _python_hook_command(_REAP_MODULE)
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


def _unignored_relative_or_none(host: OnlineHostInterface, repo_path: Path, claude_subpath: Path) -> Path | None:
    """Return the relative path if ``.claude/<claude_subpath>`` is NOT gitignored, else None.

    Computes the gitignore status of the path once via the any-rule
    ``check_path_gitignore_status`` and applies the single canonical accept
    rule ``status in (GitignoreStatus.SKIP, GitignoreStatus.IGNORED)`` -- i.e.
    the path is ignored, or the location is not a git repo (SKIP). When the path
    is accepted, returns None; otherwise returns the computed relative path
    (which reflects any ``.claude`` symlink resolution) so the caller can build
    its own error message. ``check_path_gitignore_status`` never returns
    ``ONLY_GLOBAL``, so this accept rule matches the prior ``is not NOT_IGNORED``.
    """
    status, relative = check_path_gitignore_status(host, repo_path, Path(".claude") / claude_subpath)
    if status in (GitignoreStatus.SKIP, GitignoreStatus.IGNORED):
        return None
    return relative


def _check_proxy_artifact_gitignored(host: OnlineHostInterface, work_dir: Path, claude_subpath: Path) -> None:
    """Refuse to write a provisioning artifact into a git-tracked worktree.

    Mirrors mngr_claude's settings.local.json guard: writing the file into a
    repo where its path is not gitignored would surface it as an unstaged
    change. Uses the any-rule ``check_path_gitignore_status`` (not the
    repo-rule-only variant), so a path ignored only via the user's global
    excludes is accepted -- this is a local provisioning step, not a preflight
    whose result has to hold on a remote host. Raises
    ``UnignoredProxyArtifactError`` otherwise, pointing the user at both the
    gitignore fix and the option to disable the plugin.

    We check the exact file the plugin is about to write, not its directory:
    the guard runs before the directory is created, and a directory-only
    gitignore rule (``mngr-proxy/``) does not match a not-yet-existing
    *directory* path -- but it does match a *file* under it, which is what we
    check. So checking the file accepts both ``.claude/agents/mngr-proxy/`` and
    broader rules like ``.claude/``. The remediation still points at the file's
    parent directory, since the plugin owns that whole ``mngr-proxy/`` subdir
    and one directory rule is the clean way to ignore it. ``relative.parent``
    (not the input subpath's parent) is used so the suggestion reflects any
    symlink resolution -- e.g. ``.agents/...`` when ``.claude -> .agents``.
    """
    relative = _unignored_relative_or_none(host, work_dir, claude_subpath)
    if relative is None:
        return
    raise UnignoredProxyArtifactError(
        f"'{relative}' is not gitignored in {work_dir}.\n"
        "The mngr subagent-proxy plugin writes this file when provisioning a Claude agent, "
        "but it would appear as an unstaged change in your repository.\n"
        f"Add '{relative.parent}/' to your .gitignore and try again, or disable the plugin for this repository:\n"
        f"  mngr config set --scope project plugins.{CLAUDE_SUBAGENT_PROXY_PLUGIN_NAME}.enabled false"
    )


def _check_settings_local_gitignored(host: OnlineHostInterface, repo_path: Path) -> None:
    """Verify .claude/settings.local.json is gitignored in the given repo path.

    The proxy wraps user-defined Stop hooks by writing into this file; if it is
    not gitignored the write would surface as an unstaged change. When .claude is
    a symlink, resolves it and checks the resolved path against .gitignore instead
    (e.g. .agents/settings.local.json if .claude -> .agents).

    Raises PluginMngrError if the file is not gitignored. Silently returns if the
    path is not a git repository or if the .claude symlink target is outside the
    repo (since git won't track it).
    """
    settings_relative = _unignored_relative_or_none(host, repo_path, Path("settings.local.json"))
    if settings_relative is None:
        return
    raise PluginMngrError(
        f"'{settings_relative}' is not gitignored in {repo_path}.\n"
        "mngr writes to this file (to guard user-defined Stop hooks against proxy children), "
        "but it would appear as an unstaged change.\n"
        f"Add '{settings_relative}' to your .gitignore and try again."
    )


def _write_proxy_agent_definition(host: OnlineHostInterface, work_dir: Path) -> None:
    """Write the mngr-proxy subagent definition under the agent's .claude/agents/.

    Written to ``.claude/agents/mngr-proxy/proxy.md``; Claude Code resolves the
    ``mngr-proxy`` subagent_type from the file's frontmatter ``name:`` field
    regardless of the subdirectory. Refuses to write if the path is not
    gitignored (see ``_check_proxy_artifact_gitignored``).
    """
    _check_proxy_artifact_gitignored(host, work_dir, _PROXY_AGENT_SUBPATH)
    agent_path = work_dir / ".claude" / _PROXY_AGENT_SUBPATH
    host.execute_idempotent_command(f"mkdir -p {shlex.quote(str(agent_path.parent))}", timeout_seconds=5.0)
    content = _load_resource(_AGENT_DEFINITION)
    host.write_text_file(agent_path, content)


def _write_proxy_skill(host: OnlineHostInterface, work_dir: Path) -> None:
    """Write the ``mngr-proxy`` Claude skill under the agent's .claude/skills/.

    Used in DENY mode to give Claude the full context for delegating to
    mngr-managed subagents. The deny hook's ``permissionDecisionReason``
    is intentionally short -- a one-liner pointing at this skill -- so
    the verbose two-command spawn-and-wait protocol is loaded on demand
    by Claude rather than crowding every Task transcript. Written to
    ``.claude/skills/mngr-proxy/SKILL.md`` (the skill is named by its
    directory; Claude Code requires the entry file to be exactly
    ``SKILL.md``). Refuses to write if the path is not gitignored.
    """
    _check_proxy_artifact_gitignored(host, work_dir, _PROXY_SKILL_SUBPATH)
    skill_path = work_dir / ".claude" / _PROXY_SKILL_SUBPATH
    host.execute_idempotent_command(f"mkdir -p {shlex.quote(str(skill_path.parent))}", timeout_seconds=5.0)
    content = _load_resource(_PROXY_SKILL_RESOURCE)
    host.write_text_file(skill_path, content)


def _merge_hooks_into_settings(host: OnlineHostInterface, settings_path: Path, hooks_config: dict[str, Any]) -> None:
    """Layer ``hooks_config`` onto the agent's Claude settings file.

    In normal mode this is the per-agent config-dir ``settings.json`` (the "user"
    layer Claude reads from ``$CLAUDE_CONFIG_DIR``); in ``use_env_config_dir`` mode
    it is the mngr-managed ``--settings`` file (see ``get_managed_settings_path``).
    Either way mngr_claude provisioning writes the base file before this plugin's
    ``on_after_provisioning`` runs, so it normally already exists; this read-merge-
    write tolerates a missing file regardless. ``merge_hooks_config`` preserves
    sibling (non-hook) keys.
    """
    existing_settings = read_json_dict_via_host(host, settings_path)
    merged = merge_hooks_config(existing_settings, hooks_config)
    if merged == existing_settings:
        logger.debug("Subagent-proxy hooks already configured in {}", settings_path)
        return
    # Ensure the parent exists so this doesn't depend on mngr_claude creating it first.
    write_json_dict_via_host(host, settings_path, merged, make_parent=True)


def _guard_user_stop_hooks_in_project_settings(host: OnlineHostInterface, work_dir: Path) -> None:
    """Wrap user Stop/SubagentStop hooks in ``.claude/settings.local.json`` with the
    MNGR_CLAUDE_SUBAGENT_PROXY_CHILD guard so they no-op inside proxy children.

    Without this, user-installed Stop hooks (imbue-code-guardian's
    stop_hook_orchestrator.sh, project cleanup hooks, etc.) would re-prompt
    spawned subagents into autofix/verify cycles. The wrap is env-conditional,
    so the parent agent (env unset) still runs the original command. Targets
    the *user's* hooks only; mngr's own hooks live in the managed settings file.

    Writing settings.local.json must not dirty the worktree, so this requires
    the file to be gitignored -- the one place that requirement still applies.
    """
    settings_path = work_dir / ".claude" / "settings.local.json"
    settings = read_json_dict_via_host(host, settings_path)
    if not settings:
        return
    if guard_user_stop_hooks_against_proxy_children(settings):
        _check_settings_local_gitignored(host, work_dir)
        write_json_dict_via_host(host, settings_path, settings)


def _merge_subagent_proxy_deny_hooks(host: OnlineHostInterface, hook_settings_path: Path) -> None:
    """Merge the deny-mode hooks into the agent's Claude settings file.

    Deny mode installs two hooks: a PreToolUse:Agent hook that denies
    Task calls with a short skill-pointer reason pointing at the
    ``mngr-proxy`` skill (installed alongside, under
    ``.claude/skills/``), plus the shared label-driven SessionStart
    reaper (same ``hooks/reap.py`` PROXY uses; both spawn paths attach
    the ``mngr_claude_subagent_proxy_parent_id`` label so the same
    query identifies orphans regardless of mode). It does NOT install
    a PostToolUse hook (the deny hook never runs ``mngr create``, so
    there is no per-Task-call state to clean up), does NOT walk the
    user's plugin hooks dirs to install Stop-hook guards (DENY
    children are plain claude agents without the proxy-child env var,
    so the guard predicate would never fire), and does NOT check the
    project ``settings.json`` for un-guarded Stop hooks (same reason).
    The surface is deliberately much smaller than PROXY mode.
    """
    _merge_hooks_into_settings(host, hook_settings_path, build_subagent_proxy_deny_hooks_config())


def _merge_subagent_proxy_hooks(host: OnlineHostInterface, hook_settings_path: Path, work_dir: Path) -> None:
    """Install the subagent-proxy hooks and guard user-installed Stop hooks.

    The proxy's own hooks go into the agent's Claude settings file
    (``hook_settings_path``). Separately, every user-defined
    Stop/SubagentStop command in the project's settings.local.json and in
    the plugin caches is wrapped with the MNGR_CLAUDE_SUBAGENT_PROXY_CHILD
    guard so it no-ops inside spawned proxy children -- see
    ``_guard_user_stop_hooks_in_project_settings``.
    """
    _merge_hooks_into_settings(host, hook_settings_path, build_subagent_proxy_hooks_config())

    _guard_user_stop_hooks_in_project_settings(host, work_dir)

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
    data = read_json_dict_via_host(host, path)
    if not data:
        return
    text = guarded_settings_text(data, path)
    if text is None:
        return
    host.write_text_file(path, text)


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
    host_agents_root = get_agents_root_dir(read_default_host_dir())
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
        if not is_well_formed_command_entry(cmd_entry):
            return False
        if not any(marker in cmd_entry["command"] for marker in MNGR_MANAGED_HOOK_MARKERS):
            return False
    return True


def _check_subagent_hooks_compat(host: OnlineHostInterface, agent: AgentInterface) -> None:
    """Refuse to provision a subagent-proxy child whose inherited Stop/SubagentStop hooks
    need custom translation between top-level and subagent semantics.

    mngr's own hooks load via ``claude --settings`` and are not in this file,
    so any Stop/SubagentStop hook here is user-configured (legacy mngr-marked
    leftovers are still recognized and allowed). A user hook's intended scope
    is ambiguous -- should it run once per subagent turn? once per outer turn?
    not at all? -- so we fail loudly rather than silently strip or duplicate.
    """
    settings_path = agent.work_dir / ".claude" / "settings.local.json"
    settings = read_json_dict_via_host(host, settings_path)
    if not settings:
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
                f"are ambiguous. mngr_claude_subagent_proxy does not yet know how "
                f"to translate these between the parent's scope and a "
                f"spawned subagent's scope. Review each hook: if it should "
                f"fire per subagent turn, install it there directly; if it "
                f"should only fire at the outer end_turn, narrow its "
                f"matcher. Offending settings path: {settings_path}"
            )


_OPT_OUT_PROJECT_STOP_CHECK_ENV: Final[str] = "MNGR_CLAUDE_SUBAGENT_PROXY_ALLOW_UNGUARDED_PROJECT_STOP_HOOKS"


def _check_project_settings_stop_hooks_guarded(host: OnlineHostInterface, work_dir: Path) -> None:
    """Refuse to provision when ``.claude/settings.json`` has un-guarded Stop hooks.

    settings.json is git-tracked, so the plugin doesn't auto-wrap it
    (would dirty the worktree). Instead, raise loudly so the user
    notices and adds the guard manually -- otherwise these hooks fire
    inside spawned proxy subagents and turn them into runaway autofix
    loops.

    Bypass with the ``MNGR_CLAUDE_SUBAGENT_PROXY_ALLOW_UNGUARDED_PROJECT_STOP_HOOKS``
    env var (``=1``) when you know what you're doing. Intended as a
    temporary escape hatch.
    """
    if os.environ.get(_OPT_OUT_PROJECT_STOP_CHECK_ENV, "") == "1":
        return
    settings_path = work_dir / ".claude" / "settings.json"
    settings = read_json_dict_via_host(host, settings_path)
    if not settings:
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
        f'  [ -n "$MNGR_CLAUDE_SUBAGENT_PROXY_CHILD" ] && exit 0; <original>\n'
        f"\n"
        f"...or set {_OPT_OUT_PROJECT_STOP_CHECK_ENV}=1 in the env to bypass this check "
        f"(temporary escape hatch; see mngr_claude_subagent_proxy README).\n"
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
    config = mngr_ctx.get_plugin_config(CLAUDE_SUBAGENT_PROXY_PLUGIN_NAME, SubagentProxyPluginConfig)
    return config.mode


@hookimpl(trylast=True)
def on_after_provisioning(agent: AgentInterface, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
    """Install subagent-proxy hooks on Claude agents.

    Runs trylast so mngr_claude's provisioning (which writes the base
    mngr-managed settings file) has already completed. For agents we recognize
    as our own spawned proxy-children, refuse to proceed if they inherit
    any Stop / SubagentStop hooks whose semantics differ between top-level
    and subagent contexts -- the user has to decide how those should apply.

    Behavior depends on ``SubagentProxyPluginConfig.mode``:
    - ``PROXY`` (default): install spawn / cleanup / SessionStart hooks,
      write the mngr-proxy agent definition, guard project Stop hooks.
    - ``DENY``: install the PreToolUse:Agent deny hook plus the shared
      label-driven SessionStart reaper (same ``hooks/reap.py`` PROXY
      uses), and write the ``mngr-proxy`` skill under
      ``.claude/skills/``. The other PROXY-only plumbing does NOT run
      (no PostToolUse cleanup, no Stop-hook guard, no project
      settings.json check). The deny hook never invokes ``mngr create``
      and does not generate per-Task wait-scripts; Claude reads the
      skill and runs the two-command spawn-and-wait protocol itself
      via Bash. The reaper still picks up terminal children spawned
      that way via the shared parent-id label.
    """
    config = agent.agent_config
    if not isinstance(config, ClaudeAgentConfig):
        return

    mode = _resolve_plugin_mode(mngr_ctx)
    # Where mngr's hooks live, matching mngr_claude: in normal mode the per-agent
    # config-dir settings.json (the "user" layer Claude reads, built by
    # _build_settings_json); in use_env_config_dir mode the managed --settings file
    # (no per-agent config dir exists). Both paths are derived from the same shared
    # claude_config helpers mngr_claude uses, so they never drift.
    agent_state_dir = get_agent_state_dir_path(host.host_dir, agent.id)
    hook_settings_path = get_agent_hook_settings_path(agent_state_dir, use_env_config_dir=config.use_env_config_dir)
    if mode == SubagentProxyMode.DENY:
        _merge_subagent_proxy_deny_hooks(host, hook_settings_path)
        _write_proxy_skill(host, agent.work_dir)
        return

    _check_project_settings_stop_hooks_guarded(host, agent.work_dir)
    _write_proxy_agent_definition(host, agent.work_dir)
    _merge_subagent_proxy_hooks(host, hook_settings_path, agent.work_dir)

    if _is_subagent_proxy_child(agent):
        _check_subagent_hooks_compat(host, agent)
        _strip_user_hooks_from_subagent(host, agent.work_dir)
        _install_proxy_child_auto_allow(host, hook_settings_path)


def _install_proxy_child_auto_allow(host: OnlineHostInterface, hook_settings_path: Path) -> None:
    """Auto-allow all permission dialogs in spawned mngr-proxy-child agents.

    Why: a proxy child is a Haiku dispatcher constrained to running our
    wait-script and fake_tool. When that child itself spawns a nested
    Agent call (depth 2+), our PreToolUse hook proxies it as another
    Bash invocation -- but the child has no auto-allow for Bash, so the
    nested Bash call triggers a permission dialog INSIDE the child's
    Claude session. The parent's subagent_wait then surfaces
    NEED_PERMISSION, but the only way to resolve it is
    `mngr connect <child-name>` in another terminal, which makes nested
    subagents effectively unusable.

    Auto-allowing permissions in proxy children is safe: the child is
    structurally restricted (single Bash tool, agent definition limits
    commands) and is never user-driven. Top-level (non-child) agents
    are unaffected -- they keep whatever permissions config the user
    set. The hook goes into the child's Claude settings file.
    """
    _merge_hooks_into_settings(host, hook_settings_path, build_permission_auto_allow_hooks_config())


def _strip_user_hooks_from_subagent(host: OnlineHostInterface, work_dir: Path) -> None:
    """Strip non-mngr user-configured hooks from the spawned subagent's settings.

    A spawned mngr subagent inherits the full settings.local.json from
    the source repo at create time, which typically includes whatever
    hooks the user has configured on the parent (PreToolUse filters,
    PostToolUse notifications, etc.). Their top-level-vs-subagent
    semantics are ambiguous, so drop everything that isn't recognized
    as mngr-managed. mngr's own hooks load via ``claude --settings``,
    not from here, so in practice this removes the inherited user hooks
    (the compat check has already rejected ambiguous user Stop/SubagentStop hooks).
    """
    settings_path = work_dir / ".claude" / "settings.local.json"
    settings = read_json_dict_via_host(host, settings_path)
    if not settings:
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
    _check_settings_local_gitignored(host, work_dir)
    logger.info("Stripped user-configured hooks from spawned subagent settings at {}", settings_path)
    write_json_dict_via_host(host, settings_path, settings)


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

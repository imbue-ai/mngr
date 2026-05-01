"""Shared bash-template fragments for the per-Task-call wait-scripts.

Both PROXY mode (``hooks/spawn.py``) and DENY mode (``hooks/deny.py``)
generate per-tool_use_id wait-scripts that share substantial bash
scaffolding: the same `set -euo pipefail` + `umask 077` header, the
same EXIT-trap-protected env-capture + ``mngr create --reuse`` init
block, and the same ``--spawn-only`` short-circuit.

Centralized here so the two hooks cannot drift on the load-bearing
EXIT-trap pattern that protects parent secrets from leaking to disk
on a signal between the env-file redirect and the trap installation.

``_hook_io.py`` deliberately stays focused on Python-level I/O
helpers; bash templates are a separate concern and get their own
module.
"""

from __future__ import annotations

from typing import Final

from imbue.mngr_subagent_proxy.mngr_binary import get_mngr_command_shell_form

WAIT_SCRIPT_HEADER: Final[str] = "#!/usr/bin/env bash\nset -euo pipefail\numask 077\n"

# Both modes accept ``--spawn-only`` as the first arg to skip the
# blocking ``subagent_wait`` step (used for ``run_in_background=true``
# Task calls). Identical text in both, so kept here as a constant.
WAIT_SCRIPT_SPAWN_ONLY_BRANCH: Final[str] = 'if [ "${1:-}" = "--spawn-only" ]; then\n    exit 0\nfi\n'


def build_init_block(*, agent_type: str, extra_create_env_kvs: tuple[str, ...] = ()) -> str:
    """Return the shared ``if [ ! -f "$INIT_FLAG" ]; then ... fi`` shell block.

    The block:
    1. Installs an EXIT trap that shreds ``$ENV_FILE``. The trap is
       installed BEFORE the env-redirect so a signal between the two
       cannot leave parent secrets on disk.
    2. Captures the parent's env (minus a few mngr-managed vars) to
       ``$ENV_FILE``.
    3. Runs ``mngr create --reuse`` for the subagent. ``--reuse`` is
       essential: it makes a partial-success state from a previous
       run (host provisioned but message-delivery errored) recoverable
       on the next invocation, rather than wedging permanently with
       "agent already exists".
    4. Shreds the env-file, clears the trap, and touches
       ``$INIT_FLAG`` so the next invocation skips the whole block.

    ``agent_type`` becomes the ``--type`` argument to ``mngr create``
    (``mngr-proxy-child`` for PROXY, ``claude`` for DENY).
    ``extra_create_env_kvs`` is a tuple of ``KEY=VALUE`` strings to
    pass as additional ``--env`` arguments (PROXY needs
    ``MNGR_SUBAGENT_PROXY_CHILD=1``; DENY needs none).

    The mngr-binary path is resolved at call time via
    ``get_mngr_command_shell_form()`` so the script and the Python
    helpers stay in lockstep on per-agent vs. fallback resolution.
    """
    mngr_cmd = get_mngr_command_shell_form()
    extra_env_lines = "".join(f"        --env {kv} \\\n" for kv in extra_create_env_kvs)
    return (
        'if [ ! -f "$INIT_FLAG" ]; then\n'
        # Trap removes the env-file (parent secrets) on ANY exit:
        # success, mngr-create failure, or signal. Installed BEFORE
        # the env capture so a signal arriving between the redirect
        # and the trap cannot leave secrets on disk. shred / rm -f
        # gracefully no-op if $ENV_FILE has not yet been created.
        # Cleared after touch so it doesn't fire on idempotent
        # re-entry (no ENV_FILE to clean up there).
        '    trap \'shred -u "$ENV_FILE" 2>/dev/null || rm -f "$ENV_FILE"\' EXIT\n'
        "    env | grep -Ev "
        "'^(MNGR_AGENT_STATE_DIR|MNGR_AGENT_NAME|MAIN_CLAUDE_SESSION_ID|MNGR_HOST_DIR)=' "
        '> "$ENV_FILE"\n'
        f'    {mngr_cmd} create "$TARGET_NAME:$PARENT_CWD" \\\n'
        f"        --type {agent_type} \\\n"
        "        --transfer=none \\\n"
        "        --no-ensure-clean \\\n"
        "        --no-connect \\\n"
        "        --reuse \\\n"
        '        --env-file "$ENV_FILE" \\\n'
        '        --message-file "$PROMPT_FILE" \\\n'
        "        --label mngr_subagent_proxy=child \\\n"
        f"{extra_env_lines}"
        "        --env MNGR_SUBAGENT_DEPTH=$((${MNGR_SUBAGENT_DEPTH:-0}+1))\n"
        '    shred -u "$ENV_FILE" 2>/dev/null || rm -f "$ENV_FILE"\n'
        "    trap - EXIT\n"
        '    touch "$INIT_FLAG"\n'
        "fi\n"
    )

#!/usr/bin/env bash
# mngr claude shim.
#
# Installed as `claude` in a dedicated directory that ClaudeAgent prepends to
# PATH for the agent's interactive session. Its sole job is to strip
# MAIN_CLAUDE_SESSION_ID from *nested* `claude` invocations (e.g. a `claude -p`
# the agent runs through its Bash tool) before exec-ing the real binary.
#
# Why: mngr's readiness/transcript hooks are all guarded on
# MAIN_CLAUDE_SESSION_ID (see claude_config.py SESSION_GUARD). A nested claude
# process inherits that variable from the parent session, so without this shim
# its SessionStart/UserPromptSubmit/Stop/... hooks would run and pollute the
# parent agent's transcript, lifecycle-state files, and session tracking with
# the child session. Unsetting the variable makes the guard fire so the child
# session is ignored by the parent agent.
#
# The *main* agent launch invokes the real binary by absolute path (resolved
# before this directory is on PATH) and never reaches this shim, so the main
# session's environment -- and therefore /clear, /compact, and resume -- are
# left completely untouched.

set -u

# Resolve the real `claude` by dropping our own directory from PATH and
# re-resolving. This works regardless of where claude is installed and avoids
# baking an absolute path in at provisioning time.
#
# Determine our own directory using bash parameter expansion (no external
# `dirname`, so the shim has no PATH-resolved dependencies of its own). The
# slashless case should never happen -- PATH resolution yields an absolute
# argv[0] -- but bail loudly rather than risk re-resolving (and exec-ing)
# ourselves.
self_src="${BASH_SOURCE[0]}"
case "$self_src" in
    */*) self_dir=$(CDPATH= cd -- "${self_src%/*}" && pwd) ;;
    *)
        echo "mngr claude shim: cannot determine own location (BASH_SOURCE=$self_src)" >&2
        exit 127
        ;;
esac

new_path=""
saved_ifs=$IFS
IFS=:
for entry in $PATH; do
    [ "$entry" = "$self_dir" ] && continue
    if [ -z "$new_path" ]; then
        new_path=$entry
    else
        new_path="$new_path:$entry"
    fi
done
IFS=$saved_ifs

real_claude=$(PATH="$new_path" command -v claude 2>/dev/null || true)
if [ -z "$real_claude" ]; then
    echo "mngr claude shim: could not find the real 'claude' binary on PATH (excluding $self_dir)" >&2
    exit 127
fi

unset MAIN_CLAUDE_SESSION_ID
exec "$real_claude" "$@"

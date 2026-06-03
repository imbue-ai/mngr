#!/usr/bin/env bash
# select_node_version.sh -- source me from a bash shell to select the Node
# version that apps/minds pins in apps/minds/.nvmrc, so pnpm/npm's
# `engine-strict` check (apps/minds/.npmrc) passes regardless of the shell's
# default Node.
#
# Behavior:
#   - No-op (returns 0) when the active `node` already matches the pin.
#   - Otherwise uses nvm to switch to the pinned version (modifying PATH in
#     the calling shell) and returns 0.
#   - On any failure prints an actionable hint to stderr and returns non-zero;
#     it never auto-installs Node (error-with-hint by design).
#
# Must be SOURCED, not executed: `nvm use` only affects the shell it runs in,
# so the PATH change has to land in the caller's shell. Callers should do:
#     . apps/minds/scripts/select_node_version.sh || exit 2
#
# .nvmrc is the single source of truth for the version; this script reads it
# rather than hard-coding a number (keep it in sync with package.json engines).

_minds_select_node() {
    # Run our own logic without the caller's strict modes -- nvm.sh references
    # unbound vars and trips `set -u`. Save and restore so the caller's shell
    # options are unchanged on return.
    local _had_u=0 _had_e=0
    case "$-" in *u*) _had_u=1 ;; esac
    case "$-" in *e*) _had_e=1 ;; esac
    set +ue

    local rc=0 script_dir nvmrc required current nvm_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    nvmrc="${script_dir}/../.nvmrc"

    if [ ! -f "${nvmrc}" ]; then
        echo "error: ${nvmrc} not found; cannot determine the required Node version." >&2
        rc=2
    else
        required="$(tr -d '[:space:]' < "${nvmrc}")"
        current="$(node --version 2>/dev/null)"
        if [ "${current}" = "v${required}" ]; then
            rc=0
        else
            nvm_dir="${NVM_DIR:-${HOME}/.nvm}"
            if [ ! -s "${nvm_dir}/nvm.sh" ]; then
                echo "error: active Node is ${current:-none}, but apps/minds requires v${required} (apps/minds/.nvmrc)." >&2
                echo "       nvm was not found at ${nvm_dir}." >&2
                echo "       Install nvm and run \`nvm install ${required}\`, or put v${required} first on PATH, then re-run." >&2
                rc=2
            else
                # shellcheck disable=SC1091
                . "${nvm_dir}/nvm.sh"
                if nvm use "${required}" >/dev/null 2>&1; then
                    rc=0
                else
                    echo "error: active Node is ${current:-none}, but apps/minds requires v${required} (apps/minds/.nvmrc)," >&2
                    echo "       and that version is not installed via nvm. Install it, then re-run:" >&2
                    echo "         nvm install ${required}" >&2
                    rc=2
                fi
            fi
        fi
    fi

    [ "${_had_u}" = 1 ] && set -u
    [ "${_had_e}" = 1 ] && set -e
    return "${rc}"
}

_minds_select_node

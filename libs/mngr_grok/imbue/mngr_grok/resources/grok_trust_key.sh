#!/usr/bin/env bash
# Prints grok's folder-trust key for the work dir given as the only argument (grok's workspace_key).
# Exit 0: the key is recordable. Exit 2: the key is $HOME or /, which grok trusts provisionally and never records.
# Exit 1: the work dir does not exist.
# HOME must be the HOME the grok process sees. Needs git 2.13 or newer.
set -euo pipefail

unset CDPATH GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_CEILING_DIRECTORIES GIT_INDEX_FILE \
    GIT_OBJECT_DIRECTORY GIT_NAMESPACE GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT
export GIT_DISCOVERY_ACROSS_FILESYSTEM=1

physical_path() {
    [ -n "$1" ] || return 1
    (cd -P -- "$1" 2>/dev/null && pwd -P)
}

# Prints the main worktree's root when the directory is a linked worktree of a non-bare repository, else nothing.
linked_worktree_main_root() {
    local common_raw common_dir main_dir core_worktree
    common_raw=$(cd -- "$1" && git rev-parse --git-common-dir 2>/dev/null) || return 0
    common_dir=$(cd -- "$1" && physical_path "$common_raw") || return 0
    if [ "$(git --git-dir="$common_dir" config --bool core.bare 2>/dev/null || true)" = true ]; then
        return 0
    fi
    main_dir=$(dirname -- "$common_dir")
    core_worktree=$(git --git-dir="$common_dir" config core.worktree 2>/dev/null) || core_worktree=""
    case $core_worktree in
        "") ;;
        /*) main_dir=$core_worktree ;;
        *) main_dir=$common_dir/$core_worktree ;;
    esac
    if [ "$(physical_path "$main_dir/.git" || true)" = "$common_dir" ]; then
        physical_path "$main_dir" || printf '%s\n' "$main_dir"
    fi
    return 0
}

work_dir=${1:?usage: grok_trust_key.sh <work dir>}
cwd=$(physical_path "$work_dir") || exit 1

key=""
if top=$(cd -- "$cwd" && git rev-parse --show-toplevel 2>/dev/null) && [ -n "$top" ]; then
    git_dir=$(cd -- "$cwd" && git rev-parse --absolute-git-dir 2>/dev/null) || git_dir=""
    if [ -n "$git_dir" ] && [ -f "$git_dir/commondir" ]; then
        key=$(linked_worktree_main_root "$cwd")
    fi
    if [ -z "$key" ]; then
        key=$(physical_path "$top") || key=$top
    fi
fi

home=""
if [ -n "${HOME:-}" ]; then
    home=$(physical_path "$HOME") || home=$HOME
fi

case $key in
    "" | / | "$home") key=$cwd ;;
esac
printf '%s\n' "$key"
case $key in
    / | "$home") exit 2 ;;
esac
exit 0

#!/usr/bin/env bash
# Stop hook script for Claude Code
# Prevents stopping unless all changes are committed

set -euo pipefail

if [ "${CLAUDE_FORCE_COMMIT:-0}" != "1" ]; then
    echo "CLAUDE_FORCE_COMMIT is not set to 1, skipping commit check."
    exit 0
fi

# Get lists of files in different states
untracked=$(git ls-files --others --exclude-standard)
staged=$(git diff --cached --name-only)
unstaged=$(git diff --name-only)

# make sure our output makes it to claude (must be on stderr)
echoerr() { echo "$@" 1>&2; }

# Check if there are any uncommitted changes
if [ -n "$untracked" ] || [ -n "$staged" ] || [ -n "$unstaged" ]; then
    echoerr "ERROR: Cannot stop - uncommitted changes detected!"
    echoerr ""

    if [ -n "$untracked" ]; then
        echoerr "Untracked files (need to git add or add to .gitignore):"
        echoerr "$untracked" | sed 's/^/  /'
        echoerr ""
    fi

    if [ -n "$unstaged" ]; then
        echoerr "Unstaged changes (need to git add):"
        echoerr "$unstaged" | sed 's/^/  /'
        echoerr ""
    fi

    if [ -n "$staged" ]; then
        echoerr "Staged but not committed (need to git commit):"
        echoerr "$staged" | sed 's/^/  /'
        echoerr ""
    fi

    echoerr "All files must be either gitignored or committed before stopping."
    echoerr "If you're not ready to commit yet because the task is not yet complete (ex: tests do not pass or you have a question for the user), simply prefix your commit message with WIP:"
    # must exit with exit code 2 to show stderr to claude when trying to prevent stopping
    exit 2
else
    echoerr "All changes committed. OK to stop."
    exit 0
fi

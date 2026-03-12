#!/usr/bin/env bash
set -euo pipefail

# Read JSON input from stdin
input=$(cat)

# Extract the command from tool_input.command using jq
command=$(echo "$input" | jq -r '.tool_input.command // empty')

# Check if command was extracted
if [[ -z "$command" ]]; then
    echo "No command found in input" >&2
    exit 0
fi

# Check if command starts with "git rebase"
if [[ "$command" =~ ^git[[:space:]]+rebase ]]; then
    echo "Blocked: git rebase commands are not allowed" >&2
    exit 2
fi

# Check if command is "git pull" with --rebase or -r flag
if [[ "$command" =~ ^git[[:space:]]+pull ]]; then
    if [[ "$command" == *"--rebase"* ]] || [[ "$command" =~ (^|[[:space:]])-r([[:space:]]|$) ]]; then
        echo "Blocked: git pull --rebase commands are not allowed (use git pull --merge instead)" >&2
        exit 2
    fi
fi

# Check if command starts with "git commit" and contains --amend or --fixup
if [[ "$command" =~ ^git[[:space:]]+commit ]]; then
    if [[ "$command" == *"--amend"* ]] || [[ "$command" == *"--fixup"* ]]; then
        echo "Blocked: git commit with --amend or --fixup is not allowed" >&2
        exit 2
    fi
fi

# Command is allowed
exit 0

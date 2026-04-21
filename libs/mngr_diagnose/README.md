# mngr-diagnose

Diagnose plugin for mngr -- launch an agent to diagnose bugs and prepare GitHub issues.

## Usage

```bash
# Diagnose a problem by description
mngr diagnose "create command fails when source path has spaces"

# Diagnose from an error context file (written by error handler)
mngr diagnose --context-file /tmp/mngr-diagnose-context-abc123.json

# Use a custom clone directory
mngr diagnose --clone-dir ~/mngr-clone "some error"
```

## How it works

1. Clones (or reuses an existing clone of) the mngr repository to a local directory
2. Creates an agent in a git worktree of that clone
3. The agent investigates the bug, finds root cause, and prepares a GitHub issue
4. The issue opens in the browser for user review before submission

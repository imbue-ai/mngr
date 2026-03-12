# Setting Up Ruff Pre-Commit Hook in A Repository

This guide explains how to create a ruff pre-commit hook setup

## Overview

The setup consists of three parts:
1. **Git hooks** that delegate to pre-commit (managed via `uv`)
2. **Pre-commit configuration** (`.pre-commit-config.yaml`) defining the ruff hook
3. **Ruff configuration** in `pyproject.toml`

## Prerequisites

- [uv](https://docs.astral.sh/uv/) installed (Python package manager)
- Git repository initialized

## Step 1: Add Dependencies

Add these to your `pyproject.toml` dev dependencies:

```toml
[dependency-groups]
dev = [
    "pre-commit>=4.2.0",
    "ruff~=0.12.8",
]
```

## Step 2: Create Ruff Configuration

Add to your `pyproject.toml`:

```toml
[tool.ruff]
line-length = 119
exclude = [
    # Add directories to exclude from linting/formatting
    "**/__snapshots__/**",
]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"

[tool.ruff.lint.isort]
force-single-line = true
case-sensitive = true
order-by-type = false
known-first-party = [
    # Add your project's package names here
    "myproject",
]
```

Customize as needed for your project.

## Step 3: Create Pre-Commit Configuration

Create `.pre-commit-config.yaml` in your repository root:

```yaml
# See https://pre-commit.com for more information
default_install_hook_types: [pre-commit, pre-push]

repos:
-   repo: local
    hooks:
    -   id: ruff
        name: "Python formatter + import sorter (ruff)"
        entry: bash -c 'uv run ruff check --select UP006,UP007,I,F401 --fix --force-exclude --config pyproject.toml "$@" && uv run ruff format --force-exclude --config pyproject.toml "$@"' --
        language: system
        types: [python]
```

### Understanding the Hook Entry Command

The hook runs two ruff commands in sequence:

1. **`ruff check`**: Lints code with specific rules and auto-fixes
   - `--select UP006,UP007,I,F401`: Only checks these rules:
     - `UP006`, `UP007`: pyupgrade rules (type annotation modernization)
     - `I`: isort rules (import sorting)
     - `F401`: Unused imports
   - `--fix`: Automatically fixes issues where possible
   - `--force-exclude`: Respects exclude patterns even for explicitly passed files
   - `--config pyproject.toml`: Uses config from pyproject.toml

2. **`ruff format`**: Formats code (similar to black)
   - `--force-exclude`: Respects exclude patterns
   - `--config pyproject.toml`: Uses config from pyproject.toml

## Step 4: Create Git Hook Scripts

Create `scripts/githooks/` directory with these files:

### `scripts/githooks/install.sh`
```bash
#!/usr/bin/env bash

set -euo pipefail

# Go to the root of the repo
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
cd ../..

mkdir -p .git/hooks

ln -sf ../../scripts/githooks/pre-commit .git/hooks/pre-commit
```

### `scripts/githooks/pre-commit`
```bash
#!/usr/bin/env bash
#
# This script uses uv to call pre-commit to run hooks on files about to be committed.
#
# Why not use `pre-commit install` directly?
# `pre-commit install` depends on the system Python version, whose packages may
# not be kept up-to-date. Using uv ensures consistent dependency management.
#
HERE=$(cd "$(dirname "$0")" && pwd)

# Run pre-commit through uv (last command so return code propagates to git)
uv run pre-commit hook-impl --config=.pre-commit-config.yaml --hook-type=pre-commit --hook-dir "$HERE" -- "$@"
```

Make them executable:
```bash
chmod +x scripts/githooks/install.sh
chmod +x scripts/githooks/pre-commit
```

## Step 5: Install the Hooks

From your repository root:

```bash
./scripts/githooks/install.sh
```

This creates a symlink from `.git/hooks/pre-commit` to your script.

## Step 6: Test the Setup

1. Make a change to a Python file
2. Stage and commit:
   ```bash
   git add .
   git commit -m "Test commit"
   ```
3. The ruff hook should run automatically

## Running Manually

To run the hooks manually on changed files:
```bash
uv run pre-commit run --show-diff-on-failure --files $(git diff origin/main --name-only)
```

To run on all files:
```bash
uv run pre-commit run --all-files
```

## Troubleshooting

### pre-commit not found by uv
- Run `uv sync` to install dependencies
- Ensure pre-commit is in your pyproject.toml dependencies

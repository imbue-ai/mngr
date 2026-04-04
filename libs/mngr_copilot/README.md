# mngr_copilot

A mngr plugin that adds support for [GitHub Copilot CLI](https://github.com/github/copilot-sdk) as a managed agent type.

## Overview

This plugin registers the `copilot` agent type, which runs the Copilot CLI in a managed tmux session on any mngr-supported host (local, Docker, Modal, etc.). It handles:

- Per-agent credential isolation via `COPILOT_HOME`
- Automatic syncing of GitHub OAuth tokens from your local machine to remote hosts
- macOS keychain support (reads stored tokens and writes them as plaintext for remote hosts)

## Prerequisites

The Copilot CLI must be installed and available as `copilot` in the agent's PATH. Follow the [Copilot CLI installation guide](https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli) to set it up.

To verify your local install:

```bash
copilot --version
```

## Usage

```bash
# Create a copilot agent
mngr create my-agent --type copilot

# Create on Modal
mngr create my-agent@.modal --type copilot

# Pass a GitHub token explicitly (bypasses credential sync)
mngr create my-agent --type copilot --pass-env COPILOT_GITHUB_TOKEN
```

## Authentication

The plugin follows the [Copilot SDK authentication docs](https://github.com/github/copilot-sdk/blob/main/docs/auth/index.md).

**Automatic (recommended) — keychain sync:**

On macOS, if you have previously run `copilot login`, the plugin automatically reads your stored token from the system keychain and injects it as `COPILOT_GITHUB_TOKEN` for the agent. This works for both local and remote agents.

**Manual — environment variables:**

Pass a token explicitly if you prefer not to use keychain sync, or on non-macOS systems:

```bash
mngr create my-agent --type copilot --pass-env COPILOT_GITHUB_TOKEN
# or
mngr create my-agent --type copilot --pass-env GH_TOKEN
```

The Copilot CLI checks `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, and `GITHUB_TOKEN` in that order. Explicit env vars always take precedence over keychain sync.

**BYOK:**

Pass your own API key as documented in the [BYOK docs](https://github.com/github/copilot-sdk/blob/main/docs/auth/byok.md).

## Configuration

All settings can be placed in `.mngr/settings.toml` or `~/.mngr/profiles/<id>/settings.toml`:

```toml
[copilot]
# Command to run (default: "copilot")
command = "copilot"

# Read token from macOS keychain and inject as COPILOT_GITHUB_TOKEN (default: true)
sync_copilot_credentials = true
```

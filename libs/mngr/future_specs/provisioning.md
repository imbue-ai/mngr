# Provisioning Spec

This document describes implementation details for the agent provisioning system. For user-facing documentation, see [provisioning concepts](../docs/concepts/provisioning.md).

## Overview

Agent provisioning is handled through methods on the agent class itself, not plugin hooks. This design allows agent types to define their own provisioning behavior through inheritance, using Python's standard method overriding.

The `BaseAgent` class provides default (no-op) implementations of all provisioning methods. Agent type implementations like `ClaudeAgent` override these methods to provide type-specific behavior.

## Pre-Provisioning Validation

Before any provisioning steps run, mngr calls `agent.on_before_provisioning()`. This method allows agent types to validate that required preconditions are met before any actual provisioning work begins.

Example validations an agent type might perform:
- Check that `ANTHROPIC_API_KEY` is set for the claude agent [future: incomplete]
- Check that required SSH keys exist locally
- Verify that a config file template exists at the expected path

If validation fails, the method should raise a `PluginMngrError` with a clear message explaining what is missing and how to fix it. This ensures that provisioning fails fast with actionable error messages rather than failing partway through after already making changes.

**Important**: The `on_before_provisioning()` method runs *before* any file transfers or package installations. It should only perform read-only validation checks, not make any changes to the host.

## File Transfer Collection

The next method called is `agent.get_provision_file_transfers()`.

Agent types can declare files and folders that need to be transferred from the local machine to the agent during provisioning by returning a list of transfer specifications.

Each transfer specification includes:

| Field | Type | Description |
|-------|------|-------------|
| `local_path` | `Path` | Path to the file or directory on the local machine |
| `agent_path` | `Path` | Destination path on the agent host. Must be a relative path (relative to work_dir) |
| `is_required` | `bool` | If `True`, provisioning fails if the local file doesn't exist. If `False`, the transfer is skipped if the file is missing. |

### Collection and Execution Order

1. **Collection phase**: Before provisioning begins, mngr calls `agent.get_provision_file_transfers()` to collect all file transfer requests.
2. **Validation phase**: For each transfer where `is_required=True`, mngr verifies that `local_path` exists. If any required file is missing, provisioning fails with a clear error listing all missing files.
3. **Transfer phase**: All collected transfers are executed, with optional transfers (where `is_required=False`) skipped if their source doesn't exist. Transfers happen *before* package installation and other provisioning steps.

### Use Cases

- **Config files**: Transfer local config files like `~/.anthropic/config.json` or `~/.npmrc`
- **Credentials**: Transfer credential files (subject to permission checks)
- **Project-specific files**: Transfer files referenced in `.mngr/settings.toml` that aren't part of the work_dir
- **Agent state**: Transfer agent-type-specific state that needs to be present for the agent to function

Agent types should provide configuration options for selecting which files to transfer.

Note that if an agent type needs to write files to the *host* (not the agent), it should do so as part of the `provision()` method, not via this file transfer mechanism (this is just a convenience for the common case).

## Agent Provisioning

The next method called is `agent.provision()`.

This is where agent types should check both for the presence of required packages and, ideally, minimum version requirements [future] (which helps prevent downstream failures that are harder to debug).

If a package is missing (or too old), agent types should emit a warning, and then:

1. For remote hosts: attempt to install it (if allowed / configured [future: configuration not implemented]), or fail with a clear message about what is missing and how to fix it
2. For local hosts: if running in interactive mode [future: detection not implemented], present the user with a command that can be run to either install it (if possible), or that they can run to install it themselves (if, eg, root access is required). If non-interactive, just fail with a clear message about what is missing and how to fix it.

Agent types should generally allow configuration for:

1. Disabling any kind of checking for packages (eg, assume they are properly installed)
2. Disabling automatic installation of missing packages (eg, just emit a message and the install command and fail)

The default behavior is intended to make `mngr` more usable--this way if something fails, the agent type can automatically fix it (rather than forcing the user to debug missing dependencies themselves).

Agent types can use pyinfra's built-in package management support to handle cross-platform installation of packages, or just do it themselves.

## Post-Provisioning

After all provisioning steps have completed, mngr calls `agent.on_after_provisioning()`. This method allows agent types to perform any finalization or verification steps after provisioning is done.

## Implementing Custom Agent Types

To implement provisioning for a custom agent type:

1. Subclass `BaseAgent`
2. Override the provisioning methods as needed:
   - `on_before_provisioning()` for validation
   - `get_provision_file_transfers()` for declaring files to transfer
   - `provision()` for installing packages and creating configs
   - `on_after_provisioning()` for finalization
3. Register the agent type using the `register_agent_type` hook

See `ClaudeAgent` in `libs/mngr_claude/imbue/mngr_claude/plugin.py` for a complete example.

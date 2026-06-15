<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr azure
**Usage:**

```text
mngr azure [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr azure prepare

**Usage:**

```text
mngr azure prepare [OPTIONS]
```
**Options:**

## Provider

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--provider` | text | Name of the [providers.NAME] block in settings.toml to read defaults from (subscription_id, default_region, resource_group, vnet/subnet/nsg names, allowed_ssh_cidrs). When the block does not exist, AzureProviderConfig class defaults are used as the fallback. CLI options below override either source. | `azure` |
| `--subscription-id` | text | Azure subscription ID. Defaults to the resolved provider config, then AZURE_SUBSCRIPTION_ID, then your active `az` subscription. | None |
| `--region` | text | Azure region. Defaults to the resolved provider config's default_region (westus if unset). | None |
| `--resource-group` | text | Resource group to create / reuse. Defaults to the resolved provider config's resource_group. | None |
| `--allowed-ssh-cidr` | text | Inbound CIDR allowed on tcp/22 and tcp/<container_ssh_port>. Repeat for multiple. Defaults to the resolved provider config's allowed_ssh_cidrs ('0.0.0.0/0'). Tighten for production. | None |
| `--host-dir-identity` | choice (`auto` &#x7C; `require` &#x7C; `skip`) | Whether to provision the bucket-write managed identity (user-assigned identity + a Storage Blob Data Contributor role assignment scoped to just the state storage account) that lets a VM push its host_dir to the bucket for offline reads. 'auto': attempt it, but degrade a missing-permission failure to a warning (network + bucket prepare still succeed). 'require': fail the command if the identity can't be provisioned. 'skip': don't attempt it. Needs Microsoft.ManagedIdentity/userAssignedIdentities/write + Microsoft.Authorization/roleAssignments/write (Owner or User Access Administrator) when something is actually created. | `auto` |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths; append __extend to the leaf key to extend list/dict/set fields) [repeatable] | None |

## mngr azure cleanup

**Usage:**

```text
mngr azure cleanup [OPTIONS]
```
**Options:**

## Provider

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--provider` | text | Name of the [providers.NAME] block in settings.toml to read defaults from (subscription_id, default_region, resource_group). When the block does not exist, AzureProviderConfig class defaults are used as the fallback. | `azure` |
| `--subscription-id` | text | Azure subscription ID. Defaults to the resolved provider config, then AZURE_SUBSCRIPTION_ID, then your active `az` subscription. | None |
| `--region` | text | Azure region. Defaults to the resolved provider config's default_region (westus if unset). | None |
| `--resource-group` | text | Resource group to delete. Defaults to the resolved provider config's resource_group. | None |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key. | `False` |
| `--safe` | boolean | Always query all providers during discovery (disable event-stream optimization). Use this when interfacing with mngr from multiple machines. | `False` |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |
| `-S`, `--setting` | text | Override a config setting for this invocation (KEY=VALUE, dot-separated paths; append __extend to the leaf key to extend list/dict/set fields) [repeatable] | None |

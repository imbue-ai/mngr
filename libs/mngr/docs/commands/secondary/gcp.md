<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr gcp
**Usage:**

```text
mngr gcp [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr gcp prepare

**Usage:**

```text
mngr gcp prepare [OPTIONS]
```
**Options:**

## Provider

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--provider` | text | Name of the [providers.NAME] block in settings.toml to read defaults from (project_id, default_zone, network, firewall_name, firewall_target_tag, allowed_ssh_cidrs). When the block does not exist, GcpProviderConfig class defaults are used as the fallback. CLI options below override either source. | `gcp` |
| `--project` | text | GCP project ID. Defaults to the resolved provider config's project_id (or the gcloud/ADC default). | None |
| `--zone` | text | GCE zone for the client. Firewall rules are global, but the client is zone-bound; defaults to the resolved provider config's default_zone. | None |
| `--firewall-name` | text | Firewall rule name to create / reuse. Defaults to the resolved provider config's firewall_name. | None |
| `--firewall-target-tag` | text | Network tag the rule targets (every instance is tagged with it). Defaults to the resolved provider config's firewall_target_tag. | None |
| `--network` | text | VPC network the rule applies to. Defaults to the resolved provider config's network. | None |
| `--allowed-ssh-cidr` | text | Inbound CIDR allowed on tcp/22 and tcp/<container_ssh_port>. Repeat for multiple. Defaults to the resolved provider config's allowed_ssh_cidrs ('0.0.0.0/0'). Tighten for production. | None |

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

## mngr gcp cleanup

**Usage:**

```text
mngr gcp cleanup [OPTIONS]
```
**Options:**

## Provider

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--provider` | text | Name of the [providers.NAME] block in settings.toml to read defaults from (project_id, network, firewall_name). When the block does not exist, GcpProviderConfig class defaults are used as the fallback. | `gcp` |
| `--project` | text | GCP project ID. Defaults to the resolved provider config's project_id (or the gcloud/ADC default). | None |
| `--firewall-name` | text | Firewall rule name to delete. Defaults to the resolved provider config's firewall_name. | None |
| `--network` | text | VPC network the rule applies to (part of its identity). Defaults to the resolved provider config's network. | None |
| `--force` | boolean | Also delete the GCS state bucket when it still holds offline host state left over from hosts that no longer exist as instances (otherwise cleanup refuses to delete a non-empty bucket). | `False` |

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

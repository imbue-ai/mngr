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

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--subscription-id` | text | Azure subscription ID. Defaults to the provider config / AZURE_SUBSCRIPTION_ID env var. | None |
| `--region` | text | Azure region. Defaults to the provider config's default_region (westus if unset). | None |
| `--resource-group` | text | Resource group to create / reuse. Defaults to 'mngr'. | None |
| `--allowed-ssh-cidr` | text | Inbound CIDR allowed on tcp/22 and tcp/<container_ssh_port>. Repeat for multiple. Required (fail-closed): with none supplied, prepare refuses to create a wide-open NSG. | None |

## mngr azure cleanup

**Usage:**

```text
mngr azure cleanup [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--subscription-id` | text | Azure subscription ID. Defaults to the provider config / AZURE_SUBSCRIPTION_ID env var. | None |
| `--region` | text | Azure region. Defaults to the provider config's default_region (westus if unset). | None |
| `--resource-group` | text | Resource group to delete. Defaults to 'mngr'. | None |

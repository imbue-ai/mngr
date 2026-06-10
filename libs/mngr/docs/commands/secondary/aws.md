<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr aws
**Usage:**

```text
mngr aws [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr aws prepare

**Usage:**

```text
mngr aws prepare [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--region` | text | AWS region. Defaults to the provider config's default_region (us-east-1 if unset). | None |
| `--sg-name` | text | Security group name to create / reuse. Defaults to 'mngr-aws'. | None |
| `--vpc-id` | text | VPC id to scope the SG lookup. Without this, multi-VPC name collisions raise. | None |
| `--allowed-ssh-cidr` | text | Inbound CIDR allowed on tcp/22 and tcp/<container_ssh_port>. Repeat for multiple. Defaults to ('0.0.0.0/0',) matching the provider config default. Tighten for production. | None |

## mngr aws ami

**Usage:**

```text
mngr aws ami [OPTIONS]
```
**Options:**


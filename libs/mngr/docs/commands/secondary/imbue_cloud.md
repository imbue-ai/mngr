<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr imbue_cloud
**Usage:**

```text
mngr imbue_cloud [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud auth

**Usage:**

```text
mngr imbue_cloud auth [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud auth signin

**Usage:**

```text
mngr imbue_cloud auth signin [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email | None |
| `--password` | text | Password (prompts if omitted) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud auth signup

**Usage:**

```text
mngr imbue_cloud auth signup [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email | None |
| `--password` | text | Password. When omitted, the command prompts twice on the TTY and verifies the two entries match. | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud auth signout

**Usage:**

```text
mngr imbue_cloud auth signout [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud auth status

**Usage:**

```text
mngr imbue_cloud auth status [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account; pass to query a different signed-in account). | None |

## mngr imbue_cloud auth use

**Usage:**

```text
mngr imbue_cloud auth use [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email to mark as active. Must already be signed in (run `mngr imbue_cloud auth signin --account <email>` first). | None |

## mngr imbue_cloud auth refresh

**Usage:**

```text
mngr imbue_cloud auth refresh [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud auth oauth

**Usage:**

```text
mngr imbue_cloud auth oauth [OPTIONS] {google|github}
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Optional account email. When set, the OAuth response must come back with the same email or the call fails (useful when re-authing a known account). When omitted, whatever email the OAuth provider returns becomes this session's account email -- this is the right shape for first-time signin via Google or GitHub. | None |
| `--callback-port` | integer | Bind the local OAuth callback listener to a specific port (default: auto-pick free port). | None |
| `--no-browser` | boolean | Print the authorize URL instead of launching the browser; useful when running headless. | `False` |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud auth forgot-password

**Usage:**

```text
mngr imbue_cloud auth forgot-password [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud auth resend-verification

**Usage:**

```text
mngr imbue_cloud auth resend-verification [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud hosts

**Usage:**

```text
mngr imbue_cloud hosts [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud hosts list

**Usage:**

```text
mngr imbue_cloud hosts list [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud hosts release

**Usage:**

```text
mngr imbue_cloud hosts release [OPTIONS] HOST_DB_ID
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud keys

**Usage:**

```text
mngr imbue_cloud keys [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud keys litellm

**Usage:**

```text
mngr imbue_cloud keys litellm [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud keys litellm create

**Usage:**

```text
mngr imbue_cloud keys litellm create [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--alias` | text | Optional human-readable alias for the key | None |
| `--max-budget` | float | Max spend in USD | None |
| `--budget-duration` | text | Budget reset duration (e.g. '1d', '30d') | None |
| `--metadata` | text | JSON-encoded dict of metadata to attach to the key (e.g. agent_id=...) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud keys litellm list

**Usage:**

```text
mngr imbue_cloud keys litellm list [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud keys litellm show

**Usage:**

```text
mngr imbue_cloud keys litellm show [OPTIONS] KEY_ID
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud keys litellm budget

**Usage:**

```text
mngr imbue_cloud keys litellm budget [OPTIONS] KEY_ID
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--max-budget` | float | New max budget in USD | None |
| `--budget-duration` | text | New budget reset duration (optional) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud keys litellm delete

**Usage:**

```text
mngr imbue_cloud keys litellm delete [OPTIONS] KEY_ID
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud tunnels

**Usage:**

```text
mngr imbue_cloud tunnels [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud tunnels create

**Usage:**

```text
mngr imbue_cloud tunnels create [OPTIONS] AGENT_ID
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--policy` | text | Default Cloudflare Access policy as JSON, e.g. '{"emails":["a@example.com"]}' | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud tunnels list

**Usage:**

```text
mngr imbue_cloud tunnels list [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud tunnels delete

**Usage:**

```text
mngr imbue_cloud tunnels delete [OPTIONS] TUNNEL_NAME
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud tunnels services

**Usage:**

```text
mngr imbue_cloud tunnels services [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud tunnels services add

**Usage:**

```text
mngr imbue_cloud tunnels services add [OPTIONS] TUNNEL_NAME SERVICE_NAME SERVICE_URL
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud tunnels services list

**Usage:**

```text
mngr imbue_cloud tunnels services list [OPTIONS] TUNNEL_NAME
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud tunnels services remove

**Usage:**

```text
mngr imbue_cloud tunnels services remove [OPTIONS] TUNNEL_NAME SERVICE_NAME
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud tunnels auth

**Usage:**

```text
mngr imbue_cloud tunnels auth [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud tunnels auth get

**Usage:**

```text
mngr imbue_cloud tunnels auth get [OPTIONS] TUNNEL_NAME
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--service` | text | If set, fetch the policy for this service instead of the tunnel default | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud tunnels auth set

**Usage:**

```text
mngr imbue_cloud tunnels auth set [OPTIONS] TUNNEL_NAME POLICY_JSON
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--service` | text | If set, set the policy for this service instead of the tunnel default | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud admin

**Usage:**

```text
mngr imbue_cloud admin [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud admin pool

**Usage:**

```text
mngr imbue_cloud admin pool [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud admin pool create

**Usage:**

```text
mngr imbue_cloud admin pool create [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--count` | integer | Number of pool hosts to create | None |
| `--attributes` | text | Lease-attributes JSON for the new pool rows (e.g. '{"version":"v1.2.3","cpus":2,"memory_gb":4}') | None |
| `--workspace-dir` | path | Path to the template repo checkout | None |
| `--management-public-key-file` | path | Path to the management SSH public key | None |
| `--database-url` | text | Neon PostgreSQL direct connection string | None |
| `--mngr-source` | path | Path to the mngr monorepo root. If provided, rsyncs into the template's vendor/mngr/ before creating hosts. | None |

## mngr imbue_cloud admin pool list

**Usage:**

```text
mngr imbue_cloud admin pool list [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--database-url` | text | Neon PostgreSQL direct connection string | None |

## mngr imbue_cloud admin pool destroy

**Usage:**

```text
mngr imbue_cloud admin pool destroy [OPTIONS] POOL_HOST_ID
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--database-url` | text | Neon PostgreSQL direct connection string | None |
| `--force` | boolean | Drop the row even if status != 'released' | `False` |

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
| `--all-devices` | boolean | Revoke EVERY session for this account (other devices and the browser), not just this machine's. | `False` |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud auth list

**Usage:**

```text
mngr imbue_cloud auth list [OPTIONS]
```
**Options:**


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

## mngr imbue_cloud auth login

**Usage:**

```text
mngr imbue_cloud auth login [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Optional account email. When set, the browser login must come back with the same email or the call fails (useful when re-authing a known account). When omitted, whatever account signs in on the hosted page becomes this session's account. | None |
| `--callback-port` | integer | Bind the local callback listener to a specific port (default: auto-pick free port). | None |
| `--no-browser` | boolean | Print the sign-in URL instead of launching the browser. The URL only works in a browser on THIS machine (it redirects back to a localhost listener); on a headless machine use `auth signin` instead. | `False` |
| `--success-redirect-url` | text | URL the success page links to once the callback lands (e.g. a minds:// deeplink so a click returns the user to the desktop app). Default: no link; the page just says to close the tab. | None |
| `--url-file` | file | Write the sign-in URL to this file once the callback listener is up. Lets an embedder (the minds desktop client) offer a copy-the-link fallback without parsing stderr. | None |
| `--connector-url` | text | Override connector URL | None |
| `--accounts-url` | text | Override the browser accounts-origin URL the login page is opened on (default: $MNGR__PROVIDERS__IMBUE_CLOUD__ACCOUNTS_URL, else the connector URL). Tiers with a dedicated accounts domain (e.g. production) only complete Google sign-in and session cookies on that origin. | None |

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

## mngr imbue_cloud auth is-verified

**Usage:**

```text
mngr imbue_cloud auth is-verified [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud account

**Usage:**

```text
mngr imbue_cloud account [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud account show

**Usage:**

```text
mngr imbue_cloud account show [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud account set-plan

**Usage:**

```text
mngr imbue_cloud account set-plan [OPTIONS] PLAN
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud account cleanup-grant

**Usage:**

```text
mngr imbue_cloud account cleanup-grant [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud account recheck-storage

**Usage:**

```text
mngr imbue_cloud account recheck-storage [OPTIONS]
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

## mngr imbue_cloud hosts rotate

**Usage:**

```text
mngr imbue_cloud hosts rotate [OPTIONS] HOST_REF
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud hosts enable-sharing

**Usage:**

```text
mngr imbue_cloud hosts enable-sharing [OPTIONS] HOST_REF
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
| `--rotate-on-exists` | boolean | When --alias is already taken, delete the existing key and mint a fresh one (the whole rotation runs in this single invocation) | `False` |

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

## mngr imbue_cloud bucket

**Usage:**

```text
mngr imbue_cloud bucket [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud bucket create

**Usage:**

```text
mngr imbue_cloud bucket create [OPTIONS] NAME
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--access` | choice (`read` &#x7C; `readwrite`) | Access scope for the default key minted with the bucket | `readwrite` |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud bucket list

**Usage:**

```text
mngr imbue_cloud bucket list [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud bucket info

**Usage:**

```text
mngr imbue_cloud bucket info [OPTIONS] NAME
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud bucket destroy

**Usage:**

```text
mngr imbue_cloud bucket destroy [OPTIONS] NAME
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--force` | boolean | When the destroy is refused as non-empty, delete the bucket's contents (batched S3 deletes) and retry | `False` |
| `--yes`, `-y` | boolean | Skip the --force confirmation prompt | `False` |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud bucket roll-key

**Usage:**

```text
mngr imbue_cloud bucket roll-key [OPTIONS] NAME
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud bucket keys

**Usage:**

```text
mngr imbue_cloud bucket keys [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud bucket keys list

**Usage:**

```text
mngr imbue_cloud bucket keys list [OPTIONS] [BUCKET_NAME]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud shares

**Usage:**

```text
mngr imbue_cloud shares [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud shares create

**Usage:**

```text
mngr imbue_cloud shares create [OPTIONS] HOST_ID
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |
| `--entry-label` | text | The workspace's shell-service origin label (e.g. system_interface-<rand>); the hosted web chrome enters the workspace at <entry-label>.<workspace-domain>. Omit to keep any previously recorded label. | None |
| `--preferred-region` | text | Preferred relay region code (e.g. us1) for a first-time share of a local workspace. Ignored for pool hosts, unknown regions, and re-shares (the existing region sticks). | None |

## mngr imbue_cloud shares delete

**Usage:**

```text
mngr imbue_cloud shares delete [OPTIONS] HOST_ID
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud shares status

**Usage:**

```text
mngr imbue_cloud shares status [OPTIONS] HOST_ID
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud shares list

**Usage:**

```text
mngr imbue_cloud shares list [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud shares relays

**Usage:**

```text
mngr imbue_cloud shares relays [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud sync

**Usage:**

```text
mngr imbue_cloud sync [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud sync records

**Usage:**

```text
mngr imbue_cloud sync records [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud sync records pull

**Usage:**

```text
mngr imbue_cloud sync records pull [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud sync records push

**Usage:**

```text
mngr imbue_cloud sync records push [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |
| `--input-file` | text | Read the record JSON from this file instead of stdin | None |

## mngr imbue_cloud sync records delete

**Usage:**

```text
mngr imbue_cloud sync records delete [OPTIONS] HOST_ID
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud sync scrub-secrets

**Usage:**

```text
mngr imbue_cloud sync scrub-secrets [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud sync bundle

**Usage:**

```text
mngr imbue_cloud sync bundle [OPTIONS] COMMAND [ARGS]...
```
**Options:**


## mngr imbue_cloud sync bundle pull

**Usage:**

```text
mngr imbue_cloud sync bundle pull [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

## mngr imbue_cloud sync bundle push

**Usage:**

```text
mngr imbue_cloud sync bundle push [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |
| `--input-file` | text | Read the bundle JSON from this file instead of stdin | None |

## mngr imbue_cloud sync bundle delete

**Usage:**

```text
mngr imbue_cloud sync bundle delete [OPTIONS]
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--account` | text | Account email (defaults to the active account) | None |
| `--connector-url` | text | Override connector URL | None |

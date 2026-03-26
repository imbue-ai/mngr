<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mng schedule

**Synopsis:**

```text
mng schedule [add|remove|update|list|run] [OPTIONS]
```

Schedule invocations of mng commands.

Manage cron-scheduled triggers that run mng commands (create, start, message,
exec) on a specified provider at regular intervals. This is useful for setting
up autonomous agents that run on a recurring schedule.

**Usage:**

```text
mng schedule [OPTIONS] COMMAND [ARGS]...
```
**Options:**

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## mng schedule add

**Usage:**

```text
mng schedule add [OPTIONS] [POSITIONAL_NAME]
```
**Options:**

## Trigger Definition

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--name` | text | Name for this scheduled trigger. If not specified, a random name is generated. | None |
| `--command` | choice (`create` &#x7C; `start` &#x7C; `message` &#x7C; `exec`) | Which mng command to run when triggered. | None |
| `--args` | text | Arguments to pass to the mng command (as a string). | None |
| `--schedule` | text | Cron schedule expression defining when the command runs (e.g. '0 2 * * *'). | None |

## Code Packaging

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--mng-install-mode` | choice (`auto` &#x7C; `package` &#x7C; `editable` &#x7C; `skip`) | How to make mng available in the deployed image: 'auto' detects based on current install, 'package' installs from PyPI, 'editable' packages local source, 'skip' assumes mng is already in the base image. | `auto` |
| `--snapshot` | text | Use an existing snapshot for code packaging instead of the git repo. | None |
| `--full-copy` | boolean | Copy the entire codebase into the deployed function's storage. Simple but slow for large codebases. | `False` |
| `--target-dir` | text | Directory inside the container where the target repo will be extracted. | `/code/project` |

## Deploy Files

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--include-user-settings`, `--exclude-user-settings` | boolean | Include or exclude user home directory settings files (e.g. ~/.mng/, ~/.claude.json). Default: include. | None |
| `--include-project-settings`, `--exclude-project-settings` | boolean | Include or exclude unversioned project-specific settings files. Default: include. | None |
| `--pass-env` | text | Forward an environment variable from the current shell into the deployed function (repeatable). | None |
| `--env-file` | path | Include an env file in the deployed function (repeatable). Variables are available to both the scheduled runner and the mng command. | None |
| `--upload` | text | Upload a file or directory into the deployed function (SOURCE:DEST format, repeatable). DEST paths starting with '~' go to the home directory; relative paths go to the project directory. | None |

## Execution

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--provider` | text | Provider in which to schedule the call (e.g. 'local', 'modal'). | None |

## Behavior

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--enabled`, `--disabled` | boolean | Whether the schedule is enabled. | None |
| `--verify` | choice (`none` &#x7C; `quick` &#x7C; `full`) | Post-deploy verification: 'none' skips, 'quick' invokes and destroys agent, 'full' lets agent run to completion. | `quick` |
| `--auto-merge`, `--no-auto-merge` | boolean | Fetch and merge the latest code from the target branch before each scheduled run. Requires GH_TOKEN in the environment (via --pass-env or --env-file). | `True` |
| `--auto-merge-branch` | text | Branch to fetch and merge at runtime before running the command. Defaults to the current branch when --auto-merge is enabled. | None |

## Add-specific

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--update` | boolean | If a schedule with the same name already exists, update it instead of failing. | `False` |
| `--auto-fix-args`, `--no-auto-fix-args` | boolean | Automatically add args to create commands to make sure they work as expected (e.g. --headless, --no-connect, --host-label SCHEDULE=<name>). | `True` |
| `--ensure-safe-commands`, `--no-ensure-safe-commands` | boolean | Error if the scheduled command looks unsafe (e.g. missing --branch with {DATE} or --reuse). Pass --no-ensure-safe-commands to downgrade these errors to warnings. | `True` |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNG_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |

## mng schedule list

**Usage:**

```text
mng schedule list [OPTIONS]
```
**Options:**

## Filtering

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-a`, `--all` | boolean | Show all schedules, including disabled ones. | `False` |
| `--provider` | text | Provider instance to list schedules from (e.g. 'local', 'modal'). | None |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNG_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |

## mng schedule remove

**Usage:**

```text
mng schedule remove [OPTIONS] NAMES...
```
**Options:**

## Safety

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `-f`, `--force` | boolean | Skip confirmation prompt. | `False` |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNG_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |

## mng schedule run

**Usage:**

```text
mng schedule run [OPTIONS] NAME
```
**Options:**

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNG_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |

## mng schedule update

**Usage:**

```text
mng schedule update [OPTIONS] [POSITIONAL_NAME]
```
**Options:**

## Trigger Definition

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--name` | text | Name for this scheduled trigger. If not specified, a random name is generated. | None |
| `--command` | choice (`create` &#x7C; `start` &#x7C; `message` &#x7C; `exec`) | Which mng command to run when triggered. | None |
| `--args` | text | Arguments to pass to the mng command (as a string). | None |
| `--schedule` | text | Cron schedule expression defining when the command runs (e.g. '0 2 * * *'). | None |

## Code Packaging

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--mng-install-mode` | choice (`auto` &#x7C; `package` &#x7C; `editable` &#x7C; `skip`) | How to make mng available in the deployed image: 'auto' detects based on current install, 'package' installs from PyPI, 'editable' packages local source, 'skip' assumes mng is already in the base image. | `auto` |
| `--snapshot` | text | Use an existing snapshot for code packaging instead of the git repo. | None |
| `--full-copy` | boolean | Copy the entire codebase into the deployed function's storage. Simple but slow for large codebases. | `False` |
| `--target-dir` | text | Directory inside the container where the target repo will be extracted. | `/code/project` |

## Deploy Files

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--include-user-settings`, `--exclude-user-settings` | boolean | Include or exclude user home directory settings files (e.g. ~/.mng/, ~/.claude.json). Default: include. | None |
| `--include-project-settings`, `--exclude-project-settings` | boolean | Include or exclude unversioned project-specific settings files. Default: include. | None |
| `--pass-env` | text | Forward an environment variable from the current shell into the deployed function (repeatable). | None |
| `--env-file` | path | Include an env file in the deployed function (repeatable). Variables are available to both the scheduled runner and the mng command. | None |
| `--upload` | text | Upload a file or directory into the deployed function (SOURCE:DEST format, repeatable). DEST paths starting with '~' go to the home directory; relative paths go to the project directory. | None |

## Execution

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--provider` | text | Provider in which to schedule the call (e.g. 'local', 'modal'). | None |

## Behavior

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--enabled`, `--disabled` | boolean | Whether the schedule is enabled. | None |
| `--verify` | choice (`none` &#x7C; `quick` &#x7C; `full`) | Post-deploy verification: 'none' skips, 'quick' invokes and destroys agent, 'full' lets agent run to completion. | `quick` |
| `--auto-merge`, `--no-auto-merge` | boolean | Fetch and merge the latest code from the target branch before each scheduled run. Requires GH_TOKEN in the environment (via --pass-env or --env-file). | `True` |
| `--auto-merge-branch` | text | Branch to fetch and merge at runtime before running the command. Defaults to the current branch when --auto-merge is enabled. | None |

## Common

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--format` | text | Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields. | `human` |
| `-q`, `--quiet` | boolean | Suppress all console output | `False` |
| `-v`, `--verbose` | integer range | Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE | `0` |
| `--log-file` | path | Path to log file (overrides default ~/.mng/events/logs/<timestamp>-<pid>.json) | None |
| `--log-commands`, `--no-log-commands` | boolean | Log commands that were executed | None |
| `--log-command-output`, `--no-log-command-output` | boolean | Log stdout/stderr from commands | None |
| `--log-env-vars`, `--no-log-env-vars` | boolean | Log environment variables (security risk) | None |
| `--headless` | boolean | Disable all interactive behavior (prompts, TUI, editor). Also settable via MNG_HEADLESS env var or 'headless' config key. | `False` |
| `--context` | path | Project context directory (for build context and loading project-specific config) [default: local .git root] | None |
| `--plugin`, `--enable-plugin` | text | Enable a plugin [repeatable] | None |
| `--disable-plugin` | text | Disable a plugin [repeatable] | None |

## See Also

- [mng create](../primary/create.md) - Create a new agent
- [mng start](../primary/start.md) - Start an existing agent
- [mng exec](../primary/exec.md) - Execute a command on an agent

## Examples

**Add a nightly scheduled agent**

```bash
$ mng schedule add --command create --schedule '0 2 * * *' --provider modal
```

**List all schedules**

```bash
$ mng schedule list --provider local
```

**Remove a trigger**

```bash
$ mng schedule remove my-trigger
```

**Disable a trigger**

```bash
$ mng schedule update my-trigger --disabled
```

**Test a trigger immediately**

```bash
$ mng schedule run my-trigger
```

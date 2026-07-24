<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr donate
**Usage:**

```text
mngr donate [OPTIONS]
```
**Options:**

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

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--skill` | text | Donation skill to run (the subdir name under the host-dir skill cache). | `document-review` |
| `--skill-repo` | text | Upstream git repo the skill (code + prompts) is checked out from. | `https://gitlab.com/sinnott-armstrong-lab/elsi-checklist/credits-for-science/document-review-skill.git` |
| `--skill-ref` | text | Git ref to check out: a branch to track, or a pinned commit for a reviewed version. | `c2e9bbe799c20c9da3896c2205991164f10555fd` |
| `--agent-name` | text | Name for the created donation agent. | `donate-extra-quota-bio` |
| `--dry-run` | boolean | Report the spare-capacity decision without creating an agent. | `False` |
| `--start` | boolean | Schedule donate to run automatically (installs a launchd LaunchAgent; macOS only) and exit. | `False` |
| `--stop` | boolean | Remove the scheduled donate LaunchAgent and exit. | `False` |
| `--interval-minutes` | integer range | With --start: how often the scheduled donate runs. | `10` |

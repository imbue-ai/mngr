<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr tmr-tasks

**Synopsis:**

```text
mngr tmr-tasks --tasks-file <JSONL> --mapper-prompt <FILE> --reducer-prompt <FILE> [--name <VARIANT>] [--provider <PROVIDER>] [--env KEY=VALUE] [--label KEY=VALUE] [--timeout <SECS>] [--agent-type <TYPE>]
```

Fan out a JSONL task file to one agent per task and integrate their branches.

This command runs the map-reduce framework over an explicit task file instead of pytest collection:

1. Reads and validates --tasks-file: one JSON packet per line with
   schema_version, id, optional display_id (used for agent/branch slugs),
   kind, and a free-form context object.
2. Launches one agent per task. The mapper prompt comes from --mapper-prompt
   (required; there is no packaged default -- the task semantics live with
   the producer of the task file), rendered with task_id, kind, context_json
   (the packet's context as pretty JSON), outcome_filename, and
   publish_snippet.
3. Polls agents until all finish or individually time out; pulls each
   agent's branch.bundle back into local branches.
4. If any mapper produced outputs, launches a reducer agent with
   --reducer-prompt to integrate the mapper branches.
5. Generates the shared HTML report; mapper agents must write the same
   testing_agent_outcome.json contract as `mngr tmr` mappers.

The canonical producer is `minds specs plan --for-tmr`, which emits one
packet per behavioral-spec unit; pair it with the minds spec-witnessing
prompts at apps/minds/tmr/specs_mapper.j2 and apps/minds/tmr/specs_reducer.j2.

**Usage:**

```text
mngr tmr-tasks [OPTIONS]
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
| `-h`, `--help` | boolean | Show this message and exit. | `False` |

## Other Options

| Name | Type | Description | Default |
| ---- | ---- | ----------- | ------- |
| `--tasks-file` | file | JSONL task file: one packet per line with schema_version, id, optional display_id, kind, and a free-form context object handed to the mapper prompt template. | None |
| `--name` | text | Variant name, used as the prefix for this run's agent/branch/host names. | `tmr-tasks` |
| `--mapper-prompt` | file | Mapper prompt template (required): rendered with task_id, kind, context_json, outcome_filename, and publish_snippet. There is no packaged default because the task semantics live with the producer of the task file. | None |
| `--reducer-prompt` | file | Reducer prompt template (required): rendered with inputs_dirname, mapper_outcome_filename, reducer_outcome_filename, and publish_snippet. | None |
| `--agent-type` | text | Type of agent to launch for each task | `claude` |
| `-t`, `--agent-template` | text | Create template to apply for mapper agents [repeatable, stacks in order] | None |
| `--provider` | text | Provider for agent hosts (e.g. local, docker, modal). Used for both mappers and the reducer. | `local` |
| `--env` | text | Environment variable KEY=VALUE to pass to agents [repeatable] | None |
| `--label` | text | Agent label KEY=VALUE to attach to all launched agents [repeatable] | None |
| `--snapshot` | text | Use an existing snapshot/image ID for all agents (skips building a fresh snapshot) | None |
| `--max-parallel-launch` | integer | Maximum number of agents to launch concurrently (launch-time parallelism) | `10` |
| `--agents-per-host` | integer | Number of agents sharing each remote host (ignored for local provider) | `4` |
| `--max-parallel-agents` | integer | Maximum number of mappers running at any one time (0 = no limit). When set, mappers are launched incrementally as earlier ones finish. | `0` |
| `--launch-delay` | float | Seconds to wait between launching each agent (avoids provider rate limits) | `2.0` |
| `--poll-interval` | float | Seconds between polling cycles when waiting for agents to finish | `60.0` |
| `--timeout` | float | Maximum seconds each mapper can run before being stopped (per-agent timeout) | `3600.0` |
| `--reducer-timeout` | float | Maximum seconds to wait for the reducer agent to finish | `3600.0` |
| `--output-dir` | path | Directory for the run's outputs (HTML report at index.html, per-agent artifacts) [default: <recipe>_<timestamp>/] | None |
| `--source` | directory | Source directory for discovery and agent work dirs [default: current directory] | None |
| `--reintegrate` | boolean | Re-read outcomes from a previous run, re-run the reducer, and regenerate the report. Skips discovery and mapper launching. The run to reintegrate is identified by --run-name. | `False` |
| `--run-name` | text | The run name. For new runs, overrides the auto-generated UTC YYYYMMDDHHMMSS timestamp; must not collide with prior runs whose agents are still discoverable. For --reintegrate, identifies which previous run to reintegrate (required). | None |
| `--additional-authorized-host` | text | SSH public key line to install in authorized_keys on each agent host (mappers, reducer, host pool, and snapshotter), allowing inbound SSH [repeatable] | None |

## See Also

- [mngr tmr](./tmr.md) - Run and fix tests in parallel using agents
- [mngr list](../primary/list.md) - List agents

## Examples

**Fan out minds spec units to witness-test-writing agents**

```bash
$ minds specs plan --for-tmr > /tmp/spec-tasks.jsonl && mngr tmr-tasks --tasks-file /tmp/spec-tasks.jsonl --name tmr-minds-specs --mapper-prompt apps/minds/tmr/specs_mapper.j2 --reducer-prompt apps/minds/tmr/specs_reducer.j2
```

**Use Docker provider**

```bash
$ mngr tmr-tasks --provider docker --tasks-file tasks.jsonl ...
```

**Limit to 4 concurrent agents**

```bash
$ mngr tmr-tasks --max-parallel-agents 4 --tasks-file tasks.jsonl ...
```

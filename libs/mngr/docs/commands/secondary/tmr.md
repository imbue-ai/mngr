<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr tmr

**Synopsis:**

```text
mngr tmr [TEST_PATHS...] [-- TESTING_FLAGS...] [--provider <PROVIDER>] [--env KEY=VALUE] [--label KEY=VALUE] [--timeout <SECS>] [--agent-type <TYPE>]
```

Run and fix tests in parallel using agents (test map-reduce).

This command implements a map-reduce pattern for tests:

1. Collects tests using pytest --collect-only, passing through all arguments.
2. Launches one agent per test. Each agent runs the test and, if it fails,
   attempts to diagnose and fix either the test code or the implementation.
3. Polls agents until all finish or individually time out (per-agent timeout).
   An HTML report is updated continuously during polling.
4. For successful fixes, pulls the agent's code changes into branches
   named tmr/<run>/*.
5. If any fixes succeeded, launches a reducer agent to merge all fix
   branches into a single integrated branch (tmr/<run>/reducer).
6. Generates a final HTML report summarizing all outcomes with markdown
   summaries, including the integrated branch name if applicable.

Arguments before -- are test paths/patterns (positional). Arguments after -- are
pytest testing flags shared between discovery and individual test runs. For example:

mngr tmr tests/e2e -- -m release

This discovers tests with `pytest --collect-only tests/e2e -m release` and runs
each test with `pytest tests/e2e/test_foo.py::test_bar -m release`.

Use --provider to run agents on a specific provider (e.g. docker, modal).
On providers that support snapshots (e.g. modal), the orchestrator
automatically builds and provisions one host, snapshots it, then launches
all remaining agents from that snapshot. Pass --snapshot <ID> to reuse an
existing snapshot instead of building one.
Use --env to pass environment variables and --label to tag all agents.
Use --max-parallel-agents to limit how many agents run simultaneously (0 = no limit).

Each agent writes its result to .test_output/testing_agent_outcome.json (in its work directory)
with a structured JSON containing: changes (list of kind/status/summary), errored flag,
tests_passing_before/after booleans, and a markdown summary.

**Usage:**

```text
mngr tmr [OPTIONS] [PYTEST_ARGS]...
```
## Arguments

- `PYTEST_ARGS`: Additional arguments passed through

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

- [mngr create](../primary/create.md) - Create a new agent
- [mngr list](../primary/list.md) - List agents
- [mngr rsync](../primary/rsync.md) - Rsync files between local and a remote host or agent

## Examples

**Run all tests in current directory**

```bash
$ mngr tmr
```

**Run tests in a specific file**

```bash
$ mngr tmr tests/test_foo.py
```

**Run tests with a marker**

```bash
$ mngr tmr tests/e2e -- -m release
```

**Use Docker provider**

```bash
$ mngr tmr --provider docker tests/
```

**Modal (snapshot is automatic)**

```bash
$ mngr tmr --provider modal tests/
```

**Pass env vars and labels**

```bash
$ mngr tmr --env API_KEY=xxx --label batch=run1
```

**Limit to 4 concurrent agents**

```bash
$ mngr tmr --max-parallel-agents 4 tests/
```

**Custom poll interval**

```bash
$ mngr tmr --poll-interval 30
```

**Specify output location**

```bash
$ mngr tmr --output-dir reports/run-1
```

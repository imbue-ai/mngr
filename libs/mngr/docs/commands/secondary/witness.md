<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr witness

**Synopsis:**

```text
mngr witness --root <CORPUS> [--tests <PATH>...] [--feature <PATH>...] [--area <AREA>] [--tag <TAG>] [--unit <KIND>] [--provider <PROVIDER>] [--node-provider <NODE>=<PROVIDER>...] [--node-env <NODE>:KEY=VALUE...]
```

Converge the tests witnessing a behavior corpus (setup, map, review, integrate, reduce).

Runs the witness pipeline over a behavior corpus, in five nodes:

1. setup: one agent finds or makes the shared test scaffolding every mapper
   will use, and writes a guide to it.
2. map: one agent per feature file converges that file's units to full
   coverage, tracing every assertion to the clause it witnesses.
3. review: one adversarial agent per mapper checks the trace, the verdicts,
   and the partial notes, and fixes what it can.
4. integrate: the orchestrator cherry-picks the reviewed branches together,
   recording what conflicted.
5. reduce: one agent integrates what conflicted, collapses duplicated
   scaffolding, verifies, and writes the changelog.

Mechanical gates run on every agent node; a branch that fails one is not
carried forward. The corpus is read-only to every agent. The execution plan
(where each node runs, with what environment) comes from --provider,
--node-provider, and --node-env.

**Usage:**

```text
mngr witness [OPTIONS]
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
| `--root` | directory | Behavior corpus root, conventionally <project>/behaviors; repo-relative, run from the repo root. | None |
| `--tests` | directory | Test root whose witnesses markers count [repeatable; default: the corpus root's parent] | None |
| `--feature` | text | Root-relative .feature path to run; composes with the other filters [repeatable] | None |
| `--area` | text | Only units under this folder area of the corpus | None |
| `--tag` | text | Only units carrying this tag or coordinate | None |
| `--unit` | choice (`rule` &#x7C; `scenario` &#x7C; `scenario-outline`) | Only units of this kind | None |
| `--provider` | text | Provider every node runs on unless --node-provider says otherwise | `local` |
| `--node-provider` | text | Place one node elsewhere, as <node>=<provider> [repeatable] | None |
| `--node-env` | text | Environment for one node's agents only, as <node>:KEY=VALUE [repeatable] | None |
| `--env` | text | Environment variable KEY=VALUE for every agent [repeatable] | None |
| `-t`, `--agent-template` | text | Create template to apply to every host and agent [repeatable] | None |
| `--agent-type` | text | Agent type to launch; the default is claude with permission prompts and startup dialogs switched off | `witness-claude` |
| `--max-running-agents` | integer | How many of a node's agents may run at once [default: 6 on local, unbounded elsewhere] | None |
| `--timeout` | float | Seconds each agent may run before it is stopped | `3600.0` |
| `--output-dir` | path | Where archives, the manifest, and the report go [default: witness_<execution name>/] | None |
| `--name` | text | Prefix of every agent, host, and branch name | `witness` |
| `--changelog-branch` | text | Name the changelog entries carry [default: the deliverable branch] | None |
| `--generation-root` | text | Where new witness modules go [default: the project's witnesses package] | None |
| `--keep-hosts` | boolean | Leave each node's hosts alive after it ends, for live debugging | `False` |

## See Also

- [mngr behaviors](./behaviors.md) - Inspect and validate a behavior corpus

## Examples

**Run all locally**

```bash
$ mngr witness --root libs/mngr_forward/behaviors
```

**Mappers on modal, reduce on this machine**

```bash
$ mngr witness --root libs/mngr_forward/behaviors --provider modal --node-provider reduce=local
```

**Give only the reduce node a token**

```bash
$ mngr witness --root libs/mngr_forward/behaviors --node-env reduce:GH_TOKEN=...
```

**One feature file**

```bash
$ mngr witness --root libs/mngr_forward/behaviors --feature authentication/signin.feature
```

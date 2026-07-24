<!-- This file is auto-generated. Do not edit directly. -->
<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->

# mngr tmr-specs

**Synopsis:**

```text
mngr tmr-specs --root <CORPUS> [--tests <PATH>...] [--area <AREA>] [--tag <TAG>] [--unit <KIND>] [-- TESTING_FLAGS...] [--name <VARIANT>] [--mapper-prompt <FILE>] [--reducer-prompt <FILE>] [--provider <PROVIDER>]
```

Create and update the tests witnessing a behavioral-spec corpus (spec map-reduce).

This command implements a map-reduce pattern anchored on behavioral specs:

1. Scans the corpus at --root (see `mngr specs`), fail-fasting on language
   violations, and groups its units (scenarios, scenario outlines, and
   invariant Rules) into one task per .feature file.
2. Launches one agent per spec file. Each agent converges the tests
   witnessing that file's units to the units' scope: creating missing
   witnesses, extending or trimming existing ones, fixing the
   implementation where it diverges from the spec, and keeping the
   `witnesses(coordinate, partial=...)` markers honest.
3. Polls agents until all finish or individually time out. An HTML report
   (task sections plus a per-coordinate coverage matrix) is updated
   continuously during polling.
4. Pulls each agent's changes into branches named <name>/<run>/*.
5. If any work succeeded, launches a reducer agent that integrates the
   branches (squash the test kinds, cherry-pick FIX_IMPL by priority),
   dedupes fixtures parallel mappers created independently, and audits the
   witness links by running `mngr specs matrix` over the integrated tree.
6. The corpus itself is read-only to the whole pipeline: mappers may only
   propose spec edits via the report's spec-escalations section, and an
   integrated branch that touches the corpus is mechanically refused.

Arguments after -- are pytest flags appended to the mappers' test runs.

Use --area/--tag/--unit to scope a run to part of the corpus, e.g.:

mngr tmr-specs --root apps/minds/specs --area authentication

Use --name to give a corpus variant its own prefix, and --mapper-prompt to
point it at a variant template that extends the packaged spec_mapper.j2:

mngr tmr-specs --root apps/minds/specs --name tmr-specs-minds --mapper-prompt apps/minds/tmr/specs_mapper.j2

**Usage:**

```text
mngr tmr-specs [OPTIONS]
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
| `--root` | directory | Behavioral-spec corpus root, conventionally <project>/specs (e.g. apps/minds/specs). Repo-relative; run from the repo root. | None |
| `--tests` | path | Test root witnessing the corpus; repeatable. Defaults to the corpus root's parent (a corpus at <project>/specs is witnessed by <project>'s tests). | None |
| `--area` | text | Only fan out units in this folder subtree, named as a dot-joined folder path from the corpus root (e.g. 'authentication'). | None |
| `--tag` | text | Only fan out units with this exact raw tag or exact coordinate. | None |
| `--unit` | choice (`rule` &#x7C; `scenario` &#x7C; `scenario-outline`) | Only fan out units of this kind. | None |
| `--name` | text | Variant name, used as the prefix for this run's agent/branch/host names (e.g. tmr-specs-minds) so distinct corpora stay separable and reviewable on their own. Distinct from --run-name, which identifies one run within a variant. | `tmr-specs` |
| `--mapper-prompt` | file | Override the packaged mapper prompt with this Jinja template file. It may '{% extends %}' the packaged spec_mapper.j2 by name and fill its project_guidance / infra_blockers blocks. | None |
| `--reducer-prompt` | file | Override the packaged reducer prompt with this Jinja template file. It may '{% extends %}' or '{% include %}' the packaged spec_reducer.j2 by name. | None |
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

- [mngr tmr](./tmr.md) - Run and fix tests in parallel using agents (docstring-anchored)
- [mngr specs](./specs.md) - Inspect and validate a behavioral-spec corpus
- [mngr create](../primary/create.md) - Create a new agent

## Examples

**Fan out the whole minds corpus**

```bash
$ mngr tmr-specs --root apps/minds/specs
```

**Scope to one area**

```bash
$ mngr tmr-specs --root apps/minds/specs --area authentication
```

**Only the invariant Rules**

```bash
$ mngr tmr-specs --root apps/minds/specs --unit rule
```

**The minds variant**

```bash
$ mngr tmr-specs --root apps/minds/specs --name tmr-specs-minds --mapper-prompt apps/minds/tmr/specs_mapper.j2
```

**Modal provider (snapshot is automatic)**

```bash
$ mngr tmr-specs --root apps/minds/specs --provider modal
```

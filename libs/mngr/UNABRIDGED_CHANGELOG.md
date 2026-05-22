# Unabridged Changelog - mngr

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-15

Restore Modal compatibility for the standard mngr Dockerfile and adopt offload's `post_patch_cmd` (introduced in v0.9.4). The Dockerfile is back to a single `FROM python:3.12-slim` stage (mngr's Modal image builder rejects multi-stage Dockerfiles), and all source-dependent setup (tarball extraction, git normalization, `image_commit_hash`, `uv sync`) lives in `scripts/post-source-setup.sh`, called both as the final Dockerfile RUN and as offload's `post_patch_cmd` so the two paths stay in sync. Bumps the offload pin from 0.9.2 to 0.9.5.

## 2026-05-14

`mngr create` no longer hard-codes `claude` as the default agent type. The agent type must now come from a positional argument, `--type`, or `[commands.create] type` in user settings. If none of those is supplied, `mngr create` exits with a clear error listing the registered agent types and pointing at `mngr config set commands.create.type <name> --scope user`. (Supersedes the `--type` source-default introduced in `mngr-fix-default.md` from this same release.)

New subcommand `mngr extras config`: walks through user-scope config settings the installer would otherwise leave blank. Each step short-circuits if its setting is already configured, so re-running only prompts for the gaps. Today this just covers the default agent type for `mngr create`; future config-related setup will be added as additional walk steps under the same subcommand. With an interactive terminal, presents an urwid single-select picker of every available agent type plus a "Keep no default" option, and writes the selection to `[commands.create] type` in user settings. With `-y` or without a terminal, prints the suggested `mngr config set` command and lists available agent types -- writes nothing.

`mngr extras completion` and `mngr extras claude-plugin` also use the new urwid picker (Install / Skip) instead of the old `[y/n]:` text prompt -- the entire `mngr extras -i` walkthrough now uses a consistent TUI rather than mixing the plugin-wizard's full-screen TUI with bare-text confirmation prompts.

`mngr extras -i` now also walks through the default agent type prompt as a final step, alongside the existing plugins / completion / Claude-plugin steps. `mngr extras` (no flag) reports the current default agent type as part of the status block.

`scripts/install.sh` no longer contains custom shell logic for the default agent type -- step 5 is gone, since the default agent type prompt now runs as part of `mngr extras -i` (step 4). The new subcommand is also re-runnable via `mngr extras config` if you skip it the first time.

`mngr plugin list` gains a `--kind` filter with two values, `agent-type` and `provider`, that project the plugin list to the canonical set of agent type names or provider backend names (with version/description metadata when entry-point names match).

Migration: existing users who upgrade and have no `[commands.create] type` set will see an error from `mngr create` until they run `mngr config set commands.create.type <name> --scope user` (or pick one via `mngr extras config`). The error message includes the registered agent types so you can copy-paste a value.

Bumped the pinned Claude Code CLI version from `2.1.116` to `2.1.141` in `libs/mngr/imbue/mngr/resources/Dockerfile`.

`mngr schedule add --verify quick|full` now works when the trigger's `mngr create` produces an agent that lives inside the cron-runner's local provider (i.e. inside the ephemeral Modal container). Verification now runs inside the container itself and reports the result back to the deploy machine over a structured sentinel line.

Fix tmux argv-parsing footguns for arguments starting with `-`:

- `tmux send-keys -l` now uses the `--` end-of-options separator, so agent commands and messages that start with `-` (e.g. `--model gemma`, `--help`) are no longer misparsed by tmux as flags.
- `tmux rename-session` now uses `--` before the positional new-name argument, so renaming an agent under a custom prefix that starts with `-` works correctly.

## 2026-05-14

`mngr list --format json`: the redundant `address` field on agent and host
records is no longer emitted. The same value is still reachable on the
parsed Python objects as `AgentDetails.address` / `HostDetails.address`,
and removing it from the wire format lets the output round-trip cleanly
through `AgentDetails.model_validate_json` (which previously rejected the
extra key).

`Volume`: gains a `path_exists(path)` method implemented across all
providers (local, Docker, Modal) and the `ScopedVolume` wrapper. Callers
no longer need to fall back to `listdir` and catch
provider-specific not-found errors to probe for a single file's
existence.

## 2026-05-12

# Address parsing refactor

The four shapes of address strings that mngr accepts on the command line are now
represented as separate typed values, parsed once at the CLI boundary and
threaded through the API layer as typed objects rather than raw strings.

## New address types

In `imbue.mngr.primitives` (with parsers in `imbue.mngr.api.address_parsers`):

- `HostAddress` — `HOST[.PROVIDER]` (or bare `.PROVIDER` for the new-host hint
  used by `mngr create`).
- `AgentAddress` — `NAME[@HOST[.PROVIDER]]`. Used by `mngr connect`,
  `mngr destroy`, `mngr exec`, etc. The agent component is required.
- `NewAgentLocation` — `[NAME][@HOST[.PROVIDER]][:PATH]`. The positional
  argument of `mngr create`. The name is optional (auto-generated if omitted)
  and is parsed strictly as `AgentName` (not an agent ID).
- `HostedLocation` — `[NAME[@HOST[.PROVIDER]]][:PATH]` or a bare path.
  Designates "a location on any host", local or remote. Used as the source
  argument of `mngr create --from`/`mngr pair` and as the target argument of
  `mngr push`/`mngr pull`.

Two new type aliases (in `imbue.mngr.primitives`) capture the
"name-or-id" notion at the type level:

- `AgentNameOrId = AgentId | AgentName`
- `HostNameOrId = HostId | HostName`

## CLI parses at the Click level

The Click `ParamType` adapters in `imbue.mngr.cli.address_params` (`AGENT_ADDRESS`,
`HOST_ADDRESS`, `NEW_AGENT_LOCATION`, `HOSTED_LOCATION`, `AGENT_NAME_OR_ID`,
`HOST_NAME_OR_ID`, `AGENT_NAME`) attach to `@click.argument` and
`@click.option`, so command bodies receive typed addresses directly. The
api/ layer takes the typed objects; the cli/ layer no longer holds parsing
logic that the api/ layer also needs.

## User-visible behavior changes

- **`mngr push`/`mngr pull` now accept `@HOST[.PROVIDER]:PATH` syntax** in their
  `TARGET`/`SOURCE` argument. Previously these commands had a bespoke parser
  that only understood `AGENT[:PATH]`, so a fully-qualified address like
  `mngr push my-agent@m1.modal:/path` failed with a "host filter not supported"
  error. The shared `HostedLocation` parser unifies this with the rest of the
  CLI.
- **`HostName` no longer permits dots.** Host names are validated as
  `SafeName` (alphanumeric + dashes/underscores). The `HOST.PROVIDER`
  qualifier is now exclusively the parser's responsibility -- previously
  `HostName` also carried `.provider_name` and `.short_name` properties that
  split on the dot, which contradicted the principle that "host names do not
  contain dots, so the dot is a deterministic separator."

## Internal cleanup

The following functions / types are removed; callers should use the typed
replacements:

- `parse_address_part`, `parse_host_qualifier`, `parse_source_string`,
  `parse_identifier_as_address`, `ParsedSourceLocation` -> replaced by the
  composite parsers in `api/address_parsers.py` and FrozenModel types in
  `primitives.py`.
- `find_and_maybe_start_agent` -> deleted; was redundant with
  `find_one_agent` (callers all did `discover_*` + the call). Its
  matching logic now lives in `filter_one_agent` (now also raises a
  helpful "Multiple agents found ... disambiguate using NAME@HOST.PROVIDER"
  message listing each colliding agent). Its materialization logic now lives
  in a small `materialize_agent(host_ref, agent_ref, mngr_ctx)` helper that
  callers can reuse when they already have discovered refs.
- `find_one_agent` no longer takes a `command_name`; the disambiguation
  hint in the multi-match error no longer embeds the CLI command name.
- `find_agent_for_command` no longer takes a `command_usage` argument and
  now merges its optional `host_filter` into the agent address upfront
  (raising if address and filter pin different hosts).
- `parse_agent_spec` (cli helper for `mngr push`/`pull`) -> deleted; use
  `parse_hosted_location` / the `HOSTED_LOCATION` ParamType.
- `_host_matches_filter` -> replaced by the `HostAddress.matches` method.
- `api/agent_addr.py` was folded into `api/find.py` and `api/discover.py`; with
  typed addresses as the norm, there is no longer a reason to segregate
  address-accepting functions.
- The api-level `find_one_agent`, `find_all_agents`,
  `discover_by_address`, `filter_one_host` (formerly
  `resolve_host_reference`, now tightened to require a non-None
  `HostAddress`), `filter_one_agent` (formerly
  `resolve_agent_reference`, now tightened to require a non-None
  `AgentNameOrId`), `filter_all_hosts`, `filter_all_agents`,
  `exec_command_on_agent(s)`, etc., all take typed addresses now instead of
  raw strings.
- `AgentDetails.address` and `HostDetails.address` expose the corresponding
  typed addresses as cached properties, so callers can pass them directly to
  api functions instead of reconstructing addresses from individual fields.

Improve the error message when an `[agent_types.<name>]` block in `mngr.toml` contains an unknown field. Previously the message only listed the unknown fields and valid fields, which looked like a typo report even when the real cause was a missing plugin. The message now includes a hint suggesting that the plugin providing the agent type may not be installed (mirroring the existing hint shape used for providers), and lists currently disabled plugins when relevant.

Encode the actual defaults for `mngr create` options that previously listed a default in their help text but were stored as `None` and resolved at runtime: `--type` now defaults to `"claude"` directly, and `--start-on-boot` defaults to `False`. Also corrects the `--worktree-base-folder` help text to reflect the actual default location (`<host_dir>/worktrees`).

Behavior change: when a config file (`[commands.create]`) or template sets `type` and the user passes a positional `AGENT_TYPE` on the command line, the positional now wins (matching the general "CLI > config" precedence). Previously the config-supplied `type` won, and a mismatch raised a "Conflicting agent types" error.

- Fixed: `mngr clone <agent> <new-name>@.<provider>` (and `mngr migrate` for cross-host moves) now succeeds when the source and destination agents live on different hosts. Previously the plugin-state rsync passed the destination host as both source and target, so rsync ran on the destination sandbox and failed with `change_dir "/.../plugin" failed: No such file or directory` because the source plugin dir only exists on the source agent's host. `CreateAgentOptions` now carries the source agent's location (host + state dir) as a single `HostLocation`, and `_transfer_source_plugin_data` rsyncs from that host to the destination -- so the Claude transcript, session history, and memory carry over for local->remote, remote->local, and remote->remote clones alike.

## 2026-05-11

- Fixed: `mngr list` / `mngr kanpan` no longer log a per-agent
  `WARNING: Error evaluating ... no such member in mapping: 'X'` when an
  `--include` / `--exclude` filter references a key on a schemaless
  field (`labels`, `plugin`, `host.tags`, `host.plugin`) that some
  agents happen not to have. The filter now quietly evaluates to false
  on those agents.
- `has(labels.foo)` (and the same for keys under `plugin`, `host.tags`,
  `host.plugin`) is now the recommended presence-check idiom for those
  schemaless fields, and is shown in the `mngr list --help` examples.
  Note: `labels.foo != null` does NOT work as a presence check on
  tolerant fields -- use `has(...)`.
- Filters against typoed strict fields (e.g. `host.providr` instead
  of `host.provider`) still surface a warning so users can see the
  typo.
- Bumps the `cel-python` minimum to `>=0.5.0` so the dev environment
  matches the version the global `mngr` install picks up. Earlier
  versions (e.g. 0.4.0) folded `host.providr == "local"` style misses
  silently to false instead of warning, so the strict-typo warning
  surface was narrower than intended on the locked version.

Remove a stale "(NOT IMPLEMENTED YET)" marker on the `provider_names` parameter of `imbue.mngr.api.list.list_agents`. The filter has long been wired through both batch and streaming codepaths into `list_provider_names_to_load`; the parameter-doc inline comment had not caught up.

Demote two internal trace log lines emitted during `mngr create` from INFO to DEBUG. The `_setup_per_agent_config_dir: agent=... host.is_local=True ...` and `_write_generated_files: host.is_local=True, ...` lines were leaking into user stdout on every create; they are diagnostic-only and now only appear under `MNGR_LOG_LEVEL=debug` or `-v`. The normal create output is now just `Creating agent state... / Starting agent X... / Sending initial message... / Done.`.

## 2026-05-10

- Fixed a spurious "Duplicate host name '127.0.0.1' found on provider 'docker'" warning that fired on `mngr list` when multiple Docker containers were running against a local Docker daemon. `Host.get_name()` now returns the mngr-assigned host name from the host's certified data instead of the SSH connector hostname. Use the new `get_connector_host_name()` accessor when the literal connector address is needed.

## 2026-05-09

- `mngr message <agent> -m /clear` (and `-m /compact`) no longer hangs for 90 s before returning. The `mngr-submit-<session>` tmux signal that `mngr message` waits on is now also fired from the SessionStart hook when the source is `clear` or `compact`, since those TUI-local slash commands do not trigger UserPromptSubmit.

- Changed: bumped the default `docker build` timeout for the docker provider from 5 to 10 minutes, and made it configurable per provider instance via the new `build_timeout_seconds` field (e.g. `mngr config set --scope user providers.docker.build_timeout_seconds 1800`).
- Improved: when a `docker build` exceeds the configured timeout, mngr now raises a clear `DockerBuildTimeoutError` that names the timeout and points at the config knob, instead of surfacing a generic non-zero-exit `ProcessError` that hid the cause behind a wall of build output.

Fixed `mngr gc` crashing on hosts whose SSH host key is missing from `known_hosts`. Such errors now raise `HostAuthenticationError` (a trust failure) instead of the generic `HostConnectionError`, so existing per-host gc handlers skip them with a warning instead of aborting the whole run.

- `mngr plugin add` no longer warns about unknown config fields and unknown provider backends when the local config references plugins that haven't been installed yet (those warnings would resolve themselves the moment the install completes; they were just noise)
- New `silent` flag on `_check_unknown_fields`, `_parse_providers`, `_parse_agent_types`, `_parse_plugins`, `_parse_retry_config`, `_parse_logging_config`, and `parse_config` suppresses the warnings when paired with `strict=False`. `load_config` and `setup_command_context` expose the same via a `silent_unknown_fields` parameter. Default behavior on every other command is unchanged.

## 2026-05-08

- Fixed: `mngr stop` no longer leaves orphaned child processes alive on Linux when the agent's pane process (e.g. `claude`) was killed abruptly (SIGKILL, OOM). The pane-descendant walk previously missed grandchildren that had reparented to PID 1 -- typically `playwright-mcp`, `node`, or other long-lived helpers -- so they survived `mngr stop` and accumulated, consuming memory across stop/start cycles. `Host.stop_agents` now also enumerates processes by their inherited `MNGR_AGENT_ID` env var via `/proc/<pid>/environ`, catching these orphans regardless of process tree.

Accept the `host.provider` qualifier consistently anywhere mngr takes a host
identifier (previously only the positional `agent@host.provider` form worked).
For example, `mngr create --from @m1.modal:/some/path`, `mngr limit --host
m1.modal`, `mngr snapshot create --host m1.modal`, and the `--host` filter on
list-style commands now all resolve `host.provider` deterministically.

Also fix `mngr create --from` against a remote source: the current-branch
lookup, and the related git-author/origin-URL lookups in remote-source flows,
now run through the host interface so they work for any provider rather than
raising `NotImplementedError`.

`mngr create --from` between a source and a target on the same remote host
now works: the git-mirror push short-circuits to a single in-host `git push`
between two local paths (no SSH), and `--transfer=git-worktree` is allowed
whenever source and target are on the same host -- previously it was
restricted to local-only agents. The default transfer mode for same-host
remote git sources is now `git-worktree` instead of `git-mirror`.

The internal rsync helper used for `work_dir_extra_paths` and the rsync
transfer mode also short-circuits when source and target are on the same
host, running a single rsync between two local paths on that host instead
of routing through a laptop temp directory.

- Fixed: `mngr create` now provisions credentials correctly inside nested sandboxes (e.g. a Linux lima VM running on a macOS host). `get_user_claude_config_dir()` previously returned `$ORIGINAL_CLAUDE_CONFIG_DIR` even when that path (a host-side path like `/Users/<user>/.claude`) did not exist inside the VM, causing `_provision_local_credentials` to log "No .credentials.json found to provision" and silently no-op. Spawned child agents then failed Claude sessions with "Not logged in". The helper now falls back to `$CLAUDE_CONFIG_DIR` when `$ORIGINAL_CLAUDE_CONFIG_DIR` does not resolve to an existing directory, so credential provisioning (and every other call site that resolves user-scope config) finds the live per-agent credentials.

Add a Concise spec under `specs/expose-outer-host/concise.md` for
exposing each host's outer machine (the VPS / docker daemon host /
local machine hosting a container). Restructures the host class
hierarchy so `OuterHost` becomes the base class with the minimal safe
API (file ops, command execution, locking, env vars, SSH info) and the
existing `Host` extends it to add agent / lifecycle / snapshot / tag
machinery. Adds an optional context-manager-based accessor on
`OnlineHostInterface` and `ProviderInstanceInterface` that yields
`OuterHostInterface | None`. Surfaces via a new `mngr exec --outer`
flag that dedups by outer host so the command runs once per unique
outer; `--missing-outer abort|warn|ignore` (default `warn`) controls
behavior when targeted agents have no accessible outer. The existing
one-off SSH paths (`mngr_imbue_cloud/vps_admin.py`,
`mngr_vps_docker/docker_over_ssh.py`) will be deleted and migrated to
the new abstraction. Modal, `local`, `ssh`, and docker-over-tcp return
`None` (no accessible outer). No code changes yet — spec only.

## 2026-05-07

- Fixed a perpetual one-event lag in remote (online-host) event
  follow-mode tailing. When reading `events.jsonl` over the wire,
  pyinfra's `CommandOutput.stdout` is built by `"\n".join(...)` after
  each line is `rstrip("\n")`-ed, so a file ending in `\n` and one not
  ending in `\n` produced identical strings. The follow loop's
  partial-write guard then treated the most recent complete line as
  in-flight and held it back until a later line forced it out. The
  remote read now wraps `cat` as `{ cat <file> && printf '%s' <sentinel>; }`,
  asserts the sentinel is present, and strips it back off to recover
  the file's exact byte tail.

## 2026-05-04

- Fixed: local-host shell commands issued from worker threads (e.g. inside `mngr observe --discovery-only`, `mngr list`, `mngr message`, `mngr destroy`, `mngr gc`, `mngr discover`) no longer crash with `TypeError: child watchers are only available on the default loop` on Linux. `Host._run_shell_command` now bypasses pyinfra's gevent-backed `LocalConnector` for local hosts and runs commands via the `ConcurrencyGroup` process runner.

## 2026-05-02

- JSONL parsers now surface upstream corruption rather than silently dropping bad lines
  - `MalformedJsonLineWarner.parse` raises `MalformedJsonlLineError` on lines that parse but aren't JSON objects (e.g. `[1,2,3]`); valid-but-incomplete JSON is still buffered as a possible end-of-file partial write
  - `parse_event_line`, `parse_discovery_event_line`, `parse_agents_from_mngr_output`, and `_parse_batched_json_files` (vps_docker) all raise on malformed input instead of returning `None`; rationale: stdout is for JSON data, stderr is for logs, and silently skipping garbage hides real upstream bugs
  - New `MalformedJsonlLineError` exception in `imbue.mngr.errors`
- Fixed: `resolve_provider_names_for_identifiers` no longer silently returns partial results when an identifier is unknown; it returns `None` to signal a full discovery scan is needed (regression introduced in the merge that combined the two parsing-fix branches)
- Fixed: `mngr connect` no longer fails type-checking; the two `build_agent_filter_cel` call sites now pass the required `cg` and `project_root` arguments to match `mngr list` and `mngr kanpan`

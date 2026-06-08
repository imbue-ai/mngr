# Unabridged Changelog - mngr

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-06

Regenerated the `mngr robinhood` CLI help doc (`docs/commands/secondary/robinhood.md`) to document the new `--include-partial-messages` and `--stream-plain-text` streaming flags (implemented in `imbue-mngr-robinhood`).

## 2026-06-05

Two small shared core helpers were added/extracted to support the antigravity per-agent isolation work (reusable by other plugins):

- `imbue.mngr.utils.git_utils.find_git_source_path` -- the per-agent source-repo trust resolution now delegates to this, eliminating logic duplicated byte-for-byte between the `antigravity` and `claude` plugins (no behavior change).
- `imbue.mngr.hosts.common.symlink_or_copy_on_host(host, source, dest, *, symlink, ensure_source_parent=...)` -- a one-round-trip helper that symlinks (always, even to a not-yet-existing source -- a write-through symlink) or copies (only if the source exists) a path on the host, centralizing the symlink-vs-copy credential/cache pattern.

`mngr dependencies` was reworked so that "which dependencies count" and "whether to install" are now two orthogonal options instead of being conflated into the old `-c`/`-a`/`-i` flags. The old flags were removed and replaced by:

- `--scope core|all` (default `all`): which dependencies determine the exit code (and which `--install auto` targets). `--scope core` exits non-zero only when a *core* dependency is missing -- missing optional dependencies are tolerated and the command exits 0. `--scope all` exits non-zero if anything is missing.
- `--install none|interactive|auto` (default `none`): `none` only checks; `interactive` shows the same prompt as before; `auto` installs the in-scope missing dependencies without prompting.

Mapping from the old flags: old `-c` becomes `--scope core --install auto`, old `-a` becomes `--install auto`, and old `-i` becomes `--install interactive`.

`ssh` was reclassified from a core to an optional dependency. mngr's remote-host connectivity runs through paramiko (pure-Python, no `ssh` binary), so the `ssh` binary is only needed to attach an interactive session to a remote agent and as the transport for rsync/git over SSH -- all remote-only features, putting it in the same category as `rsync` and `unison`. The core dependencies are now `git`, `tmux`, and `jq`. Every path that shells out to the `ssh` binary -- `mngr connect` to a remote agent, `mngr git push`/`pull` and `mngr rsync` to a remote host, and the git-mirror/rsync source transfer when creating a remote agent -- now raises a clear `BinaryNotInstalledError` if `ssh` is missing, instead of an opaque "ssh: command not found".

Updated documentation references for the `mngr_uncapped_claude` plugin rename to
`mngr_robinhood`: the PyPI README sub-projects list now points to
`libs/mngr_robinhood/`, and the auto-generated CLI docs entry moved from
`docs/commands/secondary/uncapped-claude.md` to
`docs/commands/secondary/robinhood.md` (command renamed from
`mngr uncapped-claude` to `mngr robinhood`).

## 2026-06-04

- Fix `mngr rsync` (and any other command resolving a host-location address) so that a *relative* `:PATH` on an agent endpoint resolves against that agent's workdir rather than the ambient working directory of the process running mngr. Previously `mngr rsync ./src/ my-agent:runtime/foo` passed the bare `runtime/foo` straight to rsync, which resolved it against the caller's cwd -- on a local-provider host that silently targeted the *caller's* checkout instead of the agent's worktree (and, when source and destination collapsed to the same directory, transferred nothing). An absolute `:PATH` is still honored verbatim, and the by-name-only form (no `:PATH`) still resolves to the workdir itself.

- Replace `@pytest.mark.flaky` with a modest `@pytest.mark.timeout(30)` bump on a set of `libs/mngr` CLI/API tests whose only observed flakiness was teardown/setup latency tripping the 10s default (not transient errors). A CI audit across recent runs found that every `@flaky`-marked test that actually flaked did so via `pytest-timeout`, all in the agent-create/clone/destroy/list/cleanup families that do real `mngr` work over tmux + subprocess. Affected tests: `test_cli_create_via_subprocess`, `test_clone_creates_agent_from_source`, `test_cleanup_destroy_single_agent`, `test_cleanup_destroy_with_provider_filter_matches`, `test_destroy_without_remove_created_branch_leaves_branch`, `test_list_command_with_{limit,limit_json_format,sort_descending,running_filter_alias,remote_filter_alias}`, `test_extras_claude_plugin_yes_flag`, `test_execute_cleanup_stop_on_online_host`, `test_list_agents_with_include_filter_excludes_non_matching`, `test_send_message_to_agents_with_include_filter`, and `test_create_with_update_flag_updates_existing_agent`. Reruns don't address latency, so a timeout bump is the appropriate remedy.

- Refresh a stale comment in `test_docker_state_transitions.py` that described the release tests as running "on release branch only". There is no `release` branch; these tests are marked `@pytest.mark.release` and run via the dedicated Release Tests workflow and TMR. No behavior change.

`mngr forward` gained an `--observe-via-file` flag: instead of spawning its own `mngr observe --discovery-only` subprocess, it tails the shared discovery events file written by another observer (e.g. the one `mngr latchkey forward` runs), so a host can run a single discovery observer. A new `tail_discovery_events_file` helper (a pure consumer that emits the latest cached snapshot then tails the log, without polling providers or writing snapshots) backs this and is shared with `run_discovery_stream`. The discovery event log now always lives under the default host dir: the internal `events_base_dir_override` config field was removed, and passing `--events-dir` together with `--discovery-only` to `mngr observe` is now a usage error (it never affected the discovery log in that mode). `--events-dir` still relocates the full observer's agent-state events.

Added `get_local_host(mngr_ctx)` to `imbue.mngr.api.providers` as the canonical way to obtain the local host (e.g. as an rsync/`copy_directory` source). Removed the duplicate `get_local_host` that lived in `imbue.mngr.cli.headless_runner`; callers now import it from `api.providers`. No user-facing behavior change -- this consolidates several copies of the same helper that had been independently reimplemented across plugins.

Added a `created_host(provider, host_name, **create_kwargs)` test context manager to `imbue.mngr.api.testing` that creates a host and destroys it on exit, replacing the create-host / try-finally-destroy boilerplate that recurs across provider tests. Test-only; no runtime impact.

Added a shared `upload_files_in_bulk` helper (`imbue.mngr.hosts.file_upload`) that transfers many files to a host in a single rsync (remote) or direct write (local), and routed `Host.provision_agent`'s user-upload and agent file-transfer loops through it instead of one `write_file` per file (a per-file SSH round-trip that did not scale -- github issue 1825). The rsync runs from the local machine via a new `copy_local_directory` host primitive (the source is implicitly the local machine, so no source-host object is threaded through `provision_agent` -- its signature is unchanged -- avoiding the host-layer-to-`api.providers` import cycle). The shared shell-library writes keep their per-file path because they need the executable bit, which the rsync staging helper does not preserve.

Fixed a deterministic `mngr create` regression on Modal (`AgentNotFoundOnHostError` immediately after provisioning). On Modal the host directory (`/mngr`) is a symlink into the mounted volume, and rsync without `--keep-dirlinks` deletes a receiver-side symlink-to-directory and replaces it with a real directory on the ephemeral filesystem when the source has a real directory at that path -- stranding `agents/<id>/data.json` (and all volume-backed state) behind the now-replaced symlink. `copy_local_directory` now passes `--keep-dirlinks` so rsync writes *through* such symlinks to the underlying storage, and `upload_files_in_bulk` rsyncs into the tightest common-ancestor directory of its destinations (hygiene, so it never stamps perms/mtimes on directories it is not deliberately writing into).

Fixed a `possibly-missing-submodule` type-checker warning in
`utils/cel_utils.py` by importing `MapType` directly from `celpy.celtypes`
instead of accessing it as `celpy.celtypes.MapType` (which relied on the
submodule being imported as a side effect). No behavior change.

## 2026-06-04

Discovery snapshots are now authoritative only for providers that succeeded on a given poll. The `FullDiscoverySnapshotEvent` contract changed: agents/hosts whose provider is in `error_by_provider_name` must be retained from prior consumer state (and surfaced as unknown/stale) rather than dropped, and are only removed on an explicit destroy event or a subsequent successful poll that omits them. Added a shared `partition_removed_agents_by_provider_error` helper that all discovery-snapshot consumers use to make this decision consistently. No discovery-event schema change.

- The e2e test suite under `libs/mngr/imbue/mngr/e2e/tutorial/` now has a release-marked pytest function for every executable command block in `mega_tutorial.sh`. `scripts/tutorial_matcher.py libs/mngr/imbue/mngr/resources/mega_tutorial.sh libs/mngr/imbue/mngr/e2e/tutorial` reports zero unmatched blocks and zero unmatched functions. Many new tests substitute lightweight command-type sleep agents for the tutorial's modal/claude examples so each test stays fast; substitutions are documented inline next to the `write_tutorial_block()` call.
- Additionally: pruned 5 stale duplicates from `tutorial/test_basic.py` (their canonical versions live in the per-section files), refreshed drifted `write_tutorial_block()` text in three existing modal/create tests, removed two tests whose corresponding tutorial blocks had been deleted or reduced to comments only (`test_create_modal_retry`, `test_create_with_transfer_git_mirror`), and moved `test_create_and_rename_agent` out of the tutorial subdir to the top-level e2e directory since the RENAMING AGENTS section is now informational-only.

- The tutorial-tied e2e tests under `libs/mngr/imbue/mngr/e2e/` (the ones with `write_tutorial_block()` calls corresponding to blocks in `mega_tutorial.sh`) have moved into a `tutorial/` subdirectory, so other e2e tests can live at the top level without the tutorial-matcher script (and other tools) treating them as tutorial gaps.

## 2026-06-03

`mngr create --format jsonl` (and every other command) now emits a structured
error record when a command fails:

```json
{"event": "error", "error_class": "FastPathUnavailableError", "message": "..."}
```

Previously a failing command only printed a human-formatted `Error: <message>`
line with no machine-readable type, so subprocess callers (e.g. minds) had to
substring-match the error text to detect specific failures -- which silently
broke when the error surfaced cleanly without the class name in a traceback. The
top-level CLI exception handler now calls `emit_error_event(...)` for real
errors (not control-flow exits like Ctrl-C / `--help`) when the resolved output
format is JSONL, attaching the exception's class name. `on_error` likewise
includes `error_class` in its JSONL error event when given the exception.

`mngr create --new-host` now tears down the host it just created if a later step
fails (provisioning, agent start, etc.), so a failed create never leaks the
host. Previously the only cleanup was removing the host lock so idle-shutdown
providers could reclaim the host on their own -- which never helped providers
that disable idle shutdown (e.g. imbue_cloud pool leases), leaving the host (and
its lease) stranded. The teardown is gated by the existing
`MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE=1`, which now retains the
failed host (not just its lock) for debugging.

`mngr observe --discovery-only` now honors `--events-dir`. Previously the flag was silently ignored for discovery-only mode, which always wrote and read the single shared discovery event log under `MNGR_HOST_DIR`. Now `--events-dir DIR` relocates the whole discovery stream (snapshots, host SSH info, and discovery errors) under `DIR`, so multiple discovery-only observers can run against the same host without their snapshots cross-contaminating each other's streams. Implemented via a new internal `MngrConfig.events_base_dir_override` field that only changes where event files live; provider/account/auth resolution still uses `default_host_dir`.

Regenerated the `mngr imbue_cloud` CLI reference docs to include the new operator-only `mngr imbue_cloud admin paid domain|email add|remove|list` commands for managing the connector's paid-user lists.

## 2026-06-02

Internal refactor with no user-visible behavior change. Removed the `emit_final_json` helper from `imbue.mngr.cli.output_helpers`, which was a one-line pass-through to the private `_write_json_line`. The underlying primitive is now public as `write_json_line` (sibling to the existing `write_human_line`), and all call sites use it directly.

The old name was misleading: despite "final JSON" implying the single terminating object emitted in `--format=json` mode, it was also called from the streaming JSONL callbacks (e.g. `mngr list`). `write_json_line` honestly describes what it does -- write one JSON object as a line -- for both the JSON and JSONL paths.

New `mngr stop --stop-host` flag: stops an agent's whole host (every
agent on it) instead of just the named agent.

- For container-backed providers `--stop-host` stops the container while
  the underlying machine keeps running; it is rejected up front on
  providers that do not support stopping hosts, and cannot be combined
  with `--archive`.
- `--stop-host` is idempotent: if the host is already offline it reports
  success instead of raising an error, so restarting an already-stopped
  workspace works.
- `--stop-host` now resolves the target host without SSH. Previously it
  failed with an `SSH error (Error reading SSH protocol banner...)` when
  the host's container was still running but its sshd was unreachable
  (sshd crashed, or PID exhaustion blocked new SSH sessions) -- one of
  the cases `--stop-host` is meant to handle. The host_id is now resolved
  from the discovery event stream and then fetched through the provider's
  own SSH-free `get_host` (which validates that the host still exists and
  supplies its name), so `mngr stop <agent> --stop-host` followed by
  `mngr start <agent>` reliably bounces an unresponsive workspace. A
  single SSH-free lookup against the one relevant provider both validates
  and names the host, so resolution does not scan every provider's hosts
  up front, and does not replay host discovered/destroyed events.
- This supports the minds tiered workspace-restart recovery flow, which
  uses a full host restart as its heavier recovery tier.
- When `--stop-host` targets multiple hosts, they are now stopped
  concurrently (via a concurrency-group executor) instead of one at a
  time, so the command no longer serializes on the slowest host. Output
  order is unchanged. If one host fails to stop, the others are still
  stopped before the error is raised (the previous sequential version
  aborted on the first failure, leaving later hosts running).

Fix `mngr list --format json` crashing on `ProviderErrorInfo` when no
agents were returned. With `--on-error continue` and a per-provider
failure, the empty-agents path passed raw `ErrorInfo` pydantic models to
`json.dumps`, which crashed with `TypeError: Object of type
ProviderErrorInfo is not JSON serializable`. The empty-agents path now
goes through the same `_emit_json_output` serializer as the non-empty
path, so `mngr list --on-error continue --format json` produces a clean
`{"agents": [], "errors": [...]}` payload instead of a traceback.

- Fix indefinite hang in git-push / rsync over SSH on macOS hosts where `SSH_AUTH_SOCK` routes to 1Password's biometric SSH agent. The shared `build_ssh_transport_command` (used by git push and rsync) now pins authentication to the explicit `-i` key via `-o IdentitiesOnly=yes -o IdentityAgent=none`. Without these flags, OpenSSH consults `SSH_AUTH_SOCK` first; in BatchMode (no TTY) the biometric prompt can never fire and ssh blocks forever on the agent reply.

`AgentError` (and all of its subclasses, e.g. `NoCommandDefinedError`, `AgentNotFoundOnHostError`,
`SendMessageError`, `AgentStartError`) now inherit from `MngrError` instead of `BaseMngrError`.
The remaining `BaseMngrError`-only error types -- `PluginSpecifierError`,
`DiscoverySchemaChangedError`, `MalformedJsonlLineError`, `TolerantPathError`, and
`IssueSearchError` -- were moved the same way. This completes the consolidation of the error
hierarchy under a single user-facing parent class: every mngr error is now a `ClickException`,
so when one reaches the CLI it renders as a clean `Error: ...` message (plus any help text)
instead of a Python traceback, and `except MngrError` handlers treat them as the user-facing
errors they are.

The now-redundant `MngrError` mix-in on `AgentNotFoundError` and `DuplicateAgentNameError`
(which already reached `MngrError` via `AgentError`) was removed; both still behave identically.

The `BaseMngrError` base class has been removed entirely. `MngrError` now inherits directly
from `click.ClickException`, and every mngr error inherits from `MngrError`. There is no longer
a separate non-user-facing error tier: all mngr errors render as a clean `Error: ...` message
at the CLI (plus any help text) rather than a traceback. This is a no-op for users -- prior
commits had already moved every error class under `MngrError`; removing `BaseMngrError` simply
finalizes that consolidation.

`except` clauses that listed an error type already covered by another type in the same clause
were collapsed (e.g. `except (MngrError, UserInputError)` -> `except MngrError`,
`isinstance(e, (MngrError, BaseMngrError))` -> `isinstance(e, MngrError)`). Clauses pairing
`MngrError` with unrelated types (`OSError`, `docker.errors.*`, etc.) are unchanged.

`HostError` (and all of its subclasses, e.g. `HostConnectionError`, `HostOfflineError`,
`HostAuthenticationError`, `CommandTimeoutError`, `HostDataSchemaError`) now inherit from
`MngrError` instead of `BaseMngrError`, consolidating the error hierarchy under a single
user-facing parent class. Host errors are now `ClickException` instances, so when one reaches
the CLI it renders as a clean `Error: ...` message (plus any help text) instead of a Python
traceback, and `except MngrError` handlers treat them as the user-facing errors they are.

The base `get_host_and_agent_details` now re-raises `HostConnectionError` from its per-agent
guard so that, even though `HostConnectionError` is now a `MngrError`, a connection failure
still reaches the host-level handler that clears the connection cache and falls back to the
offline view instead of being swallowed per-agent.

Install Node.js in the shared mngr image (`resources/Dockerfile`), pinned to `apps/minds/package.json`'s `engines.node` (24.15.0). Node is a runtime dependency of the mngr_latchkey gateway's `.mjs` extensions, and is also used by minds Python tests that evaluate `apps/minds/todesktop.js` via `node`. With Node in the image, those tests run on offload instead of being silently skipped.

## 2026-06-01

# Make `CreateAgentOptions.agent_type` required

- `CreateAgentOptions.agent_type` is now a required field (previously
  `AgentTypeName | None` defaulting to `None`). Following the removal of
  the CLI's implicit `claude` default, the residual `agent_type or
  AgentTypeName("claude")` fallbacks in `api.create.create` and
  `Host.create_agent_state` were the last places that silently defaulted
  an unset type to `claude`. Both fallbacks are gone, and the type system
  now guarantees every agent-creation path supplies a concrete type. The
  now-dead `if options.agent_type is not None:` guard around agent-type
  provisioning merging in `Host.provision_agent` was also dropped.

Marked `test_list_command_with_limit` as flaky so offload retries it automatically.

The `on_before_create` and `on_before_host_create` plugin hooks now receive the `MngrContext` as a parameter, giving plugins access to config, the plugin manager, and the concurrency group. Plugins implementing these hooks must add a `mngr_ctx` parameter to their signatures.

Plugins can now contribute standalone help topic pages via the new `register_help_topics` hook. Topics from an installed plugin show up in `mngr help` and are viewable via `mngr help <topic>`, just like mngr's built-in topics. Each topic is a `TopicHelpPage` with explicit metadata (key, description, aliases, see-also) whose body is either `InlineContent(markdown=...)` or `DocFile(path=...)`. Plugin topics that collide with a built-in topic key or alias are skipped so built-in topics always win.

`mngr help <topic>` now renders markdown nicely in an interactive terminal (headings, bold, code, links, and tables) via `rich`, with paragraphs wrapped to the terminal width; the same rendering is applied to command `--help` description and sections. Non-interactive output (pipes, scripts) stays plain. `rich` is imported lazily so it does not affect CLI startup time.

Built-in topic docs are now shipped inside the wheel (`force-include` of the topic doc dirs), fixing a bug where `mngr help <topic>` showed no doc-based topics in a PyPI/wheel install (only the top-level `docs/` tree, which is not packaged, was previously read at runtime).

Tab completion suggests every command and help topic as an argument to `mngr help` (e.g. `mngr help <TAB>` lists `create`, `address`, and any plugin-contributed topics).

Internally, the plugin-facing `TopicHelpPage` model lives in `imbue.mngr.interfaces.help_topic` (so the plugin hookspec can reference it without the plugins layer importing the CLI), while the runtime topic registry lives in `imbue.mngr.cli.help_topics`. mngr's own built-in topics are registered through this same hook (as a built-in plugin) from an explicit registry -- no directory scanning or heading parsing.

In an interactive terminal, `mngr help <topic>` now makes relative and anchor links inside doc-backed topics clickable. Previously a link like `[Idle Detection](idle_detection.md)` or `[a section](#user-input-tracking)` rendered as a dead terminal hyperlink (its relative target means nothing to a terminal or browser).

Each topic's `DocFile` now carries a `source_url` (the doc's canonical GitHub blob URL, pinned to the installed release tag, e.g. `.../blob/v0.2.9/...`, falling back to `main` when the version can't be read). At display time, relative and anchor links are resolved against that URL with `urljoin` (`#anchor` -> `<doc-url>#anchor`, `sibling.md` -> the sibling's URL, `../README.md#x` -> the parent's URL), so the rendered terminal hyperlinks open the right GitHub page/section. Already-absolute links (`https:`, `mailto:`) are left untouched, and plain non-terminal output (pipes, scripts, the doc generator) keeps the original relative links.

Plugins get this for free: a plugin that builds its `DocFile` with a `source_url` (in-repo plugins can use the new `imbue_mngr_doc_url` helper) gets the same clickable-link rewriting; one that omits it simply renders its links unchanged.

# Offline agent field generators

Implemented the plugin hook previously documented as the planned `get_offline_agent_state`, now named `offline_agent_field_generators` to mirror the existing online `agent_field_generators` hook.

- Plugins can now contribute `plugin.<plugin_name>.<field>` data for agents whose host is offline or unreachable. Each generator receives the offline `(DiscoveredAgent, HostDetails)` (rather than the live `(agent, host)` the online hook gets) and computes fields from the cached `data.json` exposed via `DiscoveredAgent.certified_data`. `None` field values are omitted and empty plugins are dropped, exactly like the online path.
- `mngr list` collects these generators and threads them through `get_host_and_agent_details` to `build_agent_details_from_offline_ref`, so offline plugin fields are usable in `mngr list` columns and CEL filters just like online ones.
- Discovery snapshots now preserve plugin fields: `discovered_agent_from_agent_details` carries `AgentDetails.plugin` into the reconstructed `certified_data`, so offline generators can still read plugin state for fully-unreachable hosts that fall back to a persisted snapshot.
- Updated the plugins concept doc to document `offline_agent_field_generators` and remove the `[future]` `get_offline_agent_state` placeholder.
- Test infrastructure: the `assert_home_is_temp_directory` safety check now also accepts `/private/tmp`, so tests run when `TMPDIR` points into `/tmp` (which macOS realpath-resolves to `/private/tmp`) rather than only the launchd `/var/folders` default.

## 2026-05-30

Tolerate per-host SSH failures during provider agent enumeration.

A single unreachable host (sshd hang, banner reset, auth failure) used to make the default `discover_hosts_and_agents` raise a `HostConnectionError` from the per-host futures loop. That bubbled up to `_construct_and_discover_for_provider` and was recorded as a whole-provider failure, so the resulting `DISCOVERY_FULL` event reported `agents=[]` / `hosts=[]` for the entire provider. Downstream, `mngr_forward`'s resolver blanked its known-agents set and every workspace on that provider became unreachable through the forward plugin -- so one broken Docker container could 503 every other workspace on the same daemon and trip minds' recovery page even for perfectly healthy workspaces.

The default `discover_hosts_and_agents` now catches `HostConnectionError` (and its `HostAuthenticationError` / `HostOfflineError` subclasses) per host and recovers the broken host's agents from the provider's offline view (described below) so the rest of the provider's hosts (and their agents) come through normally. The except block also calls `self.on_connection_error(host_id)` -- matching the contract honored elsewhere in the file -- so providers that cache per-host state (docker's container cache, modal/lima/vps_docker host caches) drop the wedged entry instead of replaying the same stale handle on the next discovery cycle.

Per-host connection errors fall back to the provider's offline view (`to_offline_host(host_id).discover_agents()`). This means a docker container whose sshd has died but whose process is still RUNNING preserves its agents in discovery, mirroring the behavior of a fully-stopped container -- the workspaces stay visible on minds' landing page instead of vanishing on the first SSE poll after sshd dies. The fallback assumes any provider whose hosts can raise `HostConnectionError` implements `to_offline_host`: the local provider never opens a connection (so it never reaches this path), and remote providers persist host/agent state. If `to_offline_host` instead fails -- `NotImplementedError` (no offline view) or `HostNotFoundError` (no persisted record for a host that was just discovered) -- that signals a broken invariant and is allowed to propagate as a per-provider `ProviderDiscoveryError` rather than being masked as an empty agent list. The SSH provider does not yet implement `to_offline_host` (tracked by a FIXME on the class), so an unreachable SSH host currently surfaces as such an error.

## 2026-05-29

- Docker provider: the per-host build image (`mngr-build-<host_id>`) is now untagged when a host is destroyed (and again, defensively, when it is deleted), so built images no longer pile up in `docker images`. Snapshot restore is unaffected -- snapshot images keep their own layers.

Regenerated the CLI reference docs to include the new `mngr imbue_cloud bucket`
command group (R2 bucket + scoped-key management) added by the mngr_imbue_cloud
plugin.

`mngr destroy` now actually destroys the host when the last agent on it
is destroyed -- the documented contract -- regardless of how recently the
host was created. Previously this fired through the post-destroy GC pass,
which gates on `min_online_host_age_seconds` (default 10 minutes), so any
host destroyed within minutes of creation leaked its cloud-side resources
(e.g. an active imbue_cloud lease, a Vultr VPS) until the 7-day
destroyed-host grace period eventually triggered `provider.delete_host`.

Two changes in `destroy.py`:

1. **Partition step reconciles discover-vs-on-host disagreement.** When
   every matched agent is a "ghost" -- returned by the provider's discover
   but absent from the host's own `get_agents()` -- the destroy CLI now
   escalates to host-level destruction (`provider.destroy_host`) instead
   of silently dropping the match. This is what was producing the
   "No agents found" message when the same agent was destroyed twice on
   an imbue_cloud-leased host: the first destroy removed `/mngr/agents/<id>/`
   on the VPS but the connector's lease list still reported the agent.

2. **Post-loop sweep destroys hosts whose last agent was just destroyed.**
   For each online host that had at least one agent destroyed in this
   invocation, the destroy CLI now re-checks `host.get_agents()` and, if
   empty, calls `provider.destroy_host` directly. Bypasses the GC's
   `min_online_host_age_seconds` filter; the GC pass that runs immediately
   after is the safety net for transient failures.

Net effect: cloud-side resources are released the moment `mngr destroy`
returns, and the destroyed-host grace period only retains historical
state -- aligning all provider types with the same semantic that the
docker / mngr_vps_docker / imbue_cloud `destroy_host` implementations
already implement individually.

## New: `--post-host-create-command`

`mngr create` learns a new repeatable flag, `--post-host-create-command`,
that runs one or more shell commands inside a newly-created host
synchronously after the host is online but before any agent work_dir is
touched. Each command runs in order via the host's normal exec path; a
non-zero exit aborts the create. Stackable from `create_templates.<name>`
via `post_host_create_command__extend = [...]`.

Motivation: an image may need first-boot setup (e.g.
forever-claude-template seeds a baked workspace from `/docker_build_code`
onto its `/mngr/` volume) that must complete before mngr's git mirror
push or any other work_dir setup. Until now this had to be encoded in the
container's `CMD`, which raced against mngr's `docker exec` calls and
required an FCT-specific `use_image_default_cmd` opt-out in
`mngr_vps_docker` / `providers.docker`. The opt-out and the
defensive `--workdir /` exec override (from commits `d77714cdf` /
`55c420c35`) are reverted in the same commit -- the new generic hook
replaces both.

Install `restic` in the mngr Docker image (`libs/mngr/imbue/mngr/resources/Dockerfile`).

This is the offload test image; the minds app now requires `restic` on the
machine running it (it initializes each workspace's backup repository
itself), and its tests exercise a real local restic repository, so restic
must be present in the test environment.

`build_check_and_install_packages_command` (in `providers/ssh_host_setup.py`) now
`mkdir -p` the symlink target before creating the `host_dir` symlink. The local
`docker` provider already creates that subdirectory eagerly so it's a safe no-op
there; the new docker_vps unified-volume layout relies on the in-script mkdir to
seed `<volume>/host_dir` before pointing `/mngr` at it.

## 2026-05-28

# `is_allowed_in_pytest` now defaults to False, and the `MNGR_ALLOW_PYTEST` escape hatch is gone

The `is_allowed_in_pytest` config field now defaults to `False` (previously
`True`). During a pytest run, `load_config` refuses to run when a config file is
loaded that does not set `is_allowed_in_pytest = true` -- and every config layer
(user/project/local) is checked individually, so a real config can't ride in
under a test config that opts in. If no config file is picked up at all, there
is nothing to protect against and mngr runs normally. This makes the guard
secure by default: a real config (the developer's `~/.mngr` or the repo's
`.mngr/settings.toml`) loaded by a poorly-scoped test now trips the guard
instead of being used to perform real operations, while configs written
specifically for tests opt in explicitly.

The `MNGR_ALLOW_PYTEST=1` environment variable, which used to bypass the guard
entirely, has been removed. It had a single user, and the existence of such a
variable was not worth the risk of it being reached for as a quick bypass
instead of properly fixing a test with a leaky environment.

# Corrected `is_error_reporting_enabled` config field description

Separately, the `is_error_reporting_enabled` field description was out of date
(it described prompting to file GitHub issues); it now matches the actual
behavior -- suggesting a diagnostic agent on an unexpected interactive error.

## Clearer settings-narrowing errors

- The "settings narrowing detected" error now names **both** implicated layers for each offending key: the file doing the (narrowing) assignment and the lower-precedence file whose value would be dropped. Each side shows the resolved file path and the matching `mngr config set --scope <user|project|local>` flag, so it is immediately clear which configs conflict (instead of just an opaque layer label and the dotted key). The `MNGR__*` env-var layer is named as such (it has no `config set` scope).
- `--setting allow_settings_key_assignment_narrowing=...` is now rejected with a clear error. That flag controls the narrowing guard, which runs while the settings files and env vars are loaded — before `--setting` is applied — so a `--setting` value could not take effect there. The error points you to set it in a `settings.toml` or via `MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING=true` instead. (Previously the narrowing error misleadingly suggested `--setting` as a remedy, and a `--setting` value for the flag was silently accepted without affecting that guard.)

### Restructure `mngr push` and `mngr pull` into `mngr rsync` and `mngr git push`/`mngr git pull`

The experimental `mngr push` and `mngr pull` commands combined three different
primitives (rsync, git push, git pull) behind `--sync-mode={files,git}` and
`--rsync-only` flags. They are replaced by three thin primitives that each wrap
a single operation:

- `mngr rsync SOURCE DESTINATION` — wraps rsync. Exactly one of `SOURCE` /
  `DESTINATION` must reference a remote agent or host; the other must be a
  local path. Local-to-local and remote-to-remote transfers are rejected.
- `mngr git push TARGET [-- GIT_ARGS...]` — thin wrapper around `git push`
  from the current working directory's repo to a remote agent or host's repo.
  Anything after `--` is passed verbatim to the underlying `git push`.
- `mngr git pull SOURCE [-- GIT_ARGS...]` — thin wrapper around `git pull`
  from a remote agent or host's repo into the current working directory's
  repo. Anything after `--` is passed verbatim to the underlying `git pull`.

The git push/pull commands are thin pass-through wrappers: mngr resolves the
endpoint, builds the SSH URL with mngr's managed credentials, sets
`receive.denyCurrentBranch=updateInstead` on push targets, and adds a
`safe.directory` entry — then runs vanilla `git push` / `git pull` with any
flags the user supplies after `--`. The mngr-side flags
`--source-branch`/`--target-branch`/`--mirror`/`--uncommitted-changes`/`--dry-run`
are gone; use the corresponding git flags directly (`feature:main` refspec
syntax, `--force --tags refs/heads/*:refs/heads/*` for a mirror push,
`--dry-run`, `--rebase`, etc.).

`mngr push` and `mngr pull` are removed (no compatibility shim).

API-level changes in `imbue.mngr.api.sync`: `pull_files`/`push_files`/`pull_git`/`push_git`
are replaced by `rsync_from_remote`, `rsync_to_remote`, `git_pull`, and
`git_push`. There is also a top-level `rsync(source_host, source_path,
destination_host, destination_path, ...)` for the two-endpoint shape used by
the CLI. `git_push`/`git_pull` now take an `extra_args: Sequence[str]`
parameter and have no structured return value (raise `GitSyncError` on
failure). The `SyncMode` enum, `GitSyncResult`, and `NotAGitRepositoryError`
are gone; `SyncFilesResult` is renamed to `RsyncResult`.

# Offload pin bump to v0.9.6

Bumped the offload version baked into `libs/mngr/imbue/mngr/resources/Dockerfile`
from `0.9.5` to `0.9.6` to keep the in-image offload binary in lockstep
with the CI pin. v0.9.6's headline feature is the new
`offload run --override-image-id <ID>` CLI flag (Modal provider only),
which lets a test run skip offload's image-setup pipeline entirely and
boot from a pre-built Modal image. See
https://github.com/imbue-ai/offload/releases/tag/v0.9.6 for the full
release notes.

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

## Settings narrowing safety net: two false-positive fixes

- A brand-new `[create_templates.<name>]` block whose only ``<opt>__extend = [...]`` entry was introduced in a single layer used to silently lose its `__extend` suffix at config-load time (`resolve_extends` resolved against a `None` base lookup and stored a bare assign). At `mngr create --template <name>` time the option was then treated as a bare assign and tripped the narrowing guard against the create command's runtime params, even though the user had written `__extend`. The suffix is now preserved verbatim inside a template's options when the base has nothing to extend, so `apply_create_template` can still apply it as an extend against the runtime params.
- `agent_types.<name>.cli_args = "..."` (string form) used to be shell-tokenized into a tuple before the narrowing check ran, so two layers that each supplied a string with different tokens tripped the narrowing guard against each other's individual tokens. Strings represent a coherent single value (scalar replacement intent), not an aggregate, so the narrowing check now exempts tuples that were normalized from string-form values. This applies to every `tuple[str, ...]` field on `AgentTypeConfig` that accepts the string shorthand (`cli_args`, `env`, `env_file`, `extra_provision_command`, `upload_file`, `create_directory`).

- Added `isolate_host_volumes` to the Docker provider config. When set to `True`, each host container only sees its own per-host sub-folder of the shared state volume (via `--mount ... volume-subpath=...`, requires Docker Engine >= 25.0), fixing the cross-host visibility that today's shared mount has. The choice is persisted per host so existing hosts keep the strategy they were created with.
- Left at its default of unset (treated as today's shared-volume behavior), the provider now emits a one-shot deprecation warning at startup noting that the default will flip to `True` in a future release. Set `isolate_host_volumes = false` explicitly to silence the warning while keeping today's behavior, or `isolate_host_volumes = true` to opt in to isolation now.

# ty 0.0.39 / paramiko 4.0 / coolname 5.0 type fixes

- Converted bracketed `# type: ignore[...]` suppressions to `# ty: ignore[...]`, as required by `ty` 0.0.39.
- coolname 5.0 widened `RandomGenerator`'s config type; the name-generator config dicts are now annotated with `coolname.CoolnameConfigT` so they type-check (coolname's value type is invariant).
- The new `types-paramiko` stubs (from the paramiko 4.0 bump) surfaced several paramiko usages in `outer_host`:
  - `_get_paramiko_transport` / `_create_sftp_client` are now typed as returning/accepting `paramiko.Transport` (was `object`).
  - The private `_put_file*` helpers are narrowed from `str | IO[str] | IO[bytes]` to `str | IO[bytes]`; only `IO[bytes]` was ever passed, and `SFTPClient.putfo` requires bytes.
- The e2e `pytest_runtest_makereport` hookwrapper's generator send type is now annotated as `pluggy.Result[pytest.TestReport]`, so `outcome.get_result()` resolves.
- Intentional reaches into paramiko internals (the `Transport._log` logging monkeypatch and a `Channel._send` access in a test that manufactures a traceback) are annotated with `# ty: ignore[unresolved-attribute]`.

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

# Regenerated CLI docs

The generated `mngr latchkey` command reference
(`libs/mngr/docs/commands/secondary/latchkey.md`) was regenerated to match the
command's current help metadata. A new CI check now fails if any generated CLI
doc drifts from `uv run python scripts/make_cli_docs.py` output.

Replaced the `gemini` entry in `PLUGIN_CATALOG` and `plugin_isolation` test fixtures with `antigravity`. The `AntigravitySignalCheck` detects the new CLI via `agy --version`. Touched-up some prose references in `mngr` resource scripts and docs to drop or refresh mentions of `mngr_gemini` now that the plugin has been renamed.

## Breaking: unified settings overrides

Any mngr config field can now be overridden from a single, unified mechanism:

- **`MNGR__X__Y__Z=value` env vars** (note the double underscores) target the dotted path `x.y.z`. This replaces the narrow `MNGR_COMMANDS_<CMD>_<PARAM>` scheme and frees plugin / CLI command names to contain multiple words. `MNGR_COMMANDS_*` is **removed**.
- **`--setting x.y.z=value`** and **`mngr config set x.y.z value`** continue to work and now go through the same resolver.
- **`__extend` operator suffix on a leaf key** (e.g. `MNGR__AGENT_TYPES__MY_CLAUDE__CLI_ARGS__EXTEND='["--model","opus"]'`) opts into additive behavior: append for lists/tuples, shallow key-merge for dicts, union for sets. The bare key is always assignment.
- **`mngr config extend KEY VALUE`** writes the `__extend` form; **`mngr config set KEY__extend VALUE`** is accepted as an alias.
- **`mngr config schema`** lists every settable key with type and current effective value; **`mngr config list --all`** includes default-valued fields too.

### Breaking changes you'll notice

- **Layer merging is now assign-by-default for every aggregate** (list, tuple, dict, set). Older configs that relied on implicit concat across user/project/local files (e.g. `cli_args` accumulating) now need an explicit `cli_args__extend = [...]` to keep the additive behavior. The five top-level container dicts on `MngrConfig` (`agent_types`, `providers`, `plugins`, `commands`, `create_templates`) keep their per-key merge — adding `[agent_types.foo]` in one scope still doesn't drop another scope's `[agent_types.bar]`. `disabled_plugins` is a separate carveout: it is populated by `--disable-plugin` CLI flags rather than TOML files, and an empty override preserves the base value (use `[plugins.<name>] enabled = false` to disable a plugin per-scope).
- **Agent-type parent-type inheritance** likewise stops auto-concatenating `cli_args` / `extra_provision_command` / `upload_file` / `create_directory` / `env` / `env_file`. Use `field__extend` to inherit-and-extend.
- **Removed env vars:** `MNGR_COMMANDS_<CMD>_<PARAM>`, `MNGR_ENABLE_PARAMIKO_LOGGING`, `MNGR_AGENT_READY_TIMEOUT`. These are promoted to first-class config fields (`logging.enable_paramiko_logging`, `agent_ready_timeout`) and remain settable via `MNGR__*`.
- **`MNGR_COMPLETION_CACHE_DIR` stays as-is** (single underscore). It's read by the tab-completion lightweight pre-reader path that intentionally skips full config loading, so it joins the "special" env vars (`MNGR_ROOT_NAME` / `MNGR_PREFIX` / `MNGR_HOST_DIR`) rather than becoming a config field. The double-underscore `MNGR__COMPLETION_CACHE_DIR` form is not recognised.
- **Renamed:** `MNGR_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE` → `MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE`.
- **Preserved aliases:** `MNGR_ROOT_NAME`, `MNGR_HOST_DIR`, `MNGR_PREFIX`, and `MNGR_HEADLESS` continue to work. Setting both an alias and its canonical `MNGR__*` form to different values raises `ConfigParseError`.
- **Field name restrictions:** field names can no longer contain `__` (reserved as the env-var segment separator and `__extend` operator). Sibling keys that lowercase-collapse to the same env-var segment now raise at config-load time.

No compatibility shim is provided; the major-version bump is the migration signal.

## Narrowing safety net and CLI-flag extension

- CLI tuple/list flags (e.g. `--env`, `--label`, `--extra-window`) now extend the merged settings value rather than replace it, with the config-supplied entries first and the CLI-supplied values appended. Matches the "settings file → CLI" precedence so users can layer additional values on top of a settings-supplied list.
- `--setting commands.<name>.<param>__extend=...` and `--setting create_templates.<name>.<param>__extend=...` now correctly extend the merged value stored inside the per-entry `defaults` / `options` mapping (previously fell through to `None` and silently acted as a plain assign for these wrapper models).
- The `__extend` operator and the narrowing safety net both apply uniformly to `agent_types.<name>.<field>`, `providers.<name>.<field>`, `create_templates.<name>.<field>`, and `plugins.<name>.<field>` -- not just to top-level `MngrConfig` fields. Adding a brand-new entry in a higher layer is always a pure addition (no narrowing); replacing or clearing a non-empty aggregate value within an existing entry follows the same opt-in / `__extend` workaround rules as the top-level fields.
- Create templates (applied at command time via `--template <name>`) also follow the assign-by-default / `__extend` / narrowing rules. Previously templates concatenated tuple options by default, which silently mixed CLI values into the template's narrowing base and made `--template a --template b` chains hard to reason about when both wrote the same key. Now templates assign-by-default and a template's bare-assign over a non-empty value raises `ConfigParseError` unless `allow_settings_key_assignment_narrowing = true`; opt-in to additive behavior with `[create_templates.<name>] env__extend = [...]`. The pipeline order is now `config_defaults -> templates -> CLI`, so a CLI flag value always appends at the end of the merged list, after any template extension.
- Added a new top-level `allow_settings_key_assignment_narrowing` setting (default `false`). When `false`, a higher-precedence settings layer that would assign over a non-empty list/tuple/dict/set value from a lower-precedence layer with anything that doesn't preserve every prior entry raises `ConfigParseError` instead of silently dropping entries. The error tells the user how to opt in (set the field to `true`) or keep the additive behavior for the specific key (use the `__extend` suffix). The default is expected to flip to `true` in a future version, and support for `false` may be removed entirely once the migration is complete. Only no-ops (override equals base) and supersets (every base entry survives, e.g. an `__extend` result) pass without flagging — clearing (`env = []`) is treated as the most extreme form of data loss and must be explicitly opted in. Layers that don't write the field at all never trigger the guard.

Fix a class of bugs where tmux commands silently misroute to the wrong agent's session under prefix collision.

When `tmux ... -t name` is invoked and no session named exactly `name` exists, tmux falls back to *session-name prefix matching* and routes the command to any live session whose name starts with `name`. If two agents have names where one is a prefix of the other (e.g. `gemini` and `gemini-to-antigravity`), then when the shorter-named agent is torn down, every subsequent `-t gemini` lookup silently lands on `gemini-to-antigravity` instead of failing. Possible consequences include:

- `kill-window` / `kill-session` tearing down the wrong agent's session
- `send-keys` / `paste-buffer` delivering input to the wrong agent
- `capture-pane` reading the wrong agent's screen
- Lifecycle checks misreporting a stopped agent's state (the symptom that first surfaced this — a stopped agent shown as `WAITING` because the check landed on a live sibling's pane)
- Background-task polling loops never terminating

Changes:
- Introduce `TmuxSessionTarget` and `TmuxWindowTarget` Pydantic classes in `imbue.mngr.hosts.tmux` whose `.as_shell_arg()` renders the `-t` argument with a leading `=` (tmux's exact-match prefix), and for window/pane commands the required explicit `:window` component.
- Route every tmux `-t` call site through the helpers: lifecycle check, send-keys / paste-buffer / capture-pane in `BaseAgent`, post-attach resize script in `connect.py`, `_build_start_agent_shell_command` in `host.py`, rename / kill / has-session paths, the `listing_utils` remote-listing script, and the TUI input pipeline.
- `build_post_attach_resize_script` now iterates windows so SIGWINCH reaches every pane in every window (previously only the active window's). Side effect of the refactor; not strictly required for the prefix-matching fix.
- Update `cleanup_tmux_session` (in `utils/testing.py`) to match the new `=<session>:0` exact-match form when pkill-cleaning orphaned activity monitors — the old substring no longer appeared in the monitor's command line after the helper refactor.
- Add unit tests in `hosts/tmux_test.py` covering the helpers' rendering contract. Live behavioral coverage of the polling-loop-never-terminates failure mode lives in the per-project regression tests under `libs/mngr_claude` and `libs/mngr_gemini`.

## 2026-05-22

## Discovery snapshots now carry per-provider state

- `FullDiscoverySnapshotEvent` (the JSONL event emitted by `mngr observe --discovery-only`) has two new fields: `providers` (providers that loaded successfully) and `error_by_provider_name` (providers whose discovery raised). Old snapshots parse cleanly (the new fields default to empty); new snapshots will trip `DiscoverySchemaChangedError` in older builds of `mngr_forward` / `mngr_latchkey` / `mngr_notifications` until those are rebuilt.
- `mngr observe --discovery-only` now emits a `FullDiscoverySnapshotEvent` on every poll, even when zero providers succeeded. Per-provider failures land in `error_by_provider_name`; consumers treat the snapshot as authoritative and drop any previously-known agents/hosts whose provider is now errored.
- `mngr list` no longer skips its side-effect snapshot when some providers failed; the snapshot now includes the per-provider error info. Snapshots are still skipped when a non-provider-attributable error happens at the top level of `list_agents`.
- Bug fix: the outer `mngr observe` (the multi-host observer) used to spawn its inner `mngr observe --discovery-only` child with an unsupported `--on-error continue` flag, killing the child on every startup. The flag is now gone.

## New `UNKNOWN` agent / host lifecycle state

- `AgentLifecycleState` and `HostState` both grow an `UNKNOWN` value, defined as "the provider that owns this agent/host could not be accessed during the most recent discovery attempt."
- `AgentObserver` now emits an UNKNOWN entry in its `FullAgentStateEvent` for any previously-observed agent whose provider just failed discovery (sticky: agent stays UNKNOWN until it reappears in a snapshot or its provider is removed from config). Agents whose provider falls out of configured set entirely are dropped from tracking instead.
- `mngr list` does NOT show UNKNOWN -- it remains stateless and only shows what its own listing returned.

## Retry semantics

- The discovery polling path no longer retries failures at the top level. Providers are responsible for retrying their own transient failures before raising; the snapshot reflects whatever they reported.

## 2026-05-21

Removed the `mngr provision` (aka `mngr prov`) subcommand and its docs. Provisioning still runs automatically during `mngr create`; the `--extra-provision-command`, `--upload-file`, and env-related flags on `mngr create` continue to work as before.

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

- `mngr create --provider lima` docs now show `--memory=N` / `--disk=N` (plain integers, no `GiB` suffix), matching what `limactl start` expects.

Show the full agent name in the tmux status bar.

User-visible changes:

- mngr's generated tmux config (`~/.mngr/tmux.conf`) now sets
  `status-left-length` to 20 so a full `mngr-...` session name shows in the
  status bar. Previously tmux's default of 10 truncated names like
  `mngr-tmux-display` to `[mngr-tmux`, with the window list mashed onto the end.
- The widening is written before the user's `~/.tmux.conf` is sourced, so a
  `status-left-length` set in the user's own config overrides it.

`Host._get_all_descendant_pids` now tracks a `visited` set so a PID-reuse cycle in the process tree (a long-lived pid X dies and is recycled as a descendant of one of its own descendants) can no longer drive the walker past Python's recursion limit. This unsticks `host.stop_agents` on long-lived agents' cleanup paths, which previously crashed with `RecursionError` and skipped the actual stop.

## 2026-05-20

Renamed the mngr-side "workspace server" feature to "system interface", matching the upstream rename of the `minds_workspace_server` package to `system_interface` in `forever-claude-template`. The HTTP endpoint `/api/agents/{id}/restart-workspace-server` became `/api/agents/{id}/restart-system-interface`, and the SSE event type `workspace_server_status` became `system_interface_status`.

## Provider gating: only `mngr create` may bootstrap host-creation state

`mngr list`, `mngr gc`, and other read flows no longer silently bootstrap
provider-side state just because a provider is enabled. Plumbed through a new
`is_for_host_creation: bool = False` parameter on
`ProviderBackendInterface.build_provider_instance` / `api.providers.get_provider_instance`,
which all backends accept and ignore by default. `mngr create` passes `True`;
every other path leaves the default. Providers that can't initialize without
their environment (e.g. Modal) now raise `ProviderUnavailableError`, which
higher-level loaders skip.

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

# Consistent agent address resolution across single-agent subcommands

Refactored how single-agent subcommands turn an `AgentAddress` into the live
interfaces they operate on. The "find" stage (discovery + matching against
the address) is now strictly separate from the "ensure live" stage (bringing
the host online, looking up the live agent, optionally starting it).

Two new helpers in `imbue.mngr.api.find` replace the previous
`is_start_desired` / `skip_agent_state_check` flags on
`find_one_agent` / `find_agent_for_command`:

- `resolve_to_started_host_and_agent`: bring the host online and resolve
  the agent ref to an `AgentInterface` without checking the agent's
  lifecycle state. Used by `push`, `pull`, `provision`, and `rename`.
- `resolve_to_started_host_and_running_agent`: as above, but also
  require / auto-start the agent process. Used by `connect` and `capture`.

Both helpers take a single `allow_auto_start` flag (driven by `--start`).

User-visible changes:

- `push`, `pull`, and `provision` no longer require the agent to be
  running. Previously they failed when targeting a stopped agent on an
  online host; now they operate on stopped agents directly.
- `push`, `pull`, `provision`, and `rename` gain a `--start/--no-start`
  flag (default `--start`) that controls whether an offline host is
  started automatically.
- The `--start` help text on `connect`, `capture`, and `exec` has been
  reworded to reflect what `--start` actually starts in each command.
- `mngr connect` no longer falls back to "most recently created agent"
  when run non-interactively without an explicit agent. It now matches
  every other single-agent command: pass an agent name, or run it from
  an interactive terminal to use the selector.
- Cancelling the interactive agent selector now exits cleanly via
  `click.Abort` instead of printing nothing and returning silently.

- `mngr list` no longer aborts with "Provider 'modal' is not available"
  when the Modal per-user environment hasn't been created yet. The
  Modal backend now raises a new `ProviderEmptyError` (distinct from
  `ProviderUnavailableError`) when its env doesn't exist, and the
  listing pipeline silently skips empty providers in every mode
  (streaming + batch, ABORT + CONTINUE). Semantically: empty means
  "the backend answered that there's nothing here" and is always safe
  to drop from a listing; unavailable means "we couldn't ask" and may
  still warrant an error.

Support a shared Modal env across an offload-acceptance / offload-release
run (opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`). `imbue.mngr.utils.testing`
gains a `read_shared_modal_env_name` helper that returns the shared env
name when the env var is set (and a non-empty dash-suffixed value), or
`None` otherwise. Used by the modal test fixtures to skip per-sandbox env
creation/deletion and route all tests into a single pre-created env, so
fanned-out offload runs stay well under Modal's per-workspace env cap.
Local pytest behavior (no env var set) is unchanged.

# `HasTranscriptMixin` formalises the raw-capture contract for `mngr transcript`

A new `HasTranscriptMixin` on `AgentInterface` formalises the raw-capture
contract; `HasCommonTranscriptMixin` extends it with the (gated) common
converter on top. Future agent types get `mngr transcript` support for free
by implementing `get_raw_transcript_scripts` + `get_common_transcript_scripts`
and shipping the matching per-agent scripts.

Fix `mngr config` help text and docs example: the example showed `--user` but the actual option is `--scope user`.

# Rename `HostedLocation` to `HostLocationAddress`

Renamed the address-side `HostedLocation` type to `HostLocationAddress` so its
name matches its peers (`HostAddress`, `AgentAddress`) and makes its
relationship to the runtime `HostLocation` type explicit.

Cascading internal renames:

- `parse_hosted_location` -> `parse_host_location_address`
- `resolve_hosted_location` -> `resolve_host_location_address`
- `ResolvedHostedLocation` -> `ResolvedHostLocationAddress`
- `HostedLocationParamType` -> `HostLocationAddressParamType`
- `HOSTED_LOCATION` (Click param type instance) -> `HOST_LOCATION_ADDRESS`
- Click param-type display name `hosted_location` -> `host_location_address`
  (visible in command-line help / docs for `mngr push`, `mngr pull`,
  `mngr pair`)

No behavior change.

Add `--restart` and `--no-resume` flags to `mngr start`.

- `mngr start my-agent --restart` stops a running agent and starts it fresh. If the agent is already stopped, it is simply started.
- `mngr start my-agent --no-resume` skips sending the resume message after starting. Can be combined with `--restart`.

### `mngr create` honors the adopt scenario (for imbue_cloud lease flows)

- minds passes `--reuse` for IMBUE_CLOUD agent creates. The bake's services agent is now named `system-services` too, which mngr's pre-flight "agent already exists on this host" check would otherwise reject. `--reuse` is necessary to signal that the lease's pre-baked agent isn't a duplicate-name collision. (`--update` is intentionally NOT passed: the adopt path in `ImbueCloudHost.create_agent_state` already patches labels + command in place; running standard provisioning on top would re-do the file-transfer + provisioning round the bake already paid for.)
- `mngr` core's duplicate-agent-name check in `api/create.py` now honors `host.pre_baked_agent_id`. With just `--reuse` the check still fired because `--reuse`'s lookup runs BEFORE `resolve_target_host` fires the lease, so the leased host's agent isn't in the operator-local mngr state yet to be reused. The pre-flight check now skips the raise when the existing agent's id matches the host's `pre_baked_agent_id` -- that's the lease-adopt scenario by design and `host.create_agent_state` knows how to hydrate the existing agent in place.
- `pre_baked_agent_id` is hoisted onto `HostInterface` as a `None`-defaulted frozen field, so the check in `api/create.py` reads `host.pre_baked_agent_id` directly (no `getattr` shim that would trip the `prevent_getattr` ratchet). Providers whose `create_host` returns a host with a baked-in agent (`ImbueCloudHost` is the only one today) populate it; every other provider's hosts default to `None` and the duplicate-name check's prior behavior is preserved.

- `ProviderError` now carries `provider_name` on the base class. Every subclass (`HostNotFoundError`, `HostNameConflictError`, `HostNotRunningError`, `HostNotStoppedError`, `SnapshotNotFoundError`, `TagLimitExceededError`, `ImageNotFoundError`, `LocalHostNotStoppableError`, `LocalHostNotDestroyableError`, `LimaHostCreationError`, etc.) now requires `provider_name` as its first constructor argument. Handlers that catch `ProviderError` can read `e.provider_name` without isinstance-narrowing to a specific subclass.

Removed the unused notion of agent "permissions" from `mngr` itself. The `Permission` primitive, `AgentPermissionsOptions`, `NoPermissionsAgentMixin`, and `get_permissions`/`set_permissions` methods on host and agent interfaces have all been deleted, along with the `--grant`/`--revoke` flags on `mngr limit` and the `--grant` flag on `mngr create`. Agent type configs no longer accept a `permissions` list. Higher-level libraries (latchkey, minds) keep their own (real) permissions concepts.

`mngr rename` now works against offline hosts: when the agent's host is
not online, the rename (and any `-l KEY=VALUE` labels) are written to
the provider's persisted agent data without starting the host. The
`--start/--no-start` flag still exists but now defaults to `--no-start`;
pass `--start` to force the host online first so tmux and the env file
are updated alongside data.json.

Add `@pytest.mark.timeout(60)` to two flaky tests in `libs/mngr/imbue/mngr/cli/test_destroy.py`: `test_destroy_via_stdin` and `test_destroy_multiple_agents`. Both spin up two `sleep` agents, wait for tmux sessions, run a parallel destroy, and wait for cleanup -- a workload that lands at or over the global 10s pytest-timeout under modal-offload contention. The first exhausted all 5 `@pytest.mark.flaky` retries on PR #1652; the second has flaked in the same CI runs. The multi-agent shape is the point of both tests (piping multiple names through stdin is the main use case for `destroy -`), so the workload cannot be shrunk. Matches the existing precedent on `test_destroy_transfer_none_keeps_shared_worktree`.

Adds shell-level integration tests for `scripts/install.sh`. The existing install tests build a venv that simulates what install.sh produces, but never invoke the script itself. The new `test_install_script.py` runs `bash scripts/install.sh` against mock `uv` and `mngr` binaries on a synthetic PATH and verifies the control flow: `uv tool upgrade` vs `uv tool install` branches, the PATH-not-set error path, and the continue-on-failure (`|| warn`) behaviour of `mngr dependencies -i` and `mngr extras -i`. No real PyPI install or system dependencies are required, so the tests run in under three seconds with no network access.

`mngr create --type X` now fails fast with `UnknownAgentTypeError` when `X` does not resolve to a registered agent class (either directly via a plugin/built-in registration, or via a `[agent_types.X]` block whose `parent_type` points to a known type), instead of silently resolving to a generic `BaseAgent` + empty config. A bare `[agent_types.X]` block without `parent_type` is also rejected. Use `--type command -- <shell command>` to run an arbitrary shell command. The `--type X -- ...` form is no longer a hidden alias for `--type command -- ...`.

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

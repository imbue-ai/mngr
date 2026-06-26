# Unabridged Changelog - mngr

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-25

Bumped the offload version baked into `libs/mngr/imbue/mngr/resources/Dockerfile` from `0.9.7` to `0.9.9` to track the CI pin.

## 2026-06-24

Added `mngr config wizard`, an interactive command that walks through common one-time user-scope configuration. Each step short-circuits when its setting is already configured, so it is safe to re-run.

The first step (shown only when the claude agent type is installed) asks whether to enable config-dir isolation for local Claude agents: enabling it keeps each agent's Claude config separate so mngr never touches your default config, while disabling it shares your default config -- which is what Claude subscriptions on macOS need to keep credentials working. The choice is written to `agent_types.claude.isolate_local_config_dir` in your user config.

Hardened agent process-tree teardown so a long-lived daemon launched under an agent (e.g. a bootstrap-managed `supervisord` and its children, like a `ttyd` bound to a fixed port) can no longer outlive the agent and collide with a relaunch.

- Lifted the `stop_agents` teardown into a shared `reap_agent_process_tree(agent)` helper: it kills the agent's tmux-session pane descendants **and** any `MNGR_AGENT_ID`-tagged orphans that reparented to PID 1 (SIGTERM then SIGKILL), without killing the tmux session itself. `stop_agents` now uses this helper (plus the session kill) via the extracted `_collect_agent_process_pids` / `_terminate_pids`.

- `start_agents` now reaps any stale process tree for an agent id before launching, but only when that agent isn't already running (so an idempotent start never tears down a live agent). This prevents a relaunch from straddling an orphaned daemon from a prior incarnation (which previously caused, e.g., `EADDRINUSE` on a service's fixed port, leaving the new service stuck restarting while the orphan kept serving).

## 2026-06-23

Exposed the discovery polling cadence as a public constant (`DISCOVERY_STREAM_POLL_INTERVAL_SECONDS` in `imbue.mngr.api.discovery_events`, previously a private `_`-prefixed value). Consumers such as minds derive freshness thresholds from it, so deriving the threshold from the cadence keeps the two from silently drifting apart.

`mngr create --format json` now includes `outer_host_public_key` and `container_host_public_key` when the provider exposes them (a new `get_ssh_host_public_keys` host/provider method, defaulting to none), so tooling can pin a host's sshd key for strict host-key checking instead of trust-on-first-use.

## 2026-06-22

Handle unauthenticated providers consistently across `mngr list` and other commands.

Previously every provider reported a missing-credentials state differently: AWS/GCP/Azure raised a verbose error and exited 1, Vultr printed an ad-hoc `WARNING` and exited 0, OVH silently reported zero agents, and a full `mngr list` (default `--on-error abort`) surfaced only whichever provider happened to fail first.

Now:

- A new shared `ProviderNotAuthorizedError` (a subclass of `ProviderUnavailableError`) represents "enabled but unauthenticated" for every provider, carrying structured `short_reason` / `short_remediation` fields plus verbose help text.

- `mngr list --on-error continue` runs the rest of the listing, then reports every failing provider in one consistent block: a single glanceable line per provider on stderr for human output (`<provider>: <reason> — <remediation> (disable: mngr config set ...)`), and structured entries in the `errors` array (with `exception_type`, `help_text`, and an `is_provider_inaccessible` flag) for `--format json` / `jsonl`. The default `--on-error abort` still fails immediately on the first provider error.

- `mngr list` now exits with the granular provider-inaccessible code (6) when every error is a provider that could not be reached or authenticated, and 1 otherwise -- in both abort and continue modes.

- The OVH backend was missing from the internal remote-backends list, so it loaded (and silently no-op'd) in environments where the other cloud backends were correctly skipped; it is now treated like AWS/GCP/Azure/Vultr.

Regenerated the `mngr forward` command reference to document its new
`--on-error {abort,continue}` option (provided by the mngr_forward plugin).

Unify the `mngr start` host lock with the normal cooperative host lock.

There is now a single host lock. State-changing operations (`create`, `start`, `gc`, ...) all acquire `Host.lock_cooperatively`, which holds a real `flock(2)` on the host's `host_lock` file -- directly on local hosts, and over a long-lived SSH exec channel on remote hosts. Previously the remote path of this lock was only an advisory marker file (no real mutual exclusion), while a separate, newer `lock_for_starting` held a real flock on its own `host_start_lock` file. The separate start lock (and its `host_start_lock` file) is removed; `mngr start` now uses the unified lock, which gives true cross-actor mutual exclusion (e.g. a remote desktop client racing an in-host boot hook) for all of these operations.

Because `start` and `gc` now share the lock, a `gc` that tears down a host/agent is serialized before a concurrent `start`; after acquiring the lock, `start` checks that the target agent's state directory still exists and fails with a clear "not found on host" error instead of trying to boot an agent that was just garbage-collected.

`create` and `start` block indefinitely until the lock is acquired (a contended create now waits rather than failing); `gc` and other callers keep a bounded wait that raises an error on timeout. While waiting on a contended lock, mngr logs a "waiting to acquire host lock" message.

The in-host idle-shutdown watcher and `mngr list`'s lock columns now detect the lock via a non-blocking `flock` probe rather than file existence, since the lock file is intentionally never deleted (its inode must stay stable across local and remote holders). The lock auto-releases when an operation finishes or errors, so a crashed controller no longer leaves a host pinned awake. `MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE=1` now keeps a failed remote host alive by launching a detached on-host process that keeps holding the flock.

`flock` (the `util-linux` package) is now a required host dependency, bootstrapped alongside `git`/`tmux`/`jq`/`sshd` on all providers (and pre-installed in the bundled image). It is already present on standard Debian-based images, so this only matters for minimal/custom images.

## 2026-06-21

- Container hosts now self-heal sshd after an out-of-band restart. The host
  container entrypoint (re)starts sshd on boot whenever mngr has already
  provisioned this host (tracked by a marker, so a host key pre-baked into the
  base image is never used by mistake), so a `docker restart`, docker daemon
  restart, or host reboot brings ssh back without waiting for `mngr start`.
  mngr's own sshd start is now idempotent (a no-op when sshd is already running).

- `mngr start` is now safe to run concurrently for the same host. The agent
  (re)launch is serialized by a dedicated cross-actor `flock` (local in-host
  starts coordinate with remote over-SSH starts), so a desktop-driven start and
  an in-host boot-hook start cannot race. The lock blocks until acquired (wrap
  the command in `timeout` for a deadline).

- Added a `--post-host-create-outer-command` create option (and matching
  create-template / settings key `post_host_create_outer_command`). It runs
  shell commands once on the host's outer machine (the underlying VM/daemon
  host) after the host is created -- e.g. to install a VM-level systemd unit.
  Skipped with a warning when the provider exposes no outer host.

Regenerated the `imbue_cloud` command reference doc to cover the new bare-metal slice options: `admin pool create --slice-env-name` and the new `admin pool teardown-slices` command.

## 2026-06-20

`mngr imbue_cloud admin pool create` now defaults `--backend` to `slice` (bare-metal slices) instead of `ovh_vps`. Baking new OVH classic VPS pool hosts is deprecated: passing `--backend ovh_vps` fails with a deprecation error pointing at `--backend slice`. Existing OVH VPS pool hosts can still be listed and destroyed. The generated CLI reference doc (`docs/commands/secondary/imbue_cloud.md`) is updated to match.

## 2026-06-19

Stop `mngr event --follow` from polling the persisted files of an offline (stopped-but-not-destroyed) agent every second.

Previously, a follower whose target host was offline kept re-reading the agent's unchanging event files once per second through the read-only volume. For the Docker provider each such read is a separate `docker exec` into the shared state container, so a handful of stopped agents could drive the Docker engine to a large, wasted CPU load (observed: ~30 `docker exec`/sec into one state container, pushing it to ~90% CPU) even though no agent work was happening.

The follow loop already tracks online/offline status and re-checks it every 30s. Each per-source tail thread now lives for the whole follow session and gates its own I/O on a shared online/offline signal: while the target is offline it parks and does no reads at all (a stopped agent cannot write events), and the periodic source-directory rescan is likewise skipped. On a transition the loop simply swaps the shared target handle and flips the gate -- threads are never torn down and recreated, so the previous teardown/restart churn (and its races) is gone. Each thread reads from whichever target (online host or offline volume) is current, re-reading its source from the start across a transition; existing event-id deduplication ensures nothing is emitted twice on resume.

When the host returns to RUNNING the threads resume reading automatically. This affects every consumer of `mngr event --follow` (the CLI, `mngr forward`, and the minds desktop app), not just one.

`build_add_authorized_keys_command` (the shared SSH-key seeding helper used by every provider's host/container setup) is now idempotent: each entry is appended only if it is not already present (a `grep -qxF` guard), so re-running it does not accumulate duplicate `authorized_keys` lines. This lets the imbue_cloud restart path re-seed a leased container's key by reusing this helper instead of hand-rolling its own command, and makes the first-time seed safe against accidental double-application.

Updated the `mngr create` help docs for `--aws-ami=` to reflect that the AWS provider now falls back to a pinned per-region default when `default_ami_id` is unset (the separate `default_ami_by_region` config field was removed).

Added `extract_agent_data_from_parsed_listing` to the shared
`providers/listing_utils` helpers, the natural companion to
`parse_listing_collection_output`. It pulls each agent's `data.json` dict out of a
parsed listing, replacing three copies of the same logic in the VPS provider's
realizers. An entry whose `data` is present but not a JSON object (a list/scalar
from a corrupt or hand-edited `data.json`) is now skipped with a WARNING rather
than silently, matching the other listing skip-sites. No user-visible behavior
change beyond the added warning.


Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename and the
accompanying class renames (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerError` -> `VpsError`, etc.). Import-only; no behavior
change.

mngr config now merges with the explicit operator algebra from the new standalone `overlay` library (see the `overlay` changelog for the operators in full): a bare `key` assigns (narrowing-checked), `key__extend` merges onto the layer below -- now **recursively** (a nested `key__extend` merges deeper while a nested bare key still replaces; backward-compatible) -- and the new `key__assign` assigns **without** the narrowing warning (the per-key opt-out to the `allow_settings_key_assignment_narrowing` flag). Within a single layer, resolution is two-phase and order-independent (assigns first, then `__extend`); a bare `key` and `key__assign` for the same field is a contradictory double-assign and raises `ConfigParseError`. New `Static*` markers (`StaticTuple` / `StaticList` / `StaticDict`) mark an aggregate atomic so replacing it is exempt from the narrowing guard; mngr's `StringDerivedTuple` / `ScalarTuple` are now `StaticTuple` subclasses.

A settings *patch* field (a claude agent type's `settings_overrides`) now **accumulates** across config scopes (user < project < local) and `parent_type` inheritance instead of a higher/child scope replacing the whole value: non-overlapping keys from every scope survive and same-key `__extend`s combine, with the narrowing guard threaded recursively (a bare assign dropping a non-empty aggregate is caught at any depth). Its deferred `__extend` / `__assign` markers are preserved through config-load and resolved at provision time against the home `settings.json`, joining the existing `create_templates.<name>` deferred paths. A `key__assign` inside `settings_overrides` now reliably suppresses the cross-scope narrowing guard even when a *lower* scope already set the key -- which is precisely the case a narrowing could fire, so the per-key opt-out now works where it matters (previously the deferred `__assign` collapsed to a bare assign at config-load whenever a lower scope had set the key, and the guard still errored on the opted-out key).

Cross-scope `settings_overrides` narrowing is now surfaced at config-load. A `settings_overrides` field accumulates across scopes, so an additive cross-scope override is a superset that never narrows; previously a higher scope whose *bare* key dropped a non-empty aggregate set by a lower scope did so silently. The merge now surfaces those in-patch drops into the loader's flag-gated narrowing error (escapable via `allow_settings_key_assignment_narrowing = true`, `allow__extend`, or `allow__assign`). Purely additive cross-scope overrides still load unchanged.

Fixed a latent crash: `MngrConfig.merge_with` raised `AttributeError` when the base config's `retry` / `logging` was `None` and the override set them (masked in production, but a real footgun). The overlay pipeline's `drop_none_values` pass treats a `None` sub-model as unset, so the re-parse defaults it rather than dereferencing `None`.

Fixed a config-merge bug where a partial override of a nested sub-model field (`logging`, `retry`, or a provider's `security_group`) silently reverted the base scope's other sub-fields to their defaults. For example, project scope `[logging] file_level = "ERROR"` plus local scope `[logging] console_level = "TRACE"` previously dropped `file_level` back to its default (and spuriously raised the narrowing error); now `file_level` carries through. Sub-model fields are detected by their *live value* being a `BaseModel` (so a `None` is simply unset and a discriminated-union value resolves to its own concrete class), and merge field-by-field across config scopes, `--setting`, and `parent_type` inheritance -- carrying through a base scope's unset sub-fields and narrowing only when a nested list/dict actually drops entries. (This also corrects a latent inconsistency in the old field-by-field merge, which used an `is-not-None` test that dropped any unset sub-field whose default was non-`None`.)

Internal (no user-facing behavior change): the model-free config-merge algebra is extracted into `overlay` (mngr imports it from `imbue.overlay`; `ConfigParseError` now subclasses `OverlayError`), and the whole config-merge surface -- `AgentTypeConfig.merge_with`, `MngrConfig.merge_with`, and `parent_type` inheritance -- now computes through overlay's typed-node pipeline (`config/overlay_merge.py`) instead of field-by-field pydantic copies, guarded by property tests asserting equivalence to the frozen old logic. The now-dead `_merge_container_dict` / `_assign_scalar` helpers and the vestigial per-model `merge_with` overrides are removed. The per-key-merging registries (`agent_types`, `providers`, `plugins`, `commands`, `create_templates`) are now identified by a declarative `RegistryField()` field annotation (parallel to `SettingsPatchField`) rather than a hard-coded field-name set, and sub-models are detected at runtime instead of by annotation introspection.

Internal (no user-facing behavior change): **all** config-load narrowing detection now flows through the single overlay merge; the separate model-walking detector (`detect_settings_narrowing` and helpers) is removed from production, kept only as a frozen equivalence-test reference. To make this faithful, the overlay pipeline re-marks `Static*` markers stripped by `model_dump` (relying on the markers' proven-pure round-trip) on the override side before merging, so an atomic-aggregate replacement (string-shaped `cli_args`, a provider's `allowed_ssh_cidrs`, an explicit `StaticList` / `StaticDict`) stays narrowing-exempt; and the overlay's `narrowing_paths` reports the specific narrowed leaf (e.g. `commands.create.defaults.env`) rather than just the containing field. The loader's two narrowing collectors collapse into one. A nested same-named container field (e.g. a plugin config's own `commands` dict) is narrowing-checked as an ordinary aggregate, since the container per-key merge applies only at the top level of `MngrConfig`.

Internal (no user-facing behavior change): now that the overlay refactor is shipped and CI-green, the equivalence-guard scaffolding is removed -- the frozen `_reference_*` old-logic copies and the `production == reference` tests in `overlay_merge_test.py` (covering `AgentTypeConfig.merge_with`, `MngrConfig.merge_with`, `parent_type` inheritance, and the model-walking narrowing detector). The behaviors they pinned stay covered by the surviving direct-assertion tests there plus the loader / data-types / registry tests. The dead string-suffix merge engine in `overlay` (`combine_patches` and the string-suffix `merge` / `finalize`) is deleted with them.

Internal (no user-facing behavior change): the loader's narrowing-attribution (which lower-precedence layer a dropped value belonged to) is now computed in linear time via a `provenance` map threaded through the config-layer fold, replacing the prior O(n^2) re-merge of every prior layer against each new layer. Attribution output is unchanged.

Internal (no user-facing behavior change): the config-merge pre-processing in `config/overlay_merge.py` is simplified to a single declarative recursive walk. The `SettingsPatchField` / `RegistryField` markers and their collectors move to a new leaf module `config/field_markers.py` (re-exported from `config/data_types.py`, so existing imports keep working), unified behind one generic `get_field_names_with_marker` helper; the marks are now read directly off each live model's class rather than threaded in as resolver callables. The three mark/unmark helper pairs collapse into one recursive `_to_operator_dict` / `_from_operator_dict` pair (a registry entry's own settings-patch and sub-model fields are now handled by the same uniform walk), the redundant per-entry class table is derived from the live entry models, and the `tuple/list/dict -> Static*` shape mapping is shared. The `StringDerivedTuple` marker is removed in favor of the identical `ScalarTuple` (a string-written `cli_args` is now a `ScalarTuple`); its narrowing-exemption behavior is unchanged. Equivalence to the prior field-by-field logic remains pinned by the frozen `_reference_*` guards in `overlay_merge_test.py`.

Internal (no user-facing behavior change): mngr's suffix-keyed-dict extend consumers (`config/key_resolver.py`, `cli/common_opts.py`) now resolve a single `__extend` through the overlay's one extend algebra via the new `extend_plain_value` adapter (a thin lift/finalize over the node engine), replacing the import of the now-deleted parallel `apply_extend` / `extend_dict` plain-dict recursion from `overlay.merge`. Resolution output is unchanged.

Internal (no user-facing behavior change): the overlay's `would_assignment_narrow` predicate moved from `imbue.overlay.merge` to the new `imbue.overlay.narrowing` module; mngr's importers (`cli/common_opts.py`, `config/data_types_test.py`) and doc references (`config/README.md`, `config/loader.py`) are updated accordingly.

Internal (no user-facing behavior change): added `write_json_dict_via_host`, the write-side counterpart to `read_json_dict_via_host`, which serializes a dict to indented JSON and writes it via the host (optionally creating the parent directory first). The Claude plugins now route their settings/hooks writes through it instead of hand-rolling the `json.dumps(..., indent=2) + "\n"` + optional `mkdir -p` dance at each site.

An expected (`MngrError`) error raised during agent provisioning -- e.g. a settings-narrowing `ConfigParseError` while building the per-agent `settings.json` -- now surfaces as a clean one-line error instead of an "Unexpected error" with a full traceback. `provision_agent` runs its body inside a concurrency group that re-raises a body exception wrapped in a `ConcurrencyExceptionGroup`; the `create` flow now unwraps a single expected error from that group (via the group's `only_exception_is_instance_of` / `get_only_exception` helpers) before it reaches the CLI.

The `mngr config` command is now exempt from the settings-narrowing guard, so a config that would otherwise narrow can still be loaded in order to *edit* it: previously `mngr config set` / `mngr config unset` -- the way to fix a narrowing config -- failed with the narrowing error themselves (a catch-22). The guard still fires for every other command.

The narrowing error's `__extend` example is now tailored to the offending key (e.g. `work_dir_extra_paths__extend = ...`, or `permissions__extend = {allow__extend = ...}` for a nested path) instead of a fixed generic example, so the message shows exactly how to fix the user's own config.

Internal (no user-facing behavior change): the per-plugin `merge_with` tests the overlay refactor retired (scalar precedence, `None`-keeps-base, type guards) are subsumed by the centralized overlay/merge tests; the one behavior not yet pinned centrally -- a `plugins` entry preserving its subclass type and type-specific fields when a higher scope sets only `enabled` -- now has a single consolidated test (`overlay_merge_test.py::test_mngr_plugins_registry_subclass_is_preserved`), the plugins-registry analogue of the existing `agent_types` and `providers` subclass-preservation tests.

Updated the shared provider release harness's offline-host_dir capability docstring to note that GCP now also supports the offline ``host_dir`` mirror (alongside AWS S3 and Azure Blob), so Trip 1's opt-in offline-host_dir step runs against GCP too.

Trimmed a monorepo-development-only paragraph from the PyPI README (regenerated from the top-level README) so it stays focused on user-relevant content.

Added a shared provider release-test harness (`imbue.mngr.providers.provider_release_testing`): a `ProviderReleaseProfile` plumbing contract plus a `run_provider_release_trip1` driver that walks one provisioned host through the full lifecycle in a single boot -- create, verify the tagged cloud resource, exec a marker file, plain stop, `stop --stop-host` (real machine stop, or a loud refusal where unsupported), start, persistence, snapshot (where supported), an out-of-band "sketchy kill", crash discovery, `gc`, and a backend-clean check. Mirrors the agent release-test harness so each provider's release suite collapses to a thin profile.

Also added `run_provider_release_trip3` ("snapshot survives destroy"): snapshot a host, destroy it, and assert the snapshot is portable -- a fresh `mngr create --snapshot` restores the captured filesystem -- or, where snapshots are not portable (the container shape's docker-commit dies with the VPS), assert that documented divergence so it flips loudly once portability lands.

Also fixed the shared `delete_modal_apps_in_environment` test helper to pass `--yes` to `modal app stop`, so the app is actually stopped in non-interactive runs (CI / release tests) instead of aborting on a confirmation prompt and relying on the environment deletion to reap it.

Also added `run_provider_release_trip4` ("error classification contract"): a no-boot CLI trip that asserts `mngr create` surfaces the right error for a provider with unresolvable credentials (the contract `ProviderUnavailableError`, or the documented divergence where a provider raises a different class), checks whether the curated help text points at the provider-correct setup command, and -- for the VPS family -- asserts a `--vps-*` build arg is rejected synchronously with the migration hint. Runs in seconds and costs no compute.

Also added `run_provider_release_trip2` ("idle auto-shutdown contract"): a single-boot trip that creates a host with a short auto-shutdown (the idle watcher for the cloud trio; the sandbox lifetime cap for Modal), polls the cloud API until the compute genuinely stops so billing halts, and -- where the provider resumes from that stopped state -- asserts `mngr start` brings it back with a pre-shutdown marker intact. Where the provider does not resume (Modal: the sandbox is terminated by its own timeout), the trip asserts the termination only and skips the resume. The shutdown is driven via `--idle-timeout` (which lands the cloud trio in a resumable stopped state) rather than the `auto_shutdown_seconds` time cap (which terminates/deletes the instance). Adds a `resumes_after_auto_shutdown` capability flag plus `auto_shutdown_create_args` / `write_auto_shutdown_settings` profile hooks.

Trip 1 gained an opt-in offline-host_dir read (off by default behind `MNGR_RELEASE_TEST_OFFLINE_HOST_DIR=1`, so the happy path stays lean): in the `--stop-host` window, where the provider captures host_dir to a state bucket (`supports_offline_host_dir`), it asserts `mngr file get <host> <path> --relative-to host` serves the marker from the offline mirror while the host stays stopped. AWS/Azure run it; GCP (`NullHostDirBackend`) and Modal (no `--stop-host`) skip.

Trip 1 also gained a bare-shape assertion (`is_bare_host` capability flag): where the profile declares a bare (no-container) shape, it asserts the agent shell is the VM's own root -- the bare host store (`/var/lib/mngr-host`) is present and there is no `/.dockerenv` -- so a NONE-isolation host that silently fell back to a container fails loudly. This is the coverage the retired per-provider bare lifecycle tests used to own, now folded into the trip's NONE parametrization.

Merge intent for an agent type's `settings_overrides` is now declared with a Claude-compatible `__mngr_merge` map instead of the `__extend` / `__assign` key suffixes.

Because `settings_overrides` is folded into a file the external AI CLI also reads (Claude Code's / antigravity's `settings.json`), the suffixes -- which that CLI does not understand and would surface as junk literal keys -- are no longer allowed there. Instead, declare the operator in a single top-level `__mngr_merge` map keyed by dotted path, which the external CLI silently ignores:

```toml
[agent_types.claude.settings_overrides.permissions]
allow = ["Bash(npm *)"]
[agent_types.claude.settings_overrides.__mngr_merge]
"permissions.allow" = "extend"   # merge onto the base; "assign" replaces without the narrowing guard
```

A bare key still assigns with the narrowing guard; the narrowing error now prints the exact `__mngr_merge` patch to add. The suggested patch is the full nested one in a single error: a dict that would drop a sibling key is suggested as `extend` (so the sibling survives) and a replaced list/value as `assign` (so your exact value is kept, not silently broadened). Raw `__extend` / `__assign` suffix keys under `settings_overrides` are a hard error pointing to `__mngr_merge`. mngr's own (non-`settings_overrides`) config is unchanged and still uses the suffixes.

New `mngr config assign <key> <value>` command, mirroring `mngr config extend`: it writes a `key__assign` entry (replace without the narrowing guard), or -- on a `settings_overrides` path -- a `__mngr_merge` `assign` directive. `mngr config set key__assign <value>` routes to it, and `mngr config get` resolves the `__assign` form.

A settings key that contains a literal dot (e.g. an MCP server name like `my.server`) cannot be targeted by a dotted `__mngr_merge` path: such a directive errors as dangling and the auto-remediation skips it rather than mis-advising.

`mngr config set` / `extend` / `assign` now let configuration errors render through the central CLI error handler (a `ConfigParseError` is a `MngrError`) instead of a local catch, so an invalid value prints e.g. `Error: Unknown configuration fields: ['provider']` rather than an `Invalid configuration: ...` line.

## 2026-06-18

Added a shared `AgentUpdatePolicy` (`AUTO` / `ASK` / `NEVER`) used by the agent plugins to govern an agent CLI's self-updater. When unset, the default is `NEVER` (block self-update) so a managed agent stays on its installed version, except an *attended* agent that implements an interactive update flow, which defaults to `ASK`; unattended agents always default to `NEVER`.

Added a shared `verify_pinned_cli_version` installation helper so agent plugins can verify an installed CLI matches a pinned version (needed because installation is skipped when the binary is already present). It matches the user's pinned string verbatim against the `--version` banner (no version scheme assumed, so pre-release/four-component pins work), errors on a confirmed mismatch, and skips when the CLI reports no version.

Clarified `non_issues.md`: the `is_`-prefix exemption and the missing-`Field()`/description exemption apply specifically to CLI command functions and CLI-options data classes (e.g. `ListCliOptions`) that mirror user-facing click options. Internal boolean fields on non-CLI data classes should still use the `is_` prefix. Also fixed a typo in that file ("duplicated" -> "duplicate").

Fixed `mngr plugin list` mislabeling opt-in plugins (e.g. `claude_subagent_proxy`) as `enabled=true` when they are actually blocked.

The reported `enabled` state now reflects the plugin's real block state: opt-in plugins that were not explicitly enabled show `enabled=false`, while a plugin enabled via `[plugins.<name>] enabled = true` still shows `enabled=true`.

Underlying this, `config.disabled_plugins` now faithfully includes opt-in plugins that are disabled by default, so every consumer of that field (not just the plugin list) sees the correct effective disabled set. This is suppressed under `MNGR_LOAD_ALL_PLUGINS`, matching plugin-manager startup, so doc/tooling runs still load every plugin.

Added a `[tmux]` configuration section for customizing the tmux sessions mngr runs agents in:

- `tmux.attach_args` -- extra tmux client flags inserted before the `attach` subcommand when connecting to an agent (`tmux <attach_args> attach ...`). The motivating case is `["-CC"]` for iTerm2 control mode (native tabs/windows); `-u` / `-2` also work. Applies to both local and remote (SSH) agents.
- `tmux.additional_config_path` -- an extra tmux config file sourced into every mngr session. Unlike the auto-generated `~/.mngr/tmux.conf`, this file is never overwritten, so it is a stable place for mngr-session-specific tmux config.
- `tmux.primary_window_name` (default `agent`) -- mngr now names the agent's primary window and targets it by name instead of the literal `:0` index, so mngr works regardless of the user's tmux `base-index` setting.

Agents that were already running before this change have an unnamed primary window that name-based targeting would miss. mngr now self-heals these in-flight sessions: the first time it inspects such an agent, it renames the session's existing primary window to `tmux.primary_window_name`, so lifecycle detection, messaging, capture, attach, and ttyd keep working across the upgrade.

See `docs/tmux_users.md` for usage.

Fixed `mngr transcript` failing with "Unknown agent type" for config-defined agent subtypes (a custom `[agent_types.X]` with a `parent_type`, e.g. `parent_type = "claude"`). The command now resolves the type through its parent chain (like every other command) instead of a flat class-registry lookup, so it recognizes such subtypes and reads the parent's transcript.

Removed `TagLimitExceededError`: it existed only to flag the EC2 50-tag ceiling for the AWS provider's offline tag mirror, which this PR replaces with the S3 state bucket.

Regenerated the `mngr aws` / `mngr azure` CLI doc pages to cover the state-bucket setup these commands now perform (the providers' state-bucket feature is described in the `mngr_aws` / `mngr_azure` changelogs).

Added shared operator-command output helpers in `mngr.cli.output_helpers`, used by the `mngr aws` / `mngr azure` / `mngr gcp` prepare/cleanup commands: `emit_operator_result(event_name, parts, output_format)` renders a sequence of `OperatorResultPart`s -- each pairing a structured-data fragment with its human line, built via `OperatorResultPart.shown(human, **data)` or `shown_if(present, human, **data)` -- as JSON / JSONL / human in one place, plus a `write_event_line` primitive for the shared `{"event": <type>, ...payload}` JSONL shape.

Test-only: raised the per-test timeout on the tmux lifecycle tests `test_start_restart_running_agent` / `test_start_restart_stopped_agent` from the default 10s to 30s (they run several sequential tmux create/stop/restart operations that can exceed 10s on a loaded CI runner).

## 2026-06-17

Added a code-derived agent capability registry: a description of which agent types have which capabilities, where each capability declares how its presence is detected (a class mixin via `issubclass`, a `waiting_reason` field generator, a plugin hookimpl, or a sibling usage plugin).

The registry introspects a loaded plugin manager to determine each registered agent type's capabilities, and a generated doc (`libs/mngr/docs/concepts/agent_capabilities.md`) renders the full capability matrix. A drift guard fails if the committed doc disagrees with the code; regenerate it with `just regenerate-agent-capabilities-doc`. This is the basis for replacing the hand-maintained parity matrix.

The registry/generator and its tests are dev-only tooling and live in `scripts/make_agent_capabilities_doc.py` (see the `dev` changelog), so they are not shipped in the `mngr` wheel. What *is* in the `mngr` package is the set of capability marker mixins in `imbue.mngr.interfaces.agent` (`CliBackedAgentMixin`, `HasSessionAdoptionMixin`, `HasStreamingSnapshotMixin`, `HasUnattendedModeMixin`, etc.), which agent classes inherit at runtime and which the generator detects.

Added contract-bearing capability mixins to `imbue.mngr.interfaces.agent`: `HasStreamingSnapshotMixin`, `HasSessionPreservationMixin`, and `HasUnattendedModeMixin`. Agent types declare these to make the corresponding capabilities (live response streaming, session preservation on destroy, unattended/auto-allow operation) code-detectable in the capability matrix.

Added `HasPermissionPolicyMixin` (per-resource allow/deny/ask policy) and `HasVersionManagementMixin` (version pin or update policy) capability mixins.

Added module-level capabilities to the matrix: `deploy_contributions` (the `get_files_for_deploy` hookimpl) and `usage_tracking` (a sibling `mngr_<harness>_usage` plugin), both detected by the agent's owning plugin entry-point name.

Made auto-install a base capability: added `HasAutoInstallMixin` (per-CLI `get_install_command`) and a shared `ensure_cli_installed` helper that checks for the binary at provision time and installs it if missing (gated by consent locally, `is_remote_agent_installation_allowed` remotely). All five agents now declare it; antigravity, opencode, and codex gain auto-install they previously lacked. Adds the `auto_install` row to the capability matrix and a new `AgentInstallationError`.

Verified opencode and antigravity auto-install end-to-end on real Modal hosts (which ship without the CLIs).

Architecture-review refinements: excluded the task-specialized skill variants (code-guardian, fixme-fairy) from the matrix (kept headless_claude, which runs genuinely different logic); added a dedicated `get_install_binary_name()` to `HasAutoInstallMixin` (decoupling the install check from the lifecycle-detection process name); and a construction-time validator on `AgentCapability` for the detection-kind/field invariant. The registry-driven behavioral exercise of each capability against a live agent is deferred to a follow-up release-test harness; detection is covered in CI by the drift guard and the builder integration test.

Gave the capability matrix a fixed column order (claude, headless_claude, antigravity, codex, opencode, pi-coding, command, headless_command) instead of alphabetical, and excluded the internal `mngr-proxy-child` agent. Rendering now raises if a registered agent type is neither in the fixed order nor the exclusion list, so a newly added agent can never be silently dropped from the table. Moved the `headless_output` row to the bottom of the matrix.

Added a third matrix state, `n/a`, for capabilities that do not apply to an agent kind (distinct from `-`, which means applicable but absent). Each capability now declares a code-derived scope based on the agent's kind:

- CLI-backed-only (`raw_transcript`, `common_transcript`, `auto_install`, `permission_policy`, `version_management`, `usage_tracking`): `n/a` for the bare command runners.

- Interactive-only (`waiting_reason_field`, `session_resume`): `n/a` for headless and bare-command agents.

- Headless-only (`headless_output`): `n/a` for every non-headless agent, since exposing `output()` non-interactively is meaningless for an interactive agent.

A genuinely-registered capability (field generator, usage source, deploy hook) that lands out of scope raises, keeping the matrix honest; an inherited capability mixin that lands out of scope just renders `n/a`.

CLI-backed scope is derived from a positive marker, `CliBackedAgentMixin`, inherited by every agent that wraps a specific external CLI (claude, codex, antigravity, opencode, pi, and headless variants). A bare command runner is simply the agent without that marker, so it needs no command-specific class for scoping; a minimal `CommandAgent` subclass of `BaseAgent` survives only to declare `HasUnattendedModeMixin`. `unattended_operation` shows present for every agent: interactive coding agents earn it by auto-allowing in-run tool prompts, while headless and bare-command agents have it by construction (no prompt to gate on), declared via `BaseHeadlessAgent` and `CommandAgent`.

Unified the TUI streaming snapshot and headless incremental output into a single `live_output` capability via a shared bare marker, `SupportsLiveOutputMixin`, inherited by both `HasStreamingSnapshotMixin` (the TUI agent's snapshot file) and `StreamingHeadlessAgentMixin` (a headless agent's incremental stdout). `headless_output` (plain `HeadlessAgentMixin`) remains a separate row.

Added a `session_resume` capability (the read-side counterpart to `session_preservation`) via `HasSessionAdoptionMixin`, whose `adopt_session` contract method an agent's `on_after_provisioning` calls to resume an existing conversation. Interactive-only: it resumes a live session, so it is `n/a` for headless and bare-command agents (e.g. `headless_claude` inherits the mixin from `ClaudeAgent` but is headless, so it renders `n/a`). Currently claude-only (its `--adopt-session` / `--from` carry-forward); other interactive CLI agents show it as an available-but-absent gap.

Scoped the send-message flow to interactive agents. `send_message` is no longer an abstract method on `AgentInterface` (so it is not a universal contract). The send-keys delivery (`send_message` + `_preflight_send_message` + `_send_message_simple` + `_send_tmux_literal_keys`) moved off `BaseAgent` onto a new `SendKeysAgent(InteractiveAgentMixin, BaseAgent)`, which `InteractiveTuiAgent` and the bare `command` runner extend; opencode/pi declare the new `InteractiveAgentMixin` directly (they deliver via their own server/extension APIs). Headless agents (`headless_claude`, `headless_command`) no longer have `send_message` at all, so `BaseHeadlessAgent`'s rejecting `_preflight_send_message` override was removed. A new `require_interactive_agent` helper narrows an agent to `InteractiveAgentMixin` (used by the `mngr message` command and the initial/resume-message paths). User-visible change: `mngr message <headless-agent>` now fails with a clear "agent type ... does not accept interactive messages" error instead of a generic send failure; `mngr message` for a bare `command` agent still works (it keeps send-keys).

Made `HasVersionManagementMixin` a functional contract: its descriptive `get_version_policy()` label is replaced by `reconcile_installed_version(host, mngr_ctx)`, which a version-managing agent calls during provisioning (once the binary is present) to enforce its intent -- a pinning agent verifies the installed version and raises on mismatch, an update-policy agent runs its update check. `version_management` is still detected in the capability matrix by `issubclass(HasVersionManagementMixin)`, unchanged.

Regenerated the `mngr imbue_cloud admin pool destroy` CLI reference docs to reflect its now backend-aware teardown: `--skip-vps-cancel` is documented as skipping the underlying-machine teardown (cancel the OVH VPS for an `ovh_vps` row, or destroy the lima VM for a `slice` row), used only when the machine is already gone.

Regenerated the `mngr imbue_cloud admin pool create` CLI reference docs to include the new `--max-concurrency` option (caps how many bare-metal slices bake at once). Docs-only change.

Removed superfluous `@pytest.mark.modal`/`@pytest.mark.rsync` marks from two transcript release tests (`test_transcript_assistant_only`, `test_tips_transcript_tail_assistant`). Both run entirely against a local command agent with a locally-seeded transcript, so they never invoke modal or rsync; the marks tripped the resource guard and failed the otherwise-passing tests.

- `mngr rsync` and `mngr git push`/`mngr git pull` now skip host/agent discovery when given a bare local path (the previous implementation always ran a full provider scan), and narrow discovery to the named provider when the address pins one (e.g. `@host.modal:/work` only queries the Modal provider, not Docker). Behaviour for fully qualified addresses is unchanged.

- `mngr create --from` similarly narrows the cached host/agent loader: when the source, the target host, and `--reuse` (if used) all pin a provider, only those providers are queried; otherwise the loader falls back to a full scan exactly as before. Bare local sources and git URLs continue to skip provider discovery entirely.

- New `imbue.mngr.api.find.resolve_host_location(parsed, mngr_ctx, *, is_start_desired=True)` helper consolidates the local-path shortcut and the discover-then-resolve flow that `mngr rsync` and `mngr git` previously duplicated. The lower-level `resolve_host_location_address(parsed, agents_by_host, mngr_ctx, ...)` is unchanged and remains the right entry point for callers (like `mngr create`) that drive discovery themselves to share a single result across multiple resolutions.

Regenerated the bundled `mngr imbue_cloud` CLI reference docs to cover the new
`admin server order --option` flag (explicit choice for multi-offer mandatory
option families like bandwidth/vrack) and the now-required `admin pool create
--backend slice --server-id` flag (explicitly chosen bare-metal box).

The plugin install wizard (`mngr extras plugins` / `mngr plugin install-wizard`) now recommends and pre-selects the AWS, GCP, and Azure provider plugins when their CLI is detected on your system (`aws`, `gcloud`, and `az` respectively), mirroring how the Claude and Modal plugins are detected. The Lima provider plugin is now likewise recommended and pre-selected when `limactl` is detected (its CLI detection previously had no effect).

Extended the shared agent release-test harness (`imbue.mngr.agents.agent_release_testing`) to exercise session adoption end-to-end: after an agent is destroyed, the arc creates a fresh agent (in a new worktree) that adopts the just-preserved session via `--adopt` and asserts it recalls the pre-destroy secret -- proving the preserved store actually resumes, not merely that its bytes landed on disk.

Adoption is no longer opt-in: the arc runs it unconditionally for every agent that preserves on destroy (`preserves_on_destroy`, default on), and every such profile must implement `adopt_session_arg(preserved_dir)` (the session id or native-store path to adopt), else it fails loudly. Adoption is now a baseline capability -- if an agent preserves its session on destroy, the harness proves that session actually resumes into a fresh worktree. This makes the strongest assertion the default and removes the silent-skip risk of the former per-profile opt-in flag.

Standardized session adoption as a first-class create capability for any agent type that supports it (`HasSessionAdoptionMixin`). The CLI option is now `--adopt` (with `--adopt-session` kept as a backward-compatible alias), declared like every other create option in `cli/create.py` rather than through the plugin-extension hook. The adopted session id(s)/path(s) ride a typed `adopt_session` field on `CreateAgentOptions` (previously the namespaced `plugin_data["adopt_session"]` key); all five adoption-capable plugins (claude, antigravity, codex, opencode, pi-coding) read `options.adopt_session` / `args.agent_options.adopt_session`. The agent-agnostic validation (the type must support session adoption; mutually exclusive with cloning via `--from`) now runs in `imbue.mngr.api.create` for every create path (CLI and programmatic); each plugin keeps its own `on_before_create` fail-fast session-id pre-resolution. The former claude-only / built-in-plugin wiring (`builtin_adopt_session`) is removed.

Added a shared `iter_agent_session_paths(local_host_dir, relpath)` helper to `imbue.mngr.api.preservation` that enumerates a per-agent path across every live and preserved local agent. The five agent plugins' session-store scanners (claude, antigravity, codex, opencode, pi-coding) now route through it instead of each re-implementing the live+preserved directory walk.

Added a `transfer_cloned_agent_session_store(...)` helper to `imbue.mngr.api.preservation` for `--from <agent>` clone-and-resume: a generic clone copies the source workspace but not the source agent's state dir, so each adoption-capable agent transfers just its native session-store relpath from the source and rebinds it. Previously only claude resumed the source's conversation on `--from`; antigravity, codex, opencode, and pi-coding now do too.

An interactive TUI agent's `TUI_READY_INDICATOR` (the readiness signal `mngr` polls the pane for before sending keystrokes) may now be either a plain string (matched as an exact substring, as before) or a compiled `re.Pattern` (matched with `re.search`). The matching mode is chosen by the value's type, not by its contents, so a plain string containing regex metacharacters still matches literally. This lets an agent whose ready state can't be captured by a single substring (e.g. an input box bounded by horizontal rules) express it as a regex.

In the shared agent release-test harness, observing the RUNNING marker is now required of every agent (previously a per-profile `observes_running_marker` opt-in). Forcing a bash tool call (which asserts the common transcript carries a tool_call nested on the assistant turn plus its tool_result) is enabled for claude, codex, opencode, and pi-coding; it stays gated off for antigravity, whose async tool execution records the result only at the next turn boundary, so a single forced-tool turn never carries a tool_result.

Fixed a bug where sending a message to a resumed interactive TUI agent could time out. The TUI-ready wait now runs inside `send_message` (not only when an agent is first created), so every send path -- including the resume message and on-demand restart -- waits for the TUI to finish rendering before pasting. This prevents keystrokes from being dropped into a session that is still replaying its restored transcript.

Gave `test_cli_create_rejects_dirty_tree_by_default` a 30s pytest timeout (matching the sibling subprocess-create test), since `uv run mngr create` startup intermittently exceeds the default 10s under CI load.

Unified the two live-output surfaces (a TUI agent's streaming-snapshot buffer and a headless agent's captured stdout) onto one shared shape, so `SupportsLiveOutputMixin` is no longer a bare marker.

It now declares `get_live_output_path()` (the host file the agent publishes live output to) and `make_live_output_reader()` (a `LiveOutputReader` that turns successive reads of that file into text deltas). The shared poll-read-extract tail loop both surfaces build on lives in `imbue.mngr.agents.live_output_tail.tail_live_output()` (the implementation layer, where it can reference the host interface directly), keeping `SupportsLiveOutputMixin` a pure-abstract capability declaration. The former `HasStreamingSnapshotMixin` is removed -- a TUI agent now inherits `SupportsLiveOutputMixin` directly and supplies a snapshot-diff reader, while a headless agent supplies a raw-text or stream-json reader. The new `imbue.mngr.interfaces.live_output` module holds the `LiveOutputReader` contract and the `RawTextReader` implementation.

No user-visible behavior change: `mngr ask` / `mngr create --stream` and the robinhood streaming paths emit the same output as before.

- `libs/mngr`: agent tmux sessions now apply the mngr host tmux config even
  when a tmux server is already running. Previously the config was passed only
  via `tmux -f <config> new-session`, which tmux honors solely when it *starts*
  a new server; any session created on an already-running server (the common
  case once one agent is up) silently inherited tmux defaults. That dropped the
  widened `status-left-length` (so `[mngr-<agent>]` was clipped to 10 chars) and
  the `Ctrl-q` / `Ctrl-t` destroy/stop hotkeys. Session creation now runs
  `tmux source-file <config>` right after `new-session`, so these apply
  regardless of server state.

- `libs/mngr`: the host tmux config now enables `set-titles` (`set -g
  set-titles on` with `set-titles-string "#S  #T"`), so the agent's session
  name and pane title are forwarded to the outer terminal's tab (e.g. the
  iTerm2 tab title) instead of falling back to `<profile>(tmux)`.

- `libs/mngr`: mngr's generated `~/.mngr/tmux.conf` no longer sources the
  user's `~/.tmux.conf`, and the agent's tmux server is no longer started with
  `-f` pointing at the mngr config. tmux loads `~/.tmux.conf` itself, once, when
  the server starts; mngr's config (sourced at agent creation) now contains only
  mngr's own settings. Re-sourcing `~/.tmux.conf` on every agent creation could
  re-run non-idempotent user config (e.g. `set -ag`, plugin `run-shell`) and
  corrupt the user's setup.

Aligned the common-transcript schema with the OpenTelemetry GenAI semantic conventions: the assistant record's `stop_reason` field is now `finish_reason` (the OTel term).

Every assistant record now carries an ordered `parts[]` array (text and tool_call segments, modelled on the OTel message parts) that preserves the intra-turn interleaving of text and tool calls -- the canonical, agent-agnostic view that `mngr transcript` renders. A `parts_ordered` flag marks whether the order is faithful (true for claude, pi-coding, opencode, and trivially for codex, whose assistant turns are each either text-only or a single tool_call) or best-effort (false for antigravity, whose native format does not record where tool calls sat relative to the text). The flat `text` + `tool_calls` fields are kept as a convenience baseline. Because every emitter fills `parts[]`, the reader renders it directly with no per-agent fallback.

## 2026-06-16

Loosened the `create_test_agent` test helper's `agent_class` parameter (and return type) to accept any `BaseAgent` subclass, including ones parameterized on a specific `AgentTypeConfig` subclass (e.g. `OpenCodeAgent`). The generic is invariant, so the previous base parameterization rejected such agents, making it impossible to type-check a test that provisions a concrete agent type. No runtime behavior change.

The plugin install wizard (`mngr plugin install-wizard`, `mngr extras -i`) now knows about the usage plugins. Phase 1 recommends the base `imbue-mngr-usage` plugin for everyone. Phase 2 offers each per-agent usage provider (`imbue-mngr-claude-usage`, `imbue-mngr-codex-usage`, `imbue-mngr-opencode-usage`, `imbue-mngr-pi-coding-usage`) only when both its agent plugin and the base usage plugin are present -- already installed or selected earlier in the wizard. This is expressed by a per-entry `gate` field on catalog entries -- a `SignalGate` (detected tool) or `RequiredPackagesGate` (other packages must be present) -- which replaces the previous separate `signal` / `requires_packages` fields and lets the wizard ask each entry whether it is unlocked rather than branching on shape. Antigravity has no usage provider, so none is offered for it.

Added shared agent-preservation wiring so any plugin can mirror the claude/usage preserve-on-destroy behavior with minimal code.

- `build_transcript_preserved_items(event_source)` returns the standard raw (`logs/<source>_transcript`) and common (`events/<source>/common_transcript`) transcript directories an agent writes, centralizing the on-disk convention.

- `preserve_agent_state(items, agent, host)` is a thin online-path wrapper (for a plugin's `on_destroy`) that resolves the agent's state directory and local preserved-files destination.

- `preserve_host_agents_on_destroy(host, mngr_ctx, agent_type, items_for_agent)` is the shared body for a plugin's `on_before_host_destroy` hookimpl: it skips hosts with no readable volume, filters discovered agents by `agent_type`, and preserves each opted-in agent straight off the host volume.

- `flag_gated_items(ref, flag_name, items)` is the shared offline selector helper: it returns `items` only when the discovered agent's persisted `agent_config[flag_name]` is truthy (else `None`), so plugins no longer hand-roll the same opt-in dict-walk for `on_before_host_destroy`.

- The shared agent release lifecycle (`run_agent_release_lifecycle`) now asserts preservation: its destroy step verifies the agent's raw and common transcripts actually landed in `<local_host_dir>/preserved/<agent-name>--<agent-id>/` (keyed on the seeded secret), so a swallowed preservation failure can no longer pass silently. Every plugin built on the shared lifecycle inherits the check.

- Profiles can declare `native_session_preserved_relpaths` so the lifecycle also asserts the agent's native resumable session store was preserved on destroy (not just the transcripts). A FIXME marks where this should grow into an actual resume-from-preserved-store check once `--adopt-session` lands for these agents.

## Azure provider registration

- Added `azure` to the set of remote provider backends that are skipped when tests load local-only backends (`_REMOTE_BACKEND_NAMES` in `providers/registry.py`), so the new `mngr_azure` plugin behaves like `aws` / `gcp` / `vultr` during test isolation.

- `ProviderUnavailableError` now accepts an optional `user_help_text` override. The default still tells the user to start Docker / disable the provider, but cloud providers (whose "unavailable" cause is a credential/subscription problem, not a local daemon) can pass curated guidance instead -- so a cloud auth failure no longer advises "start Docker". Used by the Azure provider.

- Regenerated `mngr azure` and `mngr ovh` CLI docs: `mngr azure prepare` / `mngr azure cleanup` and `mngr ovh list` now take a `--provider` option (and the standard common options) so they read defaults from the selected `[providers.NAME]` settings.toml block.

- Added the `azure` provider backend (`imbue-mngr-azure`) to the install-wizard plugin catalog (`PLUGIN_CATALOG`), so `mngr plugin install` offers it alongside `aws` / `gcp` / `ovh` / `vultr`.

- `mngr gc` gained a provider garbage-collection hook (`ProviderInstanceInterface.gc_provider_resources`, a no-op by default) so a provider can reclaim orphaned cloud resources that are not attached to any host. Reclaimed resources are reported in the gc summary (human / JSON / JSONL) under "Provider resources" and honor `--dry-run`. The Azure provider uses it to reap NIC / public-IP orphans from failed VM creates; that cleanup previously ran at the start of the next `create_instance`.

Changed: `mngr_common_transcript_flush` (shared common-transcript helper) now takes an
optional lock-acquire timeout (seconds), exported as `MNGR_CONVERT_LOCK_TIMEOUT` to each
synchronous converter pass. This lets a latency-sensitive caller (e.g. a SIGTERM/SIGINT
handler) cap how long the flush blocks waiting for the convert lock -- its only
potentially-slow step. Implemented without `timeout(1)` so it stays portable to macOS.
Callers that pass no argument are unchanged (default 30s).

- `mngr git push`, `mngr git pull`, and `mngr rsync` now run the underlying `git` / `rsync` binary as a plain subprocess with the user's stdout/stderr (no redirection), so progress, errors, and pager-style output flow directly to the terminal. stdin is redirected to /dev/null, so the underlying binary can't block waiting for input (credential prompts, host-key confirmations, merge-message editors, etc.) -- those are misconfigurations and we'd rather fail fast than have the agent hang. mngr still waits for the underlying process to exit, so destination-side cleanup -- including `mngr rsync --uncommitted-changes=merge`'s stash pop -- continues to run as before. The `_complete` JSONL terminating events (`{"success": true}` for git, `{"event": "rsync_complete", ...}` for rsync) and the trailing "Rsync complete: N files, M bytes transferred" human line are gone; on a non-zero exit, mngr raises its own `GitSyncError` / `MngrError` so the CLI still surfaces the failure (the underlying exit code is included in the message).
- `imbue.mngr.api.git.git_push` / `git_pull` and `imbue.mngr.api.rsync.rsync` / `rsync_to_remote` / `rsync_from_remote` grow an optional `run_in_terminal: bool = False` parameter. Default behavior (captured stdout/stderr via `ConcurrencyGroup`) is unchanged, so in-process callers like `mngr_pair` and `mngr_mapreduce` are unaffected; only the `mngr` CLI passes `True`. The terminal-stdio path goes through the existing `imbue.mngr.utils.interactive_subprocess.run_interactive_subprocess` helper, called with `stdin=subprocess.DEVNULL`.
- `RsyncResult`, `imbue.mngr.utils.rsync_utils.parse_rsync_output`, and the whole `rsync_utils` module are removed. The rsync API functions now return `None` -- the existing callers (the `mngr rsync` CLI and `mngr_mapreduce`) already discarded the return value, and the `files_transferred` / `bytes_transferred` counts were already useless in `run_in_terminal=True` mode. rsync still runs with `--stats`, so CLI users still see the summary block in their terminal at the end of a transfer.

`mngr stop`, `mngr destroy`, and `mngr cleanup` now aggregate and classify cleanup failures instead of hanging on, or silently swallowing, problems.

Previously the stop path ran its tmux/process-collection shell commands (`tmux list-windows`, `tmux list-panes`, `tmux kill-session`, the `pgrep` descendant walk, the `MNGR_AGENT_ID` env scan, and the SIGTERM/SIGKILL loop) without a timeout, so a wedged `tmux list-panes` could block `Host.stop_agents` indefinitely; and both the stop and destroy paths swallowed most other failures (logging a warning and exiting 0), so a partially-failed cleanup looked identical to a clean one.

Now every cleanup step is bounded and its outcome is classified as either benign (the target was already gone -- no error, exit 0) or a real failure (a resource is actually left behind). Real failures are aggregated across all steps/agents/hosts (cleanup continues rather than failing fast), each tagged with a cause category, and surfaced two ways:

- The process exits with a cause-specific, informative exit code (the most severe cause when several occur): `2` timeout, `3` processes remain, `4` local state remains, `5` host/infrastructure remains, `6` provider inaccessible, `1` other. A clean or only-benign run still exits `0`.

- Structured output (`--format json`) now reports a `failures` list (each with `category`, `message`, `agent_name`, `host_id`) and an `exit_code` field, replacing the old `errors` string list.

Benign detection: shell commands (stop, `rm`) classify by stderr message matching (e.g. tmux "can't find session", kill "No such process"); the destroy path classifies by provider exception type (e.g. Docker `NotFound`). Timeouts are treated as one failure cause among many. Stopping an agent on an offline host is now reported as a real `PROVIDER_INACCESSIBLE` failure (the host is unreachable so the agents cannot be verified stopped) rather than a silent skip.

`Host.stop_agents`, `Host.destroy_agent`, and `ProviderInstance.destroy_host` now raise a `CleanupFailedGroup` (an `ExceptionGroup` whose leaves carry the classified `CleanupFailure`s) when a real resource is left behind, and return normally otherwise -- so a caller can never silently drop a leftover-resource failure by ignoring a return value. `execute_idempotent_command` gains an opt-in `raise_on_timeout` flag that normalizes the two backends' differing timeout signals (local: a killed process; remote: `socket.timeout`) into a single `CommandTimeoutError`; other callers are unchanged. See `specs/cleanup-error-aggregation.md`.

`mngr gc` now reports cleanup failures as structured, categorized failures (consistent with `mngr destroy`/`stop`/`cleanup`) and **exits with a cause-specific exit code** (`2`/`3`/`4`/`5`/`6`, most severe when several occur) when garbage collection leaves a resource behind -- e.g. a snapshot or volume that could not be deleted (`5` host/infrastructure remains), a work dir, source dir, log, or build-cache entry that could not be removed (`4` local state remains), or an explicitly-requested provider that was unavailable (`6` provider inaccessible). Previously gc only exited non-zero when an explicitly-requested provider was skipped, so failed snapshot/volume/work-dir deletions did not affect the exit code. Structured output (`--output json`/`jsonl`) now includes a `failures` list (each with `category`, `message`, `agent_name`, `host_id`) alongside the existing `errors` strings.

Gave `test_cli_create_rejects_dirty_tree_by_default` a 30s `@pytest.mark.timeout` (matching its sibling `test_cli_create_via_subprocess`) so a cold `uv run mngr create` startup under load no longer races the global 10s pytest timeout.

- **Concurrent SSH keypair creation is now race-free.** `load_or_create_ssh_keypair` (`providers/ssh_utils.py`) serializes first-time creation behind an exclusive file lock, and `save_ssh_keypair` writes both key files atomically (temp file + `os.replace`, via the shared `atomic_write` helper) before applying their permissions. The parallel host-discovery fan-out opens one SSH connection per VPS and each lazily creates this keypair on first use; previously racing writers could leave a transient zero-byte or mismatched `.pub`, which surfaced as `ValueError: Not enough fields for public blob` deep in paramiko's certificate probe and aborted `mngr create`. This was observed intermittently on the OVH (and, via the same discovery path, Vultr) release tests.

- **paramiko's bare key-probe `ValueError` is now surfaced as a structured `HostConnectionError`.** `OuterHost._ensure_connected` wraps the `ValueError` paramiko raises when it parses a malformed/half-written `.pub` next to the private key, so callers that catch `MngrError` (e.g. best-effort host discovery) treat it as an ordinary per-host connection failure instead of letting it abort the whole operation.

## 2026-06-15

Regenerated the bundled CLI reference docs to include the new `mngr imbue_cloud admin server pricing` command (per-slice OVH bare-metal pricing table).

Regenerated the `imbue_cloud` CLI reference docs to include the new `admin server` command group (list / register / allocate-slice / set-status) added for the OVH bare-metal slices feature.

- `mngr create --format json` (and `--format jsonl`) now also reports the created host's name and SSH connection (`ssh_user` / `ssh_host` / `ssh_port`), plus an `outer_ssh_port` when the provider exposes a separate outer/management sshd (e.g. an OVH-slice's VM-root port reached via a box-forwarded port). Previously only `agent_id` / `host_id` were emitted. A new `HostInterface.get_outer_ssh_port` hook (default `None`) backs this.

- `VpsDockerProvider.record_outer_host_key` pins an outer (VPS-root) sshd host key in the provider's known_hosts -- used when operating on a VPS the provider did not order itself (e.g. the imbue_cloud rebuild on a leased host) so its outer connections pass strict host-key checking.

- `mngr create --format json` now also reports the agent SSH endpoint's on-disk private key path (`ssh_key_path`), so pool-bake tooling can run post-bake SSH steps against the host without a second `mngr list` round-trip.

Added a `--window` (`-w`) option to `mngr capture` for capturing a non-primary tmux window in the agent's session, by index (e.g. `--window 1`) or name. Without it, capture still reads the agent's primary window as before.

Added a shared shell library `mngr_common_transcript_lib.sh`, provisioned to every
agent's `commands/` dir alongside `mngr_log.sh` and `mngr_transcript_lib.sh` (via
`Host._ensure_shared_shell_libs`). It centralizes the common-transcript converter
primitives shared across agent plugins:

- the convert lock (a coarse mkdir-based mutex serializing the converter's
read-modify-write so the background daemon and an on-demand `--single-pass` flush
can't append duplicate events), and

- the turn-end flush (one synchronous `--single-pass` of the raw streamer + common
converter, in pipeline order), used by agent turn-end hooks so a WAITING-signal
consumer can't outrun the converter.

Centralized the Claude Code CLI presence check. `mngr extras` status (`_claude_plugin_status`) and the `is_claude_installed` test helper now both defer to the canonical `CLAUDE.is_available()` system-dependency check instead of re-implementing `shutil.which("claude")` inline, so the binary name and lookup logic live in one place.

Also removed a duplicate subprocess-error tuple: `extras.py` now imports the shared `SUBPROCESS_ERRORS` from `imbue.mngr.utils.deps` (promoted from the previously private `_SUBPROCESS_ERRORS`) rather than defining its own copy.

Fixed a regression in the e2e test fixture that wrote a malformed `settings.local.toml`
(a duplicated `type = "claude"` key under `[commands.create]`). The duplicate key made
every `mngr` invocation in the e2e/tutorial release suite fail to parse its config and
exit non-zero, cascading into a large block of release-test failures.

Also updated two e2e tutorial tests (`test_config_set_unknown_key_fails`,
`test_config_set_rejects_unknown_key`) that assumed the project `settings.toml` does not
exist until a command writes it. The e2e fixture now intentionally pre-seeds that file
with the pytest opt-in key, so these tests now verify that a rejected `config set` leaves
the file unchanged (and never writes the bad key) rather than asserting the file is absent.

All test-only changes; they do not change `mngr`'s runtime behavior.

## GCP provider integration

- `mngr create` CLI markdown docs regenerated to include the new `gcp` provider's build-args help (`--gcp-zone`, `--gcp-machine-type`, `--gcp-image`, `--gcp-spot`, `--git-depth`), and the `mngr gcp` operator command group docs added (`docs/commands/secondary/gcp.md`), covering both `mngr gcp prepare` and `mngr gcp cleanup`.
- `gcp` added to `_REMOTE_BACKEND_NAMES` in `providers/registry.py` (alongside `aws`/`vultr`/`modal`/`imbue_cloud`). The GCP backend resolves Application Default Credentials at build time, and `google.auth.default()` probes the GCE metadata server as its last fallback, which blocks for seconds in non-GCE environments without credentials. Marking it remote means `load_local_backend_only` (the test default) skips it, so provider-enumerating tests no longer build a default GCP provider and hang on that probe.
- `gcp` and `ovh` added to the install-wizard plugin catalog (`plugin_catalog.py`) as INDEPENDENT provider backends, so `mngr` offers them during plugin installation alongside `aws`/`vultr`/`modal`. (`ovh` was already published but had been missing from the catalog.)
- New `ssh_utils.wait_for_expected_host_key` (and the `parse_openssh_public_key_blob` helper): polls a server's live SSH host key until it matches a known expected key, then returns. Used by the GCP provider, whose `startup-script` installs the host key only after sshd has already booted with a random one; waiting before the strict-checked connection avoids a host-key-mismatch abort without resorting to TOFU.

CLI docs (`libs/mngr/docs/commands/secondary/usage.md`, regenerated via `scripts/make_cli_docs.py`): reflect the `mngr usage` help-output regrouping and the `--max-age` -> `--stale-after` rename in `imbue-mngr-usage`. `--stale-after` and `--detail` now appear under a `## Display` section; `--since` and `--preserved/--no-preserved` now appear under `## Filtering` alongside the agent-filter flags. The old `## Other Options` section is gone. The rendered synopsis is updated to `mngr usage [--stale-after DURATION] [--detail] [--since DURATION] [--no-preserved] [COMMAND]`.

`test_synopsis_lists_all_non_optout_flags` (in `libs/mngr/imbue/mngr/cli/help_formatter_test.py`): no longer silently skips commands whose synopsis is a placeholder like `[OPTIONS]`. Such commands' custom non-Common flags are now reported as missing from the synopsis (and the author must either enumerate them or add them to `_SYNOPSIS_OPTOUT_FLAGS`). Factored out a shared `_AGENT_FILTER_FLAGS` constant covering the 11 flags injected by `add_agent_filter_options`.

`mngr connect`, `mngr gc`, `mngr list`, `mngr snapshot create`, `mngr snapshot list`, `mngr snapshot destroy`: replace placeholder `[OPTIONS]` synopses with enumerated ones. For `list`, behavior/input flags (`--stdin`, `--schema`, `--ids`, `--addrs`, `--fields`, `--sort`) are pulled to the front, followed by the standard filter set, with `--limit` and `--on-error` trailing. For `connect`, `[future]` flags (`--reconnect`, `--session-command`) are omitted -- they remain unimplemented stubs.

`[future]`-flag detection: extract `_check_connect_future_options` in `connect.py` (mirroring `_check_create_future_options` / `_check_list_future_options` in `snapshot.py`) and pin every `[future]` flag with parametrized tests (`test_future_flags_raise_not_implemented_error` in `connect_test.py`; `test_snapshot_create_future_flags_raise_not_implemented_error` / `test_snapshot_list_future_flags_raise_not_implemented_error` in `snapshot_test.py`) that fail when any `[future]` flag stops raising `NotImplementedError`. The failure message tells the author to add the flag to its command's synopsis and delete the corresponding test case.

CLI docs regenerated: `libs/mngr/docs/commands/primary/connect.md`, `libs/mngr/docs/commands/primary/list.md`, `libs/mngr/docs/commands/secondary/gc.md`, `libs/mngr/docs/commands/secondary/kanpan.md`.

Added a shared `WaitingReason` enum (`imbue.mngr.primitives`) and a shared `classify_waiting_reason` rule (`imbue.mngr.hosts.common`) so agent plugins compute the `waiting_reason` field from one source of truth instead of each defining their own. The codex and claude plugins now import these instead of duplicating the enum and the gating logic, and use the existing `OnlineHostInterface.path_exists` for marker checks rather than a private file-existence helper.

## 2026-06-14

- Changed: `mngr extras` status (`_print_extras_status`) now accepts an injectable `claude_status_fn` (mirroring the existing `status_fn` seam on the `_install_*` helpers), so its test can skip shelling out to the `claude` CLI. This removes the slow, variable Node-process startup that made `test_print_extras_status_runs_without_error` flaky under the offload timeout. Internal/test-only; default behavior is unchanged.

Fixed `mngr clone` failing with "destination path ... already exists and is not an empty directory" when cloning a remote agent to a local target (e.g. `mngr clone <agent> <name> --provider local`). The git-mirror transfer initializes a bare repo at the target before fetching, but the remote-source-to-local-target path used `git clone --mirror`, which refuses a non-empty destination. It now performs a mirror-style `git fetch` into the existing bare repo instead.

User-facing CLI errors (`MngrError` and its subclasses) now render their `Error:` line in bold red on a color-capable terminal, matching the colored `ERROR:` prefix already used for `logger.error`. Previously click printed the line in the default terminal color, so an actionable failure (for example, "run `mngr gcp prepare` first" before the firewall rule exists) was visually indistinguishable from normal output. Coloring is gated on the same policy as other mngr output: it is suppressed when stderr is not a TTY or when `NO_COLOR` is set, so piped/captured output stays plain. Exit semantics are unchanged (still a clean exit 1, never a traceback).

Fix the "branch already checked out" error from `mngr create` to suggest the correct flag. It previously pointed users at `--in-place`, which no longer exists (it was consolidated into `--transfer`); it now suggests `--transfer=none`.

Fixed `mngr clone` (and any agent creation) failing when transferring a git repo from one remote host to another remote host. Previously the git push was run directly on the remote source host but pointed at the target's SSH key and known_hosts files, which only exist on the local orchestrator machine -- producing errors like "Identity file ... not accessible" and "Host key verification failed". The transfer now relays through a local bare mirror: it pulls from the source using the source's credentials, then pushes to the target using the target's, both run locally where those files exist. This matches the existing remote-to-remote handling already used for rsync transfers.

Fixed the SSH-backed-host test fixture (`local_sshd`) leaking `git config --global` writes into the developer's real `~/.gitconfig`. Tests that exercise remote git transfers run `git config --global --add safe.directory ...` over the SSH connection, where the test's HOME-redirection fixtures cannot reach; the fake sshd now sets `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` for its sessions so those writes stay inside the test sandbox.

## 2026-06-13

Fixed a data-loss bug in volume garbage collection and made discovery fail
loudly (instead of silently skipping) when a provider's backend is unreachable.

The data-loss bug: when the Docker daemon became briefly unavailable during a
`mngr` operation that runs GC (e.g. a Docker daemon restart), the Docker
provider's `discover_hosts` swallowed the failure and returned an empty host
list. GC then treated every volume as orphaned and deleted it -- wiping the
per-host data of still-live hosts, so their containers could no longer be
restarted. The Docker provider's `discover_hosts` now raises
`ProviderUnavailableError` when the daemon is unreachable instead of returning
`[]`, so an unreachable daemon can no longer be mistaken for "this provider has
zero hosts". GC skips an unavailable provider at its own boundary (it must not
delete volumes it cannot verify).

"Unreachable" is judged by the transport, not by the exception base class: a
dropped connection or timeout (including the daemon disappearing mid-operation,
which surfaces as a raw `requests` connection error rather than a
`DockerException`) maps to `ProviderUnavailableError`, while a
`docker.errors.APIError` -- meaning the daemon was reached and answered with an
error -- propagates as a real fault. This keeps a healthy-but-erroring daemon
from being silently treated as offline (and its provider wrongly skipped by GC).

Discovery no longer hides an unreachable provider. Previously, multi-provider
discovery silently skipped a provider whose backend was down. That meant a
command could quietly do a partial job -- e.g. `mngr message my-agent`, intended
to reach every instance of `my-agent`, could miss an instance on a down provider
without telling you. Now `discover_hosts_and_agents` propagates
`ProviderUnavailableError`, so commands that scan every provider (`message`,
`limit`, `snapshot`, `create`) fail loudly rather than silently omit agents on
the unreachable provider.

Targeted commands now scope discovery so an *unrelated* down provider can't fail
them. `mngr rsync`, `mngr git push`/`pull`, and `mngr event <host>` now resolve
only the provider(s) that could actually hold the target -- via the `.PROVIDER`
qualifier and/or the agent name (resolved through the discovery event stream) --
instead of blindly scanning every provider. So `mngr rsync ./x agent@host.local`
keeps working when an unrelated Docker daemon is down (Docker is never queried),
while a command whose target really is on the down provider fails with a clear
"provider is not available" error.

- `mngr snapshot create`, `mngr snapshot list`, and `mngr snapshot destroy` now take agent and host targets as a single positional list. The `--agent`/`--host` flags have been removed; write `agent`, `agent@host[.provider]`, `@host[.provider]`, or a bare `host-...` ID instead.
- `mngr event @host[.provider]` now narrows discovery to the pinned provider, matching the agent path's behavior.

- `mngr message` (alias `msg`) no longer accepts `--provider`. The set of providers to query during discovery is now derived from the agent addresses themselves: the union of providers named by the addresses, or a full scan if any address omits its provider. Previously the discovery call ignored the providers named by the addresses and only honored `--provider`, so e.g. `mngr msg agent@host.provider_a --provider provider_b` would query the wrong provider and report the agent missing.
- Internally, `mngr message` now routes through `find_all_agents` like the other agent-address subcommands instead of running its own CEL-filter pipeline. The `send_message_to_agents` API now accepts a pre-resolved `Sequence[AgentMatch]` instead of CEL `include_filters` / `exclude_filters` / `all_agents` / `provider_names`. No behavioral change for users beyond the `--provider` removal above.

Fixed the DESCRIPTION (and other prose) sections of `mngr <command> --help` to be indented to the man-page depth of seven spaces in an interactive terminal (the pager / rich path). Previously these sections rendered flush-left, unlike the piped/plain output and the surrounding sections.

## 2026-06-12

Updated the auto-generated `mngr create` docs for the `--adopt-session` option: a bare session ID is now searched in the current and user-scope Claude config dirs, every live local mngr agent, and preserved sessions from destroyed agents.

Internal: added `get_agents_root_dir(host_dir)` as the single source of truth for the agents-state root directory, defined alongside `get_agent_state_dir_path` in `imbue.mngr.hosts.common` (a low-level module importable without circular-import issues). Consolidated the previously hand-written `host_dir / "agents"[ / str(agent_id)]` path constructions across the codebase to route through these two helpers. No behavior change.

Added support for agent-type aliases and made disabled-plugin errors name the real owning plugin.

- Plugins can now register short alternate names for the agent types they provide via the new `register_agent_aliases` hook. An alias is accepted anywhere its canonical type is and resolves to the same agent (e.g. `mngr create my-agent agy` is equivalent to `mngr create my-agent antigravity`). Aliases are a name-resolution layer above the agent-type registries: an alias is not itself a distinct agent type, so it does not appear in `mngr plugin list --kind agent-type`, but it is tab-completable for `--type` and the persisted agent records the canonical type name. If you define a custom `[agent_types.X]` block whose name matches a built-in alias, your custom type takes precedence: the alias is dropped (with a warning) and the name resolves to your custom type.

- mngr now records which plugin registered each agent type, so the "plugin is disabled, enable it with `mngr plugin enable ...`" message names the actual registering plugin instead of assuming the plugin name equals the agent-type name. This fixes the message for types whose name differs from their plugin's entry-point name (e.g. the `pi-coding` type registered by the `pi_coding` plugin).

Tab completion now completes the `-S`/`--setting` config override (`KEY=VALUE`) on every command.

- Pressing TAB after `-S` (or `--setting`) completes the config KEY against the same catalog of keys behind `mngr config set` (e.g. `mngr create -S head<TAB>` -> `headless=`). Keys with a constrained value set (booleans, enums like log levels, provider/agent-type names) insert `KEY=` and then list the allowed values on the next TAB; free-form keys complete to the bare key name.

- Values complete too: `mngr create -S logging.console_level=<TAB>` lists `TRACE`/`DEBUG`/... and `mngr create -S headless=<TAB>` lists `true`/`false`. Works in both zsh and bash (which tokenize `KEY=VALUE` differently).

- Fixed a related completion bug: short value-taking options (`-S`, `-m`, `-b`, `-l`, `-n`, `-t`, `-o`, `-i`, `-s`, `-w`) were not recognized as consuming their value, so their argument was miscounted as a positional. This could suppress completion of a later positional argument on commands with a fixed argument count (e.g. after `-S KEY=VALUE`). Long and short option forms are now classified uniformly (this also fixes the same miscount for the long `--verbose` count option), and the bash `=`-word-break splitting of `KEY=VALUE` values is handled so the value pieces are not miscounted.

- `agent_types.*` keys now complete for builtin/registered agent types (e.g. `agent_types.claude.*`, `agent_types.codex.*`), not just the custom agent types defined in your config. These are derived from each agent type's config schema, so every settable field is offered (including container fields like `config_overrides` that have no value set yet), and constrained fields complete their values (`parent_type` -> agent type names, boolean fields -> `true`/`false`).

- Config keys now complete one dotted segment at a time instead of dumping every fully-qualified key at once. For both `-S`/`--setting` and `mngr config get`/`set`/`unset`, the first TAB shows the top-level keys (with deeper keys collapsed to a `section.` branch, e.g. `agent_types.`, `logging.`), and each further TAB drills into the next segment. The trailing `.` on a branch is inserted with no following space so you can keep typing the next segment (in zsh and bash). For `-S`, a key with a constrained value set is completed to `KEY=` the same way -- its allowed values are deferred to the next TAB rather than listed as soon as the key prefix matches.

- Shell completion now installs as a small, stable shim in your rc that sources a managed completion file (`~/.mngr/completions/mngr.{zsh,bash}`) which mngr keeps up to date. This means completion improvements (like the segment-at-a-time drilling above) apply automatically when you upgrade mngr -- no need to re-edit your rc file. The managed files are refreshed automatically in the background (e.g. on `mngr list`) and by `mngr extras completion`.

- If you have an older self-contained completion function in your rc (from before the shim), tab completion now prints a nudge (rate-limited to at most once a day, and only while the install is out of date) to run `mngr extras completion` to switch to the managed shim. `mngr extras completion` recognizes an outdated install (rather than treating any existing completion as already-configured), installs the up-to-date shim, and **removes the old self-contained completion block** when it byte-for-byte matches a form mngr generated (a hand-edited or unrecognized block is left untouched). So migrating is a single command with no leftover cruft. After installing, it prints the exact `source` command to activate completion in your current shell without opening a new one.

Fixed environment-variable forwarding for remote streaming SSH commands
(`OuterHost.execute_streaming_command` with `env=...`). The streaming path
prepended env vars as a bare `KEY=VAL command` prefix, which in the shell only
applies to the single simple command it precedes -- so for a compound command
like `install && tool ...` the var was gone by the time the second command ran.
It now uses `export KEY=VAL && command` (mirroring the non-streaming pyinfra
path), so the var is set in the shell environment for the whole command. This is
what made remote `depot build` fail with "missing API token" even though
`DEPOT_TOKEN` was supplied via `env`. Extracted the prefixing into a pure
`_prepend_env_exports` helper with unit tests.

Also fixed provider-config parsing to coerce field types. `_parse_providers` used
`model_construct`, which stored raw TOML scalars without coercion -- so an enum
field like `builder = "DEPOT"` stayed the string `"DEPOT"` and a `tuple` field
stayed a list. That tripped pydantic serializer warnings on `model_dump` and, for
a provider block defined in a single config layer (no merge to re-coerce it),
broke identity checks like `builder is DockerBuilder.DEPOT` (silently falling
back to the non-depot path). It now uses `model_validate`, which coerces while
still recording only the provided keys in `model_fields_set` so per-field
config-layer merging is unaffected. This also coerces nested-model provider
fields (e.g. SSH static `hosts` tables to `SSHHostConfig`), subsuming the
dedicated post-`model_construct` coercion helper that previously handled only
that case.

Added a `ScalarTuple` marker type (and the `ScalarStrTuple` annotated alias) for
tuple-typed settings fields that are semantically a single scalar value -- a
higher-precedence config layer that sets one replaces the whole value rather
than tripping the settings-narrowing guard. `StringDerivedTuple` (string-shaped
TOML values like `cli_args = "..."`) is now a specialization of it. This lets a
field like the AWS provider's `allowed_ssh_cidrs` be tightened in a developer's
`settings.local.toml` without the guard rejecting it as "dropping" the committed
default -- combining CIDRs across config layers is never the intent. The marker
is applied by `model_validate` (via the after-validator), so it relies on the
provider-parsing switch to `model_validate` described above.

## AWS provider support: shared layer changes

The `mngr_aws` plugin lands as a new provider backend. The shared `mngr` layer picks up the following supporting changes:

- New `resolve_backend_and_config(provider_name, mngr_ctx)` helper on `mngr/providers/registry.py`. Both `get_provider_instance` and the `mngr create` bootstrap path use it, replacing duplicated "configured-instance vs. bare-backend-name fallback" logic.
- `is_for_host_creation` removed from `ProviderBackendInterface` (Modal-specific flag was being `del`'d by every other backend); replaced with a default-no-op `bootstrap_for_host_creation(name, config, mngr_ctx)` method that Modal overrides and that `mngr create` invokes before `build_provider_instance`.
- The Docker state-container leak fix (read-only commands like `mngr list`/`gc` no longer lazily create the singleton state container) is carried into this new shape: the Docker backend's `build_provider_instance` now unconditionally raises `ProviderEmptyError` when the state container is missing (it is always read-only), and the Docker backend overrides `bootstrap_for_host_creation` to create the container on the `mngr create` path. This replaces main's `is_for_host_creation`-gated version of the same fix.
- `mngr/api/create.py`'s host-creation bootstrap helper is now public as `bootstrap_backend_for_host_creation(provider_name, mngr_ctx)` so other entry points (e.g. `mngr_tmr`'s snapshot path) can trigger the same one-time bootstrap before calling `get_provider_instance`.
- `aws` added to the remote-backend list and `mngr` plugin catalog.
- `mngr create` CLI markdown docs regenerated to include the AWS provider's build-args help.
- `test_cleanup_stop_action_with_real_agent` and `test_list_command_with_running_filter_alias` marked `@pytest.mark.flaky` after observing intermittent 10s-timeout failures on loaded offload sandboxes; pass locally in <3s.
- `_is_transient_ssh_error` (in both `hosts/host.py` and `hosts/outer_host.py`) now treats Python's built-in `TimeoutError` as transient. pyinfra's `read_output_buffers` raises a bare `TimeoutError` when an SSH command's response doesn't arrive within its per-command read timeout (e.g. when the remote sshd is reloaded mid-read during cloud-init bootstrap); the retry loop now picks it up rather than letting it escape host creation.
- `_run_shell_command`, `_get_file`, `_put_file`, and `execute_streaming_command` on `OuterHost` (and `Host._run_shell_command`) now catch the post-retry `TimeoutError` and surface it as a structured `HostConnectionError`. Their inner retry handlers also disconnect on `TimeoutError` so retries rebuild the SSH connection rather than reusing the dead channel.

- Resizing the terminal while attached to a remote agent (AWS or any SSH-backed provider) now reflows correctly instead of showing a field of padding dots once the window grew past a fixed size. The post-attach step previously ran `tmux resize-window -A`, which has a documented side effect of switching the window's `window-size` option to `manual` -- pinning the window at its attach-time size so it no longer tracked the client. It now only sends `SIGWINCH` to the pane processes (to prompt a redraw), leaving `window-size` at tmux's default `latest` so the window resizes with the client on every change.

- **De-duplicated the SSH error-translation chain in `OuterHost`.** The identical `except TimeoutError / OSError "Socket is closed" / EOFError,SSHException -> HostConnectionError` block that was copy-pasted across `_run_shell_command`, `_get_file`, `_put_file`, `execute_streaming_command`, and `list_directory` is now a single `_translate_ssh_errors(...)` context manager parameterized by the per-operation messages. No behavior change (the list-directory path still lets a raw `TimeoutError` propagate via the optional `timed_out` arg).

Regenerated the `mngr schedule` CLI reference docs to include the new
`--timezone` option on `schedule add` (added in the mngr-schedule plugin),
which pins the IANA timezone the `--schedule` cron expression is interpreted in.

Moved the built-in `codex` agent type out of mngr core into the external `imbue-mngr-codex` plugin (added to the plugin catalog as a recommended INDEPENDENT-tier agent type); removed the in-core `codex_agent` stub and its direct registration.

Updated the codex tutorial e2e tests (`test_create_basic.py`, `test_agent_types.py`) to match: codex is now a real agent-type plugin rather than a command-driven stub, so the tests create it with `--no-auto-start` (the real codex run is covered by the `mngr_codex` plugin's own release test) instead of faking it with a `command` override. Two of them were also dropped from `@pytest.mark.modal`: real codex can't run on the throwaway Modal hosts (no binary/auth), which is exactly what the old `sleep` fake worked around; Modal-path coverage of the create mechanism remains in the other (non-codex) tutorial tests. Also fixed a pre-existing duplicate `type = "claude"` key in the e2e test fixture's `settings.local.toml` template, which the old codex tests had been masking by rewriting the file via `mngr config set`.

Added `mngr list --schema`, a machine- and human-readable catalog of every field you can reference in `--include`/`--exclude`, `--sort`, and `--fields`/`--format`.

- `mngr list --schema` lists each referenceable field with its type, description, and the contexts it works in: `cel` (usable in `--include`/`--exclude` and `--sort`, which share one evaluation context) and `template` (also usable in `--fields`/`--format`). It composes with `--format json`, `--format jsonl`, and `--format` template strings, and is rejected (with a clear error) if combined with any agent-selection option since the catalog is static.

- The catalog is derived live from the real data shape (`AgentDetails`/`HostDetails`), so it always reflects the actual models -- including deeply nested fields like `host.resource.cpu.count` and `host.ssh.host`. The non-model fields (the computed `age`/`runtime`/`idle`, the `host.provider`/`project` aliases, and dynamic patterns like `labels.$KEY`) are listed explicitly and pinned to the real computation/alias tables by tests.

- The `project` field is now usable in CEL filters and sorts (e.g. `--include 'project == "mngr"'`, `--sort project`), mirroring the existing `host.provider` alias and the `--project` flag; previously it only worked in `--fields`/`--format` templates.

- The `mngr list` help "Available Fields" section (and the generated `docs/commands/primary/list.md` on GitHub) is now rendered from this same catalog, so the documented fields can no longer drift from the models.

Marked the `pi-coding` agent type plugin (`imbue-mngr-pi-coding`) as recommended
in the plugin catalog, so `mngr extras` offers it by default alongside the other
agent types (claude, opencode, antigravity) now that it has real lifecycle
support.

Added reusable gitignore-status helpers (with a `GitignoreStatus` result) to `mngr.api.git`. Given a host, a repo path, and any repo-relative path (which need not exist yet), they report whether that path is gitignored -- resolving symlinks anywhere along the path (e.g. `.claude -> .agents`) first so `git check-ignore` doesn't choke with "beyond a symbolic link":

- `check_path_gitignore_status` -- ignored by any rule (returns `SKIP` / `IGNORED` / `NOT_IGNORED`).

- `check_path_repo_gitignore_status` -- same, but a path ignored only by the user's global excludes returns `ONLY_GLOBAL` rather than `IGNORED` (for preflight checks whose result must also hold on a remote host / fresh clone, which has no global excludes).

Plugins use these to guard files they write into an agent worktree against showing up as untracked changes.

Added a canonical schema for the agent-agnostic common-transcript envelope
(`imbue.mngr.agents.common_transcript_records`). It is the single source of truth
for the `user_message` / `assistant_message` / `tool_result` records every agent
plugin emits into the stream `mngr transcript` reads, with a validator and a
conformance test asserting that all five emitters -- claude, antigravity,
opencode, pi-coding, and codex -- produce records matching it, so the
independently written emitters cannot silently drift on the shared fields.
A meta-test discovers every registered agent type that emits a common transcript
and fails if any lacks such a conformance test, so the requirement is enforced
rather than relying on convention -- a new agent plugin cannot merge without one.

Added a shared agent release-lifecycle harness
(`imbue.mngr.agents.agent_release_testing`) that drives the common create -> WAITING ->
message -> transcript -> stop/start resume -> destroy arc with per-agent profiles, so
each plugin's release test is a thin profile and every agent is held to the same
lifecycle and the same canonical-transcript contract.

## 2026-06-11

Replaced direct built-in exception raises (ValueError/RuntimeError) in config key resolution, docker provider config validation, and agent discovery with dedicated custom exception types.

Added an `OPT_IN_PLUGINS` set to the config pre-reader (`config/pre_readers.py`) for plugins that are **disabled by default** and must be explicitly enabled with `[plugins.<name>] enabled = true`. This inverts the normal default (plugins load unless explicitly disabled) for the listed plugins, reusing the same `enabled` config key. The first opt-in plugin is `claude_subagent_proxy`, which is very experimental and breaks other tooling.

The docker provider now raises a typed, actionable `DockerRuntimeNotRegisteredError`
when the configured `docker_runtime` (e.g. `runsc` for gVisor) is not registered
with the Docker daemon, instead of letting Docker's raw exit-125 `ProcessError`
propagate. The old behavior surfaced the entire `docker run` command line with the
real cause ("unknown or invalid runtime name: runsc") buried inside it and no
guidance. The new error renders as a clean message naming the runtime and provider,
with `user_help_text` pointing at the fix (install the runtime, or set
`docker_runtime=runc` via `mngr config set` / the
`MNGR__PROVIDERS__<NAME>__DOCKER_RUNTIME` env var). Because it is an `MngrError`
subclass, `mngr create --format jsonl` now emits `error_class:
"DockerRuntimeNotRegisteredError"` so callers can branch on the type.

## 2026-06-10

Added the `log_warnings` loguru-capture fixture to the shared plugin test helper (`register_plugin_test_fixtures` in `imbue.mngr.utils.plugin_testing`), so plugin test suites that register the standard fixtures can assert on emitted warnings without defining their own copy. The capture logic lives in a single `capture_log_warnings()` context manager in `imbue.mngr.utils.testing`, which both that fixture and mngr's own `conftest.py` `log_warnings` fixture delegate to. (Affects test infrastructure only.)

Improved test quality for the top-level `imbue/mngr` modules: replaced tests that passed without verifying behavior. `mngr --version` is now asserted to succeed unconditionally (the prior test accepted a since-fixed failure mode); deleted tautological `ToolRequirement` constructor tests and a type-enforced catalog signal check; strengthened the installable-packages, catalog-floor, and create-time parsing assertions; and removed a permanently-skipped upgrade-discovery test whose behavior is already covered by `hosts/host_test.py` and `test_install.py`.

- Sending a message to a Claude agent now confirms submission as soon as the message is accepted into the agent's queue, instead of always waiting on the `UserPromptSubmit` hook. The hook only fires when the prompt reaches the model, which for a message sent to a *busy* agent is when the agent finally dequeues it -- potentially many minutes later -- so `mngr message` (and any caller of the send path, e.g. an HTTP front-end) could block up to the full submission timeout and exceed a front-door proxy timeout, even though the message was already queued and would be processed. `send_enter_via_tmux_wait_for_hook` now watches a fresh `enqueue` event in the agent's transcript log *concurrently* with the hook's `tmux wait-for` signal (in a single remote command) and returns as soon as either lands. This keeps the call fast for busy agents while still confirming via the hook for prompts that never enqueue a model turn (the `/clear` and `/compact` TUI-local commands, whose signal is fired from `SessionStart`). Behavior is unchanged for TUIs that don't supply an acceptance-marker command (they keep the original hook-only wait). The shared `tui_utils` module stays agent-neutral: it watches an opaque, agent-supplied "message accepted" marker command for a fresh monotonic token, and the Claude-specific transcript-log/`enqueue`-event schema now lives in the Claude plugin rather than in the shared module.

The git repo-detection helpers (`find_git_worktree_root`, `is_git_repository`, `find_git_common_dir`) no longer silently swallow unexpected `git` failures. Previously any `ProcessError` (a non-zero exit, a timeout, or a failure to even spawn the subprocess) was caught and turned into a "not in a git repository" answer (None/False), so a transient or environmental git problem would silently drop the project-scope config layer (e.g. disabled plugins would not be blocked) or otherwise misreport repository state, with no explanation. Now only git's own "not a git repository" result maps to that sentinel; every other failure is raised with git's actual return code and stderr, so problems surface loudly instead of causing confusing downstream misbehavior. The detection calls also force a C locale so the check cannot be defeated by a localized git. This is the production-side fix for a rare flaky failure of `test_create_plugin_manager_blocks_disabled_plugins` in CI. No change to behavior when running inside or outside a git repository normally.

Regenerated the `mngr kanpan` CLI reference doc to document the new `--format json` / `--format jsonl` output (the command now prints a board snapshot for programmatic use instead of ignoring the flag).

- `mngr create --reuse` now scopes the existing-agent lookup to the host named in the agent address (e.g. `babatest` in `system-services@babatest.docker`), not just the provider. Previously, when creating a new host (`--new-host`), the reuse lookup ignored the address's host name and matched *any* same-named agent on the provider, so it raised "Multiple agents found with name '<name>'. Use address syntax ..." as soon as two or more same-named agents were discoverable -- even though the address already specified the host. This broke callers that deliberately share one agent name across many hosts and rely on the host name for identity (the minds desktop client names every workspace's primary agent the constant `system-services`). With the fix, a create targeting a brand-new host finds nothing to reuse and proceeds to create, while a re-create targeting an existing host reuses exactly the agent on that host. Genuinely ambiguous reuse (a shared name with no host in the address) still raises the disambiguation error.

Regenerated command docs: the `file` and `tmr` See-Also sections now link to `mngr rsync` instead of the removed `push`/`pull` commands, fixing broken `[mngr help push](mngr help push)` / `[mngr help pull](mngr help pull)` markdown links. The doc generator (`scripts/make_cli_docs.py`) now fails `--check` on any See-Also reference that resolves to neither a known command nor a help topic, so stale references like these are caught going forward.

`test_create_plugin_manager_blocks_disabled_plugins` now asserts up front that `MNGR_LOAD_ALL_PLUGINS` is not set, failing loudly with a diagnostic message if it is. That env var disables plugin blocking, so a leak from another test or imported module would otherwise silently mask the test; rather than papering over it, the test acts as a tripwire that surfaces the leak so it gets fixed at its source.

Strengthened several weak or fragile unit tests under `imbue/mngr/utils` so they actually catch regressions:

- `build_cel_context` tests now assert the converted CEL value types (`StringType`, nested `MapType`) and exact values, instead of merely checking that keys are present. They now catch a regression that stopped converting raw values to `celpy.celtypes`, which would break dot-notation filtering.
- The `imbue.mngr` import smoke test now asserts the package name and that the `cli` console-script entrypoint is a real Click command, instead of the tautological `assert mngr`; the file was also renamed from `test_mngr_import.py` to `mngr_import_test.py` to match the unit-test naming convention.
- `get_current_branch` test now checks out a deterministic, uniquely-named branch and asserts the exact returned name, so it would fail if the function returned a commit-ish or remote ref rather than the branch name.
- `check_bash_version` test now exercises the version-comparison branch (an unreachable `minimum=999` returns `False`) instead of only asserting the return type.
- `_format_arg_value` complex-object test now asserts the exact rendered repr via an inline snapshot, catching dropped fields or changed quoting.
- The asciinema cast-player init-script test now verifies the script wires `AsciinemaPlayer.create(...)` to the correct player div id (`player-0`) and runs inside the `DOMContentLoaded` handler, rather than just checking for substrings.
- Editor test sleep scripts now use large, globally-unique durations to avoid leak-detector collisions, with clarifying comments about why the long sleeps make the synchronous `is_running()` assertions race-free.
- The name-generator uniqueness tests now assert the observable behavior -- that 50 draws yield at least 10 distinct names -- instead of a probabilistic `>= 5` threshold or a seed-pinned exact count. The threshold is chosen so a correct generator's flake probability is ~1e-24, while not coupling the test to wordlist size or RNG draw order. (Per-name validity is covered by the existing per-style tests.)
- Relocated the name-generator tests from `test_name_generator.py` (integration-test naming) to `name_generator_test.py` (unit-test naming) to match their actual nature and the `_test.py` convention.
- Removed two tautological dataclass/enum tests (`InstallMethod` field round-trip and `DependencyCategory` member values) whose behavior is already covered by the behavioral install-command tests; added a comment to the logging-suppressor buffering test explaining why its count bound is `>=`.
- Raised the coverage floor from 80% to 85% (CI measures ~87%).

- `libs/mngr`: regenerate the `mngr forward` CLI reference (`docs/commands/secondary/forward.md`). The `--port` option's help text and default drifted out of sync with the dynamic-port behavior; the regenerated doc now describes the "try 8421, fall back to an OS-assigned port" semantics.

## 2026-06-10

Addressed Josh's review feedback on PR #1937:

- `mngr gc --provider <name>` now exits non-zero when an explicitly-named provider is unavailable. The other selected providers still run to completion; the unavailable provider is reported as an error in the summary so the user can see what was not gc'd. Empty providers (e.g. a fresh Modal per-user environment with nothing to collect) remain silently skipped, since their state is known to be empty and there is nothing to do. The automatic post-destroy gc path (which always passes `--all-providers` internally and tolerates skips) is unaffected.

- Removed the `-a` / `--all` / `--all-agents` flag from `mngr message` (alias `mngr msg`). The tutorial and CLI examples now use the explicit `mngr list --ids | mngr msg -` pattern. Users who relied on `-a` should switch to piping ids from `mngr list` (optionally with `--include` / `--exclude` to scope the broadcast).

- Dropped `--no-ensure-clean` from the agent-type e2e tutorial tests. The e2e fixture now gitignores the per-test project config directory (where `mngr config set` writes its files), so the working tree stays clean and the flag is no longer needed. The `@pytest.mark.rsync` markers on those tests (whose only purpose was to satisfy the resource guard for the rsync path that `--no-ensure-clean` happened to trigger) are removed alongside.

- Removed a stale duplicate `type = "claude"` line in the e2e fixture's seeded `settings.local.toml` that was causing every release-tier e2e/tutorial test to fail with "Cannot overwrite a value".

## 2026-06-09

Fixed the SSH provider silently disabling strict host-key checking for statically-configured hosts.

- A host defined under `[providers.<pool>.hosts.<host>]` that set **both** `key_file` and
  `known_hosts_file` lost its `known_hosts_file` whenever the backend expanded the `key_file` path.
  `SSHProviderBackend.build_provider_instance` rebuilt the `SSHHostConfig` by re-listing only
  `address`/`port`/`user`/`key_file`, so `known_hosts_file` silently became `None` and strict
  host-key checking was turned off for that host. The backend now updates only the `key_file`
  field, preserving `known_hosts_file` (and any future fields), matching the dynamic-hosts path
  that already did this correctly.
- Consolidated the `key_file` path-expansion logic (previously hand-rolled separately in the static
  and dynamic host paths) into a single `SSHHostConfig.with_expanded_key_file()` method that both
  paths now call, so neither can silently drop a field if `SSHHostConfig` gains one in the future.

Fixed a crash that made statically-configured SSH hosts unusable, and added documentation for the SSH provider.

- Static `[providers.<pool>.hosts.<host>]` tables defined in `settings.toml` previously crashed every
  host-enumerating command (`mngr list`, `mngr connect`, `mngr create <agent>@<host>.<pool>`, ...) with
  `AttributeError: 'dict' object has no attribute 'key_file'`. Provider configs are built with
  `model_construct` (to keep unset top-level fields `None` for config-layer merging), which does not
  coerce nested values, so each host entry stayed a raw dict instead of an `SSHHostConfig` and blew up
  as soon as the backend touched it. The config loader now coerces nested pydantic-model fields (the
  only one today being the SSH provider's `hosts` map) after `model_construct`, so static SSH hosts load
  and resolve correctly. A malformed host entry now produces a clear `providers.<pool>.hosts` config
  error instead of a late crash.
- Added an [SSH provider documentation page](../docs/core_plugins/providers/ssh.md) covering host
  configuration (`address`/`port`/`user`/`key_file`/`known_hosts_file`), the dynamic-hosts file, the
  `NAME@HOST.PROVIDER` form for running an agent on a configured host, and the provider's limitations
  (no host creation/snapshots/tags). Registered the `ssh` backend in the provider concepts doc.

Fixed the Docker provider leaking singleton "state containers". Read-only commands (`mngr list`, `mngr gc`, `mngr cleanup`, and any cross-provider discovery) no longer create a Docker state container when none already exists: the Docker backend now treats the provider as empty and skips it, mirroring how the Modal backend skips a provider whose environment does not exist. Only `mngr create` (which passes `is_for_host_creation=True`) creates the state container. Behavior for existing Docker hosts is unchanged.

Also hardened the test suite against leaked Docker state containers: fixed an off-by-one in the leaked-container detector, and the per-worker session cleanup now fails the suite when a state container created under one of its own test prefixes is left behind (while still warn-and-cleaning unattributable containers from other concurrent workers or older sessions).

Fixed create-template `setting`/`setting__extend` entries being silently dropped. A `--template` whose definition sets `setting__extend = ["providers.docker.docker_runtime=runsc"]` (or any other config key) now actually reaches the resolved config instead of being ignored. Direct CLI `-S` still wins over a template-provided setting for the same key.

A template `setting` that targets `commands.*` or `create_templates.*` now raises a clear error (those sections are resolved before template settings are applied, so the value could never take effect) instead of being silently ignored.

Fixed `mngr config get` and `mngr config list --all` so they surface provider-subclass fields (e.g. `docker_runtime` on a docker provider) instead of reporting "Key not found".

Introduced a standard way for plugins to preserve files from an agent's state directory when
the agent (or its whole host) is destroyed, and made stopped hosts readable through a uniform
interface.

- New `HostFileReadInterface` (in `interfaces/host.py`) captures the read-only file operations
  (`read_file`, `read_text_file`, `path_exists`, `get_file_mtime`, `list_directory`) that work
  even when a host is not online, as long as its persistent storage (volume) is reachable.
  `OuterHostInterface` now extends it, so every online host is a `HostFileReadInterface`.
- New `OfflineHostWithVolume` (in `hosts/offline_host.py`) implements `HostFileReadInterface`
  on top of a stopped host's persisted volume, addressing files by absolute paths under
  `host_dir` exactly as an online host would. `make_readable_offline_host()` wraps a plain
  `OfflineHost` in this readable form when the provider yields a volume for it (else returns the
  plain `OfflineHost`), and every provider's offline-host construction now does so -- so a
  stopped host is readable whether it is reached via `get_host` (the destroy/GC path) or
  `to_offline_host`. The volume *reference* is fetched via a new provider method,
  `get_volume_reference_for_host`, which (unlike `get_volume_for_host`) skips any network
  existence probe -- e.g. Modal's `listdir` -- and returns the lazy reference, so constructing a
  readable offline host (including during host discovery) adds no per-host probe; only providers
  that actually probe (Modal) override the method, and a since-deleted volume surfaces as a
  read/write failure at access time. This lets callers treat a stopped-but-volume-backed host
  uniformly with an online one instead of branching on online-vs-offline and reaching for the
  raw `Volume` API.
- New `api/preservation.py` with `PreservedItem`, `preserve_agent_data()`, and
  `get_preserved_agent_dir()`. Callers declare a list of paths (relative to the agent state
  dir) to keep; the same declaration is executed against either an online host (rsync for
  directories) or a volume-backed offline host (file-by-file walk). Preserved files mirror the
  agent-state-dir layout verbatim under `<local_host_dir>/preserved/<agent-name>--<agent-id>/`.
- `OuterHost` gained a `list_directory()` implementation (local filesystem walk, or SFTP
  `listdir_attr` over the same paramiko channel used for remote file reads).
- The listing-entry type `VolumeFile` is now the shared return type for every
  `HostFileReadInterface.list_directory` (hosts as well as volumes). Its `file_type` uses the
  full `FileType` enum (file, directory, symlink, pipe, socket, block, character, other), moved
  into core `interfaces/data_types.py` from `mngr_file` (which now re-exports it), with a
  canonical `FileType.from_stat_mode` classifier; `VolumeFile` also gained an optional
  `permissions` string. Producers fill these to the fidelity their source allows: a host
  classifies the real `stat`/`lstat` mode and reports a permissions string, while a bare volume
  only distinguishes file vs. directory and leaves `permissions` None. (`VolumeFileType` is gone,
  folded into `FileType`.)
- New `HostFileWriteInterface` (`write_file`, `write_text_file`), the write companion to
  `HostFileReadInterface`. `OuterHostInterface` extends it (so every online host writes), and
  `OfflineHostWithVolume` implements it by writing the stopped host's volume (file modes are not
  settable through a volume write, so `mode` is ignored there). This lets write commands target
  an online or a stopped host through one interface.
- `api/events.py` now reads and discovers event journals through `HostFileReadInterface`
  (an online host, or a readable stopped host whose volume is reachable) addressed by a single
  absolute events path under the host's `host_dir`. This removes the separate code paths that
  shelled out `find`/`cat` over SSH for online hosts and used a separately-fetched
  events-scoped `Volume` for everything else, collapsing the dual `online_host`/`volume`
  representation on `EventsTarget` into one `host` handle. It also drops the trailing-newline
  "sentinel-cat" workaround: the host read path is byte-exact (local reads bytes directly,
  remote uses SFTP), so a file's exact trailing-newline state survives without the sentinel.

Regenerated the `mngr usage` command reference docs to include the new `--preserved` / `--no-preserved` flag (on `mngr usage` and `mngr usage wait`). The behavior itself lives in the `mngr_usage` plugin; this is just the generated CLI doc reflecting it.

# Docstring: update stale provider-field example

`ProviderInstanceConfig.merge_with` used `is_host_in_docker` as an illustrative
provider field in its docstring. That field was removed from the Lima provider
(which no longer runs agents in a nested Docker container); the example now
references `is_run_as_root`. No behavior change.

Fixed the e2e tutorial test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that
wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated
`settings.local.toml`. The duplicate key caused every e2e tutorial command to fail
with a TOML parse error ("Cannot overwrite a value"). Removing the redundant line
restores config parsing so the e2e tutorial tests run.

Fixed: the very first `mngr create --provider modal NAME` (including the
`--reuse` form) against a brand-new per-user Modal environment no longer fails
with `Provider 'modal' has no state yet`. The CLI create path resolved the
new-host provider (used to tear the host down if a post-create step fails) with
read-only semantics, so on a not-yet-existing Modal environment it raised
`ProviderEmptyError` before the create could bootstrap the environment. It now
resolves that provider with `is_for_host_creation=True`, matching the API-layer
resolution, so the environment is created on first use as documented.

Fixed the e2e test fixture's generated `settings.local.toml`, which defined the
`type` key twice under `[commands.create]`. The duplicate key produced invalid
TOML ("Cannot overwrite a value"), causing every `mngr` invocation in e2e
tutorial tests to fail while parsing the config file.

Fixed the e2e test fixture that wrote a duplicate `type` key into
`settings.local.toml`, which produced invalid TOML and broke
`mngr observe --discovery-only` (and other config-reloading commands) under
the e2e suite. Also strengthened `test_advanced_observe_stream` to verify the
documented DISCOVERY_FULL snapshot contract (source, agents/hosts/providers
collections, and presence of the local provider).

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) so the
`settings.local.toml` it writes is valid TOML. The fixture had an accidental
duplicate `type = "claude"` key under `[commands.create]`, which made the
strict (tomllib) config read path fail with "Cannot overwrite a value". This
broke e2e tests whose commands parse config strictly, e.g.
`mngr list --running --format json` in `test_advanced_watch_dashboard_running`.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated `settings.local.toml`, which produced invalid TOML ("Cannot overwrite a value") and broke every e2e tutorial test's environment setup.

Fixed the e2e tutorial test fixture and the command-agent dev-server test.

- Removed a duplicate `type = "claude"` key in the `[commands.create]` section of the
  `settings.local.toml` written by the shared `e2e` fixture (`e2e/conftest.py`). The duplicate
  key was a merge artifact that made the TOML unparseable, so every e2e tutorial command that
  loaded the config failed with "Cannot overwrite a value".
- `test_command_agent_dev_server_extra_windows` is a purely local command-agent test, so it no
  longer carries a spurious `@pytest.mark.modal` (which the resource guard rejected as "marked
  but never invoked"). Its verification listing is now scoped to `--provider local`, mirroring
  the sibling `test_command_agent_python_http`; the Modal command-agent path remains covered by
  `test_command_agent_batch_job_modal`.

Fixed the e2e tutorial test fixture: removed a duplicate `type = "claude"` key in the generated `settings.local.toml` that produced an invalid TOML file ("Cannot overwrite a value"), which caused `mngr create` to fail in command-agent tutorial tests.

Fixed the e2e test fixture so the shared `settings.local.toml` it writes is valid
TOML. The `[commands.create]` table contained a duplicate `type = "claude"` key,
which `tomlkit` rejects with "Cannot overwrite a value". This broke any e2e command
that loaded the merged config (e.g. `mngr config edit`), surfacing a config-parse
error instead of the command's real behavior.

Also strengthened `test_config_edit_editor_failure` to assert that `mngr config edit`
propagates the editor's exact exit code (1 from `/bin/false`) rather than merely
exiting non-zero.

Fixed the e2e tutorial test fixture which wrote a duplicate `type = "claude"`
key under `[commands.create]` in `settings.local.toml`. The duplicate produced
invalid TOML, causing `mngr` commands in affected e2e tests to fail at config
parse time with "Cannot overwrite a value" instead of exercising the command
under test. Also hardened `test_config_edit_scope_missing_editor` to assert the
missing-editor error names the editor and points at `$EDITOR`/`$VISUAL`.

Fixed the e2e tutorial test fixture so the `mngr config` tutorial tests pass again. The shared `e2e` fixture had two regressions: a duplicate `type = "claude"` key inside `[commands.create]` of the seeded `settings.local.toml` (which made the file unparseable, breaking `config path`/`config edit`), and a stray seed of the project-scope `settings.toml` (which prevented `config edit`/`config set` tests from exercising genuine first-use behavior, where the project config file does not yet exist). No user-facing behavior change; this is test-infrastructure only.

Fixed the e2e tutorial test fixture and the `test_config_edit` release test.

- The shared e2e subprocess fixture wrote a `settings.local.toml` containing a
  duplicate `type = "claude"` key under `[commands.create]`, which made tomlkit
  reject the file ("Cannot overwrite a value") and broke every e2e tutorial
  test. Removed the duplicate key.
- Adapted `test_config_edit` to the fixture's seeded project `settings.toml`:
  the project-scope config file already exists, so the test now verifies that
  `config edit` opens that exact file in `$EDITOR` and that the editor's marker
  persists into it, instead of asserting the file was created from scratch.
- Added `@pytest.mark.timeout(60)` to `test_config_edit`, which now runs two
  mngr subprocesses and exceeded the default 10s function timeout.

Fix duplicate `type = "claude"` key in the e2e test fixture's generated `settings.local.toml`. The duplicate line made the TOML unparseable, causing config-reading tutorial tests (e.g. `test_config_get_missing_key`) to fail with a parse error instead of exercising the intended behavior.

Fixed the e2e test fixture's local `settings.local.toml` template, which had a
duplicate `type = "claude"` key under `[commands.create]`. The duplicate made the
file invalid TOML, so any test that exercised `mngr config set --scope local`
(e.g. the `test_config_get` tutorial test) failed when tomlkit re-parsed the file
with "Cannot overwrite a value".

Fixed the e2e tutorial test fixture so that the generated `settings.local.toml`
no longer contains a duplicate `type = "claude"` key under `[commands.create]`.
The duplicate produced invalid TOML that tomlkit rejected when `mngr config set
... --scope local` re-saved the file, breaking config tutorial tests such as
`test_config_list_json`.

Fixed the e2e test fixture so `mngr config` tutorial tests work again.

- The shared e2e fixture wrote `settings.local.toml` with the `type = "claude"` key duplicated
  under `[commands.create]`, producing invalid TOML. Any `mngr config` command that loaded the
  merged config (e.g. `mngr config list --scope user`) then failed with a "Cannot overwrite a
  value" parse error. Removed the duplicate so the fixture emits valid TOML.
- Strengthened `test_config_list_scope` to assert real scope isolation: the fixture's
  `connect_command` value lives only in the local scope, so it must appear under
  `--scope local` and must not bleed into the user/project views.

Fixed the e2e test fixture that seeded a malformed `settings.local.toml` with a duplicate `type = "claude"` key under `[commands.create]`. TOML disallows duplicate keys, so any `mngr config set --scope local` (which re-parses and re-saves the file via tomlkit) failed with "Cannot overwrite a value". This unblocks the config tutorial e2e tests (e.g. `test_config_list`).

Fixed the e2e test fixture that seeds the local-scope config file: it wrote a
duplicate `type = "claude"` key under `[commands.create]`, producing an
unparseable `settings.local.toml`. This caused `test_config_path_invalid_scope`
(and any command that loaded the merged config) to fail with a TOML parse error
instead of exercising the intended behavior.

Fixed the e2e test fixture that seeded a malformed `settings.local.toml` with a
duplicate `type = "claude"` key under `[commands.create]`, which made every config
command fail to parse the file ("Cannot overwrite a value"). This unblocks
`test_config_path_scope` and the other CONFIGURATION tutorial e2e tests.

Fixed the e2e test fixture in `imbue/mngr/e2e/conftest.py` that wrote an invalid `settings.local.toml`: the `[commands.create]` table contained a duplicate `type = "claude"` key (introduced by a botched merge), which made every `mngr` invocation fail with "Cannot overwrite a value". This unblocks the e2e tutorial config tests (e.g. `test_config_path`).

Fixed the e2e test fixture's `settings.local.toml`, which wrote a duplicate `type = "claude"` key under `[commands.create]`. The duplicate is invalid TOML and caused `mngr config set` (which re-parses the file with a strict editing parser) to fail with "Cannot overwrite a value". Also added an explicit 60s timeout to `test_config_set_default_provider`, which runs two `mngr` invocations and was exceeding the 10s default.

Fixed the `test_config_set_headless` e2e tutorial test. The e2e fixture now seeds the project `settings.toml` with the pytest opt-in, so the test no longer appends a duplicate `is_allowed_in_pytest` key (which produced a TOML "Cannot overwrite a value" parse error). Also removed a duplicate `type = "claude"` key that the fixture wrote into `settings.local.toml`, which broke `mngr config set` whenever it loaded the merged config via tomlkit. Strengthened the test to assert the value lands in the project scope and is actually written to the on-disk `settings.toml`.

Fixed the e2e test fixture that seeded an invalid `settings.local.toml`: a bad
merge had added a duplicate `type = "claude"` key under `[commands.create]`,
which made the file unparseable TOML and broke `test_config_set_scope` (and any
other config test that loads the local layer).

Fixed the e2e config tests: the shared `e2e` fixture wrote a duplicate `type = "claude"` key into the local `settings.toml`, which is invalid TOML and broke `mngr config set` of an unknown key. Also updated `test_config_set_unknown_key_fails` to verify the rejected key is absent from the project settings file (the fixture now seeds that file with the pytest opt-in) rather than asserting the file does not exist.

Fixed the e2e test fixture that seeded a malformed `settings.local.toml` with a
duplicate `type = "claude"` key under `[commands.create]`. The duplicate caused
`mngr config set` (which round-trips the file through tomlkit) to fail with
"Cannot overwrite a value", breaking `test_config_set` and other config tests.

Fixed the e2e test fixture's `settings.local.toml`, which contained a duplicate
`type = "claude"` key under `[commands.create]` and was therefore invalid TOML.
Any e2e command that performed a full merged-config load (e.g. `mngr config
unset`) failed with a "Failed to parse config file" error instead of running.
Also tightened `test_config_unset_missing_key` to assert the error names the
specific missing key.

Fixed the e2e tutorial test for `mngr config unset`. The shared e2e fixture
wrote a `settings.local.toml` with a duplicate `type = "claude"` key under
`[commands.create]`, which made `tomllib` reject the file and broke config
loading for every e2e mngr command. Removed the duplicate. Also reworked
`test_config_unset` to set `commands.create.provider` before unsetting it (a
key that is never set cannot be unset) and to verify the value is actually
removed from the project settings file.

Fixed the e2e tutorial test fixture, which wrote an invalid `settings.local.toml` with a
duplicated `type = "claude"` key under `[commands.create]`. This caused every command run
through the fixture to abort with a TOML parse error ("cannot overwrite a value") instead of
exercising the actual code path.

Also strengthened `test_connect_by_agent_id_fictional` to assert that connecting to a
nonexistent agent id reports a clean user-facing "not found" error with no leaked Python
traceback.

Fixed the e2e tutorial connect tests (`test_connect.py`). The shared e2e
fixture wrote a `settings.local.toml` with a duplicate `type = "claude"` key
under `[commands.create]`, which made every `mngr create` in these tests fail
with a TOML parse error; removed the duplicate. The interactive
`run_connect_interactively` helper now clears the inherited `$TMUX`/`$TMUX_PANE`
and forces the builtin tmux attach (via `MNGR_CONNECT_COMMAND_ACTIVE`) so the
standalone `mngr connect` performs a real attach instead of being intercepted by
the no-op connect command or refused by the nested-tmux guard. The four
interactive connect tests also got a `@pytest.mark.timeout(120)` since the
create/attach/detach flow exceeds the default 10s per-test timeout. Test-only
change; no user-facing behavior changed.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated `settings.local.toml`, which caused a TOML parse error ("cannot overwrite a value") and broke e2e tutorial tests. No user-visible behavior change.

Fixed the e2e test fixture (`conftest.py`) that wrote an invalid `settings.local.toml` with a duplicate `type = "claude"` key under `[commands.create]`. The duplicate key caused a TOML parse error ("cannot overwrite a value") that broke every e2e tutorial command, including `test_connect_explicit_host`.

Fixed the e2e tutorial connect tests (`test_connect.py`):

- Repaired the e2e fixture, which wrote a duplicate `type = "claude"` key into
  `settings.local.toml` and caused every `mngr create` in the e2e suite to fail
  with a TOML "Cannot overwrite a value" parse error.
- Added `@pytest.mark.timeout(120)` to the interactive `mngr connect` tests, which
  perform a full agent create plus interactive connect and exceed the default 10s
  per-test timeout.
- Added `test_connect_no_start_fails_when_stopped`, covering the documented
  unhappy path for `mngr connect --no-start` (it refuses to connect to a stopped
  agent rather than auto-starting it).

Fixed the e2e tutorial connect tests. The shared e2e fixture wrote a `settings.local.toml` with a duplicated `type = "claude"` key under `[commands.create]`, which made every `mngr` invocation fail with a TOML "Cannot overwrite a value" parse error; the duplicate line is removed. Also added a `@pytest.mark.timeout(120)` override to `test_connect_short_form`, which drives an interactive `mngr conn` attach that polls for up to 30s and so needs more than the 10s global pytest timeout.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated
`settings.local.toml`, which caused every e2e tutorial test to fail with a TOML
"Cannot overwrite a value" parse error when running `mngr create`.

Added `test_connect_with_start_restarts_stopped_agent`, an e2e test that shares
the `mngr connect --start` tutorial block but stops the agent first, verifying
that `--start` actually restarts a stopped agent (the existing test only connects
to an already-running agent, where `--start` is a no-op).

Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated
`settings.local.toml` that produced invalid TOML ("Cannot overwrite a value"),
causing `mngr` config loading to fail in e2e tutorial tests. Also strengthened
the `test_control_mngr_via_env_rejects_invalid_value` release test to assert on
the specific "Unknown provider backend" rejection message.

Fixed the e2e test fixture (`conftest.py`) which wrote a duplicate `type = "claude"` key into the generated `settings.local.toml`, causing all e2e tutorial tests to fail config parsing ("Cannot overwrite a value"). Also tightened `test_create_agent_args_require_dash_separator` to assert on the specific unrecognized-option failure mode.

Fixed the e2e test fixture so tutorial tests can run again, and strengthened the
create-and-destroy tutorial test.

- The e2e conftest fixture wrote a `settings.local.toml` with a duplicate
  `type = "claude"` key under `[commands.create]` (a merge artifact). tomlkit
  rejects duplicate keys, so every e2e tutorial command failed up front with
  "Cannot overwrite a value". Removed the duplicate line.
- `test_create_and_destroy_agent` now asserts the agent appears in `mngr list`
  before it is destroyed, making the post-destroy absence check a real
  before/after contrast.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`), which wrote a
`settings.local.toml` with a duplicate `type = "claude"` key under `[commands.create]`.
TOML rejects the duplicate ("Cannot overwrite a value"), which made every e2e command
(`mngr create`, etc.) fail to parse its config. Removed the redundant line.

Added `@pytest.mark.timeout(300)` to the `test_rename.py` e2e tests so that
`mngr list --format json` has time to complete remote-provider discovery (matching the
convention used by the other create+list e2e tests), and strengthened
`test_create_and_rename_agent` to verify the renamed agent is preserved in place (same
command, still alive) rather than only checking its name.

Fixed the `test_create_codex_agent` e2e tutorial test. The e2e fixture was
writing a duplicate `type = "claude"` key into `[commands.create]` in the
generated `settings.local.toml`, which produced invalid TOML and broke any
command (such as `mngr config set --scope local`) that loads and re-saves that
file via tomlkit. Removed the duplicate. Also added a `@pytest.mark.timeout(120)`
to the test (it runs three sequential mngr operations that each perform full
provider discovery, exceeding the default 10s timeout) and removed the spurious
`@pytest.mark.modal` (the test only creates a local agent and never invokes the
modal CLI binary the resource guard tracks).

Fixed the e2e test fixture that seeded `settings.local.toml` with a duplicate
`type = "claude"` key under `[commands.create]`. The duplicate parsed on initial
load but caused `mngr config set --scope local` to fail with "Cannot overwrite a
value" when it re-saved the file, breaking `test_create_codex_explicit_type`.

Fixed the e2e tutorial test fixture and the `test_create_codex_positional` release test.

- The e2e fixture's `settings.local.toml` was emitting a duplicate `type = "claude"` key under
  `[commands.create]`, producing malformed TOML. Any `mngr config set` that re-parsed the merged
  config then failed with "Cannot overwrite a value". Removed the duplicate line.
- `test_create_codex_positional` now scopes its verification `mngr list` to `--provider local`
  (the agent is created locally) and raises the per-test timeout to 120s, matching the sibling
  `test_create_codex_explicit_type`. The previous unscoped `mngr list` fanned out to every
  provider (including Modal) and exceeded the default 10s timeout. The test also now asserts the
  created agent reached a RUNNING/WAITING state, not just that it has the codex type.

Fixed the e2e tutorial test fixture and the `--type command -- <cmd>` create test.

- Removed a duplicate `type = "claude"` key under `[commands.create]` in the e2e
  `settings.local.toml` that the fixture writes (`e2e/conftest.py`). The duplicate key made
  the file invalid TOML, so every `mngr` invocation in an e2e test aborted with
  "Cannot overwrite a value".
- Added `@pytest.mark.timeout(120)` to
  `test_create_command_agent_runs_post_dash_command_in_agent`, matching its sibling
  real-create tests. A real create (tmux session + asciinema connect, plus a one-time ttyd
  install) followed by `mngr exec` and `mngr list` routinely exceeds the default 10s
  function timeout.

Fixed the e2e test fixture's generated `settings.local.toml`, which had a duplicate `type = "claude"` key under `[commands.create]`. The duplicate produced invalid TOML ("Cannot overwrite a value"), causing every e2e command to fail to parse its config. Removed the duplicate line so the fixture writes valid TOML again.

Strengthened `test_create_command_custom_script` to confirm the forwarded command is actually running as a process inside the agent (via `mngr exec ... ps`), rather than only checking the recorded metadata and state.

Fixed the e2e tutorial test fixture so the `command`-type agent tutorial tests run again.

- The shared `e2e` fixture (`e2e/conftest.py`) was writing a `settings.local.toml` with a
  duplicate `type = "claude"` key under `[commands.create]`, which is invalid TOML and made
  every `mngr` invocation in the affected e2e tests fail with "Cannot overwrite a value". Removed
  the duplicate key.
- `test_create_command_python_http` now carries `@pytest.mark.timeout(120)` (matching its
  sibling `test_create_command_custom_script`), since creating a command agent plus the
  follow-up `mngr list`/`mngr exec` provider discovery can exceed the default 10s per-test
  timeout when a remote provider is unreachable.

Fixed the e2e tutorial test fixture and the `test_create_copy` release test.

- The e2e `settings.local.toml` written by the test fixture contained a duplicate
  `type = "claude"` key under `[commands.create]`, which made TOML parsing fail for
  every `mngr` command in the e2e tutorial suite. The duplicate has been removed.
- `test_create_copy` carried a spurious `@pytest.mark.modal` mark even though it only
  creates a local agent with a local git-mirror transfer; the resource guard's
  NEVER_INVOKED check failed the test. The mark has been removed.
- Strengthened `test_create_copy` to verify the git-mirror copy is a functional
  repository that carries over the source repo's commit history (via `git log`), not
  merely a directory containing a `.git` folder.

Fixed the `test_create_custom_yolo_agent_type` e2e tutorial test. The shared e2e
fixture wrote a duplicate `type = "claude"` key into `settings.local.toml`,
producing invalid TOML that made `mngr config edit` fail to parse the config. The
test also configured the custom `yolo` agent type with only a `command` (no
`parent_type`), so the type could not resolve to a concrete agent class. The test
now points `yolo` at the built-in `command` parent, scopes its verification
`mngr list` to the local provider, and drops the spurious `@pytest.mark.modal`
mark (the test only exercises the local provider).

Fixed a duplicate `type = "claude"` key under `[commands.create]` in the e2e tutorial test fixture's `settings.local.toml`. The duplicate caused a TOML "Cannot overwrite a value" parse error that broke `mngr create` in every e2e tutorial test.

Fixed the e2e tutorial test fixture (`e2e/conftest.py`) that wrote an invalid
`settings.local.toml` with a duplicate `type = "claude"` key under
`[commands.create]`, which caused `mngr create` to fail with a TOML parse error
("Cannot overwrite a value") in every e2e tutorial test. Also added a
`@pytest.mark.timeout(120)` mark to `test_create_default_branch`, which was
exceeding the default 10s per-test timeout because it runs `mngr create` plus
several `mngr exec` commands.

Fixed the e2e test fixture, which wrote a duplicate `type = "claude"` key into the generated `settings.local.toml`, producing invalid TOML that made every `mngr` subprocess fail to parse its config. Also gave `test_create_default_project_label` a 120s timeout (matching its sibling agent-creation tests) so it no longer trips the 10s default pytest timeout while `mngr list` performs provider discovery.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote a
`settings.local.toml` containing a duplicate `type = "claude"` key under
`[commands.create]`. The duplicate produced invalid TOML, causing every e2e
tutorial test that loads the local config to fail with "Cannot overwrite a
value". Removed the duplicate line.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote a
duplicate `type = "claude"` key under `[commands.create]` in the generated
`settings.local.toml`. The duplicate key produced invalid TOML, so every e2e
command that loaded the merged config failed with `Failed to parse config file
... Cannot overwrite a value`. Removed the redundant line so the fixture emits
valid TOML.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) which wrote a
`settings.local.toml` containing a duplicate `type = "claude"` key under
`[commands.create]`. The duplicate (a merge artifact) made the file invalid TOML,
so every `mngr create` invoked from an e2e test aborted with
`Failed to parse config file ...: Cannot overwrite a value`. Removing the duplicate
key restores the fixture so the docker (and all other) tutorial e2e tests can run.

Fixed the e2e test fixture's `settings.local.toml` template, which contained a
duplicate `type = "claude"` key under `[commands.create]`. The duplicate made
the file invalid TOML, so every `mngr` command in the docker tutorial e2e tests
failed with "Cannot overwrite a value" instead of exercising the docker provider.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated
`settings.local.toml` (`libs/mngr/imbue/mngr/e2e/conftest.py`). The duplicate
produced a "Cannot overwrite a value" TOML parse error that caused every e2e
tutorial `mngr` command to exit 1 before reaching the provider. This unblocks
the Docker create tutorial tests (and all other e2e tests sharing the fixture).

Fixed the e2e test fixture (`mngr/e2e/conftest.py`) which wrote a `settings.local.toml` containing a duplicate `type = "claude"` key under `[commands.create]`. The duplicate key is invalid TOML and caused every Docker e2e tutorial test (e.g. `test_create_docker_volume_start_arg`) to fail with "Cannot overwrite a value" when `mngr` parsed the config file.

Fixed the e2e test fixture so `mngr` commands run again: the generated
`settings.local.toml` had a duplicate `type = "claude"` key under
`[commands.create]`, which made the config fail to parse with "Cannot overwrite
a value" and broke every e2e test. Also gave `test_create_duplicate_name_fails`
a 120s timeout (matching sibling e2e tests) since it creates a live agent and
runs `mngr list`, whose provider discovery can exceed the default 10s timeout.

Fixed the e2e test fixture, which wrote an invalid `settings.local.toml` containing a duplicate `type = "claude"` key under `[commands.create]`. This caused every e2e tutorial test to fail with a "Cannot overwrite a value" TOML parse error. Also gave `test_create_from_another_agent` a 120s timeout (it runs two `mngr create` operations plus a clone) and added `test_create_from_another_agent_source_alias` to cover the documented `--source` alias for `--from`.

Test maintenance for the tutorial e2e suite (no user-facing behavior change):

- Fixed the e2e test fixture so it no longer writes an invalid `settings.local.toml`. The
  `[commands.create]` section had a duplicate `type = "claude"` key, which made every `mngr`
  command in an e2e tutorial test fail with "Cannot overwrite a value" while parsing the config.
- Strengthened `test_create_git_mirror_with_existing_branch`: it now also verifies that the
  agent's git mirror is checked out at the same commit the existing branch points to in the
  source repo (not merely that a same-named branch exists). Hardened its `mngr exec` verification
  calls with a longer timeout to absorb agent/provider-discovery latency under local load.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) which wrote a
duplicate `type = "claude"` key into the generated `settings.local.toml`. TOML
forbids duplicate keys, so every e2e tutorial test using this fixture failed with
"Cannot overwrite a value" while parsing the config. Removed the duplicate.

Also gave `test_create_headless` the same `@pytest.mark.timeout(120)` its sibling
multi-operation tests carry (it runs create + list + exec, exceeding the 10s
default), and strengthened its assertion to verify the headless agent actually
runs inside its dedicated worktree rather than only checking the `exec` exit code.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) so the generated
`settings.local.toml` no longer wrote `type = "claude"` twice under `[commands.create]`.
The duplicate key produced invalid TOML ("Cannot overwrite a value"), which made every
e2e tutorial command fail to parse its config. Removing the duplicate restores the intended
single default agent type and unblocks the e2e tutorial tests (e.g. `test_create_headless`).

Fixed the e2e test fixture, which wrote a `settings.local.toml` containing a
duplicate `type = "claude"` key under `[commands.create]`. TOML rejects a
repeated key, so every `mngr` command run through the e2e fixture failed with a
config parse error. Removing the duplicate restores the e2e suite.

Strengthened `test_create_help_succeeds` to assert that `mngr create --help`
emits the command's own NAME summary, SYNOPSIS, and EXAMPLES sections (not just
two flag strings), confirming the help genuinely belongs to the `create` command.

Fixed the e2e tutorial test fixture (`conftest.py`) that wrote a duplicate `type = "claude"` key into `settings.local.toml` under `[commands.create]`, producing invalid TOML and causing `mngr create` to fail with "Cannot overwrite a value" during e2e tutorial tests.

Fixed the e2e test fixture that seeded a malformed `settings.local.toml` with a
duplicate `type = "claude"` key under `[commands.create]`, which made every
`mngr create` invoked from an e2e tutorial test fail with a TOML "Cannot
overwrite a value" parse error. Removed the duplicate key.

Fixed the e2e test fixture that generated an invalid `settings.local.toml`: the
`[commands.create]` table set `type = "claude"` twice, which is not valid TOML
("Cannot overwrite a value") and caused every e2e tutorial test to fail at config
load. Removed the duplicate key.

Also added an e2e test (`test_create_unnamed_agent_gets_random_name`) covering the
documented behavior that `mngr create` with no name argument generates a random
agent name.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote an
invalid `settings.local.toml` with a duplicate `type = "claude"` key under
`[commands.create]`. The duplicate key caused every e2e test using this fixture
to fail with "Cannot overwrite a value" when mngr parsed the config, before any
command logic ran. Removed the duplicate line.

Fixed the e2e test fixture (`e2e/conftest.py`) that wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`, which made the file invalid TOML and caused every `mngr` command in e2e tests to fail with a config parse error. Also strengthened `test_create_short_forms` to exercise the tutorial's positional agent-type argument (`mngr create my-task command`) and to assert that the resolved agent type and stand-in command are correct.

Fixed the e2e test fixture so the release tests in `test_create_basic.py` run again.

- The shared e2e `settings.local.toml` fixture (in `e2e/conftest.py`) wrote `type = "claude"`
  twice under `[commands.create]`, producing invalid TOML. Every `mngr` invocation in these
  tests aborted with "Cannot overwrite a value" before doing any work. Removed the duplicate
  key.
- Added `@pytest.mark.timeout(120)` to `test_create_with_agent_args`, which runs two sequential
  `mngr` operations (create, list) each performing full provider discovery and so exceeds the
  default 10s pytest-timeout.

Fixed the e2e test fixture (`imbue/mngr/e2e/conftest.py`) that wrote `type = "claude"` twice into the `[commands.create]` table of the generated `settings.local.toml`. The duplicate key produced invalid TOML ("Cannot overwrite a value"), causing every tutorial e2e test that runs `mngr create` to fail with a config parse error. Removed the duplicate line.

Fixed a malformed `settings.local.toml` written by the e2e test fixture: a duplicate `type = "claude"` key under `[commands.create]` caused every `mngr` command in the e2e tutorial tests to abort with a TOML "Cannot overwrite a value" parse error. Removing the duplicate line restores the e2e tutorial release tests (e.g. `test_create_with_connect_command`).

Fixed the e2e test fixture (`imbue/mngr/e2e/conftest.py`) that wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`. The duplicate key caused TOML parsing to fail with "Cannot overwrite a value", breaking e2e tutorial tests including `test_create_with_custom_branch_pattern`.

Fixed the e2e test fixture that wrote a duplicate `type = "claude"` key under `[commands.create]` in `settings.local.toml`, which caused every config-loading e2e command to fail with a TOML "Cannot overwrite a value" parse error instead of running. Also strengthened `test_create_with_dirty_tree_fails` to verify no agent is left behind when the clean-working-tree guard aborts.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) so it no
longer writes a duplicate `type = "claude"` key into the per-test
`settings.local.toml`. The duplicate made tomlkit reject the config file
("Cannot overwrite a value"), causing every e2e tutorial test that creates an
agent to fail. This affected `test_create_with_env_file` and its siblings in
`test_env_vars.py`.

Also added `test_create_with_missing_env_file_is_rejected`, an unhappy-path e2e
test covering the same `--env-file` tutorial block: it verifies that pointing
`--env-file` at a nonexistent file is rejected with a clear error and creates no
agent.

Fixed two e2e test-fixture issues that broke the create-time env-var tutorial tests:

- The e2e fixture wrote a duplicate `type = "claude"` key into the same
  `[commands.create]` table of `settings.local.toml` (a stray line left by a
  bulk merge), which is invalid TOML and made *every* e2e command fail with
  "Cannot overwrite a value". Removed the duplicate.
- `test_create_with_env_vars` still carried a stale `@pytest.mark.modal` even
  though it creates on the default provider and never invokes Modal (its sibling
  default-provider tests had the mark removed in the same merge). The resource
  guard correctly flagged the superfluous mark; removed it.

Fixed the e2e tutorial test `test_create_with_env` and its shared fixture:

- Removed a duplicate `type = "claude"` key in the e2e `settings.local.toml`
  written by the `e2e` fixture, which made the file invalid TOML and caused
  every `mngr create` in the e2e tutorial tests to fail with a config parse
  error.
- Reworked the `--env` test to launch its agent body via `bash -c '...'` so the
  compound command and `$MNGR_TEST_VAR` expansion run inside the agent's shell,
  instead of being collapsed into a single (non-existent) command word by the
  command agent's per-argument shell quoting.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated `settings.local.toml`, which caused a TOML parse error ("Cannot overwrite a value") and broke `mngr` commands in all e2e tutorial tests.

Fixed the e2e tutorial test fixture: `settings.local.toml` was written with a duplicate
`type = "claude"` key under `[commands.create]`, which made every `mngr` command in the e2e
tutorial suite fail to parse its config ("Cannot overwrite a value"). Removed the duplicate key.

Also strengthened `test_create_with_extra_tmux_windows` to verify that the extra tmux windows
are actually running their configured commands (not just that windows with the right names
exist), matching the tutorial's promise that `-w name="cmd"` starts a window running that command.

Fixed the e2e test fixture (`conftest.py`) which wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`. The duplicate key caused every command in affected e2e tests to fail with a TOML parse error ("Cannot overwrite a value") before reaching the behavior under test. This unblocks `test_create_with_invalid_label_format` and other e2e tutorial tests.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) which wrote a
duplicate `type = "claude"` key into the generated `settings.local.toml`, making the
file unparseable and causing every e2e `mngr` command to fail with "Cannot overwrite
a value". Removed the duplicate key.

Strengthened `test_create_with_json_output` to verify that the `agent_id`/`host_id`
returned by `mngr create --format json` actually match the agent reported by
`mngr list --format json`, and that the agent is in a running state -- confirming the
machine-readable identifiers are real and usable for scripting, not just well-formed.

Fixed the e2e test fixture, which wrote a duplicate `type` key into its generated `settings.local.toml` and caused every e2e `mngr` command to fail config parsing. Added an e2e test that the `mngr create --quiet` flag suppresses all console output while still creating the agent.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's
`settings.local.toml` that made the file invalid TOML, causing
`test_create_with_label` (and any e2e test loading local config) to fail with
"Cannot overwrite a value".

Extended `test_create_with_label` to verify labels and host labels actually
drive `mngr list` filtering (matching values include the agent, non-matching
values exclude it).

Fixed the e2e test fixture (`conftest.py`) which wrote a duplicate `type = "claude"` key into the `[commands.create]` block of `settings.local.toml`, producing invalid TOML that made every `mngr create` in the release e2e suite fail with a config parse error. Also added an unhappy-path e2e test (`test_create_rejects_malformed_label`) covering a `--label` value that is not in KEY=VALUE format.

Fixed the e2e test fixture's `settings.local.toml` generation, which wrote a
duplicate `type = "claude"` key under `[commands.create]` and caused every e2e
`mngr create` to fail with a TOML "Cannot overwrite a value" parse error.

Strengthened `test_create_with_message` to verify the initial message is
actually delivered into the agent's tmux pane (via `tmux capture-pane`), rather
than only checking that mngr logged "Sending initial message".

Fixed the e2e tutorial test fixture, which wrote a duplicate `type = "claude"`
key under `[commands.create]` in `settings.local.toml`, making the file
unparseable and breaking every e2e tutorial test that depended on it. Added a
120s function-timeout override to `test_create_with_no_ensure_clean` (a real
create plus `mngr list` exceeds the default 10s), and added an unhappy-path test
verifying that `mngr create` aborts on a dirty working tree when
`--no-ensure-clean` is omitted.

Fixed a duplicated `type = "claude"` key in the e2e test fixture's generated
`settings.local.toml`, which made `tomllib` reject the file and caused every
`mngr` command in e2e tests to fail with a config parse error instead of
exercising the real code path. Also strengthened the nonexistent-base-branch
e2e test to assert the failure is actually about the missing base branch, so it
can no longer pass for an unrelated reason.

Fixed the e2e tutorial test fixture and broadened coverage of the `mngr create --pass-env`
tutorial block.

- Removed a duplicate `type = "claude"` key the `e2e` fixture wrote into the generated
  `settings.local.toml`. The duplicate produced invalid TOML, so every `mngr` command in the
  e2e/tutorial release suite failed with `Cannot overwrite a value`.
- Added `test_create_with_pass_env_skips_unset_var`, an unhappy-path test for the same
  `--pass-env` tutorial block: forwarding a variable that is not set in the current shell does
  not fail `create`; the variable is simply absent from the agent's environment.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) which wrote a duplicate `type = "claude"` key into `[commands.create]` in the generated `settings.local.toml`, causing every e2e tutorial test to fail with a TOML "Cannot overwrite a value" parse error. Also added the `@pytest.mark.timeout(120)` marker to the `test_create_with_pass_env` and `test_create_with_pass_env_unset` tutorial tests so they no longer hit the default 10s pytest timeout while running multiple `mngr` subprocess commands.

Fixed the e2e tutorial test fixture that wrote a duplicate `type = "claude"` key into the generated `settings.local.toml`, which produced an invalid-TOML parse error and broke every e2e tutorial test. Added a `@pytest.mark.timeout(120)` to `test_create_with_pass_env` so it matches its sibling tests and does not hit the default 10s timeout during the slow `mngr list` provider-discovery step.

Strengthened `test_create_with_pass_env` to additionally exec into the running agent and assert the forwarded `API_KEY` is visible in its live environment, not just in the on-disk env file.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated `settings.local.toml` that caused a TOML parse error ("Cannot overwrite a value") in all e2e tutorial tests. Also strengthened `test_create_with_plugin_flags` to verify the failed create leaves no agent behind.

Fixed the e2e tutorial test fixture: removed a duplicate `type = "claude"` key
in the generated `settings.local.toml` that produced invalid TOML and broke
`mngr create` across e2e tests. Also added a `@pytest.mark.timeout(120)` mark to
`test_create_with_project_label` so its multi-step `mngr` subprocess calls are
not killed by the global 10s pytest timeout.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) which wrote a
duplicate `type = "claude"` key under `[commands.create]` in the generated
`settings.local.toml`. TOML rejects the duplicate key, causing every e2e test
that loads this config (including `test_create_with_quiet_output`) to fail with
"Cannot overwrite a value". Removed the duplicate line.

Fixed the e2e test fixture (`conftest.py`) that wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`. The duplicate key produced invalid TOML ("Cannot overwrite a value"), causing every `mngr` command run inside the e2e tests to fail config parsing. Removing the duplicate restores valid config and unblocks the affected e2e tests.

Fixed the e2e tutorial test fixture, which wrote a duplicate `type = "claude"` key into `[commands.create]` in the generated `settings.local.toml`. The duplicate key made the file invalid TOML, causing every e2e tutorial command to fail with a config parse error ("Cannot overwrite a value").

Also strengthened `test_create_with_source_path_no_git` to assert that a non-git source folder produces no agent git branch and that the agent's work directory is not a git repository, reinforcing the tutorial's claim that mngr does not require git.

Fixed the e2e test fixture's generated `settings.local.toml` which had a duplicate `type = "claude"` key under `[commands.create]`, causing all e2e tutorial tests using the fixture to fail at config-parse time with "Cannot overwrite a value".

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) so the seeded `settings.local.toml` no longer contains a duplicate `type = "claude"` key under `[commands.create]`. The duplicate produced invalid TOML, causing every e2e tutorial command that loads this config to fail with a "Cannot overwrite a value" parse error (most visibly breaking `test_create_with_template_modal_disabled`, which then failed for the wrong reason).

Fixed the e2e test fixture and expanded coverage for `mngr create --template`.

- The e2e `settings.local.toml` written by the test fixture contained a duplicate
  `type = "claude"` key under `[commands.create]`, which made tomlkit refuse to parse the
  config ("Cannot overwrite a value") and broke every e2e test that loaded it. Removed the
  duplicate line.
- Added `test_create_with_nonexistent_template`, an unhappy-path companion to
  `test_create_with_template`, verifying that creating with an unconfigured template name
  fails with a helpful error that names the missing template and lists the available ones,
  and that no agent is created.

Fixed the e2e test fixture in `conftest.py` that wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`. The duplicate produced invalid TOML, causing every e2e tutorial test (including `test_create_with_transfer_none`) to fail config parsing with "Cannot overwrite a value".

Fixed the e2e test fixture (`e2e/conftest.py`) so that the `settings.local.toml` it writes no
longer contains a duplicate `type = "claude"` key under `[commands.create]`. The duplicate key
caused tomlkit to fail config parsing ("Cannot overwrite a value"), which made every e2e tutorial
test (including the Docker tutorial tests) fail before running any `mngr` command.

Fixed the e2e test fixture that wrote a duplicate `type = "claude"` key into
`settings.local.toml`, which made every e2e tutorial command fail to parse its
config. Also corrected the `test_destroy_all_via_stdin` release test: it only
manages local agents, so it never invokes Modal and no longer carries the
`@pytest.mark.modal` mark. Strengthened the test to assert that both agents are
reported as destroyed by the piped `mngr list --ids | mngr destroy - --force`
command.

Fixed the release e2e test `test_destroy_by_session_name_happy_path` (destroy section).

- Fixed the shared e2e fixture (`e2e/conftest.py`): the generated `settings.local.toml` had a
  duplicate `type = "claude"` key inside `[commands.create]` (introduced by a squashed
  conflict resolution), which made tomlkit reject the file with "Cannot overwrite a value" and
  broke `mngr create` for every e2e test using the fixture. Removed the duplicate line.
- Removed the stale `@pytest.mark.modal` marker from the test. It only creates a local
  `command`-type agent and destroys it by tmux session name; it never invokes the bare `modal`
  CLI (the only path the resource guard observes), so the guard failed the test with "marked
  with @pytest.mark.modal but never invoked modal". The mark has no effect on CI selection
  (release offload filters by `release`).

Fixed the e2e test fixture, which wrote a duplicate `type = "claude"` key under
`[commands.create]` in the generated `settings.local.toml`, causing every e2e
test to fail with a TOML parse error ("Cannot overwrite a value"). The default
agent type is now written exactly once.

Strengthened `test_destroy_multiple_at_once` to also assert on the
"Successfully destroyed 3 agent(s)" summary line, verifying that a single
`mngr destroy a b c --force` command tears down the exact number of agents
requested.

Fixed the e2e test fixture that wrote an invalid `settings.local.toml` containing a duplicate `type = "claude"` key under `[commands.create]`, which made TOML parsing fail and broke agent creation across e2e tests. Also added the missing `@pytest.mark.timeout(120)` marker to `test_destroy_no_gc` so it no longer falls back to the too-short default timeout.

Fixed the e2e test fixture's generated `settings.local.toml`, which contained a
duplicate `type = "claude"` key under `[commands.create]` and caused every e2e
tutorial test to fail with a TOML parse error during `mngr create`.

Added `test_destroy_keeps_branch_by_default`, a companion test for the
`mngr destroy --remove-created-branch` tutorial block that verifies the documented
safe default: a plain destroy leaves the agent's git branch intact.

Fixed the e2e test fixture's generated `settings.local.toml`, which contained a
duplicate `type = "claude"` key under `[commands.create]` that made TOML parsing
fail ("Cannot overwrite a value") and broke `mngr create` in every tutorial e2e
test. Also removed an incorrect `@pytest.mark.modal` from
`test_destroy_short_form_running_requires_force`: that unhappy-path test refuses
to destroy a running local agent without `--force`, so it never invokes modal.

Fixed the e2e tutorial test fixture so the `mngr rm` short-form destroy release test passes. The shared `settings.local.toml` written by the e2e fixture had a duplicate `type = "claude"` key under `[commands.create]` (a merge artifact), which made every `mngr create` in the e2e tutorial tests fail with a TOML "Cannot overwrite a value" parse error. Also removed the superfluous `@pytest.mark.modal` mark from `test_destroy_short_form`, which only creates a local `command`-type agent and never invokes the `modal` binary, so the resource guard failed it with a "marked modal but never invoked modal" violation.

Fixed a duplicate `type = "claude"` key in the e2e test fixture's `settings.local.toml`, which caused every e2e test to fail at agent creation with a TOML "Cannot overwrite a value" parse error.

Fixed the e2e tutorial test fixture: removed a duplicate `type = "claude"` key in the generated `settings.local.toml`, which caused a TOML parse error ("Cannot overwrite a value") that broke all e2e tutorial tests using the shared fixture.

Fixed the e2e tutorial test fixture (`E2eSession`) that wrote an invalid `settings.local.toml` containing a duplicate `type = "claude"` key under `[commands.create]`, which made every `mngr` command in the e2e tutorial tests fail to parse its config. Also added a `@pytest.mark.timeout(60)` override to `test_env_var_mngr_headless`, which makes several sequential `mngr` subprocess calls and was timing out under the default 10s limit.

Fixed the e2e test fixture (`conftest.py`) that wrote an invalid `settings.local.toml`: the `[commands.create]` table contained a duplicate `type = "claude"` key, which TOML rejects ("Cannot overwrite a value"). This broke `mngr create` in every e2e test. Also removed a duplicate `_parse_jsonl_events` helper definition in `test_event.py`.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote a
duplicate `type = "claude"` key into the generated `settings.local.toml`,
producing invalid TOML that made every tutorial e2e agent-creation command fail
with "Cannot overwrite a value". The fixture now writes the default agent type
once. This unblocks the `mngr event` tutorial e2e tests (including
`test_event_follow_filter_source`).

Fixed the e2e tutorial test fixture so agent creation works again. The shared
`settings.local.toml` written by the e2e session fixture contained a duplicate
`type = "claude"` key under `[commands.create]`, which made the config file
invalid TOML and caused every `mngr create` in the tutorial e2e tests to fail
with "Cannot overwrite a value". Removed the duplicate key.

Also removed a redundant second definition of the `_parse_jsonl_events` helper
in `test_event.py` that shadowed the stronger original (the original asserts
each parsed line is a JSON object).

Fixed the e2e tutorial test fixture so `mngr event` tutorial tests can create agents again.

- The e2e fixture wrote a duplicate `type = "claude"` key inside `[commands.create]` in the
  generated `settings.local.toml`, producing invalid TOML. Every tutorial command that creates
  an agent failed with "Cannot overwrite a value". Removed the duplicate key.
- Test-only cleanup in `test_event.py`: removed a duplicated `_parse_jsonl_events` helper (the
  second, weaker definition shadowed the first) and strengthened
  `test_event_head_conflicts_with_tail` to assert that no events are emitted to stdout when
  `--head` and `--tail` are combined.

Test-only changes (no user-visible behavior change):

- Fixed the e2e tutorial test fixture (`e2e/conftest.py`): the generated
  `settings.local.toml` had a duplicate `type = "claude"` key under
  `[commands.create]`, which made TOML parsing fail with "Cannot overwrite a
  value". This broke agent creation for every e2e tutorial test. Removed the
  redundant key.
- Cleaned up `e2e/tutorial/test_event.py`: removed a duplicate
  `_parse_jsonl_events` definition that shadowed the stricter one, and
  strengthened `test_event_head` to assert that `--head` returns the leading
  prefix of the full event stream (the earliest events, not the tail).

Fixed the e2e tutorial test fixture, which wrote an invalid `settings.local.toml` containing a
duplicate `type = "claude"` key under `[commands.create]`. This caused every tutorial e2e test
that creates an agent to fail with a TOML parse error ("Cannot overwrite a value"). The duplicate
line was removed so the generated config is valid.

Also strengthened `test_event_include_filter_rejects_invalid_cel` to assert that a rejected
`--include` CEL filter produces no event output on stdout, verifying the command fails loudly
rather than silently emitting events.

Fixed the e2e test fixture that generated an invalid `settings.local.toml`: the
`[commands.create]` table contained a duplicate `type = "claude"` key, which made
tomlkit reject the file ("Cannot overwrite a value") and caused every `mngr create`
in the e2e tutorial tests to fail. Removed the duplicate key. Also removed a
shadowing duplicate `_parse_jsonl_events` helper in the event tutorial tests so the
stricter (object-asserting) parser is the one actually used.

Fixed the e2e tutorial test fixture so the generated `settings.local.toml` no longer emits a duplicate `type = "claude"` key under `[commands.create]`, which produced an invalid-TOML parse error ("Cannot overwrite a value") and broke `mngr create` in the tutorial event tests. Also strengthened `test_event_tail` to verify the `--tail 20` JSONL contract (at most 20 events, each carrying the guaranteed fields), matching its sibling event tests, and removed a duplicate `_parse_jsonl_events` helper definition.

Fixed the e2e tutorial test fixture (`conftest.py`) that wrote an invalid `settings.local.toml` with a duplicated `type = "claude"` key under `[commands.create]`. The duplicate key caused a TOML parse error ("Cannot overwrite a value") during `mngr create`, breaking agent creation in the e2e tutorial tests.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote an
invalid `settings.local.toml`: the `[commands.create]` table defined `type = "claude"`
twice, which TOML rejects ("Cannot overwrite a value"). This caused every `mngr create`
in the e2e tutorial suite to fail at setup with a config-parse error. Removed the
duplicate key so the fixture writes a single `type = "claude"` default, unblocking the
`mngr exec` tutorial tests (and all other e2e tests sharing this fixture).

Fixed the e2e tutorial test fixture and expanded `mngr exec --cwd` coverage.

- The e2e fixture wrote a duplicate `type = "claude"` key into the generated
  `settings.local.toml`, which the TOML parser rejected ("Cannot overwrite a
  value"). Every tutorial test that creates an agent (`mngr create`) failed at
  the create step before reaching its actual assertions. Removed the duplicate
  key so the fixture-generated config parses.
- Added `test_exec_cwd_nonexistent`, an unhappy-path test sharing the
  `mngr exec --cwd` tutorial block: it verifies that pointing `--cwd` at a
  directory that does not exist on the agent host causes exec to exit nonzero
  rather than silently running in the default work_dir.

Fixed the e2e tutorial fixture so `mngr create` works again: the generated
`settings.local.toml` had a duplicate `type = "claude"` key under
`[commands.create]`, which made the TOML parser reject the config ("Cannot
overwrite a value"). Removed the duplicate.

Gave `test_exec_git_log` a `@pytest.mark.timeout(60)` override (matching the
identically-shaped `test_exec_branch_show_current`), since `mngr create` plus
`mngr exec` together exceed the 10s default pytest timeout.

Fixed the e2e tutorial test fixture that generated an invalid `settings.local.toml` with a duplicate `type = "claude"` key under `[commands.create]`, which made every e2e tutorial test fail to create agents. Also added a 120s timeout to `test_exec_git_push_then_merge`, which exercises a slow `mngr exec`, so it no longer trips the default 10s pytest timeout.

Fixed the e2e tutorial test fixture, which wrote a duplicate `type = "claude"`
key into the per-test `settings.local.toml`, producing invalid TOML that made
`mngr create` (and therefore every e2e tutorial test) fail to load its config.
Also gave `test_exec_short_form` an explicit `@pytest.mark.timeout(120)` so the
create+exec cycle is not killed by the repo-wide 10s default timeout.

Fixed a bug in the e2e tutorial test fixture (`conftest.py`) that wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`. The config parser rejected the duplicate key ("Cannot overwrite a value"), causing every e2e tutorial test's `mngr create` step to fail. Removed the duplicate key.

Fixed the e2e tutorial test fixture that wrote a malformed `settings.local.toml` containing a duplicate `type = "claude"` key under `[commands.create]`, which caused a TOML parse error ("Cannot overwrite a value") and made `mngr create` fail in every tutorial e2e test.

Fixed the e2e `test_full_lifecycle` release test (and the shared e2e fixture it depends on):

- Removed a duplicate `type = "claude"` key under `[commands.create]` in the e2e conftest's
  generated `settings.local.toml`. The duplicate made tomlkit refuse to parse the file
  ("Cannot overwrite a value"), causing every `mngr` command in e2e tests to fail at config
  load.
- Added `@pytest.mark.timeout(300)` to `test_full_lifecycle`, matching the convention used by
  the other multi-command e2e tests. Without it the test inherited the global 10s `func_only`
  timeout and was killed partway through its sequence of `mngr` commands.

Fixed the e2e test fixture's generated `settings.local.toml`, which defined the `[commands.create]` `type` key twice. TOML forbids redefining a key in the same table, so every e2e tutorial test failed at startup with "Cannot overwrite a value" before any command ran. Removed the duplicate key.

Fixed the e2e test fixture so it no longer writes a duplicate `type = "claude"` key into the generated `settings.local.toml`. The duplicate caused a TOML parse error ("Cannot overwrite a value") that broke `test_invalid_provider_fails` (and any other e2e test that loaded the merged config), masking the real behavior under test.

Fixed the e2e test fixture so it no longer emits an invalid `settings.local.toml`: the
`[commands.create]` table set `type = "claude"` twice, which is a duplicate-key TOML error
("Cannot overwrite a value") that caused every command loading the merged config (e.g.
`mngr list`) to fail. Removed the duplicate key.

Added a happy-path companion to the `mngr list --fields "name,state,initial_branch"` tutorial
test (`test_list_fields_original_branch_with_agent`) that creates an agent and asserts the
`initial_branch` column actually displays the branch mngr created for it (`mngr/my-task`),
complementing the existing empty-list ("No agents found") coverage.

Test-only changes (no user-visible behavior change).

- Fixed the e2e test fixture: the generated `settings.local.toml` defined `type = "claude"` twice
  under `[commands.create]`, which is an invalid duplicate TOML key and caused every `mngr` command
  in the e2e suite to fail with a config parse error. Removed the duplicate.
- Strengthened `test_list_filter_by_state` to also assert that the `--stopped` flag returns exactly
  the same set of agents as its documented CEL alias `--include 'state == "STOPPED"'`.

Fixed a bug in the shared e2e test fixture (`e2e/conftest.py`) where the generated
`settings.local.toml` contained a duplicate `type = "claude"` key under
`[commands.create]`. The duplicate key made the file invalid TOML, so every `mngr`
command run by an e2e test failed during config parsing with
`Cannot overwrite a value`. Removing the redundant line restores a valid config and
unblocks the docker tutorial e2e tests (and all other e2e tests sharing this fixture).

Fixed the e2e test fixture (`conftest.py`) that wrote an invalid `settings.local.toml` with a duplicate `type = "claude"` key under `[commands.create]`, which made every e2e `mngr create` fail with a TOML "Cannot overwrite a value" parse error.

Strengthened `test_multiple_agents_coexist` to verify that coexisting agents each occupy a distinct working directory (their own worktree), rather than only checking that an `echo` command runs on each.

Fixed the e2e test fixture so it no longer writes a duplicate `type = "claude"` key into the generated `settings.local.toml`, which had been causing every config load (and thus commands like `mngr plugin disable`) to fail with a TOML "Cannot overwrite a value" parse error. Also added a 60s `@pytest.mark.timeout` to the plugin e2e tests, which run several real `mngr` subprocess invocations and exceed the default 10s per-test timeout.

Fixed the e2e test fixture and the plugin e2e tests so the plugin disable/enable roundtrip
release test runs correctly:

- Removed a duplicate `type = "claude"` key from the `[commands.create]` table that the e2e
  fixture writes into `settings.local.toml`. The duplicate produced invalid TOML, so every
  command in an affected e2e test failed up front with "Cannot overwrite a value".
- Added `@pytest.mark.timeout(300)` to the two plugin e2e tests, matching the convention used
  by other multi-command e2e release tests. Their several real CLI subprocess invocations
  exceed the global 10s `func_only` timeout.

Fixed the e2e test fixture's generated `settings.local.toml`, which contained a
duplicate `type = "claude"` key under `[commands.create]` (a squash-merge
artifact) that produced invalid TOML and broke every e2e test with a config
parse error. Also strengthened `test_plugin_list_active_to_see_types` to verify,
via JSON output, that the `claude`, `codex`, and `command` agent types appear as
their own enabled plugin entries rather than relying on loose substring matches.

Fixed the e2e tutorial test fixture (`e2e` in `conftest.py`) which wrote a malformed `settings.local.toml` containing a duplicate `type = "claude"` key under `[commands.create]`. The config loader now rejects duplicate TOML keys, so this broke every e2e tutorial test with "Cannot overwrite a value". Removed the accidental duplicate line.

Also strengthened `test_recipe_launch_check_cleanup` to verify that destroy removes the agent itself (gone from `mngr list`, no longer resolvable by `mngr exec`), not just its branch.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote an invalid `settings.local.toml`: a merge artifact had duplicated the `type = "claude"` key inside the `[commands.create]` table, causing every `mngr` command in e2e tests to fail with `Failed to parse config file ...: Cannot overwrite a value`. Removed the duplicate key.

Fixed the e2e test fixture that wrote a duplicate `type = "claude"` key into the
generated `settings.local.toml`, which produced an invalid-TOML parse error
("Cannot overwrite a value") and broke every e2e command (e.g. `mngr create`).

Strengthened `test_rename_dry_run_does_not_rename` to also verify (via `mngr
exec`) that the agent remains reachable and running its command under its
original name after a dry-run, not just that the name is unchanged in
`mngr list`.

Test-only changes (no user-visible behavior change):

- Fixed the e2e `e2e` fixture (`e2e/conftest.py`), which wrote a `[commands.create]`
  block with a duplicate `type = "claude"` key into `settings.local.toml`. TOML rejects
  duplicate keys, so every e2e test using the fixture failed at the first `mngr` command
  with "Cannot overwrite a value". Removed the duplicate line.
- Strengthened `test_tips_exec_env_inspect`: it now cross-checks that the
  `MNGR_AGENT_ID` exported into the exec'd environment matches the id mngr records for
  the agent, and verifies the `env | sort` output is actually sorted.

Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote a duplicate
`type = "claude"` key under `[commands.create]` in the generated `settings.local.toml`. The
duplicate key made the file invalid TOML, so every `mngr` command run through the e2e fixture
failed with "Cannot overwrite a value". Removing the duplicate restores the e2e tutorial tests
(including `test_tips_transcript_tail_assistant`).

Fixed the e2e tutorial test fixture and strengthened the unknown-command test.

- The e2e `settings.local.toml` written by the tutorial-test fixture contained a duplicate
  `type = "claude"` key under `[commands.create]` (a merge artifact). This is invalid TOML and
  made every e2e tutorial command abort with a config parse error ("Cannot overwrite a value")
  instead of running. Removed the duplicate so the fixture config parses again.
- `test_unknown_command_fails` now asserts the precise Click usage exit code (2), that stdout is
  empty (clean for scripting), and that the error names the offending command, in addition to
  pointing the user back at `mngr --help`.

## 2026-06-08

# Install the agent-coordination Claude Code skills

`mngr extras claude-plugin` now installs more than just the code review
plugin. It offers two Claude Code plugins and lets you install either or
both:

- `imbue-code-guardian` -- automated code review enforcement (unchanged).
- `imbue-mngr-skills` -- the `message-agent`, `wait-for-agent`, `find-agent`,
  and `mngr-help` skills for working with mngr, published from the dedicated
  `imbue-ai/mngr-claude-skills` repo.

With an interactive terminal the step shows a checkbox picker of the
not-yet-installed plugins (all preselected; Space toggles, Enter confirms),
matching the multi-select UI of the `mngr extras plugins` wizard;
`mngr extras claude-plugin -y` auto-installs every plugin that is not already
present. `mngr extras` status output reports each plugin's
installed/not-installed state individually.

Regenerated the `mngr imbue_cloud admin pool create` CLI reference docs to
include the new `--no-recycle` flag (forces a fresh OVH VPS order instead of
reclaiming a cancelled one). Docs-only change to `libs/mngr/docs/`.

Test-only flake mitigations (no production code change):

- Made the Docker `test_pull_image_not_found_raises` integration test resilient to a Docker Hub registry-connectivity flake: when the registry is unreachable (the pull times out before returning its 404), the test now skips instead of failing, while still asserting the clean "image not found" path when the registry is reachable. Also marked it `@pytest.mark.flaky` so offload retries it.
- Marked the tmux integration test `test_start_restart_stopped_agent` `@pytest.mark.flaky` (it occasionally exceeds the 10s pytest-timeout by a few hundred ms under CI load), matching its already-flaky siblings (`test_list`, `test_create`, `test_connect`, `test_destroy`) so offload retries it rather than hard-failing.

- Marked two real-agent integration tests (`test_stop_agent_kills_multi_pane_processes`,
  `test_cleanup_destroy_json_output_with_real_agent`) as `@pytest.mark.flaky` so
  offload retries them. They intermittently exceed the 10s `pytest-timeout` under
  offload load while waiting on a spawned agent process; this matches the
  already-flaky sibling `test_start_restart_stopped_agent`. No production change.

Fixed a batch of breakage in the test suite introduced by a bulk merge:

- Added missing imports and a missing fixture parameter in the e2e tutorial tests (`json`, `re`, `Path`, `Any`, `sys`, and the `temp_git_repo` fixture) that caused F821 errors.
- Fixed a stale `_create_my_task` call signature, removed an unused `json` import, deduplicated stray imports, and deleted a dead duplicate transcript-staging helper in `test_transcript.py`.
- Applied ruff import-sorting (`destroy_test.py`, `start_test.py`) and formatting (14 e2e test files) that the merge left unformatted.
- Regenerated the CLI markdown docs to reflect new options the merge added (`mngr connect --connect-command`, `mngr destroy --dry-run`, and updates to `start`/`stop`/`message`/`snapshot`).
- Rephrased a comment in `test_templates.py` that coincidentally tripped the `exec()` ratchet (the prose read "exec (which ...").
- Replaced `@pytest.mark.flaky` with `@pytest.mark.timeout(30)` on `test_list_command_with_sort_by_name`, the one sibling in the `test_list_command_with_*` family missed by the earlier timeout-flake audit (its teardown latency trips the 10s default under CI load; reruns do not help latency).

No user-facing behavior change.

Added a `docker_runtime` option to the docker provider config. When set (e.g. `docker_runtime = "runsc"`), mngr passes `--runtime=<value>` to `docker run`, letting hosts run agent containers under an alternative runtime such as gVisor. Defaults to unset (no `--runtime` flag, i.e. Docker's default runtime). The named runtime must be registered with the Docker daemon, otherwise container creation fails with Docker's native "unknown runtime" error. Override per-environment with `MNGR__PROVIDERS__<NAME>__DOCKER_RUNTIME` (e.g. `=runc` where gVisor is unavailable, such as CI).

Fixed a shell-quoting bug in agent launch: extra `agent_args` (the arguments passed after `--` on `mngr create`) are now shell-quoted before being spliced into the launch command.

Previously, `BaseAgent.assemble_command` joined `agent_args` into the command string with plain spaces. An argument whose value contained spaces or shell metacharacters -- for example `--model "Gemini 3.5 Flash (Medium)"` -- was emitted raw (`--model Gemini 3.5 Flash (Medium)`), so the shell that evaluates the launch command word-split the value and parsed `(Medium)` as a subshell, failing with `syntax error near unexpected token '('`.

Each `agent_args` element is now passed through `shlex.quote` (via a shared `quote_agent_args` helper, which the `mngr_claude` plugin's own `assemble_command` override also uses so the two cannot drift). `cli_args` are left unquoted, unchanged: string-form `cli_args` configs are split with a quote-preserving (non-POSIX) shlex and so already arrive shell-safe. This fixes every agent type that inherits the base method, including `antigravity`/`agy`.

Added shell tab-completion for the positional arguments of `mngr plugin add` and `mngr plugin remove`.

- `mngr plugin add <TAB>` suggests installable plugin package names (e.g. `imbue-mngr-claude`, `imbue-mngr-modal`) drawn from the plugin catalog -- the same set the `mngr extras` install wizard offers.
- `mngr plugin remove <TAB>` suggests the plugin packages currently installed (from the uv-tool receipt), filtered to packages that actually register `mngr` entry points -- so non-plugin dependencies installed alongside plugins (e.g. workspace libraries) are not offered.

Both support prefix filtering and repeat the completion for each package when operating on several at once.

Test-infrastructure cleanup: the shared mngr plugin test fixtures (HOME
isolation via the autouse `setup_test_mngr_env`, temp host/profile/config dirs,
git-repo helpers, and the shell-stub fixtures `stub_mngr_log_sh` /
`mngr_transcript_lib_sh`) are now single-sourced in
`imbue.mngr.utils.plugin_testing` and exposed through
`register_plugin_test_fixtures`. mngr's own `conftest.py` now registers that
shared set rather than redefining ~20 duplicate fixtures, keeping only two that
still differ for mngr-core: the deliberately-blocking `plugin_manager`, and
`mngr_test_id` (which differs only incidentally -- its `worker_test_ids`
bookkeeping is read solely by mngr-core's `session_cleanup` leak scan, which is
not shared with plugins). The shared `temp_mngr_ctx` now resets the
provider-instance cache on teardown for plugins too, so that behavior no longer
diverges. No user-facing behavior change.

- `plugin_catalog.py`: `UNPUBLISHED_PACKAGES` is now the single source of truth for "deliberately not published to PyPI", consulted by both the install wizard (which never offers an unpublished package) and the release tooling (which auto-discovers every `libs/*` package as a publish candidate and subtracts this set). Added `imbue-mngr-mapreduce` (internal map-reduce framework library, no CLI), `imbue-mngr-claude-subagent-proxy` (experimental, coupled to Claude Code internals), and `skitwright` (e2e-test-only helper) alongside the existing `imbue-mngr-tmr`. No runtime behavior change.

Fixed `ProviderInstanceConfig.merge_with` so a higher-precedence config layer
only overrides the provider fields it actually set. It previously used an
"override wins unless its value is None" rule, which meant any field whose
default is a non-None value (a `bool` defaulting to `False`, an empty tuple,
etc.) was silently reset to that default whenever a higher layer touched the
provider block at all -- even via a single-key override like a create
template's `setting__extend = ["providers.<name>.is_enabled=true"]`.

Concretely, applying `providers.lima.is_enabled=true` (as the minds
forever-claude-template's lima create template does) reset `is_host_in_docker`,
`install_gvisor_runtime`, and `default_container_run_args` back to their
defaults, so the Lima provider silently ran in direct-in-VM mode instead of
docker-in-VM mode. The merge now uses `model_fields_set` (matching
`AgentTypeConfig` / `PluginConfig`), so untouched fields keep their base value.

`mngr create --new-host` now tears down a freshly-created host on *any* failure
up to and including the initial-message send, so a failed create never leaks a
host (or, for non-idle-shutdown providers, its lease). The whole create flow --
host env-var write, on_host_created hooks, post-host-create commands, locking,
provisioning, agent start, and the initial-message delivery -- is now wrapped in
a single continuous teardown guard, closing a gap where failures between the
former two separate guard blocks (and the host env-var write) could leak the
host. The `--edit-message` send, which the CLI performs after the API create
returns, is now likewise covered. The existing
`MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE=1` escape hatch still
retains a failed host for debugging.

- Fix the "collect results from all agents" example in the tutorial. It used `mngr exec "$agent" -- git log --oneline -3`, but `mngr exec` takes the command as a single positional argument (not a kubectl-style `--`-separated argv), so the `--oneline`/`-3` tokens were mis-parsed as agent names and the command always failed. The example now passes the command as a single quoted string: `mngr exec "$agent" "git log --oneline -3"`.

- Fix the `test_advanced_fan_out_create` e2e tutorial test so it runs. The
  fan-out loop creates four local command agents, which invokes `rsync` and
  `tmux` and takes longer than the default per-test timeout, so the test now
  carries `@pytest.mark.rsync`, `@pytest.mark.tmux`, and an extended
  `@pytest.mark.timeout(180)` (plus a larger per-command timeout for the shared
  loop). The superfluous `@pytest.mark.modal` mark was removed because the test
  substitutes local agents and never exercises Modal.
- Strengthen the same test's assertions: instead of only checking the fan-out
  loop exits 0, it now verifies that one agent was created per task (via
  `mngr list --provider local --addrs`) and that the task commands are actually
  running (via `mngr exec ... pgrep`).

Removed the superfluous `@pytest.mark.modal` from the `mngr observe --discovery-only` e2e tutorial test (it never shells out to the `modal` CLI, so the mark was a resource-guard `NEVER_INVOKED` violation) and strengthened it to create a local agent, capture the JSONL discovery stream, and assert the stream is well-formed JSON that reports the agent.

Fixed the `test_advanced_watch_dashboard_running` e2e tutorial test: removed the incorrect `@pytest.mark.modal` mark (the `watch -n 5 mngr list --running` dashboard command only enumerates the local provider when no remote hosts are registered, so it never contacts Modal) and added a verification that the underlying `mngr list --running` query succeeds and returns a well-formed, empty dashboard.

Fixed the `test_advanced_watch_list_live_dashboard` e2e tutorial test. It
carried a spurious `@pytest.mark.modal` mark, but the `watch -n 5 mngr list`
dashboard command it exercises only performs Modal host discovery via the
in-subprocess gRPC SDK, which the resource guard cannot observe (the guard only
tracks the `modal` CLI binary or in-process gRPC). The mark therefore tripped
the guard's "marked modal but never invoked modal" check. Removed the mark and
strengthened the test to create a local agent and assert it appears in the live
dashboard output, rather than only checking that the `watch` wrapper exits.

Strengthened the `mngr archive` e2e tutorial test (`test_archive_command`) to verify
the agent is actually archived: it now stops the agent first (mirroring the tutorial
narrative), runs `mngr archive my-task`, and asserts the `archived_at` label is set and
the agent appears under `mngr list --archived`. Added an unhappy-path test
(`test_archive_running_agent_is_skipped`) verifying that archiving a running agent
without `--force` is a no-op that warns and leaves the agent un-archived.

Fixed the `test_archive_stopped_via_stdin` e2e tutorial test: removed the
incorrect `@pytest.mark.modal` marker (the test only exercises local lifecycle
commands and never invokes the Modal CLI, so the resource guard failed the
otherwise-passing test) and strengthened it to verify the archive's actual
effect (the `archived_at` label is applied, the agent appears under
`mngr list --archived`, and is filtered out of `mngr list --active`).

Fixed the `test_command_agent_dev_server_extra_windows` e2e release test, which
was marked `@pytest.mark.modal` but never ran any command that contacts Modal, so
the resource guard failed it. It now runs `mngr list` (which exercises the Modal
discovery path, matching the sibling create tests) to verify the command agent was
created, and asserts that the extra `logs` tmux window requested via `-w` actually
exists.

Fixed and strengthened the `test_command_agent_python_http` e2e tutorial test
for the "RUNNING NON-AGENT PROCESSES" section. The test created a local
`--type command` agent but was incorrectly marked `@pytest.mark.modal`, so the
resource guard failed it for never invoking Modal; the mark was removed. The
test now also verifies actual behavior: it checks that the managed process is
running inside the agent (`mngr exec ... ps`) and that the agent is listed as a
local command agent with the expected command (`mngr list --provider local
--format json`). No production code changes.

- Strengthen the `mngr config edit --scope project` e2e tutorial test (`test_config_edit_scope`) to verify the actual behavior: it now resolves the project-scoped config path via `mngr config path --scope project`, confirms the edit command announces and targets that exact file, and checks that the file is created from the template on disk. Added a companion unhappy-path test (`test_config_edit_scope_missing_editor`) verifying the command fails cleanly with an "Editor not found" message when `$EDITOR`/`$VISUAL` point at a nonexistent program.

Strengthened the `mngr config edit` e2e tutorial test: it now drives the command
with a fake editor and verifies the editor is invoked on the freshly-created
config file (rather than only checking the exit code). Added a companion test
covering the unhappy path where the editor exits non-zero and the command
propagates the failure.

- Strengthen the `mngr config get` tutorial release test: it now establishes a value with `mngr config set` and reads it back, asserting the returned value is exactly what was set (instead of only checking the exit code). Added a companion test covering the unhappy path where `mngr config get` is asked for a key that has not been set, verifying it exits non-zero with a clear "Key not found" message.

- Fix the `test_config_list_scope` release e2e test, which ran three sequential `mngr config list --scope ...` subprocesses and exceeded the default 10s per-test pytest-timeout (each `mngr` cold-start costs several seconds). Added a `@pytest.mark.timeout(60)` marker, matching how other multi-command e2e tutorial tests handle cumulative subprocess startup time.
- Strengthen the same test to assert each invocation reports the scope it actually read from (`Config from user/project/local`), so a pass confirms `--scope` selects the right config file rather than silently falling back to the merged view.

- Strengthen the `mngr config list` e2e tutorial test: it now persists a known top-level value (`headless`) and asserts that the human-readable output carries both the "Merged configuration (all scopes):" banner and the persisted `headless = true` line, rather than only checking the exit code. Added a companion `test_config_list_json` covering the same tutorial block via `--format json`, asserting the parsed document carries the value under the top-level `config` object.

Strengthened the `mngr config path --scope user` e2e tutorial test to verify the
reported path is actually the user-scope config file (by writing a value at user
scope and confirming it lands in that exact file), and added an unhappy-path test
that asserts an unsupported `--scope` value is rejected.

Strengthened the `mngr config path` e2e tutorial test (`test_config_path`) to verify actual behavior: it now asserts that all three scopes (user, project, local) are reported, that the user/local scopes resolve to `settings.toml` / `settings.local.toml`, and that the `(exists)`/`(not found)` annotation for each scope matches the real on-disk state.

Fixed the e2e tutorial test harness so that `mngr config set` against the
project scope works under pytest. The shared e2e fixture now seeds the
project `settings.toml` with the `is_allowed_in_pytest` opt-in (previously
only `settings.local.toml` opted in, so a project-scope `config set` created
a file that the pytest config guard rejected on the next load), and opts the
harness into the assign-by-default merge semantics
(`allow_settings_key_assignment_narrowing = true`) so a project-scope
`commands.create.*` setting no longer collides with the local-scope
`connect_command` in the command's shared `defaults` map. This unblocks the
`test_config_set_default_provider` and `test_config_set_headless` tutorial
tests.

Strengthened the `mngr config set headless true` tutorial e2e test
(`test_config_set_headless_globally`) to verify the value actually persists to
the project-scope config file (read back directly as a boolean), rather than
only checking the command exits 0. Added a companion unhappy-path test
(`test_config_set_rejects_unknown_key`) that confirms `mngr config set` rejects
an unknown key with a non-zero exit and does not create the config file. No
production behavior change.

Fixed the `test_config_set_headless` e2e release test so it reliably passes: added a `@pytest.mark.timeout(60)` marker (two sequential `mngr` invocations exceeded the default 10s per-test timeout) and seeded the project `settings.toml` with `is_allowed_in_pytest = true` before `mngr config set headless true` writes to it, so the follow-up `mngr config get headless` no longer trips the pytest config opt-in guard.

Strengthened the `mngr config set --scope` e2e tutorial test (`test_config_set_scope`) to verify the actual effect of the command: it now reads the value back from the user scope and confirms the value did not leak into the project scope. Added a companion unhappy-path test (`test_config_set_invalid_scope`) that confirms an unrecognized `--scope` value is rejected and writes nothing.

- Harden the `mngr config set` tutorial e2e test: give it an explicit 60s timeout so a cold-start `mngr` subprocess does not blow the default 10s pytest budget, assert that the value actually lands in the project `settings.toml` (rather than only checking the exit code), and add an unhappy-path test verifying that setting an unknown configuration key is rejected and not persisted.

Removed a superfluous `@pytest.mark.modal` mark from the
`test_connect_by_agent_id_fictional` e2e test. The test connects to a
non-existent agent id and only verifies that mngr parses the id-as-target
syntax and reports a clean "not found" error; it never shells out to the
`modal` CLI (the only Modal chokepoint tracked inside the mngr subprocess),
so the resource guard correctly flagged the mark as never invoked. The test
still runs in the release suite (it is `@pytest.mark.release`), which executes
on Modal infrastructure regardless of the mark. Also strengthened the test's
assertion to confirm the command exits non-zero and the error explicitly names
the missing agent id. No production behavior change.

Fixed the `mngr connect` e2e tutorial tests (`test_connect.py`). The happy-path
tests asserted `mngr connect my-task` succeeds, but the standalone `connect`
command execs `tmux attach` (the `connect_command` override only applies to
`create`/`start`), which aborts with "open terminal failed: not a terminal"
under the plain pipe-based test runner. Added an `E2eSession.run_connect_interactively`
helper that runs the command under a PTY, waits for the tmux client to attach,
and detaches it externally so the command exits cleanly, and switched the
happy-path tests to it (now also asserting the command attaches to the named
agent's session). Removed the superfluous `@pytest.mark.modal` from the
local-only connect tests, which never query modal (the resource guard enforces
this); the id/host error-path tests keep it.

Fixed the `test_connect_explicit_host` e2e tutorial test, which was failing
because it carried `@pytest.mark.tmux`, `@pytest.mark.rsync`, and
`@pytest.mark.modal` resource-guard marks that the command never exercises.
`mngr conn my-task@my-host` fails during host resolution (the host does not
exist) and exits before attaching a tmux session, running rsync, or making any
real Modal call, so the resource guard rejected the superfluous marks. The test
now carries only `@pytest.mark.release`, matching the other unhappy-path connect
test. Also strengthened the assertion to verify the error clearly reports the
missing host rather than only checking the exit code.

Fixed the `test_connect_no_start` e2e tutorial test. It now allows enough time for
the create+connect flow (adds `@pytest.mark.timeout(180)`), drops the superfluous
`@pytest.mark.modal` mark (the local create/connect path never invokes Modal), and
asserts the real behavior of `mngr connect my-task --no-start`: a running agent is
accepted (no auto-start-disabled error) and connect proceeds to a real `tmux attach`,
which fails cleanly with "open terminal failed" in the headless harness. Also
corrected the module docstring, which incorrectly claimed the e2e fixture rewrites
the standalone `connect` command to a no-op recorder.

Fixed the `test_connect_short_form` e2e tutorial test for `mngr conn`.

`mngr connect`/`conn` replaces itself with `tmux attach`, which blocks until the
user detaches and requires a real terminal, so the test could never succeed when
run headlessly (it either hung on a tty or failed with "open terminal failed:
not a terminal"). The e2e session now exposes `run_connecting_command`, which
launches the connect command under a pseudo-terminal in a background subprocess
and verifies that a client actually attaches to the agent's tmux session (the
observable effect of a successful connect). The `@pytest.mark.modal` mark was
also removed because connecting to a local agent never invokes Modal.

`mngr connect` now honors a custom `connect_command` (via the new
`--connect-command` flag or the `connect_command` config), running it instead
of the builtin tmux attach -- matching how `mngr create` and `mngr start`
already behave. A re-entrancy guard (the `MNGR_CONNECT_COMMAND_ACTIVE`
environment variable) prevents infinite recursion when a custom connect command
itself invokes `mngr connect`.

Fixed and expanded the e2e release test for controlling mngr via `MNGR__*`
environment variables (`test_control_mngr_via_env`). The test now opts into
assign-by-default behavior so it works around the e2e fixture's
`[commands.create]` local setting, asserts the agent actually lands on the
`local` provider chosen via the env var, and adds an unhappy-path test verifying
that an invalid provider value supplied via the env var is rejected. No
user-facing behavior change.

- Fix the `test_create_address_syntax_existing_host` tutorial e2e test (`mngr create my-task@my-dev-box`). It now passes an explicit `--type` so the command reaches host resolution (the agent-type default is not configured in the isolated test environment, so without it the command failed earlier on "No agent type provided"), and it verifies the expected "Could not find host" error. Removed the inaccurate `@pytest.mark.modal` mark because resolving a non-existent named host short-circuits before any Modal API call. Also strengthened the test to assert the parsed host name (`my-dev-box`) is echoed in the error.

- Remove the superfluous `@pytest.mark.modal` from the e2e tutorial test `test_create_and_destroy_agent`. The test creates a `--type command` (local) agent and its body never invokes the Modal CLI, so the resource guard correctly flagged the mark as never-invoked (the only Modal touch is the fixture's teardown environment cleanup, which runs after the call-phase guard check). The test still carries the `rsync` and `tmux` marks it genuinely exercises.

- Test-only change (no user-visible behavior change): removed a stale `@pytest.mark.modal` from the `mngr rename` e2e release test, which created only a local command agent and never actually invoked Modal (the mark began failing once the resource-guard's must-use enforcement was re-enabled). Also added an e2e release test covering `mngr rename --dry-run`, asserting it previews the rename without mutating the agent's name.

Updated the "clone" creation tutorial block to use the current `mngr create --transfer=git-mirror` flag instead of the removed `--clone` flag, which created a full git clone (an independent copy of the repo with its own working directory and history) rather than a worktree. The corresponding e2e release test (`test_create_clone`) now runs the updated command and verifies the agent runs in a separate working directory that is a real git clone (its own `.git` directory rather than a worktree's `.git` file).

- Fix the `test_create_codex_agent` e2e tutorial test so its scaffolding `mngr config set agent_types.codex.command 'sleep 99999'` writes to the `local` config scope (`settings.local.toml`) instead of the default `project` scope. The e2e fixture only opts `settings.local.toml` into the pytest run (`is_allowed_in_pytest = true`); writing a fresh `project` `settings.toml` produced a config file without that flag, so the subsequent `mngr create` refused to load it under the pytest config guard. Also strengthen the test to assert the created codex agent's resolved command matches the configured `sleep 99999`.

Fixed the `test_create_codex_explicit_type` release e2e test for the codex agent-type tutorial block: the test now configures the codex command in the local config scope (whose `settings.local.toml` opts into the pytest config guard) instead of the project scope, and dropped the superfluous `@pytest.mark.modal` mark since a local `--no-connect` create never invokes Modal. The test also now verifies via `mngr list --format json` that the created agent has type `codex` and is running, rather than only checking the exit code.

Removed the inapplicable `@pytest.mark.modal` mark from the release e2e test
`test_create_command_agent_runs_post_dash_command_in_agent`. The test creates a
`command`-type agent on the default (local) provider and never invokes Modal in
any way the resource guard can observe, so the mark caused a spurious
NEVER_INVOKED guard failure. The test still exercises the documented `mngr create
--type command -- <command>` behavior and verifies the command actually runs in
the agent.

- Tighten the `test_create_command_custom_script` e2e tutorial test. Dropped the superfluous `@pytest.mark.modal` mark (the local command-agent it creates never exercises Modal, which the resource guard now flags as a `NEVER_INVOKED` violation), and strengthened it to verify that the custom command is actually forwarded to a running `command`-type agent (via `mngr list --format json`) rather than only asserting that `mngr create` exits 0.

Fixed the `test_create_command_python_http` e2e tutorial test: it was marked
`@pytest.mark.modal` but only creates a local `command`-type agent, so it never
invoked Modal and failed the resource guard. Removed the spurious mark and added
verification that the agent is actually created with the expected command and is
running inside the agent.

- Fixed the BASIC CREATION tutorial: the "create a copy instead of a worktree" example used the removed `--copy` flag. It now uses `mngr create my-task --transfer=git-mirror`, matching the current `--transfer` interface (a plain rsync copy is still the default for non-git projects).

- Add an e2e release test (`test_create_default_branch_distinct_per_agent`) covering the tutorial's claim that `mngr create` creates a *separate* default branch *per agent*: it creates two agents and verifies each lands on its own distinct `mngr/<name>` branch (worktree HEAD checked via `mngr exec`), with both branches starting from the same base commit.

Removed the superfluous `@pytest.mark.modal` from the `test_create_default` e2e
tutorial test (it creates a local-provider agent and never invokes Modal, so the
resource guard flagged the mark as never-invoked). Strengthened the test to
verify the agent is actually running inside its worktree by execing `pwd` in the
agent and comparing it against the reported `work_dir`.

Fixed the tutorial's Docker resource-limit example: `mngr create my-task
--provider docker -s cpus=2` used a bare `cpus=2` token, which `docker run`
would have interpreted as the image name rather than a CPU limit. The example
now uses the correct `--cpus=2` flag. Also strengthened the corresponding e2e
release test to specify an agent type and to verify the CPU limit is actually
applied inside the created container.

Fixed the `test_create_docker_custom_dockerfile` e2e release test, which had been
failing since the `mngr create` agent-type default was moved into user config
(an explicit `--type` is now required). The test now passes `--type command` and
builds the custom image from `debian:bookworm-slim` (which provides `apt-get` for
the required host packages) instead of `alpine`, and verifies the custom Dockerfile
was actually used by reading back a marker file baked into the image.

- Fixed the `test_create_docker_default_image` e2e tutorial test: the isolated test profile has no configured default agent type, so the bare `mngr create my-task --provider docker` it ran failed with "No agent type provided". The test now passes `--type command -- sleep <N>` explicitly (matching the basic-creation tutorial tests) and additionally verifies the container is running mngr's default image (`debian:bookworm-slim`) by reading `/etc/os-release` via `mngr exec`.

- Fix the `test_create_docker_start_args_overview` e2e release test so the `mngr create --provider docker` command supplies an agent type (`--type command`), which the isolated test environment does not configure as a default. The test now also reads the container hostname back to confirm the `-s` start arg is forwarded to `docker run`, rather than only checking the create command's exit code.

Fixed the `test_create_docker_start_args` e2e tutorial test: the `mngr create`
invocation now passes `--type command -- sleep ...` so that the isolated test
environment (which has no default agent type configured) can create the agent
and keep the container alive for the follow-up `mngr exec my-task hostname`
assertion that verifies the `-s "--hostname=..."` start arg was forwarded to
`docker run`.

Fixed the `test_create_docker_volume_start_arg` e2e tutorial test, which was
failing because `mngr create` requires an agent type and the test supplied
none. The test now uses the standard `--type command -- sleep <N>` stand-in
(matching the other tutorial create tests) and verifies that the `-v` start arg
actually bind-mounts the host directory into the container.

Strengthened the e2e test for duplicate agent names (`mngr create` with an
already-used name). It now asserts the failure is specifically the
duplicate-name rejection ("already exists") and verifies the rejected duplicate
leaves the original agent untouched -- exactly one agent of that name remains,
still running its original command rather than the duplicate's.

- Remove the superfluous `@pytest.mark.modal` from the `test_create_from_another_agent` e2e tutorial test. The test only creates local command agents and clones one from another via rsync; it never invokes Modal through the bare `modal` CLI (the only path the resource guard can observe -- exercised solely during Modal *host* creation). `mngr list` enumerates the Modal provider through the in-process-untrackable SDK, so the mark could never be satisfied and the resource guard failed the test with "marked with @pytest.mark.modal but never invoked modal". The mark has no effect on CI test selection (release offload jobs filter by `release`, not `modal`), so dropping it does not change where the test runs.
- Strengthen `test_create_from_another_agent` to verify the core behavior of `--from <agent>`: it now writes a marker file into the source agent's work directory before cloning and asserts the cloned agent received it, confirming the source agent's directory contents are actually copied (previously the test only checked that the two agents had distinct work dirs on the same host and that the clone got its own branch).

- Fix the release e2e test `test_create_git_mirror_with_existing_branch` so it passes again. The test only ever creates a local-provider agent (via `--transfer=git-mirror --branch <current>`), so it never invokes Modal, but it was marked `@pytest.mark.modal`. Since provider discovery now skips the Modal backend when no Modal environment exists (`ProviderEmptyError`), `mngr list` makes no Modal call and the resource guard failed the test with "marked with @pytest.mark.modal but never invoked modal". Removed the stale mark. Also strengthened the test to assert that omitting the `:NEW` part creates no new `mngr/*` branch in either the source repo or the agent's git mirror. No production behavior change.

- Remove the superfluous `@pytest.mark.modal` from the `test_create_headless` e2e release test. The test creates a local command agent and only runs `mngr list`, which never invokes the Modal CLI (the resource guard only tracks Modal CLI invocations in subprocesses, and `mngr list` discovery does not create a Modal environment), so the resource guard correctly flagged the mark as never-invoked.
- Strengthen `test_create_headless` to verify the headless agent is actually running by exec-ing into its host, instead of only checking that it appears in `mngr list` output.

Fixed the `test_create_headless` tutorial e2e test (`mngr create --headless`): added a per-test timeout matching the other agent-creating e2e tests so the real create plus `mngr list` no longer trips the global 10s default, and removed the superfluous `@pytest.mark.modal` mark since the test creates a local agent and never invokes Modal. Also strengthened the test to verify the headless agent is actually running and reachable via `mngr exec`.

Strengthened the `mngr create --help` e2e tests: the help command now also asserts that stderr is empty (no stray warnings or deprecation notices), and a new test verifies the abbreviated forms advertised in the help's own SYNOPSIS -- the `-h` short flag and the `c` alias -- both produce the create help output.

Added an unhappy-path e2e test (`test_create_rejects_unknown_option`) covering the same `mngr create` tutorial block: it verifies that an unknown option is rejected with exit code 2, an empty stdout, and a usage error on stderr.

Fixed the `test_create_in_place_alias_target` e2e release test so it passes: added an explicit `@pytest.mark.timeout(120)` (the test runs four sequential `mngr` invocations and was exceeding the global 10s `func_only` timeout), and removed the superfluous `@pytest.mark.modal` mark (the test is local-only and never invokes Modal, which the resource guard correctly flagged).

Fixed the `test_create_in_place` release e2e test: added an explicit
`@pytest.mark.timeout(120)` so the test's multiple serial `mngr` subprocess
invocations are not killed by the default 10s per-test timeout, and removed the
superfluous `@pytest.mark.modal` mark (the in-place `--transfer=none` flow runs
entirely on the local provider and never invokes Modal). Also strengthened the
test to confirm at runtime, via `mngr exec my-task pwd`, that the agent process
actually runs in the source directory rather than relying on `mngr list`
metadata alone.

Fixed a regression where the first `mngr create --provider modal` for a brand-new
Modal environment failed with "Provider 'modal' has no state yet" instead of
bootstrapping the environment. The create flow now resolves the new-host provider
with `is_for_host_creation=True`, allowing the per-user Modal environment to be
created on first use.

Strengthened the `test_create_modal_idle_mode_run` e2e test to verify the
concrete effect of the create (a command-type agent that runs the requested
command with run idle mode and a 60s timeout) instead of only checking the exit
code, and marked it flaky so offload retries transient remote Modal create
failures.

- Fix `mngr create --provider modal` (and any other backend with one-time per-user bootstrap) so the very first create in a fresh Modal environment no longer fails with `Provider 'modal' has no state yet`. The create path resolves the new-host provider for failure-teardown before provisioning; that lookup was constructed without `is_for_host_creation=True`, so on a not-yet-existing Modal environment it eagerly raised `ProviderEmptyError` instead of letting the create bootstrap the environment. It now passes `is_for_host_creation=True`, matching the subsequent host-creation lookup.

- Fixed the `test_create_modal_pass_host_env` e2e tutorial test so it actually runs: the isolated test profile configures no default agent type, so the bare `mngr create my-task --provider modal --pass-host-env MY_VAR` command failed with "No agent type provided". The test now passes `--type command -- sleep ...` (matching the convention used by the other create tests) and additionally verifies, via `mngr exec`, that the forwarded `MY_VAR` host env var actually reaches the host. No production behavior change.

- Fix the `test_create_named_agent` e2e release test so it passes: bump its per-test timeout to 120s (the cumulative `create` + `list` + `exec` work over the full provisioning path exceeds the default 10s func-only timeout, especially when the ttyd download is slow), and drop the spurious `@pytest.mark.modal` mark. The test creates a purely local agent (`mngr create my-task`) and never invokes Modal in a guard-tracked way during the call phase, so the mark tripped the resource-guard NEVER_INVOKED check once the body completed. This matches the 48 other local-only e2e tests that already omit the Modal mark.
- Strengthen `test_create_named_agent` to assert that `mngr exec my-task pwd` output is rooted in the agent's dedicated worktree (the unique `my-task-<hash>` directory reported by `mngr list`), rather than only checking that the command succeeded.

Fixed the `test_create_short_forms` e2e tutorial test (BASIC CREATION). It now
carries an explicit `@pytest.mark.timeout(120)` because it issues two `mngr
create` commands, whose combined function-body time exceeds the global 10s
pytest-timeout default. Removed its `@pytest.mark.modal` mark: the test only
creates local (`--type command`) agents and runs `mngr list`, which reaches
Modal exclusively via the in-process gRPC SDK inside the spawned `mngr`
subprocess -- a path the resource guard cannot track -- so the mark tripped the
guard's NEVER_INVOKED check. No user-facing behavior changes.

- Fix the `test_create_stack_templates` e2e tutorial test so it actually runs: add the missing `@pytest.mark.tmux` and a `@pytest.mark.timeout(120)` (a real local `mngr create` exceeds the 10s default), drop the inaccurate `@pytest.mark.modal` (the locally-substituted template never touches Modal), and correct the `with-tests` template stub (a stray trailing comma had been redirecting its body into a `settings.local.toml,` file, and `no_ensure_clean` is not a valid template field -- it now writes `ensure_clean = false`). Also add an unhappy-path test verifying that stacking an undefined `--template` name fails fast with a clear "not found" error.

Fixed the `test_create_template_short_form` e2e tutorial test: it now carries `@pytest.mark.tmux` (command-type agents start via tmux) and no longer carries the spurious `@pytest.mark.modal` (the locally-substituted template runs entirely on the local provider and never invokes modal). Also strengthened the test to verify the template actually took effect by confirming the agent runs in-place.

- Remove the superfluous `@pytest.mark.modal` mark from the `test_create_with_agent_args` e2e tutorial test. The test creates a purely local agent and runs `mngr list`, which only makes Modal gRPC calls when a Modal environment already exists; for a local-only test no Modal call is ever made, so the resource guard correctly flagged the mark as never-invoked.
- Add a complementary unhappy-path e2e test verifying that agent-targeted flags (e.g. `--model opus`) must be separated with `--`, and that omitting the separator makes `mngr create` reject the flag as an unknown option.

Strengthened the e2e tutorial test for `mngr create --branch BASE:NEW`: the happy-path test now also verifies (via `git worktree list`) that the agent's worktree is actually checked out on the new branch at the base commit, and a new unhappy-path test confirms that creating an agent with a nonexistent base branch fails cleanly without leaving a dangling agent branch.

Fixed the `test_create_with_connect_command` release e2e test (custom `--connect-command` on `mngr create`). Added an explicit `@pytest.mark.timeout(120)` (the default 10s per-test budget was exceeded by the create + list commands) and removed the stale `@pytest.mark.modal` mark: the test uses the default local provider and `mngr list` never shells out to the guarded `modal` CLI, so the mark tripped the resource guard's never-invoked check once the test ran to completion.

Strengthened the `test_create_with_custom_branch_pattern` e2e test to also verify that a `--branch ":feature/*"` pattern (with an omitted BASE) creates the new branch off the current branch, by asserting `feature/my-task` points at the same commit as `HEAD`.

Strengthened the e2e test `test_create_with_dirty_tree_fails` so it actually exercises the dirty-working-tree guard: it now supplies a concrete agent type (previously the create aborted on a missing default type, so the test passed for the wrong reason) and asserts that the failure message specifically mentions the uncommitted changes and the `--no-ensure-clean` escape hatch.

- Fix the `test_create_with_env_file` e2e tutorial test: a plain local command-agent create never exercises cross-provider discovery, so the `@pytest.mark.modal` mark was superfluous and the resource guard failed the otherwise-passing test. Dropped the mark (matching the equivalent local-only test) and strengthened the test to assert that the `--env-file` variable is actually loaded into the agent's on-disk environment, rather than only checking that the command exits cleanly.

Added a `@pytest.mark.timeout(120)` marker to the `test_create_with_env` e2e tutorial test so it no longer hits the global 10s pytest timeout when run directly (the heavier e2e tests already carry explicit timeout markers). Also strengthened the test to assert that `--env` persists the variable into the agent's on-disk env file, in addition to the existing tmux-pane check.

Strengthened the `test_create_with_explicit_branch_name` e2e tutorial test: it now asserts that an explicit `--branch ":feature/my-task"` name is taken literally (no default `mngr/my-task` branch is created) and that the new branch starts from the current branch's commit.

Fixed the `test_create_with_extra_tmux_windows` e2e release test: it now overrides
the default 10s function timeout (a real local create plus `mngr list` can exceed
it, especially when `ttyd` is being installed) and no longer carries a stale
`@pytest.mark.modal`. Since `mngr list` stopped auto-creating the per-user Modal
environment for read-only commands, this purely local-provider test never invokes
Modal, so the resource guard was correctly flagging the mark as never-invoked.
The test also now exercises two named extra tmux windows (`server` and `logs`),
matching the tutorial more faithfully.

- Fix the `test_create_with_json_output` e2e tutorial test (BASIC CREATION section): remove the incorrect `@pytest.mark.modal` marker (the test uses the local provider and never exercises the Modal CLI, so the resource guard failed it with a "never invoked modal" violation) and add `@pytest.mark.timeout(120)` (the create-plus-list body exceeds the global 10s default and was being killed mid-create).
- Add `test_create_with_quiet_output`, a sibling e2e test sharing the same output-format tutorial block, that verifies `mngr create --quiet` emits no stdout while still creating the agent (covering the previously-undocumented "--quiet suppresses all output" line of the block).

Fixed the `test_create_with_json_output` e2e release test: added the missing `@pytest.mark.timeout(120)` (matching the other modal-backed e2e tests) and removed the superfluous `@pytest.mark.modal` mark (the test creates a local command agent and only runs `mngr list`, neither of which invokes the guard-tracked `modal` CLI). Also strengthened its assertions to parse and cross-check the `--format json` output of both `mngr create` and `mngr list`.

Fixed the `test_create_with_label` e2e tutorial test: gave it a 120s timeout
(it creates a real command agent and then runs `mngr list`, which exceeds the
default 10s budget) and removed the incorrect `@pytest.mark.modal` mark (the
test only creates a local `--type command` agent and never exercises Modal).

Added `test_create_with_invalid_label_format`, an unhappy-path test sharing the
same tutorial block, which verifies that `mngr create --label <no-equals>` is
rejected with a `KEY=VALUE` error and creates no agent.

Fixed the `test_create_with_label` e2e release test. It now raises the pytest
timeout (the local `mngr create --type command` routinely exceeds the global
10s default) and drops the superfluous `@pytest.mark.modal` mark, since the
test performs a purely local create whose Modal discovery never reaches the
resource guard's subprocess-tracked `modal` CLI path.

Fixed the `test_create_with_message` e2e release test (tutorial `mngr create --message` block). It was hitting the default 10s function timeout during `mngr list` and then failing the resource guard's NEVER_INVOKED check for `@pytest.mark.modal`. Gave it a `@pytest.mark.timeout(120)` (matching the sibling idle-timeout test) and dropped the `@pytest.mark.modal` mark, since the test creates a local command agent and only touches Modal via `mngr list`'s in-subprocess SDK discovery, which the guard cannot observe.

Fixed the `test_create_with_multiple_labels` e2e tutorial test: raised its per-test timeout so the local `mngr create` has time to complete, dropped the superfluous `@pytest.mark.modal` mark (the local create never invokes Modal), and strengthened it to verify that both labels are actually applied to the created agent via `mngr list --format json`.

Fixed the `test_create_with_pass_env` e2e tutorial test, which was failing
because it carried a superfluous `@pytest.mark.modal` mark while only creating
a local command agent (the tutorial block uses the default local provider, so
the test never invoked Modal). Removed the unused mark, then strengthened the
test to verify that the forwarded `--pass-env` variable is actually present in
the created agent's environment via `mngr exec`.

Fixed the `test_create_with_pass_env` e2e tutorial test, which was incorrectly
marked `@pytest.mark.modal` despite never exercising the modal provider (it
creates a local `--type command` agent). The modal resource guard failed the
test during teardown; removing the mark fixes it.

Added a companion unhappy-path test, `test_create_with_pass_env_unset`, that
verifies `mngr create --pass-env` silently skips a variable that is not set in
the current shell: the agent is still created and the variable is absent from
its environment.

- Add a happy-path e2e tutorial test (`test_create_with_real_plugin_flags`) for the `mngr create --plugin/--disable-plugin` tutorial block. The existing `test_create_with_plugin_flags` only exercises the unhappy path (non-existent plugin names, where the strict `--disable-plugin` validation fails before `--plugin` is ever applied); the new test passes *real* registered plugin names so both flags take effect and verifies the agent is actually created and running.

Fixed the `test_create_with_project_label` e2e tutorial test, which was marked
`@pytest.mark.modal` but never invoked the modal binary (provider discovery uses
the modal Python SDK, not the guarded binary), causing the resource guard to fail
it. Removed the superfluous mark and added a companion `test_create_default_project_label`
that verifies the default `project` label is derived from the git repo folder name
when `--project` is omitted.

Fixed a regression where `mngr create --provider modal` (and any other backend
with one-time bootstrap resources) failed against a brand-new Modal environment
with `Provider 'modal' has no state yet: Modal environment ... does not exist
yet`. The create path eagerly loaded the provider for failure-teardown using a
read-only construction (`is_for_host_creation=False`), which refused to
bootstrap the environment before the actual host-creation step could create it.
It now loads with `is_for_host_creation=True`, matching the create intent (the
instance is cached, so no second provider is built).

Also tightened the `test_create_with_snapshot_fictional` release test: it now
runs the full `mngr create --provider modal --snapshot snap-123abc` flow with an
explicit agent type, verifies the bad snapshot id is actually handed to Modal
and rejected, and asserts the failure is a clean single-line error with no raw
Python traceback.

Fixed the `test_create_with_source_path_no_git` e2e tutorial test: removed the superfluous `@pytest.mark.modal` mark (the test only creates a local agent and never invokes Modal, which tripped the resource-guard "marked modal but never invoked" check) and added an explicit `@pytest.mark.timeout(120)` so the create/list/exec sequence is not killed by the default 10s per-test timeout.

Removed the inapplicable `@pytest.mark.modal` mark from the e2e tutorial test
`test_create_with_source_path`. The test creates a local agent via
`mngr create --from <path>`; its only modal contact is the incidental
discovery `mngr list` performs via the in-process modal SDK inside the `mngr`
subprocess, which the resource guard cannot observe. The mark therefore always
failed the guard's "marked modal but never invoked modal" check.

Added an explicit `@pytest.mark.timeout(120)` to the `test_create_with_template_modal_disabled` e2e release test so the `mngr create` subprocess it spawns is not killed by the tight global 10s pytest timeout on a cold start. This matches the convention already used by the other subprocess-spawning e2e tests (e.g. in `test_create_modal.py` and `test_create_commands.py`). No user-facing behavior change.

Fixed the `test_create_with_template` e2e release test. The test creates a
local in-place agent (via a `transfer = "none"` create template), so it does
not exercise the Modal provider: removed the superfluous `@pytest.mark.modal`
(which the resource guard correctly flagged as never invoked) and scoped its
verification `mngr list` to `--provider local` so it no longer fans out to the
slow, network-bound Modal provider. Also strengthened the test to confirm the
agent's actual runtime working directory via `mngr exec my-task pwd`, not just
the `mngr list` metadata. No production behavior change.

Removed the incorrect `@pytest.mark.modal` mark from the `test_create_with_transfer_none` e2e tutorial test. The test creates a purely local in-place agent (`--transfer=none`), so no Modal environment is ever created and `mngr list` correctly skips Modal discovery; the resource guard failed the test for declaring a Modal dependency it never exercises. Also strengthened the test to verify the agent's actual runtime working directory via `mngr exec`.

- Fix the `test_default_output_human_readable` e2e tutorial test: remove the superfluous `@pytest.mark.modal` mark (running `mngr ls` against an empty, isolated test environment makes no Modal API call, so the resource guard correctly rejected the mark) and strengthen its assertions to confirm the default `mngr ls` output is human-readable (`No agents found`) rather than the machine-readable `[]` that `--format json` would emit.

Fixed the tutorial's "destroy all docker agents" one-liner: it now pipes agent
ids into `mngr destroy -f -` (with the `-` stdin placeholder) instead of
`mngr destroy -f`. Without `-`, `mngr destroy` does not read from stdin and
fails with "Must specify at least one agent". This matches every other
filter-into-stdin example in the tutorial (e.g. `mngr list --ids | mngr stop -`).

Also hardened the corresponding release test to create a real Docker agent and
verify it is actually destroyed by the command, rather than only running the
command against an empty agent list.

Fixed the "destroy all Modal agents" tutorial example, which piped agent ids into `mngr destroy -f` without the `-` stdin placeholder. Like every other stdin-consuming command (`exec`, `stop`, `start`, `message`), `destroy` only reads piped input when given an explicit `-`, so the documented command failed with "Must specify at least one agent (use '-' to read from stdin)". The example (and its release test) now read `mngr list --include 'host.provider == "modal"' --ids | mngr destroy -f -`.

Strengthened the `test_destroy_all_modal_agents` release test to create a real Modal agent, confirm it is listed, run the destroy-all command, and verify the agent is gone -- so the test exercises Modal and validates the destroy behavior instead of running against an empty environment.

Added a `@pytest.mark.timeout(120)` marker to the `test_destroy_all_via_stdin`
e2e tutorial test. The test creates two command agents plus several list/destroy
commands, which exceeds the default 10s per-test timeout (it previously timed out
during the second `mngr create`). The new marker matches the convention used by
other multi-step destroy tests (e.g. `test_create_and_destroy_agent`, which uses
`@pytest.mark.timeout(60)` for a single create+destroy).

Fixed the `test_destroy_by_session_name` e2e test, which was failing because it
was marked `@pytest.mark.modal` even though the command under test
(`mngr destroy --session my-session-name`) fails fast on input validation and
never provisions a modal-backed agent. Removed the spurious mark and added a
happy-path test that creates a real agent and destroys it via its derived tmux
session name, verifying the agent is actually gone.

Corrected the "DESTROYING AGENTS" section of the mega tutorial: the
`mngr list --ids | mngr destroy - --dry-run` example referenced a `--dry-run`
flag that was removed from `mngr destroy` (and the other multi-target commands).
The tutorial now shows how to preview what would be destroyed by running
`mngr destroy my-task` without `--force` and answering "no" at the confirmation
prompt, which lists the targets without destroying anything.

Also fixed the corresponding e2e tutorial test (`test_destroy_dry_run`) so it
exercises this confirmation-preview behavior and gives the agent-creation step a
realistic timeout.

- Add a `--dry-run` flag to `mngr destroy`. It previews exactly which agents would be destroyed (honoring the same `--format`, JSON, and JSONL output options as a real destroy) and then exits without touching anything -- no agents, hosts, branches, or garbage collection. This matches the behavior already documented in the tutorial (e.g. `mngr list --ids | mngr destroy - --dry-run`) and mirrors the existing `--dry-run` on `mngr archive` and `mngr cleanup`.

Fixed the `test_destroy_multiple_at_once` e2e release test, which destroys
multiple agents in a single `mngr destroy agent-1 agent-2 agent-3 --force`
command. The test was hitting the global 10s pytest timeout (it creates three
agents and destroys them), so it now carries an explicit `@pytest.mark.timeout(120)`.
The misapplied `@pytest.mark.modal` mark was removed because the test exercises
only local command agents and never invokes Modal. Also strengthened the test
to verify all three agents exist before the destroy, that each is reported as
destroyed, and that none remain afterward. No mngr behavior change.

- Fix the `test_destroy_no_gc` e2e tutorial test, which failed the resource guard with "marked with @pytest.mark.modal but never invoked modal". By design `mngr destroy --no-gc` skips the post-destroy garbage-collection pass, which is the only step that reaches Modal, so the test cannot invoke Modal. Removed the incorrect `@pytest.mark.modal` mark and rewrote the test to actually exercise the flag: it now creates an agent, destroys it with `--no-gc --force`, and verifies the agent is gone and that the "Garbage collecting..." progress output is absent (proving gc was disabled), instead of merely checking that the bare command errors out.

- Fixed the `test_destroy_remove_branch` tutorial e2e test (`mngr destroy --force --remove-created-branch`). It now carries `@pytest.mark.timeout(60)` because destroying an agent and running the default garbage collection exceeds the 10s default pytest timeout, and the spurious `@pytest.mark.modal` mark was removed since the test creates a purely-local command agent and never invokes Modal (the resource guard flagged the unused mark). Also strengthened the test to verify the actual effect: the agent's `mngr/my-task` branch exists after create, the destroy output reports `Deleted branch: mngr/my-task`, and the branch is gone afterward.

- Fixed the `test_destroy_remove_created_branch_inline` e2e tutorial test (WORKING WITH GIT section): a create-then-destroy cycle needs more than the default 10s per-test timeout, so the test now carries `@pytest.mark.timeout(60)` to match the sibling create+destroy tests. Also strengthened the test to verify the actual effect of `mngr destroy --remove-created-branch`: it now confirms the agent's `mngr/my-task` branch exists beforehand, that destroy reports deleting it, and that the branch is genuinely gone from the repo afterward.

Fixed the `test_destroy_short_form` e2e release test, which was timing out under the global 10s default because creating and destroying a real agent takes longer. Added a `@pytest.mark.timeout(120)` override (matching the other agent-creating destroy tests) and strengthened the test to verify that `mngr rm` actually removes the agent, plus added coverage for the safe-by-default behavior where a non-forced `mngr rm` refuses to destroy a still-running agent.

Fixed the `test_destroy_specific` e2e tutorial test so the documented `mngr destroy my-task` command is actually exercised: the agent is now stopped before the bare (non-`--force`) destroy so it is genuinely destroyed instead of being skipped by the running-agent guard, an explicit per-test timeout was added (the destroy + gc exceeds the 10s default), and the test now verifies the agent is really gone via `mngr list`. Removed the superfluous `@pytest.mark.modal` mark: the test creates a localhost agent, which never invokes Modal (only remote-host provisioning does), so the mark could never be satisfied.

Fixed the `test_destroy_with_gc` e2e tutorial test: added a `@pytest.mark.timeout(120)` marker (the global 10s default was too short for the create + destroy + gc flow over the test harness) and removed a superfluous `@pytest.mark.modal` mark (the test exercises a local command agent, so gc never makes a Modal network call and the resource guard flagged the unused mark). Also strengthened the assertions to verify gc actually ran and the agent is gone.

- Remove the superfluous `@pytest.mark.modal` from the `test_env_var_mngr_headless` e2e tutorial test. The test only runs `mngr list` (with no agents) and `mngr config get headless`, which never invoke the Modal provider, so the resource guard failed the otherwise-passing test as a superfluous-mark violation.

Fixed the `mngr event` tutorial e2e tests (`test_event.py`) so they run
reliably. Each test now carries an explicit `@pytest.mark.timeout(120)` (they
previously inherited the 10s default, which is too short for a release e2e test
that creates an agent and reads its events) and passes generous subprocess
timeouts to the `mngr create`/`mngr event` calls. Removed the superfluous
`@pytest.mark.modal` mark: these tests use the default (local) provider, which
never invokes Modal, so the resource guard rejected the unused mark. The tests
keep the `tmux` and `rsync` marks, which the local agent creation genuinely
exercises. No production behavior change.

Also strengthened `test_event_default` to verify the tutorial's documented
contract that `mngr event` emits clean JSONL: every line on stdout must parse
as a JSON object (catching warnings or log lines leaking into the jq-able
stream), and any events present must carry the four guaranteed fields
(`event_id`, `timestamp`, `source`, `type`).

- Fix the `test_event_head` e2e tutorial test (`mngr event my-task --head 10`). It carried a superfluous `@pytest.mark.modal` mark even though creating a local command agent and reading its events never invokes modal, which the resource-guard check flagged as a violation. Removed the mark and added `@pytest.mark.timeout(120)` so the test does not trip the default 10s pytest-timeout while a local agent is created (rsync + tmux) and its events are read. Also strengthened the test to assert that `--head 10` returns at most 10 JSONL events, each carrying the four guaranteed fields (`event_id`, `timestamp`, `source`, `type`), and added an unhappy-path test (`test_event_head_conflicts_with_tail`) verifying that combining `--head` and `--tail` fails with "Cannot specify both --head and --tail".

Fixed the `test_event_tail` tutorial e2e test (`mngr event my-task --tail 20`).

- Added a `@pytest.mark.timeout(120)` override so the test is not killed by the
  default 10s per-test timeout while it creates a local command agent and reads
  its events.
- Removed the inapplicable `@pytest.mark.modal` mark: `mngr event <agent>`
  resolves the agent via the discovery event-stream optimization, which narrows
  discovery to the agent's (local) provider and never invokes Modal, so the
  resource guard flagged the mark as never exercised.
- Strengthened the assertions to verify the documented JSONL contract: every
  emitted line is a JSON object carrying the guaranteed `event_id`, `timestamp`,
  `source`, and `type` fields, and `--tail 20` yields at most 20 events.

- Fixed the `test_exec_all_git_status` tutorial e2e test (WORKING WITH GIT section, `mngr list --ids | mngr exec - "git status --short"`). It was inheriting the 10s default pytest timeout because `test_git.py` was the only e2e tutorial file missing per-test `@pytest.mark.timeout` markers, so it timed out before the piped command finished. Added `@pytest.mark.timeout(120)` and removed the superfluous `@pytest.mark.modal` mark: the test operates entirely on a local agent and never invokes the Modal CLI, which is the only Modal signal the resource guard can observe across the mngr subprocess boundary. Also strengthened the assertion to verify the fan-out actually reaches the agent (not merely that the command exits 0).

- Fix the `test_exec_as_other_user` e2e tutorial test (covering the `mngr exec <agent> "sudo -u other-user ..."` block) so it passes: add a `@pytest.mark.timeout(120)` override (the global 10s pytest timeout killed the test mid-`mngr exec`) and remove the incorrect `@pytest.mark.modal` mark (the test creates a local command agent and never invokes Modal, which tripped the resource-guard "marked modal but never invoked modal" check). Also strengthen the assertion to verify `mngr exec` streams back the executed command's real stdout (a numeric uid line) rather than only checking the exit code.

Removed the superfluous `@pytest.mark.modal` mark from the `test_exec_basic`
e2e tutorial test. The test creates a single local command agent and runs
`mngr exec` against it by name, a path that never invokes Modal, so the
resource guard failed the test for declaring a Modal dependency it did not
exercise. The `rsync` and `tmux` marks remain because local `mngr create`
genuinely invokes both.

Also strengthened the assertion to verify that `mngr exec` forwards the
command's stdout back from the agent's host (checking for the leading
"total" line of `ls -la`), rather than only checking the exit code.

Fixed the `test_exec_branch_show_current` e2e tutorial test (WORKING WITH GIT
section). It was hitting the default 10s pytest timeout during agent creation and
carried a superfluous `@pytest.mark.modal` mark even though it only exercises a
local command agent. Added `@pytest.mark.timeout(60)` and removed the modal mark,
and strengthened the assertion to verify the agent reports its own
`mngr/{agent_name}` branch.

- Fix the `test_exec_cwd` tutorial e2e test: it created a local `command` agent (which never touches Modal) yet carried `@pytest.mark.modal`, so the resource guard failed it with "marked with @pytest.mark.modal but never invoked modal", and it lacked a `@pytest.mark.timeout` override so it hit the 10s default timeout while creating the agent and running exec. Removed the superfluous `modal` mark and added `@pytest.mark.timeout(300)`.
- Strengthen the same test's assertions: verify `mngr exec --cwd /tmp "pwd"` prints exactly `/tmp` (line-anchored), and add a contrasting run without `--cwd` confirming the command defaults to the agent's work_dir rather than `/tmp`.

Fixed the `test_exec_force_commit` git tutorial e2e test: removed the superfluous `@pytest.mark.modal` mark (the test creates a local command agent and runs `mngr exec`, which never invokes Modal, so the resource guard failed it). Strengthened the test to verify the actual force-commit behavior by creating an uncommitted change in the agent, committing it via `mngr exec`, and asserting the commit message lands in the agent's git log and the change is no longer reported as uncommitted.

Fixed the `test_exec_git_log` tutorial e2e test: removed the superfluous
`@pytest.mark.modal` marker (the test exercises a local command agent and
never invokes Modal, which the resource guard flags), and strengthened its
assertions to verify the agent's `git log --oneline` output shows real commit
history rather than only checking the exit code.

Fixed the `test_exec_git_push_then_merge` e2e release test. It was marked
`@pytest.mark.modal` even though its body only creates a local command agent and
runs local git operations (`mngr exec`, `git fetch`, `git merge`), so it never
invokes Modal -- the resource guard failed the otherwise-passing test with "never
invoked modal". Removed the spurious mark. The test now also verifies the actual
behavior: that the agent-side `git push` fails because no `origin` remote is
configured (proving `mngr exec` forwards the command to the agent host and
surfaces its non-zero exit code) and that the local `git fetch && git merge` chain
succeeds as an up-to-date no-op.

Fixed the `test_exec_git_status_short` e2e tutorial test: removed the superfluous
`@pytest.mark.modal` marker (the test only creates a local command-type agent and
never invokes Modal, so the resource guard failed it), and strengthened it to
deterministically create an uncommitted file in the agent's workspace and assert
that `git status --short` reports it.

- Fix the `test_exec_no_start` e2e tutorial test (covering `mngr exec --no-start`): removed the spurious `@pytest.mark.modal` mark, since the test creates a local agent and the local-host exec never invokes Modal, which made the resource guard fail the test. Also strengthened the assertion to verify the command actually ran on the agent's host (the `cat /etc/os-release` output contains `NAME=`) rather than only checking the exit code.

Fixed the `mngr exec` "error handling on multiple agents" tutorial block, which
still referenced the removed `-a`/`--all` flag. It now demonstrates the supported
pattern for targeting all agents: `mngr list --ids | mngr exec - ...`. The
corresponding e2e release test was updated to match and to verify the command
actually runs on the agent's host.

Fixed the `test_exec_short_form` e2e tutorial test: removed the spurious
`@pytest.mark.modal` mark. The test creates a local `--type command` agent and
runs `mngr x my-task "git status"` on it, so it never provisions a Modal
environment and never invokes the `modal` CLI; the modal resource guard
therefore failed it with "marked with @pytest.mark.modal but never invoked
modal". Also strengthened the assertion to verify the short-form `mngr x`
actually ran git in the agent's work_dir (observing the `On branch` output)
rather than only checking the exit code.

- Fix the `test_exec_with_start` e2e tutorial test (`mngr exec my-task --start`). It was missing the `@pytest.mark.timeout` marker that every other e2e tutorial test carries, so under the default 10s pytest timeout it timed out before `mngr create` finished. It also carried a superfluous `@pytest.mark.modal` mark even though it only exercises a local-provider agent and never invokes Modal, which tripped the resource guard's "marked modal but never invoked modal" check. Added a 180s timeout marker and removed the Modal mark. Also strengthened the test to assert that `mngr exec` actually forwarded the command output (the contents of `/etc/os-release`) rather than only checking the exit code.

- Remove the incorrect `@pytest.mark.modal` from the `test_full_lifecycle` e2e release test. The test exercises a local-provider command agent (create, exec, stop, start, destroy) and never initializes the Modal provider, so it never invokes the Modal CLI. The mark caused the resource guard to fail the otherwise-passing test with "marked with @pytest.mark.modal but never invoked modal". The `tmux` and `rsync` marks are retained because the local command agent runs in tmux and transfers its worktree via rsync.
- Strengthen `test_full_lifecycle` to verify that `mngr start` actually relaunches the agent's own command after a stop: it now asserts the `sleep 100100` process is running again post-restart (via `mngr exec ... 'ps aux'`), rather than only checking that `exec` works.

- Fix the `test_gc_background_watch` e2e tutorial test (CLEANING UP RESOURCES section). It was marked `@pytest.mark.modal`, but `mngr gc` only reaches Modal via in-process gRPC SDK calls in the `mngr` subprocess -- which the resource guard cannot observe (only `modal environment create`/`deploy` shell out to the guarded `modal` CLI binary, and gc does neither). The mark could therefore never be satisfied, so the test always failed with a "never invoked modal" resource-guard violation. Removed the superfluous mark. Also strengthened the test to verify the `commands.destroy.gc` setting actually persists and that `mngr gc` (the command `watch` runs) produces garbage-collection results.

- Fix `mngr gc --provider <name>` so that an explicitly selected provider which reports it has no state yet (e.g. a Modal per-user environment that has not been created) is skipped rather than failing the whole command. This matches the existing behavior of `mngr list --provider modal` and the documented intent that read/cleanup paths can always safely skip an empty provider; previously `mngr gc --provider modal` exited with an error on a fresh setup.
- Fix `mngr create` on a fresh Modal account: new-host creation no longer fails with "Provider 'modal' has no state yet". The provider handle used for teardown-on-failure was being constructed read-only *before* the host was resolved, which raised before the create path could bootstrap the Modal per-user environment. It is now obtained after host resolution, reusing the creation-capable (cached) provider instance.

Fixed the `test_get_json_into_var` scripting e2e test. It now overrides the 10s
global pytest timeout (Modal provider discovery inside the `mngr list` subprocess
can exceed it) and no longer carries `@pytest.mark.modal`: `mngr list` discovers
Modal via the in-process Python SDK inside the subprocess, which the modal
resource guard (whose SDK monkeypatch lives only in the pytest process) cannot
observe, so the mark produced a spurious "never invoked modal" failure. The test
also strengthens its assertion to confirm the shell variable actually captured a
non-empty JSON document.

Fixed the `test_git_merge_agent_branch` e2e release test: removed the
superfluous `@pytest.mark.modal` marker (the test only creates a local command
agent and merges its branch, so it never invokes Modal and the resource guard
failed it). Also strengthened the test to merge real agent work and verify the
merged file appears in the caller's working tree.

Updated the `mngr --help` tutorial line to list current top-level commands (`git`, `clone`) instead of the removed standalone `push`/`pull` commands, which are now `mngr git push`/`mngr git pull`. Added an unhappy-path e2e test verifying that an unknown command fails with a helpful error pointing back to `--help`.

Strengthened the `mngr --help` e2e test to assert that the help output lists the
other commands the tutorial advertises (`destroy`, `message`, `connect`, `clone`
in addition to `create` and `list`), and added an unhappy-path test verifying
that an unknown command fails and points the user back to `mngr --help`.

Strengthened the `test_invalid_provider_fails` e2e test so it genuinely exercises the unknown-provider path. Previously the command `mngr create my-task --provider nonexistent ...` was rejected first for not specifying an agent type, so the test passed without ever validating the provider. It now passes `--type command` so the failure is attributable to the unknown provider backend, asserts the error message names the offending provider, and confirms via `mngr list` that the failed create left no agent behind. Test-only change; no user-facing behavior change.

Fixed the tutorial example that combines `mngr list --format json` with `jq`. The
filter now reads `.agents[] | select(.state == "RUNNING") | .name` to match the
actual JSON shape (`mngr list --format json` emits an object with an `agents`
array), instead of the previous `.[]` which errored with "Cannot index array with
string". Tightened the corresponding e2e test accordingly.

- Fix the "JSON and JSONL works with most commands" tutorial example so it actually runs. It previously used `mngr snapshot list --format json`, but `mngr snapshot list` requires an explicit agent or host target and exits with a usage error when given none, so the example never worked standalone. It now uses `mngr list --format json && mngr plugin list --format jsonl`, two commands that work without setup and still demonstrate that `--format json`/`--format jsonl` apply across different commands. The corresponding e2e test additionally parses the combined output to confirm the JSON half is a single object and the JSONL half is one parseable object per line.

Fixed the `test_list_active_filter` e2e tutorial test, which exercises `mngr list --active`. The test was marked `@pytest.mark.modal`, but in a fresh environment with no agents `mngr list` deliberately skips the (not-yet-created) Modal provider instead of invoking it, so the resource guard failed the test for declaring a Modal mark it never used. Removed the superfluous mark and strengthened the test to assert the command reports "No agents found".

Fixed the `test_list_archived_filter` e2e release test: dropped the superfluous `@pytest.mark.modal` mark (the read-only `mngr list --archived` smoke test never invokes the `modal` CLI in an empty environment, so the resource guard flagged the mark as never-invoked) and strengthened it to assert that the `--archived` filter returns an empty, well-formed listing in a fresh environment.

Fixed the `test_list_combine_exclude_filters` e2e tutorial test (LABELS AND FILTERING section). The test was marked `@pytest.mark.modal` but a read-only `mngr list` never invokes the Modal CLI binary (it only uses the Modal SDK, which the resource guard cannot observe from the `mngr` subprocess), so the resource guard failed the test with "marked with @pytest.mark.modal but never invoked modal". Removed the superfluous mark and strengthened the test to create labeled agents and assert that `--exclude` applies OR logic (an agent matching any exclusion filter is dropped).

- Test-only change: fixed and strengthened the `test_list_combine_include_filters` e2e tutorial test for the LABELS AND FILTERING section. Removed an incorrect `@pytest.mark.modal` (the test never invokes Modal, so the resource guard failed it) and rewrote it to create labeled agents, stop one, and assert that combining multiple `--include` CEL filters applies AND semantics (every returned agent must satisfy all clauses).

Fixed the `test_list_compound_cel` LABELS tutorial e2e test, which timed out under the default 10s pytest limit and carried an unsatisfiable `@pytest.mark.modal` mark (a plain `mngr list` never shells out to the `modal` CLI, so the guard reported it never invoked Modal). Added a `@pytest.mark.timeout(120)` override and dropped the Modal mark. The test now creates labelled local agents and asserts that the compound CEL expression `labels.team == "backend" && state == "RUNNING"` enforces both predicates of the conjunction.

- Fix the "OUTPUT FORMATS" tutorial's custom-format example: `mngr list --format '{agent.name} ({agent.state})'` referenced a non-existent `agent.` namespace and rendered empty fields (` ()`). The list item *is* the agent, so fields are referenced bare; the example is now `mngr list --format '{name} ({state})'`, which renders `<name> (<STATE>)`.
- Strengthen the `test_list_custom_human_format` e2e release test: it now creates a real agent and asserts the template expands to the agent's actual name and state, plus an edge case confirming that unknown template fields render as empty strings.

Fixed the `test_list_exclude_filter` e2e tutorial test. It carried a
`@pytest.mark.modal` mark, but the documented `mngr list --exclude ...` command
never shells out to the `modal` CLI (it only performs in-process SDK discovery,
which the resource guard cannot observe across the subprocess boundary), so the
guard failed the test with "marked with @pytest.mark.modal but never invoked
modal". Removed the mark and rewrote the test to create two labeled command
agents and assert that the exclusion filter actually drops the matching agent
while keeping the others.

- Fix the WORKING WITH GIT tutorial to reference the real `initial_branch` field (the branch mngr creates for each agent) instead of the non-existent `git.original_branch` field in its `mngr list --fields` example, and harden the corresponding e2e test (correct its resource marks and assert on the command's actual output).

Fixed the `test_list_filter_by_label_cel` e2e tutorial test. It was marked `@pytest.mark.modal`, but a `mngr list` against an empty environment never creates Modal state and so never invokes the Modal CLI -- the only Modal usage the resource guard can track across the e2e subprocess boundary -- causing a spurious "marked with @pytest.mark.modal but never invoked modal" failure. The mark was removed, and the test now creates local agents with distinct `priority` labels to verify that the CEL filter actually includes matching agents and excludes non-matching ones.

Fixed the `test_list_filter_by_state` and `test_multiple_agents_coexist` e2e release tests, which could never pass: they lacked a per-test `@pytest.mark.timeout` override and so inherited the global 10s timeout (too short for creating/stopping agents), and they carried a spurious `@pytest.mark.modal` mark even though they only ever create local-provider agents (tripping the resource guard's "marked modal but never invoked modal" check). Also strengthened `test_list_filter_by_state` to assert on each agent's actual reported `state` (STOPPED vs RUNNING/WAITING), not just name membership in the filtered list.

Fixed the `test_list_filter_project_cel` e2e tutorial test, which exercises filtering agents by project with a CEL expression (`mngr list --include 'project == "my-project"'`). The test carried an unsatisfiable `@pytest.mark.modal` mark: `mngr list` reaches Modal only through the in-process gRPC SDK, which the e2e subprocess harness cannot track (the modal resource guard is only satisfied by the `modal` CLI binary, which list never invokes). Removed the spurious mark so the local CEL-filtering behavior is tested without a false guard failure.

Also strengthened the happy-path assertion (the CEL filter is evaluated and matches no agents in a fresh environment) and added an unhappy-path test that verifies a syntactically invalid `--include` expression is rejected with a clear `Invalid include filter expression` error.

Fixed the `test_list_format_json_recap` e2e test (covering `mngr list --format json`): removed an incorrect `@pytest.mark.modal` marker (a bare `mngr list` against an empty environment never invokes Modal, so the resource guard failed the test) and strengthened it to parse the JSON document and assert the documented `agents`/`errors` array structure.

Fixed the `test_list_format_jsonl_recap` e2e tutorial test (covering `mngr list --format jsonl`). It was failing for two reasons: the bare command exceeded the 10s default pytest timeout because Modal discovery is slow when credentials are present, and `@pytest.mark.modal` deterministically tripped the resource guard's "never invoked modal" check (a read-only `mngr list` reaches Modal only via in-process gRPC inside the `mngr` subprocess, which the in-process SDK guard cannot observe, and it never shells out to the guard-observable `modal` CLI). Added a `@pytest.mark.timeout(180)` override and removed the unenforceable `@pytest.mark.modal`. Also strengthened the assertion to verify the JSONL contract (every emitted line parses as a JSON object).

Removed the unsatisfiable `@pytest.mark.modal` mark from the `test_list_format_jsonl` e2e tutorial test. `mngr list` against an empty environment never shells out to the `modal` CLI binary (it only does in-process SDK discovery), so the modal resource guard reported the mark as never-invoked and failed the test. Also strengthened the test to verify that the JSONL output is well-formed (each emitted line is a standalone JSON object).

Fixed the `mngr list --host-label` e2e tutorial test so it no longer carries a
`@pytest.mark.modal` mark it cannot satisfy: `mngr list` on a fresh environment
skips the Modal provider (the environment does not exist yet) and never runs the
`modal` binary, so the resource guard correctly reported the mark as superfluous.
Also strengthened the test to verify the empty-result output and added an
error-path test for an invalid `--host-label` value (missing `KEY=VALUE`).

- Fix the tutorial's jq label-filtering example. `mngr list --format json` emits a top-level object (`{"agents": [...], "errors": [...]}`), not a bare array, so the documented `mngr list --format json | jq '.[] | select(.labels.priority == "high")'` failed with `jq: error: Cannot index array with string "labels"` (because `.[]` iterates the object's values -- the two arrays). The example now uses `.agents[]` to iterate the agent objects. Updated both the `mega_tutorial.sh` resource and the corresponding e2e test (`test_list_jq_filter`).

Fixed the LABELS tutorial e2e test `test_list_jsonl_jq_stream` so it exercises the streaming jq filter against a real fleet: it now seeds two local command agents labeled `priority=high` and `priority=low`, then asserts `mngr list --format jsonl | jq 'select(.labels.priority == "high")'` emits the high-priority agent and drops the low-priority one. Removed the superfluous `@pytest.mark.modal` (plain `mngr list` exercises Modal only via the in-process SDK, which is not tracked across the `mngr` subprocess) and added the `rsync`/`tmux` marks plus a longer timeout that local agent creation requires.

- Remove a superfluous `@pytest.mark.modal` from the e2e tutorial test `test_list_label_filter`. The resource guard correctly flagged it as never invoking modal: `mngr list` skips the Modal provider (via `ProviderEmptyError`) when the per-user Modal environment does not exist yet, which is always the case in these no-agent e2e tests, so the `modal` binary is never invoked.
- Strengthen the happy-path assertion to verify the label filter actually runs and produces an empty result ("No agents found"), and add an unhappy-path test (`test_list_label_filter_invalid_format`) covering the `KEY=VALUE` validation error for a malformed `--label` value.

Fixed the `test_list_limit` e2e tutorial test: removed the superfluous
`@pytest.mark.modal` mark. `mngr list --limit 10` in a fresh environment never
invokes the Modal CLI (the only Modal usage the e2e resource guard can observe
in a subprocess), so the mark tripped the guard's `NEVER_INVOKED` check.

Strengthened the same test so it actually exercises `--limit`: it now creates
two agents, asserts that `mngr list --limit 10` shows both, and that
`mngr list --limit 1` truncates the result to a single agent (previously it
only ran the command against an empty environment and checked the exit code).

Fixed the `test_list_local_filter` e2e release test (covering the `mngr list --local` tutorial block). The test runs `mngr` as a subprocess, where the real Modal SDK call happens during full provider discovery; the resource guard's in-process Modal SDK monkeypatch cannot observe that subprocess call, so `@pytest.mark.modal` was failing as "never invoked". The test now records the subprocess Modal usage with the guard explicitly (mirroring the lima release test's approach). It also carries an explicit `@pytest.mark.timeout(60)` because `mngr list`'s discovery path routinely runs ~10s, past the default 10s per-test timeout (the release CI lane already overrides this globally to 90s). Strengthened the assertion to verify the command reports "No agents found" in the fresh environment.

Fixed the `test_list_pipe_stdin` e2e tutorial test (covering `mngr list --format "{id}" | head -n 2 | mngr list --stdin`): removed the incorrect `@pytest.mark.modal` mark (the list pipeline never invokes Modal) and gave it a realistic timeout, then improved it to create a real agent and verify that ids piped through `mngr list --stdin` actually filter the listing.

- e2e tests: fixed the PROJECTS tutorial test `test_list_project_dot` by removing the
  superfluous `@pytest.mark.modal` mark. `mngr list --project .` runs in a subprocess
  and only touches Modal via the SDK (gRPC), which the resource guard cannot track from
  the subprocess (it only tracks the Modal CLI there), so the mark tripped the guard's
  "marked but never invoked" check. Also strengthened the test to assert the command
  emits a clean "No agents found" listing, confirming `.` is expanded to the current
  project rather than rejected or treated literally.

- `mngr list --fields` now accepts `project` as a short alias for the `labels.project` field (mirroring the existing `--project` filter flag and the `host.provider` field alias). Previously `mngr list --fields "name,project,state"` rendered an empty `PROJECT` column because `project` resolved to a nonexistent top-level attribute; it now shows each agent's project label.

Removed the superfluous `@pytest.mark.modal` from the `test_list_project_filter`
e2e tutorial test. A read-only `mngr list --project ...` against a fresh
environment never invokes the `modal` CLI binary (the only Modal usage trackable
from a spawned subprocess) and never creates the Modal environment, so the
resource guard's "marked modal but never invoked modal" check failed the test.

Also strengthened the test to assert the filtered listing renders the expected
empty result ("No agents found") instead of only checking the exit code, so a
command that errors but still exits 0 would be caught.

- Fix the `test_list_provider_docker` e2e tutorial test so it actually exercises the docker provider. The test was marked `@pytest.mark.docker` but only ran `mngr list --provider docker`, which reads the state volume via the docker SDK in a subprocess (invisible to the CLI-binary resource guard) and never invokes the `docker` CLI -- so the resource guard flagged the mark as "never invoked". The test now first creates a lightweight docker command agent (which shells out to `docker build`/`docker run`, satisfying the guard) and then asserts that the created agent appears in `mngr list --provider docker` output, making the listing assertion meaningful instead of just checking a clean exit. Added `@pytest.mark.rsync` and `@pytest.mark.timeout(300)` to match the other docker-create tests. The tutorial block itself is unchanged.

Removed the spurious `@pytest.mark.modal` mark from the `mngr list --provider`
e2e tutorial test. The mark is unsatisfiable for these subprocess-based e2e
tests (the Modal SDK guard only observes in-process calls, and `mngr list`
skips the Modal backend entirely when its per-user environment does not exist
yet), so the resource guard failed them with "marked modal but never invoked".
Also strengthened the test to assert the empty-listing output and added an
unhappy-path test covering an unknown provider name.

Fixed the `test_list_remote_filter` e2e tutorial test. It was marked
`@pytest.mark.modal`, but `mngr list --remote` only performs read-only Modal SDK
discovery (which short-circuits when no Modal environment exists) and never
invokes the Modal CLI, so the resource guard correctly reported that the test
never exercised Modal. Removed the unsatisfiable mark and strengthened the test
to actually verify the `--remote` filter: it now creates a local agent and
asserts that the agent is excluded from `mngr list --remote` while remaining
visible under `mngr list --local`.

Tightened the `mngr list --running` e2e tutorial test (`test_list_running_filter`). It previously carried `@pytest.mark.modal` but only ran `mngr list --running` against an empty environment, which never reaches Modal; the resource guard now flags such superfluous marks. The test now creates real agents and asserts that `--running` includes a genuinely running agent and excludes a stopped one.

- Fix the release test `test_list_stopped_filter` (in `imbue/mngr/e2e/tutorial/test_list.py`) by removing its superfluous `@pytest.mark.modal`. In an isolated, empty environment, `mngr list --stopped` discovers via the provider SDKs (Modal gRPC) and never shells out to the `modal` CLI binary -- the only Modal usage the resource guard can observe across the `mngr` subprocess boundary -- so the mark tripped the guard's "marked with @pytest.mark.modal but never invoked modal" check. Also strengthen the test to assert the command reports `No agents found` rather than only checking the exit code. No production behavior change.

Fixed the `test_list_watch_mode` e2e tutorial test. It was marked
`@pytest.mark.modal` even though `watch -n5 mngr list` against a fresh,
empty environment never exercises Modal (the listing pipeline skips the
Modal provider when its environment does not exist), which made the resource
guard fail the test for a superfluous mark. Removed the mark and strengthened
the assertion to verify that `watch` actually renders the wrapped `mngr list`
output.

- Remove the superfluous `@pytest.mark.modal` mark from the `test_list_with_no_agents` e2e tutorial test. In a fresh, isolated environment `mngr list` correctly short-circuits Modal host discovery (the Modal environment does not exist yet, so the provider raises `ProviderEmptyError` without making any Modal API call). Because the test never actually invokes Modal, the resource guard failed it with "marked with @pytest.mark.modal but never invoked modal". The test still runs under `@pytest.mark.release` and verifies that `mngr list` succeeds and prints "No agents found".

- Add a `-a`/`--all` flag to `mngr message` (alias `mngr msg`) to broadcast a message to every agent, matching the documented tutorial usage (`mngr msg -a -m "..."`). Previously the flag was undocumented in the CLI and rejected with "No such option: -a", even though the underlying API already supported it. `--all` cannot be combined with explicit agent names, and on its own it no longer requires naming an agent.

Fixed the `test_message_commit_request` git tutorial e2e test (`mngr msg`): it
now has a longer per-test timeout to accommodate cross-provider agent discovery,
and no longer carries a superfluous `@pytest.mark.modal` mark (the command only
contacts Modal via the mngr subprocess SDK, which the resource guard cannot
attribute to the test, so the mark always tripped the "never invoked" check).
The test also asserts that `mngr msg` actually reports delivery to the target
agent, not just that the command exits cleanly.

Fixed the `test_message_filtered_backend` e2e tutorial test (LABELS AND FILTERING
section). The test now creates a real backend-labeled agent and a frontend-labeled
agent before running the `mngr list --include ... --ids | mngr message -` pipeline,
and asserts the message reaches only the backend agent. Replaced the incorrect
`@pytest.mark.modal` marker (the command never invokes Modal) with the markers that
match the resources actually used (`tmux`, `rsync`) plus an explicit per-test timeout.

Fixed the `test_message_filtered_via_stdin` e2e tutorial test. It now carries a
`@pytest.mark.timeout(90)` (plus a longer per-command timeout) so the two-stage
`mngr list | mngr msg` pipeline has enough headroom on slow filesystems, and the
superfluous `rsync`/`tmux`/`modal` resource marks were dropped because the
empty-filter pipeline is a no-op that never invokes those resources.

Also hardened the test to assert the filtered id list really is empty and that
no message is reported as delivered, and added a happy-path companion test
(`test_message_filtered_via_stdin_delivers_to_matching_agents`) that pipes a
non-empty id list (local-provider agents) into `mngr msg -` and verifies the
message is actually delivered to each matched agent.

- Fixed the `test_message_multiple_agents_by_name` e2e tutorial test: added `@pytest.mark.timeout(120)` so creating three local command agents over tmux+rsync no longer trips the default 10s pytest-timeout, and removed the superfluous `@pytest.mark.modal` mark (the test only messages local agents by name and never invokes Modal, which the resource guard correctly rejected). Also strengthened the assertions to verify each named agent is actually reached (`Message sent to: agent-N`) and that the aggregate success count is reported.

- Fix the `test_message_one_agent` e2e tutorial test: messaging a single named local agent never invokes Modal, so its superfluous `@pytest.mark.modal` was removed (the resource guard was failing the otherwise-passing test). Strengthened its assertions to verify the message is actually delivered to the named agent (`Message sent to: my-task`, exactly one successful delivery). Added an unhappy-path `test_message_nonexistent_agent` covering the same tutorial block, which asserts that messaging a non-existent agent is a no-op (`No agents found to send message to`, exit 0) rather than an error.

Fixed the `test_message_short_form` e2e tutorial test. It previously timed out
under the default 10s pytest timeout while creating the local command agent, and
it carried an incorrect `@pytest.mark.modal` marker even though messaging a
single named local agent never invokes Modal (the resource guard fails such
tests). Added `@pytest.mark.timeout(180)` and removed the `modal` marker. Also
strengthened the test to assert the message was actually delivered ("Message
sent to: my-task" / "Successfully sent message to 1 agent(s)") rather than only
checking the exit code. Test-only change; no user-facing behavior change.

Fixed the multi-agent e2e release tests (`test_multiple_agents_coexist`,
`test_list_filter_by_state`) so they pass outside of offload: added explicit
`@pytest.mark.timeout` markers (the global 10s default killed them in plain
`pytest` runs) and removed the spurious `@pytest.mark.modal` mark. These tests
create only local-provider command agents and never invoke Modal through a
tracked code path, so the Modal resource guard failed them with "marked with
@pytest.mark.modal but never invoked modal".

Fixed the release e2e test `test_observe_discovery_pipe_python` (which exercises the tutorial's `mngr observe --discovery-only | python` pipe). It had been failing the resource-guard check because it carried `@pytest.mark.modal` while a discovery-only command never invokes the `modal` CLI binary (it reaches modal only via the in-process gRPC SDK, whose guard monkeypatch does not cross the `mngr` subprocess boundary). Removed the inapplicable mark and strengthened the test to warm the discovery cache and assert the discovery snapshot actually flows through the python one-liner. Test-only change; no user-visible behavior change.

Fixed the `test_observe_discovery_recap` e2e release test, which covers the
`mngr observe --discovery-only` tutorial block. The test was marked
`@pytest.mark.modal`, but on a fresh test environment (no Modal agents created)
the discovery stream deliberately skips the Modal provider because its
environment does not exist yet, so no Modal call is ever made. The resource
guard therefore failed the test for declaring a Modal dependency it never
exercised. Removed the superfluous mark and strengthened the test to assert that
the stream actually emits a `DISCOVERY_FULL` JSONL snapshot, rather than only
checking that the command exits cleanly.

Strengthened the `test_plugin_add_by_git` e2e tutorial test. It now asserts the
command exits with code 1 and emits a clean "Aborted:" message (rather than just
a non-zero exit code), confirming that `--git` is accepted as a source specifier
and the command reaches an intentional error path instead of a click usage error
or an uncaught traceback.

Raised the per-test timeout for the `mngr plugin add <name>` e2e tutorial test so it accounts for mngr's ~10s cold-start cost, and strengthened its assertions to verify the command exits with a clean, controlled error (non-zero exit plus an "Aborted" message, no traceback) instead of merely checking the exit code.

Strengthened the `mngr plugin add --path` e2e tutorial test to assert a clean abort (exit code 1) with an `Aborted` message and no traceback or click usage error, rather than only checking for a non-zero exit code. This verifies the `--path` option is recognized and the command fails gracefully when the path cannot be installed.

Fixed the `test_plugin_disable_user_scope` e2e tutorial test to match the actual
behavior of `mngr plugin disable my-plugin --scope user`: disabling a not-yet-installed
plugin is a soft operation that succeeds (with a warning) and persists the setting. The
test now asserts the command succeeds, emits the "not currently registered" warning, and
verifies the disabled state is persisted by reading it back via `mngr config get
plugins.my-plugin.enabled --scope user`.

Fixed the `test_plugin_enable_project_scope` e2e tutorial test to match the actual behavior of `mngr plugin enable --scope project`. Enabling a not-yet-installed plugin is a soft pre-configuration that succeeds, records `plugins.<name>.enabled = true` in the project `settings.toml`, and warns that the setting applies once the plugin is installed. The test now verifies this concrete effect instead of incorrectly expecting a failure.

Strengthened the `test_plugin_list_active_to_see_types` e2e tutorial test to verify that `mngr plugin list --active` actually lists the built-in agent types (claude, codex, command) the tutorial discusses, instead of only asserting the command exits successfully.

Strengthened the `mngr plugin list --active` e2e tutorial test to verify the actual filtering behavior: it now parses the JSON output to assert every listed plugin is enabled, disables a real plugin, and confirms it disappears from `--active` while still appearing (as disabled) in the unfiltered `mngr plugin list`.

Corrected the plugin-management tutorial: `mngr plugin list --fields` now demonstrates the real `enabled` field instead of the non-existent `active` field (which silently rendered as `-` for every plugin). Strengthened the corresponding e2e release test to assert that the requested fields render real values.

Strengthened the `mngr plugin list` e2e tutorial test (`test_plugin_list_shows_installed`) to assert the listing renders its column headers (NAME, VERSION, DESCRIPTION, ENABLED) and includes core always-shipped plugins (`claude`, `modal`), rather than only checking for the substring `claude`.

Strengthened the `mngr plugin remove` e2e tutorial test to assert that removing
a non-installed plugin fails cleanly with a user-facing "Aborted" error and
never a Python traceback, and added an unhappy-path test that verifies an
invalid package name is rejected with a clear argument-validation error.

Fixed the `test_recipe_launch_check_cleanup` release e2e test (COMMON TASKS recipe block) so it passes against the current CLI. The test substitutes a local `command` (sleep) agent for the recipe's modal claude agent, and several recipe steps behave differently for that stand-in:

- Added the missing `@pytest.mark.timeout(300)` marker (all sibling tutorial e2e tests carry one); without it the test fell back to the global 10s default and timed out partway through.
- The `mngr transcript` step now asserts the real behavior for a `command` agent: command agents do not produce a common transcript, so the exact recipe command (`mngr transcript fix-bug --tail 3`) exits non-zero with a clear "does not produce a common transcript" message.
- The `mngr conn` step runs in the e2e harness without a TTY, so the interactive `tmux attach` cannot complete; the test now verifies the command resolves the named agent and reaches the connect step rather than asserting a clean exit.
- Removed the superfluous `@pytest.mark.modal` marker: the recipe substitutes a local command agent and never invokes Modal, which tripped the resource-guard "marked modal but never invoked modal" check.

Also strengthened the test's verifications: it now confirms the created agent is genuinely alive in its own worktree (`mngr exec fix-bug pwd`) -- the concrete intent of the "check what agents are running" step, which `mngr list --running` alone could not show for the idle `sleep` stand-in (it reports WAITING, not RUNNING) -- and confirms `mngr destroy --remove-created-branch` actually removed the agent's branch.
</content>

Fixed the `test_recipe_multi_agent_parallel_workflow` e2e release test for the
MULTI-AGENT WORKFLOWS tutorial recipe:

- Added a `@pytest.mark.timeout(120)` override so the multi-step recipe (which
  creates three command agents plus list/wait/exec/msg/merge/destroy) is not
  killed by the default 10s per-test timeout, and removed the superfluous
  `@pytest.mark.modal` mark since the test exercises local command agents only.
- Corrected the tutorial's "message all agents" step: `mngr msg -a` is not a
  valid option. It now uses the documented idiom
  `mngr list --ids | mngr msg - -m "..."` (updated in `mega_tutorial.sh` too).
- Strengthened the test to verify actual behavior rather than just exit codes:
  all three agents are created and isolated in distinct worktrees, the
  broadcast message reaches all three, and cleanup removes them.

- Fix the very first `mngr create --provider modal` (or any create that bootstraps a fresh per-user Modal environment) so it no longer aborts with `Provider 'modal' has no state yet`. The create path's teardown-guard provider resolution was running with read-only semantics and raising `ProviderEmptyError` before the environment could be created; it now resolves the provider in host-creation mode, creating the environment exactly once.
- Extend the `mngr snapshot create -` (snapshot-all-via-stdin) e2e test to verify the snapshot is actually recorded for the agent and visible via `mngr snapshot list`, rather than only checking the command's exit code.

- Add a `--dry-run` flag to `mngr snapshot destroy`. It resolves and reports exactly which snapshots would be destroyed (honoring `--snapshot` / `--all-snapshots`) without deleting anything, in human, JSON, JSONL, and `--format` template output. This matches the behavior already documented in the tutorial.
- Fix a regression where the very first `mngr create --provider modal` (or `mngr create NAME@.modal`) against a brand-new Modal environment failed with "Provider 'modal' has no state yet" instead of bootstrapping the per-user Modal environment. The create path now resolves the new-host provider with `is_for_host_creation=True`, consistent with how the host itself is resolved.

- Fix the `test_start_all_via_stdin` e2e tutorial test (STARTING/STOPPING section). It was failing two ways: the global 10s `pytest-timeout` budget was too short for a test body that runs multiple `mngr` subprocess invocations (rsync + tmux), and it carried a superfluous `@pytest.mark.modal` mark even though it only exercises a local-provider agent and never invokes the `modal` binary (which tripped the resource-guard "marked modal but never invoked modal" check). Added `@pytest.mark.timeout(120)` and removed the modal mark. Also strengthened the test to stop the agent and verify it transitions back to running when started via stdin, rather than only checking the command exit code. Test-only; no runtime behavior change.

Fixed the `test_start_connect` e2e tutorial test (covers `mngr start <agent> --connect`):
raised its per-test timeout so the full local create + start round-trip fits, and
dropped the inapplicable `@pytest.mark.modal` mark (starting a named local agent never
enumerates Modal). Also strengthened the test to verify the `--connect` path actually
ran the connect command (rather than only checking the command's exit code).

- Add a `--dry-run` flag to `mngr start`. With it, the command reports which agents *would* be started (e.g. `mngr list --ids | mngr start - --dry-run`) and returns without contacting any host or starting anything. This matches the `--dry-run` already supported by `mngr archive`, `mngr destroy`, and `mngr gc`, and the dry-run usage shown in the tutorial.

- Fix the `test_start_idempotent` e2e tutorial test (STARTING AND STOPPING AGENTS) so it can run standalone, not only under the offload release harness. Added an explicit `@pytest.mark.timeout(180)` (the test creates and starts a real agent over tmux, which exceeds the 10s default that applies outside offload) and removed the inaccurate `@pytest.mark.modal` mark (the test exercises a local command agent and never invokes Modal; the resource guard correctly flagged the mark as superfluous).
- Strengthen the same test to verify actual behavior beyond a zero exit code: after the idempotent start it now confirms the agent was not torn down by exec'ing into it and checking the command lands in the agent's own worktree. Added a companion `test_start_stopped_agent` covering the primary documented path -- stopping a running agent and starting it back up -- asserting on the `Stopped agent` / `Started agent` output and post-restart reachability.

- Remove the incorrect `@pytest.mark.modal` from the `test_start_multiple_agents` e2e tutorial test. The test only creates and starts local `--type command` agents and never runs a Modal-touching command (e.g. `mngr list`), so the resource guard failed it for declaring a `modal` mark that was never exercised. Also strengthened its assertions to verify the single `mngr start agent-1 agent-2 agent-3` invocation actually reports all three agents as started, rather than only checking the exit code.

- Fix the `test_stop_all_via_stdin` lifecycle e2e test (and improve its assertions). It was marked `@pytest.mark.modal` but creates a default (local-provider) command agent, so it never invokes the Modal CLI; the resource guard correctly failed it for declaring a Modal mark it never exercised. Removed the spurious `modal` mark (keeping `rsync`/`tmux`, which the local create path genuinely uses) and added behavioral assertions that verify the agent actually transitions to a stopped state after `mngr list --ids | mngr stop -`.

Fixed the `test_stop_archive` e2e lifecycle test: added a `@pytest.mark.timeout(120)` mark (the test was hitting the global 10s pytest timeout during the create + stop flow) and removed the superfluous `@pytest.mark.modal` mark, since the test only exercises a local command agent and never invokes Modal. Also strengthened the test to verify that `mngr stop my-task --archive` actually stops and archives the agent (the agent now appears under `mngr list --archived` and is excluded from `mngr list --active`).

Corrected the tutorial comment for `mngr stop --archive`: it previously claimed the command "creates a snapshot before stopping", but `--archive` only marks the agent archived (sets the `archived_at` label) while preserving its state -- no snapshot is created.

- Fix the `test_stop_basic` e2e tutorial test (STARTING AND STOPPING AGENTS section). It inherited the global 10s pytest timeout, which was too short for a real agent create + stop and caused the test to time out; it now sets an explicit `@pytest.mark.timeout(180)`. The spurious `@pytest.mark.modal` mark was also removed because the test creates a local command agent and never invokes Modal (its commands run entirely against the local provider). The test now also verifies the concrete effect of `mngr stop` by asserting the agent is reported as `STOPPED` via `mngr list --stopped` and no longer appears in `mngr list --running`.

- Fix the `test_stop_by_session_name` e2e tutorial test (covering `mngr stop --session`) so it reliably passes: add a `@pytest.mark.timeout(60)` override (the default 10s function timeout was too tight for the mngr subprocess cold start, matching the timeout overrides used by every other e2e tutorial test file) and drop the inaccurate `@pytest.mark.modal` mark (the malformed placeholder session name is rejected by the prefix-format guard before any provider/Modal scan runs). Also tighten the assertions to verify a clean non-zero exit with a clear validation error and no Python traceback.

- Add a `--dry-run` flag to `mngr stop`. It reports which agents (or, with `--stop-host`, which hosts) would be stopped without actually stopping anything, matching the `--dry-run` flag already offered by `mngr archive`, `mngr destroy`, and `mngr cleanup`. This makes the documented `mngr list --ids | mngr stop - --dry-run` tutorial example work.

Fixed the create-template e2e tutorial tests (`test_templates.py`). The
`test_templates_setup_via_config_edit` test now opts the project `settings.toml`
that `mngr config edit --scope project` creates into the pytest run (via
`is_allowed_in_pytest = true`) before invoking `mngr create`, so the config
loader no longer refuses the freshly created project config. Also added the
missing `@pytest.mark.tmux` to all three template tests, which create local
command agents that use tmux.

- Fix the `test_tips_exec_env_inspect` e2e tutorial test so it passes: it creates a local `--type command` agent and execs a specific agent by name, which only invokes `tmux` and `rsync` (never Modal), so the spurious `@pytest.mark.modal` was dropped and a `@pytest.mark.timeout(120)` was added (the default 10s pytest timeout was killing the legitimately slow `mngr exec` call). Also strengthened the assertion to confirm `mngr exec my-task -- env` actually runs inside the agent's environment (checking for the injected `MNGR_AGENT_NAME`/`MNGR_AGENT_ID` variables) rather than relying on the pipeline's exit code, which the `| sort` pipe would otherwise mask.

- Fix the `test_tips_transcript_tail_assistant` e2e tutorial test (TIPS AND TRICKS section). The test previously created a `command`-type `my-task`, but `mngr transcript` now rejects agent types that do not produce a common transcript, so the command exited non-zero. The test now seeds a known common transcript on a `claude`-typed agent and asserts that `mngr transcript my-task --tail 5 --role assistant` emits exactly the last five assistant messages and no user messages. Also dropped the superfluous `@pytest.mark.modal` mark (the local-only flow never invokes modal). The tutorial block is unchanged.

- Fix the `test_transcript_assistant_only` e2e tutorial test (`mngr transcript my-task --role assistant`). It previously created a `command`-type agent, which `mngr transcript` correctly rejects because command agents do not emit a common transcript, so the test failed before exercising any transcript behavior. The test now creates a transcript-capable (`claude`-typed) agent and seeds a representative common transcript (user, assistant, and tool-result events), then asserts that `--role assistant` shows only the assistant message and filters out the user message and tool result. No production behavior changed.

Fixed the `mngr transcript` e2e tutorial tests (`test_transcript.py`). They
previously created a `command`-type agent, which `mngr transcript` rejects
(command agents produce no common transcript), so the tests could never pass.
They now create a real local `claude` agent with an initial message, wait for
the first assistant reply, and assert on the actual transcript content for each
variant (`--role`, `--tail`, `--format jsonl`).

- Fixed the `mngr transcript` e2e tutorial tests, which previously created a `command`/sleep agent as a Claude stand-in. `mngr transcript` only works for agent types that emit a common transcript (Claude, Antigravity), so those tests failed with "does not produce a common transcript". They now create a real headless Claude agent with an initial message and wait for its transcript before exercising `mngr transcript`. Added a `setup_claude_trust` helper to the e2e session, added per-test `@pytest.mark.timeout(300)` markers, removed the spurious `@pytest.mark.modal` mark (these tests run entirely on a local agent), and added an assertion that the JSONL output parses and contains the sent user message.

- Fix the `test_transcript_tail_one` e2e tutorial test so it actually exercises `mngr transcript my-task --tail 1`. The test previously created a `command`-type agent (a `sleep` stand-in), but `command` agents legitimately produce no common transcript, so `mngr transcript` always exited non-zero. The test now materializes a stopped `claude` agent together with its `claude/common_transcript` events file on disk and asserts that `--tail 1` shows exactly the most recent message. Its `@pytest.mark.modal`, `@pytest.mark.tmux`, and `@pytest.mark.rsync` marks were dropped because the test no longer creates an agent and therefore never touches those resources (the resource guard flags such superfluous marks).
- Add `test_transcript_tail_one_on_command_agent_errors`, an error-path test for the same tutorial block that confirms `mngr transcript --tail 1` on a `command` agent fails with a clear "does not produce a common transcript" message.

Fixed the `test_troubleshoot_check_agent_state` e2e tutorial test: added the
`@pytest.mark.timeout(120)` override that every other create-based e2e tutorial
test uses (the global 10s function-body timeout was killing the create + list)
and removed the spurious `@pytest.mark.modal` mark, since the test only creates a
local command agent and never exercises Modal. Also strengthened the assertions to
verify the just-created agent actually appears in `mngr list` output with its
resolved `local` provider, rather than only checking the command's exit code.

- Fixed the `test_troubleshoot_follow_events` e2e release test, which was failing reliably. It inherited the global 10s per-test timeout, but each `mngr` subprocess invocation costs several seconds of cold-start, so two invocations (create plus follow) could not fit. Added an explicit `@pytest.mark.timeout(120)` (matching the other e2e tutorial tests) and removed a stale `@pytest.mark.modal` mark -- the test exercises a local command agent and never invokes Modal, which the resource guard flagged once the test ran to completion.
- Strengthened the same test to bound `mngr event --follow` with a window that outlasts mngr's startup and to assert the command stays in the follow stream loop until killed (exit code 124), instead of masking the exit status with `|| true`. Added a companion unhappy-path test that verifies `mngr event <unknown-agent> --follow` fails fast with an agent-not-found error rather than hanging.

Fixed the `test_troubleshoot_gc_dry_run_then_gc` release test so it no longer
trips the default 10s pytest timeout: `mngr gc` walks every configured provider
(including Modal), which routinely takes longer than 10s. The test now carries an
explicit `@pytest.mark.timeout(120)` and gives each `mngr gc` invocation a
generous per-run timeout, matching the pattern used by the sibling
`test_troubleshoot_destroy_and_recreate_modal` test.

Fixed the host-diagnostics block in the mega tutorial. `mngr exec` takes the
command to run as a single argument (its last positional), so the previous
`mngr exec my-task -- ps aux` form parsed `ps` as a second agent name and
failed with "Agent not found". The tutorial now quotes each command, e.g.
`mngr exec my-task "ps aux"`, which also makes the `cat ... | tail -20` pipe
run on the agent's host as intended.

- Fix the `test_troubleshoot_recent_events` e2e tutorial test for `mngr event my-task --tail 20`. The test created a local `command` agent and viewed its events, so it never provisions Modal (the event-stream discovery optimization resolves the known agent to the local provider only). The spurious `@pytest.mark.modal` was therefore failing the resource guard's "marked modal but never invoked modal" check. Removed the mark and added a `@pytest.mark.timeout(120)` since the create-plus-view sequence runs longer than the default 10s per-test timeout.
- Strengthen the `mngr event` tutorial coverage: the happy-path test now asserts the printed output is well-formed JSONL (each event record is a JSON object), and a new `test_troubleshoot_recent_events_missing_agent` covers the unhappy path where viewing events for an unknown agent fails with a clear "Could not find agent" error.

Fixed the `test_troubleshoot_stop_restart` e2e tutorial test (TROUBLESHOOTING section). The test created a local command agent and ran `mngr stop` / `mngr start --connect`, but never exercised Modal, so the superfluous `@pytest.mark.modal` mark tripped the resource guard; it also exceeded the default 10s function timeout. Removed the unused `modal` mark and added `@pytest.mark.timeout(120)`. Added behavioral verification that asserts the agent's state transitions to stopped after `mngr stop` and back to running after `mngr start`.

Fixed the `test_troubleshoot_transcript_for_errors` e2e tutorial test. The
troubleshooting block substitutes a lightweight `command` agent for the
tutorial's claude agent, and `mngr transcript` correctly rejects command
agents (they produce no common transcript). The test now asserts that clear
diagnostic instead of expecting success, adds a timeout override to cover the
agent-create latency, and drops the superfluous `modal` mark (the transcript
diagnostic is a local, client-side agent-type check that never scans modal).

- Fix the `test_usage_wait_and_create` e2e tutorial test: give it a `@pytest.mark.timeout(180)` (the default 10s is far too short for `mngr usage wait`, whose first discovery poll over all providers takes ~30s), raise the command run timeout to 120s, and drop the inapplicable `@pytest.mark.modal` (the timeout-capped `usage wait` short-circuits the chained `mngr create`, so Modal is never invoked in a guard-trackable way). The test now also asserts the command exits 2 (usage wait timed out, create skipped) and that no `chore` agent was created.

## 2026-06-07

# Add a public `set_command` setter on agents

Agents now expose a certified `set_command(command)` setter (on `AgentInterface` / `BaseAgent`)
alongside the existing `get_command`, mirroring the other certified field getters/setters
(`set_labels`, `set_is_start_on_boot`, ...). It persists the agent's stored launch command through
the same atomic write + external-storage save path as the other setters. This lets callers update
the command that an agent re-runs on its next start/restart without reaching into the agent's
on-disk `data.json` directly.

Added per-agent tmux window sizing and resize policy.

- `mngr create` accepts `--tmux-width`, `--tmux-height`, and `--tmux-window-size` (`manual|latest|largest|smallest`). These set the agent's tmux window dimensions at session creation and its resize policy.
- Defaults are unchanged from before: a `200x50` window with tmux's default resize-on-attach behavior. `manual` pins the window to its configured size so it is never resized when a client attaches.
- The options are persisted on the agent (in `data.json`) and applied on every (re)start, so they survive `stop`/`start`, `clone`, `migrate`, and `snapshot`. They are provider-agnostic (local, docker, modal, remote).
- `mngr connect` skips its post-attach resize for a `manual`-window agent (decided on the remote host at attach time), so the pinned dimensions survive an interactive attach.

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

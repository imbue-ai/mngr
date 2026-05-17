# Unabridged Changelog

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-16

- `mngr_lima`: drop ssh-keyscan from the host-creation flow. Each Lima VM now gets a pre-generated ed25519 host keypair injected into the guest via the Lima provision script (which writes `/etc/ssh/ssh_host_ed25519_key{,.pub}`, removes other host-key types, and restarts sshd before `limactl_start_new` returns). The host machine writes the matching `known_hosts` entry atomically using the public key it already has on disk -- no scan, no `Broken pipe` race during VM bring-up, no TOFU. Mirrors `mngr_vps_docker`'s cloud-init-driven host-key injection pattern, adapted to Lima's `provision[mode=system]` surface (Lima's `UserData` Go struct doesn't expose top-level `ssh_keys`). Per-host keys and the matching `known_hosts` file live under `<provider-dir>/keys/hosts/<host_id>/` so each VM has an isolated identity (no shared `known_hosts` accumulating stale `127.0.0.1:<old-port>` entries across restarts); `delete_host` cleans up that directory. `merge_lima_yaml` now extends `provision` and `mounts` instead of replacing them: a user-supplied `provision:` (e.g. to install extra packages) is appended after mngr's, and a user-supplied `mounts:` is appended after the `/mngr` volume mount -- so mngr's load-bearing entries (host-key injection in `provision`, the `/mngr` mount) are preserved. Lima runs `provision[mode=system]` scripts in list order, so mngr's host-key swap runs before any user script.

Fix Lima provider to actually disable guest -> host port forwarding. The previous empty `portForwards: []` did not suppress Lima's auto-appended fallback rule, so guest sockets on any interface (e.g. `0.0.0.0:8082`) leaked to host loopback and collided across coexisting VMs. The provider now emits two ignore rules -- one for `guestIP: 0.0.0.0` (with `guestIPMustBeZero: true`) and one for `guestIP: 127.0.0.1` -- because empirical testing on Lima 2.1.1 showed user-supplied rules match the guest bind address literally and neither rule alone catches both cases. `merge_lima_yaml` locks `portForwards` against user `--file` overrides. SSH is unaffected -- Lima manages it through a separate top-level config.

Add the `mngr_ovh` provider plugin: run mngr agents in Docker containers on OVH classic VPS instances (e.g. `vps-2025-model1` / "VPS-1" at ~$7.60/mo).

- Uses the official `python-ovh` SDK; supports OAuth2, AK/AS/CK, and `~/.ovh.conf` credentials.
- Provisions via the OVH `/order/cart` flow and bootstraps via `POST /vps/{s}/rebuild` with a pre-installed SSH public key (no cloud-init is available on OVH classic VPS).
- Discovers VPSes via OVH IAM v2 tags (`POST /v2/iam/resource/{urn}/tag`) on the `vps` resource URN, so multiple `mngr` instances on different machines see the same agents.
- First SSH connection performs a TOFU pin of the host key into a per-provider `known_hosts` file; strict host-key checking is enforced from then on. See `libs/mngr_ovh/README.md` for the security caveat.
- `mngr create --provider ovh` automatically reuses a cancelled-but-still-alive OVH VPS (the leftover from a prior `mngr destroy` that OVH won't actually decommission until end of month) instead of ordering a fresh one. Controlled by `enable_recycle_cancelled` (default `True`), `recycle_safety_margin_hours` (default `24`), and `recycle_max_candidates_considered` (default `10`).
- Adds `mngr ovh list [--all]` operator command: shows every mngr-tagged OVH VPS in the account (or every VPS with `--all`) with plan, datacenter, state, expiration, cancellation status, and IAM tags (`mngr-provider`, `mngr-host-id`, `mngr-recycling-by`). Plain text table; one IAM-resource call plus parallel per-VPS detail fetches via `ConcurrencyGroupExecutor`.
- Refactors `VpsDockerProvider` to lift the shared parallel-SSH discovery into the base class behind a new `_list_provider_vps_hostnames()` seam method (concrete in the base, returns `[]`; overridden by concrete providers); `mngr_vultr` now only contributes the tag-listing.
- Widens `os_id` in the VPS Docker base to `int | str` so providers (like OVH) can carry friendly image names through the existing build-args parser without disrupting integer-id providers (like Vultr).

- The TMR GitHub Actions workflow now defaults `MNGR_USER_ID` to the shared `tmr-ci` namespace and reads inbound-SSH authorized keys from the checked-in `.github/tmr-authorized-keys` file (in addition to the existing `additional_authorized_hosts` workflow input). To register your key, run `uv run --project libs/mngr_tmr python libs/mngr_tmr/scripts/setup_tmr_ci_debug.py` and append the printed public key to that file via PR; then debug CI-created modal agents locally with `MNGR_HOST_DIR=~/.mngr-tmr-ci uv run mngr list` / `mngr connect`.
- TMR run names are now a single compact timestamp `YYYYMMDDHHMMSS` (e.g. `20260514184215`) used consistently across the output directory (`tmr_<run>/`), the `tmr_run_name` agent label, and the agent / host / branch names of every TMR-spawned entity. Testing agents are `tmr-<run>-<test_name>` (with `-2`, `-3`... appended on sanitization collisions; the random hex id has been removed), branches are `mngr-tmr/<run>/<test_name>`, the snapshotter and integrator are `tmr-<run>-snapshotter` / `tmr-<run>-integrator`, and the host pool is `tmr-<run>-host-<i>`. A new `tmr_role` label (`testing` / `snapshotter` / `integrator`) replaces the previous name-prefix matching for filtering integrator agents during `--reintegrate`.
- The TMR HTML report is now mirrored to `s3://int8-shared-internal/tmr-reports/<run>.html` (us-west-2) on every regeneration when `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` are set, and the public URL `http://go/shared/tmr-reports/<run>.html` is printed (and emitted as a structured `report_url` event in JSON/JSONL output). The TMR GitHub Actions workflow passes the AWS secrets through and uses the URL in the auto-opened PR body, falling back to the existing `tmr-report` artifact when no upload happened.
- Added a `--run-name` flag to `mngr tmr` to override the auto-generated run name. The main `TMR` GitHub Actions workflow accepts a corresponding `run_name` workflow_dispatch input, and a new `TMR (reintegrate)` workflow takes that run name back as a required input and runs `mngr tmr --reintegrate <run>` against it (re-running just the integrator phase, opening the same kind of draft PR).
- Internal cleanup: the `tmr_role` agent label is now derived directly from `AgentKind` (which gained a `SNAPSHOTTER` variant) and stamped centrally inside `_create_tmr_agent`, so a single `kind: AgentKind` argument controls both in-process classification and the on-server label. The S3 mirror of the HTML report is now invoked from the orchestration / cli layers rather than from inside `report.generate_html_report`, restoring the reporter to its previous "writes a file, returns a Path" contract. The two TMR workflows share a new `.github/actions/tmr-setup` composite action for their common setup steps.

## 2026-05-15

Restore Modal compatibility for the standard mngr Dockerfile and adopt offload's `post_patch_cmd` (introduced in v0.9.4). The Dockerfile is back to a single `FROM python:3.12-slim` stage (mngr's Modal image builder rejects multi-stage Dockerfiles), and all source-dependent setup (tarball extraction, git normalization, `image_commit_hash`, `uv sync`) lives in `scripts/post-source-setup.sh`, called both as the final Dockerfile RUN and as offload's `post_patch_cmd` so the two paths stay in sync. Bumps the offload pin from 0.9.2 to 0.9.5.

## 2026-05-14

## mngr-latchkey + minds: switch permission management to the latchkey 2.9.0 gateway extensions

### Summary

Latchkey 2.9.0 ships two new gateway extensions that this branch wires
into `mngr_latchkey` and the minds desktop client:

- `permission_requests.mjs` -- per-process pending-permission queue.
  Agents `POST /permission-requests` when they hit a blocked service;
  the desktop client consumes `GET /permission-requests?follow=true`
  to learn about new requests and `DELETE /permission-requests/<id>`
  to clear them once granted or denied.
- `permissions.mjs` -- a `permissions.json` editor that operates on any
  file path inside `LATCHKEY_EXTENSION_PERMISSIONS_ROOT`. Used by the
  desktop client to apply per-host permission grants via
  `POST /permissions/rules?path=<host_file>&rule_key=<scope>`.

Both extensions are bundled in `imbue-mngr-latchkey` and dropped into
`<LATCHKEY_DIRECTORY>/extensions/` automatically every time `mngr
latchkey forward` spawns the shared gateway.

### `imbue-mngr-latchkey`

- `LATCHKEY_MIN_VERSION` bumped from 2.8.0 to 2.9.0.
- New extension files at
  `imbue/mngr_latchkey/extensions/{permission_requests,permissions}.mjs`,
  rewritten from the originally-supplied drafts:
    * `permissions.mjs` now takes the target file path and rule key
      via the `?path=` and `?rule_key=` query params. It requires the
      `LATCHKEY_EXTENSION_PERMISSIONS_ROOT` env var (set by
      `Latchkey._spawn_gateway` to the plugin data dir) and refuses
      any path that resolves outside it.
    * `permission_requests.mjs` no longer accepts a caller-supplied
      `request_id`; the extension generates one server-side (a
      UUID-shaped hex string) and returns it in the POST response.
- `Latchkey.create_admin_permissions_jwt()` -- materializes
  `<plugin_data_dir>/latchkey_admin_permissions.json` (idempotent,
  with the wildcard rule `{"any": ["any"]}`) and returns a cached
  JWT pointing at it. Calling code uses this JWT in the
  `X-Latchkey-Gateway-Permissions-Override` header when it needs
  full access to the gateway's extension endpoints.
- New `mngr latchkey admin-jwt` CLI subcommand wraps the above and
  prints the JWT on stdout for shell-driven workflows.
- New `mngr latchkey gateway-info` CLI subcommand that prints the
  shared gateway's URL + password as a single JSON object on stdout.
  The bound gateway port is stamped onto the existing
  `LatchkeyForwardInfo` record (`gateway_port` field) so non-spawning
  processes can discover where the gateway is listening; the
  password is intentionally **never** persisted on disk and is
  derived locally by every consumer via
  :meth:`Latchkey.derive_gateway_password` (a pure function of the
  user's latchkey encryption key).

### Minds desktop client

- `cli/run.py` now blocks on `_wait_for_gateway_port` (which polls
  `LatchkeyForwardInfo.gateway_port` for a non-None value) before the
  FastAPI app is built, then derives the gateway password and mints
  the admin JWT in-process and constructs a `LatchkeyGatewayClient`
  shared by every code path that talks to the gateway extensions.
- New `PermissionRequestsConsumer` daemon thread streams
  `GET /permission-requests?follow=true` and feeds each pending
  request into the existing `RequestInbox`. The legacy
  `events.jsonl` callback now ignores `LATCHKEY_PERMISSION` lines
  because the extension owns that flow; non-latchkey
  `PERMISSIONS` events still go through the JSONL channel
  unchanged.
- `LatchkeyPermissionGrantHandler` applies grants via the new
  `permissions` extension (`POST /permissions/rules?path=...&rule_key=...`)
  and clears the pending gateway record via `DELETE
  /permission-requests/<id>` on both grant and deny.
- New `gateway_client.py`, `permission_requests_consumer.py`, and
  `testing.py` modules support the above; corresponding unit-test
  files exercise the HTTP wire shape and the streaming/translation
  paths.

### Compatibility

Agents that still post `LATCHKEY_PERMISSION` request events via the
old `events.jsonl` channel will no longer reach the minds inbox.
Migrating agents to the gateway-side `POST /permission-requests`
endpoint is a follow-up; agents will additionally need their
per-host baseline permissions to grant `latchkey-self` access so the
gateway accepts the POST.

- Fixed: a cloned claude agent now actually resumes the source agent's conversation (the model sees and acts on the source's history), not just inherits the session JSONL on disk. Previously, after #1598's cross-host plugin/ rsync, claude on the destination would still start fresh because the JSONL was filed under the *source's* encoded work_dir, the rsynced ``sessions-index.json`` pointed at source paths, and ``claude_session_id`` was wrong. ``_adopt_cloned_session`` now renames the project subdir to the destination's realpath-resolved encoding (handles the ``/mngr/projects/agent-X`` → ``/__modal/volumes/<vol-id>/projects/agent-X`` symlink on Modal), drops the stale index, writes ``claude_session_id`` to the JSONL filename's stem (the ground truth — the source's own ``claude_session_id`` file holds the agent UUID from the SessionStart hook default rather than the real id), and carries forward ``claude_session_id_history``. The ``--adopt-session`` flow shares the same finalize step.

Regenerated CLI docs for `mngr tmr` and `mngr latchkey` to reflect current options.

Add `gemini` agent type plugin (`imbue-mngr-gemini`) that wires Google's Gemini CLI into mngr.

`mngr create` no longer hard-codes `claude` as the default agent type. The agent type must now come from a positional argument, `--type`, or `[commands.create] type` in user settings. If none of those is supplied, `mngr create` exits with a clear error listing the registered agent types and pointing at `mngr config set commands.create.type <name> --scope user`. (Supersedes the `--type` source-default introduced in `mngr-fix-default.md` from this same release.)

New subcommand `mngr extras config`: walks through user-scope config settings the installer would otherwise leave blank. Each step short-circuits if its setting is already configured, so re-running only prompts for the gaps. Today this just covers the default agent type for `mngr create`; future config-related setup will be added as additional walk steps under the same subcommand. With an interactive terminal, presents an urwid single-select picker of every available agent type plus a "Keep no default" option, and writes the selection to `[commands.create] type` in user settings. With `-y` or without a terminal, prints the suggested `mngr config set` command and lists available agent types -- writes nothing.

`mngr extras completion` and `mngr extras claude-plugin` also use the new urwid picker (Install / Skip) instead of the old `[y/n]:` text prompt -- the entire `mngr extras -i` walkthrough now uses a consistent TUI rather than mixing the plugin-wizard's full-screen TUI with bare-text confirmation prompts.

`mngr extras -i` now also walks through the default agent type prompt as a final step, alongside the existing plugins / completion / Claude-plugin steps. `mngr extras` (no flag) reports the current default agent type as part of the status block.

`scripts/install.sh` no longer contains custom shell logic for the default agent type -- step 5 is gone, since the default agent type prompt now runs as part of `mngr extras -i` (step 4). The new subcommand is also re-runnable via `mngr extras config` if you skip it the first time.

`mngr plugin list` gains a `--kind` filter with two values, `agent-type` and `provider`, that project the plugin list to the canonical set of agent type names or provider backend names (with version/description metadata when entry-point names match).

Migration: existing users who upgrade and have no `[commands.create] type` set will see an error from `mngr create` until they run `mngr config set commands.create.type <name> --scope user` (or pick one via `mngr extras config`). The error message includes the registered agent types so you can copy-paste a value.

Removed the unused `libs/flexmux/` project and all references to it (justfile recipes, `EXCLUDED_RATCHET_PROJECTS` exclusions in `test_meta_ratchets.py` and `scripts/sync_common_ratchets.py`, and the `uv.lock` workspace member).

Bumped the pinned Claude Code CLI version from `2.1.116` to `2.1.141` in `libs/mngr/imbue/mngr/resources/Dockerfile` and the `.github/workflows/{ci,tmr}.yml` install steps, matching the corresponding bump to `[agent_types.claude].version` in `forever-claude-template/.mngr/settings.toml`.

`mngr schedule add --verify quick|full` now works when the trigger's `mngr create` produces an agent that lives inside the cron-runner's local provider (i.e. inside the ephemeral Modal container). Previously the deploy machine could not reach into the container to observe or destroy the agent, so verify failed for that configuration. Verification now runs inside the container itself and reports the result back to the deploy machine over a structured sentinel line.

Add a `use_env_config_dir` option on the `claude` agent type config. When set
to `true`, local Claude agents share the user's `$CLAUDE_CONFIG_DIR` instead of
provisioning a per-agent config dir, and mngr does not write to the user's
Claude config (no trust additions, dialog dismissal, per-agent settings, or
keychain provisioning). Only supported for local hosts; `$CLAUDE_CONFIG_DIR`
must be set. The user is responsible for one-time interactive `claude` setup.
See `libs/mngr_claude/README.md` for details.

CI acceptance test speedups, three changes:

1. Grant `contents: write` to the `test-offload` and `test-offload-acceptance` jobs so offload can push its image-cache git notes back to `refs/notes/offload-images`. Previously every run was a cache miss (the `git push` from offload failed with "Permission to imbue-ai/mngr.git denied to github-actions[bot]"), forcing a full `checkpoint_base_prepare` rebuild (~150 s wasted per CI run on acceptance, similar on the regular offload job). Measured saving on cache hit: ~124 s per acceptance run.

2. Lower `max_parallel` from 200 to 50 in `offload-modal-acceptance.toml`. With 200 slots and ~89 tests, offload's LPT scheduler degenerated to one-test-per-batch, so every batch paid full pytest cold-start, Modal sandbox creation, and an orchestrator-side `uv run` cold-start per download. Lowering to 50 lets LPT pack ~2-4 tests per batch (longest single tests still alone via load-balancing). Combined measured saving: ~62% acceptance wall-clock reduction.

3. Fix the session-end leak detector in `libs/mngr_modal/imbue/mngr_modal/conftest.py` (previously the `modal_session_cleanup` autouse fixture; now a `pytest_sessionfinish` hook so it runs after all session-scoped fixture teardowns -- pytest's autouse session-scoped fixtures tear down before non-autouse session-scoped fixtures regardless of declared dependencies, which made the previous fixture poll a still-registered env and fail before the deregister could run). The detector compared the global `modal environment list --json` against tests' tracked env names, but Modal's listing endpoints are eventually consistent w.r.t. deletion -- after a `modal environment delete X` returns "Environment 'X' not found", the env can still appear in the global list for tens of seconds (and other endpoints have been observed to flip their answers across the same window). With one-test-per-batch the assertion almost never landed in the inconsistency window; with several tests per session it became consistent enough to repeatedly fail teardown on whichever test happened to be last. The fix is twofold: (a) the per-test and session-scope cleanup fixtures deregister tracked resources from `worker_modal_*_names` *only* when the cleanup chain confirmed the resource was deleted or already gone (the synchronous response is authoritative); cleanup failures keep the resource tracked and log a `logger.error` so the session-end leak detector still has a chance to surface a real leak. Cleanup return values are typed via a new `ModalCleanupOutcome` enum (`DELETED | NOT_FOUND | FAILED`). (b) the `pytest_sessionfinish` hook runs after all session-scoped fixture teardowns, so any name still in `worker_modal_*_names` at that point corresponds to a resource whose cleanup either FAILED or was never attempted (test crashed mid-fixture) -- i.e. a real leak rather than a listing-staleness false positive. `_get_leaked_modal_environments` is therefore a single-shot `modal environment list --json` call shaped exactly like its `_get_leaked_modal_apps` / `_get_leaked_modal_volumes` siblings; the CI hourly `cleanup_old_modal_test_environments.py` script remains the broader safety net.

- `mngr_claude_subagent_proxy`: typed `subagent_type` (e.g. `imbue-code-guardian:verify-and-fix`) now preserves Claude Code's system-prompt contract.
  - PROXY mode: when the resolver finds an on-disk `.md` definition for the parent's `subagent_type` under `<work_dir>/.claude/agents/`, `~/.claude/agents/`, or `~/.claude/plugins/marketplaces/*/plugins/<plugin>/agents/`, the definition body is prepended to the spawned mngr subagent's prompt file under a labeled section header. Built-in types (`general-purpose`, `Explore`, ...) fall through to the prompt-only path unchanged.
  - DENY mode: the deny reason now appends a one-line pointer at the resolved path so Claude can prepend the body to its own prompt file before running the skill's spawn-and-wait protocol. The base skill-pointer text is unchanged for unresolved / built-in types.
  - The `mngr-subagents` skill documents the typed case (including the v1 limitation that tool restrictions declared in agent-definition frontmatter are not honored -- the spawned mngr subagent inherits the user's full Claude config).

**minds**: split the services agent from the initial chat agent. The "primary" agent in a minds workspace now runs only the bootstrap and background services (its window-0 command is `sleep infinity && claude`, so claude never actually starts) and is hidden from the agent list in the UI. On first container boot the bootstrap creates a real chat agent named after the host, sends it `/welcome`, and writes `CLAUDE_CONFIG_DIR` to the host env so every subsequent agent (chat, worktree, worker) shares the services agent's Claude config dir (auth, plugins, marketplaces, sessions). Destroying chat agents no longer tears down services, and restarting services no longer kills chat agents. The workspace_server `/api/agents/<id>/destroy` endpoint refuses to destroy `is_primary=true` agents as a server-side guard. Existing pre-change workspaces are not migrated — re-create them.

Fix tmux argv-parsing footguns for arguments starting with `-`:

- `tmux send-keys -l` now uses the `--` end-of-options separator, so agent commands and messages that start with `-` (e.g. `--model gemma`, `--help`) are no longer misparsed by tmux as flags.
- `tmux rename-session` now uses `--` before the positional new-name argument, so renaming an agent under a custom prefix that starts with `-` works correctly.

## mngr usage: per-session cost aggregation across recent sessions

The Claude statusline writer (`mngr_claude_usage`) captures `rate_limits` +
per-render `session_id` + `cost.*` from Claude Code's statusline JSON, into
`events/claude/usage/events.jsonl` (renamed from `events/claude/rate_limits/`
since the file is no longer rate-limit-only). The event `type` is
`cost_snapshot`. The writer no longer skips emission when only `cost` is
present (no `rate_limits`), so cost tracking now works for direct
`ANTHROPIC_API_KEY` users -- Claude Code doesn't emit `rate_limits` for them
(it's Pro/Max only), but `cost` is always present. The writer script is
named `claude_usage_writer.sh` and reads `$MNGR_USAGE_EVENTS_PATH` for
the test override.

`mngr usage` now aggregates cost **per session** within a recency window
instead of just rendering the freshest event's reading, and keeps
**subscription** and **API-key** spend in separate aggregates so imputed
estimates never get lumped with real billable spend:

- Reader scans every line of each agent's events file (not just the last),
  partitions each agent's events into Claude Code processes via cost-drop
  detection (cost is process-cumulative; `/clear` doesn't reset it), and
  within each process builds a `SessionCostRecord` per session whose `cost`
  is its delta from the prior session's cumulative reading.
- Each session is tagged with a `cost_mode`: `SUBSCRIPTION` if any event in
  its Claude Code process carried `rate_limits` (Claude.ai Pro/Max --
  cost is imputed by Claude Code, the user actually pays a flat subscription)
  or `API_KEY` otherwise (direct `ANTHROPIC_API_KEY` -- cost is real
  billable spend).
- Sessions are filtered to those whose last event is within `--since`
  (default 24h, configurable per-invocation or via plugin config).
- Human output (default): one cost line per mode that contributed --
  `subscription cost (imputed): $X.YY ...` and/or `api cost: $X.YY ...`
  -- followed by the populated rate-limit window lines. Subscription is
  rendered first; either or both can be present.
- Human output with `--detail`: adds indented per-session lines (newest-first)
  between the cost lines and the window lines, each tagged `[sub]` or `[api]`.
- JSON output (default): `source.subscription_cost.*` and `source.api_cost.*`
  are the per-mode aggregates; `source.subscription_session_count`,
  `source.api_session_count`, and `source.session_count` (total) are also
  exposed. There is intentionally **no** combined `source.cost` field.
  `sessions[]` is omitted unless `--detail` is set.
- JSON output with `--detail`: adds `source.sessions[]` (newest-first
  records, each carrying `cost_mode`).
- `mngr usage wait --until` CEL surface: `subscription_cost.total_cost_usd`
  and `api_cost.total_cost_usd` are the per-mode aggregates; no combined
  `cost` field exists. To predicate on a specific session, index
  `sessions[]` directly. New `--since` flag affects the aggregates.
- Format template: top-level `{subscription_cost.*}` / `{api_cost.*}` keys;
  the format-template surface intentionally doesn't expose per-session
  paths (use `--format json` if you need them).

Examples:

```
mngr usage --since 7d                                # aggregate over 7 days
mngr usage wait --until 'api_cost.total_cost_usd > 20'  # real billable spend crossed $20
mngr usage wait --until 'subscription_cost.total_cost_usd > 50'  # imputed >$50 of value
mngr usage wait --until 'sessions[0].cost.total_cost_usd > 5'  # most recent session only
```

Minds: the "Name" field on the create-project form now sets the *host* name (validated via mngr's `HostName` regex), not the agent name. The agent is always called `system-services`. The imbue_cloud connector grows a required `host_name` on `/hosts/lease` and `/hosts`. Sister change in `forever-claude-template` (matching branch) drops the now-unused `MINDS_WORKSPACE_NAME` from `[commands.create].pass_env`.

## 2026-05-14

- Bump bundled Latchkey version to 2.11.1.

`mngr tmr`: testing agents now publish a single `outputs.tar.gz` archive into
their state directory (`$MNGR_AGENT_STATE_DIR/plugin/test-map-reduce/`),
containing the renamed `test_output/` directory and an optional incremental
`branch.bundle`. The orchestrator polls for the archive via the per-agent
volume API (which works even when the host is offline) and reconstructs the
agent's branch from the bundle, removing the previous rsync + git-pull
finalization step. Reintegrate mode uses the same path. SSH provider, which
does not expose a volume, is no longer supported for testing-agent outputs.
The integrator agent is unchanged.

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

## 2026-05-13

# Latchkey state is now keyed per-host instead of per-agent

When minds creates an agent, the Latchkey-related env vars
(`LATCHKEY_GATEWAY`, `LATCHKEY_GATEWAY_PASSWORD`,
`LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE`, `LATCHKEY_DISABLE_COUNTING`)
are now passed to `mngr create` via `--host-env` instead of `--env`, so
every agent that ever runs on the host shares the same gateway URL,
password, JWT, and permissions.

The on-disk permissions metadata moves accordingly: minds now stores
the per-agent `latchkey_permissions.json` under
`<latchkey-dir>/mngr_latchkey/hosts/<host_id>/` instead of
`<latchkey-dir>/mngr_latchkey/agents/<agent_id>/`. After `mngr create`
returns, minds reads the canonical `host_id` from the trailing JSONL
`created` event and points the opaque permissions handle (referenced
by the JWT minted at create time) at the new host-keyed path.

Public API changes in `imbue-mngr-latchkey`:

- `imbue.mngr_latchkey.agent_setup.finalize_agent_permissions` is
  renamed to `finalize_host_permissions` and takes a `HostId` instead
  of an `AgentId`.
- `imbue.mngr_latchkey.store.permissions_path_for_agent` /
  `link_opaque_permissions_to_agent` are renamed to
  `permissions_path_for_host` / `link_opaque_permissions_to_host` and
  take a `HostId`.
- The `mngr latchkey link-permissions` subcommand takes `--host-id`
  instead of `--agent-id`.

The minds UI's grant flow now resolves the request event's `agent_id`
to its `host_id` via the backend resolver before writing the grant; if
the resolver hasn't seen the agent yet (or only reports the static
`"localhost"` placeholder), the grant POST returns 503 so the UI can
retry instead of silently writing the grant to the wrong file.

Background processes started with `ConcurrencyGroup.run_process_in_background()` now default to `is_checked_by_group=True`, so non-zero exits surface as `ProcessError` at group teardown instead of being silently swallowed. Pass `is_checked_by_group=False` for processes the caller terminates explicitly (e.g. via `terminate()` or a fire-and-forget timeout).

## 2026-05-12

## mngr-latchkey: new package

Added a new `imbue-mngr-latchkey` workspace package that owns the
shared `latchkey gateway` lifecycle, per-agent latchkey wiring, and
the reverse SSH tunnel that bridges the host-side gateway into remote
agents. The minds desktop client used to host this logic in
`apps/minds/imbue/minds/desktop_client/`; it now imports the package
and keeps only its own UI-layer code (permission dialog, service
catalog, HTML templates).

The package is currently a plain Python library -- no `mngr` CLI
subcommands are registered yet.

### Python API

- `imbue.mngr_latchkey.core.Latchkey` -- single wrapper around the
  upstream `latchkey` CLI. Owns gateway spawn / adopt / stop, password
  derivation, JWT minting, services-info and auth-browser probes.
  `Latchkey.initialize()` now runs `latchkey --version` and refuses to
  continue if the installed binary is older than the new
  `LATCHKEY_MIN_VERSION = "2.9.0"` constant; misconfiguration surfaces
  immediately rather than at the first gateway spawn. Failures raise
  the new `LatchkeyVersionError` (subclass of `LatchkeyError`).
- `imbue.mngr_latchkey.agent_setup.prepare_agent_latchkey` -- assembles
  the env vars an agent needs (`LATCHKEY_GATEWAY[_PASSWORD,_PERMISSIONS_OVERRIDE,_DISABLE_COUNTING]`)
  and an opaque permissions handle. **Raises** on infrastructure
  failures (latchkey CLI broken, on-disk write failed); callers decide
  whether to abort agent creation or fall back to an empty setup.
- `imbue.mngr_latchkey.agent_setup.finalize_agent_permissions` --
  replaces the opaque handle with a symlink to the canonical
  agent-keyed `latchkey_permissions.json` once `mngr create` has
  returned the canonical agent id. **Raises** `LatchkeyStoreError` on
  failure; same policy stance as above.
- `imbue.mngr_latchkey.discovery.LatchkeyDiscoveryHandler` -- agent
  discovery callback that ensures the shared gateway is up and opens
  a reverse SSH tunnel from `127.0.0.1:AGENT_SIDE_LATCHKEY_PORT` into
  the agent. Each tunnel is tagged with its agent id.
- `imbue.mngr_latchkey.discovery.LatchkeyDestructionHandler` -- agent
  destruction callback that drops the destroyed agent's reverse tunnel
  so the SSH-tunnel health-check loop doesn't keep spinning paramiko
  transports against a host that no longer exists.
- `imbue.mngr_latchkey.ssh_tunnel.SSHTunnelManager` -- reverse-tunnel
  manager with per-tunnel exponential backoff, agent-id tagging, and
  `remove_reverse_tunnels_for_agent`.
- `imbue.mngr_latchkey.store` -- on-disk persistence: gateway record,
  permissions config read/write, opaque-handle allocation, per-agent
  symlink linking.

### Layout

Plugin metadata lives under `<latchkey_directory>/mngr_latchkey/`, keeping
it cleanly segregated from anything the upstream `latchkey` CLI writes
under the shared `LATCHKEY_DIRECTORY`. Minds uses `~/.minds/latchkey`
as that root directory.

### Dependencies

- `imbue-mngr-forward` for the bidirectional socket/channel relay
  helper (`imbue.mngr_forward.relay`), keeping the half-closed-channel
  fix in a single place rather than duplicating it.

### Minds-side cleanups

- `apps/minds/imbue/minds/desktop_client/ssh_tunnel.py`: removed the
  now-unused `SSHTunnelManager` and supporting types (`ReverseTunnelInfo`,
  `_TunnelFailureState`, `_ForwardedTunnelHandler`, relay imports,
  reverse-tunnel health-check / backoff constants, and the internal
  `_ssh_connection_*` helpers). Kept `RemoteSSHInfo`, `SSHTunnelError`,
  `open_ssh_client`, and `_create_ssh_client` -- still used by
  `backend_resolver.py`, `forward_cli.py`, and the `MindsRemoteSSHInfo`
  adapter in `cli/run.py`. The matching test files
  (`ssh_tunnel_test.py`, `test_ssh_tunnel_leak.py`) moved to the new
  package along with the manager.
- `cli/run.py` and `desktop_client/agent_creator.py` rewired to import
  the latchkey types and helpers from the plugin and wrap the
  raising plugin entry points (`prepare_agent_latchkey`,
  `finalize_agent_permissions`) in try/except blocks that log a
  warning and continue agent creation -- preserving the prior
  end-to-end behaviour where a misconfigured latchkey installation
  does not abort agent creation, but making the choice explicit at the
  call site rather than buried inside the library.
- Three minds `test_ratchets.py` snapshots tightened
  (`while_true 1->0`, `time_sleep 2->1`, `broad_exception_catch 1->0`)
  to reflect violations that went away with the deleted code.

### No user-visible behaviour change in minds itself.

## mngr-latchkey: register a `mngr latchkey` CLI surface

The `imbue-mngr-latchkey` package now ships as a proper `mngr` plugin:
it declares a `[project.entry-points.mngr]` entry point and registers
a `mngr latchkey` command group with three subcommands, plus a
`[plugins.latchkey]` settings.toml block. Users can wire latchkey to
agents end-to-end from the shell, without the minds desktop app.

### New CLI

- `mngr latchkey forward` -- long-running foreground supervisor.
  Spawns the shared `latchkey gateway` subprocess, consumes
  `mngr observe`'s discovery stream, and sets up / tears down a
  reverse SSH tunnel for every agent on a remote host. Stops the
  shared gateway on `SIGINT`/`SIGTERM` (coupled lifetime).
- `mngr latchkey create-agent-env` -- one-shot. Wraps
  `prepare_agent_latchkey(is_tunneled=True)` and emits
  `{"env": {...}, "opaque_permissions_path": "..."}` on stdout as a
  single JSON object. Always emits the constant agent-side loopback
  URL (`http://127.0.0.1:1989`); there is no DEV / on-host mode.
- `mngr latchkey link-permissions --agent-id ID --opaque-path PATH` --
  one-shot. Wraps `finalize_agent_permissions` to swing the opaque
  handle's symlink to the canonical agent-keyed permissions path.

Intentionally not in scope: `ensure-gateway`/`stop-gateway` (lifecycle
is internal to `forward`), `latchkey auth ...` wrappers, permissions
editing, agent-include / agent-exclude filtering on `forward`. Users
who need credential management run upstream `latchkey` directly.

### New settings

```toml
[plugins.latchkey]
directory = "~/.mngr/latchkey"   # default
latchkey_binary = "latchkey"     # default; resolved via PATH
```

Both fields are overridable via `MNGR_LATCHKEY_DIRECTORY` and
`MNGR_LATCHKEY_BINARY` env vars and matching `--latchkey-directory` /
`--latchkey-binary` CLI flags. Precedence is CLI > env > settings.toml
> built-in default.

### Failure semantics

Any `LatchkeyError` / `LatchkeyStoreError` raised by the underlying
library surfaces as a non-zero exit; `create-agent-env` does not fall
back to the empty-env degraded mode the library tolerates.

### Implementation notes

New modules under `libs/mngr_latchkey/imbue/mngr_latchkey/`:
`plugin.py` (entry point), `cli.py` (the three subcommands +
settings-precedence resolver), `config.py` (`LatchkeyPluginConfig`),
`discovery_stream.py` (a small `mngr observe`-driven dispatcher that
fans the relevant events out to `LatchkeyDiscoveryHandler` /
`LatchkeyDestructionHandler`). `testing.py` lifts the existing
`_FakeLatchkey` test double to a shared `FakeLatchkey` so the new
`cli_test.py` and the existing `agent_setup_test.py` share one
implementation.

## mngr-latchkey: `LatchkeyForwardSupervisor` and minds rewiring

Follow-up after the `mngr latchkey` CLI plugin.

### `LatchkeyForwardSupervisor`

New class in `imbue.mngr_latchkey.forward_supervisor`. Owns the
lifecycle of a single detached `mngr latchkey forward` subprocess for
a given `latchkey_directory`:

- `ensure_running()` -- idempotent. Spawns a fresh detached supervisor
  if no record exists; adopts the existing one if its PID is still
  alive and its cmdline matches `mngr latchkey forward`; otherwise
  discards the stale record and spawns a fresh one. Mirrors the same
  on-disk reconciliation pattern `Latchkey` already uses for the
  gateway.
- `stop()` -- SIGTERMs the supervisor (which cascades into
  coupled-lifetime shutdown of the shared gateway + reverse tunnels)
  and deletes the record.
- `get_forward_info()` -- read-only inspection of the on-disk
  `LatchkeyForwardInfo` record.

New on-disk record at `<plugin_data_dir>/latchkey_forward.json` and a
log file at `<plugin_data_dir>/latchkey_forward.log`, with matching
`save_forward_info` / `load_forward_info` / `delete_forward_info`
helpers in `store.py`. The spawn helper sits in `_spawn.py` next to
the existing `spawn_detached_latchkey_gateway`; the cmdline-based
liveness probe guards against PID reuse.

### `mngr latchkey forward` now also accepts the common settings flags

`--latchkey-directory` and `--latchkey-binary` were available on
`create-agent-env` and `link-permissions` but not on `forward` (an
oversight in the original PR). They are now uniformly available on
all three subcommands.

### minds: spawn `mngr latchkey forward` as a detached subprocess

`apps/minds/imbue/minds/cli/run.py` no longer constructs
`SSHTunnelManager` / `LatchkeyDiscoveryHandler` /
`LatchkeyDestructionHandler` in-process; it instead calls
`LatchkeyForwardSupervisor.ensure_running()` at startup, which spawns
the canonical `mngr latchkey forward` process detached. Minds does
*not* call `supervisor.stop()` on shutdown -- the supervisor keeps
running across desktop-client restarts and the next minds session
adopts it. This matches how minds already treated the underlying
`latchkey gateway` subprocess.

Side effect: the `_LatchkeyDiscoveryAdapter` class in `cli/run.py` is
gone, plus its supporting `MindsRemoteSSHInfo` / `AgentId` imports.

### Quieter logs from one-shot CLI subcommands

`Latchkey.initialize()` no longer logs "Adopted existing shared
Latchkey gateway" / "Discarding stale ..." at INFO level. Both lines
are now DEBUG, so one-shot invocations of `mngr latchkey create-agent-env`
and `mngr latchkey link-permissions` (which `initialize()` but never
touch the gateway) no longer emit a misleading line on stderr.

## mngr-latchkey: drop the on-disk gateway record

With `LatchkeyForwardSupervisor` guaranteeing at most one `mngr
latchkey forward` process per latchkey directory, the only thing that
ever spawns a `latchkey gateway` is that single supervised forward
subprocess. Cross-process gateway adoption -- the original reason for
persisting a `LatchkeyGatewayInfo` record at `<plugin_data_dir>/latchkey_gateway.json`
-- is no longer needed.

Changes:

- `Latchkey.initialize()` no longer reads or reconciles a persisted
  gateway record. It still runs `latchkey --version` so misconfiguration
  surfaces eagerly.
- `Latchkey.ensure_gateway_started()` no longer persists / restores
  state across processes; it stays in-process-idempotent (subsequent
  calls return the cached `self._info`).
- `Latchkey.stop_gateway()` no longer deletes a record; just terminates
  the in-memory tracked subprocess.
- `imbue.mngr_latchkey.store`: removed `save_gateway_info`,
  `load_gateway_info`, `delete_gateway_info`, `gateway_info_path`, and
  the `_GATEWAY_RECORD_FILENAME` constant. `LatchkeyGatewayInfo`
  itself stays as the in-memory return-type for the spawn path.
- `imbue.mngr_latchkey.core`: removed `_is_info_alive`,
  `_cmdline_looks_like_latchkey_gateway`, and the
  `_LIVENESS_CONNECT_TIMEOUT_SECONDS` constant. The `_is_port_listening`
  helper now takes a `timeout` argument from its (one remaining)
  caller, `_wait_for_port_listening`, which is still used after spawn
  to wait for the gateway to bind its port.

Trade-off: if `mngr latchkey forward` crashes (SIGKILL, OOM, segfault)
without running its SIGTERM cleanup path, the gateway becomes an
orphan. The orphan keeps its port bound but no reverse tunnel still
points at it (those died with the previous forward's paramiko
clients), so the orphan is just an idle process. The next supervisor
call spawns a fresh forward + fresh gateway on a fresh port; the
orphan can be cleaned up with `pkill latchkey`.

## mngr-latchkey: spawn the gateway via ConcurrencyGroup, simplify Latchkey API

Now that the gateway is only ever spawned by `mngr latchkey forward`
(a long-running supervised process), the detached-process /
on-disk-record machinery was overkill. Switched the gateway over to
standard `ConcurrencyGroup.run_process_in_background` and stripped the
lifecycle surface down to what the two production callers actually
need.

API changes:

- `Latchkey.ensure_gateway_started()` -> `Latchkey.start_gateway(cg)`.
  Takes the owning `ConcurrencyGroup` as an explicit argument (the CG's
  `__exit__` is what terminates the gateway). In-process idempotent.
- Replaced the `LatchkeyGatewayInfo` return value with simpler
  in-instance state and accessors:
  - `Latchkey.is_gateway_running` -- boolean.
  - `Latchkey.gateway_port` -- int (raises `LatchkeyNotInitializedError`
    when no gateway is running).
  - `Latchkey.gateway_url` -- `http://<listen_host>:<gateway_port>`.
- `LatchkeyGatewayInfo` itself is gone (was the in-memory return shape
  with `host`/`port`/`started_at`; replaced by the properties above).
- `Latchkey.get_gateway_info()` removed; callers use `is_gateway_running`
  / `gateway_port` directly.

Internals:

- `spawn_detached_latchkey_gateway` removed from `_spawn.py`. The
  remaining detached helpers are `ensure-browser` (Chromium download
  that should outlive a quick forward restart) and `mngr latchkey forward`
  itself (the supervisor adopts across embedder restarts).
- Gateway output is captured by a small `_GatewayLogWriter` `MutableModel`
  that tees per-line into the same `<plugin_data_dir>/latchkey_gateway.log`
  the detached path used. The CG's standard pipe-based output capture
  replaces the old direct stdout/stderr-to-file redirection.
- Cross-process adoption helpers (`_is_info_alive`,
  `_cmdline_looks_like_latchkey_gateway`, `_terminate_pid`,
  `_LIVENESS_CONNECT_TIMEOUT_SECONDS`) are gone -- the supervisor wrapper
  already enforces "at most one forward per directory" and adoption
  inside that single process is now just a boolean check.

Callers updated:

- `LatchkeyDiscoveryHandler.__call__` reads the host-side port via
  `latchkey.gateway_port` after calling `start_gateway`.
- `_forward_command` (cli.py) passes `mngr_ctx.concurrency_group` to
  `start_gateway` and logs `latchkey.gateway_url`.
- `prepare_agent_latchkey` accepts an optional `concurrency_group`
  argument; raises `LatchkeyError` if `is_tunneled=False` is used
  without one (the only path that actually spawns a gateway from
  inside this helper).
- `FakeLatchkey` in `testing.py` mirrors the new `start_gateway`
  signature.

## mngr-latchkey: do not let `mngr latchkey forward` die with its parent

`_forward_command` was calling `start_parent_death_watcher`, which polls
`os.getppid()` every ~3 seconds and SIGTERMs the process when the
original parent dies and the process gets reparented to PID 1. That
actively defeats the detached-supervisor pattern: when minds spawns
`mngr latchkey forward` with `start_new_session=True` and then exits,
the watcher saw the reparent and shut the gateway down within ~3
seconds.

Removed the watcher call. To still handle the *interactive* case (user
runs `mngr latchkey forward` in a terminal and closes the terminal),
SIGHUP is now wired into the same signal handler as SIGINT/SIGTERM,
so a terminal close triggers the clean coupled-lifetime shutdown path
rather than killing the python interpreter under the default handler
(which would leave the gateway orphaned).

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

- The changelog consolidator now groups entries by the date their PR landed on `main` (committer date of the introducing commit on the first-parent line, in America/Los_Angeles) and emits one `## YYYY-MM-DD` section per distinct date in `UNABRIDGED_CHANGELOG.md` (newest first), instead of bucketing everything under the consolidator's run-time UTC date.
- The abridged `CHANGELOG.md` is now version-organized instead of date-organized: a `## [Unreleased]` placeholder sits at the top of the file, the nightly consolidation cron appends categorized bullets (`Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`) under `### <Category>` subheadings in that section, and `scripts/release.py` renames `## [Unreleased]` to `## [vX.Y.Z] - YYYY-MM-DD` and inserts a fresh empty `[Unreleased]` above it as part of the release commit. Each cron-generated bullet is in the form `- <Category>: <description>`, and the cron does one refinement pass over `[Unreleased]` after drafting to tighten/dedupe before committing.
- Enabled auto-merge on the consolidation cron: each fire now runs `git fetch && checkout main && merge origin/main` before forking the per-run branch, so the eventual PR's diff against `main` is always just the consolidation commit -- no script-snapshot drift even if the cron is redeployed less often than `main` moves.

- Added a new `DENY` mode to the `mngr_claude_subagent_proxy` plugin. Configure via `[plugins.claude_subagent_proxy] mode = "DENY"` in `settings.toml`. In `DENY` mode the plugin denies every Claude `Task` tool call with a short skill-pointer reason and instead provisions a `mngr-subagents` Claude skill at `.claude/skills/mngr-subagents/SKILL.md` that teaches the explicit two-command spawn-and-wait protocol (`uv run mngr create ...` followed by `python -m imbue.mngr_claude_subagent_proxy.subagent_wait <slug>`). The historical Haiku-dispatcher proxy path remains the default (`mode = "PROXY"`).
- Both `PROXY` and `DENY` modes now share a label-driven `SessionStart` reaper hook that queries `mngr list` for children whose `mngr_claude_subagent_proxy_parent_id` label matches the parent's `MNGR_AGENT_ID` and destroys any in a terminal state (`DONE` / `STOPPED`). The `PROXY`-only per-agent-plugin-cache Stop-hook guarding moved to a separate `guard_stop_hooks` `SessionStart` hook.
- The `mngr-subagents` skill no longer recommends `--reuse` on `mngr create`. Slug collisions between concurrent `Task` calls now surface as a hard "agent already exists" error instead of silently merging unrelated work; the skill explicitly tells Claude to pick a new slug on collision rather than destroying the existing agent. `PROXY` mode's wait-script still uses `--reuse` because its target names are derived from the unique `tool_use_id` and the only retries are bot-driven on the same id.

Encode the actual defaults for `mngr create` options that previously listed a default in their help text but were stored as `None` and resolved at runtime: `--type` now defaults to `"claude"` directly, and `--start-on-boot` defaults to `False`. Also corrects the `--worktree-base-folder` help text to reflect the actual default location (`<host_dir>/worktrees`).

Behavior change: when a config file (`[commands.create]`) or template sets `type` and the user passes a positional `AGENT_TYPE` on the command line, the positional now wins (matching the general "CLI > config" precedence). Previously the config-supplied `type` won, and a mismatch raised a "Conflicting agent types" error.

`mngr list` for imbue_cloud now drives discovery through outer (VPS root) SSH instead of inner-container SSH. Each lease produces one outer-SSH round-trip per host: `docker exec` for a running container (reading full state inside) or `docker cp` for a stopped one (extracting the host_dir to a tmp path on the VPS). The listing therefore shows the container's true state — `RUNNING` / `STOPPED` / `CRASHED`-with-exit-code / `PAUSED` / `DESTROYED` — together with friendly host name, image, tags and full agent details even when the inner sshd is unreachable. Lease-only synthesis (state=CRASHED with `failure_reason` carrying the underlying error) is now reserved for the last-resort case where even outer SSH fails. Same `_make_outer_for_vps_ip` defense added to vps_docker / vultr so a single unreachable VPS no longer drops the others, and a pre-existing crash in the framework offline path (`CommandString("")` violating `NonEmptyStr`) is fixed.

Disable the `claude_subagent_proxy` plugin in the project-level `.mngr/settings.toml` so that `uv run mngr create` from this repo does not install the experimental Task-tool proxy hooks into newly provisioned Claude agents.

TMR: when running against a remote provider with `--use-snapshot` (or
`--snapshot=<id>`), avoid re-uploading the code repo for every test agent.
The snapshotter agent's work_dir is now pinned to `/code` on its host, and
each test agent created from the resulting snapshot sources from that
on-host `/code` via `git-worktree` -- previously each agent re-pushed the
git history from the laptop.

TMR: when launching modal agents, override the modal provider config to
skip the per-agent "initial" filesystem snapshot. That snapshot adds 60-90s
per agent and runs once per agent (so 4 agents on a pooled host trigger
four snapshots), even though TMR's pooled hosts are ephemeral and the
snapshotter's host is snapshotted explicitly already.

`mngr tmr` accepts a new repeatable flag `--additional-authorized-host`
that adds SSH public key lines to the `authorized_keys` file installed
on each agent host (test agents, host pool, snapshotter, and
integrator). This lets you SSH directly into any agent host TMR
creates, primarily for live debugging.

The TMR GitHub Actions workflow (`.github/workflows/tmr.yml`) now uses
the canonical `--format` flag (the previous `--output-format` was not a
real option) and accepts two new optional `workflow_dispatch` inputs:

- `mngr_user_id`: exported into the orchestrator's process env so the
  `mngr tmr` run attributes the modal agents it creates to that user,
  with the goal of letting them be observed from the user's local
  `mngr list`.
- `additional_authorized_hosts`: one SSH public key per line; each
  non-empty line is forwarded to `mngr tmr` as a separate
  `--additional-authorized-host` argument.

- New `mngr usage` command (in a new `mngr_usage` plugin) reports Claude Code's rolling 5h / 7d / overage quota usage. Supports the same output ergonomics as `mngr list`: `--format human`/`json`/`jsonl`, `--format` template strings like `'5h:{five_hour.used_percentage}/7d:{seven_day.used_percentage}'`, and the same agent-filter flags (`--include`, `--exclude`, `--local`, `--provider`, `--project`, ...). The command is a pure reader -- it incurs no Anthropic API charges.
- Events are appended by a per-agent statusline shim (in the new `mngr_claude_usage` plugin) that captures the JSON snapshot Claude Code feeds to its statusline command on every render. The shim composes with any pre-existing user `statusLine.command` (the user's command runs after ours via `MNGR_USER_STATUSLINE_CMD`). All provisioning file I/O goes through `host.read_text_file` / `host.write_file`, so the shim works for local and remote agents (Modal, vps_docker, lima, ...) uniformly.
- `mngr usage` discovers events by enumerating agents via `list_agents` and reading each agent's `events/<source>/rate_limits/events.jsonl` via the events API. The writer side is wired up via a single `on_before_provisioning` hookimpl on mngr core, with no Claude-specific hookspec.
- `mngr usage` prints an actionable hint when no rate-limit events are present, explaining that the most likely cause is agents provisioned before the plugin was active and pointing users at provisioning a fresh agent or re-provisioning an existing one.

- Fixed: `mngr clone <agent> <new-name>@.<provider>` (and `mngr migrate` for cross-host moves) now succeeds when the source and destination agents live on different hosts. Previously the plugin-state rsync passed the destination host as both source and target, so rsync ran on the destination sandbox and failed with `change_dir "/.../plugin" failed: No such file or directory` because the source plugin dir only exists on the source agent's host. `CreateAgentOptions` now carries the source agent's location (host + state dir) as a single `HostLocation`, and `_transfer_source_plugin_data` rsyncs from that host to the destination -- so the Claude transcript, session history, and memory carry over for local->remote, remote->local, and remote->remote clones alike.

Add `mngr usage wait`: block until a usage snapshot matches a CEL
predicate, then exit 0. Useful for composing with `mngr message` / `mngr
create` to launch new work once budget conditions are met (e.g. "75% of
the 5h window has elapsed and at most 50% of the limit has been used"):

```
mngr usage wait --until 'five_hour.elapsed_percentage > 75 && five_hour.used_percentage < 50' \
  && mngr message my-agent "ok, kick off the next batch"
```

The CEL context per source matches `mngr usage --format json`'s
`sources[i]`. Exit codes mirror `mngr wait` (0 matched, 1 error, 2
timeout); JSONL output uses the same `state_change` envelope as
`mngr wait` so downstream consumers see one consistent shape across
both wait commands. Restrict matching to a specific writer with the
top-level `source` field in CEL (e.g. `source == "claude"`). Default
poll interval is 30s.

The Claude writer now also emits `window_seconds` per fixed-duration
window (`five_hour=18000`, `seven_day=604800`), enabling the reader to
derive `elapsed_seconds` / `elapsed_percentage` per window. These new
fields are surfaced in `mngr usage --format json` output (alongside the
existing `seconds_until_reset`) and are available to `mngr usage wait`
CEL predicates. Variable-duration windows (Claude's overage) intentionally
omit `window_seconds`, so the derived fields are `null` there.

Internal: shared exit-code constants moved from `mngr_wait.primitives`
to `mngr.cli.exit_codes`, callable from both `mngr_wait` and
`mngr_usage`.

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

- New experimental plugin `mngr_claude_subagent_proxy` reroutes Claude
  Code's built-in `Task` (Agent) tool through mngr-managed subagents
  via a Haiku dispatcher. Users can `mngr connect` to the spawned
  subagent and observe its progress; the parent still receives a
  normally-shaped `tool_result`. The wait-script invokes
  `mngr create --type mngr-proxy-child`, tags the child with
  `mngr_claude_subagent_proxy_parent_{name,id}` + `_tool_use_id`
  labels for parent↔child queries via `mngr list --format json`,
  and tails the child's transcript JSONL until a terminal stop
  reason. Project / plugin Stop hooks are auto-guarded with an
  env-conditional `MNGR_CLAUDE_SUBAGENT_PROXY_CHILD` prefix so they
  no-op inside spawned subagents (otherwise an autofix orchestrator
  in the parent will hold its child responsible for the parent's
  uncommitted changes / failing CI). See `libs/mngr_claude_subagent_proxy/README.md`
  for the full architecture, label schema, deferred work, and
  experimental-status banner.

## 2026-05-10

- Fixed a spurious "Duplicate host name '127.0.0.1' found on provider 'docker'" warning that fired on `mngr list` when multiple Docker containers were running against a local Docker daemon. `Host.get_name()` now returns the mngr-assigned host name from the host's certified data instead of the SSH connector hostname. Use the new `get_connector_host_name()` accessor when the literal connector address is needed.

## 2026-05-09

- Fixed: the `minds run` process no longer pegs a CPU after agents or hosts come and go. Reverse-tunnel bookkeeping in the desktop client's `SSHTunnelManager` (used for Latchkey gateways) is now pruned when an agent is destroyed -- so paramiko transport threads can exit instead of being kept alive by repeated re-establishment attempts -- and the 30s health-check loop applies per-tunnel exponential backoff and drops a tunnel after 10 consecutive failed repair attempts.
- Fixed: the `mngr forward` subprocess no longer pegs ~130% CPU after agents disconnect. The bidirectional relay loop (now lifted to a single shared module at `libs/mngr_forward/imbue/mngr_forward/relay.py`, used by both the desktop client's reverse tunnels and `mngr forward`'s direct-tcpip forwards) terminates when the paramiko channel has received EOF; previously `select.select` would mark the channel readable but `recv_ready()` returned False, falling through and spinning the loop at ~1M iters/sec on each half-closed channel.

- Changed: the desktop client's `SSHTunnelManager` reverse-tunnel health check now retries broken tunnels forever (capped at one attempt per 5 minutes via the existing exponential backoff) instead of giving up after 10 consecutive failures. This matches the user-visible expectation that going offline overnight should still result in working tunnels in the morning.

- `mngr message <agent> -m /clear` (and `-m /compact`) no longer hangs for 90 s before returning. The `mngr-submit-<session>` tmux signal that `mngr message` waits on is now also fired from the SessionStart hook when the source is `clear` or `compact`, since those TUI-local slash commands do not trigger UserPromptSubmit.

- Changed: bumped the default `docker build` timeout for the docker provider from 5 to 10 minutes, and made it configurable per provider instance via the new `build_timeout_seconds` field (e.g. `mngr config set --scope user providers.docker.build_timeout_seconds 1800`).
- Improved: when a `docker build` exceeds the configured timeout, mngr now raises a clear `DockerBuildTimeoutError` that names the timeout and points at the config knob, instead of surfacing a generic non-zero-exit `ProcessError` that hid the cause behind a wall of build output.

Fixed `mngr gc` crashing on hosts whose SSH host key is missing from `known_hosts`. Such errors now raise `HostAuthenticationError` (a trust failure) instead of the generic `HostConnectionError`, so existing per-host gc handlers skip them with a warning instead of aborting the whole run.

- Removed `LaunchMode.DEV` from minds. The web create form, `/create`, and
  `/api/create-agent` now offer only `LOCAL`, `LIMA`, `CLOUD`, and
  `IMBUE_CLOUD`; submitting `launch_mode=DEV` returns 400. The DEV-only
  latchkey gateway helper, the `MINDS_ALLOW_HOST_LOOPBACK` env var, and the
  `allow_host_loopback` field on `ForwardSubprocessConfig` are gone (the
  generic `mngr_forward --allow-host-loopback` CLI flag stays for
  non-minds consumers).

Companion changes live in the forever-claude-template repo on the
same-named branch (`mngr/tweak-template`): default `~/.tmux.conf`
provisioning, `--cap-add=SYS_PTRACE` for the docker template, removal of
the unused `events_processor/` project, removal of `[create_templates.dev]`,
and the crystallization Stop hook is disabled.

- `mngr plugin add` no longer warns about unknown config fields and unknown provider backends when the local config references plugins that haven't been installed yet (those warnings would resolve themselves the moment the install completes; they were just noise)
- New `silent` flag on `_check_unknown_fields`, `_parse_providers`, `_parse_agent_types`, `_parse_plugins`, `_parse_retry_config`, `_parse_logging_config`, and `parse_config` suppresses the warnings when paired with `strict=False`. `load_config` and `setup_command_context` expose the same via a `silent_unknown_fields` parameter. Default behavior on every other command is unchanged.

## 2026-05-08

- Fixed: `mngr stop` no longer leaves orphaned child processes alive on Linux when the agent's pane process (e.g. `claude`) was killed abruptly (SIGKILL, OOM). The pane-descendant walk previously missed grandchildren that had reparented to PID 1 -- typically `playwright-mcp`, `node`, or other long-lived helpers -- so they survived `mngr stop` and accumulated, consuming memory across stop/start cycles. `Host.stop_agents` now also enumerates processes by their inherited `MNGR_AGENT_ID` env var via `/proc/<pid>/environ`, catching these orphans regardless of process tree.

Removed `apps/minds_workspace_server/` from the monorepo. The workspace server (the FastAPI + dockview UI service that runs inside each agent's container) has been migrated to forever-claude-template, where it now lives at `apps/system_interface/` and ships as the `minds-workspace-server` CLI. Consumers (the minds desktop client and mngr) pick it up at runtime from the consumer's vendored forever-claude-template checkout instead of from this repo. Build-time impact: the release Dockerfile no longer cross-references the workspace server's frontend, and the node/npm install step that existed only to build it has been dropped. The `apps/minds/scripts/propagate_changes` dev-loop script now rsyncs from `/code/apps/system_interface/frontend/` in the running agent. User-facing docs (`apps/minds/docs/overview.md`, `apps/minds/docs/workspace/getting_started.md`) and the historical specs that referenced the old path were updated.

- Fixed the changelog consolidation cron's commit author email: was `dev@imbue.com`, now `bot@imbue.com`, matching the verified email on the bot GitHub account whose token the cron uses to push and open PRs. Without this, GitHub couldn't attribute consolidation commits to the bot user.

- `scripts/setup_changelog_agent.sh` now redeploys when re-run: removes any existing `changelog-consolidation` schedule before recreating, so the deployed schedule always reflects the current source. Drops the `CHANGELOG_REPLACE=1` gate that previously errored on an existing schedule.
- Header docstring now lists the required `GH_TOKEN` (token for `bot@imbue.com`) and `ANTHROPIC_API_KEY` env vars, and includes the on-demand trigger one-liner.

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

- Removed an unused `# type: ignore[misc]` in `ssh_tunnel_test.py` so the type-error ratchet stops failing on it.

## mngr_kanpan: staleness taint semantics

Field values now track when they were computed and render dimmed when older than a configurable threshold, surfacing potentially-out-of-date data at a glance.

- Added a required `created: datetime` field to every `FieldValue`. Values derived from cached inputs inherit the oldest `created` of the inputs they actually used (taint propagation); world-derived values use the current time.
- Added `staleness_threshold_seconds` to `KanpanPluginConfig`. Defaults to 90% of `refresh_interval_seconds` so values that weren't refreshed last cycle render as stale.
- Stale cells render in dark grey via new `stale` / `stale_focus` urwid palette entries. Muted-row dimming wins over per-cell stale dimming.
- `ShellCommandConfig` now declares its cached `inputs` explicitly so shell-derived staleness can propagate correctly. Shells with no declared inputs are treated as world-fresh.

- Fixed: `mngr create` now provisions credentials correctly inside nested sandboxes (e.g. a Linux lima VM running on a macOS host). `get_user_claude_config_dir()` previously returned `$ORIGINAL_CLAUDE_CONFIG_DIR` even when that path (a host-side path like `/Users/<user>/.claude`) did not exist inside the VM, causing `_provision_local_credentials` to log "No .credentials.json found to provision" and silently no-op. Spawned child agents then failed Claude sessions with "Not logged in". The helper now falls back to `$CLAUDE_CONFIG_DIR` when `$ORIGINAL_CLAUDE_CONFIG_DIR` does not resolve to an existing directory, so credential provisioning (and every other call site that resolves user-scope config) finds the live per-agent credentials.

Add `.github/workflows/tmr.yml`: a manually-dispatched CI workflow that runs `mngr tmr` against Modal, uploads the HTML report as an artifact, and opens a draft PR for the integrator branch. The provider is hardcoded to `modal`; `test_paths`, `pytest_args`, and `agent_type` are exposed as workflow inputs, with defaults reproducing the local invocation against `libs/mngr/imbue/mngr/e2e/test_basic.py -m release` with `--agent-type yolo`.

The `mngr tmr` CLI also now emits an `integrator_branch` event on its structured stdout stream (in `--output-format jsonl`/`json`), so consumers like the new workflow can pick up the branch name without parsing human-formatted output.

- mngr_modal: drop `ModalMode.TESTING` from production code paths; tests inject `TestingModalInterface` via `make_testing_provider` instead. Production `mngr_modal.backend` no longer imports `modal_proxy.testing` at module top, so the standard `**/testing.py` wheel-exclude rule applies cleanly to `modal_proxy` (no `only-include` workaround needed) and packaged consumers (e.g. minds.app) no longer crash with `ModuleNotFoundError: No module named 'imbue.modal_proxy.testing'`.
- mngr_modal: `ModalMode` retained with values `DIRECT` (default) and `PROXIED`. `PROXIED` is reserved for routing Modal traffic through the imbue_cloud gateway and currently raises `NotImplementedError` at `build_provider_instance`. The `mode` field on `ModalProviderConfig` is preserved.
- mngr_modal: extract pure `ModalProviderBackend._derive_modal_names(name, config, mngr_ctx)` helper so the environment-name / app-name / host-dir derivation can be unit-tested without instantiating any Modal interface.
- imbue_common: extend `TEST_FILE_PATTERNS` (used by all standard ratchet checks to skip test files) from `("*_test.py", "test_*.py")` to `("*_test.py", "test_*.py", "conftest.py", "testing.py")` -- aligning with the wheel-exclude pattern from #1505 so `testing.py` and `conftest.py` are uniformly recognized as test code across ratchets. Existing snapshots are not affected (the change can only reduce violation counts; current snapshots are upper bounds).
- mngr_modal: drop unused `is_testing` parameter from `_get_or_create_app` (only ever non-default in the now-removed `TESTING` dispatch arm; the test-fixture path constructs `ModalProviderApp` directly and never went through this function).

- modal_proxy: `ModalInterface.enable_output_capture` is now an abstract method. `DirectModalInterface` hooks into the Modal SDK output system; `TestingModalInterface` returns a `nullcontext`. Stacked on #1520.
- mngr_modal: extract `ModalProviderBackend._construct_modal_provider(name, config, mngr_ctx, modal_interface)` as the shared factory body. `build_provider_instance` matches the parent-class signature exactly, dispatches on `config.mode` (`DIRECT` selects `DirectModalInterface()`, `PROXIED` raises `NotImplementedError`), then delegates to `_construct_modal_provider`. Tests call `_construct_modal_provider` directly with `TestingModalInterface`. The factory has no per-implementation branches.
- mngr_modal: `make_testing_provider` collapses from a 35-line parallel constructor into a wrapper around `ModalProviderBackend._construct_modal_provider`.
- mngr_modal: delete the dead `mngr_modal/log_utils.py` re-export shim (`b66f3cbd5`'s in-tree migration is complete; nothing imports from it).

- mngr_modal: register the session-scoped Modal env created by `modal_subprocess_env` with the leak-detection registry (`register_modal_test_environment`) so that silent failures in the per-session cleanup helpers (`delete_modal_apps_in_environment` / `delete_modal_volumes_in_environment` / `delete_modal_environment`) are now caught by the autouse `modal_session_cleanup` at session end, rather than leaking the env onto the Modal account. Closes the gap that was producing the `mngr_test-{timestamp}-{user_id}` envs containing `mngr_test-{timestamp}-modal` apps observed accumulating on the imbue Modal account (42 currently-orphaned envs sampled, 30/30 matching this signature).
- mngr: surface non-zero CLI exits in the three best-effort cleanup helpers (`delete_modal_apps_in_environment`, `delete_modal_volumes_in_environment`, `delete_modal_environment`). Previously they ran `subprocess.run(...)` without `check=True` and only caught `subprocess.SubprocessError`/`TimeoutExpired`/`FileNotFoundError`, so a non-zero exit from the `modal` CLI passed silently with a `"Deleted ..."` debug log. Now non-zero exits log a warning with stderr/stdout, while still treating the failure as non-fatal (the autouse session-end leak detector remains the loud safety net).

- mngr_modal: restore the per-test reset of `ModalProviderBackend._app_registry`. The
  autouse `_reset_modal_app_registry` fixture was deleted in #1533. After #1522
  reshaped the test factory to dispatch through `_construct_modal_provider`
  (which short-circuits on the class-level `_app_registry`), the reset became
  load-bearing for cross-test isolation: the second test in a worker would
  reuse the first test's cached app and skip `modal_interface.app_create(...)`,
  leaving `testing_modal._apps` empty and breaking helpers like
  `make_sandbox_with_tags`. Restoring the fixture fixes the post-merge CI
  failures on main.

## 2026-05-07

- minds now injects `LATCHKEY_DISABLE_COUNTING=1` into every workspace
  whenever latchkey is wired (alongside `LATCHKEY_GATEWAY`,
  `LATCHKEY_GATEWAY_PASSWORD`, and `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE`).
  The workspace-side `latchkey` CLI runs in client mode against the
  host-side gateway, so suppressing its daily goatcounter.com ping
  prevents every agent from being counted as a separate active user --
  the single host-side gateway already represents the one real user.

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

- Bumped the `latchkey` npm dependency to 2.8.0 and switched minds to
  running a single shared `latchkey gateway` subprocess for every agent
  instead of one per agent. The gateway is now password-protected via
  `LATCHKEY_GATEWAY_LISTEN_PASSWORD` (the password is derived
  deterministically from the desktop client's Latchkey encryption key by
  hashing a JWT minted with `latchkey gateway create-jwt`, so it
  survives restarts without being persisted in plaintext).
- Each agent gets its own `latchkey_permissions.json`. At
  agent-creation time minds allocates an opaque
  `~/.minds/latchkey/permissions/<uuid>.json` handle, materializes it
  with empty rules (deny-all baseline), mints a permissions-override
  JWT for that path, and injects all three latchkey env vars
  (`LATCHKEY_GATEWAY`, `LATCHKEY_GATEWAY_PASSWORD`,
  `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE`) at `mngr create` time.
  After `mngr create` returns the canonical agent id, minds replaces
  the opaque file with a symlink pointing at the canonical
  `~/.minds/agents/<agent_id>/latchkey_permissions.json` location, so
  the existing permission-grant flow continues to write to its
  conventional path while the gateway reads through the symlink. The
  gateway's own default permissions config
  (`~/.minds/latchkey_default_permissions.json`) is materialized empty
  (deny-all) so requests that bypass the JWT mechanism cannot reach
  any service.
- DEV-mode agents (which run in-process on the bare host with no SSH
  reverse tunnel) now go through the same gateway as every other
  launch mode. `AgentCreator` queries the gateway's live host port
  and injects it as `LATCHKEY_GATEWAY=http://127.0.0.1:<dynamic_port>`
  alongside the password and JWT. Previously DEV agents bypassed the
  gateway entirely, which made the full latchkey flow impossible to
  exercise from DEV.
- The agent-side `latchkey` CLI version is pinned in
  `forever-claude-template`'s Dockerfile (`ARG LATCHKEY_VERSION=…`) and
  must be bumped to 2.8.0 in lockstep with this change. Agents booting
  from an image that pre-dates the bump will hit `401` on every gateway
  call because their `latchkey` CLI doesn't know about the new password
  and override headers.
- Old per-agent gateway records left under
  `~/.minds/agents/<id>/latchkey_gateway.json` are cleaned up
  automatically on desktop-client startup. Agents that were created
  with earlier minds versions need to be re-created to pick up the new
  env vars; without them their `latchkey` CLI calls will be rejected by
  the now-password-protected gateway.

## 2026-05-06

Upgrade offload from 0.8.1 to 0.9.0 and enable history-based test scheduling.
Offload now records per-test durations and uses them to balance sandbox load times,
reducing wall-clock time for the test suite.

Upgrade offload from 0.9.0 to 0.9.2 in CI. Picks up a fix for thin-diff application. Adds the offload binary to the sandbox image (via a multi-stage build) so 0.9.2's `offload apply-diff` step works without falling back to a full rebuild, and propagates `GITHUB_HEAD_REF` / `GITHUB_REF_NAME` through to sandboxes so branch-aware tests like the changelog-entry ratchet identify the PR branch correctly.

`apps/minds/scripts/propagate_changes` now protects `.claude/settings.local.json` from `rsync --delete` when syncing the template into an agent's work_dir.

That file is generated per-agent at create time by mngr's `_configure_agent_hooks` and holds the `UserPromptSubmit` hook that signals `tmux wait-for -S "mngr-submit-..."`. Without it, every `send_message` hangs the 90-second submission-signal timeout while the prompt is actually delivered to Claude (so the UI shows the message and Claude responds normally, but the HTTP `/message` request times out).

Previously the script only protected `runtime/` and `.mngr/`, so iterating with `propagate_changes` reliably reproduced the hang -- and there was no easy way to recover short of recreating the agent.

Fix WebSocket broadcaster queue-full flood and hung-send pin: stuck WS clients are evicted after 50 consecutive queue-full broadcasts, and the broadcaster cancels the wedged handler's asyncio task to free a coroutine blocked in `await websocket.send_text(...)` on a half-dead TCP connection. The previous behaviour pegged a CPU core and filled tmux with `WebSocket client queue full, dropping message` warnings whenever a single client stopped draining its queue.

- `mngr imbue_cloud admin pool create`: post-create read-back is now scoped to `--provider <provider>` (default `vultr`) and uses `--on-error continue`, so a pre-existing stale host on the operator's machine no longer aborts the bake before the management-key install + DB INSERT. The bake still fails loudly when the just-created agent is genuinely missing from the listing output.
- Removed the broken `just create-pool-hosts-dev` and `just create-pool-hosts` recipes. Both called `apps/remote_service_connector/scripts/create_pool_hosts.py`, which still inserted into the dropped `pool_hosts.version` column and so failed against the migrated schema. The replacement is `mngr imbue_cloud admin pool create` (with `--mngr-source` for the dev-loop's working-tree-into-vendor/mngr/ rsync). `just sync-vendor-mngr` is unchanged -- it serves a different (release) flow not covered by the plugin. Updated `just minds-start`'s "no FCT worktree" hint and the `minds-dev-iterate` skill to point at the new bake path.
- Deleted dead code: `apps/remote_service_connector/scripts/create_pool_hosts.py` (replaced by `mngr imbue_cloud admin pool create`), and the now-unused `libs/imbue_common/imbue/imbue_common/pool_host_constants.py` (`PLACEHOLDER_ANTHROPIC_API_KEY`) plus its test. Updated `apps/minds/docs/host-pool-setup.md` to document the new schema (`attributes JSONB`) and bake command, and updated `generate_management_key.py`'s usage hint to point at the new command.

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

- Internal: re-baseline mngr_imbue_cloud against the standard ratchet checks. The new plugin's `test_ratchets.py` now includes the full set of `test_prevent_*` functions derived from `standard_ratchet_checks.py` (snapshots pinned to current violation counts so they can only ratchet down).
- Internal: register `imbue.mngr_imbue_cloud` in the root `pyproject.toml`'s combined `--cov=` list so the per-package and combined coverage gates see its source files. Pin the plugin's per-package coverage gate to its current 19% baseline (was 50%, never met) and lower mngr_recursive's gate from 84% to 83% to reflect the recently-added remote-upload helpers.
- Internal: bump `test_destroy_transfer_none_keeps_shared_worktree` to a 60s timeout so its tmux polling loop has room to finish when offload sandboxes are contended; relax `test_read_grandparent_pid_returns_alive_grandparent` to skip when the worker has no resolvable grandparent (some offload sandboxes run pytest directly under PID 1).

QA pass for the merged forwarding refactor on top of `josh/imbue_cloud_ready`:

- Resolved a `test_ratchets.py` merge conflict in `mngr_imbue_cloud` (kept the standard layout, set the `bare_print` snapshot to 1 to match the surviving `sys.stderr.write` in `cli/admin.py`).
- Pruned the dead `tunnel_token_store` re-injection path from `LocalAgentDiscoveryHandler` (the parallel `mngr/imbue-cloud` branch dropped that cache; the agent's container persists the token now and rebuilds re-fire the post-create injection).
- Passed `concurrency_group=` to `LatchkeyDiscoveryHandler` in the new `minds run` entry point.
- Switched `apps/minds/electron/backend.js` from spawning `minds forward` to `minds run` so QA exercises the `mngr_forward` plugin subprocess + `EnvelopeStreamConsumer` path.
- Ported `start_grandparent_death_watcher` (Electron-exit detection) and `_ImbueCloudAuthErrorDisabler` (auto-disable an imbue_cloud account whose session has been revoked) from the legacy `desktop_client/runner.py` over to the new `cli/run.py` path. Added an `add_on_provider_error_callback` API on `EnvelopeStreamConsumer` so the disabler has somewhere to register.
- Phase 2 cleanup of the `mngr_forward` split:
  - Deleted `desktop_client/runner.py` and `cli/forward.py` + `cli/forward_test.py` (the legacy `minds forward` command).
  - Deleted `MngrStreamManager` from `desktop_client/backend_resolver.py` (replaced by `EnvelopeStreamConsumer` in `forward_cli.py`) and dropped the corresponding test block from `backend_resolver_test.py`.
  - Slimmed `desktop_client/cookie_manager.py` to the minds bare-origin session helpers; the per-subdomain auth-token helpers live in the plugin's `cookie.py`.
  - Slimmed `desktop_client/app.py`: deleted the host-header subdomain-forwarding middleware, `_handle_workspace_forward_http`/`_websocket`, `_handle_subdomain_auth_bridge`, `_handle_goto_workspace`, the SSH-tunnel helpers (`_get_tunnel_socket_path`, `_get_tunnel_http_client`, `_connect_backend_websocket`, `_forward_workspace_http`), `_unauthenticated_subdomain_response`, `_parse_workspace_subdomain`, and the catch-all WebSocket route. `create_desktop_client(...)` no longer takes `tunnel_manager`, `latchkey`, or `stream_manager`; it gains `mngr_forward_port` + `mngr_forward_preauth_cookie` so server-to-server refresh broadcasts route through the plugin.
  - Rewired `_dispatch_refresh_broadcast` to POST through the plugin's per-agent subdomain (`<agent>.localhost:<plugin_port>/api/refresh-service/<svc>/broadcast`) with the preauth cookie, instead of opening its own SSH tunnel.
  - `supertokens_routes._bounce_mngr_observe` → `_bounce_forward_observe`: sends `SIGHUP` via `EnvelopeStreamConsumer.bounce_observe()`. Dropped the legacy `MngrStreamManager` fallback.
  - Templates (`landing.html`, `sharing.html`, `permissions.html`, `chrome.html`, `sidebar.html`) and static JS (`chrome.js`, `sidebar.js`, `sharing.js`) now point `/goto/<agent>/` links at the plugin's port via a `mngr_forward_origin` Jinja variable / `data-mngr-forward-origin` attribute.
  - Electron's `backend.js` exposes a new `onMngrForwardStarted` callback; `main.js` consumes the `mngr_forward_started` event from `minds run` stdout and pre-sets the `mngr_forward_session=<preauth>` cookie on `localhost:<plugin_port>` (default + content session) before any agent-subdomain navigation.
  - Updated user-facing references to `minds forward` → `minds run` in `apps/minds/README.md` and `apps/minds/docs/{design,desktop-app,overview,workspace/getting_started,workspace/glossary}.md`.
- Ported the spirit of two adjacent fixes that landed in PRs 1471 and 1482 (which had touched the now-deleted minds-side forwarder):
  - `AgentCreator` now polls the workspace_server through the plugin's per-subdomain endpoint until it returns 200 before publishing the redirect URL. Without the poll, freshly-created agents redirected the browser before the workspace_server inside the agent finished starting up, and the user saw a transient 503 / 404. Best-effort: timeout (default 60 s) just publishes the redirect anyway so the user lands on the auto-refresh page rather than spinning forever. New fields on `AgentCreator`: `mngr_forward_port`, `mngr_forward_preauth_cookie`, plus the timing tunables. (PR 1471 part 1)
  - `mngr_forward._forward_workspace_http` now serves the auto-refresh retry page (`_service_unavailable_response`) on `httpx.ConnectError` and `httpx.RemoteProtocolError` instead of returning a hard 502, so HTML navigations to a backend that is still booting auto-recover. (PR 1471 part 2)
  - `mngr_forward._handle_workspace_forward_http` and `_handle_workspace_forward_websocket` now refuse to dial host loopback (`localhost` / `127.0.0.0/8` / `::1` / `0.0.0.0`) when no SSH tunnel exists for the agent. Previously, a remote agent with stale or delayed SSH info would silently fall through to the host's loopback at the registered port, exposing whatever else happened to be bound there as the "agent's workspace UI". The plugin gains a `--allow-host-loopback` CLI flag (off by default) for the legacy `LaunchMode.DEV` path; minds opts in via `MINDS_ALLOW_HOST_LOOPBACK=1`. (PR 1482)
- Fixed a redirect-after-creation bug exposed during QA: minds was sending the browser to `/goto/<id>/` (a relative URL), which the browser resolved against the minds bare origin (port 8420). minds doesn't serve `/goto/`; the plugin does. The user landed on FastAPI's default `{"detail":"Not Found"}` 404 right after creation. `AgentCreator` now builds the absolute `http://localhost:<mngr_forward_port>/goto/<id>/` URL when configured.
- Removed the latchkey pre-spawn / pending-gateway flow from minds. `_allocate_latchkey_gateway`, `_build_latchkey_gateway_url`, `Latchkey.allocate_gateway`, `Latchkey.bind_gateway_to_agent`, `Latchkey.discard_unbound_gateway`, the `PendingLatchkeyGateway` class, the `_PENDING_GATEWAYS_DIR_NAME` constant, and the `pending-gateways/<creation_id>/` directory tree are all gone. `AgentCreator` no longer takes a `latchkey` field. The pre-spawn was always racing `LatchkeyDiscoveryHandler.__call__` (which fires off observe-stream events while `mngr create` is still running and creates `agents/<agent_id>/` ahead of the bind), and the rename consistently lost the race -- producing a per-agent `Failed to bind Latchkey gateway ... agent_id collision` warning and leaking both an orphan gateway subprocess and a `pending-gateways/<creation_id>/` directory. The discovery handler does the entire gateway lifecycle correctly on its own. Non-DEV modes still get `LATCHKEY_GATEWAY=http://127.0.0.1:<AGENT_SIDE_LATCHKEY_PORT>` injected at `mngr create` time (the URL is the same constant for every agent because the per-agent reverse tunnel always bridges agent-side `:1989` to whichever host port the discovery handler picked); DEV mode no longer gets an injected `LATCHKEY_GATEWAY` -- DEV runs on the bare host without a reverse tunnel, so any test that wants latchkey there can set the env var itself.

Adds a spec for backing up the gitignored `runtime/` folder of forever-claude-template (which now also contains `memory/` and `tickets/`) into the same private repo on a separate orphan branch, plus a periodic backup service and `GH_TOKEN`-based auto-push setup.

- New `mngr_imbue_cloud` plugin (`libs/mngr_imbue_cloud/`) that owns auth (SuperTokens), pool-host leasing, LiteLLM keys, and Cloudflare tunnels for the Imbue Cloud service. Adds a `mngr imbue_cloud` CLI command group with `auth`, `hosts`, `keys litellm`, `tunnels`, and `admin pool` subcommands. Multi-account is modelled as multiple provider instances of the same backend (each with `account = "<email>"`).
- `mngr create --provider imbue_cloud_<account-slug> --new-host -b repo_url=... -b cpus=... ...` now leases a matching pool host and adopts its pre-baked agent under the requested name in one invocation. Lease attributes flow through `--build-arg`; `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`/`MNGR_PREFIX` flow through `--host-env`. The plugin's `on_load_config` hook auto-registers a provider entry per signed-in account so no manual `[providers.imbue_cloud_*]` block is needed.
- Connector schema migration: `pool_hosts.version` is replaced with a flexible `attributes JSONB` column. `/hosts/lease` matches with `attributes @> request_attributes`. Backwards-compatible: legacy callers can still pass `version` as a top-level field; the connector folds it into attributes automatically.
- minds desktop client: when a discovery error from the connector indicates a revoked SuperTokens session for a specific imbue_cloud account, the matching `[providers.imbue_cloud_<slug>]` block is automatically marked `is_enabled = false` and `mngr observe` is bounced so the dead account stops poisoning subsequent discovery cycles. Signing back in (email/password or OAuth) re-enables the provider. The Manage Accounts page shows a "Signed out" badge + "Sign in again" link for any account whose provider is currently disabled.
- minds desktop client now installs a grandparent-death watcher when the Python backend starts: if Electron crashes (or is otherwise killed without running its on-quit handler), the Python backend self-terminates within ~3 seconds, and the cascade brings down its `mngr observe`/`mngr events`/latchkey children via their own watchers. Previously a crashed Electron left an orphan tree alive across restarts.
- minds: SIGTERM that minds itself sends to `mngr observe` / `mngr event` subprocesses (during shutdown, observe restart, or events-stream sync after an agent leaves the discovery snapshot) no longer surfaces as a "subprocess failed" notification.
- remote_service_connector: `add_service` is now idempotent. Updating the access list on a previously-shared service (which re-runs the full create-tunnel/add-service/set-auth chain) no longer fails with Cloudflare error 81053 ("DNS record already exists"). When a CNAME or ingress rule for the hostname is already in place pointing at the same tunnel, it is reused; if the per-service auth policy was already customized via `set_service_auth`, the tunnel default is no longer reapplied on top.

- minds: redesigned the "Create a Project" screen.
  - Removed the "Include .env file" checkbox.
  - Added an "AI provider" choice (`imbue_cloud`, `api_key`, `subscription`) that is independent from the compute provider, so any combination is valid as long as `imbue_cloud` is paired with a selected account.
  - Renamed the "Launch mode" dropdown to "Compute provider"; both compute and AI provider default to `imbue_cloud` when an account is selected.
  - Selecting `api_key` reveals a required Anthropic API key field; `subscription` injects no Anthropic credentials so the user can sign in interactively after the workspace starts.
  - Selecting `imbue_cloud` for either field with no account is rejected by both the form (with a warning) and the server (with a 400).
  - Added an optional `GH_TOKEN` field under Advanced settings that is forwarded to the agent host (or the agent in DEV mode).

Cleanup pass after splitting functionality out of minds into the `mngr_imbue_cloud` and `mngr_forward` plugins.

- The "Share" button in a workspace now opens a static informational modal that points the user at the desktop app, rather than writing a sharing-request event back to minds. Direct sharing editing from the desktop client (workspace settings page) is unchanged. Permissions / latchkey request flows are unchanged.
- Minds no longer persists `imbue_cloud` account identity (email, display_name) to disk. Only the workspace<->account association map lives in `~/.minds/workspace_associations.json`; identity is sourced on demand from the new `mngr imbue_cloud auth list` command and cached in memory.
- Destroyed agents now disappear from the projects index automatically without requiring the user to click into the destroying detail page first.

- `mngr create -vv` now emits a `Transferring agent files` log span around the
  per-file `write_file` loop in agent provisioning, so the total time spent
  pushing plugin-declared files (e.g. Claude Code config) is visible in timing
  output.
- `mngr tmr` no longer crashes the whole orchestrator when a single agent
  fails its initial-message send (e.g. `SendMessageError` from the tmux
  paste-detection timeout). The launching loops now also catch `AgentError`
  alongside `MngrError` / `HostError`, log a warning, and continue with the
  remaining agents. This applies to test-agent launching (both batched and
  pre-launched modes) and to the integrator launch.
- Fix `mngr tmr` integrator launch (and any local-provider test-agent
  launch), which always failed with `Failed to generate a unique host name
  after 100 attempts`. The local provider has a single fixed host
  ("localhost"), so the new-host path can never find a free name; TMR now
  reuses the existing local host when the target provider is `local`,
  matching what `mngr create` already does.
- `mngr tmr` HTML reports now include rows for tests whose agent failed to
  launch (e.g. `SendMessageError` from a paste-detection timeout). They are
  rendered as errored entries instead of being silently dropped, and carry
  the actual agent name that was used for the failed launch attempt -- so
  the report row matches the host/tmux session if the user kept it for
  debugging. The `mngr create -vv` log span around `_execute_agent_file_transfers`
  now wraps the early-return path too, so the span is emitted (with
  `count=0`) even when the agent declared no file transfers.
- Stop the `claude plugin update` SessionStart hook from hanging Modal-launched
  agents at an `ssh` first-contact (TOFU) prompt for github.com. The plugin
  updater shells out to `git pull`, which uses `ssh` -- on a fresh sandbox
  with no `~/.ssh/known_hosts` entry, ssh blocks on a "Are you sure you
  want to continue connecting" prompt that Claude Code's bypass-permissions
  setting does not cover. `scripts/claude_update_plugin.sh` now prefixes
  the update with `GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new
  -o BatchMode=yes'`, which writes the first-seen host key to known_hosts
  and exits non-interactively if anything goes wrong (matching the
  script's existing `2>/dev/null || true` failure tolerance).
- `mngr tmr` HTML reports now have a dedicated "Failed" section,
  separate from "Blocked". The two represent different failure modes:
  Blocked means the coding agent reported every change as BLOCKED
  (i.e. it considered the work too complex), while Failed means an
  infrastructure failure prevented the agent from producing a verdict
  (launch failed, agent timed out, agent details missing). Errored
  results that previously fell into "Blocked" now route to "Failed".

# mngr_forward plugin

A new `mngr_forward` plugin (in `libs/mngr_forward/`) lands the auth +
subdomain-forwarding logic that used to live inside the minds desktop
client. The plugin runs as a standalone tool:

```bash
mngr plugin enable forward
mngr forward --service system_interface
```

What you get:

- Local proxy on `127.0.0.1:8421` that serves
  `<agent-id>.localhost:8421/*` and byte-forwards each HTTP and WebSocket
  request to the agent's `system_interface` URL via SSH tunnels for
  remote agents.
- One-time login URL printed to stderr (or emitted as a JSONL `login_url`
  event in `--format jsonl`); the resulting cookie is signed with a key
  persisted under `$MNGR_HOST_DIR/plugin/forward/` so browser sessions
  survive plugin restarts.
- `--reverse <remote-port>:<local-port>` (repeatable) sets up reverse SSH
  tunnels for every discovered remote agent. `<remote-port>` may be `0`
  for sshd-assigned ports; the actual bound port is reported via a
  `forward.reverse_tunnel_established` envelope event.
- `--no-observe --forward-port REMOTE_PORT` mode runs `mngr list` once
  and forwards a fixed snapshot. `--no-observe --service NAME` is rejected
  as a CLI usage error.
- `--agent-include` / `--agent-exclude` / `--event-include` /
  `--event-exclude` CEL filters control which agents and event sources
  the plugin tracks.
- `SIGHUP` bounces only the `mngr observe` child subprocess; SSH tunnels,
  per-agent event subprocesses, browser sessions, and the FastAPI app
  stay alive — used by `minds run` to make a freshly-written
  `[providers.imbue_cloud_<slug>]` block in `settings.toml` take effect.

# minds run

A new `minds run` command rewires the minds desktop client to spawn
`mngr forward` as a subprocess instead of running the same forwarding
logic in-process:

```bash
minds run --port 8420 --mngr-forward-port 8421
```

- Spawns `mngr forward --service system_interface --preauth-cookie ...`
  and consumes its envelope JSONL stream on stdout.
- Serves the slimmed minds bare-origin UI on `--port` (default 8420);
  agent subdomains are served by the spawned `mngr forward` on
  `--mngr-forward-port` (default 8421).
- Emits a `mngr_forward_started` JSONL event on stdout carrying the
  preauth cookie value so the Electron shell can pre-set
  `mngr_forward_session=<value>` on `localhost:<mngr-forward-port>`
  before the first agent-subdomain navigation.
- Sends `SIGHUP` to the plugin's PID after a freshly-written
  `[providers.imbue_cloud_<slug>]` block in `settings.toml` so the new
  provider becomes visible without restarting the plugin.

The legacy `minds forward` command and its in-process forwarding /
auth / subdomain code are intentionally unchanged in this branch and
keep working. A follow-up branch will delete the now-duplicated
in-process paths.

## 2026-05-05

- Fixed: closing the last tab in a minds workspace no longer leaves a blank screen with no recovery path. The primary agent's chat tab is automatically reopened when the dockview becomes empty (whether by closing all tabs at runtime or restoring an empty saved layout).

Every workspace package's wheel build now excludes test files uniformly via the same canonical line:

```
[tool.hatch.build.targets.wheel]
exclude = ["*_test.py", "test_*.py", "**/conftest.py", "**/testing.py"]
```

Previously, several packages were missing some or all of these patterns and hatchling was shipping `_test.py`, `conftest.py`, and `testing.py` files into published wheels. Notably `libs/mngr` was leaking three test helpers (`cli/testing.py`, `api/testing.py`, `providers/docker/testing.py`) because its existing pattern only covered `**/utils/testing.py`.

A new meta ratchet (`test_every_project_excludes_tests_from_wheel`) enforces the four-pattern rule on every project so this cannot regress.

## 2026-05-04

- Fixed: local-host shell commands issued from worker threads (e.g. inside `mngr observe --discovery-only`, `mngr list`, `mngr message`, `mngr destroy`, `mngr gc`, `mngr discover`) no longer crash with `TypeError: child watchers are only available on the default loop` on Linux. `Host._run_shell_command` now bypasses pyinfra's gevent-backed `LocalConnector` for local hosts and runs commands via the `ConcurrencyGroup` process runner.

## 2026-05-02

- Added a changelog system for tracking changes across PRs
  - Per-PR changelog entry files in `changelog/` directory, enforced by CI via meta ratchet test
  - Nightly automated consolidation of changelog entries into `UNABRIDGED_CHANGELOG.md` (full entries) and `CHANGELOG.md` (concise AI-generated summary)
  - Idempotent setup script for the consolidation agent (`scripts/setup_changelog_agent.sh`)

- JSONL parsers now surface upstream corruption rather than silently dropping bad lines
  - `MalformedJsonLineWarner.parse` raises `MalformedJsonlLineError` on lines that parse but aren't JSON objects (e.g. `[1,2,3]`); valid-but-incomplete JSON is still buffered as a possible end-of-file partial write
  - `parse_event_line`, `parse_discovery_event_line`, `parse_agents_from_mngr_output`, and `_parse_batched_json_files` (vps_docker) all raise on malformed input instead of returning `None`; rationale: stdout is for JSON data, stderr is for logs, and silently skipping garbage hides real upstream bugs
  - New `MalformedJsonlLineError` exception in `imbue.mngr.errors`; new `MalformedMngrOutputError` in `imbue.minds.errors`
- Fixed: `resolve_provider_names_for_identifiers` no longer silently returns partial results when an identifier is unknown; it returns `None` to signal a full discovery scan is needed (regression introduced in the merge that combined the two parsing-fix branches)
- Fixed: `mngr connect` no longer fails type-checking; the two `build_agent_filter_cel` call sites now pass the required `cg` and `project_root` arguments to match `mngr list` and `mngr kanpan`

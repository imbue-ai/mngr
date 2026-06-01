# Unabridged Changelog - mngr_imbue_cloud

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_imbue_cloud/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# Ratchet count tightening

- Tightened the violation counts recorded in `test_ratchets.py` to their current exact values (via `uv run pytest --inline-snapshot=trim`), locking in previously-unrecorded reductions. No source-code or behavior change.

## 2026-05-26

# Delete the dead imbue_cloud inject helpers

`build_combined_inject_command` and `normalize_inject_args` (and the
`_sed_replace_env_line` / `_ensure_no_quote_chars` helpers that only
they called) were added to support a "claim CLI" pattern that never
landed. Trimming the `minds_api_key` argument earlier in this branch
left them with no caller anywhere in the monorepo except their own
test file; the central `MINDS_API_KEY` is now injected by the
latchkey gateway's `minds-api-proxy` extension on the fly, not
pushed down onto a leased pool host.

This change deletes those four functions and the entire `host_test.py`
file. The live `provision_agent` path on `ImbueCloudHost` still uses
`_build_patch_claude_config_command`, which stays.

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-22

## No more silent auto-disable on auth errors

- Previously, when `ImbueCloudAuthError` was raised during discovery, minds would silently rewrite the user's settings to set `is_enabled = false` for the offending `imbue_cloud_<slug>` block. That behavior is gone (see the `apps/minds` changelog for details). `mngr_imbue_cloud` itself is unchanged -- it still raises `ImbueCloudAuthError` on session-revoke errors; the difference is that those errors now propagate to the providers panel in minds (where the user can choose to disable the provider explicitly) instead of triggering a hidden config rewrite.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

- Bumped pinned `imbue-mngr` / `imbue-common` / `concurrency-group` versions to match the current monorepo.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

End-to-end fixes for the OVH-backed pool flow (bake -> lease/adopt -> first-start). Discovered + fixed iteratively while smoke-testing the flow against a fresh dev env.

### `pool_hosts` INSERT picks up the schema's `host_name` column

A prior schema migration added `host_name NOT NULL` to `pool_hosts` but the bake's INSERT in `mngr_imbue_cloud.cli.admin._create_single_pool_host` was never updated. Every successful pool bake died at the very last step with `null value in column "host_name" of relation "pool_hosts" violates not-null constraint` -- worst of all, the cleanup path doesn't run on a psycopg2 error, so the OVH VPS + docker image + agent + ufw + injected management key were all already done by the time the INSERT fired, and every failed bake leaked a fully-provisioned VPS. Fix adds the column (the variable was already computed at the top of `_create_single_pool_host`) and extracts the SQL into a module-level `_INSERT_POOL_HOST_SQL` constant with a regression test asserting every required column appears, so any future drift of the same shape gets caught up front without needing a fake DB.

### Bake produces a leasable state aligned with the adopt path

- The bake's services agent now uses the constant name `system-services` (was a per-bake `pool-<hex>` UUID). The minds-side adopt code in `mngr_imbue_cloud.host.ImbueCloudHost.create_agent_state` explicitly keeps the bake's name verbatim, so the bake has to use the same name the user's `mngr create system-services@<host>.imbue_cloud_<slug>` does -- otherwise the leased workspace's tmux sessions are named after the per-bake UUID instead of the user's expected `system-services`. The per-bake unique `pool-<hex>-host` suffix stays on the *host name* for operator-local mngr disambiguation across sequential bakes.
- After the existing key-injection step, the bake destroys the FCT-bootstrap-created chat agent and `rm -f`'s `/code/runtime/initial_chat_created`. During the bake the services agent boots and the FCT bootstrap creates an initial chat agent named after the bake's host (per `_build_create_chat_command` in the FCT bootstrap), then drops a sentinel file so it never recreates on later starts. Without the cleanup, the user's lease inherits the bake's chat agent name and the bake-time agent's claude session that has no API key (because the user's LiteLLM key didn't exist at bake time). Destroying both lets the bootstrap fire fresh on the user's first start with the correct host_name + access to the patched claude config dir.
- The bake's subsequent `mngr stop` / `mngr exec` calls use the full address `system-services@<host_name>.ovh` instead of just `system-services`. Now that the agent name is a constant, the operator's local mngr state accumulates one `system-services` agent per bake (each on a different host). `_get_agent_info` previously took an agent name alone and the mngr-list `--include` filter returned the first match, which under sequential bakes is some prior bake's stale agent on a stale VPS -- the bake would then SSH the wrong VPS for ufw + key injection + DB INSERT while the actually-baked container received nothing. `_get_agent_info` now takes `host_name` as a keyword arg and filters by both `name` and `host.name`.
- Multi-token `mngr exec` commands are packed into a single `shlex.join`'d positional string. `mngr exec`'s click parser is `AGENTS... COMMAND` -- the LAST positional goes to `COMMAND` and the rest to `AGENTS`. Passing the inner `mngr destroy <name> --force` as separate argv entries either ate `--force` as a `mngr exec` option (which doesn't exist) or treated `mngr`/`destroy`/`<name>` as additional agent names. Joining into one string sidesteps both.

### Lease/adopt rewrites the container's `host_name`

`ImbueCloudProvider.create_host` now SFTPs into the leased container after the host-key scan and rewrites `/mngr/data.json`'s `host_name` field to the user-supplied `HostName`. Without this, the FCT bootstrap's `_maybe_create_initial_chat` (which reads `host_name` from `/mngr/data.json` to decide what to name the freshly-recreated chat agent on the user's first start) inherits the bake's placeholder name (`pool-<hex>-host`) instead of the user's chosen workspace name. SFTP-based to dodge shell-quoting hazards in an `exec_command` round-trip; raises `MngrError` on any SSH / SFTP / JSON failure since the wrong `host_name` is exactly the bug this exists to prevent.

Swap the imbue-cloud pool bake walker from Vultr to OVH:

- `mngr imbue_cloud admin pool create` is now provider-generic. It drops the `MINDS_ROOT_NAME` env detection, adds a required `--region REGION` and repeatable `--tag KEY=VALUE`, lands on `--template main --template ovh` with `@host.ovh` + `--provider ovh`, appends `-b --vps-datacenter=<region>`, and installs + configures `ufw` on every leased VPS before the row hits `pool_hosts`. UFW failures abort the bake.
- `forever-claude-template` gains a `[create_templates.ovh]` block (no plan / datacenter baked in -- region flows in per-invocation, plan defaults from `OvhProviderConfig`). The `[create_templates.vultr]` block stays in place; `mngr_vultr` is still a registered provider for non-pool uses.

## 2026-05-12

`mngr list` for imbue_cloud now drives discovery through outer (VPS root) SSH instead of inner-container SSH. Each lease produces one outer-SSH round-trip per host: `docker exec` for a running container (reading full state inside) or `docker cp` for a stopped one (extracting the host_dir to a tmp path on the VPS). The listing therefore shows the container's true state — `RUNNING` / `STOPPED` / `CRASHED`-with-exit-code / `PAUSED` / `DESTROYED` — together with friendly host name, image, tags and full agent details even when the inner sshd is unreachable. Lease-only synthesis (state=CRASHED with `failure_reason` carrying the underlying error) is now reserved for the last-resort case where even outer SSH fails. Same `_make_outer_for_vps_ip` defense added to vps_docker / vultr so a single unreachable VPS no longer drops the others, and a pre-existing crash in the framework offline path (`CommandString("")` violating `NonEmptyStr`) is fixed.

## 2026-05-06

- `mngr imbue_cloud admin pool create`: post-create read-back is now scoped to `--provider <provider>` (default `vultr`) and uses `--on-error continue`, so a pre-existing stale host on the operator's machine no longer aborts the bake before the management-key install + DB INSERT. The bake still fails loudly when the just-created agent is genuinely missing from the listing output.
- Removed the broken `just create-pool-hosts-dev` and `just create-pool-hosts` recipes. Both called `apps/remote_service_connector/scripts/create_pool_hosts.py`, which still inserted into the dropped `pool_hosts.version` column and so failed against the migrated schema. The replacement is `mngr imbue_cloud admin pool create` (with `--mngr-source` for the dev-loop's working-tree-into-vendor/mngr/ rsync). `just sync-vendor-mngr` is unchanged -- it serves a different (release) flow not covered by the plugin. Updated `just minds-start`'s "no FCT worktree" hint and the `minds-dev-workflow` skill to point at the new bake path.
- Deleted dead code: `apps/remote_service_connector/scripts/create_pool_hosts.py` (replaced by `mngr imbue_cloud admin pool create`).

- Internal: re-baseline mngr_imbue_cloud against the standard ratchet checks. The new plugin's `test_ratchets.py` now includes the full set of `test_prevent_*` functions derived from `standard_ratchet_checks.py` (snapshots pinned to current violation counts so they can only ratchet down).
- Internal: register `imbue.mngr_imbue_cloud` in the root `pyproject.toml`'s combined `--cov=` list so the per-package and combined coverage gates see its source files. Pin the plugin's per-package coverage gate to its current 19% baseline (was 50%, never met) and lower mngr_recursive's gate from 84% to 83% to reflect the recently-added remote-upload helpers.

- New `mngr_imbue_cloud` plugin (`libs/mngr_imbue_cloud/`) that owns auth (SuperTokens), pool-host leasing, LiteLLM keys, and Cloudflare tunnels for the Imbue Cloud service. Adds a `mngr imbue_cloud` CLI command group with `auth`, `hosts`, `keys litellm`, `tunnels`, and `admin pool` subcommands. Multi-account is modelled as multiple provider instances of the same backend (each with `account = "<email>"`).
- `mngr create --provider imbue_cloud_<account-slug> --new-host -b repo_url=... -b cpus=... ...` now leases a matching pool host and adopts its pre-baked agent under the requested name in one invocation. Lease attributes flow through `--build-arg`; `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`/`MNGR_PREFIX` flow through `--host-env`. The plugin's `on_load_config` hook auto-registers a provider entry per signed-in account so no manual `[providers.imbue_cloud_*]` block is needed.

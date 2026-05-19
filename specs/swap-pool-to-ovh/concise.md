# Swap Pool Hosts From Vultr to OVH

Cut the imbue-cloud pool bake (and the matching `minds env destroy` walker) over from Vultr to OVH Cloud, and refactor the bake's responsibility split so minds owns env-aware orchestration and `mngr_imbue_cloud admin` owns provider-generic host creation.

## Overview

* The current pool bake (`mngr imbue_cloud admin pool create`) is hardwired to Vultr (`--template main --template vultr`, `@host.vultr`, `--provider vultr`) and reaches into `MINDS_ROOT_NAME` to auto-inject a `minds_env=<name>` tag. We are swapping it to OVH and removing the minds-awareness from `mngr_imbue_cloud` so the admin command becomes a generic "create a pool host of provider X" tool.
* Hard cutover: no migration of existing Vultr-backed `pool_hosts` rows; ops drops/destroys them by hand after merge. `mngr_vultr` stays registered as a normal mngr provider for non-pool uses; only the pool bake and the `minds env destroy` walker swap.
* `mngr_ovh` gains a `MNGR_VPS_EXTRA_TAGS` reader symmetric to `mngr_vps_docker.build_vps_tags`, so the existing tag-injection contract Just Works for OVH IAM v2 tags.
* A new top-level `minds pool` CLI group (`create` / `list` / `destroy`) is the env-aware entry point that subprocesses into the de-minds-ified `mngr imbue_cloud admin pool create`, supplying region, `--tag minds_env=<active-env-name>`, and the per-env workspace/management-key/database-url flags. The orphaned `apps/minds/imbue/minds/cli/pool.py` duplicate is deleted in the same change.
* OVH defaults are kept (`OvhProviderConfig.default_plan="vps-2025-model1"`, `default_image_name="Debian 12 - Docker"`, `default_region="US-EAST-VA"`); `recycle_safety_margin_hours` drops from 24 -> 2 because pool workloads are the recycle path's intended user and faster reuse is the whole point.

## Expected Behavior

### Operator runs `minds pool create`

* Operator has an activated minds env (`MINDS_ROOT_NAME=minds-<env>` or `minds`); otherwise the command refuses with a clear error.
* Operator runs e.g. `uv run minds pool create --region US-EAST-VA --count 3 --attributes '{"cpus":2,"memory_gb":4}' --workspace-dir ./forever-claude-template --management-public-key-file ./id_ed25519.pub --database-url "$NEON_DB_DIRECT"`.
* The command subprocesses into `mngr imbue_cloud admin pool create` once per host, forwarding every flag as-is and additionally injecting `--tag minds_env=<active-env-name>` (resolved from `MINDS_ROOT_NAME`: `minds` -> `production`, `minds-<env>` -> `<env>`) and `--region <region>`.
* Per-host output streams to stderr line-by-line (preserved from today's `_run_mngr_command(is_streaming=True)`); the final per-batch JSON summary is the only stdout payload.
* `minds pool list` / `minds pool destroy` are 1:1 forwards of the admin equivalents; `list` filters its output to the active env's tag, `destroy` takes a `pool_hosts.id`.

### `mngr imbue_cloud admin pool create` (provider-generic)

* Accepts a required `--region REGION`, repeatable `--tag KEY=VALUE`, and the existing `--count` / `--attributes` / `--workspace-dir` / `--management-public-key-file` / `--database-url` / `--mngr-source` flags. No `MINDS_ROOT_NAME` detection; no implicit env-name tagging; no per-tier knowledge.
* The bake invokes `mngr create <name>@<name>-host.ovh --new-host --template main --template ovh ... --label ...` with `--vps-datacenter=<region>` appended via `-b`, and `MNGR_VPS_EXTRA_TAGS=k1=v1,k2=v2` (built from the repeated `--tag` flags) injected into the subprocess env.
* After the inner `mngr create` succeeds, the bake installs and configures ufw on the leased VPS over SSH: `apt-get install -y ufw` -> `ufw allow 22/tcp` -> `ufw allow 2222/tcp` -> `ufw default deny incoming` -> `ufw default allow outgoing` -> `ufw --force enable`. The allow-22-first ordering is load-bearing (it must precede `enable` to avoid evicting the in-progress SSH session). Any non-zero exit from this sequence aborts the bake; the operator re-runs after fixing the underlying issue.
* The bake then installs the management key on both the VPS root account (port 22) and inside the container (via `mngr exec`), inserts the `pool_hosts` row, and emits the summary JSON to stdout.

### `mngr_ovh` OvhProvider extra tags

* `OvhProvider._provision_vps` reads `MNGR_VPS_EXTRA_TAGS` (same comma-separated `key=value` shape `mngr_vps_docker.build_vps_tags` uses for Vultr-style tags) and attaches each entry as an additional IAM v2 tag alongside `mngr-provider` / `mngr-host-id`.
* Parsing is strict: an entry without `=` is an error and aborts provisioning. Keys are pre-validated locally against OVH's IAM tag character regex so bad input fails fast, before any API call. The IAM `attach_tags` failure (in case the regex grows out of sync with OVH's server-side rules) still propagates as a real error rather than being silently swallowed.

### `minds env destroy` walker

* Replaces the Vultr `/instances` walker with an OVH IAM-v2 walker.
* For the active env, the destroy step lists every OVH VPS tagged with the bake's `mngr-provider=<bake's provider name>` and filters client-side for `tags["minds_env"] == <env-name>`; each match is destroyed via `OvhVpsClient.destroy_instance`. OVH's two-step terminate semantics still apply (the VPS keeps billing through end of month); the spec does not attempt to short-circuit that.

### OVH credentials

* The Vault entry at `<tier>/ovh` holds `OVH_APPLICATION_KEY`, `OVH_APPLICATION_SECRET`, `OVH_CONSUMER_KEY` (AK/AS/CK). The env CLI forwards them into the bake subprocess as same-named env vars; `mngr_ovh.OvhProviderConfig.resolve_python_ovh_kwargs` picks them up via its existing `_pick_secret` fallback. `OVH_ENDPOINT` is not threaded through -- bake uses the `OvhProviderConfig` default (`ovh-us`), and `--region` is validated server-side against the order endpoint's `requiredConfiguration`.
* Documented region options for `--region`: `US-EAST-VA`, `US-WEST-OR` (the codes the OVH README and IAM URN region-derivation table know about today). Other OVH datacenter codes accepted by the `ovh-us` endpoint also work and are validated at provision time -- failure surfaces as a clear "datacenter not allowed for this plan" error from `validate_datacenter`.

### Recycle behavior

* `enable_recycle_cancelled=True` stays the default; the spec only changes `recycle_safety_margin_hours` from 24 -> 2.
* End-result: a `mngr destroy` on a leased OVH pool host immediately followed by a fresh `minds pool create` will re-claim the cancelled VPS rather than ordering a fresh month, as long as >2h remain before the OVH expiration boundary.

## Changes

### Deleted

* `apps/minds/imbue/minds/cli/pool.py` -- orphaned duplicate of the admin pool-bake.
* `apps/minds/imbue/minds/envs/providers/vultr_tags.py` -- Vultr-specific tag walker for env destroy.
* `vultr_api_key` field on `ProviderCredentials` (in `apps/minds/imbue/minds/envs/provisioning.py`).
* `list_vultr_instances` / `delete_vultr_instances` fields on `Providers` (same file).
* `_list_vultr_for_provider` / `_delete_vultr_for_provider` shims in `apps/minds/imbue/minds/cli/env.py` and their `vultr_secret` Vault read.
* The `cli.add_command(pool)` registration in `apps/minds/imbue/minds/cli_entry.py`.

### Added

* `apps/minds/imbue/minds/envs/providers/ovh_tags.py` -- env tag walker keyed on OVH IAM v2 tags (uses `mngr_ovh.iam_tags.list_vps_resources_for_provider` filtered by `tags["minds_env"] == <env-name>`; deletion calls `OvhVpsClient.destroy_instance`).
* `apps/minds/imbue/minds/cli/pool.py` (rewritten) -- top-level `minds pool` click group with `create` / `list` / `destroy` subcommands; subprocesses to `mngr imbue_cloud admin pool create` / `list` / `destroy` with `--tag minds_env=<active-env>` and `--region <region>` injected on `create`. Registered via `cli.add_command(pool)` in `cli_entry.py`.
* `ovh_application_key`, `ovh_application_secret`, `ovh_consumer_key` fields on `ProviderCredentials` (replacing `vultr_api_key`).
* `list_ovh_instances` / `delete_ovh_instances` fields on `Providers`, with matching `_list_ovh_for_provider` / `_delete_ovh_for_provider` real implementations in `cli/env.py`.
* `[create_templates.ovh]` block in `~/project/forever-claude-template/.mngr/settings.toml`: `provider = "ovh"`, `target_path = "/code/"`, `idle_mode = "disabled"`, `pass_host_env = ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "GH_TOKEN"]`, `build_arg = ["--file=Dockerfile", "."]`. Leaves `[create_templates.vultr]` in place untouched.

### Modified

* `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/cli/admin.py`:
  * `pool_create` gains required `--region REGION` and repeatable `--tag KEY=VALUE` options.
  * `_activated_minds_env_name`, `_MINDS_ROOT_NAME_PATTERN`, and the `MNGR_VPS_EXTRA_TAGS=minds_env=...` env injection are deleted; tags now come straight from the CLI flag.
  * The `_create_single_pool_host` inner `mngr create` switches to `address = f"{agent_name}@{host_name}.ovh"`, `--template main --template ovh`, and appends `-b --vps-datacenter=<region>`. `_get_agent_info` default provider flips to `"ovh"`.
  * Post-bake SSH steps add the ufw install/configure block before the management-key install. Non-zero exit from any ufw command aborts the bake.
  * `vps_ip` written into `pool_hosts` is whatever the inner `mngr list` reports as `host.ssh.host` -- for OVH that's a serviceName-shaped DNS name (e.g. `vps-eec8860b.vps.ovh.us`), which paramiko resolves at SSH time. No `pool_hosts` schema change needed.
* `libs/mngr_ovh/imbue/mngr_ovh/backend.py` (and helpers in `iam_tags.py`):
  * `OvhProvider._provision_vps` reads `MNGR_VPS_EXTRA_TAGS`, strict-parses each comma-separated `key=value` pair, pre-validates keys against OVH's IAM tag regex, and merges the resulting entries into the tags dict passed to `attach_tags`. Strict-parse / regex failures raise rather than skip.
* `libs/mngr_ovh/imbue/mngr_ovh/config.py`:
  * `OvhProviderConfig.recycle_safety_margin_hours` default 24 -> 2; field docstring updated to reflect the pool-workload rationale.
* `apps/minds/imbue/minds/envs/provisioning.py` / `cli/env.py`:
  * `ProviderCredentials.vultr_api_key` replaced with the OVH triple (see Added).
  * `Providers.list_vultr_instances` / `delete_vultr_instances` replaced with the OVH equivalents (see Added).
  * `destroy_env` step 2 ("Vultr instances tagged with this env") becomes "OVH VPSes tagged with this env"; the surrounding flow is unchanged.
  * `_load_dev_credentials_from_vault` swaps the `<tier>/vultr` read for a `<tier>/ovh` read pulling the AK/AS/CK triple; same "Vault entry optional, warn on missing" semantics.

### Tests

* Unit tests only for this changeset; an acceptance-level test exercising the full bake against a mocked OVH client is **out of scope** (covered by a separate, more-realistic testing task).
* Extend `libs/mngr_ovh/imbue/mngr_ovh/backend_test.py` (or a sibling) to cover: `MNGR_VPS_EXTRA_TAGS` empty / single / multiple entries reach `attach_tags`; strict-parse rejection of malformed entries; local IAM-regex rejection of invalid keys.
* Extend `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/cli/admin_test.py` to cover: `--region` is required; `--tag` repeats build the right `MNGR_VPS_EXTRA_TAGS` env var; the inner `mngr create` invocation lands on `.ovh` with the expected templates / `-b --vps-datacenter=`; no `MINDS_ROOT_NAME`-derived behavior remains; ufw command sequence runs in the required order and aborts on non-zero.
* New `apps/minds/imbue/minds/cli/pool_test.py` covering: `minds pool create` requires an active env, derives `--tag minds_env=<env>` correctly for both `production` and `minds-<env>`, forwards every other flag verbatim, and reports a useful error when the activated env is missing. Subprocess invocations are faked.
* Extend `apps/minds/imbue/minds/envs/provisioning_test.py` to cover the OVH walker swap: destroy enumerates via the injected fake `list_ovh_instances` and deletes via the injected fake `delete_ovh_instances`.
* Bump any affected `test_ratchets.py` snapshots for the new modules.

### Documentation

* `libs/mngr_ovh/README.md`: clarify the actual plan size of `vps-2025-model1` (README currently says VPS-1 / 2 GB / $7.60; user-reported figure is 8 GB / <$8 -- verify against the live OVH price sheet before merge); mention the new `MNGR_VPS_EXTRA_TAGS` handling and the changed `recycle_safety_margin_hours` default.
* `libs/mngr_imbue_cloud/README.md`: replace the Vultr + Neon pool-admin example with the OVH + Neon equivalent; show `--region` and `--tag`.
* New / updated `apps/minds/docs/...` page covering `minds pool create` (the env-aware wrapper), including the per-region runbook and the manual-cleanup note for the Vultr legacy pool rows.

## Open Questions

* **Plan size mismatch.** `OvhProviderConfig.default_plan="vps-2025-model1"` is documented as VPS-1 / 2 GB / ~$7.60/mo in `libs/mngr_ovh/README.md` (and used as a 1-vCPU / 2 GB / 40 GB mock in `recycle_test.py`), but the operator expectation is 8 GB / <$8/mo. The spec assumes whichever the live OVH price sheet says today; reconcile the README and tests in the same PR so the docs stop disagreeing with reality.
* **Per-env region defaults.** `minds pool create --region` is required-no-default in this spec. If a `dev` operator routinely picks the same region, a future change could read a per-env default from the activated env's client config; out of scope here.
* **Legacy Vultr pool row cleanup.** Manual, no runbook code in this PR; if drift turns out to be a real operational headache we'd add a `mngr imbue_cloud admin pool destroy-all-vultr` helper later.

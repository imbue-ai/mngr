# Handoff — AWS minds compute provider + onboarding-message-delivery investigation

Branch: `mngr/aws-minds-provider`  ·  PR: **#2145** (draft)  ·  Base: **`origin/main`** (`3a0dfcb…`) — NOT local `main` (stale; would show ~18 unrelated merged PRs).

---

## 1. Feature work (DONE — implemented, tested, autofixed)

Adds **AWS** as a minds compute provider, parallel to Vultr/OVH/Imbue Cloud. Decisions came from a `/blueprint` Q&A; plan in `blueprint/aws-minds-compute-provider/plan-aws-minds-compute-provider.md`.

What shipped:
- **`LaunchMode.AWS`** added; **`CLOUD` renamed to `VULTR`** (clean rename, no alias). `apps/minds/imbue/minds/primitives.py`.
- `CONFIGURED_AWS_REGIONS` + `DEFAULT_AWS_REGION` (8 AMI regions) in `primitives.py` (mngr-free so `bootstrap` can import).
- **Per-region `[providers.aws-<region>]` blocks** written into the mngr profile settings at startup, **credential-gated** (only when AWS creds plausibly present). `apps/minds/imbue/minds/bootstrap.py` (`_ensure_mngr_settings`, `_write_aws_provider_blocks`, `_aws_credentials_plausibly_configured`). Blocks set `backend=aws`, `default_region`, `default_instance_type="t3.large"`, `install_gvisor_runtime=true`, `docker_runtime="runsc"`.
- **Address mapping**: `LaunchMode.AWS` → `system-services@<host>.aws-<region>`, region threaded; `--template main --template aws`; `run_mngr_aws_prepare` runs `mngr aws prepare --provider aws-<region> --region <region>` before each AWS create. `apps/minds/imbue/minds/desktop_client/agent_creator.py`.
- **Create form**: required AWS region dropdown (geo-nearest default), env-var credentials note. `region_preference.py` (AWS coords/key), `app.py` (`_region_provider_key_for_launch_mode`, `_build_region_form_context`), `templates/pages/Create.jinja`.
- **Listing**: compute-provider label on every workspace row; `aws-<region>` collapses to "AWS". New `apps/minds/imbue/minds/desktop_client/provider_display.py` (`friendly_provider_label`); wired via `backend_resolver` `provider_name` → `app.py` landing → `templates.py` → `Landing.jinja`.
- **`mngr aws prepare` read-only-first**: `libs/mngr_aws/imbue/mngr_aws/client.py` `_ssh_ingress_already_authorized` — skips the `AuthorizeSecurityGroupIngress` write when the SG already has the required ingress, so it succeeds with a describe-only key.
- **Packaging**: `apps/minds/pyproject.toml` depends on `imbue-mngr-aws`.
- **FCT template** `[create_templates.aws]`: committed in the FCT worktree at `.external_worktrees/forever-claude-template` (gitignored here), branch `mngr/aws-minds-provider`, commit `ae31e496`. **NOT pushed** to `imbue-ai/forever-claude-template` (that's a release step).
- Per-project changelogs (`apps/minds`, `libs/mngr_aws`, `dev`).

Tests: `libs/mngr_aws` 131 passed; minds unit/integration passed; ratchets pass; `ruff` + `ty` clean. Release test `apps/minds/test_aws_workspace_release.py` (gated on `MNGR_AWS_RELEASE_TESTS=1` + AWS creds) **ran for real on EC2 and PASSED** — verified runsc/gVisor container (`/proc/version` `…-gvisor`, dmesg "Starting gVisor…"), `aws-us-east-1` provider label, instance terminated on teardown.

### Autofix gate
Ran twice; converged. 6 MINOR fixes accepted. One **rejected** fix (re-adding `@pytest.mark.rsync` to the release test — false positive: the test's source is a git repo → GIT_MIRROR transfer, no rsync; verified WITH-mark fails / WITHOUT passes). Per the unattended override, the rejected fix commit lives on pushed branch **`mngr/aws-minds-provider___readd-rsync-marker`**; reverted on the working branch; recorded in `.reviewer/outputs/autofix/unfixed/`. A NOTE comment in the release test documents why the marker must not be re-added.

---

## 2. Live environment state (CLEANUP NEEDED)

- **minds Electron app is RUNNING** in the background (`just minds-start`, env `dev-josh-1`, node 24.15.0, AWS static creds in env). Backend was on a random port (last seen `:34479`); `mngr forward` on `:8421`. Stop with `just minds-stop` or close the window.
- **Real EC2 instance still up** (costs money): `i-02945eefd16ad0b2c`, us-west-2, **t3.medium**, IP `35.164.85.246`, container `minds-dev-josh-1-aws-1`, host/agent name `aws-1`. The `mngr-aws` SG exists in us-east-1 AND us-west-2 (from `prepare`). **Destroy when done** (`mngr destroy system-services@aws-1.aws-us-west-2 --force`, or via the app).
- A SEPARATE pre-existing instance `i-084042e1c8f49cf28` (created 2026-06-12, auto-named, provider tag `aws`, NOT pytest-tagged) is **not ours** — left untouched (shared `josh` account).
- The running instance is **t3.medium** because it was created before the t3.large change; I live-patched the `dev-josh-1` profile so *future* creates use t3.large.

### AWS access gotcha
`josh` profile = account `116301555306`. `~/.aws/credentials` was being edited concurrently (a `.swp` existed), causing intermittent `ProfileNotFound`. Workaround used everywhere: resolve static keys once via `/tmp/resolve_josh_creds.py` (`eval "$(uv run python /tmp/resolve_josh_creds.py)"`), then `unset AWS_PROFILE`. The desktop client's bootstrap needs `AWS_*` in its env to write the provider blocks.

---

## 3. The onboarding-message bug (root cause found; ONE OPEN QUESTION)

**Symptom:** AWS workspace comes up fine, but the onboarding "initial message" (e.g. "just say hi") is never delivered. `onboarding.py::_send_initial_problem` loops `mngr message -- <host_name>` every 2s; all attempts return "not delivered" (exit 0, empty), gives up after 1h.

**It is NOT the SSH read timing out.** That was a red herring — SSH to the host is fast now (outer `:22` 0.15s, container `:2222` 0.6s, `docker exec` 0.8s). The single `Failed to read records from VPS … Could not connect (timed out)` (log line 565) was a one-off during the FCT build (2-vCPU under load, 10s SSH handshake timeout).

**Actual root cause — two stores, offline discovery reads the wrong one:**
- The vps_docker volume root (`Options.device` = `/mngr-btrfs/<vol>`) has, by design (`host_store.py`): `host_state.json`, `agents/<id>.json` (the **outer** host-record store), and `host_dir/` (the container's `MNGR_HOST_DIR`).
- `/mngr` → `/mngr-vol/host_dir` (symlink). `MNGR_HOST_DIR=/mngr` (minds sets this via `_remote_host_env_flags`). So in-container agents write to `<vol>/host_dir/agents/<id>/`.
- The **outer store** (`<vol>/agents/<id>.json`) is written ONLY by host-side `persist_agent_data`, called at agent-CREATE time (`host.py:2050`). So it contains only agents created by the **host-side** mngr → just `system-services`.
- The **chat agent `aws-1`** is created by the **in-container bootstrap** → lands in `<vol>/host_dir/agents/agent-abdba999…/`, with **no outer-store `.json`**.
- `mngr message` → `find_all_agents` → `discover_hosts_and_agents` → `_read_records_from_vps` → `host_store.list_persisted_agent_data()` reads **only** `<vol>/agents/*.json` → sees `system-services`, never `aws-1`. (Confirmed via `mngr message -vv` trace.)
- `mngr list` finds `aws-1` because it takes a DIFFERENT path: `instance.py:1737` `get_host_and_agent_details` runs `build_listing_collection_script` **inside the container** (live inner-store listing), bypassing the outer store.

**Likely a regression, not AWS-specific.** `AwsProvider` shares all host-store/discovery code with `OvhProvider` (overrides only create/parse/list-IPs). The `host_dir` split was introduced **2026-06-03** by `70492b4a7 "Move per-host VPS docker unified volume onto a btrfs subvolume"` (added `HOST_DIR_SUBPATH="host_dir"` + the `/mngr → <vol>/host_dir` indirection). Before it, `MNGR_HOST_DIR` was the volume root = the outer store, so in-container agents wrote straight into what discovery reads.

### >>> RESOLVED (2026-06-14): imbue_cloud is NOT broken; AWS/OVH/Vultr ALL are <<<
Root cause is **NOT the June-3 host_dir split**. It is a **discovery-path asymmetry** between providers:

- **`VpsDockerProvider.discover_hosts_and_agents`** (inherited unchanged by `AwsProvider`, `OvhProvider`, `VultrProvider` — none override it) reads agents from the **persisted outer store** `<vol>/agents/*.json` via `_discover_host_records_with_agents` → `_read_records_from_vps` → `host_store.list_persisted_agent_data()`. (instance.py:1470, 1611)
- **`ImbueCloudProvider.discover_hosts_and_agents`** (its own implementation, extends `BaseProviderInstance`, NOT `VpsDockerProvider`) reads agents **LIVE** from the running container via `_collect_listing_raw_via_outer` → `build_outer_listing_collection_script` (which `docker exec`s `build_listing_collection_script` to read `host_dir/agents/*/data.json`). (imbue_cloud/instance.py:580-668, 695)

The outer store is populated **only** by the host-side mngr's `provider_instance.persist_agent_data`, called at agent create/update (host.py:2050 `create_agent_state`, 951 `save_agent_data`). The minds **chat agent** is created by the **in-container FCT bootstrap**, whose `provider_instance` is the in-container `local` provider — so its persist never reaches the AWS/OVH/Vultr outer store. Only `system-services` (created host-side by the desktop client's `mngr create system-services@host.<provider>`) lands in the outer store.

Net: `mngr message <chat-agent>` routes through `discover_hosts_and_agents`. On vps_docker providers that reads the outer store → chat agent absent → "not delivered". On imbue_cloud it reads live → chat agent present → works. `mngr list` works on ALL providers because vps_docker's `get_host_and_agent_details` does a *separate* live in-container read (instance.py:1737).

**EMPIRICALLY CONFIRMED on the live AWS host (i-02945eefd16ad0b2c, 2026-06-14):**
- Outer store `/mngr-vol/agents/`: 1 file → `system-services` only.
- Live `/mngr-vol/host_dir/agents/`: 2 dirs → `system-services` AND `aws-1` (the chat agent onboarding targets).

So: **imbue_cloud not broken** (live-read discovery; matches "worked Thursday"). **AWS, OVH, Vultr all broken** (shared outer-store discovery; structurally identical). Local `docker`/`modal`/`lima` providers are NOT affected this way (they don't use the vps_docker outer-store-only discovery — docker uses base discovery which reads live).

### Fix IMPLEMENTED (Option A) — 2026-06-14
`VpsDockerProvider` discovery now reads agents **live** from the container via `build_outer_listing_collection_script`, exactly as `ImbueCloudProvider` does (`libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py`):
- New module helpers `_read_live_listing_from_vps` + `_extract_live_agent_data`, and a typed `_VpsDiscoveryData` result (records + live agent data + running state).
- `_read_records_from_vps` now reads `host_state.json` (for the record) AND runs the outer listing script for live agents + container running state; `_discover_host_records_with_agents` / `discover_hosts_and_agents` consume the new structure.
- Removed the now-redundant `_container_running_cache` + separate `docker_inspect_running` call in `discover_hosts_and_agents` (running state comes from the same live read). Net SSH round-trips per host during discovery went DOWN (was: store reads + separate inspect; now: one listing read).
- The persisted outer store (`persist_agent_data` / `list_persisted_agent_data`) is still written and still read by offline rename/`mngr label` — only *discovery* switched to live reads.
- Tests: updated `instance_discovery_test.py` to the new return type; added `test_read_records_from_vps_surfaces_live_in_container_agents` (the regression guard) + `_extract_live_agent_data` test. vps_docker 280 / aws+ovh+vultr 421 / imbue_cloud 176 quick tests pass; ty + ruff clean.
- **Verified live** against the running AWS host: the new read path returns BOTH `system-services` and `aws-1` (the in-container chat agent), where the old outer-store path returned only `system-services`.
- Changelog: `libs/mngr_vps_docker/changelog/mngr-aws-minds-provider.md`.

Rejected alternatives: Option B (in-container bootstrap reflects its agent into `/mngr-vol/agents/<id>.json`) — fragile, needs minds/FCT changes, in-container `local` provider can't reach the vps outer store cleanly. Option C (read both, merge) — two sources of truth, more code.

Prior (now-superseded) hypothesis is below for history.

### (superseded) earlier hypothesis
**The imbue_cloud FAST path may explain why "it worked Thursday" — and may reveal the real intended mechanism.** The user's recollection: on the imbue_cloud fast path "we end up creating our own agent on there somehow," and that may either (a) insert the agent record **locally / host-side** for discovery, or (b) place it in a location `mngr message` actually reads (the outer store). If so, imbue_cloud onboarding worked **because the fast-path adoption registers the agent host-side (outer store)** — NOT because of the in-container bootstrap chat agent. That would mean: for every mode where the chat agent is created **in-container by the bootstrap** (docker/lima/vultr/ovh/aws), `mngr message <host>` to that chat agent is structurally broken by the June-3 split, and imbue_cloud only escaped it via its different (host-side adoption) creation path.

Concretely, next session should:
1. Trace the imbue_cloud fast path: `mngr_imbue_cloud` `ImbueCloudProvider.create_host` / `ImbueCloudHost.create_agent_state` (the lease/adopt of the pre-baked `system-services` pool agent). Find **where it writes the agent record** — outer store (`<vol>/agents/<id>.json` via host-side `persist_agent_data`) vs in-container. The pre-baked pool agent is named `system-services` and adopted under the host name; check whether the *chat* agent (host-name-named, created by the in-container bootstrap on first boot) is what onboarding messages, and whether IT is reachable on imbue_cloud.
2. Reconcile: minds' bootstrap creates the chat agent **in-container for ALL modes** (named after the host; see `apps/minds` README/overview + FCT bootstrap). So confirm whether imbue_cloud onboarding actually delivered to that in-container chat agent, and if so, by what path (does imbue_cloud's discovery do the live in-container listing for `mngr message` too? does the adopt path register the chat agent in the outer store?).
3. Decide the real fix: (a) make the offline discovery / outer store read `<vol>/host_dir/agents/`, or (b) reflect in-container agents into the outer store, or (c) don't split — point `MNGR_HOST_DIR` at the outer-store dir. Also confirm reproducibility on OVH/docker with THIS branch's mngr (should fail identically → proves shared regression).

### Key code references for the bug
- `apps/minds/imbue/minds/desktop_client/onboarding.py` — `_send_initial_problem` (retry loop), `OnboardingApplier`.
- `apps/minds/imbue/minds/desktop_client/latchkey/handlers/messaging.py` — `MngrMessageSender.deliver` (`_MNGR_MESSAGE_TIMEOUT_SECONDS=30`).
- `libs/mngr/imbue/mngr/cli/message.py` ~137 — `find_all_agents(filter_all=False)`.
- `libs/mngr/imbue/mngr/api/find.py` — `find_all_agents`, `_find_agents_by_identifiers_or_state` (passes `agent_identifiers` + `reset_caches=False`).
- `libs/mngr/imbue/mngr/api/discover.py` — `discover_hosts_and_agents` (`agent_identifiers` event-stream shortcut at ~177; full scan otherwise).
- `libs/mngr_vps_docker/imbue/mngr_vps_docker/host_store.py` — outer store layout, `list_persisted_agent_data`, `persist_agent_data`.
- `libs/mngr_vps_docker/imbue/mngr_vps_docker/instance.py` — `_read_records_from_vps` (~1580, reads outer store), `get_host_and_agent_details` (~1700, live in-container listing via `build_listing_collection_script` ~1737), `persist_agent_data` (~2111).
- `libs/mngr_vps_docker/imbue/mngr_vps_docker/container_setup.py:49-55` — `HOST_VOLUME_MOUNT_PATH="/mngr-vol"`, `HOST_DIR_SUBPATH="host_dir"`.
- `libs/mngr/imbue/mngr/hosts/host.py:951, 2050` — `persist_agent_data` called at agent create.

### How to inspect the live host (while it's up)
```
# resolve creds + key
eval "$(uv run python /tmp/resolve_josh_creds.py)"; unset AWS_PROFILE
PROF=~/.minds-dev-josh-1/mngr/profiles/3bdad98064fd489389b1cc868da5045f
cp "$PROF/providers/aws/aws-us-west-2/keys/vps_ssh_key" /tmp/vps_key && chmod 600 /tmp/vps_key
ssh -i /tmp/vps_key -p 22 -o StrictHostKeyChecking=no root@35.164.85.246 \
  'docker exec minds-dev-josh-1-aws-1 sh -c "ls -la /mngr-vol/agents/ /mngr-vol/host_dir/agents/"'
# outer store has only system-services.json; host_dir/agents has BOTH agent dirs.
```
`mngr list` vs `mngr message` (both with `dev-josh-1` activated + AWS static creds): list finds `aws-1`, message returns empty — deterministic.

---

## 4. Misc / smaller follow-ups noted during the work
- Unsuppressed **default `aws` provider**: minds suppresses the default `imbue_cloud` block but not a default `aws` one, so a region-less `aws` provider gets auto-created and logs `Discovery error from aws: … credentials not configured` each cycle. Should suppress it in `bootstrap._ensure_mngr_settings` like imbue_cloud. (Noise; not the message bug.)
- `mngr list` fans out to all 8 AWS region providers each cycle (cost of per-region blocks) — slower discovery when AWS configured.
- Open plan questions (not blockers): exact region set (currently all 8 AMI regions), whether to tighten `allowed_ssh_cidrs` from `0.0.0.0/0`.
- `/verify-conversation` gate has not been run for this work yet.

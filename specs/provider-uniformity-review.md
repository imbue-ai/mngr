# Provider Uniformity Review — User-Visible Behavior Across All `mngr` Providers

**Scope.** All `mngr` provider plugins as of branch `mngr/reviewer-providers`:
- **`mngr_modal`** — hosted sandboxes (battle-tested baseline)
- **`mngr_aws`**, **`mngr_azure`**, **`mngr_gcp`** — new, experimental cloud VMs
- **`mngr_vultr`**, **`mngr_ovh`** — older cloud-VPS providers (Vultr battle-tested)
- **`mngr_lima`** — local macOS VM
- **`mngr/providers/docker`** — local Docker
- **`mngr/providers/ssh`** — bring-your-own host

This review focuses on **user-visible behavior consistency** — what the user types, what they see, what they pay — rather than internal architecture. The companion `provider-architecture-review.local.md` (dated branch `ev/main`) is the architectural counterpart and was treated as orientation but verified independently.

The orchestration used 8 parallel category subagents (one per slice of user-visible behavior) plus one extra agent covering the smaller-provider subset. The eight slices are summarized below; each has a standalone deep-dive at `/tmp/provider-review-reports/0N-*.md`.

---

## TL;DR — top findings ranked

| # | Finding | Severity | Category |
|---|---|---|---|
| 1 | **Azure/GCP `mngr stop --stop-host` is a silent cost leak** — only stops container; VM keeps billing | high | Stop/Start |
| 2 | **Azure `auto_shutdown_minutes` does not stop billing** ("Stopped (not deallocated)") — looks identical to AWS/GCP behavior but isn't | high | Idle/cost |
| 3 | **AWS default `allowed_ssh_cidrs = ("0.0.0.0/0",)` while GCP/Azure default `()`** — same flag, opposite security posture | high (security) | Networking |
| 4 | **No auto-snapshot on AWS/Azure/GCP create** — Modal-style hard-crash recovery silently absent on cloud providers | high | Snapshots |
| 5 | **Idle-driven self-stop only on Modal and AWS** — Azure/GCP idle agents continue billing forever | high | Idle/cost |
| 6 | **Vultr/OVH have no `pytest_sessionfinish` orphan scanner** — release-test crash leaks real billable VPSes | high (cost) | Tests |
| 7 | **AWS/GCP `ProviderUnavailableError` falls through to "start Docker" default help text** — wrong advice for cloud auth failures | medium-high | Credentials |
| 8 | **Modal has no `mngr modal cleanup` analog** while AWS/Azure/GCP all do | low-medium | Destroy |
| 9 | **SSH provider lies: `supports_shutdown_hosts=True` but `stop_host` raises `NotImplementedError`** | medium | Capability |
| 10 | **Stopped-host visibility asymmetric**: Modal+AWS show stopped hosts in `mngr list`; Azure/GCP/Vultr/OVH cannot if VM is deallocated | high | Discovery |
| 11 | **AWS auto-snapshot README contradicts AWS code** — README says AWS client snapshot methods are implemented; code raises `VpsDockerError` | medium | Snapshots |
| 12 | **Container-shape knobs (`--cpu`/`--memory`/`--gpu`) are Modal-only** — no cross-provider way to ask for "~2 vCPU/4GB" | medium | Create UX |
| 13 | **Vultr/OVH `mngr stop`/`mngr start` not implemented** even though release tests named for them — documented behavior contract gap | medium | Lifecycle |
| 14 | **`start_host(snapshot_id=…)` silently no-ops on AWS/Azure/GCP** — Modal honors it | medium | Snapshots |
| 15 | **Modal README has no Setup section** (42 lines) while AWS/GCP/Azure READMEs are 173-218 lines | medium | Docs |
| 16 | **Docker provider binds port 22 on all host interfaces** (`0.0.0.0:<random>:22`) by default | medium (security) | Networking |
| 17 | **Auto-shutdown wiring is not pinned by tests** — pre-create gate fires but no test asserts value reaches cloud API | medium | Tests |
| 18 | **GCP lowercases mngr-provider label**; mixed-case provider name silently collides with its lowercased twin | medium | Discovery |
| 19 | **AWS `_validate_provider_args_for_create` checks pytest but not SG existence**, while GCP also pre-flights firewall | low | Create UX |
| 20 | **Build-args help text differs across providers** in length, formatting, and inline-defaults convention | low | Create UX |

---

## How to read this report

Each numbered section below corresponds to one user-visible behavior slice. Within each:
- **Headline.** What the user actually experiences.
- **Provider-by-provider comparison** in a small matrix.
- **Key findings** with `file.py:lineno` cites.
- **Symmetric strengths** worth preserving.

The detail bundle (`/tmp/provider-review-reports/0{1..9}-*.md`) carries the full evidence; this is the synthesis.

---

## 1. `mngr create` UX, build args, and defaults

### Headline

Modal takes container-shaped flags (`--cpu=2 --memory=4 --gpu=a10g`) with no provider prefix; AWS/Azure/GCP each carry their own vendor-canonical prefix (`--aws-instance-type=…`, `--azure-vm-size=…`, `--gcp-machine-type=…`) and only expose region, instance size, and spot. There is **no provider-agnostic way to ask for "~2 vCPU and 4 GB"** — a user moving a `mngr create` command between providers has to relearn the flag for shape every time. README depth varies wildly: Modal at 42 lines vs the cloud trio at 173-218.

### Build-arg surface (selected)

| Concept | modal | aws | gcp | azure |
|---|---|---|---|---|
| Region | `--region=NAME` | `--aws-region=…` | `--gcp-zone=…` (zonal!) | `--azure-region=…` |
| Shape | `--cpu` + `--memory` + `--gpu` | `--aws-instance-type=…` | `--gcp-machine-type=…` | `--azure-vm-size=…` |
| Image | `--image=…` | `--aws-ami=…` | n/a (per-host override unsupported) | n/a |
| Dockerfile | `--file=PATH` | (passthrough to docker build) | (passthrough) | (passthrough) |
| Spot | n/a | `--aws-spot` | `--gcp-spot` | `--azure-spot` |
| Timeout | `--timeout=SEC` | (use `auto_shutdown_minutes`) | (use `auto_shutdown_minutes`) | (use `auto_shutdown_minutes`) |
| Secret env | `--secret=VAR` | n/a | n/a | n/a |
| Volume | `--volume=NAME:PATH` | n/a | n/a | n/a |
| Offline / CIDR allowlist | `--offline` / `--cidr-allowlist` | n/a | n/a | n/a |

### Key findings

- **F-CREATE-1.** Container-shape knobs (`--cpu`/`--memory`/`--gpu`) modal-only; cloud trio requires vendor SKUs. *Fix:* add `--cpu`/`--memory` aliases that resolve to representative SKU per provider.
- **F-CREATE-2.** `--region` semantics mismatch: Modal advisory; AWS/GCP/Azure refuse cross-region with `VpsApiError(400, "Cross-region create not supported")` (`libs/mngr_aws/imbue/mngr_aws/client.py:574-579`). Error text mentions "client bound" — user thinks "provider bound" — and lacks the "use `--provider aws-west` instead" pointer.
- **F-CREATE-3.** AWS-only `--aws-ami`; GCP explicitly says no (`libs/mngr_gcp/imbue/mngr_gcp/backend.py:259-260`); Azure silent.
- **F-CREATE-4.** `_validate_provider_args_for_create` divergence: GCP pre-flights firewall rule and warns about implicit project resolution (`libs/mngr_gcp/imbue/mngr_gcp/backend.py:101-149`); AWS/Azure don't. *Fix:* AWS should pre-flight SG existence; Azure should pre-flight subnet/NSG.
- **F-CREATE-5.** `get_build_args_help` divergence in format makes `mngr create --help` show four visibly inconsistent sections.

### Symmetric strengths
- All three cloud providers share `raise_if_unknown_provider_arg` / `raise_if_vps_migration_arg` — same shape of error, same migration hint.
- `--git-depth=N` uniform across cloud trio.
- Cross-region/zone error: same shape `VpsApiError(400, "Cross-… create not supported")`.
- Pytest `auto_shutdown_minutes` guard at the same hook with same logic.
- `--<provider>-spot` opt-in: uniform flag name, presence-only.

---

## 2. List / discovery (`mngr list`, `mngr gc`)

### Headline

Modal and AWS converge: every host the user ever created — RUNNING, STOPPED, CRASHED, DESTROYED with `--include-destroyed` — appears in `mngr list` with the right `host_name`, the right `host_state`, and its agents. **Azure and GCP do neither**: they inherit `VpsDockerProvider.discover_hosts_and_agents`, whose host enumeration drops anything without a current public IP. Since neither Azure nor GCP overrides `stop_host`, this is latent today — but the moment they grow VM-level stop (both READMEs mark as future work), stopped hosts will silently vanish.

### State × provider matrix (excerpt)

| State | Modal | AWS | Azure | GCP |
|---|---|---|---|---|
| RUNNING | shown | shown | shown | shown |
| STOPPED (after `mngr stop --stop-host`) | shown (host record on Volume) | shown (rebuilt from EC2 tags) | **N/A by design** (`stop_host` not overridden) | **N/A by design** |
| CRASHED (host unreachable) | shown (derive offline state) | shown (cache fallback) | shown only if record cached | shown only if record cached |
| DESTROYED (with `--include-destroyed`) | shown | shown | shown | shown |
| Credentials missing | `ProviderEmptyError` (silently skipped) | `ProviderUnavailableError` (warned, surfaced) | same | same |

### Key findings

- **F-LIST-1.** Stopped-host visibility asymmetric. AWS built `_find_instance_for_host`, `discover_hosts_and_agents` override, `to_offline_host` tag-fallback (`libs/mngr_aws/imbue/mngr_aws/backend.py:655-794`). Azure/GCP inherit the base which can't see stopped/deallocated hosts. *Fix:* lift the AWS `mngr-agent-<id>` tag-mirror pattern into `mngr_vps_docker` base.
- **F-LIST-2.** Name resolution while stopped diverges: Modal works, AWS works, Azure/GCP fail with `HostNotFoundError` once VM is deallocated.
- **F-LIST-3.** GCP's lowercase label-folding (`libs/mngr_gcp/imbue/mngr_gcp/client.py:512`) silently collides mixed-case provider names: `[providers.GcpProd]` and `[providers.gcpprod]` map to the same filter.
- **F-LIST-4.** `mngr gc` (no `--provider`) silently drops a provider whose creds are missing — `get_all_provider_instances` logs at DEBUG only (`libs/mngr/imbue/mngr/api/providers.py:211-213`). A `mngr gc` after expired AWS SSO reports "0 resources" with no warning.
- **F-LIST-5.** Modal uses underscore tag keys (`mngr_host_id`); everyone else uses dashes (`mngr-host-id`). Scripts walking tags need two code paths.

### Symmetric strengths
- All four share `mngr.api.list._construct_and_discover_for_provider` — error shape uniform.
- All four use the same `DiscoveredHost` / `DiscoveredAgent` shape; downstream rendering provider-agnostic.
- AWS/Azure/GCP all use `_list_instances_cached` — cloud API hit once per command.
- All three new providers correctly raise `ProviderUnavailableError` (state unknown) vs `ProviderEmptyError` (state known empty). Verified at `backend_test.py:453-465` (AWS), `:41-58` (Azure), `:56-83` (GCP).

---

## 3. `mngr stop` / `mngr start` lifecycle

### Headline

**Modal and AWS halt compute billing on stop; Azure and GCP silently leak.** The plain `mngr stop my-agent` is uniform (tmux only) across all four. With `--stop-host`, Modal refuses (errors loudly), AWS stops the EC2 instance and preserves EBS, **Azure and GCP only stop the Docker container while the VM keeps running and billing**. Both READMEs honestly say so, but the CLI message "Stopped host: …" is the same as AWS's — there's no visible cue about cost. Idle-driven self-stop works only on Modal (snapshot endpoint) and AWS (sentinel + systemd `.path` unit on outer); Azure/GCP idle agents bill until manually destroyed.

### Behavior matrix (selected)

| Behavior | Modal | AWS | Azure | GCP |
|---|---|---|---|---|
| `mngr stop my-agent` (plain) | tmux only | tmux only | tmux only | tmux only |
| `mngr stop --stop-host` | **refuses** (`HostShutdownNotSupportedError`; user-confusing but at least loud) | stops EC2; EBS preserved; `stop_reason=STOPPED` | **container only; VM bills** | **container only; VM bills** |
| Idle self-stop | Modal endpoint snapshots+terminates sandbox | sentinel + systemd path unit → `aws ec2 stop-instances` | none | none |
| `mngr start` from stopped | restores snapshot, new sandbox | locates by `mngr-host-id` tag, `start_instance`, new IP, `_rebind_known_hosts` | n/a | n/a |
| IP continuity | n/a (new sandbox each restart) | handled explicitly (`_rebind_known_hosts`) | trivial (VM never stopped) | trivial |

### Key findings

- **F-STOP-1.** Azure/GCP `--stop-host` silent cost leak (high severity). *Fix:* override `stop_host` to call `deallocate` / `instances.stop` — OR set `supports_shutdown_hosts = False` until VM-level stop is implemented so `--stop-host` errors loudly like Modal.
- **F-STOP-2.** Idle-driven self-stop only on Modal + AWS. *Fix:* port AWS pattern to Azure (managed identity + `az vm deallocate`) and GCP (service account + `gcloud compute instances stop`).
- **F-STOP-3.** `HostState.STOPPED` semantics differ. AWS persists `stop_reason=STOPPED` via `_record_stop_reason` (`libs/mngr_aws/imbue/mngr_aws/backend.py:453-473`). Azure/GCP can't distinguish "VM disappeared" from "destroyed cleanly".
- **F-STOP-4.** Modal `--stop-host` refuses; behaviorally equivalent goal (terminate + snapshot) is reached differently. Asymmetry user-visible.

### Symmetric strengths
- Plain `mngr stop my-agent` consistent everywhere.
- `--dry-run` works identically.
- Stop is idempotent everywhere.
- `--stop-host` parallel via `mngr_executor`.

---

## 4. `mngr destroy` + region/account-wide `mngr <provider> cleanup`

### Headline

`mngr destroy` semantics are uniform from CLI surface (`--force`, `--dry-run`, `--gc`, `--remove-created-branch`). Per-host cleanup leans heavily on cloud-native cascades: AWS `DeleteOnTermination=True` on EBS, Azure `delete_option=Delete` on NIC/IP/OS-disk, GCP `auto_delete=True` on boot disk. Region/account-wide cleanup commands exist on **AWS, Azure, GCP** — and only on those three. **Modal has no `mngr modal cleanup` analog**, and OVH ships an inspection-only `mngr ovh list` rather than a setup/teardown pair. Azure's pre-create partial-failure handling is the most robust: it tracks and reclaims orphaned NICs/public IPs that Azure's 180s NIC reservation can leave behind.

### Cleanup matrix

| Provider | Scope | Refusal semantics | Idempotent | IAM/RBAC documented |
|---|---|---|---|---|
| Modal | **NO CLEANUP CMD** | n/a | n/a | n/a |
| AWS | SG + self-stop IAM profile | refuses if mngr instances exist | yes | yes (`libs/mngr_aws/README.md:166`) |
| Azure | whole `managed-by=mngr` resource group | refuses if mngr VMs exist; checks RG tag | yes | **NOT documented** |
| GCP | firewall rule | refuses if mngr instances exist (project-wide) | yes | yes (`libs/mngr_gcp/README.md:151-155`) |

### Key findings

- **F-DESTROY-1.** Modal lacks `cleanup` analog. *Fix:* either add a no-op `mngr modal cleanup` that confirms no orphans, or document the gap.
- **F-DESTROY-2.** Azure README missing "Required RBAC" section. AWS and GCP both document. *Fix:* add it.
- **F-DESTROY-3.** AWS doesn't auto-detect "host terminated via cloud console" — destroy path tries SSH cleanup leg and times out before falling through. *Fix:* short-circuit SSH leg when `get_instance_status` returns UNKNOWN/terminated.
- **F-DESTROY-4.** Azure's `_reclaim_orphaned_network_resources` self-healing sweep (`libs/mngr_azure/imbue/mngr_azure/client.py:623-668`) is great but Azure-only. *Fix:* promote pattern to shared `VpsDockerProvider` hook.
- **F-DESTROY-5.** Notable: the `_cleanup_after_create_failed` symbol referenced in our prior internal review and code review **does not exist** in the codebase — partial-failure cleanup is the base's inline `try/except` plus Azure's bespoke `finally` block. Naming gap worth fixing in any future refactor.

### Symmetric strengths
- Single `mngr destroy` CLI with uniform flags; parallel destroy via `mngr_executor(max_workers=32)`.
- All three cloud `destroy_instance` are idempotent on 404.
- All three `cleanup` refuse-while-resources-exist with consistent error messages ("Refusing to clean up…destroy them first with `mngr destroy <agent>`").
- All three `cleanup` check tags before deleting shared infra — cannot accidentally delete user resources.

---

## 5. Snapshots

### Headline

**Only Modal auto-snapshots on agent create.** Every Modal sandbox gets an `initial` snapshot via `on_agent_created` (`libs/mngr_modal/imbue/mngr_modal/backend.py:686`), so a hard-killed sandbox can be rehydrated. **AWS, Azure, and GCP have zero auto-snapshot wiring** and rely on `VpsDockerProvider.create_snapshot` which is a `docker commit` of the container layer stored on the VPS's own disk. A hard-killed AWS/Azure/GCP host (or `mngr destroy`) takes its only snapshot copy with it. Manual `mngr snapshot create my-agent` works on all four, but the cloud variants are **single-host docker layers, not portable artifacts**.

### Capability matrix

| Capability | Modal | AWS | Azure | GCP |
|---|---|---|---|---|
| `supports_snapshots` | True | True (inherited) | True (inherited) | True (inherited) |
| `mngr snapshot create` | Modal `sandbox.snapshot_filesystem()` | `docker commit` | same | same |
| Auto-snapshot on agent create | **Yes** | **No** | **No** | **No** |
| Auto-snapshot on `stop_host` | yes | **skipped** | docker commit | docker commit |
| Resume from `--snapshot <id>` (`start_host`) | **Yes** | **silent no-op** (base ignores `snapshot_id`) | same | same |
| Snapshot survives `destroy_host` | yes (records preserved for `gc_snapshots`) | **no** | **no** | **no** |
| `VpsClient`-level disk snapshot | n/a | **stubbed: raises** (`libs/mngr_aws/imbue/mngr_aws/client.py:977-991`) | implemented but **unused** | implemented but **unused** |

### Key findings

- **F-SNAP-1.** Auto-snapshot-on-create modal-only. No hard-crash recovery for cloud providers. *Fix:* add `on_agent_created` hookimpl per provider that creates real disk snapshot (Azure/GCP impls already exist in `client.py`).
- **F-SNAP-2.** AWS README contradicts AWS code about snapshot support (`libs/mngr_aws/README.md:187` vs `libs/mngr_aws/imbue/mngr_aws/client.py:984-991`).
- **F-SNAP-3.** `start_host(snapshot_id=…)` silently no-ops on AWS/Azure/GCP. *Fix:* implement or raise `SnapshotsNotSupportedError` explicitly.
- **F-SNAP-4.** `supports_snapshots = True` for cloud providers means something very different from Modal. *Fix:* add finer-grained `supports_persistent_snapshots` flag OR document in `mngr snapshot --help`.

### Symmetric strengths
- `mngr snapshot` CLI gates on `provider.supports_snapshots` uniformly.
- All four return same `SnapshotInfo` shape; JSON output uniform.
- `mngr snapshot create … → SnapshotId → mngr snapshot destroy …` round-trips on all four.

---

## 6. Credentials, provider config UX, first-run errors

### Headline

What the user has to do to get started is *not* symmetric. Each provider documents a different credential mechanism, and that part is mostly fine — but documentation depth diverges dramatically (Modal README has **no Setup section at all**: 42 lines vs ~173–218 for the others). The error-classification contract is **almost** uniform post the `provider-architecture-review.local.md` Finding A fix: AWS/GCP/Azure all raise `ProviderUnavailableError` on missing creds — but **only Azure passes curated `user_help_text`**. AWS and GCP fall through to the default help in `libs/mngr/imbue/mngr/errors.py:216-219`, which literally tells the user to **"start Docker"** — wrong advice for a cloud auth failure.

### Minimum-config matrix

| Provider | Creds | Required keys | First-run DX |
|---|---|---|---|
| Modal | `uvx modal token set` | None | A: zero-config, env auto-created |
| AWS | boto3 default chain | None (but `default_region` silently overrides `AWS_REGION` env!) | B+: zero-config after `aws configure`, requires `mngr aws prepare` |
| GCP | ADC | None (project from gcloud) | B: works after `gcloud auth application-default login` + `mngr gcp prepare --allowed-ssh-cidr` |
| Azure | `DefaultAzureCredential` | None (subscription from `az` session) | B: works after `az login` + `mngr azure prepare --allowed-ssh-cidr` |

### Key findings

- **F-CRED-1.** AWS/GCP `ProviderUnavailableError` falls through to "start Docker" default help text. *Fix:* curate per-backend help text like `_azure_unavailable_error` (`libs/mngr_azure/imbue/mngr_azure/backend.py:36-53`); ideally hoist into `ProviderUnavailableError` as a per-backend hook.
- **F-CRED-2.** Modal README has no Setup section.
- **F-CRED-3.** AWS silently overrides `AWS_REGION` env via `default_region` because `boto3.Session(region_name=self.default_region)` is unconditional (`libs/mngr_aws/imbue/mngr_aws/config.py:172`). *Fix:* defer to `os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")` before pinning.
- **F-CRED-4.** `allowed_ssh_cidrs` defaults: AWS `("0.0.0.0/0",)` (open!) vs Azure/GCP `()` (fail-closed). Same flag, different fail mode. *Fix:* default AWS to `()` to match.
- **F-CRED-5.** Modal README density mismatch (42 vs 173-218 lines).

### Symmetric strengths
- Credential resolution delegated to each cloud's SDK; no secrets in `mngr.toml`.
- `ProviderUnavailableError` vs `ProviderEmptyError` contract enforced uniformly post-fix.
- `mngr list` and `mngr gc` handle both error classes identically.
- All three cloud providers ship `prepare`/`cleanup` admin commands.

---

## 7. Operator setup, idle/auto-shutdown, networking defaults

### Headline

The three new providers share a deliberately three-way structure: `prepare` → `cleanup` → `_validate_provider_args_for_create` → pytest orphan scanner. The cost-safety story is **not** uniform: AWS `auto_shutdown_minutes` truly self-terminates the EC2 instance; GCP `auto_shutdown_minutes` truly self-deletes the GCE VM; **Azure `auto_shutdown_minutes` only runs `shutdown -P` and leaves the VM "Stopped (not deallocated)" — still billing**. Idle watcher: AWS has the sentinel+systemd pattern; Azure/GCP have none. Networking defaults: AWS `("0.0.0.0/0",)` while Azure/GCP `()` (fail-closed).

### Cost-safety matrix

| Provider | `auto_shutdown_minutes` effect | Idle watcher | Pytest gate | `pytest_sessionfinish` scanner |
|---|---|---|---|---|
| Modal | sandbox `default_sandbox_timeout=900` → terminate | yes (built-in) | env-name pattern | yes |
| AWS | `shutdown -P +N` + `InitiatedShutdownBehavior=terminate` → terminate | yes (sentinel + systemd) | yes | yes (`mngr-pytest-launched` scan) |
| GCP | `scheduling.max_run_duration` + `instance_termination_action=DELETE` → delete | none | yes | yes |
| Azure | `shutdown -P +N` only → "Stopped (not deallocated)" — **still bills** | none | yes | yes (also reclaims orphaned NICs/IPs) |
| Vultr | `shutdown -P` halts OS — billing continues | none | none | **none** |
| OVH | `shutdown -P` halts OS — monthly billing | none | none | **none** |

### Key findings

- **F-OPS-1.** AWS default ingress `0.0.0.0/0` is the odd one out. The config comment justifies "matches Vultr/OVH norm" — but Vultr/OVH have no managed firewall to default. AWS has one and is choosing the default. *Fix:* default `allowed_ssh_cidrs = ()`, require `--allowed-ssh-cidr` on `prepare`.
- **F-OPS-2.** Azure `auto_shutdown_minutes` doesn't stop billing. Documented as caveat at `libs/mngr_azure/README.md:160-166`, but pattern-matches AWS/GCP visually. *Fix:* warn louder; implement managed-identity self-delete (already a Future improvement).
- **F-OPS-3.** Vultr and OVH have no `pytest_sessionfinish` orphan scanner. A killed release test leaks billable VPS. *Fix:* add scanner mirroring AWS/Azure/GCP pattern.
- **F-OPS-4.** Azure README missing RBAC section.
- **F-OPS-5.** GCP/Azure have no in-VM idle watcher. *Fix:* port AWS sentinel+systemd pattern.

### Symmetric strengths
- Identical `_validate_provider_args_for_create` shape across AWS/GCP/Azure; "Mirrors the AWS guard:" docstring comments.
- Lookup-only hot path with `prepare` pointer on missing infra.
- `cleanup` refusal semantics consistent.
- All three READMEs carry "experimental — not yet production" header.

---

## 8. Test contracts — what's pinned, what's not

### Headline

Roughly **60-70% of the user-visible behavior matrix is pinned** somewhere in the test suite, but coverage is highly asymmetric. AWS, Azure, GCP, Modal, Docker, Lima are well-covered with deep unit + lifecycle + release tests. **Vultr and OVH are thinly covered**: no `pytest_sessionfinish` orphan scanner, no fail-closed pinning, no stop/start tests, no spot/instance-type build-arg coverage. The biggest specific hole is that **auto-shutdown values do not flow through to the underlying cloud call in any pinned test** — the pre-create gate fires on `None`, but no test asserts that `auto_shutdown_minutes=60` actually produces `shutdown -P +60` in user_data or `max_run_duration=3600` in GCP scheduling.

### Top 10 test holes

1. **Vultr/OVH orphan scanner** — `pytest_sessionfinish` hook, modeled on `libs/mngr_aws/imbue/mngr_aws/conftest.py:143-180`. *Easy.*
2. **Auto-shutdown wiring** — `test_create_instance_passes_auto_shutdown_to_user_data` (AWS); GCP/Azure/Modal counterparts. *Easy unit tests in each `client_test.py`.*
3. **Vultr/OVH credentials error classification** — mirror `mngr_azure/backend_test.py:41-58`. *Easy.*
4. **Stopped-host discovery on Azure/GCP** — AWS pins exhaustively at `backend_test.py:231-300`; no equivalent. *Medium.*
5. **Vultr/OVH absence-of-spot pinning** — pin the absence so a future flag doesn't slip in silently. *Easy.*
6. **Modal pytest gate** for `auto_shutdown_minutes`. *Easy if hook exists.*
7. **Per-provider capability-flag pinning** for AWS/Azure/GCP/Vultr/OVH (Modal/Lima/Docker/SSH already do it). *Easy.*
8. **Networking warning on `0.0.0.0/0` for GCP/Azure** — AWS pins warning; GCP/Azure only pin fail-closed empty-CIDR. *Easy.*
9. **Vultr/OVH `mngr stop`/`mngr start` semantics** — release tests `test_create_stop_start_destroy` exist but providers don't implement stop/start. **Behavior contract gap, not just test gap.** *Medium-Hard.*
10. **Cross-region refusal on Vultr/OVH** — AWS/Azure/GCP pin; Vultr/OVH not tested. *Easy if check exists; Hard if silent failure.*

### Symmetric strengths
- Build-arg parsing tests are strong for AWS/Azure/GCP/Modal.
- Cross-region refusal symmetric on cloud-API trio (`client_test.py:623`, `:159`, `:218`).
- List filtering by provider tag uniformly pinned.
- Pytest gate uniformly tested on AWS/Azure/GCP with three near-identical tests each.
- Snapshot semantics on Modal exceptionally well-covered (`test_modal_instance.py:212, 252-310, 377-475`).

### Suggested cross-provider refactor

A shared `pytest.mark.parametrize("provider_name", [...])` for: `supports_*` flags, `create rejects --foo-spot=true`, `cross-region create raises`, `credentials missing raises ProviderUnavailableError` — would let missing per-provider coverage land in one PR rather than copying tests 5 times across providers.

---

## 9. The other providers (Vultr / OVH / Lima / Docker / SSH)

### Headline

Vultr is the closest of these five to the modal/aws/azure/gcp baseline — same `VpsDockerProvider` lifecycle and same `--<provider>-region`/`--<provider>-plan` shape — but **bakes in the same cost-unsafe defaults that the AWS README treats as defects**: `0.0.0.0` ingress, no idle stop of the VM, monthly billing irrespective of `mngr stop`. OVH ships an entire `mngr ovh list` operator-CLI group plus a unique pending-orders reconcile loop that AWS/GCP/Azure would benefit from copying. **Lima and Docker have native stop/start** that the baseline narrative ("stop only stops container") doesn't describe. **SSH provider lies about its capabilities** (`supports_shutdown_hosts=True` but `stop_host` raises `NotImplementedError`).

### Taxonomy

| Provider | Category | Stop semantics | Snapshots | Build-arg prefix |
|---|---|---|---|---|
| modal | hosted-sandbox | terminate | yes | bare |
| aws | cloud | container only; instance bills (unless `--stop-host`) | yes | `--aws-` |
| azure | cloud | container only; VM bills | yes | `--azure-` |
| gcp | cloud | container only; VM bills | yes | `--gcp-` |
| vultr | cloud | container only; VPS bills | yes | `--vultr-` |
| ovh | cloud | container only; monthly billing | yes (btrfs) | `--ovh-` + `--ovh-datacenter` |
| lima | local-VM | `limactl stop` (real VM stop) | **no** | `--file` only |
| docker | local | `docker stop` (container) | yes (docker commit) | none |
| ssh | BYO | **NotImplementedError** | no | none |

### Key findings

- **F-OTHER-1.** Vultr is the second "battle-tested" provider but its cost-safety defaults precede AWS's. If we're judging AWS by Vultr's precedent, the AWS-too-open finding gets weaker. If we're judging by what we want, both should fail-closed.
- **F-OTHER-2.** Docker provider `-p :22` binds container sshd on all host interfaces — not localhost. Asymmetric with Lima, which goes the opposite extreme and blocks all guest→host port forwarding.
- **F-OTHER-3.** OVH's `mngr ovh list [--all]` inspection-CLI pattern is unique and **should be portable**: it would help operators audit untagged-but-billed instances across AWS/GCP/Azure/Vultr.
- **F-OTHER-4.** SSH provider's `supports_shutdown_hosts=True` is a lie — any caller branching on it to offer `mngr stop` will fail. *Fix:* return `False`, or document `stop_host` as no-op rather than raise.
- **F-OTHER-5.** SSH `discover_hosts` hard-codes `HostState.RUNNING` for every configured host. Down hosts show as running until user tries to use them.
- **F-OTHER-6.** OVH's pending-orders reconcile pattern (write JSON marker on order-delivery timeout; reconcile on next `mngr create`) is unique — AWS could want the analogous "RunInstances returned but instance never became reachable" recovery.

---

## Cross-cutting recommendations

### Cost / billing safety (highest priority)

1. **Make `--stop-host` semantics uniform.** Either Azure/GCP implement VM-level stop (preferred), or they set `supports_shutdown_hosts = False` so the flag errors loudly like Modal. Today it silently leaks compute cost.
2. **Make `auto_shutdown_minutes` actually stop billing on Azure.** Implement managed-identity self-delete or strongly warn at config-load time.
3. **Add `pytest_sessionfinish` orphan scanner to Vultr and OVH.**
4. **Add idle watcher to Azure and GCP** mirroring AWS's sentinel+systemd pattern.

### Security defaults

5. **Default AWS `allowed_ssh_cidrs = ()` to match Azure/GCP** fail-closed. The same flag should not behave oppositely across providers in the same monorepo.
6. **Default Docker provider port binding to `127.0.0.1`** rather than `0.0.0.0`.

### Discovery & visibility

7. **Lift AWS's `mngr-agent-<id>` tag-mirror + tag-based offline-host reconstruction into `mngr_vps_docker` base** so Azure/GCP/Vultr/OVH all show stopped hosts in `mngr list` once they grow VM-level stop.
8. **Adopt OVH's `mngr <provider> list` inspection-CLI pattern** for AWS/Azure/GCP/Vultr — uniform untagged-instance audit.
9. **Bump `mngr gc` log level** from DEBUG to WARNING when a provider has `ProviderUnavailableError` and `--provider` is unspecified.

### Snapshots

10. **Wire `on_agent_created` snapshot hook on Azure/GCP** using the existing (but unused) disk-snapshot impls in `client.py`. AWS would need to first un-stub `client.py:977-991`.
11. **Reconcile AWS README's snapshot claims** with the code.
12. **Either honor `start_host(snapshot_id=…)` or raise `SnapshotsNotSupportedError`** on AWS/Azure/GCP — silent no-op is the worst option.

### Error messages & first-run UX

13. **Curate `user_help_text` for AWS and GCP** in `ProviderUnavailableError` so the user is not told to "start Docker" when their cloud auth expires. Pattern: `_azure_unavailable_error` (`libs/mngr_azure/imbue/mngr_azure/backend.py:36-53`). Ideally hoist into the exception as a per-backend hook.
14. **Write a Modal Setup section** in `libs/mngr_modal/README.md` matching the depth of AWS/GCP/Azure READMEs.
15. **Add an Azure RBAC section** to `libs/mngr_azure/README.md`.

### Build-args & defaults

16. **Add cross-provider `--cpu`/`--memory` aliases** that resolve to per-provider SKUs.
17. **Standardize the cross-region/zone error message** with a "use `--provider <other>`" pointer.
18. **Make `_validate_provider_args_for_create` pre-flight required infra** on AWS (SG) and Azure (subnet/NSG), matching GCP's pattern.
19. **Standardize `get_build_args_help` format** across all four providers.

### Tests (locks in the above)

20. **Add the Top 10 holes from §8** as a punch list:
    - `test_create_instance_passes_auto_shutdown_to_user_data` (per provider)
    - `test_provider_capabilities` (per provider)
    - `test_ensure_<firewall|network>_warns_when_open_to_internet` (GCP/Azure)
    - `pytest_sessionfinish` orphan scanner conftest (Vultr/OVH)
    - `test_build_provider_instance_raises_provider_unavailable_without_api_key` (Vultr/OVH)
    - `test_discover_hosts_and_agents_surfaces_stopped_vm_from_tags` (Azure/GCP — after VM-level stop lands)
21. **Introduce parameterized cross-provider tests** for `supports_*`, cross-region refusal, credentials missing — to land per-provider coverage in one PR.

---

## Open questions for the human reviewer

1. **Is the goal "all providers behave the same to users" or "all providers are honest about how they differ"?** Several findings (auto-snapshot, native stop) require deep work to make uniform; others (`supports_shutdown_hosts` lying on SSH, AWS `0.0.0.0/0` default, AWS `_region` shadowing env) are dishonest defaults that should be fixed regardless.
2. **Vultr is "battle-tested" but its defaults precede AWS.** Should Vultr be the bar, or should Vultr also be tightened?
3. **Should `mngr <provider> list` be a pluggy contract** so every provider (Modal included) ships an operator inspection command modeled on OVH's `mngr ovh list`?
4. **Modal lacks a `cleanup` command.** Add a no-op for parity, or document the gap?
5. **`_cleanup_after_create_failed` doesn't exist** despite being referenced in code review. Worth a rename/refactor toward a uniform base hook?
6. **Should `supports_persistent_snapshots` be a separate capability flag** from `supports_snapshots`, to make the "docker commit on single VPS" vs "Modal cross-host snapshot" distinction explicit?
7. **For local providers (Lima, Docker, SSH), should `--auto-shutdown-minutes` be rejected at parse time** rather than silently ignored?
8. **For SSH provider, should `mngr create --provider ssh` be rejected at config-validation time** rather than after the full command is typed (then `NotImplementedError`)?

---

## Appendix: Source reports

The orchestrator dispatched 8 parallel subagent reviews. Their unabridged outputs are at `/tmp/provider-review-reports/`:

- `01-create-build-args.md` — Create UX + build args + defaults
- `02-list-discovery.md` — `mngr list`, tagging, name resolution, gc
- `03-stop-start.md` — Stop/Start lifecycle, billing, idle
- `04-destroy-cleanup.md` — Destroy + region cleanup
- `05-snapshots.md` — Snapshot semantics + auto-snapshot
- `06-credentials-config.md` — Credentials, config UX, first-run errors
- `07-operator-idle-networking.md` — Operator setup, idle, networking defaults
- `08-test-contracts.md` — Test coverage + holes
- `09-other-providers.md` — Vultr / OVH / Lima / Docker / SSH

The earlier architectural review at `provider-architecture-review.local.md` was used as orientation; all claims were verified independently against current code on branch `mngr/reviewer-providers`.

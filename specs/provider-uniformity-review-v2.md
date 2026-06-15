# Provider Uniformity Review — Round 2

**Date.** Originally 2026-06-14. **Status update 2026-06-15** — see §0 below for the audit pass after the `ev/main` force-push.

**Round 1.** `specs/provider-uniformity-review.md` (2026-06-11) — the original review. Treated `modal/aws/azure/gcp` deeply and `vultr/ovh/lima/docker/ssh` shallowly.

**This round** extends the review to **equal depth across all 9 providers** and is accompanied by two companion docs:

- **`specs/provider-shape.md`** — a forward-looking, prescriptive spec describing what an mngr provider OUGHT to look like. The "core providers doc" called out in this round's brief.
- **`specs/provider-uniformity-review-lifecycle.md`** — a deep dive into create / stop / start / destroy / cleanup behavior across all 9 providers, with a full lifecycle matrix and a `CleanupFailedGroup` adoption table.
- **`specs/provider-release-tests.md`** — proposal for a common release-test suite that walks every provider through the shape doc with `xfail`s flagging current inconsistencies.

This document is the **synthesis layer**: top findings, what changed since 2026-06-11, what remains broken, and where each finding lives in the detail docs.

---

## §0. Status update — 2026-06-15

Re-audit against `origin/ev/main` HEAD `e1175077c` after the force-push. ~240 provider-touching commits landed since the 2026-06-11 baseline; ~80 of them since the 2026-06-14 round-2 writeup. Net of the new audit:

- **STILL OPEN: 27.** The headline cost-leak and capability-honesty findings remain in place.
- **PARTIALLY FIXED / MORPHED: 6.** Detailed below.
- **FIXED: 4.** Detailed below.
- **WORSENED: 4.** Two of them — see §0.2 — invert the round-2 prescription.
- **NEW: 5.** New cross-provider improvements landed.

### §0.1 Fixes since 2026-06-14

- **F-SNAP-2 (AWS README contradicts code) — FIXED via deletion.** Commit `a4c1f7924` removed `create_snapshot`, `delete_snapshot`, `list_snapshots` from `VpsClientInterface` and from every backend (AWS/Azure/GCP/Vultr/OVH); commit `278ef2909` synced the AWS README. Provider snapshots now uniformly flow through `docker commit` at the provider layer. **Round-2 N-7 ("READMEs reference deleted methods") is partly addressed for AWS; Azure/GCP/Vultr/OVH README sync TBD.**
- **N-8 (stale `auto_shutdown_minutes=60` in AWS test) — FIXED.** `libs/mngr_aws/imbue/mngr_aws/backend_test.py:92` now reads `auto_shutdown_seconds=3600`.
- **F-D-12 (Azure default image Ubuntu) — FIXED.** Commit `4ae369728` switched Azure to `Debian:debian-12:12-gen2` (`libs/mngr_azure/imbue/mngr_azure/config.py:29-32`). The fleet is now uniformly Debian 12.
- **GCP default image — FIXED.** Commit `a9bbd4725` switched GCP to `projects/debian-cloud/global/images/family/debian-12` (`libs/mngr_gcp/imbue/mngr_gcp/config.py:26`). GCP also bootstraps via GCE startup-script rather than cloud-init now — a small departure from the cloud-init-everywhere story.

### §0.2 Findings WORSENED (or design-superseded)

- **F-D-1 / F-OPS-1 (AWS open-by-default ingress) — SUPERSEDED.** Round-2 recommended `allowed_ssh_cidrs = ()` (fail-closed) as the uniform default. **The codebase chose the opposite direction**: commits `f58b71939` (Azure) and `16fdec0cb` (GCP) flipped both providers from `()` to `("0.0.0.0/0",)` "to match AWS". The cloud trio is now **uniformly fail-open by default with a runtime warning**. The rationale is reasonable (SSH is key-only on all cloud-created hosts; password auth disabled; opening tcp/22 exposes the port but not a usable login). `specs/provider-shape.md` §3.1 has been updated to describe the chosen standard rather than the previous fail-closed prescription. This round-2 finding is **withdrawn**.
- **The cloud trio default is now uniformly `0.0.0.0/0`.** The "AWS is the outlier" framing in the round-2 status table is no longer accurate.

### §0.3 Findings PARTIALLY FIXED / MORPHED

- **F-CREATE-3 (per-host image override).** GCP gained `--gcp-image=` flag in commit `8a0fd81de`. Azure still has no equivalent.
- **F-LIST-4 / F-DLT-6 (`mngr gc` silently drops bad-creds provider).** Commit `e5a5fb5db` adds structured `CleanupFailure` failure categories + cause-specific non-zero exit codes to `mngr gc`. The `providers.py:211-213` DEBUG log line is unchanged, but the *user-visible result* of `gc` is materially better: operators see the leak in the exit code (`LOCAL_STATE_REMAINS` / `HOST_RESOURCE_REMAINS` / `PROVIDER_INACCESSIBLE` / `OTHER`).
- **F-STOP-2 (idle self-stop on Modal+AWS only).** Commit `814f08f07` lifted activity-watcher relaunch into the base `start_host`. This silently fixes the idle-host re-stop bug on Vultr/OVH (which install a watcher at create but used to lose it on resume). Azure/GCP still have no in-VM watcher at create time, so the original gap persists for them.
- **F-STOP-3 (`HostState.STOPPED` semantics).** Commit `dec33516a` folded `stop_reason` into the base `stop_host` signature; the AWS-only `_record_stop_reason` helper is gone. AWS now passes `stop_reason=STOPPED` through `super().stop_host(...)`. The lifecycle review's matrix (`specs/provider-uniformity-review-lifecycle.md`) has been patched.
- **F-DESTROY-2 (Azure README RBAC section).** Commits `b9082d810`, `547dcadd8`, `a7b94f6a2` reword Azure permission text. Whether the RBAC section is now fully present should be re-verified line-by-line.
- **Round-2 v2 status table inaccuracy.** The original v2 table claimed GCP "gained the same shape via `GcpCredentialsError` / `GcpProjectError`". **Those classes do not exist in the codebase**; the half-fix for curated `user_help_text` is Azure-only. Strike the GCP entry from F-CRED-1's "half-fixed" classification — AWS and GCP both still fall through to the default "start Docker" help text.

### §0.4 NEW findings introduced by recent commits

- **NEW-1 (high, security).** Fail-open is now the cloud-trio standard. See §0.2.
- **NEW-2 (medium).** Base `start_host` relaunches the idle watcher on resume (`814f08f07`) — symmetric strength across providers that install a watcher at create time.
- **NEW-3 (low).** AWS security hardening (`71b310628`) deleted `SSH_HOST_KEY_TAG` / `CONTAINER_SSH_HOST_KEY_TAG` (host-key-in-tags MITM defense) and made per-instance discovery skip corrupt `mngr-host-id`/`Name` tags rather than abort.
- **NEW-4 (low).** AWS agent tags became per-field (`446d6a964`). Replaces the single `mngr-agent-<id>=<json>` packed tag with `mngr-agent-<id>-name`/`-type`/`-labels`. Shape doc §3.8 reference updated.
- **NEW-5 (medium).** Live agent discovery in base (`8cca7406d`) — multi-agent visibility is no longer Modal-only. Vultr/OVH/AWS now show in-container agents that were never written to the outer store. Pairs with the new `specs/provider-shape.md` §1.8 ("N agents on one host") which formalizes the N-agents-per-host contract; release-test Trip 1b exercises it.
- **NEW-6 (low).** GCP got `--gcp-image` per-host override (`8a0fd81de`).
- **NEW-7 (low).** GCP bootstraps via GCE startup-script (`a9bbd4725`) instead of cloud-init. Spec mentions of "cloud-init `shutdown -P`" in the GCP context should clarify that GCP uses a startup-script path now.
- **NEW-8 (informational).** §1.8 of `specs/provider-shape.md` formalizes the multi-agent contract that the interface always implied. Per-provider tiers:
  - **Tier A (verified, tested):** Modal, Lima, Docker, local.
  - **Tier B (live works since `8cca7406d`; offline mirror missing or capped):** AWS (capped at ~16 agents by EC2's 50-tag wall; raises `NotImplementedError` beyond), Azure/GCP/Vultr/OVH (offline mirror raises `HostNotFoundError` when VPS unreachable — inherited base path).
  - **Tier B-degraded:** SSH — no offline view at all (`libs/mngr/imbue/mngr/providers/ssh/instance.py:208-216` FIXME).
  - **Tier C (single-agent by construction):** none.

### §0.5 Defaults table corrections (round-2 §2 row patches)

| Row | Round-2 said | Current truth | Trigger commit |
|---|---|---|---|
| Azure default image | "Ubuntu" (F-D-12 outlier) | `Debian:debian-12:12-gen2` | `4ae369728` |
| GCP default image | (not specified) | `projects/debian-cloud/global/images/family/debian-12` | `a9bbd4725` |
| Azure `allowed_ssh_cidrs` | `()` fail-closed | `("0.0.0.0/0",)` (matches AWS) | `f58b71939` |
| GCP `allowed_ssh_cidrs` | `()` fail-closed | `("0.0.0.0/0",)` (matches AWS) | `16fdec0cb` |
| AWS `allowed_ssh_cidrs` | `("0.0.0.0/0",)` (outlier) | `("0.0.0.0/0",)` (now the cloud-trio standard) | unchanged |
| Azure managed-VM filter | "scan over `mngr-provider=` prefix" | membership check on `managed-by=mngr` | `c0a38eba6` (refactor, semantically same) |

### §0.6 Reading order

The original round-2 sections below (§1 onwards) are preserved as written on 2026-06-14. Where this §0 status update contradicts a later section, **§0 takes precedence** and the contradictory text below should be read as historical.

---

## TL;DR

The last 3 days landed three improvements: the `auto_shutdown_minutes` → `auto_shutdown_seconds` rename across the VPS-Docker family, `CleanupFailedGroup` as the destroy contract, and `AzureSubscriptionError` replacing bare `ValueError`. **All other prior-review headline findings remain present**, plus several new ones that the equal-depth pass on vultr/ovh/lima/docker/ssh surfaced.

### Cross-round status table — what the prior review flagged

| Prior-review finding | Status as of 2026-06-14 |
|---|---|
| Azure/GCP `--stop-host` silent cost leak (F-STOP-1) | **STILL OPEN.** Neither overrides `stop_host`; inherited base = container-only. |
| Azure `auto_shutdown_seconds` doesn't stop billing (F-D-5) | **STILL OPEN.** OS halt only; VM stays "Stopped (not deallocated)". |
| AWS `allowed_ssh_cidrs = ("0.0.0.0/0",)` open default (F-D-1) | **STILL OPEN.** Unchanged. |
| No auto-snapshot on AWS/Azure/GCP create (F-SNAP-1) | **STILL OPEN.** No `on_agent_created` hook. |
| Idle-driven self-stop only on Modal+AWS (F-STOP-2) | **STILL OPEN.** |
| Vultr/OVH no `pytest_sessionfinish` orphan scanner | **STILL OPEN.** |
| AWS/GCP `ProviderUnavailableError` "start Docker" help text (F-CRED-1) | **HALF-FIXED.** Azure gained curated `user_help_text` via `_azure_unavailable_error` and `AzureSubscriptionError`; GCP gained the same shape via `GcpCredentialsError` / `GcpProjectError`. AWS still falls through to default. |
| Modal has no `mngr modal cleanup` analog | **STILL OPEN.** |
| SSH `supports_shutdown_hosts=True` lie | **STILL OPEN.** Single-line fix. |
| Stopped-host visibility asymmetric (F-LIST-1) | **STILL OPEN.** Only AWS reconstructs from tags. |
| AWS auto-snapshot README contradicts code (F-SNAP-2) | **MORPHED.** The dead-code cleanup in `mngr/separate-snapshots` (`a4c1f7924`) deleted the contradictory client methods. But the README still references them, and **Vultr/OVH READMEs have no snapshot section at all**. |
| Container-shape knobs (`--cpu/--memory/--gpu`) Modal-only | **STILL OPEN.** |
| `start_host(snapshot_id=…)` silent no-op on AWS/Azure/GCP (F-SNAP-3) | **STILL OPEN.** Plus a new variant: `create_host(snapshot=…)` also silently ignored. |
| Modal README has no Setup section | **STILL OPEN.** |
| Docker provider `-p :22` binds `0.0.0.0` | **STILL OPEN.** |
| Auto-shutdown wiring not pinned by tests | **STILL OPEN.** |
| GCP lowercase label folding silently collides mixed-case names | **STILL OPEN.** |
| Build-args help text differs across providers | **STILL OPEN.** |

### New findings this round (equal-depth pass on vultr/ovh/lima/docker/ssh)

| New finding | Severity |
|---|---|
| **N-1.** Vultr/OVH have **no `allowed_ssh_cidrs` field at all** — there's no managed-firewall surface; VPS is internet-reachable on tcp/22 and tcp/2222 the moment it boots. | high (security; sleeper) |
| **N-2.** OVH `destroy_host` semantics are "cancel at expiration", not "destroy now" — VPS keeps running until month boundary. | medium |
| **N-3.** `supports_volumes=True` is a lie on the entire VPS-Docker family. `list_volumes()=[]`, `delete_volume()=pass` (`mngr_vps_docker/instance.py:2215-2219`). | medium |
| **N-4.** `create_host(snapshot=…)` silently no-ops on AWS/Azure/GCP/Vultr/OVH/Docker — only Modal honors it. Sister bug to F-SNAP-3. | high |
| **N-5.** Lima `stop_host(create_snapshot=True)` and `start_host(snapshot_id=…)` parameters are vestigial — silently ignored. | medium-high |
| **N-6.** `CleanupFailedGroup` doesn't capture create-time rollback (Lima `_cleanup_failed_lima_instance`, OVH `_terminate_orphaned_fresh_order`, Azure NIC/IP reclaim). | medium |
| **N-7.** Five cloud-VPS READMEs reference snapshot client methods that `mngr/separate-snapshots` deleted; Vultr/OVH READMEs have no snapshot section. | medium |
| **N-8.** AWS `backend_test.py:84` still constructs `AwsProviderConfig(auto_shutdown_minutes=60)`. Field doesn't exist post-rename. | low (latent breakage) |
| **N-9.** Container port 2222 binds `0.0.0.0` on all VPS providers, but `mngr_vps_docker/config.py:48` comment claims "VPS localhost only" — docs lie. | medium |
| **N-10.** Vultr `build_provider_instance` swallows `ValueError` and constructs with `api_key=""`. On token rotation/typo, provider is silently empty — same fix as AWS/Azure/GCP got, never applied to Vultr. | high |
| **N-11.** OVH `build_provider_instance` never raises `ProviderUnavailableError`; emptiness gated downstream by `is_unconfigured`. | high |
| **N-12.** Modal credential errors break the `Empty / Unavailable` contract — `ModalAuthError` is `PluginMngrError`, wrapped as `ProviderDiscoveryError`, not `ProviderUnavailableError`. | high |
| **N-13.** Docker provider binds `-p :22` → `0.0.0.0:<random>:22` (LAN-reachable). Lima goes opposite extreme. | medium (security) |
| **N-14.** SSH `discover_hosts` hard-codes `HostState.RUNNING` for every configured host — down hosts show as running until user tries to use them. | medium |
| **N-15.** Vultr lists every instance in the account and filters client-side; for a 100-VPS account where 1 is mngr-managed, every `mngr list` pulls every instance. | low |
| **N-16.** Modal uses underscore tag keys (`mngr_host_id`); everyone else uses dashes (`mngr-host-id`). Cross-provider scripts need two code paths. | low |
| **N-17.** Disk-size knob name differs three ways across cloud trio (`root_volume_size_gb` / `os_disk_size_gb` / `boot_disk_size_gb`). User porting `settings.toml` between providers silently picks up default. | low |

### What's now genuinely uniform (preserve)

- `auto_shutdown_seconds` field name shared across all 5 VPS-family providers (even though behavior diverges).
- `CleanupFailedGroup` honored at API + CLI layer uniformly. Adopted in Modal, VPS-Docker base, Lima, Docker.
- Cloud-trio `_validate_provider_args_for_create` is a clean shared extension point with identical pytest guards.
- `AzureSubscriptionError(MngrError, ValueError)` preserves the `except ValueError` wrap into `ProviderUnavailableError`.
- `mngr/separate-snapshots` cleanup deleted 5 dead methods × 5 providers — strict improvement.
- `default_idle_timeout = 800 s` uniform on 8/9 providers (only SSH lacks the field).
- 30 GB default disk + `Public-IP=True` + `auto_shutdown_seconds=None` + `debian:bookworm-slim` container image — uniform across cloud trio + VPS family.
- `mngr.api.list._construct_and_discover_for_provider` uniformly partitions failures into `providers` vs `error_by_provider_name`.
- All three cloud-trio `cleanup` commands refuse-while-resources-exist with consistent error messages.

---

## How to navigate the round-2 docs

- **Top-level synthesis (this file).** Status table + new findings + open questions.
- **`specs/provider-shape.md`** — *prescriptive*. Read first if you're implementing or maintaining a provider. Uses MUST/SHOULD/MAY. 11 sections covering the user contract, capability flags, default values that providers should share, lifecycle hooks, error classification, operator commands, test requirements, anti-patterns observed today, taxonomy, an implementer checklist, and open design questions.
- **`specs/provider-uniformity-review-lifecycle.md`** — *descriptive*. The deep dive on create/stop/start/destroy/cleanup behavior. Has the full lifecycle matrix (9 providers × 6 verbs) and the `CleanupFailedGroup` adoption matrix.
- **`specs/provider-release-tests.md`** — *proposal*. A common release test suite: four multi-step "trips" that amortize the spinup time and walk each provider through the shape doc, with every cross-provider divergence flagged with `xfail` + the round-1/round-2 finding it pins.
- **`specs/provider-uniformity-review.md`** — the original 2026-06-11 round-1 review. Still valid for everything it covered; this round-2 doc supplements rather than replaces it.

---

## Round-2 detail bundle

The five round-2 subagent reports are at `/tmp/provider-review-reports-v2/` (kept off-commit, available for verification):

- `01-lifecycle.md` — covered by the lifecycle spec at `specs/provider-uniformity-review-lifecycle.md`
- `02-discovery-errors.md` — 9-provider discovery, tags, error classification
- `03-defaults.md` — the big 9-provider × 12-default-category table
- `04-snapshots-capabilities.md` — 9-provider snapshot semantics + capability-flag honesty
- `05-tests.md` — 9-provider × 20-behavior test coverage matrix, Top 15 holes

---

## Cross-cutting recommendations (round-2 punch list)

Ordered by impact × ease. Items marked *(carryover)* are from round 1 still open.

### Single-line correctness fixes (very high impact, very low effort)

1. **Flip SSH `supports_shutdown_hosts` to `False`.** `mngr/providers/ssh/instance.py:104-106`. Single character change. Surface: stops the contradiction with `:184-190` raising `NotImplementedError`. *(carryover)*
2. **Change AWS `allowed_ssh_cidrs` default to `()`.** `libs/mngr_aws/imbue/mngr_aws/config.py:127`. Matches Azure/GCP fail-closed pattern. *(carryover)*
3. **Bump `mngr gc` log level on `ProviderUnavailableError`** from DEBUG to WARNING. `libs/mngr/imbue/mngr/api/providers.py:211-213`. *(carryover)*
4. **Fix stale `auto_shutdown_minutes` in `mngr_aws/backend_test.py:84`** → `auto_shutdown_seconds=3600`.

### Curated `user_help_text` (cleanup of half-fixed)

5. **Add `_aws_unavailable_error` and `_gcp_unavailable_error`** mirroring `_azure_unavailable_error` (`libs/mngr_azure/imbue/mngr_azure/backend.py:36-53`). Or hoist the pattern into `ProviderUnavailableError` as a per-backend hook. *(carryover, half-fixed)*
6. **Make Modal `ModalAuthError` raise `ProviderUnavailableError`** with curated help text, so it joins the cloud trio's contract.
7. **Replace Vultr's silent-empty-on-missing-creds with `ProviderUnavailableError`.** Same shape AWS/Azure/GCP got. *(N-10)*
8. **OVH `build_provider_instance` should raise `ProviderUnavailableError`** when creds are unconfigured, not silently return an empty provider. *(N-11)*

### Lifecycle + cost safety

9. **Override `stop_host` on Azure/GCP/Vultr** to actually stop the VM — OR set `supports_shutdown_hosts = False` until that lands. *(carryover, F-STOP-1)*
10. **Implement Azure managed-identity self-delete after `auto_shutdown_seconds`** so the field's semantics match AWS/GCP. *(carryover, F-D-5)*
11. **Add `pytest_sessionfinish` orphan scanner to Vultr/OVH.** Copy AWS pattern at `mngr_aws/conftest.py:106-134`. OVH is the high-cost case (monthly billing). *(carryover)*
12. **Override `_validate_provider_args_for_create` on Vultr/OVH** to require `auto_shutdown_seconds` in pytest. *(carryover)*
13. **Port AWS idle watcher (sentinel + systemd `.path` unit) to GCP and Azure.** Or document the gap loudly in each README. *(carryover)*

### Snapshots + capability honesty

14. **Either honor `start_host(snapshot_id=…)` and `create_host(snapshot=…)` on AWS/Azure/GCP/Vultr/OVH/Docker, or raise `SnapshotsNotSupportedError`.** Silent no-op is the worst option. *(carryover + N-4)*
15. **Override `supports_volumes` to `False` on the VPS-Docker family** until `list_volumes`/`delete_volume` actually do something. Or distinguish "host has volume mounts" from "provider exposes managed-volume CRUD". *(N-3)*
16. **Add a `supports_persistent_snapshots` flag** to honestly distinguish Modal (snapshots survive `destroy_host`) from cloud-VPS (`docker commit` on a single host, lost on destroy). Or document the difference in `mngr snapshot --help`. *(carryover)*
17. **Reconcile cloud-VPS READMEs with deleted snapshot client methods.** AWS/GCP/Azure all still reference the deleted methods; Vultr/OVH have no snapshot section. *(N-7)*

### Networking + security defaults

18. **Build firewall integration for Vultr/OVH**, or document loudly that VPS is internet-reachable as soon as it boots. *(N-1)*
19. **Bind `container_ssh_port = 2222` to `127.0.0.1`** on the VPS, and have cloud-trio firewall rules only open tcp/22. Or fix the misleading "VPS localhost only" comment at `mngr_vps_docker/config.py:48`. *(N-9)*
20. **Bind Docker provider `-p :22` to `127.0.0.1::22`** by default. *(N-13)*
21. **Warn at provider load when two GCP-targeted provider names lowercase-fold to the same string.** *(carryover, F-DLT-7)*

### Discovery + visibility

22. **Lift AWS's `_discovered_host_from_tags` + `_offline_host_from_tags` + `discover_hosts_and_agents` triad into `VpsDockerProvider`** as overridable hooks. So when Azure/GCP/Vultr/OVH grow VM-level stop, stopped-host visibility follows automatically. *(carryover, F-LIST-1)*
23. **Make SSH `discover_hosts` probe reachability** (parallel TCP-connect with 2s timeout) instead of hard-coding `HostState.RUNNING`. *(N-14)*
24. **Vultr should filter via API tag instead of pulling every account instance** if the Vultr API supports it. Cosmetic. *(N-15)*

### Tests (pin the above)

25. **Add per-provider capability-flag pinning tests.** One 4-line test per provider modeled on `mngr_lima/instance_test.py:23`. Catches silent inherited drift. *(carryover, expanded)*
26. **Add `test_create_instance_passes_auto_shutdown_to_*`** for AWS/Azure/GCP/Vultr/OVH/Modal. *(carryover)*
27. **Add `CleanupFailedGroup` raise-on-partial-failure tests** for each provider's destroy path. Zero hits today. *(N-6, expanded)*
28. **Promote one happy-path lifecycle test per provider from release-tier to acceptance-tier** so default CI exercises `mngr create --provider <X>`. Today AWS/Azure/GCP/Vultr/OVH/GCP lifecycle is release-only — CI from forks never runs these.

### Conventions / consolidation

29. **Standardize disk-size knob name across cloud trio** as `root_disk_size_gb`, or document the alias. *(N-17)*
30. **Migrate Modal tag keys from underscores to dashes** with a backward-compat read path for existing sandboxes. *(N-16)*
31. **Promote OVH's `mngr <provider> list` operator-CLI pattern** to a pluggy convention; ship `mngr aws list`, `mngr gcp list`, `mngr azure list`, `mngr vultr list`. *(carryover)*

---

## Open questions for the human reviewer

1. **Should `VpsDockerProvider.supports_shutdown_hosts` default to `False`** so subclasses must explicitly opt in once they implement real VM-level stop? Today the inherited `True` is what creates the Azure/GCP/Vultr cost leak.
2. **Should `CleanupFailedGroup` cover create-time rollback** as well as destroy? Today the contract is destroy-only; Lima/OVH/Azure NIC-IP have create-time partial failures that bypass it.
3. **Should `supports_persistent_snapshots` exist as a separate flag** to distinguish Modal-style portable snapshots from cloud-VPS docker-commit?
4. **Should AWS `allowed_ssh_cidrs = ()` default land, even though Vultr/OVH have no firewall surface and are wide-open by default?** Vultr is the "battle-tested" precedent; if we want fail-closed everywhere, Vultr/OVH need firewall integration too.
5. **Should `_validate_provider_args_for_create` move into `ProviderInstanceInterface`** so every provider must opt in or explicitly opt out, rather than the `VpsDockerProvider`-only no-op default?
6. **Should `mngr` reserve `auto_shutdown_seconds` as a hard cost-stop contract** that providers must implement honestly, or keep it as a "best-effort, semantics per provider" knob?
7. **For the `_cleanup_after_create_failed` symbol referenced in prior review and code review:** does not exist. Worth renaming the existing patterns (`_cleanup_failed_lima_instance`, `_terminate_orphaned_fresh_order`, Azure NIC reclaim) into a uniform base hook?
8. **Modal/Lima/Docker/SSH lack `prepare`/`cleanup` analogs** while the cloud trio + OVH have them. Should this be a `ProviderBackendInterface` contract or stay as an optional per-provider feature?

# Unabridged Changelog - dev

Full, unedited changelog entries consolidated nightly from individual files in `dev/changelog/`. Covers repo-level dev tooling: CI workflows, repo scripts, top-level configuration, build tooling, ratchets, and the changelog tooling itself.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-25

Bumped the offload CI pin in `.github/workflows/ci.yml` from `0.9.7` to `0.9.9` (the latest release), updating the cargo cache key, the version check, and the `cargo install` invocation.

Add the blueprint planning document for the minds error-reporting & "get help" work (`blueprint/minds-error-reporting-help/`), which scopes the full four-phase design (consent + settings, report-a-bug + API, in-workspace agent help, out-of-workspace agent help). Phases 1-2 are implemented in this PR; phases 3-4 will follow as stacked PRs.

`just minds-start` no longer defaults the workspace name to `mindtest`. Its `agent_name` argument now defaults to empty, so a plain `just minds-start` leaves `MINDS_WORKSPACE_NAME` unset and the create form generates an automatic `mind-N` name -- matching what a shipped binary does. Pass a name (`just minds-start my-agent`) to pin the workspace name explicitly; a pinned name is used verbatim and a collision errors at create time rather than being auto-suffixed to `mindtest-2`.

Changed: The nightly changelog consolidation automation now merges its PR immediately instead of leaving it for a human to review and merge. The in-run accuracy review remains the quality gate.

Added a design doc, `specs/tmr-bounded-convergence-and-normalization.md`, for improving the e2e tests TMR generates: bounding per-test complexity via a tutorial-anchored convergence objective (with deletion as a first-class action), and a suite-level normalization stage in the reducer (utility extraction gated on the tutorial-1:1 predicate, plus a FIXME-resolution/escalation lifecycle verified on offload). No build or tooling behavior change.

Added a design blueprint (`blueprint/gateway-agent-id-validation/`) documenting the decision to reject a malformed permission-request `agent_id` at the latchkey gateway (the agent's HTTP tool call) instead of only guarding against it defensively on the consumer side.

## 2026-06-24

Removed the minds app's `postinstall` CSS-compile hook (it broke ToDesktop's `--prod` cloud install). To cover the one path that compiled CSS only as an install side-effect -- the `minds_electron` end-to-end test, which launches `electron main.js` directly rather than through `pnpm start` -- the CSS build is now wired in explicitly: a "Build Tailwind CSS for the e2e app" step in the CI e2e job, and a `minds-css` dependency on the `just minds-test-electron` recipe.

`scripts/install.sh` now runs `mngr config wizard` as a final step to populate common user-scope configuration (e.g. whether to isolate the Claude config dir for local agents). Like the other steps, it prompts before changing anything and is safe to re-run.

`scripts/push_vault_from_file.py` now writes each declared key as its own single-`value` leaf at `secrets/minds/<tier>/<service>/<KEY>` (the new "split" Vault secret layout) instead of a single flat KV entry with many fields.

Added `scripts/remove_old_flat_vault_secrets.py`, a one-off cleanup tool that deletes the old flat per-service Vault entries for a tier (`secrets/minds/<tier>/<service>`) once they have been mirrored into the split layout. It refuses to delete any entry whose split mirror is missing or whose keys/values disagree, defaults to a dry-run, and requires `--yes` to actually delete.

`scripts/changelog_deploy.sh` now reads its `GH_TOKEN` / `ANTHROPIC_API_KEY` from the split layout (`secrets/mngr/dev/github/GH_TOKEN` and `secrets/mngr/dev/anthropic/ANTHROPIC_API_KEY`, value under `.data.data.value`) instead of the old flat entries.

## 2026-06-23

Added the discovery-health-watchdog design doc (`blueprint/discovery-health-watchdog/plan-discovery-health-watchdog.md`): an app-global watchdog that detects a stalled or dead workspace-discovery pipeline (via snapshot-freshness age for a producer stall and the consumer's unexpected-exit signal), self-heals it with a cheap-to-heavy producer re-kick ladder (supervisor bounce, then restart), and surfaces a terminal app-level "blocked" recovery screen when self-healing is exhausted.

Updated the remote-mind-recovery blueprint (`blueprint/remote-mind-recovery/plan-remote-mind-recovery.md`): collapsed the recovery page's redundant second sampler of host/provider state onto the passive discovery resolver, then revised the post-outage handling. Rather than a transient `REACHABILITY_UNCONFIRMED` recovery tier plus a client-side convergence loop, the redirect to the recovery page is now gated on fresh discovery, so the recovery page always classifies from trustworthy state. A further revision section collapses the two backend-unreachable tiers (`PROVIDER_UNAVAILABLE` / `WORKSPACE_UNREACHABLE`) into a single `BACKEND_UNREACHABLE` tier.

Add the `traceback-with-variables` dependency to the workspace (used by the minds Sentry integration to format tracebacks with local variables), which updates the root `uv.lock`.

Added a `just minds-start-cloud` recipe for launching the minds desktop client in dev mode to test the `imbue_cloud` provider against pre-baked pool slices.

Unlike `just minds-start` (which targets local Lima/Docker dev and forces local-worktree create-form defaults), `minds-start-cloud` leaves the form's shipped fallbacks in place -- the canonical forever-claude-template remote plus `FALLBACK_BRANCH` -- so an `imbue_cloud` create matches and fast-path leases a slice baked at that tag instead of dropping to the slow rebuild path. It also skips the live-mngr to `vendor/mngr/` rsync, which is irrelevant to an already-baked pool slice.

Added a `just backfill-pool-host-keys` recipe that wraps `minds pool backfill-host-keys` for the activated minds env -- the one-time SSH host-key backfill to run once per tier after deploying the host-key-pinning connector.

Consolidated the docs describing how FCT's `vendor/mngr` is kept in sync, so the `git archive` (release) vs `rsync` (dev/bake) mechanisms are explained in one canonical place (`apps/minds/docs/vendor-mngr-sync.md`) instead of being re-described in each skill.

Trimmed the duplicated rsync-form explanation out of the `minds-dev-workflow` skill and pointed the `minds-justfile` skill's `sync-vendor-mngr` entry at the canonical doc. Also corrected the `minds-dev-workflow` skill's "Editable installs" note to attribute `uv tool install -e` to `scripts/build_workspace.sh` (which the Dockerfile invokes) rather than the Dockerfile directly, and to name the actual mngr install target (`vendor/mngr/libs/mngr`).

Added the implementation plan `blueprint/pin-imbue-cloud-host-keys/` for removing trust-on-first-use (TOFU) from the imbue_cloud pool flow by pinning all SSH host keys.

Added an optional `fct` argument to the `just minds-start` recipe, letting you point a launch at a specific forever-claude-template worktree (the directory synced into `vendor/mngr/` and exported as `MINDS_WORKSPACE_GIT_URL`) instead of the fixed `.external_worktrees/forever-claude-template`. An absolute path is used as-is; a relative one is resolved against the mngr root. Omitting it keeps the previous default, so existing invocations are unchanged.

Rewrote the `release-minds` skill to be a thin pointer to `apps/minds/docs/release.md`, the canonical release runbook, instead of describing its own (now-obsolete) release flow.

The old skill assumed a `~/project/minds_prod` clone and a long-lived `minds_v<version>` release branch, and walked through a manual `vendor/mngr` sync — none of which matches the documented process (release from `main` via two PRs, `just sync-vendor-mngr`, and `minds-v<version>` tags on both repos). The skill now resolves the target version from its args and defers entirely to the runbook, so the release process is maintained in exactly one place.

## 2026-06-22

Renamed the `just minds-tailwind` recipe to `just minds-css`: it now compiles the minds desktop client's Tailwind v4 stylesheet (`static/app.css` -> minified `static/app.min.css`) via the pinned `@tailwindcss/cli`, instead of fetching the Tailwind Play CDN JS bundle. The `.gitignore` entry tracks the new compiled artifact (`app.min.css`) in place of the retired `tailwind.js`.

- Added the design blueprint for handling unauthenticated providers consistently
  under `blueprint/consistent-provider-auth-failures/`.

- The `forward-system-interface` justfile recipe now resolves an agent's id with
  `mngr list --on-error continue`, so an unauthenticated/unreachable provider no
  longer aborts the lookup of a local agent.

- Added the design blueprint for robust provider-error handling across minds
  discovery and `mngr list` callers under
  `blueprint/robust-minds-list-provider-errors/`.

Added an implementation blueprint for the minds desktop client's FastAPI-to-Flask migration under `blueprint/minds-flask-migration/`.

Added a `blueprint/` planning document (`blueprint/remove-system-interface-asyncio/`) capturing the implementation plan for migrating the Minds app's system interface off asyncio (FastAPI -> Flask + flask-sock, served by a threaded Werkzeug HTTP/1.1 server). The migration itself is implemented in the `forever-claude-template` repo, not in this monorepo.

Added the design plan document for unifying the `mngr start` host lock with the cooperative host lock (`blueprint/unify-remote-host-lock/`). No runtime or tooling behavior changes at the repo-root level.

## 2026-06-21

- Added the design blueprint for the sshd / agent restart-robustness work under
  `blueprint/sshd-restart-robustness/`.

Added the implementation plan for sharing a bare-metal slice box across developer environments under `blueprint/share-bare-metal-across-dev-envs/`.

## 2026-06-20

Removed the `bake-pool-host-{dev,prod}` justfile recipes (they baked OVH classic VPS pool hosts, now deprecated). Pool hosts are baked as bare-metal slices via the `bake-slice-{dev,prod}` recipes; `list-pool-hosts` and `destroy-pool-host` are unchanged and still cover legacy OVH VPS rows. The `minds-justfile` and `minds-dev-workflow` skill docs were updated to match (slice bakes are the documented path; OVH VPS baking is marked deprecated).

## 2026-06-19

Added a design doc for extending minds' workspace-recovery flow to remote (Imbue Cloud) minds: a blueprint plan under `blueprint/remote-mind-recovery/`. No build/tooling behavior change.

Added `specs/bare-providers/` (spec.md + concise.md): a design proposal for
running agents directly on a cloud VM with no Docker container, as a second
config-selected shape of the aws/gcp/azure providers. Introduces a
substrate-x-realizer architecture (a `HostRealizer` seam injected like the existing
`VpsClient`) so "with Docker" vs "without Docker" becomes a reusable axis rather
than a per-cloud class matrix, with a staged rollout that later folds
local/docker/lima/ssh into the same grid. Also adds `specs/uncertainties.md` noting
that the bare mode supersedes the "single mode of operation" framing in
`specs/vps-docker-provider/spec.md`, and `specs/bare-providers/extraction_design.md`
giving the implementation-level `HostRealizer` seam contract, state-ownership
split, host-record evolution, and per-method migration for Stage 1.

Updated the root pytest coverage config to track the renamed `imbue.mngr_vps`
package (was `imbue.mngr_vps_docker`).

Registered the new `overlay` workspace library in the root `pyproject.toml`: added it as a `[tool.uv.sources]` workspace source, and added `--cov=imbue.overlay` to the shared coverage flags so the library is measured in the offload combined-coverage gate (and the `test_top_level_cov_flags_are_union_of_subproject_cov_flags` meta-ratchet stays satisfied).

Added a new runtime dependency at the repo root (recorded in ``uv.lock``): ``google-cloud-storage>=2.18``, used by the GCP provider's new offline ``host_dir`` GCS state bucket. No top-level config or build tooling changes; the dependency is declared in ``libs/mngr_gcp/pyproject.toml`` and propagates to the lockfile.

Removed a monorepo-development-only paragraph (the `~/.local/bin` pre-commit shim note) from the top-level README so the published PyPI README stays focused on user-relevant content.

`make_cli_docs.py` now also generates the provider/agent config tables in each plugin README from the Pydantic field descriptions (the source of truth, also shown by `mngr config`), spliced between markers and verified by the docs `--check` gate so the tables can no longer drift from the code.

The `regenerate-cli-docs` pre-commit hook now runs `make_cli_docs.py --check` (non-mutating, covering every generated file) instead of regenerating in place and diffing only the mngr command docs, and its trigger now includes the provider/agent `config.py` / `plugin.py` sources and generated provider READMEs. Previously, drift in the generated provider README tables could slip past the hook.

The provider/agent config tables are rendered entirely from the model: each table only names its config class and which inherited base fields to also surface, and the field names, defaults, and descriptions are derived automatically (a small per-field override covers non-literal defaults like "gcloud/ADC default"). A field added to a config model now appears in its table automatically, so it can't silently vanish.

Vendored `specs/provider-release-tests.md` (the common provider release-test suite proposal, originally authored in PR #2142) into the tree so the new provider release harness ships alongside the design doc its docstrings cite as the source of truth for the trip definitions and the still-owed future trips.

## Provider specs: cross-provider review + prescriptive shape doc, accurate against current code

Added a set of provider specs under `specs/` covering all nine `mngr` provider plugins (modal, aws, azure, gcp, vultr, ovh, lima, docker, ssh), then brought them into agreement with the `mngr/bare-providers` merge and verified every claim and citation against the current code. Common to all: the package/class rename (`mngr_vps_docker` -> `mngr_vps`, `VpsDockerProvider` -> `VpsProvider`, `VpsDockerHostRecord` -> `VpsHostRecord`, `VpsDockerHostStore` -> `VpsHostStore`, `VpsDockerError` -> `VpsError`, `MinimalVpsDockerProvider` -> `MinimalVpsProvider`) and the realizer/isolation architecture (a `HostRealizer` seam with `DockerRealizer` for `isolation=CONTAINER` and `BareRealizer` for `isolation=NONE`, selected by config). Citations are file+symbol (no line numbers, which rot on every refactor).

- `specs/provider-uniformity-review.md` (descriptive current state): cross-provider review with a ranked findings table, six lifecycle matrices (create / stop / stop --stop-host / start / destroy / cleanup), a `CleanupFailedGroup` adoption matrix, defaults table, tag/label conventions, error-classification table, snapshot matrix, build-args table, test-coverage gaps, and a recommendations punch list. Re-ranked the findings (the `--stop-host` leak, Azure auto-shutdown billing, idle self-stop, and stopped-host visibility are now resolved for the cloud trio, open only for Vultr/OVH) and added an isolation/realizer section and the state-bucket discovery story. Corrected against current code: machine stop/start is layered via the base `OfflineCapableVpsProvider` plus per-cloud `_pause_cloud_instance`/`_resume_cloud_instance` hooks (no subclass `stop_host` overrides); curated `ProviderUnavailableError` help text is uniform across AWS/GCP/Azure and Modal raises `ProviderUnavailableError` on missing creds (both former findings resolved); dropped the removed agent-tag-mirror subsystem (`TagMirrorVpsProvider`, per-agent EC2 tags, agent caps) in favor of the required state bucket; `supports_snapshots` is realizer-class-derived (`isinstance(_realizer, SnapshotCapableRealizer)`); cloud-trio release coverage is the shared `run_provider_release_trip{1..4}` harness over CONTAINER and bare.

- `specs/provider-shape.md` (prescriptive contract): what an `mngr` provider OUGHT to look like -- user contract (including the N-agents-per-host invariant), capability-flag honesty, shared defaults (security/CIDR, idle, auto-shutdown, resources, regions, sizes, images, tags, keys, exposure), lifecycle override hooks, error classification, operator commands, test requirements, anti-patterns, taxonomy table, implementer checklist, and open design questions (MUST/SHOULD/MAY). Incorporated the realizer/isolation contract (bare isolation requires a real machine stop/start lifecycle or must reject `isolation=NONE` via `BareIsolationNotSupportedError`; bare placements are not snapshot-capable). Corrected against current code: all three clouds extend `OfflineCapableVpsProvider` directly (the removed agent-tag-mirror subsystem and `TagMirrorVpsProvider` are gone); offline reconstruction is backed by an external `HostStateStore` (required state bucket on AWS/Azure, GCE metadata on GCP) with no agent cap; the cloud-trio `allowed_ssh_cidrs` default is uniformly open `("0.0.0.0/0",)` with a warning (key-only SSH is the control; no AWS-vs-trio posture mismatch); curated `ProviderUnavailableError` help text is uniform across the trio. Split the anti-patterns into resolved (Azure auto-shutdown billing, `--stop-host` leave-VM-running on GCP/Azure, stopped-host discovery dropping providers, AWS/GCP default help text, Modal wrong-error-class on missing creds) and still-real (SSH `supports_shutdown_hosts` lie, `supports_volumes` True-but-`[]`, snapshot-arg no-op, Docker `0.0.0.0:22`, Modal underscore tags).

- `specs/implementing-a-provider.md` (dev guide): walkthrough for adding a new provider plugin, organized around user-visible behaviors, plus a Common Gotchas section. Added the realizer-seam and isolation-mode guidance and refreshed the stop/start, capability-flag, N-agents, and cost-safety sections. Corrected against current code: the realizer factory is `_realizer_for_isolation` (not `_build_realizer`); all three clouds extend `OfflineCapableVpsProvider` directly (no `TagMirrorVpsProvider`); machine stop/start is via the `_pause_cloud_instance`/`_resume_cloud_instance` hooks; the idle-watcher machinery (`_create_shutdown_script`/`_install_idle_watcher`) lives in the base, not the AWS/Azure backends; `supports_snapshots` is realizer-class-derived; Modal/AWS/GCP credential errors are now contract-compliant; dropped the removed per-agent EC2 tag mirror and its agent cap.

- `specs/provider-release-tests.md` (release-test proposal): originally proposed a common release-test suite of five multi-step "trips" (full lifecycle + sketchy kill + GC; second-agent-on-same-host; idle auto-shutdown; snapshot-survives-destroy; error classification). Since that proposal has landed, condensed the doc to a remaining-gaps tracker: the shared `run_provider_release_trip{1..4}` harness and the AWS/GCP/Azure coverage (parametrized over container and bare isolation) are cited as implemented, and the doc retains only the confirmed-open gaps (Vultr/OVH and Lima/Docker/SSH not on the harness, the N-agents Trip 1b, a standalone offline-host_dir trip, `supports_volumes` honesty, `--snapshot`-at-create on the VPS family, and the container-ingress probe).

- `specs/cleanup-error-aggregation.md`: cite/naming refresh only (the core model is unchanged) -- VPS-family container teardown now lives behind `DockerRealizer.teardown_placement`, the already-gone helper is `is_vps_resource_already_gone`, and the provider count was corrected; left the separate local-docker `DockerHostStore` path untouched.

- Documented Vault setup for pool/slice bakes in the `minds-dev-workflow` skill: bakes need an interactive `vault login -method=oidc`, the minds wrappers auto-apply the imbue HCP `VAULT_ADDR`/`VAULT_NAMESPACE` defaults (from `vault_reader.py`) so they work with just the token, and raw `vault`/`mngr imbue_cloud admin` commands need those two exported (a `127.0.0.1:8200` "connection refused" means a missing address, not a logged-out session).

## 2026-06-18

The `identify-*` skills (`identify-doc-code-disagreements`, `identify-inconsistencies`, `identify-outdated-docstrings`, `identify-style-issues`) now accept a `target_path` argument instead of a bare library name. You can scope them to a whole library (`libs/mngr` or just `mngr`) or to any subdirectory within one (e.g. `libs/mngr/imbue/mngr/cli`). Each skill resolves the scan scope and its containing library, gathers the containing library's context, and writes findings to the containing library's `_tasks/` folder.

Added a new `identify-suspicious-edge-cases` skill that flags over-broad exception catches, fallback `else` branches, defensive guards, and unnecessary `| None` types under a given path.

Add a design spec (`specs/provider-state-bucket/`) for giving the AWS and Azure providers a cloud object-storage bucket (S3 / Azure Blob) that holds mngr control-plane state, so a stopped instance's host record, agent metadata, and `host_dir` are all readable offline without hitting the 256-char EC2/VM tag-value limit.

The spec covers: `prepare`/`cleanup` creating and tearing down the bucket plus a best-effort bucket-write identity (AWS IAM instance profile / Azure managed identity, provisioned when the `is_offline_host_dir_enabled` provider config field is on); moving the per-agent tag mirror into the bucket via the existing `persist_agent_data` / `list_persisted_agent_data_for_host` hooks; and an on-by-default `host_dir` offline volume backed by an on-box sync daemon (instance-push) read back through `get_volume_for_host()` -> `OfflineHostWithVolume`. GCP is intentionally out of scope (its per-instance metadata allowance is sufficient).

Added `moto[s3]` to the root dev dependency group for in-memory S3 unit tests of the new AWS state bucket.

## 2026-06-17

Added a design doc (`specs/agent-plugin-parity/capability-mixins.md`) proposing a code-derived agent capability taxonomy: capability mixins plus a registry that generates the parity matrix from the agent classes, replacing the hand-maintained table and guarding against doc/code drift.

Added `scripts/make_agent_capabilities_doc.py`, the dev-only generator for the code-derived agent capability matrix doc. It loads every installed mngr plugin (local backend only, so no docker/modal SDKs), builds the matrix from the agent classes + their plugins, and either rewrites `libs/mngr/docs/concepts/agent_capabilities.md` or, with `--check`, fails if it is stale. This mirrors `scripts/make_cli_docs.py` and keeps the generator out of the shipped `mngr` wheel (it has no runtime importers); the capability mixins it detects remain in `imbue.mngr.interfaces.agent`. The registry/detection logic and its tests moved here from the package (`scripts/make_agent_capabilities_doc_test.py`).

Added a `just regenerate-agent-capabilities-doc` recipe that runs the generator (`uv run python scripts/make_agent_capabilities_doc.py`).

Removed the throwaway synthetic-base doc (`dev/agent-mixins-synthetic-base.md`); the synthetic base branch is no longer needed.

Updated the capability-mixins design doc to match what shipped: the three-state `Y`/`-`/`n/a` matrix with the code-derived `CapabilityScope` model, the positive `CliBackedAgentMixin` kind marker, the unified `live_output` capability, and the `session_resume` capability (the original doc forbade `n/a` entirely).

The `just destroy-pool-host` recipe comment now documents that teardown mirrors the row's backend -- cancelling the OVH VPS for an `ovh_vps` row, or destroying the lima VM (freeing the box slot) for a `slice` row -- and that `--skip-vps-cancel` is for when the underlying machine is already gone (not just the OVH VPS).

Added `just bake-slice-dev` and `just bake-slice-prod` recipes for baking bare-metal slices (lima/QEMU VMs carved on a pre-registered, prepped OVH bare-metal box) into the minds pool.

They are thin wrappers over `minds pool create --backend slice` (which resolves the tier's pool key, and the host_pool DSN for shared tiers, from Vault), mirroring the existing `bake-pool-host-{dev,prod}` recipes for OVH VPSes -- the only difference is the backend. Both require an activated minds env and `vault login` first.

Updated the agent capability-matrix generator's test (`scripts/make_agent_capabilities_doc_test.py`) to expect the `session_resume` capability on every interactive agent (claude, codex, opencode, pi-coding, antigravity), not just claude. This reflects session adoption being generalized from a claude-only feature to a shared capability declared by `HasSessionAdoptionMixin`, which the generated `agent_capabilities.md` matrix now reports for all five interactive agents.

Added a design spec (`specs/gcp-azure-stop-start-lifecycle/spec.md`) for bringing
the AWS stop/start (idle-pause + resume) lifecycle to the GCP and Azure providers:
`mngr stop` halts live-instance compute billing (disk preserved), `mngr start`
resumes the session with all files intact, and a stopped VM stays visible in
`mngr list` and resumable by name.

Updated the agent-capability-doc generator's test (`scripts/make_agent_capabilities_doc_test.py`) for the live-output mixin unification: its TUI-snapshot fixture now inherits `SupportsLiveOutputMixin` directly, since the `HasStreamingSnapshotMixin` it used was removed when the TUI snapshot and headless streaming surfaces were unified onto one `live_output` capability.

Added a design spec (`specs/common-transcript-standard/spec.md`) for tracking the OpenTelemetry GenAI semantic conventions in the agent-agnostic common-transcript schema instead of bespoke field names: a vocabulary alignment (`stop_reason` -> `finish_reason`) across all five emitters, and a universal ordered `parts[]` field that every emitter fills (with a `parts_ordered` flag marking antigravity's best-effort order), so the reader renders one uniform shape with no per-agent fallback.

Recorded the change in the agent-plugin parity reference (`specs/agent-plugin-parity/spec.md`): a new "Ordered assistant parts[]" row in the capability matrix and a note in the transcript-capture dimension.

## 2026-06-16

Regenerated `uv.lock` to match the version revert of `imbue-mngr-opencode-usage` and `imbue-mngr-pi-coding-usage` back to 0.1.0.

## 2026-06-16

Added `specs/agent-usage-plugins/spec.md`: a design spec for extending `mngr usage` cost/usage tracking beyond Claude to the OpenCode, pi, and Codex harnesses. The spec generalizes the usage event schema to report raw token counts (with the reader deriving and provenance-flagging cost via a canonical pricing table), keeps dollars as the cross-harness comparable unit, and lays out three thin per-harness writer plugins. Antigravity and the Claude-subagent-proxy are documented as out of scope. The
per-harness data exposure was verified against the locally installed harnesses
(OpenCode 1.16.2, Codex 0.138.0, pi 0.79.1): OpenCode reports cost+tokens
directly; Codex's `token_count` events expose cumulative tokens plus rate-limit
windows (so Codex subscription agents get Claude-style windows as a bonus). A
live two-turn `pi-coding` agent confirmed pi reports cost natively
(`usage.cost.total`, matching the canonical Anthropic prices exactly) with
non-overlapping cache-exclusive token buckets, so pi is reported-cost (estimate
only as a fallback), leaving Codex as the only purely token-derived harness.

Documented the install-wizard surfacing of the usage plugins: added an "Install-wizard recommendation" section to `specs/agent-usage-plugins/spec.md`, and recorded the antigravity gap (the one agent type with no usage provider, so the wizard offers none for it) in the `specs/agent-plugin-parity/spec.md` current-state matrix (new "Usage tracking plugin" row) and its observations.

Extended the local-scratch gitignore convention to Python and text files: `**/*.local.py` and `**/*.local.txt` are now ignored, mirroring the existing `**/*.local.md` and `**/*.local.sh` patterns. Lets one-off validation harnesses and probe scripts (named `whatever.local.py` / `whatever.local.txt`) stay untracked and survive the stop hook's working-tree cleanup.

Add a design spec (`specs/aws-ec2-stop-start-lifecycle/`) for giving AWS agents a Modal-like idle-paused-but-resumable lifecycle via native EC2 stop/start (instead of EBS snapshots). Phases 1 (native EC2 instance stop/start), 2 (the self-stopping idle watcher), and 4 (offline EC2-tag discovery so stopped hosts stay resumable by name) are marked implemented.

The idle watcher is a host-side systemd path unit that powers the host off (`shutdown -P now`) when an in-container sentinel goes stale; EC2's `InstanceInitiatedShutdownBehavior` (new `terminate_on_shutdown` config flag, default `stop`) decides whether that shutdown stops the instance (resumable via `mngr start`, EBS-only cost) or terminates it. The spec documents the single-flag tradeoff (resumable-on-idle OR self-terminating, not both) in Decision #3, plus the `prepare`/`cleanup` permission notes.

## Azure provider wiring

- Added `--cov=imbue.mngr_azure` to the root pytest coverage config so the new `mngr_azure` package is covered alongside the other provider plugins. The package is picked up automatically by the `libs/*` uv workspace glob.

- Registered the `azure` command group in `scripts/make_cli_docs.py` (`SECONDARY_COMMANDS`) so `mngr azure` gets a generated doc page, alongside `aws` / `gcp`.

- The `azure` create template now builds the project Dockerfile on the VM (so azure agents get `gh` and the full mngr toolchain) instead of coming up on a bare `debian:bookworm-slim` base. It mirrors the `gcp` template: `build_arg = ["--azure-vm-size=...", "--file=libs/mngr/imbue/mngr/resources/Dockerfile", "."]` -- the context is the worktree, which the shared `mngr_vps_docker` build flow clones (overlaying uncommitted changes) and uploads, resolving `--file` inside it. Also forwards `GH_TOKEN` + runs `gh auth setup-git` (via the `github_setup` window), sets `agent_args=--dangerously-skip-permissions` and `target_path=/code/mngr`.

- `[providers.azure] builder = "DEPOT"` builds on depot's cached remote builders (like `gcp`) so azure creates after the first reuse cached layers instead of building cold. Requires `DEPOT_TOKEN` exported at `mngr create -t azure` time (read from the create shell, not `pass_env`); `depot.json` in the repo supplies the project id. Drop the block to fall back to a native `docker build` on the VM.

Synced the root design specs to the removed VPS-client snapshot surface: `specs/vps-docker-provider/spec.md` and `concise.md` no longer declare `create_snapshot` / `delete_snapshot` / `list_snapshots` on `VpsClientInterface`; `specs/ovh-vps-provider/spec.md` drops the OVH snapshot-wrapper bullet and its snapshot test scenarios; `specs/azure-provider/concise.md` drops the managed-disk-snapshot client bullet; and `specs/aws-ec2-stop-start-lifecycle/spec.md` no longer says the `AwsVpsClient` snapshot methods exist-but-unwired.

Also synced the matching `list_ssh_keys` references (removed alongside the snapshot methods): `specs/ovh-vps-provider/spec.md` no longer lists `list_ssh_keys` as a client method, and `specs/azure-provider/concise.md`'s method count drops from ~11 to ~7.

Added `specs/cleanup-error-aggregation.md`, a design spec for making `mngr stop`/`destroy`/`cleanup` aggregate and classify failures (benign "already gone" vs. real "resource left behind"), with cause-specific exit codes, across both the stop and destroy paths.

`minds-launch-to-msg.yml`: show the ref name **and** the resolved commit, not the tag object.

The Slack notification and step summaries resolved `commit_sha` / `template_ref` with `git ls-remote refs/tags/<tag>` (no peel), so a run against an **annotated** tag (e.g. `minds-v0.3.1`) displayed the tag-*object* SHA — a SHA you can't `git checkout` and that doesn't match the commit the run actually built. The `check_should_run` compute step now peels annotated tags (`^{}`) to the commit they point at (also making the pair-key / marker cache consistent between tag and SHA reruns), carries the input ref through as new `mngr_ref` / `fct_ref` outputs, and the Slack line + should-run summary now render `` `<ref>` (`<commit>`) `` — e.g. `` `minds-v0.3.1` (`d05797429`) ``. Raw-SHA inputs still render just the commit. The `launch_to_msg` job's own `resolve FCT template ref` step (its per-job summary) is peeled the same way, so no step surfaces a tag-object SHA anymore.

`justfile`: realign the `sync-vendor-mngr` recipe with the current release flow — its comment now tells you to position the mngr checkout at the **verified release SHA** (not blindly `main`, which can drift past it), points at `apps/minds/docs/release.md` instead of the stale `release-minds` skill, and **no longer hardcodes a personal FCT path** — the FCT checkout path comes from the positional arg, else `FCT_DIR` read from a gitignored, **minds-scoped** `apps/minds/.env` (template: committed `apps/minds/.env.example`), else `$FCT_DIR` in your shell. No shell-rc edit, it reaches non-interactive agent shells, nothing personal is committed, and **only this recipe** loads that `.env` (no repo-wide `set dotenv-load`). Errors with usage if none is set. `release.md` documents this up front for release agents.

## 2026-06-15

`just bake-pool-host-dev` now passes `--skip-deferred-install-wait` so dev pool bakes don't wait the extra few minutes for the deferred Playwright/apt install before stopping the services agent.

Replaced the `just bake-pool-host` recipe with `just bake-pool-host-dev` (bake from a working tree -- best-effort branch label) and `just bake-pool-host-prod` (clone an exact FCT tag -- strict), reflecting that the imbue_cloud pool bake now derives the stamped repo identity from its source rather than from hand-typed `--attributes`. The `minds-justfile` skill documents the dev-vs-production distinction and how to set the create form's repository for a fast-path match.

Added a `just minds-install` recipe that installs the minds desktop client's node deps (electron, etc.) using the Node version pinned in `apps/minds/.nvmrc` (selected via `select_node_version.sh`), so the install no longer fails with `ERR_PNPM_UNSUPPORTED_ENGINE` when the shell's default node has drifted off the pin. `just minds-start`'s "not installed yet" hint now points at `just minds-install` (instead of a raw `cd apps/minds && pnpm install`, which skipped the node selection and hit the engine check).

Added a design doc (`blueprint/ovh-baremetal-slices/`) for extending the imbue_cloud pool to allocate "slices" (lima/QEMU VMs) on rented OVH bare-metal servers as an alternative to ordering OVH VPSes, including the data model, admin lifecycle, connector release fork, and a recorded pricing gotcha (catalog base price excludes RAM/storage upgrades).

Added a refactor design doc (`blueprint/mngr-imbue-cloud-module-layers/`) proposing a layered sub-package structure for the `mngr_imbue_cloud` plugin (with an `import-linter` ordering contract), isolating the slice/bare-metal subsystem and the pool-bake code into their own layers and decomposing the oversized `instance.py`.

Added an `import-linter` "mngr_imbue_cloud layers contract" (root `pyproject.toml`) and a `test_meta_ratchets.py` test that enforces it, as part of restructuring the `mngr_imbue_cloud` plugin into layered sub-packages.

Bumped the per-test timeout on the `test_cli_docs_are_up_to_date` meta-ratchet test: the enlarged imbue_cloud CLI surface (the new `admin server` + slice commands) made full CLI-doc regeneration exceed the default 10s pytest-timeout in the slower offload sandbox.

Fixed the per-PR changelog enforcement check, which was passing vacuously in CI.

The check previously ran as an acceptance test (`test_pr_has_changelog_entry`) inside the offload Modal sandbox, but the sandbox does a fresh `git init` (so `main == HEAD`) and never fetches `origin`, so its base-branch diff always came back empty and the check passed no matter what. Any PR could merge without changelog entries.

The enforcement now lives in a dedicated CI gate, `scripts/check_changelog_entries.py` (run via the `check-changelog` GitHub Actions job and the `just check-changelog` recipe), which computes the changed-file set against the real base branch on the orchestrator where a base ref actually exists. It refuses to run with a loud non-zero exit if it cannot resolve a diff base distinct from HEAD, so it can never again pass vacuously. The old sandbox-bound acceptance test has been removed.

Expanded CLAUDE.md flaky-test guidance: first investigate why a test is flaky and make it more robust if possible; if it is correct but fundamentally needs more time, bump that test's timeout (but avoid unreasonably long timeouts -- prefer leaving it marked flaky for infrastructure-level flukes).

## GCP provider support: root-level changes

- Top-level coverage configuration adds `--cov=imbue.mngr_gcp` so the new package contributes coverage data.
- `scripts/make_cli_docs.py` adds `gcp` to `SECONDARY_COMMANDS` so the `mngr gcp` operator command group gets generated docs (required by `help_formatter_test`).
- `uv.lock` updated to add the new `imbue-mngr-gcp` workspace package and its dependencies (`google-cloud-compute`, `google-auth`, and their transitive deps).

- `.mngr/settings.toml` gains a `gcp` create-template (`mngr create -t gcp`) and a shared `[providers.gcp]` block, the analogue of the existing `modal` template. Like the `aws` template it builds via the `mngr_vps_docker` backend (`--file=` + `.` context) on depot's remote builders (`builder = "DEPOT"`), so it needs `DEPOT_TOKEN` and `GH_TOKEN` at create time. The provider defaults to `us-west1`/`us-west1-a` on an `e2-standard-2` VM; per-developer `allowed_ssh_cidrs` stays in the gitignored `.mngr/settings.local.toml` and the SSH firewall is created once via `mngr gcp prepare`.

Updated the agent-plugin-parity spec to record that `opencode` now implements the `waiting_reason` field generator (online), and documented that the `@opencode-ai/sdk` type stubs are out of sync with the shipped opencode binary on the permission events (the stubs say `permission.updated`/`permissionID`; the running 1.16.2 server emits `permission.asked`/`requestID`).

Documented the cross-plugin `waiting_reason` parity picture and implemented it for codex: the agent-plugin-parity spec now classifies each agent type -- implemented (claude, codex), doable-but-unimplemented (opencode, whose event bus exposes `permission.asked`/`permission.replied`), blocked-on-upstream (antigravity, which prompts but emits no event while blocked), and inapplicable (pi, which has no tool-approval prompt at all) -- while codex now implements both `PERMISSIONS` and `END_OF_TURN`.

Verified live against codex 0.139.0 that the `PermissionRequest` hook fires and blocks while the approval dialog is open (and clears on `PostToolUse`), and recorded two corrections: codex has no `PostToolUseFailure` event (cleanup is `PostToolUse` + `Stop` only) and `PermissionRequest` payloads carry no `tool_use_id`.

## 2026-06-14

Added `scripts/extract_antigravity_proto_schema.py`, a developer tool that recovers
antigravity's (`agy`) protobuf schema by scanning the `agy` binary for its embedded
`FileDescriptorProto`s (antigravity ships no `.proto` files). It previously lived only as an
inline appendix in `libs/mngr_antigravity/dev/README.md`; promoting it to a committed script
lets the new antigravity schema-verification release test invoke it directly. Run it with
`uv run python scripts/extract_antigravity_proto_schema.py "$(which agy)" --grep CortexStep`
(use `-v` to debug-log the bounded set of descriptor candidates it skips).

Added the implementation plan for the AWS minds compute provider under `blueprint/aws-minds-compute-provider/`.

- Fixed: `scripts/changelog_deploy.sh` now stops *every* Modal app in the changelog schedule's isolated environment before redeploying (via a new `--stop-all-apps` action in `scripts/changelog_schedule_utils.py`), instead of only the app matching the current name. A past app-naming-scheme change had orphaned an old cron app that kept firing, producing a second nightly `mngr/changelog-consolidation-*` branch; sweeping the whole environment makes redeploys orphan-proof.

- Fixed: `modal app stop` invocations now pass `--yes` (in `scripts/modal_nuke.py` and the new sweep), so they no longer abort with "no interactive terminal detected" under newer Modal CLIs when run non-interactively.

- Changed: The `dev` project's `CHANGELOG.md` is now date-organized, mirroring `UNABRIDGED_CHANGELOG.md`, instead of carrying an ever-growing `[Unreleased]` section. `dev` is never released, so nothing ever finalized its `[Unreleased]`; the nightly consolidation now summarizes each landed date independently into its own `## <date>` section (dated when the entries landed, not when the bot ran), per `scripts/changelog_consolidation_prompt.md`. The existing backlog was collapsed under its consolidation date.

Updated `uv.lock` to add the `anthropic` package (and its transitive `docstring-parser`
dependency), newly required by `libs/mngr_claude` for the shared typed Claude stream-json envelope.
The substantive change lives under `libs/mngr_claude` (see that project's changelog); this is the
root-level lockfile update that pins the resolved dependency tree.

## 2026-06-13

Added a design plan under `blueprint/host-backup-snapshot-rotation/` for fixing empty gVisor host backups: unique time-named btrfs snapshots, keep-newest-N retention, and exit-code-only backup failure signaling.

## 2026-06-12

Added `specs/agent-plugin-parity/spec.md`, a developer reference mapping every feature the
mature `mngr_claude` and `mngr_antigravity` agent plugins implement (lifecycle/state
detection, subagent-aware idle gating, auth/credential sharing, HOME/config isolation,
permissions, trust/onboarding, transcripts, conversation resume, session preservation,
deploy contributions, and more), plus a current-state matrix for the `codex`/`opencode`/
`pi-coding` stubs, a recommended bring-up sequence, and a per-CLI investigation checklist.
Intended to guide bringing new agent plugins up to parity with Claude.

Updated the repo-root README's "Shell completion" section: documented `-S`/`--setting` completion, and the new managed-shim install model (the rc holds a small shim that sources a mngr-managed completion file, so completion updates apply on upgrade without re-editing the rc).

Added an `aws` create-template to the repo's `.mngr/settings.toml` for dogfooding this
codebase on an AWS EC2 host, mirroring the existing `modal` and `docker` dev templates.

`mngr create -t aws <name>` builds the dev Dockerfile and runs an agent on EC2. Because
the AWS/`mngr_vps_docker` backend runs `docker build` on the remote VPS (rewriting
`--file=` relative to the uploaded context), the template uses the real-source-tree
build shape (context `.`, cloned + overlaid with uncommitted changes) rather than the
`.mngr/dev/build/` keyframe tarball shape that modal/docker use. The clone is full
history (no `--git-depth`): after the build, mngr seeds the work dir by pushing the
local repo's refs into the container's `/code/mngr/.git` as a thin pack, which needs the
container repo to already contain the base objects -- a shallow clone fails with
"pack has N unresolved deltas / index-pack abnormal exit".

The shared `[providers.aws]` config (region `us-west-2`, plan `t3.large`,
`auto_shutdown_minutes = 120`, `builder = "DEPOT"`) is committed in `.mngr/settings.toml`;
only the operator-specific `allowed_ssh_cidrs` lives in the gitignored
`.mngr/settings.local.toml`. The two blocks merge per-field (ProviderInstanceConfig.merge_with
honors `model_fields_set`). `builder = "DEPOT"` builds the image on depot's cached remote
builders; `DEPOT_TOKEN` and `GH_TOKEN` must be exported when running `mngr create -t aws`
(`depot.json` in the repo supplies the project id). The template uses `pass_env__extend`
(not plain `pass_env`) so it adds `GH_TOKEN` without clobbering any inherited `pass_env`
(e.g. a user profile's `ANTHROPIC_API_KEY`); the existing `modal` template's `pass_env`
was switched to `pass_env__extend` for the same reason.

This also fixes a bug in `mngr_vps_docker` that broke `builder = "DEPOT"` for all VPS
backends: the depot CLI installs to `/root/.depot/bin` (not on the non-interactive shell's
PATH), but the build invoked it by bare name, failing with "depot: command not found". It
is now invoked by absolute path. See the `mngr_vps_docker` changelog entry.

## AWS provider support: root-level changes

- `mngr create` CLI markdown docs regenerated to include the new AWS provider's build-args help (removes the dropped Vultr/OVH `--vps-os=` line at the same time). The per-provider prefix rename (`--aws-region=`, `--aws-instance-type=`, `--vultr-region=`, `--vultr-plan=`, `--ovh-datacenter=`, `--ovh-plan=`) lands in the regenerated text too.
- `scripts/make_cli_docs.py` SECONDARY_COMMANDS gains `"aws"` so the new `mngr aws prepare` / `mngr aws ami` command group renders a generated `libs/mngr/docs/commands/secondary/aws.md` page.
- Top-level coverage configuration adds `--cov=imbue.mngr_aws` so the new package contributes coverage data.
- `uv.lock` reverted to match `main` except for the new AWS additions (`imbue-mngr-aws`, `boto3-stubs`, `botocore-stubs`, `mypy-boto3-ec2`, `types-awscrt`, `types-s3transfer`). An earlier full re-lock had floated ~100 unrelated packages to latest, including `ty` 0.0.24 -> 0.0.39, whose stricter checks surfaced 52 pre-existing type errors repo-wide and failed CI.
- On merging `main`, the `uv.lock` conflict was re-resolved the same way (lock matches `main` plus only the six AWS additions). The four boto3/botocore type-stub packages (`boto3-stubs`, `botocore-stubs`, `mypy-boto3-ec2`, `types-awscrt`) are pinned to the latest versions published before 2026-05-10 so they satisfy the two-week supply-chain cooldown (these stubs release ~daily, so the newest always falls inside the window).

Restructured the changelog consolidation prompt
(`scripts/changelog_consolidation_prompt.md`) to produce more concise
summaries: the concise `CHANGELOG.md` bullets are now generated once per
project over all of that project's new dated sections (rather than once per
date, which created cross-date duplicates), followed by a single critical
"concision pass" that drops non-notable bullets and tightens the rest. The
merging step now also scrutinizes the `Fixed` category, dropping fixes for bugs
that were both introduced and fixed within the current release window (which
never reached a released version). Relatedly, `scripts/consolidate_changelog.py`
now prints one `SECTION <project> <date>...` line per project (listing its dates)
instead of one line per project-date, matching how the prompt summarizes.

Fixed the nightly changelog consolidation schedule firing at 8 AM Pacific
instead of midnight. `scripts/setup_changelog_agent.sh` set the cron to
`0 8 * * *` assuming it was interpreted as UTC, but the schedule is actually
interpreted in the deploying machine's local timezone (Pacific). It now uses
`0 0 * * *` with an explicit `--timezone America/Los_Angeles`, so it fires at
midnight Pacific regardless of where the deploy runs.

Renamed the changelog tooling scripts so they all share a `changelog_` prefix
and sort together: `consolidate_changelog.py` -> `changelog_consolidate.py`,
`trigger_changelog_consolidation.py` -> `changelog_schedule_utils.py` (the old
name implied it triggered something; it only holds the schedule's shared
identifiers + plugin-disable args), and `setup_changelog_agent.sh` ->
`changelog_deploy.sh`. All internal imports, docstrings, and the consolidation
prompt were updated to match.

Added three justfile recipes:

- `just release [args...]` wraps `scripts/release.py` (args forward as-is).

- `just changelog-deploy` wraps `scripts/changelog_deploy.sh` to (re)deploy the
nightly changelog-consolidation schedule.

- `just changelog-trigger` runs the consolidation on demand (the same agent the
schedule runs nightly), opening a PR.

`scripts/release.py`'s pre-release gate now points users at `just
changelog-trigger` to consolidate pending entries, instead of printing a long
`mngr schedule run ... --disable-plugin ...` one-liner.

`changelog_deploy.sh` now reads the agent's `GH_TOKEN` and `ANTHROPIC_API_KEY`
from Vault (`secrets/mngr/dev/github` and `secrets/mngr/dev/anthropic`) at
deploy time instead of from the operator's environment; run `vault login
-method=oidc` first. `VAULT_ADDR`/`VAULT_NAMESPACE` default to the imbue HCP
cluster.

Small phrasing fixes to the `aws` create-template comments in `.mngr/settings.toml`: dropped
the redundant "analogue of the modal template" aside and the "(the worktree)" qualifier on the
build context (with the broadened clone -- see the `mngr_vps_docker` changelog -- `mngr create
-t aws` works from a primary checkout too, not only a linked worktree), and removed the stale
note that per-developer `allowed_ssh_cidrs` must live in `.mngr/settings.local.toml` (the
provider already defaults it to `0.0.0.0/0`).

Added `imbue.mngr_codex` to the root pytest coverage targets for the new codex plugin.

Extended `specs/agent-plugin-parity/spec.md` (dimension D, "subagent-aware idle gating")
with a note on a related premature-idle failure mode: the RUNNING/WAITING marker tracks the
agent's turn/loop, not work it detaches from that loop. Documents how a CLI's
`run_in_background`-style tool (or a `cmd &`) can make the agent report WAITING while a
launched task still runs; that claude does not solve this for backgrounded bash (its Stop
hook waits only for sibling stop-hook processes and *excludes* `CLAUDECODE=1` bash-tool
tasks); and that the `CLAUDECODE=1` tag is nonetheless the discriminator that *would* make a
descendant-liveness wait safe. Distinguishes in-loop pending work, which the CLIs' idle
signals do gate correctly (agy's `fullyIdle:true`-plus-root-match clears only on the root's
final Stop, not interim Stops or a subagent's own idle; pi's foreground tools block the turn
so `agent_end` waits for them), from detached work, which is loop-scoped for claude, agy, and
pi alike. Adds a matching investigation-checklist question.

Refreshed `specs/agent-plugin-parity/spec.md` with the lessons from the pi-coding port now
that it is a real, near-`antigravity`-parity plugin (not a stub):

- Updated the state matrix and intro (pi is no longer framed as a stub; its rows flip to Y for
  lifecycle marker, subagent gating, readiness, transcripts, resume, and trust).
- Added a new dimension F, "Input delivery & submission confirmation" (renumbering the later
  dimensions): the tmux paste+Enter path is fragile (pi swallowed the first Enter), a CLI may
  expose a better programmatic input channel (pi injects via `pi.sendUserMessage`), and you
  must confirm a message actually started a turn (the marker), not scrape the pane.
- Added a "Your lever: shell hooks vs an in-process extension" section, including the
  in-process-extension hazard class (unhandled promise rejection crashing the host, jiti
  bare-specifier traps, emit-don't-tail transcripts).
- Sharpened existing dimensions with bugs hit during the port: the readiness "gating on an
  early banner loses the first message" failure mode; the trust "verify empirically what
  triggers the dialog -- pi triggers on `.pi`/`.agents/skills`, not CLAUDE.md/AGENTS.md, and
  trust guards config-loading, not prompt injection" warning; and the transcript
  derived-from-raw (claude/agy) vs independent-emission (pi) distinction.
- Extended the investigation checklist: a mechanism/input-delivery group, a
  packaging/distribution group (`PLUGIN_CATALOG`, signal check, `is_recommended`,
  publishability), and a "verify each answer against the running binary, not docs/source" note.

Added canonical justfile recipes for pool-host operations: `just
bake-pool-host <attributes-json> <region> [workspace_dir] [count] [extra
flags]`, `just list-pool-hosts`, and `just destroy-pool-host <id>`. These are
thin wrappers around the env-aware `minds pool {create,list,destroy}` CLI, which
resolves OVH creds, the management SSH key, and the staging/production host_pool
DSN from the activated tier's Vault entries automatically -- no hand-exported
secrets. (The DSN resolution lives in the `minds pool` CLI itself, not in the
justfile, so the recipes stay one-liners and `minds pool` works the same way
when invoked directly.)

Removed the broken `cleanup-pool-hosts` recipe: it sourced the long-gone
`.minds/<env>/neon.sh` shell files (secrets are in Vault now) and was redundant
with the connector's hourly release-cleanup cron. The new `destroy-pool-host`
recipe is the env/Vault-aware single-host replacement.

Fixed `just test-acceptance`: its marker expression was `-m "no release"`, a
pytest syntax error (`no` is not an operator) that failed at collection; it is
now `-m "not release"`.

Removed a duplicated forever-claude-template worktree-existence check block in
`just minds-start`.

Added a `minds-justfile` skill that routes any minds task (app, pool hosts,
environments, deployments, tests) through the root justfile, and directs adding
a recipe when one is missing.

Merged the `pi-coding` and `opencode` agent-plugin ports into a single branch and
began unifying their cross-cutting pieces. Updated the agent-plugin-parity spec
(`specs/agent-plugin-parity/spec.md`) to reflect `mngr_opencode` as a real,
fully-implemented port rather than a `BaseAgent` stub: filled its column in the
capability matrix, added the HTTP client/server architecture as a fourth
integration lever alongside shell-hooks and the in-process extension, and
documented its real mechanisms across the parity dimensions.

Also updated the same spec to reflect `mngr_codex` as a real, fully-implemented
shell-hooks port rather than the lone `BaseAgent` stub: filled its column in the
capability matrix, and documented its real mechanisms across the parity
dimensions -- most notably its third, distinct subagent-aware idle-gating shape
(dedicated `SubagentStart`/`SubagentStop` hooks tracking one file per in-flight
async subagent, with the `active` marker recomputed under an `mkdir`-based lock).
No named agent type is a stub any more. Also documented codex's launch-time
update-dialog suppression (`check_for_update_on_startup = false`, which prevents the
"Update available!" prompt from intercepting the first pasted message on resume) and
its mngr-side update notify + opt-in auto-update.

## 2026-06-11

- Add a planning document at `blueprint/workspace-color-picker/plan-workspace-color-picker.md` describing the workspace color-picker feature: a 12-color palette (11 named Figma colors + `#ffffff` white) plus an optional custom hex in workspace settings, replacing the SHA-derived per-workspace accent. (The implementation lands in `apps/minds/` -- see that project's changelog entry for the user-visible scope.)

- `CLAUDE.md`: Clarified that release tests do *not* run in CI (unlike acceptance tests), so anyone developing or modifying release tests must run them locally to verify them.

## 2026-06-10

Ignore local scratch shell scripts: added a general `**/*.local.sh` rule to `.gitignore` (mirroring the existing `**/*.local.md`), so any `*.local.sh` helper script stays untracked. This subsumes the previous single-file `**/scripts/notify_user.local.sh` entry, which was removed.

Also broadened the identify-* `_tasks/` ignore rule from `*/*/_tasks/` to `**/_tasks/`, so the `dev` project's root-level `dev/_tasks/` output folder is ignored consistently with the `libs/<name>/_tasks/` and `apps/<name>/_tasks/` ones (the old two-level glob missed it).

Hardened edge-case handling across `scripts/` per a suspicious-edge-case review:

- `release.py`: `_get_pypi_version` and `_is_published_on_pypi` no longer swallow failures -- any network/HTTP/payload error now propagates (release.py needs PyPI access anyway, and "assume published" on error silently skipped a new package's first-publication safeguard). `_get_pypi_version` returns a plain `str` now, so its caller drops the `(could not check)` / `is not None` handling. `_detect_changed_packages` now treats only `git diff --quiet` exit code 1 as "changed" and fails loudly on a real git error (exit > 1), instead of misreading a git failure as "every package changed".
- `modal_nuke.py`: replaced the `.get(..., "unknown")` fallback chains feeding `modal app stop`/`modal volume delete` with direct reads of the keys Modal's `--json` output actually emits (`"App ID"`, `"Name"`), raising a clear `ModalSchemaError` naming the unexpected schema if a key is missing, so the destructive path never runs against a placeholder identifier.
- `make_cli_docs.py`: dropped a dead `option.type is not None` guard, removed a redundant `hasattr(command, "commands")` guard, and made an unresolved See-Also reference raise (caught by `--check`) instead of emitting a broken markdown link.
- `sync_common_ratchets.py`: a check function in the source-of-truth file with no `# --- section ---` header now raises instead of silently syncing a bogus `# --- Unknown ---` section monorepo-wide.
- Added focused tests for `modal_nuke` and `make_cli_docs`; added clarifying comments to `junit_test_summary.py`, `warm_cli_example.py`, and the doc-inference heuristics. `warm_cli_example.py` now warns to stderr instead of silently swallowing a failed `os.chdir`.
- `make_cli_docs_test.py`: importing `make_cli_docs` sets `MNGR_LOAD_ALL_PLUGINS=1` process-wide (it must, to load all providers for doc generation); the test now pops that env var after import so the side effect cannot leak into other tests in the same xdist worker (it was breaking `libs/mngr`'s `create_plugin_manager` blocking test).

Added the `identify-bad-tests` Claude skill. It scans a target path -- either a whole library or any
subdirectory within one -- for low-quality, fragile, or misleading tests and reports candidates ranked
by importance into the containing library's `_tasks/bad-tests/<date>.md`, in the same format as the
other `identify-*` skills (so findings feed into `create-fixmes`). The skill grounds its checks in the
"# Testing" section of the style guide: tautological/unfalsifiable assertions, "no exception raised"
checks, tests coupled to implementation details, error tests that don't pin the error type/message,
weak coverage-chasing assertions, missing edge/branch cases, mock and fake misuse, flakiness and
isolation hazards, wrong test type/location/marking, test-grouping classes and poor naming, and
snapshot misuse. The central evaluation question is whether a test would actually fail if the code
under test had a real bug. Unlike the other skills it deliberately reads the `_test.py` / `test_*.py`
files (which the repo conventions normally skip), and it defers raw pattern occurrences already
counted by `test_ratchets.py` to those ratchets, reporting only the semantic test-quality problem.

Also fixed a contradictory instruction shared by the existing `identify-*` skills
(`identify-style-issues`, `identify-doc-code-disagreements`, `identify-outdated-docstrings`,
`identify-inconsistencies`, `identify-suspicious-edge-cases`): their intro said to commit when
finished, but their output files are gitignored and the closing line says no commit is needed.
Removed the contradictory parenthetical from each.

No runtime or tooling change.

- Add a daily `schedule:` trigger to the `minds launch-to-first-message`
  workflow. At 14:00 UTC (07:00 PDT / 06:00 PST) it builds + verifies the
  current mngr `main` HEAD against FCT `main`, with the full slack flow
  (latchkey + mocked slack server). Surfaces drift between the two repos
  the morning it happens instead of waiting for the next manual dispatch.
- `commit_sha` and `template_ref` inputs are now optional. Empty
  `commit_sha` -> `github.sha` (mngr main HEAD when triggered by schedule;
  caller's branch HEAD when dispatched without a value). Empty
  `template_ref` -> `main`. Existing dispatches that pass both inputs
  behave identically.
- The cron only fires once this workflow file lands on the default branch
  (`main`); GitHub Actions ignores schedule triggers defined only on
  feature branches.

- minds-launch-to-msg.yml: build job moves from `ubuntu-latest` to the self-hosted `minds-runner` Mac. Required to bundle Mac-native uv/git/lima into the resulting .app (the Linux runner shipped ELF binaries that crashed the desktop client at `uv` exec). Build and verify now serialize on the same runner.
- repo: `.gitignore` now also ignores `**/scripts/*.local.sh` (one-off local test harnesses), `apps/minds_workspace_server/package-lock.json`, and `**/.DS_Store`.
- specs: update `specs/electron-desktop-app/` (spec + concise) to reflect the shipped minds desktop-app architecture.
- minds-launch-to-msg.yml: swap headline screenshot source -- per-window Playwright `.win.png` captures are now embedded in the GitHub step summary, with full-desktop `screencapture -x` shots demoted to `.desktop.png` forensic dupes in the artifact. CDP page activation does not move macOS WindowServer z-order, so the full-desktop captures routinely showed the unauthenticated /welcome BrowserWindow instead of the actual chat / projects / approval pages the e2e script was driving. The per-page captures bypass WindowServer (DOM-to-raster via CDP) and consistently show the correct content.
- minds-launch-to-msg.yml: stop publishing screenshots to the `ci-screenshots` orphan git branch -- that branch grew to ~1.2GB of PNGs and was downloaded by every clone of the repo. Screenshots now ride only in the per-run `launch-to-first-message-<run_id>` GitHub Actions artifact (auto-expires per `retention-days`). The job summary now lists a manifest of milestone -> filename instead of inline images; viewers download the artifact zip to inspect. The orphan branch is being deleted from origin in the same change.
- minds-launch-to-msg.yml: tee the launch-to-msg + slack flow Python script's stdout+stderr to `/tmp/launch-to-msg-logs/e2e-stdout.log` and bundle that directory into the diagnostics artifact. The e2e script's structured loguru output (phase progressions, kick attempts, navigation events) was previously only visible in GHA's console log (expires); the artifact zip is the durable post-mortem surface.
- launch_to_msg_e2e.py: skip the periodic kick when no chat window is currently visible (replaces `find_chat_window(ctx) or win` with a `find_chat_window` check + early-skip). During the latchkey approval flow `win` is often the `/requests/<id>` page, which has no textarea; the previous behavior logged a spurious warning every KICK_INTERVAL.
- Pre-merge cleanup of CI workflow hygiene: `minds-playwright-vanilla.yml` renamed to `minds-macos-launch.yml` (display name + job name aligned); added html reporter + always-upload + `run_attempt`-suffixed artifact so passing reruns no longer erase failing-attempt screenshots; trigger changed from `branches: [wz/minds_onboard]` to `[main]` + open `pull_request` so the workflow keeps running post-merge.

## 2026-06-09

Added the titlebar-workspace-accent blueprint under ``blueprint/`` describing
the rework of the per-workspace accent from a small swatch next to the title
into a full-width colored top bar with rounded edges below. The
implementation lives under ``apps/minds/``.

Add a blueprint plan under `blueprint/loading-window-position/` describing
the fix for the startup loading window jumping from the default centered
position to its restored bounds when the backend comes up. The plan
covers reusing the existing `restoreWindowBounds()` helper at the
app-startup site, expected behavior in first-launch, multi-window,
display-gone, and deleted-workspace cases, and the manual verification
scenarios used since this is Electron main-process code with no
automated test harness in the repo.

Updated the changelog-writing guidance in `CLAUDE.md`: when a per-PR changelog
entry uses a list, its bullets should be separated by a double newline (a blank
line between each bullet).

Added a blueprint plan (`blueprint/docker-state-container-leak/`) documenting the investigation and fix for leaked Docker state containers from local test runs.

Added an implementation-plan design doc under `blueprint/` for the create-template `setting`/`setting__extend` fix (see the `libs/mngr` entry for the user-visible behavior change).

`scripts/snapshot_minds_e2e_state.py` now sets `LATCHKEY_DISABLE_COUNTING=1` in the in-sandbox runner before booting minds. The snapshot builder is test infrastructure (it captures on-disk state into the fixture image used by the `minds_snapshot_resume` tests), so its booted minds -> `mngr latchkey forward` -> `latchkey gateway` chain should not count toward Latchkey's usage -- mirroring the opt-out the pytest conftest already applies to the equivalent e2e test. Genuine minds installs (including dev-from-source launches via `just minds-start`) intentionally still count.

## 2026-06-08

Fixed the `publish` workflow, which had been failing at the "Verify versions and pin consistency" step since `scripts/utils.py` started importing `UNPUBLISHED_PACKAGES` from `imbue.mngr`. A bare `uv run` only syncs the root project (which does not depend on `imbue-mngr`), so the import raised `ModuleNotFoundError: No module named 'imbue.mngr'`. The three `scripts/verify_publish.py` invocations now use `uv run --all-packages` so the workspace package is installed.

## 2026-06-08

Added the inbox-modal-refactor blueprint under ``blueprint/`` describing
the consolidation of the requests panel into the same modal surface as
the permission dialogs. The implementation lives under ``apps/minds/``.

Fixed the `mngr-shim-installed` pre-commit hook (`scripts/check_mngr_shim.sh`) giving a false failure when invoked under `uv run` (e.g. during `mngr create`, which makes its initial commit under uv). `uv run` force-prepends the project's `.venv/bin` to PATH, so the project-local `mngr` console script shadowed the dev shim inside the hook even though the shim wins in a normal shell. The hook now drops the active `VIRTUAL_ENV`'s bin dir before resolving `mngr`, evaluating resolution the way an interactive shell would, while still catching a genuinely stale global ahead of `~/.local/bin`.

# Point mngr at the imbue-mngr-skills Claude Code plugin

The `imbue-mngr-skills` Claude Code plugin (the `message-agent`,
`wait-for-agent`, `find-agent`, and `mngr-help` skills) is published from its
own GitHub repo, `imbue-ai/mngr-claude-skills`, as a Claude Code plugin
marketplace -- mirroring how `imbue-code-guardian` is distributed from its own
repo.

This repo dogfoods the published plugin: `.claude/settings.json` registers the
`imbue-mngr` marketplace from `imbue-ai/mngr-claude-skills` and enables
`imbue-mngr-skills@imbue-mngr`, and `scripts/claude_update_plugin.sh` refreshes
it on SessionStart alongside `imbue-code-guardian`.

These skills previously lived in this repo's project-level `.claude/skills/`
directory; they have moved out to the dedicated repo so any mngr user can
install them for any project (via `mngr extras claude-plugin`, or
`claude plugin marketplace add imbue-ai/mngr-claude-skills` +
`claude plugin install imbue-mngr-skills@imbue-mngr`).

Added the implementation blueprint for the minds create-flow fixes under `blueprint/minds-create-flow-fixes/`.

- Added the implementation plan for the final workspace-create fixes under
  `blueprint/`.

Fixed the root `.gitignore` `tmr-report/` pattern to use a `**/` prefix, satisfying the `test_gitignore_patterns_use_double_star` check that keeps `.gitignore` compatible with `.dockerignore`. This was flagged by CI after a bulk merge added the unprefixed pattern.

Added a blueprint plan (`blueprint/gvisor-docker-hardening/`) for hardening docker invocations with the gVisor (runsc) runtime.

Added a dev `mngr` shim (`scripts/mngr`) so `mngr` always runs the checkout you're working in (per-worktree, by cwd) instead of a stale global install. A pre-commit hook (`scripts/check_mngr_shim.sh`) installs the shim automatically (a symlink in `~/.local/bin`) and verifies it's on PATH -- no per-worktree setup. Updated the README dev-install notes accordingly (use the shim, not `uv tool install -e libs/mngr`).

Added the implementation blueprint for the Lima docker-in-VM (`is_host_in_docker`)
work under `blueprint/lima-docker-host/`, and recorded the new
`imbue-mngr-lima` -> `imbue-mngr-vps-docker` internal dependency in the
`scripts/utils.py` package graph (used by the version-sync check).

Added `test_every_mngr_plugin_isolates_home_in_tests` to `test_meta_ratchets.py`:
every mngr plugin (any project with a `[project.entry-points.mngr]` table) must
call `register_plugin_test_fixtures(globals())` in a conftest, guaranteeing its
tests redirect $HOME away from the developer's real home directory.

- Release tooling (`scripts/release.py`, `scripts/utils.py`): the publish graph is now **auto-discovered from the workspace** instead of being a hand-maintained allowlist. Every `libs/*` package is a publish candidate unless it is explicitly listed in `UNPUBLISHED_PACKAGES` (in `libs/mngr/.../plugin_catalog.py`, the single opt-out shared with the install wizard). Previously a package nobody remembered to add to the hardcoded `PACKAGES` tuple was invisible to the release script -- never bumped, never pin-aligned, never offered -- which let several plugins (`mngr_usage`, `mngr_ovh`, `mngr_imbue_cloud`, `mngr_latchkey`, `mngr_forward`, `mngr_schedule`, `mngr_claude_usage`, `mngr_robinhood`) silently fall into limbo with stale internal pins.
- Pin alignment (`update_internal_dep_pins`) now walks **every workspace member** (`libs/` and `apps/`) across `[project.dependencies]`, every `[project.optional-dependencies]` extra, and every `[dependency-groups]` group -- not just the published packages' main dependencies. Publishable packages have their publishable internal runtime deps forced to `==<version>` (a published wheel must pin its internal deps); everywhere else, only existing `==` pins are realigned, so deliberately-unpinned deps stay unpinned. This is what keeps the override-free `uv lock` that `apps/minds/scripts/build.js` runs resolvable.
- Pin-consistency verification (`verify_pin_consistency`) was generalized to the same broad scope so a stale or missing internal pin now fails `test_internal_dep_pins_are_consistent` in CI, rather than only surfacing when someone builds the ToDesktop bundle. `validate_package_graph` now asserts the publish graph is *closed* (no publishable package has a runtime dependency on an unpublished workspace package, which would be unresolvable on PyPI). A new `test_every_lib_is_classified` ratchet guarantees every `libs/*` package is either published or in `UNPUBLISHED_PACKAGES` -- nothing can silently fall through again.
- New-package detection now considers the full release candidate set (directly-changed packages **plus** everything pulled in by the cascade and the mngr-always rule), not just directly-changed packages. An unpublished package reached only via cascade (e.g. one that depends on `imbue-mngr` and so cascades every release) is now correctly offered for first publication instead of being silently bumped and published as if it already existed.

`just minds-start` now exports `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1` alongside
the `MINDS_WORKSPACE_*` vars. This is the explicit opt-in that makes the minds
desktop create-form honor the local-worktree defaults on any tier (including
staging / production), instead of only on per-developer dev envs.

Added `**/tmr-report/` to the root `.gitignore` so the test-orchestrator
(mapreduce) run-report directory written into a worktree is not flagged as an
untracked change. The existing `**/tmr_*/` pattern did not match the
dash-separated `tmr-report/` name.

- Gitignore the `tmr-report/` orchestrator output directory (alongside the existing `tmr_*/` rule) so test-runner report artifacts are not flagged as uncommitted changes.

- Gitignore the `tmr-report/` directory at the repo root: it holds transient task-runner orchestration scratch output (e.g. `events.jsonl`) and should never be committed.

Add `tmr-report/` to `.gitignore` so the TMR (test-mediated reconciliation) orchestrator's scratch output directory is ignored (the existing `**/tmr_*/` pattern uses an underscore and did not match the dash-named directory).

## 2026-06-07

Added the blueprint plan `blueprint/mngr-agent-sdk/plan-mngr-agent-sdk.md` describing the
mngr-backed Claude Agent SDK (`imbue.mngr_robinhood.agent_sdk`). The implementation itself lives
under `libs/mngr_robinhood` (see that project's changelog).

Excluded the new opt-in live Claude Agent SDK test suite from CI by adding `and not sdk_live`
to both pytest filter expressions in `offload-modal.toml`. Added a `just test-sdk-live` recipe
that sets `RUN_SDK_LIVE_TESTS=1` and runs the `sdk_live`-marked tests in `libs/mngr_robinhood`.

# Blueprint plan for finishing the mngr-backed Agent SDK

Added the implementation plan at `blueprint/finish-agent-sdk/plan-finish-agent-sdk.md` describing
how the remaining control surfaces of the mngr-backed Agent SDK are completed (see the
`libs/mngr_robinhood` changelog entry for the user-visible behavior).

Added a blueprint design doc (`blueprint/tmux-window-size/`) describing the configurable tmux window-size feature implemented in this branch.

## 2026-06-06

Added `blueprint/claude-stream-buffer/plan-claude-stream-buffer.md`, the design plan for approximate Claude response streaming via the mngr tmux session (implemented in `imbue-mngr-claude` and `imbue-mngr-robinhood`).

## 2026-06-05

- Release tooling (`scripts/utils.py`): added `imbue-mngr-usage`, `imbue-mngr-claude-usage`, `imbue-mngr-forward`, `imbue-mngr-latchkey`, `imbue-mngr-imbue-cloud`, `imbue-mngr-ovh`, `imbue-mngr-schedule`, and `imbue-mngr-robinhood` to the hard-coded `PACKAGES` publish graph so they are version-bumped, pin-aligned, and offered for first publication by `scripts/release.py`. Their internal dependency pins were realigned to the current workspace versions to satisfy `test_internal_dep_pins_are_consistent`.

## 2026-06-05

`scripts/install.sh` now invokes the reworked dependencies command as `mngr dependencies --install interactive --scope core` (was `mngr dependencies -i`). The `--scope core` flag means the installer only treats a missing *core* dependency (`git`/`tmux`/`jq`) as a hard failure that triggers its warning; a missing optional dependency (`ssh`/`rsync`/`unison`/`claude`) no longer trips the warning. The interactive prompt is unchanged, so users can still choose to install everything.

Updated root-level references for the `mngr_uncapped_claude` plugin rename to
`mngr_robinhood`: the top-level `README.md` sub-projects list, the
`--cov=imbue.mngr_robinhood` coverage entry in the root `pyproject.toml`,
the `robinhood` entry in `scripts/make_cli_docs.py`'s secondary-command
set, the `specs/robinhood/` spec directory, and `uv.lock`.

## 2026-06-05

Updated the repo-root local-dev LiteLLM proxy config (`litellm_proxy/config.yaml`) to expose the full current Anthropic Claude lineup (Opus 4.8/4.7/4.6/4.5/4.1, Sonnet 4.6/4.5, Haiku 4.5, plus the dated Opus 4 / Sonnet 4 ids) with inline per-token pricing. This file is kept in sync with `apps/modal_litellm/app.py` by a drift test.

## 2026-06-04

Add a blueprint plan for the apps/minds template migration to JinjaX (`blueprint/jinjax-migration/`).

- Add a new `audit-ci` Claude skill (`.claude/skills/audit-ci/SKILL.md`) that documents how to audit recent CI runs for anomalies (warnings, uncached/rebuilt docker images, flaky/slow tests, regressions). It explains this repo's counterintuitive CI layout -- test results live in separately-synthesized `Unit + Integration Tests` / `Acceptance Tests` check-runs (shown as "in 0s") rather than in the workflow jobs -- and includes calibration notes to avoid common false positives (duration variance vs regressions, normal Modal host-creation output, single broken branches vs systemic issues).
- Speed up the `test-offload` and `test-offload-acceptance` checkouts: instead of `fetch-depth: 0` (which fetches the full history of *every* branch), do a default shallow checkout and then `git fetch --unshallow` only the current ref. offload needs the full ancestry of HEAD to find its checkpoint commit and thin-diff against it, but not other branches; on a repo with many branches the all-branches fetch can add minutes to each run.

The bash strict-mode meta-ratchet snapshot is raised from 10 to 12 to accommodate the two minds verify scripts (`apps/minds/scripts/first-message-verify.sh` and `launch-and-verify.sh`), which intentionally omit `set -e` (they handle errors explicitly and their retry loops depend on commands exiting non-zero). The docstring now documents this exception alongside the existing `.minds/template/*.sh` accommodation and notes that the count is enumerated against the full local checkout (offload sandboxes see fewer because `.dockerignore` omits some tracked paths).

- Remove the dead "release" branch apparatus from CI and give the release tests a real home. There is no `release` branch -- releases are cut from `main` as `v*` tags -- so the old `test-release`/`test-docker-release` jobs, gated to `refs/heads/release` push, never ran. `ci.yml` no longer references the release branch (dropped the `release` push trigger and the four `github.ref != 'refs/heads/release'` job guards), and the two release-test jobs move to a new dedicated workflow `.github/workflows/release-tests.yml`. That workflow runs on `workflow_dispatch` (trigger it against `main` to validate a commit before you cut a release) and automatically on `v*` tag pushes (a backstop record). Note: it is not a hard publish gate -- `publish.yml` runs on the same tag independently. `scripts/release.py` now prints an advisory warning before the release confirmation prompt if the Release Tests workflow has not passed on the exact commit being tagged. Also refresh the stale "Release Tests" description in `style_guide.md` and drop the dead `release` branch from the changelog-ratchet PR-branch skip in `test_meta_ratchets.py`.

Added a blueprint planning doc (`blueprint/disable-ovh-qemu-backups/`) for disabling OVH-side VPS backups by purging qemu at the OVH provider level.

Bumped GitHub Actions that were pinned to Node.js-20 runtimes (deprecated by
GitHub; forced to Node 24 starting 2026-06-16) to their latest Node.js-24
majors: `actions/cache` v4->v5, `actions/upload-artifact` v4->v7,
`actions/setup-node` v4->v6, `actions/checkout` v4->v6 (vet.yml),
`extractions/setup-just` v2->v4, `mikepenz/action-junit-report` v5->v6, and
`astral-sh/setup-uv` v6->v7. This removes the Node.js-20 deprecation warnings
from CI logs.

Upgraded two vulnerable transitive dependencies in `uv.lock` to their fixed
versions (surfaced by `uv audit`): `idna` 3.14->3.16 and `starlette`
1.0.0->1.0.1.

## 2026-06-04

- The `/sync-tutorial-to-e2e-tests` skill's default test-directory argument now points at the new `libs/mngr/imbue/mngr/e2e/tutorial/` subdirectory, so it no longer flags non-tutorial e2e tests as unmatched.

## 2026-06-03

Updated the root `.minds/template/ovh.sh` secret template comment to note that the OVH AK/AS/CK credentials are now pushed to Modal (as the `ovh-<tier>` secret) for the connector's runtime cleanup of released pool hosts, not just read on the operator's machine during deploy/destroy. Also adds the blueprint plan for the leased-host cleanup work.

Fixed stale references in the `minds-dev-workflow` skill and the `minds-start`
justfile error hints:

- Dev env naming corrected from `<your-user>-dev` to `dev-<your-user>`. The
  `DevEnvName` validator requires the tier prefix first (`dev-`/`ci-`), so
  `josh-dev` is invalid while `dev-josh` is valid. Also corrected the derived
  paths the skill documented (`MINDS_ROOT_NAME=minds-dev-<user>`, env root
  `~/.minds-dev-<user>/`, container `minds-dev-<user>-mindtest-host`).
- Worktree base branch example `josh/start-minds` (no longer exists on the FCT
  remote) replaced with `origin/main` in the skill and in both `just
  minds-start` error hints.
- Pool-host baking described as OVH-backed (the imbue_cloud pool's VPS provider)
  rather than the outdated "Vultr".

`just forward-system-interface` now writes the Cloudflare tunnel token to
`runtime/secrets/cloudflare_tunnel.env` (one of the per-secret env files in the
`runtime/secrets/` directory) instead of the old single `runtime/secrets` file,
matching the directory-based secrets layout the FCT runner and minds now use.

`just minds-start` and `just minds-build` now select the Node version pinned in
`apps/minds/.nvmrc` (via nvm) before launching, so they no longer fail with
`ERR_PNPM_UNSUPPORTED_ENGINE` when the shell's default Node has drifted off the
pin. The selection is a no-op when the active Node already matches and errors
with an actionable hint when nvm or the pinned version is missing (it never
auto-installs Node). Shared with `propagate_changes` via the new
`apps/minds/scripts/select_node_version.sh` helper.

Added `specs/discovery-provider-error-resilience.md` documenting the two remaining discovery-resilience loose threads from the workspace-flicker debugging: (1) retaining known hosts/agents through a transient provider discovery error (drop only on explicit destroy or a successful poll; mark retained items unknown/stale by reusing `error_by_provider_name`), and (2) bouncing/restarting the latchkey forward on the same triggers minds uses to bounce its own observe, so latchkey picks up mid-session provider/config changes.

Removed the `.minds/template/paid-accounts.sh` secret template and folded `MINDS_PAID_ADMIN_KEY` + `MINDS_PAID_LIST_CACHE_TTL_SECONDS` into `.minds/template/supertokens.sh`, reflecting the move of paid-user tracking from a Modal-secret allowlist to database tables. Updated the vault-environments spec's service list. Added the implementation blueprint under `blueprint/paid-user-tables/`.

Added a design blueprint (`blueprint/imbue-cloud-slow-path/`) for the imbue_cloud
robust fast/slow-path host-leasing change.

## 2026-06-02

Added the design doc for the tiered system-interface restart
(`blueprint/tiered-restart-v2/plan-tiered-restart-v2.md`), describing the
two-tier minds workspace recovery flow and the `mngr stop --stop-host`
flag that backs the host-restart tier.

Added the implementation plan for the error-hierarchy collapse under `blueprint/`. No runtime
or tooling change.

## 2026-06-01

Tightened the `test_every_project_has_changelog_layout` meta-ratchet to also require a `.gitkeep` inside each project's `changelog/` directory. Previously only the directory's existence was checked, so a newly added project with no `.gitkeep` would pass until a later consolidation run drained its entries and the empty directory silently vanished from git. Requiring the `.gitkeep` upfront catches the omission when the project is first added.

## 2026-06-01

`markdown-it-py` is now an explicit (rather than only transitive) dependency in the lockfile: mngr uses rich's own CommonMark parser directly to rewrite links when rendering help topics for the terminal.

## 2026-05-29

# Spec file-tree updates for the apps/minds todesktop config rename

- `specs/electron-desktop-app/concise.md` and `specs/electron-desktop-app/spec.md`:
  the file-tree listings for `apps/minds/` now show `todesktop.js` instead of
  `todesktop.json`. The rename happens in the apps/minds slice of this PR (see
  `apps/minds/changelog/mngr-activate-todesktop-binary-hook.md`); these spec
  updates keep the documented layout in sync with the actual one.

- Added a design spec under `specs/docker-cleanup-state-and-images/` documenting the Docker build-image and state-container cleanup work.

Added the implementation spec for Imbue Cloud R2 bucket support
(`specs/imbue-cloud-r2-buckets/spec.md`).

Updated the `.minds/template/cloudflare.sh` secret template to document that
`CLOUDFLARE_API_TOKEN` must now be an account-owned (`cfat_`) token carrying
`Workers R2 Storage: Edit` + `Account API Tokens: Edit` (on top of the existing
tunnel/DNS/Access/KV permissions), and that R2 must be enabled on the Cloudflare
account.

- Drop the now-removed `--use-snapshot` flag from the TMR GHA workflow (`.github/workflows/tmr.yml`) so the scheduled/manual TMR runs don't fail at invocation. Snapshot building on `--provider modal` is automatic now, so behavior is unchanged. Also refresh a stale comment in `.github/workflows/tmr-reintegrate.yml` that mentioned the same removed flag.

# Self-hosted Mac runner + launch-to-first-message workflow

- Added `.github/workflows/minds-launch-to-msg.yml`, a `workflow_dispatch` job that (given a minds commit SHA and forever-claude-template ref) either reuses an existing ToDesktop build matching the commit or runs `pnpm dist` to build a fresh draft, then on the self-hosted `minds-runner` macOS host downloads the resulting `.app`, launches it, waits for the backend to come up, and optionally round-trips a real first-message chat against a LIMA agent before cleaning up. Collects diagnostic artifacts on failure.
- Added `.github/workflows/minds-runner-reset.yml`, a `workflow_dispatch` job to manually reset the self-hosted runner to a clean state (and optionally install a fresh `.app` from a ToDesktop `.zip` URL).
- Companion infrastructure (the runner Mac itself: Tailscale-tagged, LaunchAgent-installed GitHub Actions runner) lives outside this repo. The runner is registered at the `imbue-ai` org level and is targeted by `runs-on: [self-hosted, macOS, minds-runner]`.

Added `specs/minds-backup-provider/concise.md`, the spec for wiring the
imbue_cloud bucket capability into the minds workspace-creation flow (backup
provider toggle, async post-creation restic config injection, and the
forever-claude-template `host_backup` contract changes).

Added spec `specs/host-backup/concise.md` for a new continuous-backup
service that runs inside every mind workspace. The service uses restic
against a Cloudflare R2 bucket by default and takes consistent btrfs
subvolume snapshots on lima / vps-docker (no-op on plain docker). The
in-container `host_backup` library + bootstrap config wiring lives in
forever-claude-template (separate PR). This monorepo's changes provision
the outer-side `snapshot_helper.sh` systemd unit on vps-docker hosts;
see `libs/mngr_vps_docker/changelog/mngr-mind-backup.md` and
`libs/mngr_ovh/changelog/mngr-mind-backup.md` for the per-project
details.

- Added a spec (``specs/symlink-code-onto-mngr-volume/concise.md``) describing the relocation of the forever-claude-template workspace from ``/code/`` onto the ``/mngr/`` persistent volume (as ``/mngr/code/``), with safety-net ``/code -> /mngr/code`` and ``/worktree -> /mngr/worktree`` symlinks. The spec covers the Dockerfile bake-and-relocate dance (workspace baked at ``/mngr/code/`` then renamed to ``/docker_build_code`` so the volume mount path is empty in the image), the first-boot atomic-seed CMD logic, the per-template scope (``docker``/``vultr``/``ovh`` run the full dance; ``lima`` aligns the path but skips the dance; ``imbue_cloud`` inherits from the ``ovh`` bake), and the no-auto-migration story for existing live hosts. The actual implementation lives in the forever-claude-template repo on the ``mngr/symlink-code`` branch.

Added the design doc for putting the per-host VPS docker unified volume onto
a loop-mounted btrfs subvolume (`specs/vps-docker-btrfs/concise.md`). See the
per-project entries under `libs/mngr_vps_docker/`, `libs/mngr_vultr/`, and
`libs/mngr_ovh/` for the implementation details.

Added a new design spec under `specs/vps-docker-unified-volume/concise.md`
that documents the docker_vps provider's move from a two-volume layout
(per-user state container + per-host data volume) to a single unified
per-host Docker volume on the VPS. The spec captures the rationale,
expected on-volume layout (`host_state.json`, `agents/<agent_id>.json`,
`host_dir/`), discovery behavior (find the volume via the agent
container's `com.imbue.mngr.host-id` label), and the breaking-change
caveat that pre-existing docker_vps hosts cannot be discovered after
upgrade.

## 2026-05-28

Bump the `test-docker-electron` CI job's Node.js to 24.15.0 and pnpm to 10.33.4 to match the new exact-version pins in `apps/minds/package.json`. Also refresh the example `pyproject.toml` block in `specs/electron-desktop-app/spec.md` so it matches the real packaged file (`requires-python = "==3.12.13"` and the actual three-dependency list) instead of the older `>=3.12` / single-`imbue-minds` snapshot, and correct the standalone-pyproject path reference in that spec from `electron/pyproject.toml` to `electron/pyproject/pyproject.toml`.

# Changelog consolidation: accuracy review of new bullets

The nightly changelog consolidation agent now reviews the `CHANGELOG.md`
bullets it just generated for factual accuracy against the code, before
opening its PR. After committing the consolidation, it spawns one or more
fresh-context `general-purpose` reviewer subagents (spec in
`scripts/changelog_accuracy_reviewer.md`, relative to the repo root) and
partitions the projects that gained new bullets across them at its
discretion -- so a trivial change touching every package needn't spawn a
reviewer per package -- running them in parallel. Each verifies its
assigned projects' newly-added bullets against the actual code, correcting
or removing inaccurate ones and collapsing bullets that another bullet
materially supersedes. This guards against stale or inaccurate changelog
entries.

Each reviewer edits only the `CHANGELOG.md` files of its assigned projects
(the code is treated as ground truth -- reviewers never modify source) and
commits its own corrections, staging only those files so the parallel
reviewers don't clobber each other. Reviewers run unattended -- they
self-review rather than asking a user -- and report their findings back to
the consolidation agent, which decides what to do with them. The run's
outcome JSON reports `pr_url` on success and `notes` (the failing step and
error detail) on failure.

# Enforce the supply-chain cooldown via `[tool.uv] exclude-newer`, refreshed at release

- Moved the two-week dependency cooldown from a time-relative test to uv's native
  resolver enforcement. Added `[tool.uv] exclude-newer` to the root `pyproject.toml`
  (initial value `2026-05-23T00:00:00Z`), so `uv lock` simply refuses to consider any
  package version uploaded after the cutoff. This is proactive (you cannot lock a
  too-new package) rather than after-the-fact detection.
- `scripts/release.py` now advances the cutoff at each release: it sets
  `exclude-newer` to (today's UTC date - 2 weeks) just before regenerating
  `uv.lock`, and commits the root `pyproject.toml` alongside the version bumps. The
  update is **forward-only** -- it takes `max(current_cutoff, release_date - 2 weeks)`,
  so a release cut while the current cutoff is still younger than two weeks leaves it
  untouched rather than pushing it back. This avoids re-excluding a deliberately-pinned
  fresh dependency and breaking resolution. The
  initial value is set to just past the newest locked package for the same reason,
  which makes per-package exemptions unnecessary.
- Removed `test_no_dependencies_younger_than_two_weeks` (and its
  `_FRESHNESS_EXEMPT_PACKAGES` / `_lock_package_upload_time` helpers) from
  `test_meta_ratchets.py`; uv now enforces the cooldown at lock time, so the test is
  redundant. Its `ty`/`modal` exemptions are no longer needed because the cutoff is
  kept recent enough to admit them directly.
- Added unit tests (`scripts/release_test.py`) covering the forward-only advance, the
  no-op when the cutoff is still within the window, and the boundary case.
- The cooldown does not protect against a compromise that stays undetected past the
  window; its only value is the detection delay before we adopt a release.

# Dropped the removed `MNGR_ALLOW_PYTEST` from the env-settings spec

`MNGR_ALLOW_PYTEST` was removed from mngr in this PR (the pytest config guard is
now per-config via `is_allowed_in_pytest`). Removed the now-stale reference to it
from `specs/env-settings-overrides/concise.md`.

Added `libs/mngr_mapreduce` to the workspace; root `pyproject.toml` now collects coverage for `imbue.mngr_mapreduce` alongside the other workspace packages.

Add a `uv-sync-pre-push` hook to `.pre-commit-config.yaml` (registered for the `pre-push` stage, ordered as the first local hook) that runs `uv sync --all-packages` before a push whenever that push touches dependency files (`uv.lock` or any `pyproject.toml`). This keeps the local environment in sync with just-merged dependencies, primarily for the case where the code-guardian stop hook merges `origin/main` and then pushes the merge commit. Pushes that do not change dependency files are unaffected (the hook is skipped).

The hook runs before the other pre-push hooks (`ruff`, `ty`, `regenerate-cli-docs`, `compile-style-guide`) on purpose: those all shell out to `uv run`, which does not install all workspace members on its own. When a merge of `origin/main` adds a new workspace member (or otherwise changes dependencies), those hooks would otherwise import a member missing from the shared `.venv` and fail with `ModuleNotFoundError`. Syncing `--all-packages` first populates the environment so they pass. (The complementary removed-member case is already handled by the existing `clean-stale-workspace-dirs` post-checkout hook.)

Retire the hand-written git-hook installer: delete `scripts/githooks/install.sh` and `scripts/githooks/pre-commit`, and update `scripts/ruff-precommit-setup-guide.md` to install hooks with `uv run pre-commit install` instead. The hand-written shim existed to avoid `pre-commit install` depending on the system Python, but running `pre-commit install` through `uv` already pins the generated hooks to the uv-managed virtual environment (`.venv`), so the shim was redundant. The symlink-based installer was also incomplete -- it only ever installed the `pre-commit` hook, never the `pre-push` or `post-checkout` hooks the configuration relies on -- whereas `pre-commit install` installs every hook type in `default_install_hook_types`.

# Test-efficiency groundwork: offload v0.9.6 + minds e2e snapshot script

Two changes that together lay the groundwork for much faster minds
end-to-end tests:

- Bumped the offload CI pin from `0.9.5` to `0.9.6` (`.github/workflows/ci.yml`).
  v0.9.6 adds `offload run --override-image-id <ID>`, which lets us point
  offload at a pre-built Modal image and skip the entire image-setup
  pipeline (Modal provider only). See
  https://github.com/imbue-ai/offload/releases/tag/v0.9.6 for the full
  release notes.
- Added `scripts/snapshot_minds_e2e_state.py`, a demonstration script that
  creates a Modal sandbox with `experimental_options={"vm_runtime": True}`,
  installs the Docker + Node + pnpm + xvfb stack the
  `test-docker-electron` CI job needs, calls the shared
  `imbue.minds.desktop_client.e2e_workspace_runner.create_workspace_via_electron`
  driver directly (no pytest) while deliberately skipping the
  `mngr destroy` cleanup so the workspace agent + Docker container
  survive into the snapshot, and then calls
  `sandbox.snapshot_filesystem()` to capture the state. The resulting
  Modal image ID can be fed back to offload via `--override-image-id` so
  future test runs boot from an already-warm workspace + Docker
  container in seconds instead of rebuilding from scratch every time.
  The script intentionally opts in to `vm_runtime` only for itself --
  Modal has capacity issues with that runtime, so we do not flip it on
  for the general mngr_modal provider.

# Consolidated ty/ruff ratchet tests to run once repo-wide

The per-project `test_no_type_errors` and `test_no_ruff_errors` tests (~36 copies,
one per workspace member) were redundant: `ty check` resolves the uv workspace
root (root `pyproject.toml` declares `[tool.uv.workspace] members = ["libs/*",
"apps/*"]`) and scans every member on each invocation regardless of the directory
it runs from, and the repo-wide ruff check is a strict superset of the per-project
ruff checks. Each duplicate invocation was a full ~0.8s cold workspace scan with
no cross-process cache benefit.

Removed the per-project copies and kept a single repo-wide `test_no_type_errors`
and `test_no_ruff_errors` in `test_meta_ratchets.py`, updating the meta-ratchet
expected-test-name set accordingly.

Because `ty` (unlike `ruff`) was not in pre-commit, scoped local runs such as
`just test-quick libs/<project>` no longer type-checked at all after the
consolidation. Added a `ty` hook to `.pre-commit-config.yaml` that runs
`uv run ty check` over the whole workspace at the `pre-push` stage (ty can't
scope to staged files, so running it per-commit would add a fixed full-workspace
scan to every commit). Pushes now get a type-check gate; the single
`test_no_type_errors` in `test_meta_ratchets.py` remains the CI backstop.

No user-facing behavior change.

## 2026-05-27

# Bump `ty` to 0.0.39, plus paramiko/coolname dependency bumps

- Raised the `ty` type checker floor from `0.0.24` to `0.0.39` (root `pyproject.toml`).
- Bumped pinned dependencies in `uv.lock`: `paramiko` 3.5.1 -> 4.0.0 and `coolname` 3.0.0 -> 5.0.0. The paramiko bump also pulls `pyinfra` 3.6.1 -> 3.8.0 and adds `invoke` and `types-paramiko` transitively (pyinfra 3.8.0 depends on `types-paramiko`).
  - Note: paramiko 4.0.0 is the ceiling while we depend on `pyinfra`; pyinfra 3.8.0 constrains `paramiko<5`, so paramiko 5.0.0 is not yet installable.
  - The newly-present `types-paramiko` stubs make ty type-check paramiko usage for the first time; resulting type errors were fixed across the affected projects.
- Behavioral note for contributors: `ty` 0.0.39 no longer honors the bracketed PEP-484 form `# type: ignore[<mypy-code>]`. Only bare `# type: ignore` and `ty`'s own `# ty: ignore[<ty-rule>]` are respected. All bracketed `# type: ignore[...]` comments in the repo were converted to `# ty: ignore[...]` using ty's rule names.
- Documented in `CLAUDE.md` (the "# Ratchets" section) how to tighten a ratchet count after reducing violations: `uv run pytest --inline-snapshot=trim <test_ratchets.py>` (only `=trim` lowers a count that already passes its `<=` check; `=fix`/`=update` do not).
- Tightened recorded ratchet violation counts to their current exact values across all projects via `--inline-snapshot=trim`, locking in previously-unrecorded reductions (test-config only; no source or behavior change).
- Ran `uv lock --upgrade` under a two-week supply-chain cooldown (adopting only releases that have been public for at least two weeks) to bump floating dependencies. Notable bumps within that window: `starlette` 0.50 -> 1.0, `urwid` 3.0 -> 4.0, `pydantic` 2.12 -> 2.13, `cryptography` 46 -> 48, `typer` 0.21 -> 0.25, `uvicorn` 0.40 -> 0.46. The cooldown holds back even-newer releases, e.g. `wsgidav` stays 4.3.3 rather than 4.3.4 (4.3.4 adds a `bcrypt<5` cap and a `passlib` dep), so `bcrypt` stays at 5.0.
  - Bumped the `supertokens-python` floor (see the `remote_service_connector` changelog) so the resolver keeps it at the latest 0.31.3 instead of backtracking to 0.30.3; that also keeps `aiosmtplib` at 5.x for free.
- Added `test_no_dependencies_younger_than_two_weeks` (in `test_meta_ratchets.py`) to enforce the cooldown: it fails if any locked dependency was published within the last two weeks, except deliberately-trusted exemptions (`ty` -- our dev-only type checker, pinned to the latest 0.0.39; `modal` -- explicitly pinned to ==1.4.3). uv's static `[tool.uv] exclude-newer` only accepts a fixed date, so the relative cutoff lives in this (time-relative) test instead; regenerate compliant locks with `uv lock --upgrade --exclude-newer "2 weeks"`. The cooldown does not protect us from a compromise that stays undetected past the window, nor the first project to lock a release -- its only value is the detection delay before we adopt (and, for runtime deps in published wheels, re-propagate) a release.

## 2026-05-26

# Repo-root spec annotation

[`specs/minds-rest-api/spec.md`](../../specs/minds-rest-api/spec.md)
got a top-of-file banner noting that the per-agent `MINDS_API_KEY` and
the per-agent reverse SSH tunnel for the Minds API are both gone --
agents now reach the API exclusively through the latchkey gateway's
`minds-api-proxy` extension, with a single installation-wide
`MINDS_API_KEY`. See the changelogs for the `minds` and `mngr_latchkey`
projects for the full design + implementation notes.

- Updated the minds Electron acceptance test spec (``specs/minds-electron-acceptance-test/spec.md``) to reference ``launch_mode=DOCKER`` instead of ``launch_mode=LOCAL``, matching the corresponding minds enum rename. The test code in ``apps/minds`` was already updated; this brings the spec in sync.

- Updated the nightly changelog consolidation prompt (`scripts/changelog_consolidation_prompt.md`) so the concise `CHANGELOG.md` is a notable-only summary: non-notable changes (canonically, changes that only affect tests rather than user-facing behavior) are now omitted from `CHANGELOG.md` entirely instead of being forced into a `Changed` bullet. Such entries are still preserved verbatim in each project's `UNABRIDGED_CHANGELOG.md`.
- Added a `dev`-project exception to that rule: because `dev` tracks repo-level developer tooling (CI, scripts, build config, ratchets, the changelog system) rather than product behavior, `dev` entries are judged by developer/maintainer impact rather than end-user-facing behavior.

# CI guard for stale generated CLI docs

`scripts/make_cli_docs.py` gained a `--check` mode that reports any stale
generated docs (and the exact regen command) and exits non-zero without writing
anything. Its content generation was refactored so a single
`collect_generated_files()` function is the shared source of truth for both
writing the docs and checking them, so the writer and checker cannot drift.

A new `test_cli_docs_are_up_to_date` (in `test_meta_ratchets.py`, alongside the
existing repo-wide ruff check) runs that `--check` mode and fails if the
committed CLI docs or PyPI README are out of date, pointing you at
`uv run python scripts/make_cli_docs.py`. This complements the existing
`test_all_non_hidden_commands_have_generated_docs`, which only checks that a doc
file exists per command, by also verifying the file contents are current.

Workspace + scripts metadata follows the rename of `libs/mngr_gemini` to `libs/mngr_antigravity`: workspace `pyproject.toml` cov target, `test_profiles.toml` mngr-suite test paths, top-level `README.md`, and the package list in `scripts/utils.py`.

- Added `specs/env-settings-overrides/concise.md` documenting the new `MNGR__*` env-var override scheme, the `__extend` operator, and the assign-by-default merge semantics shipped with this PR. See the `mngr` changelog entry for the user-visible behavior.

Broadened the autofix auto-accept rules to cover any pure DRY cleanup that is a clear
improvement and doesn't change behavior (e.g. inline-re-construction folded into a
pre-existing local). Previously the rule only listed specific cases.

## 2026-05-26

## dev

- TMR workflows (`tmr.yml`, `tmr-reintegrate.yml`) now re-assert `mngr tmr`'s exit code via `exit "${PIPESTATUS[0]}"` after the `| tee tmr-report/events.jsonl` pipeline. The implicit `pipefail` propagation was observed to not catch the left-side failure in this step, letting a failed run be reported as successful.

## 2026-05-22

- New direct dependencies recorded in `uv.lock` to support the minds
  WebDAV file-server mount: `wsgidav` (the WebDAV server itself) and
  `a2wsgi` (the WSGI-to-ASGI adapter that bridges it onto Starlette /
  FastAPI). Both are pulled in via `apps/minds/pyproject.toml`.

- The `TMR` GitHub Actions workflow now runs on a daily cron at 08:00 UTC (00:00 PST; shifts to 01:00 PDT in summer, since GitHub Actions cron has no timezone support). The cron lives in a new `TMR (scheduled)` workflow that gates on a prior periodic PR and then invokes the main `TMR` workflow via `workflow_call`; manual `workflow_dispatch` runs of TMR remain independent of the gate.
- The default `test_paths` workflow input now points at the whole `libs/mngr/imbue/mngr/e2e/` directory instead of only `test_basic.py`, so both scheduled and one-click runs exercise the full e2e suite.
- Scheduled-run gate behavior:
  - If a prior scheduled run's PR (label: `tmr-periodic`) is open and 4 days old or younger, today's scheduled run is skipped and a new comment is posted on the open PR explaining the policy. The recurring daily nudge is intentional.
  - If the prior PR is more than 4 days old, the gate posts a closing comment, closes the PR (with `--delete-branch`), proceeds with a fresh run, and after the new PR is opened posts a follow-up "Superseded by #N" comment on the closed PR.
- The auto-opened PR from scheduled runs is labeled `tmr-periodic` (the label is created on demand) and assigned to `qi-imbue` and `joshalbrecht`. Manual-run PRs are unlabeled, unassigned, and therefore invisible to the gate.

## Spec: discovery providers and errors

- Add `specs/discovery-providers-and-errors/concise.md` describing the cross-project change that promotes per-provider state (successfully loaded providers, per-provider discovery errors) to first-class fields on `FullDiscoverySnapshotEvent`, replaces minds' silent auto-disable-on-auth-error machinery with a visible providers panel + explicit Enable/Disable toggle, adds a new `UNKNOWN` value to `AgentLifecycleState` / `HostState` for previously-tracked agents whose provider just failed, and teaches `mngr_notifications` to recognize the indirect `RUNNING -> UNKNOWN -> WAITING` transition. See the per-project changelog entries in `libs/mngr/`, `libs/mngr_forward/`, `libs/mngr_imbue_cloud/`, `libs/mngr_notifications/`, and `apps/minds/` for the actual code changes this spec describes.

## 2026-05-21

- `CLAUDE.local.md` is now copied into agent workdirs by default, so user-specific Claude instructions from the host repo are available inside agents.

Adds a `just minds-test-electron` recipe that wraps the new `test_create_local_docker_workspace_via_electron` Electron acceptance test in `xvfb-run -a`, and wires the existing `test-docker` CI job to install Node, pnpm, xvfb, and the apps/minds pnpm dependencies so the Electron binary is available for the run.

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

Add `specs/minds-env-activate-split/concise.md`: design for splitting
`minds env activate` into a default use-mode (no `MODAL_PROFILE`) and an
opt-in `--deploy` mode. Fixes the spurious Modal-discovery warnings and
Latchkey breakage hit by users who activated `staging` only to *use* the
deployed tier but had no Modal token for the `minds-staging` workspace.

Root-level surface changes for the `mngr_robinhood` plugin: README updated to advertise the new `robinhood` command and link to the new sub-project, and the auto-generated CLI docs gained an entry at `libs/mngr/docs/commands/secondary/robinhood.md` so `mngr ask` and `mngr --help` know about the command.

## 2026-05-20

Restructure the changelog system from a single repo-wide changelog to one set of changelog artifacts per project, owned inside each project's own directory.

- Each project (every `libs/<name>` and `apps/<name>`, plus the synthetic top-level `dev/`) now holds three things at its root: `changelog/` (per-PR entry files), `CHANGELOG.md` (concise summary), and `UNABRIDGED_CHANGELOG.md` (verbatim per-date sections).
- Per-PR entry files now live at `<project_dir>/changelog/<branch>.md` (one per project the PR touches), instead of a single `changelog/<branch>.md` at the repo root.
- The consolidator (`scripts/consolidate_changelog.py`) walks each project's `<project_dir>/changelog/` and routes its entries into `<project_dir>/UNABRIDGED_CHANGELOG.md`. The machine-readable output format is now one `SECTION <project> <date>` line per inserted section.
- The `test_pr_has_changelog_entry` ratchet now computes the projects the PR diff touches and requires `<project_dir>/changelog/<branch>.md` for each. Adding the entry file inherently satisfies the requirement for the project that owns it; the consolidation cron's own branch prefix is the only special-cased exemption.
- New `test_every_project_has_changelog_layout` meta-ratchet enforces that every project has `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md`, and a `changelog/` directory. Stubs were added for projects without entries yet.
- `scripts/changelog_consolidation_prompt.md` updated to parse `SECTION` lines and summarize each project's section into that project's `CHANGELOG.md` `[Unreleased]`.
- `scripts/release.py` finalizes each bumped package's and each first-time-publish package's `libs/<name>/CHANGELOG.md` `[Unreleased]` section. `apps/<name>/CHANGELOG.md` and `dev/CHANGELOG.md` are not versioned, so their `[Unreleased]` accumulates entries indefinitely.
- New shared `scripts/changelog_projects.py` owns the path-to-project mapping (used by the consolidator, the ratchet, and the release script).
- `test_meta_ratchets._get_all_project_dirs` and `all_known_projects` now both build on a shared `pyproject_projects()` helper in `scripts/changelog_projects.py`, instead of `_get_all_project_dirs` going through `all_known_projects` and filtering out the synthetic `dev` bucket.
- The `test_pr_has_changelog_entry` ratchet's "missing entries" failure message now names the resolved diff base and warns that a misconfigured/stale base can make unrelated `main` files appear as if they changed on this branch, falsely implicating projects the PR didn't touch — in which case the right fix is to refetch the base, not to add placebo entries for projects you didn't actually change.

The existing top-level `CHANGELOG.md` and `UNABRIDGED_CHANGELOG.md` were retroactively split into per-project files; see each project's `CHANGELOG.md` for its history.

`scripts/release.py` now refuses to cut a release when there are unconsolidated entries in `changelog/`, since those would otherwise be omitted from the version's release notes. When the gate fires it prints the exact one-liner that triggers the `changelog-consolidation` schedule on demand (the same one that normally runs nightly), so the human can run it, land its PR, and re-run the release. The predicate ("are there pending entries?") lives next to the consolidator's own filter in `scripts/consolidate_changelog.py`, and the plugin-disable args used around `mngr schedule` invocations live in `scripts/trigger_changelog_consolidation.py` and are shared by `scripts/setup_changelog_agent.sh`.

Collapse Modal environments across an offload-acceptance / offload-release
run to a single shared env (opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`).
Each fanned-out sandbox in `just test-offload-acceptance` and
`just test-offload-release` used to mint its own Modal environment and
delete it on teardown -- dozens to hundreds per run, driving the
1500-env-per-workspace cap into transient failures. The justfile recipes
now pre-create a single `mngr_test-YYYY-MM-DD-HH-MM-SS-shared-<uuid>` env
once, forward its name into every sandbox via `--env`, and `trap`-delete
it at recipe exit.

- The TMR GitHub Actions workflow now defaults `MNGR_USER_ID` to the shared `tmr-ci` namespace and reads inbound-SSH authorized keys from the checked-in `.github/tmr-authorized-keys` file (in addition to the existing `additional_authorized_hosts` workflow input). To register your key, run `uv run --project libs/mngr_tmr python libs/mngr_tmr/scripts/setup_tmr_ci_debug.py` and append the printed public key to that file via PR; then debug CI-created modal agents locally with `MNGR_HOST_DIR=~/.mngr-tmr-ci uv run mngr list` / `mngr connect`.
- The TMR GitHub Actions workflow passes the AWS secrets through for the S3 report mirror and uses the public URL in the auto-opened PR body, falling back to the existing `tmr-report` artifact when no upload happened.
- The main `TMR` GitHub Actions workflow accepts a corresponding `run_name` workflow_dispatch input, and a new `TMR (reintegrate)` workflow takes that run name back as a required input and runs `mngr tmr --reintegrate <run>` against it (re-running just the integrator phase, opening the same kind of draft PR).
- The two TMR workflows share a new `.github/actions/tmr-setup` composite action for their common setup steps.

## 2026-05-14

CI acceptance test speedups (workflow-side):

1. Grant `contents: write` to the `test-offload` and `test-offload-acceptance` jobs so offload can push its image-cache git notes back to `refs/notes/offload-images`. Previously every run was a cache miss (the `git push` from offload failed with "Permission to imbue-ai/mngr.git denied to github-actions[bot]"), forcing a full `checkpoint_base_prepare` rebuild (~150 s wasted per CI run on acceptance, similar on the regular offload job). Measured saving on cache hit: ~124 s per acceptance run.

2. Lower `max_parallel` from 200 to 50 in `offload-modal-acceptance.toml`. With 200 slots and ~89 tests, offload's LPT scheduler degenerated to one-test-per-batch, so every batch paid full pytest cold-start, Modal sandbox creation, and an orchestrator-side `uv run` cold-start per download. Lowering to 50 lets LPT pack ~2-4 tests per batch (longest single tests still alone via load-balancing). Combined measured saving: ~62% acceptance wall-clock reduction.

Bumped the pinned Claude Code CLI version from `2.1.116` to `2.1.141` in the `.github/workflows/{ci,tmr}.yml` install steps.

Removed the unused `libs/flexmux/` project and all references to it (justfile recipes, `EXCLUDED_RATCHET_PROJECTS` exclusions in `test_meta_ratchets.py` and `scripts/sync_common_ratchets.py`, and the `uv.lock` workspace member).

## 2026-05-12

- The changelog consolidator now groups entries by the date their PR landed on `main` (committer date of the introducing commit on the first-parent line, in America/Los_Angeles) and emits one `## YYYY-MM-DD` section per distinct date in `UNABRIDGED_CHANGELOG.md` (newest first), instead of bucketing everything under the consolidator's run-time UTC date.
- The abridged `CHANGELOG.md` is now version-organized instead of date-organized: a `## [Unreleased]` placeholder sits at the top of the file, the nightly consolidation cron appends categorized bullets (`Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`) under `### <Category>` subheadings in that section, and `scripts/release.py` renames `## [Unreleased]` to `## [vX.Y.Z] - YYYY-MM-DD` and inserts a fresh empty `[Unreleased]` above it as part of the release commit. Each cron-generated bullet is in the form `- <Category>: <description>`, and the cron does one refinement pass over `[Unreleased]` after drafting to tighten/dedupe before committing.
- Enabled auto-merge on the consolidation cron: each fire now runs `git fetch && checkout main && merge origin/main` before forking the per-run branch, so the eventual PR's diff against `main` is always just the consolidation commit -- no script-snapshot drift even if the cron is redeployed less often than `main` moves.

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

## 2026-05-08

- Fixed the changelog consolidation cron's commit author email: was `dev@imbue.com`, now `bot@imbue.com`, matching the verified email on the bot GitHub account whose token the cron uses to push and open PRs. Without this, GitHub couldn't attribute consolidation commits to the bot user.

- `scripts/setup_changelog_agent.sh` now redeploys when re-run: removes any existing `changelog-consolidation` schedule before recreating, so the deployed schedule always reflects the current source. Drops the `CHANGELOG_REPLACE=1` gate that previously errored on an existing schedule.
- Header docstring now lists the required `GH_TOKEN` (token for `bot@imbue.com`) and `ANTHROPIC_API_KEY` env vars, and includes the on-demand trigger one-liner.

- Removed an unused `# type: ignore[misc]` in `ssh_tunnel_test.py` so the type-error ratchet stops failing on it.

## 2026-05-06

Upgrade offload from 0.8.1 to 0.9.0 and enable history-based test scheduling.
Offload now records per-test durations and uses them to balance sandbox load times,
reducing wall-clock time for the test suite.

Upgrade offload from 0.9.0 to 0.9.2 in CI. Picks up a fix for thin-diff application. Adds the offload binary to the sandbox image (via a multi-stage build) so 0.9.2's `offload apply-diff` step works without falling back to a full rebuild, and propagates `GITHUB_HEAD_REF` / `GITHUB_REF_NAME` through to sandboxes so branch-aware tests like the changelog-entry ratchet identify the PR branch correctly.

## 2026-05-05

Every workspace package's wheel build now excludes test files uniformly via the same canonical line:

```
[tool.hatch.build.targets.wheel]
exclude = ["*_test.py", "test_*.py", "**/conftest.py", "**/testing.py"]
```

Previously, several packages were missing some or all of these patterns and hatchling was shipping `_test.py`, `conftest.py`, and `testing.py` files into published wheels. Notably `libs/mngr` was leaking three test helpers (`cli/testing.py`, `api/testing.py`, `providers/docker/testing.py`) because its existing pattern only covered `**/utils/testing.py`.

A new meta ratchet (`test_every_project_excludes_tests_from_wheel`) enforces the four-pattern rule on every project so this cannot regress.

## 2026-05-02

- Added a changelog system for tracking changes across PRs
  - Per-PR changelog entry files in `changelog/` directory, enforced by CI via meta ratchet test
  - Nightly automated consolidation of changelog entries into `UNABRIDGED_CHANGELOG.md` (full entries) and `CHANGELOG.md` (concise AI-generated summary)
  - Idempotent setup script for the consolidation agent (`scripts/setup_changelog_agent.sh`)

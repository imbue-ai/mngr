# Changelog - dev

A concise, human-friendly summary of changes for repo-level dev tooling: CI workflows, repo scripts, top-level configuration, build tooling, ratchets, and the changelog tooling itself. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## 2026-06-25

### Added

- Added: Blueprint planning document for the minds error-reporting & "get help" work (`blueprint/minds-error-reporting-help/`), scoping the full four-phase design (phases 1-2 implemented in this run; 3-4 will follow as stacked PRs).
- Added: Design doc `specs/tmr-bounded-convergence-and-normalization.md` for improving the e2e tests TMR generates — tutorial-anchored convergence with deletion as a first-class action, plus a suite-level normalization stage in the reducer and a FIXME-resolution/escalation lifecycle.
- Added: Design blueprint `blueprint/gateway-agent-id-validation/` documenting the decision to reject malformed permission-request `agent_id`s at the latchkey gateway instead of only guarding against them on the consumer side.

### Changed

- Changed: Bumped the offload CI pin in `.github/workflows/ci.yml` from `0.9.7` to `0.9.9` (cargo cache key, version check, and `cargo install` invocation updated to match).
- Changed: `just minds-start` no longer defaults the workspace name to `mindtest`. Its `agent_name` argument defaults to empty, so a plain `just minds-start` leaves `MINDS_WORKSPACE_NAME` unset and the create form generates an automatic `mind-N` name — matching what a shipped binary does. Pass a name explicitly to pin it (a collision now errors at create time rather than being auto-suffixed).
- Changed: Nightly changelog consolidation automation now merges its PR immediately instead of leaving it for a human to review and merge. The in-run accuracy review remains the quality gate.

## 2026-06-24

### Added

- Added: `scripts/remove_old_flat_vault_secrets.py` — one-off cleanup tool that deletes the old flat per-service Vault entries for a tier (`secrets/minds/<tier>/<service>`) once they have been mirrored into the split layout. Refuses to delete any entry whose split mirror is missing or whose keys/values disagree; defaults to dry-run; requires `--yes` to actually delete.

### Changed

- Changed: `scripts/install.sh` now runs `mngr config wizard` as a final step to populate common user-scope configuration (e.g. whether to isolate the Claude config dir for local agents). Like the other steps, it prompts before changing anything and is safe to re-run.
- Changed: `scripts/push_vault_from_file.py` writes each declared key as its own single-`value` leaf at `secrets/minds/<tier>/<service>/<KEY>` (the new "split" Vault secret layout) instead of a single flat KV entry with many fields. `scripts/changelog_deploy.sh` reads `GH_TOKEN` / `ANTHROPIC_API_KEY` from the split layout.
- Changed: Removed the minds app's `postinstall` CSS-compile hook (it broke ToDesktop's `--prod` cloud install). The CSS build is now wired in explicitly at its real consumption points: a "Build Tailwind CSS for the e2e app" step in the CI minds_electron e2e job, and a `minds-css` dependency on the `just minds-test-electron` recipe.

## 2026-06-23

### Added

- Added: `just minds-start-cloud` recipe — launches the minds desktop client in dev mode to test the `imbue_cloud` provider against pre-baked pool slices. Unlike `just minds-start`, it leaves the form's shipped fallbacks in place (the canonical forever-claude-template remote plus `FALLBACK_BRANCH`) so an `imbue_cloud` create matches and fast-path leases a slice baked at that tag instead of dropping to the slow rebuild path. Also skips the live-mngr to `vendor/mngr/` rsync.
- Added: `just backfill-pool-host-keys` recipe wrapping `minds pool backfill-host-keys` for the activated minds env — the one-time SSH host-key backfill to run once per tier after deploying the host-key-pinning connector.
- Added: Optional `fct` positional argument on `just minds-start` to point a launch at a specific forever-claude-template worktree (absolute path used as-is; relative resolved against the mngr root). Omitting it keeps the previous default.
- Added: New runtime dependency `traceback-with-variables` (used by the minds Sentry integration to format tracebacks with local variables); updates `uv.lock`.
- Added: Design blueprints — `blueprint/discovery-health-watchdog/`, `blueprint/pin-imbue-cloud-host-keys/`, plus revisions to `blueprint/remote-mind-recovery/`.

### Changed

- Changed: Rewrote the `release-minds` skill to be a thin pointer to `apps/minds/docs/release.md`, the canonical release runbook, instead of describing its own (now-obsolete) release flow.
- Changed: Consolidated the docs describing how FCT's `vendor/mngr` is kept in sync into a single canonical place (`apps/minds/docs/vendor-mngr-sync.md`); the `minds-dev-workflow` and `minds-justfile` skills now point at it instead of re-describing the mechanisms.

## 2026-06-22

### Added

- Added: Design blueprints — `blueprint/consistent-provider-auth-failures/`, `blueprint/robust-minds-list-provider-errors/`, `blueprint/minds-flask-migration/`, `blueprint/remove-system-interface-asyncio/`, and `blueprint/unify-remote-host-lock/`.

### Changed

- Changed: Renamed `just minds-tailwind` to `just minds-css` — it now compiles the minds desktop client's Tailwind v4 stylesheet (`static/app.css` -> minified `static/app.min.css`) via the pinned `@tailwindcss/cli`, instead of fetching the Tailwind Play CDN JS bundle. The `.gitignore` entry tracks the new compiled artifact (`app.min.css`) in place of the retired `tailwind.js`.
- Changed: `forward-system-interface` justfile recipe now resolves an agent's id with `mngr list --on-error continue`, so an unauthenticated/unreachable provider no longer aborts the lookup of a local agent.

## 2026-06-21

### Added

- Added: Design blueprints `blueprint/sshd-restart-robustness/` and `blueprint/share-bare-metal-across-dev-envs/`.

## 2026-06-20

### Removed

- Removed: `bake-pool-host-{dev,prod}` justfile recipes (they baked OVH classic VPS pool hosts, now deprecated). Pool hosts are baked as bare-metal slices via the `bake-slice-{dev,prod}` recipes; `list-pool-hosts` and `destroy-pool-host` are unchanged and still cover legacy OVH VPS rows. The `minds-justfile` and `minds-dev-workflow` skill docs were updated to match.

## 2026-06-19

### Added

- Added: `specs/bare-providers/` (spec.md + concise.md + extraction_design.md) — design proposal for running agents directly on a cloud VM with no Docker container, introducing a substrate-x-realizer architecture and a staged rollout that later folds local/docker/lima/ssh into the same grid.
- Added: A set of provider specs under `specs/` (`provider-uniformity-review.md`, `provider-shape.md`, `implementing-a-provider.md`) covering all nine `mngr` provider plugins (modal, aws, azure, gcp, vultr, ovh, lima, docker, ssh), and `specs/provider-release-tests.md` condensed to a remaining-gaps tracker now that the shared `run_provider_release_trip{1..4}` harness has landed.
- Added: `blueprint/remote-mind-recovery/` design doc for extending minds' workspace-recovery flow to remote (Imbue Cloud) minds.
- Added: New `overlay` workspace library registered at the repo root (`[tool.uv.sources]` workspace source, plus `--cov=imbue.overlay` in the shared coverage flags).
- Added: New runtime dependency at the repo root (`uv.lock`): `google-cloud-storage>=2.18`, used by the GCP provider's offline `host_dir` GCS state bucket.

### Changed

- Changed: `make_cli_docs.py` now also generates the provider/agent config tables in each plugin README from the Pydantic field descriptions (the source of truth, also shown by `mngr config`), spliced between markers and verified by the docs `--check` gate so the tables can no longer drift from the code. The `regenerate-cli-docs` pre-commit hook now runs `make_cli_docs.py --check` (non-mutating, covering every generated file) and its trigger includes the provider/agent `config.py` / `plugin.py` sources.
- Changed: Updated the root pytest coverage config to track the renamed `imbue.mngr_vps` package (was `imbue.mngr_vps_docker`).

### Removed

- Removed: Monorepo-development-only paragraph (the `~/.local/bin` pre-commit shim note) from the top-level README so the published PyPI README stays focused on user-relevant content.

## 2026-06-18

### Added

- Added: New `identify-suspicious-edge-cases` skill — flags over-broad exception catches, fallback `else` branches, defensive guards, and unnecessary `| None` types under a given path.
- Added: `specs/provider-state-bucket/` design spec for the AWS / Azure offline state stored in cloud object storage (S3 / Azure Blob) so a stopped instance's host record, agent metadata, and `host_dir` are readable offline without hitting the 256-char EC2/VM tag-value limit.
- Added: `moto[s3]` to the root dev dependency group for in-memory S3 unit tests of the new AWS state bucket.

### Changed

- Changed: The `identify-*` skills (`identify-doc-code-disagreements`, `identify-inconsistencies`, `identify-outdated-docstrings`, `identify-style-issues`) now accept a `target_path` argument instead of a bare library name. You can scope them to a whole library or to any subdirectory within one; each skill resolves the scan scope and its containing library, and writes findings to the containing library's `_tasks/` folder.

## 2026-06-17

### Added

- Added: `scripts/make_agent_capabilities_doc.py`, a dev-only generator for a code-derived agent-capability matrix doc. It loads every installed mngr plugin (local backend only), builds the matrix from the agent classes plus their plugins, and either rewrites `libs/mngr/docs/concepts/agent_capabilities.md` or, with `--check`, fails if it is stale. Mirrors `scripts/make_cli_docs.py` and stays out of the shipped `mngr` wheel. Paired with a new `just regenerate-agent-capabilities-doc` recipe.
- Added: `just bake-slice-dev` and `just bake-slice-prod` recipes for baking bare-metal slices (lima/QEMU VMs on a pre-registered, prepped OVH bare-metal box) into the minds pool. Thin wrappers over `minds pool create --backend slice`, mirroring the existing `bake-pool-host-{dev,prod}` recipes for OVH VPSes.
- Added: Design specs — `specs/agent-plugin-parity/capability-mixins.md` proposing the code-derived agent capability taxonomy that shipped this run; `specs/gcp-azure-stop-start-lifecycle/spec.md` for bringing the AWS stop/start (idle-pause + resume) lifecycle to GCP and Azure; and `specs/common-transcript-standard/spec.md` tracking the OpenTelemetry GenAI semantic conventions in the agent-agnostic common-transcript schema (a `stop_reason` → `finish_reason` vocabulary alignment across all five emitters, plus a universal ordered `parts[]` field).

### Changed

- Changed: Updated `specs/agent-plugin-parity/spec.md` (new "Ordered assistant parts[]" row, transcript-capture note) and updated `specs/agent-plugin-parity/capability-mixins.md` to match what shipped (the three-state `Y`/`-`/`n/a` matrix with the code-derived `CapabilityScope` model, the positive `CliBackedAgentMixin` kind marker, the unified `live_output` capability, and the `session_resume` capability).
- Changed: The `just destroy-pool-host` recipe comment now documents that teardown mirrors the row's backend — cancelling the OVH VPS for an `ovh_vps` row, or destroying the lima VM (freeing the box slot) for a `slice` row — and that `--skip-vps-cancel` is for when the underlying machine is already gone.

## 2026-06-16

### Added

- Added: Root-level wiring for the new `azure` provider plugin — `--cov=imbue.mngr_azure` in pytest coverage, `azure` registered in `scripts/make_cli_docs.py` `SECONDARY_COMMANDS` (so `mngr azure` gets a generated doc page alongside `aws` / `gcp`), and an `azure` create template in `.mngr/settings.toml` that builds the project Dockerfile on the VM (so azure agents get `gh` and the full mngr toolchain). `[providers.azure] builder = "DEPOT"` builds on depot's cached remote builders (requires `DEPOT_TOKEN` at create time).
- Added: Design specs — `specs/agent-usage-plugins/spec.md` (extending `mngr usage` to OpenCode, pi, and Codex), `specs/aws-ec2-stop-start-lifecycle/` (Modal-like idle-paused-but-resumable lifecycle for AWS agents via native EC2 stop/start; phases 1, 2, and 4 marked implemented), and `specs/cleanup-error-aggregation.md` (`mngr stop`/`destroy`/`cleanup` aggregate and classify failures with cause-specific exit codes).
- Added: Documented the install-wizard surfacing of the usage plugins in `specs/agent-usage-plugins/spec.md` and recorded the antigravity gap in `specs/agent-plugin-parity/spec.md` (new "Usage tracking plugin" row).

### Changed

- Changed: Synced the root design specs to the removed VPS-client snapshot surface and `list_ssh_keys` (`specs/vps-docker-provider/`, `specs/ovh-vps-provider/`, `specs/azure-provider/concise.md`, `specs/aws-ec2-stop-start-lifecycle/spec.md`).
- Changed: Extended the local-scratch `.gitignore` convention to Python and text files — `**/*.local.py` and `**/*.local.txt` are now ignored, mirroring the existing `**/*.local.md` and `**/*.local.sh` patterns. Lets one-off validation harnesses and probe scripts stay untracked and survive the stop hook's working-tree cleanup.
- Changed: `justfile`'s `sync-vendor-mngr` recipe realigned with the current release flow — its comment now tells you to position the mngr checkout at the **verified release SHA** (not blindly `main`, which can drift past it), points at `apps/minds/docs/release.md`, and no longer hardcodes a personal FCT path: the FCT checkout path comes from the positional arg, else `FCT_DIR` read from a gitignored minds-scoped `apps/minds/.env` (template: committed `apps/minds/.env.example`), else `$FCT_DIR` in your shell.

### Fixed

- Fixed: `minds-launch-to-msg.yml` now resolves and renders the ref name **and** the resolved commit instead of a tag-object SHA. The Slack notification and step summaries previously resolved `commit_sha` / `template_ref` with `git ls-remote refs/tags/<tag>` (no peel), so a run against an **annotated** tag (e.g. `minds-v0.3.1`) displayed the tag-*object* SHA — a SHA you can't `git checkout`. The `check_should_run` compute step now peels annotated tags (`^{}`), so no step surfaces a tag-object SHA anymore.

## 2026-06-15

### Added

- Added: `just minds-install` recipe that installs the minds desktop client's node deps (electron, etc.) using the Node version pinned in `apps/minds/.nvmrc` (selected via `select_node_version.sh`), so installs no longer fail with `ERR_PNPM_UNSUPPORTED_ENGINE` when the shell's default node has drifted off the pin. `just minds-start`'s "not installed yet" hint points at it.
- Added: Design doc `blueprint/ovh-baremetal-slices/` for extending the imbue_cloud pool to allocate lima/QEMU VM "slices" on rented OVH bare-metal servers, and `blueprint/mngr-imbue-cloud-module-layers/` proposing the layered sub-package structure for `mngr_imbue_cloud` (with the `import-linter` ordering contract).
- Added: `import-linter` "mngr_imbue_cloud layers contract" (root `pyproject.toml`) plus a `test_meta_ratchets.py` test that enforces it.

### Changed

- Changed: Replaced `just bake-pool-host` with `just bake-pool-host-dev` (bake from a working tree — best-effort branch label) and `just bake-pool-host-prod` (clone an exact FCT tag — strict), reflecting that the imbue_cloud pool bake now derives the stamped repo identity from its source rather than from hand-typed `--attributes`. `just bake-pool-host-dev` also passes `--skip-deferred-install-wait` so dev pool bakes skip the few-minute deferred Playwright/apt install before stopping the services agent. The `minds-justfile` skill documents the dev-vs-production distinction.
- Changed: Expanded CLAUDE.md flaky-test guidance — first investigate why a test is flaky and try to make it more robust; if it is correct but fundamentally needs more time, bump that test's timeout (avoid unreasonably long timeouts — prefer leaving it marked flaky for infrastructure-level flukes).
- Changed: Bumped the per-test timeout on the `test_cli_docs_are_up_to_date` meta-ratchet — the enlarged imbue_cloud CLI surface (the new `admin server` + slice commands) made full CLI-doc regeneration exceed the default 10s pytest-timeout in the slower offload sandbox.

### Fixed

- Fixed: Per-PR changelog enforcement check, which had been passing vacuously in CI. The check previously ran as an acceptance test inside the offload Modal sandbox, but the sandbox's fresh `git init` made `main == HEAD` so the base-branch diff always came back empty — and any PR could merge without changelog entries. Enforcement now lives in a dedicated CI gate (`scripts/check_changelog_entries.py`, run via the `check-changelog` GitHub Actions job and `just check-changelog`) that computes the changed-file set against the real base branch on the orchestrator, and refuses to run with a non-zero exit if it cannot resolve a diff base distinct from HEAD.

## 2026-06-14

### Added

- Added: `scripts/extract_antigravity_proto_schema.py` -- a developer tool that recovers antigravity's (`agy`) protobuf schema by scanning the `agy` binary for embedded `FileDescriptorProto`s (agy ships no `.proto` files). Promoted from an inline appendix in `libs/mngr_antigravity/dev/README.md` so the new antigravity schema-verification release test can invoke it directly.
- Added: Implementation plan for the AWS minds compute provider under `blueprint/aws-minds-compute-provider/`.

### Changed

- Changed: The `dev` project's `CHANGELOG.md` is now date-organized (mirroring `UNABRIDGED_CHANGELOG.md`) instead of carrying an ever-growing `[Unreleased]` section; the nightly consolidation now summarizes each landed date independently into its own `## <date>` section, per `scripts/changelog_consolidation_prompt.md`.
- Changed: Updated `uv.lock` to add the `anthropic` package (and its transitive `docstring-parser` dependency), newly required by `libs/mngr_claude` for the shared typed Claude stream-json envelope.

### Fixed

- Fixed: `scripts/changelog_deploy.sh` now stops *every* Modal app in the changelog schedule's isolated environment before redeploying (via a new `--stop-all-apps` action in `scripts/changelog_schedule_utils.py`). A past app-naming-scheme change had orphaned an old cron app that kept firing a second nightly `mngr/changelog-consolidation-*` branch; sweeping the whole environment makes redeploys orphan-proof.
- Fixed: `modal app stop` invocations (in `scripts/modal_nuke.py` and the new changelog sweep) now pass `--yes`, so they no longer abort with "no interactive terminal detected" under newer Modal CLIs when run non-interactively.

## 2026-06-13

### Added

- Added: `aws` create-template in `.mngr/settings.toml` for dogfooding the codebase on AWS EC2 -- `mngr create -t aws <name>` builds the dev Dockerfile and runs an agent on EC2. The shared `[providers.aws]` config (`us-west-2`, `t3.large`, `auto_shutdown_minutes = 120`, `builder = "DEPOT"`) is committed; `DEPOT` requires exporting `DEPOT_TOKEN` and `GH_TOKEN`.
- Added: New design specs and blueprints under `specs/` and `blueprint/` (agent-plugin parity, env-settings overrides, discovery error-resilience, two-tier workspace recovery, host / imbue-cloud backups, R2 buckets, docker cleanup and gvisor hardening, the workspace color picker, the JinjaX template migration, the agent SDK, and more).
- Added: New Claude skills -- `minds-justfile` (routes minds tasks through the root justfile), `audit-ci` (audits recent CI runs for anomalies), and `identify-bad-tests` (flags low-quality / fragile tests into the library's `_tasks/`).
- Added: Changelog / release justfile recipes -- `just release` wraps `scripts/release.py`, `just changelog-deploy` (re)deploys the nightly consolidation schedule, and `just changelog-trigger` runs consolidation on demand. Plus pool-host recipes `just bake-pool-host` / `list-pool-hosts` / `destroy-pool-host` wrapping the env-aware `minds pool` CLI.
- Added: Nightly changelog consolidation now runs a per-project accuracy review, spawning reviewer subagents that verify the generated bullets against the actual code and commit corrections.
- Added: Supply-chain cooldown enforced at lock time -- root `pyproject.toml` `[tool.uv] exclude-newer` makes `uv lock` refuse any package newer than the cutoff, which `scripts/release.py` advances forward-only to `(today_utc - 2 weeks)` each release.
- Added: Pre-push hooks in `.pre-commit-config.yaml` -- `uv-sync-pre-push` runs `uv sync --all-packages` when a push touches `uv.lock` / `pyproject.toml` (so later `uv run` hooks don't `ModuleNotFoundError` on a new workspace member), and a `ty` hook runs `uv run ty check` workspace-wide.
- Added: `scripts/make_cli_docs.py --check` mode reports stale generated CLI docs and exits non-zero; a new `test_cli_docs_are_up_to_date` meta-ratchet runs it so committed CLI docs / the PyPI README cannot drift from the generator.
- Added: Dev `mngr` shim (`scripts/mngr`) so `mngr` always runs the checkout you're working in (per-worktree, by cwd) instead of a stale global install; a pre-commit hook (`scripts/check_mngr_shim.sh`) installs and verifies it automatically.
- Added: New meta-ratchet `test_every_mngr_plugin_isolates_home_in_tests` -- every mngr plugin must call `register_plugin_test_fixtures(globals())` in a conftest so its tests redirect `$HOME` away from the developer's real home.
- Added: `scripts/snapshot_minds_e2e_state.py`, a Modal-sandbox script that boots the desktop client and snapshots the running workspace; the resulting image ID can be passed to offload via `--override-image-id` to boot e2e runs from an already-warm workspace.
- Added: New CI workflows -- `release-tests.yml` (runs release tests on `workflow_dispatch` / `v*` tags, with `release.py` warning if it has not passed on the commit being tagged), self-hosted-macOS `minds-launch-to-msg.yml` (+ `minds-runner-reset.yml`), and a daily TMR cron plus a `TMR (reintegrate)` workflow sharing a `tmr-setup` composite action.
- Added: `just minds-test-electron` recipe (wraps the Electron acceptance test in `xvfb-run`; the `test-docker` job now installs Node / pnpm / xvfb) and `just test-sdk-live` (runs the `sdk_live`-marked live Claude Agent SDK tests).
- Added: Twice-daily `minds launch-to-first-message` schedule that builds and verifies current mngr `main` against FCT `main` with the full slack flow, surfacing drift before each workday.
- Added: Updated `.minds/template/cloudflare.sh` to document that `CLOUDFLARE_API_TOKEN` must now be an account-owned token with R2 storage permissions.
- Added: Design plan under `blueprint/host-backup-snapshot-rotation/` for fixing empty gVisor host backups -- unique time-named btrfs snapshots, keep-newest-N retention, and exit-code-only backup failure signaling.

### Changed

- Changed: Restructured the changelog consolidation prompt (`scripts/changelog_consolidation_prompt.md`) for more concise summaries -- concise `CHANGELOG.md` bullets are generated once per project (not once per date, which created cross-date duplicates), followed by a critical concision pass that drops non-notable bullets; the merge step also drops `Fixed` entries for bugs introduced and fixed within the release window.
- Changed: Nightly consolidation now treats each `CHANGELOG.md` as a notable-only summary -- non-notable (canonically, test-only) changes are omitted entirely (still kept verbatim in `UNABRIDGED_CHANGELOG.md`), with a `dev`-project exception judged by developer / maintainer impact.
- Changed: Restructured the changelog system to one set of artifacts per project -- per-PR entries live at `<project_dir>/changelog/<branch>.md` and the consolidator routes each into the project's own `UNABRIDGED_CHANGELOG.md`; ratchets enforce the layout.
- Changed: Renamed the changelog tooling scripts to share a `changelog_` prefix (`changelog_consolidate.py`, `changelog_schedule_utils.py`, `changelog_deploy.sh`); `changelog_deploy.sh` now reads `GH_TOKEN` / `ANTHROPIC_API_KEY` from Vault at deploy time (`vault login -method=oidc` first).
- Changed: `scripts/release.py` now refuses to cut a release when any `changelog/` has unconsolidated entries, and points users at `just changelog-trigger` to run the consolidation on demand.
- Changed: Release tooling now auto-discovers the publish graph from the workspace (every `libs/*` package is a candidate unless listed in `UNPUBLISHED_PACKAGES`, enforced by a ratchet), walks every member's deps / extras / groups for internal-pin alignment (`test_internal_dep_pins_are_consistent`), and considers the full release-candidate cascade when offering new packages for first publication.
- Changed: `imbue-mngr-skills` is now published from its own GitHub repo as a plugin marketplace (mirroring `imbue-code-guardian`); this repo dogfoods the published plugin instead of carrying the skills in `.claude/skills/`.
- Changed: AWS provider root-level changes -- regenerated `mngr create` CLI docs (new AWS build-args help, dropped the Vultr / OVH `--vps-os=` line, per-provider prefix renames), added `aws` to `make_cli_docs.py` `SECONDARY_COMMANDS` and to top-level coverage, and added the six new AWS deps to `uv.lock`.
- Changed: Scripts (`release.py`, `modal_nuke.py`, `make_cli_docs.py`, `sync_common_ratchets.py`, `warm_cli_example.py`) no longer swallow unexpected failures -- PyPI lookups raise on network / HTTP errors, Modal identifiers come from documented `--json` keys (raising `ModalSchemaError` on schema drift), and several silent-fallback bugs now raise instead.
- Changed: `minds-launch-to-msg.yml` consolidated -- build moved to the self-hosted `minds-runner` Mac (so the `.app` ships Mac-native `uv` / `git` / `lima`), `commit_sha` made required (no stale-bundle escape hatch), the renamed `minds-macos-launch.yml` smoke folded in as a parallel job, and the shell-script + slack-mock pipeline rewritten as one Python script (`apps/minds/scripts/launch_to_msg_e2e.py`). Screenshots now ride only per-run GitHub artifacts (the ~1.2 GB `ci-screenshots` orphan branch was retired), with per-window Playwright captures as the headline shots.
- Changed: CI speedups -- acceptance wall-clock cut ~62% (`contents: write` for the image-cache git-notes push, `max_parallel` 200→50 for better LPT packing); `test-offload` / `test-offload-acceptance` unshallow only the current ref; and the dead `release`-branch jobs were removed (release tests moved to `release-tests.yml`).
- Changed: Consolidated `test_no_type_errors` / `test_no_ruff_errors` to run once repo-wide from `test_meta_ratchets.py`, removing ~36 redundant per-project copies.
- Changed: Retired the hand-written git-hook installer (`scripts/githooks/`) in favor of `uv run pre-commit install`, which installs every hook type (the old symlink installer only ever installed `pre-commit`).
- Changed: Dependency bumps under the two-week cooldown -- `ty` floor `0.0.24`→`0.0.39` (which no longer honors `# type: ignore[<code>]`; all were converted to `# ty: ignore[<rule>]`), majors via `uv lock --upgrade` (`paramiko` 3→4, `coolname` 3→5, `starlette` 0.50→1.0, `urwid` 3→4), the offload CI pin `0.9.5`→`0.9.7`, and Node-24 runtimes for `test-docker-electron` and all GitHub Actions.
- Changed: Tracked plugin renames / additions in root config -- `mngr_gemini`→`mngr_antigravity`, `mngr_uncapped_claude`→`mngr_robinhood`, and added `libs/mngr_mapreduce` to the workspace (+ coverage).
- Changed: Documented in `CLAUDE.md` -- release tests do not run in CI (must be run locally), per-PR changelog list bullets need a blank line between them, and how to tighten a ratchet count (`pytest --inline-snapshot=trim`). `CLAUDE.local.md` is now copied into agent workdirs so user-specific instructions are available inside agents.
- Changed: Skill fixes -- corrected stale dev-env naming in the `minds-dev-workflow` skill and `minds-start` hints (`dev-<user>`, tier prefix first), pointed `sync-tutorial-to-e2e-tests` at the new `e2e/tutorial/` dir, and removed the contradictory "commit when finished" notes from the `identify-*` skills.
- Changed: `just minds-start` selects the `.nvmrc`-pinned Node before launching (erroring with a hint if nvm / the version is missing, never auto-installing) and exports `MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1` so the create-form honors local-worktree defaults on any tier.
- Changed: `just forward-system-interface` writes the Cloudflare tunnel token to `runtime/secrets/cloudflare_tunnel.env`, matching the directory-based secrets layout; removed `.minds/template/paid-accounts.sh`, folding `MINDS_PAID_ADMIN_KEY` / `MINDS_PAID_LIST_CACHE_TTL_SECONDS` into `supertokens.sh` (paid-user tracking moved to DB tables).
- Changed: `scripts/snapshot_minds_e2e_state.py` sets `LATCHKEY_DISABLE_COUNTING=1` so the snapshot builder does not count toward Latchkey usage; genuine installs still count.
- Changed: TMR GitHub Actions workflow defaults `MNGR_USER_ID` to the shared `tmr-ci` namespace, reads inbound-SSH keys from a checked-in `.github/tmr-authorized-keys`, drops the removed `--use-snapshot` flag, and passes AWS secrets for the S3 report mirror.
- Changed: Collapsed Modal environments across `just test-offload-acceptance` / `test-offload-release` to a single shared env (opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`) to stay under the 1500-env-per-workspace cap; `sdk_live` tests are excluded from CI.
- Changed: Broadened the autofix auto-accept rules to cover any pure DRY cleanup that is a clear, no-behavior-change improvement.
- Changed: `scripts/install.sh` invokes `mngr dependencies --install interactive --scope core`, so a missing optional dependency (`ssh` / `rsync` / `unison` / `claude`) no longer trips the installer warning -- only missing core dependencies do.
- Changed: Updated the local-dev LiteLLM proxy config (`litellm_proxy/config.yaml`) to the full current Anthropic Claude lineup with per-token pricing, kept in sync with `apps/modal_litellm/app.py` by a drift test.
- Changed: `.gitignore` now ignores `**/*.local.sh` (mirroring `**/*.local.md`) and broadens the `_tasks/` rule to `**/_tasks/` so the root-level `dev/_tasks/` output folder is ignored too.

### Removed

- Removed: Broken `cleanup-pool-hosts` justfile recipe -- it sourced the long-gone `.minds/<env>/neon.sh` files (secrets moved to Vault) and was redundant with the connector's hourly release-cleanup cron; `destroy-pool-host` is the env / Vault-aware replacement.
- Removed: Unused `libs/flexmux/` project and all references (justfile recipes, ratchet exclusions, `uv.lock` workspace member).
- Removed: `test_no_dependencies_younger_than_two_weeks` from `test_meta_ratchets.py` -- the cooldown is now enforced at lock time via `[tool.uv] exclude-newer`, so the time-relative test is redundant.

### Fixed

- Fixed: Nightly changelog consolidation schedule fired at 8 AM Pacific instead of midnight -- the cron was set to `0 8 * * *` assuming UTC, but it is interpreted in the deploying machine's local timezone. Now uses `0 0 * * *` with an explicit `--timezone America/Los_Angeles`.
- Fixed: `just test-acceptance` marker expression was `-m "no release"` (a pytest syntax error that failed at collection); now `-m "not release"`.
- Fixed: TMR workflows now re-assert `mngr tmr`'s exit code via `exit "${PIPESTATUS[0]}"` after the `| tee` pipeline, so a failed run is no longer reported as successful when `pipefail` fails to propagate the left-side failure.
- Fixed: Added a `**/tmr-report/` pattern to the root `.gitignore` (the existing `**/tmr_*/` pattern used an underscore and did not match the dash-named directory).

### Security

- Security: Upgraded two vulnerable transitive dependencies in `uv.lock` to their fixed versions (surfaced by `uv audit`): `idna` 3.14→3.16 and `starlette` 1.0.0→1.0.1.

## 2026-05-13

### Added

- Added: `mngr_user_id` / `additional_authorized_hosts` `workflow_dispatch` inputs to the TMR GitHub Actions workflow.

### Changed

- Changed: `CHANGELOG.md` is now version-organized — `[Unreleased]` accumulates categorized bullets across cron runs and `scripts/release.py` renames it on each release.
- Changed: Changelog consolidator groups entries by PR-landed committer date (America/Los_Angeles) and emits one `## YYYY-MM-DD` section per distinct date in `UNABRIDGED_CHANGELOG.md`.
- Changed: Consolidation cron auto-merges `origin/main` before forking the per-run branch, so each PR's diff is just the consolidation commit.
- Changed: TMR GitHub Actions workflow uses the canonical `--format` flag (the previous `--output-format` was not a real option).

## 2026-05-11

### Added

- Added: Per-PR changelog entry system in `changelog/` with nightly consolidation into `UNABRIDGED_CHANGELOG.md` and a version-organized `CHANGELOG.md`; idempotent setup at `scripts/setup_changelog_agent.sh`.
- Added: New meta ratchet `test_every_project_excludes_tests_from_wheel` enforcing a uniform wheel-exclude pattern across every package.

### Changed

- Changed: Upgraded offload from 0.8.1 → 0.9.2 in CI with history-based test scheduling, thin-diff application fix, and propagation of `GITHUB_HEAD_REF` / `GITHUB_REF_NAME` to sandboxes.
- Changed: Workspace wheels uniformly exclude `*_test.py`, `test_*.py`, `**/conftest.py`, `**/testing.py` — previously `libs/mngr` was leaking three test helpers.
- Changed: `scripts/setup_changelog_agent.sh` redeploys when re-run (removes any existing schedule first) and drops the `CHANGELOG_REPLACE=1` gate; the consolidation cron's commit author is now `bot@imbue.com`.

### Fixed

- Fixed: Changelog consolidation cron commit author email corrected from `dev@imbue.com` to `bot@imbue.com` so GitHub attributes commits to the bot account whose token it uses.

# Group 7: data & durability

Concepts: versions, state, specs/blueprints, tests, changelogs, backups, snapshots, files/file sharing, memories, logs, events, upstream/parent.

Code roots used:
- Repo root: `/Users/gabeguralnick/.sculptor/workspaces/2c74b313b6044ad686f6aac67f005037/code` (hereafter `REPO`)
- Minds desktop: `REPO/apps/minds/imbue/minds/`
- FCT worktree: `REPO/.external_worktrees/forever-claude-template/` (hereafter `FCT`)
- mngr: `REPO/libs/mngr/imbue/mngr/`

---

## versions

### 1. CANONICAL DEFINITION

There is no single abstraction called "versions" in the codebase. The concept exists across two distinct mechanisms:

- **Workspace code versions**: the git history of the agent's workspace repository (FCT). The workspace IS a git repository; every committed change is a version.
- **Runtime state versions**: commits on the orphan branch `mindsbackup/$MNGR_AGENT_ID` produced by the `runtime_backup` service every 60 seconds.

The closest authoritative statement:

`FCT/CLAUDE.md` line 374-378:
> `runtime/` is gitignored from the main branch ... `runtime/` is backed up automatically by the `runtime-backup` service onto a separate orphan branch (`mindsbackup/$MNGR_AGENT_ID`)

`FCT/libs/runtime_backup/src/runtime_backup/runner.py` line 20-22:
```python
RUNTIME_DIR = Path("runtime")
TICK_INTERVAL_SECONDS = 60
LOG_FILE = Path("/tmp/runtime-backup.log")
```

### 2. ALL USAGES

**Workspace code versions** (implicit git):
- `FCT/.external_worktrees/` subtrees are additional git repos with independent histories
- Any `git log`, `git diff`, `git show` on the FCT repo root

**Runtime state versions** (runtime_backup):
- `FCT/libs/runtime_backup/src/runtime_backup/runner.py` line 127: commit message `"runtime backup: {_now_iso_utc()}"`
- `FCT/libs/runtime_backup/src/runtime_backup/runner.py` line 174: only pushes when `GH_TOKEN` is set
- Branch naming: `mindsbackup/$MNGR_AGENT_ID` (set up by bootstrap, not in runner.py directly)

**No explicit "version" type or API** was found anywhere in `REPO/libs/mngr/` or `REPO/apps/minds/` for workspace code.

### 3. COMPETING/MULTIPLE DEFINITIONS

Two orthogonal version histories for a workspace:
- **Main branch history**: code/skills/config changes (git commits on main or feature branches)
- **Orphan branch history**: fine-grained snapshots of `runtime/` state every 60s

### 4. TERMINOLOGY VARIANTS

- "backup" is used interchangeably with "version checkpoint" for runtime state in `FCT/libs/runtime_backup/README.md`
- No term "version" appears as a noun in any function or class name

### 5. AMBIGUITIES/INCONSISTENCIES

- There is no `mngr version` command or API endpoint for workspace code history
- The concept of "versions" as a user-facing feature is absent from CLI help text
- runtime_backup commits are labeled "runtime backup" not "runtime version", blurring the line between backup and versioning

### 6. DOC/CODE DIVERGENCES

- `FCT/libs/runtime_backup/README.md` says the orphan branch provides "fine-grained checkpoint" history, framing it as versioning, but the runner code never uses the word "version" — only "backup"

### 7. RECOMMENDED CANONICAL TERM + DEFINITION

**Recommended term**: Do not introduce a standalone "versions" concept. Instead:
- **Code history**: the git history of the workspace repository (existing git terminology)
- **Runtime checkpoints**: the time-stamped commits on orphan branch `mindsbackup/<agent_id>` (produced by runtime_backup)

What must change: if user-facing "workspace version" browsing is needed, surface it explicitly in `mngr` CLI with a dedicated `mngr history` or similar command. Currently no such surface exists.

---

## state

### 1. CANONICAL DEFINITION

"State" in FCT means persistent feature state stored under `runtime/<feature>/`. This is established as a monorepo convention:

`FCT/CLAUDE.md` line 16:
> State directories live under `runtime/<feature>/`.

The `runtime/` directory itself is gitignored from the main branch:

`FCT/CLAUDE.md` line 374:
> `runtime/` is gitignored from the main branch

### 2. ALL USAGES

**Known runtime/ subdirectories** (from CLAUDE.md, README.md, config.py):
- `runtime/memory/` — Claude auto-memory (`FCT/CLAUDE.md` line 362)
- `runtime/tickets/` — tk ticket system (`FCT/CLAUDE.md` line 15)
- `runtime/backup.toml` — host_backup config (`FCT/libs/host_backup/src/host_backup/config.py` line 30: `BACKUP_TOML_PATH = Path("runtime/backup.toml")`)
- `runtime/secrets/restic.env` — injected restic credentials (`FCT/libs/host_backup/src/host_backup/config.py` line 31: `RESTIC_ENV_PATH = Path("runtime/secrets/restic.env")`)
- `runtime/last-restic-prune` — prune timestamp (`FCT/libs/host_backup/src/host_backup/config.py` line 32: `PRUNE_TIMESTAMP_PATH = Path("runtime/last-restic-prune")`)
- `runtime/events/` or `events/` — event logs (see events section)

**Minds desktop**: uses `minds_api_key` stored in app state (`REPO/apps/minds/imbue/minds/desktop_client/webdav.py` line: `app.state.minds_api_key`), but this is Python ASGI app state, not the FCT runtime/ convention.

**mngr**: `REPO/libs/mngr/imbue/mngr/cli/snapshot.py` references provider-level "state" loosely, but has no `runtime/` directory.

### 3. COMPETING/MULTIPLE DEFINITIONS

- **FCT runtime/ state**: filesystem directories under `runtime/<feature>/`
- **ASGI app state**: Python `app.state.*` in Minds desktop server (`webdav.py`, FastAPI app object)
- **host_backup internal state**: `BackupState` object passed through tick loop (`FCT/libs/host_backup/src/host_backup/runner.py`)
- **BackupStatusState enum**: in `REPO/apps/minds/imbue/minds/desktop_client/backup_status.py`: `NOT_CONFIGURED`, `NEVER`, `BACKED_UP`, `BACKING_UP`, `UNKNOWN`

### 4. TERMINOLOGY VARIANTS

- "state" = `runtime/<feature>/` in FCT agent context
- "state" = Python object in Minds backend context
- "state" = backup status enum in desktop client context
- "runtime" is sometimes used as a synonym for "state" (e.g. `runtime_backup` backs up "state")

### 5. AMBIGUITIES/INCONSISTENCIES

- The `runtime/` directory serves double duty: both persistent agent state AND ephemeral secrets (e.g. `runtime/secrets/restic.env`). "Secrets" in `runtime/secrets/` are not "state" in the user-facing sense.
- `FCT/libs/host_backup/src/host_backup/runner.py` maintains an in-memory `BackupState` object alongside on-disk state files — two layers of "state" for the same service.
- The `events/` directory location is ambiguous: host_backup's `get_events_dir()` returns `Path(state_dir) / "events" / "backup"` where `state_dir` is computed from environment, but FCT CLAUDE.md doesn't mention `events/` as a state subdirectory.

### 6. DOC/CODE DIVERGENCES

- `FCT/CLAUDE.md` says "State directories live under `runtime/<feature>/`" but `runtime/last-restic-prune` (a file, not a directory) is used as a state marker. Minor inconsistency: not all state is a directory.
- `FCT/libs/host_backup/src/host_backup/config.py` line 222-227: `get_events_dir()` returns `Path(state_dir) / "events" / "backup"` — this is outside `runtime/` if `state_dir` is set to the repo root. The naming convention breaks down here.

### 7. RECOMMENDED CANONICAL TERM + DEFINITION

**Recommended term**: "runtime state" for the filesystem convention; "service state" for in-memory Python state.

- **Runtime state**: All persistent data under `runtime/<feature>/` in an FCT workspace. Gitignored from main branch. Backed up by runtime_backup (lightweight) and host_backup (comprehensive). Accessed only by in-container services.
- **Service state**: In-memory Python state objects (`app.state`, `BackupState`, etc.). Not persisted directly; services rebuild from runtime state on restart.

What must change: the `events/` directory should be clearly documented as part of the runtime state convention in FCT CLAUDE.md. The exception for files vs directories in `runtime/` should be explicitly noted (e.g. `runtime/last-restic-prune` is a file).

---

## specs / blueprints

### 1. CANONICAL DEFINITION

Two distinct artifact types exist:

**Specs** (`specs/` directory in FCT): design/architecture documents. No skill produces them; they appear to be human-authored.

**Blueprints** (`blueprint/` directory in FCT): implementation plans produced by the `blueprint-generate` skill, following a Q&A phase from the `blueprint` skill.

`FCT/.agents/skills/blueprint-generate/SKILL.md`:
> Ends Q&A, generates slug, creates `blueprint/<slug>/` dir, writes plan

`FCT/.agents/skills/blueprint/SKILL.md`:
> Q&A-driven plan-writing session (does NOT write the plan)... Explores codebase, asks 3-5 clarifying questions per round

### 2. ALL USAGES

**specs/** in FCT (6 subdirectories observed):
- `chat-agent-activity-indicator/`
- `crystallize-simplicity-bias/`
- `do-something-new-skill/`
- `skill-lifecycle-shared-refs/`
- `system_interface/`
- `worker-gates-via-main/`

**blueprint/** in FCT (11 subdirectories observed):
- `agent-layout-ops/`, `agent-open-tab/`, `build-web-service-restructure/`, `chat-progress-view/`, `claude-login-modal/`, `end-of-turn-progress-rendering/`, `migrate-workspace-server/`, `robust-subagent-linkage/`, `scaling-design/`, `tk-step-ticket-model/`, `transcript-driven-progress-view/`

**Skills** (in FCT):
- `FCT/.agents/skills/blueprint/SKILL.md` — initiates Q&A
- `FCT/.agents/skills/blueprint-generate/SKILL.md` — writes plan to `blueprint/<slug>/`

**Mngr/minds**: No `specs/` or `blueprint/` directories exist in `REPO/libs/mngr/` or `REPO/apps/minds/`. These are FCT-specific conventions.

### 3. COMPETING/MULTIPLE DEFINITIONS

- "spec" and "blueprint" are sometimes used interchangeably in developer parlance, but the codebase makes them distinct directories
- "plan" appears in blueprint-generate SKILL.md as the output artifact, adding a third term for the same concept
- In `REPO` (the mngr/minds monorepo), there is a `CLAUDE.md` line: "When adding tests, consider whether it should be a unit test..." — no reference to specs or blueprints at all

### 4. TERMINOLOGY VARIANTS

- "spec" = design/architecture doc in `specs/`
- "blueprint" = implementation plan in `blueprint/`
- "plan" = synonym for blueprint (used in blueprint-generate SKILL.md)
- "design doc" = informal synonym for spec

### 5. AMBIGUITIES/INCONSISTENCIES

- The distinction between `specs/` and `blueprint/` is not documented anywhere in FCT CLAUDE.md or a top-level README. Someone reading the repo structure cannot determine the difference without reading both skill files.
- `blueprint-generate` creates plans in `blueprint/<slug>/` but the skill is named `blueprint-generate` not `plan-generate`, implying the output should be called a "blueprint" — yet `SKILL.md` says "writes plan"
- The `blueprint` skill (Q&A) and `blueprint-generate` skill (plan writing) are two separate skills that must be used in sequence, but there is no guard preventing direct use of `blueprint-generate` without the Q&A phase

### 6. DOC/CODE DIVERGENCES

- `FCT/.agents/skills/blueprint/SKILL.md` says the blueprint skill "does NOT write the plan" — this is consistent with `blueprint-generate`. No divergence.
- `FCT/blueprint/` contains 11 items vs `FCT/specs/` with 6; blueprints are more numerous, suggesting active use, while specs may be aspirational/legacy.

### 7. RECOMMENDED CANONICAL TERM + DEFINITION

**Recommended terms**:
- **Spec** (`specs/<name>/`): a freeform design or architecture document, human-authored, describing the intended behavior of a feature. No standard template or tooling.
- **Blueprint** (`blueprint/<slug>/`): a structured implementation plan produced by the `blueprint` + `blueprint-generate` skill pair, following a codebase-grounded Q&A process. Output: a plan document in `blueprint/<slug>/`.

What must change: FCT CLAUDE.md should document the distinction explicitly. The `blueprint-generate` SKILL.md should consistently use "blueprint" (not "plan") to describe its output, or the directory should be renamed `plans/`.

---

## tests

### 1. CANONICAL DEFINITION

Test categories are defined in `REPO/style_guide.md` lines 1729-1909:

- **Unit tests** (`*_test.py`): fast, isolated, no I/O
- **Integration tests** (`test_*.py`, no mark): end-to-end within the monorepo, no network
- **Acceptance tests** (`test_*.py` with `@pytest.mark.acceptance`): real dependencies, run in CI on all branches
- **Release tests** (`test_*.py` with `@pytest.mark.release`): comprehensive, run only on `v*` tags
- **Ratchet tests** (`test_ratchets.py`, `test_project_ratchets.py`, `test_meta_ratchets.py`): code quality checks, inline-snapshot counts

Additional test types not in the taxonomy above:
- **Deployment tests** (`REPO/apps/minds/imbue/minds/deployment_tests/`): live-environment tests
- **E2E workspace runner** (`REPO/apps/minds/imbue/minds/desktop_client/e2e_workspace_runner.py`): reusable driver for Electron + Docker e2e tests

### 2. ALL USAGES

**Unit tests**:
- `REPO/libs/mngr/imbue/mngr/**/*_test.py` — numerous files following `*_test.py` pattern
- `REPO/apps/minds/imbue/minds/**/*_test.py`

**Integration tests**:
- `REPO/libs/mngr/imbue/mngr/**/test_*.py` without marks
- `REPO/apps/minds/imbue/minds/**/test_*.py` without marks

**Acceptance tests**:
- `@pytest.mark.acceptance` in `test_*.py` files

**Release tests**:
- `@pytest.mark.release` in `test_*.py` files

**Ratchet tests**:
- `REPO/libs/mngr/imbue/mngr/test_ratchets.py`
- `REPO/libs/mngr/imbue/mngr/test_project_ratchets.py`
- `REPO/libs/mngr/imbue/mngr/test_meta_ratchets.py`
- Counts stored as inline-snapshot values; updated with `--inline-snapshot=trim`

**Deployment tests** (`REPO/apps/minds/imbue/minds/deployment_tests/`):
- `primitives.py` defines `RunId`, `SharedEnvRole`, `MailtmAddress`, `MailtmJwt`, `SignupEmailAddress`, `VerificationToken`, `OneTimeLoginCode`
- Uses `DEPLOYMENT_ENVS_JSON_ENV_VAR = "MINDS_DEPLOYMENT_TEST_ENVS_JSON"` (`primitives.py`)
- Tests real Minds cloud infrastructure

**E2E workspace runner** (`REPO/apps/minds/imbue/minds/desktop_client/e2e_workspace_runner.py`):
- Docstring: "Reusable end-to-end driver for Electron app creates a Docker workspace"
- Used by `test_desktop_client_e2e.py` and `scripts/snapshot_minds_e2e_state.py`
- Not a test file itself; a reusable helper

**Test infrastructure**:
- `just test-offload` — fans tests across Modal sandboxes
- `just test-quick <path>` — fast local single-test iteration
- `PYTEST_MAX_DURATION_SECONDS` — global lock file deadline for kill-safe runs

### 3. COMPETING/MULTIPLE DEFINITIONS

- "integration test" in `style_guide.md`: no network, within monorepo — but "deployment test" is essentially a network-dependent integration test run against real infrastructure. These are in separate directories, not marked as `@pytest.mark.acceptance` or `@pytest.mark.release`.
- "e2e test" is used informally for the Electron+Docker test (via `e2e_workspace_runner.py`) but does not correspond to any official test category in `style_guide.md`.
- Ratchet tests are distinct from correctness tests; they enforce quantitative quality bounds.

### 4. TERMINOLOGY VARIANTS

- "unit test" = `*_test.py`
- "integration test" = `test_*.py` no mark
- "acceptance test" = `@pytest.mark.acceptance`
- "release test" = `@pytest.mark.release`
- "ratchet" / "ratchet test" = `test_ratchets.py` family
- "deployment test" = `deployment_tests/` directory (Minds-specific)
- "e2e test" = informal, Electron + Docker via `e2e_workspace_runner.py`

### 5. AMBIGUITIES/INCONSISTENCIES

- Deployment tests (`deployment_tests/`) have no official category in `style_guide.md`. They are run separately and are not tagged with standard marks.
- The `e2e_workspace_runner.py` produces tests that live in `test_desktop_client_e2e.py`, which presumably carries a standard mark, but the runner itself is not a test.
- The ratchet test files (`test_ratchets.py`, `test_project_ratchets.py`, `test_meta_ratchets.py`) all begin with `test_` (not `*_test.py`), yet they behave more like unit tests (fast, isolated). They do not carry `@pytest.mark.acceptance` or `@pytest.mark.release`. This is a unique category that straddles the naming convention.

### 6. DOC/CODE DIVERGENCES

- `style_guide.md` (lines 1729-1909) defines the test taxonomy but does not mention "deployment tests" as a category. The `deployment_tests/` directory exists in `apps/minds/` and represents a real test category that is undocumented in the style guide.
- `style_guide.md` does not define "e2e test" as a category, but `e2e_workspace_runner.py`'s docstring uses the term "end-to-end driver."

### 7. RECOMMENDED CANONICAL TERM + DEFINITION

Keep the existing 4-category taxonomy but extend it officially:
- **Deployment tests**: tests in `<project>/deployment_tests/` that run against live infrastructure; require network and credentials; run on deployments only. Add to `style_guide.md`.
- **E2E tests**: tests that use `e2e_workspace_runner.py` or similar full-stack drivers; classify under acceptance or a new `@pytest.mark.e2e` mark.

What must change: `style_guide.md` should document deployment tests as an official fifth category and clarify where e2e tests fit.

---

## changelogs

### 1. CANONICAL DEFINITION

Per-PR changelog entry files at `<project_dir>/changelog/<branch-name>.md`, consolidated nightly into `CHANGELOG.md` and `UNABRIDGED_CHANGELOG.md`.

From `REPO/CLAUDE.md` (project instructions):
> Each project holds its own changelog artifacts inside its own directory: `<project_dir>/changelog/` (per-PR entries), `<project_dir>/CHANGELOG.md` (concise summary), `<project_dir>/UNABRIDGED_CHANGELOG.md` (verbatim).

### 2. ALL USAGES

**Per-PR entries** (observed):
- `REPO/apps/minds/changelog/mngr-gcp.md` — example entry (branch-named, slashes replaced by dashes)
- `REPO/libs/mngr/changelog/` — directory exists with branch-named files (e.g. `mngr-gcp.md`, `mngr-usage-filter-by-age.md`)

**Consolidated changelogs**:
- `REPO/apps/minds/CHANGELOG.md` — concise AI-generated summary
- `REPO/apps/minds/UNABRIDGED_CHANGELOG.md` — verbatim entries
- `REPO/libs/mngr/CHANGELOG.md` and `REPO/libs/mngr/UNABRIDGED_CHANGELOG.md`

**Projects covered**:
- `libs/` subdirectories (e.g. `libs/mngr/`, `libs/imbue_common/`)
- `apps/` subdirectories (e.g. `apps/minds/`)
- Synthetic `dev/` for root-level changes (`scripts/`, `.github/`, `justfile`)

**CI enforcement**: CLAUDE.md states "CI will fail if any are missing" — enforced by `test_pr_has_changelog_entry()` in `REPO/test_meta_ratchets.py`, which requires `<project_dir>/changelog/<branch-name>.md` for every project the branch touches (slashes replaced by dashes), skipping branches whose prefix is in `_CHANGELOG_EXEMPT_BRANCH_PREFIXES = ("mngr/changelog-consolidation",)`. A companion `test_every_project_has_changelog_layout()` requires each project to ship `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md`, and a `changelog/.gitkeep`. Project ownership of paths is resolved via `scripts/changelog_projects.py`.

**FCT changelogs**: FCT has its own `changelog/` directory (separate from the monorepo) following the same convention, since FCT is a separate git repo.

### 3. COMPETING/MULTIPLE DEFINITIONS

- `CHANGELOG.md` = concise summary (AI-generated)
- `UNABRIDGED_CHANGELOG.md` = full verbatim entries (fan-in of per-PR files)
- Per-PR entry = `changelog/<branch-name>.md` (input)
- "changelog consolidation" = the nightly agent process that fans per-PR entries into the two summary files

### 4. TERMINOLOGY VARIANTS

- "changelog entry" = per-PR `changelog/<branch-name>.md` file
- "consolidated changelog" = `CHANGELOG.md`
- "unabridged changelog" = `UNABRIDGED_CHANGELOG.md`
- "nightly consolidation" = the agent run that fans entries

### 5. AMBIGUITIES/INCONSISTENCIES

- The branch name becomes the filename, with slashes replaced by dashes. The CLAUDE.md rule is `<project_dir>/changelog/<branch-name>.md` where slashes → dashes. This means `gabriel/taxonomizing` becomes `gabriel-taxonomizing.md`. But if two branches produce the same slug after dash-replacement (edge case), files would collide.
- CLAUDE.md says "changelog consolidation agent's own PRs (`mngr/changelog-consolidation-*`) are exempt from this requirement" — so there is an exemption for the agent that PRODUCES changelogs.

### 6. DOC/CODE DIVERGENCES

- The changelog requirement IS enforced programmatically: `test_pr_has_changelog_entry()` in `REPO/test_meta_ratchets.py` fails CI when a touched project lacks its per-PR entry. The consolidation tooling (`scripts/changelog_consolidate.py`, `scripts/changelog_projects.py`, with `*_test.py` coverage) fans entries into the summary files. No divergence on enforcement.
- The double-newline bullet format ("If the entry uses a list, separate the bullets with a double newline") is a CLAUDE.md convention; no code enforces this format.

### 7. RECOMMENDED CANONICAL TERM + DEFINITION

The current system is well-defined. No renaming needed.

**Canonical terms**:
- **Changelog entry**: a `changelog/<branch-name>.md` file per project per PR
- **Changelog**: `CHANGELOG.md` (concise, auto-summarized)
- **Unabridged changelog**: `UNABRIDGED_CHANGELOG.md` (verbatim fan-in)

What must change: nothing structural. Enforcement is now an explicit meta-ratchet (`test_pr_has_changelog_entry()` in `REPO/test_meta_ratchets.py`), so it is no longer opaque to contributors.

---

## backups

### 1. CANONICAL DEFINITION

Two distinct backup systems exist:

**host_backup** (FCT `libs/host_backup/`): encrypted, deduplicated restic backup of the entire `/mngr/` (host_dir) to remote storage (R2/S3). Run hourly inside the container.

`FCT/libs/host_backup/src/host_backup/runner.py` line 34:
```python
LOG_FILE = Path("/tmp/host-backup.log")
```
Default tick interval: `backup_interval_seconds` from `BackupConfig`, default 3600s.

`FCT/libs/host_backup/README.md`:
> host_backup covers the whole host_dir (code, worktrees, agent state, chat sessions, logs)

**runtime_backup** (FCT `libs/runtime_backup/`): git commit of `runtime/` directory to orphan branch `mindsbackup/$MNGR_AGENT_ID` every 60 seconds.

`FCT/libs/runtime_backup/src/runtime_backup/runner.py` line 20-22:
```python
RUNTIME_DIR = Path("runtime")
TICK_INTERVAL_SECONDS = 60
LOG_FILE = Path("/tmp/runtime-backup.log")
```

`FCT/libs/runtime_backup/README.md`:
> runtime_backup only ships runtime/ to a GitHub orphan branch as a fine-grained checkpoint

**Minds desktop backup management**: `REPO/apps/minds/imbue/minds/desktop_client/` contains the client-side orchestration for host_backup configuration:
- `backup_provisioning.py`: provisions restic repo, generates passwords, injects `restic.env` into workspace
- `backup_env_store.py`: stores canonical `restic.env` at `backup_envs/<agent_id>.env` in minds data dir (mode 0600)
- `backup_password_store.py`: stores master password at `<data_dir>/backup_password` (mode 0600)
- `backup_status.py`: queries backup status (enum: `NOT_CONFIGURED`, `NEVER`, `BACKED_UP`, `BACKING_UP`, `UNKNOWN`)
- `backup_export.py`: exports latest restic snapshot as zip
- `restic_cli.py`: runs local restic binary from minds (desktop) app

### 2. ALL USAGES

**host_backup** (inside container):
- `FCT/libs/host_backup/src/host_backup/runner.py` — tick loop
- `FCT/libs/host_backup/src/host_backup/config.py` — `BACKUP_TOML_PATH = Path("runtime/backup.toml")`, `RESTIC_ENV_PATH = Path("runtime/secrets/restic.env")`
- `FCT/libs/host_backup/src/host_backup/events.py` — 13 event types, emits to `events_dir / "events.jsonl"`
- `FCT/libs/host_backup/src/host_backup/snapshot.py` — `SnapshotTakerInterface`, three methods: `BTRFS_LOCAL`, `OUTER_TRIGGER`, `DIRECT`

**runtime_backup** (inside container):
- `FCT/libs/runtime_backup/src/runtime_backup/runner.py` — commits `runtime/` to orphan branch

**Minds desktop** (outside container):
- `REPO/apps/minds/imbue/minds/desktop_client/backup_env_store.py` — canonical `restic.env` at `backup_envs/<agent_id>.env`; "never auto-deleted -- not even on workspace destroy"
- `REPO/apps/minds/imbue/minds/desktop_client/backup_provisioning.py` — `_RESTIC_ENV_REMOTE_PATH = "runtime/secrets/restic.env"` (line 57); providers (`BackupProvider`): `IMBUE_CLOUD`, `API_KEY`, `CONFIGURE_LATER`
- `REPO/apps/minds/imbue/minds/desktop_client/backup_password_store.py` — master password never enters workspace; `save_backup_password_if_absent` with `O_EXCL` write-once semantics; one password shared across all workspaces
- `REPO/apps/minds/imbue/minds/desktop_client/backup_status.py` — `compute_backup_status_for_workspace()` reads canonical env, calls `restic_cli.is_backup_in_progress()` and `get_latest_snapshot_time()`
- `REPO/apps/minds/imbue/minds/desktop_client/backup_export.py` — uses `restic restore` (parallel) not `restic dump` (sequential); exports to `/tmp/minds-backup-export-<host_id>.zip`
- `REPO/apps/minds/imbue/minds/desktop_client/restic_cli.py` — runs local restic; constants: `_RESTORE_TIMEOUT_SECONDS = 600.0`, `_LOCK_STALE_SECONDS = 30 * 60.0`, `_AUTH_PROPAGATION_RETRY_SECONDS = 60.0`

### 3. COMPETING/MULTIPLE DEFINITIONS

Two backup systems with completely different mechanisms, scopes, and storage targets:

| Aspect | host_backup | runtime_backup |
|--------|-------------|----------------|
| Scope | Entire host_dir (`/mngr/`) | `runtime/` only |
| Mechanism | restic (encrypted, deduplicated) | git commit + push |
| Storage | Remote R2/S3 bucket | GitHub orphan branch |
| Interval | 3600s (configurable) | 60s (fixed) |
| Credentials | `runtime/secrets/restic.env` | `GH_TOKEN` env var |
| Log | `/tmp/host-backup.log` | `/tmp/runtime-backup.log` |

### 4. TERMINOLOGY VARIANTS

- "backup" alone is ambiguous — could mean either system
- "restic backup" = specifically the host_backup restic operation
- "runtime backup" = specifically runtime_backup git commits
- "snapshot" is used within host_backup to mean a consistent filesystem view BEFORE restic runs (see snapshot section), adding confusion

### 5. AMBIGUITIES/INCONSISTENCIES

- The word "backup" in `runtime_backup` refers to git commits, not restic snapshots. This contradicts common usage where "backup" implies encryption and integrity guarantees.
- `backup_env_store.py` docstring: "The copy inside the workspace at `runtime/secrets/restic.env` is just an injected mirror of this file." The word "mirror" is clear but the mirroring direction (minds → workspace) is not enforced by code — it could drift if the workspace modifies its copy.
- `backup_password_store.py`: master password is shared across all workspaces. This is a key design decision with security implications not documented anywhere visible to users.
- host_backup and runtime_backup are separate services; there is no cross-referencing between them (no shared config, no shared event log). An observer of one cannot infer the status of the other.

### 6. DOC/CODE DIVERGENCES

- `FCT/libs/host_backup/README.md` lists `SnapshotMethod.BTRFS_LOCAL` for lima (macOS), `OUTER_TRIGGER` for vps-docker, `DIRECT` for plain docker. The code (`FCT/libs/host_backup/src/host_backup/config.py` `SnapshotMethod` enum) matches this.
- `FCT/libs/runtime_backup/README.md` says runtime_backup "only ships runtime/." Code confirms: `RUNTIME_DIR = Path("runtime")` — no divergence.
- Minds desktop `backup_export.py` uses `restic restore` not `restic dump`. Comment in the file explains this is intentional for parallel downloads. No doc-code divergence, but there's no user-facing documentation of this choice.

### 7. RECOMMENDED CANONICAL TERM + DEFINITION

Rename to eliminate ambiguity:
- **Workspace backup** (current: host_backup): encrypted restic backup of the full workspace filesystem to remote object storage. Managed by Minds desktop; executed inside container.
- **Runtime checkpoint** (current: runtime_backup): git commit of `runtime/` to GitHub orphan branch. Provides fine-grained checkpointing of agent state, not disaster recovery.

What must change: Update FCT CLAUDE.md and READMEs to consistently distinguish "workspace backup" (full, encrypted, remote) from "runtime checkpoint" (partial, git, GitHub). Rename the `runtime_backup` library to `runtime_checkpoint` or `runtime_git_backup` to make the distinction clear at a glance.

---

## snapshots

### 1. CANONICAL DEFINITION

The term "snapshot" is overloaded across two completely unrelated systems:

**System A: restic snapshots** (within host_backup): deduplicated, point-in-time backup artifacts stored in a restic repository. Created by `host_backup` service per tick. Queried by Minds desktop via `restic_cli.py`.

`REPO/apps/minds/imbue/minds/desktop_client/restic_cli.py`:
```python
restic snapshots --latest 1 --json
```

**System B: mngr snapshots** (provider-level): VM/disk snapshots created by `mngr snapshot create`. Provider-level filesystem capture. Completely separate from restic.

`REPO/libs/mngr/imbue/mngr/cli/snapshot.py` help text:
> Snapshots capture the complete filesystem state of a host

**System C: pre-restic filesystem snapshots** (within host_backup): a consistent local filesystem view taken BEFORE restic reads, to ensure consistency during backup. Created by `SnapshotTakerInterface` implementations.

`FCT/libs/host_backup/src/host_backup/snapshot.py` line 34-48:
```python
class SnapshotResult(FrozenModel):
    """Outcome of a successful `take_snapshot` call."""
    method: SnapshotMethod  # BTRFS_LOCAL, OUTER_TRIGGER, DIRECT
    snapshot_path: str
    read_path: Path
    duration_seconds: float
```

### 2. ALL USAGES

**Restic snapshots** (System A):
- `REPO/apps/minds/imbue/minds/desktop_client/restic_cli.py`: `get_latest_snapshot_time()`, `is_backup_in_progress()`, `restore_snapshot()`
- `REPO/apps/minds/imbue/minds/desktop_client/backup_status.py`: `compute_backup_status_for_workspace()` calls `get_latest_snapshot_time()`
- `REPO/apps/minds/imbue/minds/desktop_client/backup_export.py`: `export_latest_snapshot_zip()` — restores latest restic snapshot

**mngr snapshots** (System B):
- `REPO/libs/mngr/imbue/mngr/cli/snapshot.py`: `mngr snapshot create/list/destroy`
- `SnapshotInfo` (from provider interface): `id`, `name`, `created_at`, `size_bytes`
- `SnapshotsNotSupportedError` — raised for unsupported providers
- Alias: `snap`

**host_backup pre-backup filesystem snapshots** (System C):
- `FCT/libs/host_backup/src/host_backup/snapshot.py`: `SnapshotTakerInterface`, `SnapshotResult`, `SnapshotError`
- `FCT/libs/host_backup/src/host_backup/config.py`: `SnapshotMethod` enum (`BTRFS_LOCAL`, `OUTER_TRIGGER`, `DIRECT`)
- `FCT/libs/host_backup/src/host_backup/events.py`: `BackupEventType.SNAPSHOT_CREATED`, `BackupEventType.SNAPSHOT_DELETED`

### 3. COMPETING/MULTIPLE DEFINITIONS

Three usages of "snapshot":
1. Restic snapshot = a backup artifact in a restic repository (content-addressed, deduplicated)
2. mngr/provider snapshot = a provider-level VM or disk capture (e.g. DigitalOcean droplet snapshot)
3. host_backup internal snapshot = a temporary filesystem view (btrfs subvolume, bind mount, or direct path) taken to feed restic

All three are in active use. None is deprecated.

### 4. TERMINOLOGY VARIANTS

- "restic snapshot" — used in `restic_cli.py` comments
- "host snapshot" — used in host_backup README
- "provider snapshot" / "VM snapshot" — used informally for mngr snapshots
- `SnapshotResult` — host_backup internal snapshot result type
- `SnapshotInfo` — mngr provider snapshot info type
- `snapshot_path` vs `snapshot_id` — different fields in different systems

### 5. AMBIGUITIES/INCONSISTENCIES

- `FCT/libs/host_backup/src/host_backup/events.py` emits `SNAPSHOT_CREATED` and `SNAPSHOT_DELETED` events referring to the filesystem snapshot (System C), not the restic snapshot (System A). But the event names could easily be confused with restic snapshot lifecycle events.
- `REPO/libs/mngr/imbue/mngr/cli/snapshot.py` help text ("Snapshots capture the complete filesystem state of a host") describes mngr snapshots in terms almost identical to restic snapshots.
- `backup_export.py` calls `restore_snapshot()` in `restic_cli.py` — "restore from snapshot" — adding "restore" as a term tied to restic snapshots specifically.

### 6. DOC/CODE DIVERGENCES

- `FCT/libs/host_backup/README.md` lists snapshot methods (btrfs_local, outer_trigger, direct) and their platforms — matches code exactly.
- No divergence found, but the three-way overload of "snapshot" is not acknowledged in any doc.

### 7. RECOMMENDED CANONICAL TERM + DEFINITION

Disambiguate at the terminology level:
- **Backup artifact** (currently: restic snapshot): a deduplicated point-in-time backup stored in a restic repository. Use "backup artifact" or "restic backup" in user-facing text.
- **Host snapshot** (currently: mngr snapshot): a provider-level VM or disk snapshot created by `mngr snapshot`. Keep "snapshot" here as it is standard cloud terminology.
- **Consistency capture** (currently: host_backup internal snapshot): a temporary local filesystem view taken to ensure restic reads a consistent state. Rename internally: `ConsistencyCapture`, `CaptureResult`, `CaptureTakerInterface`.

What must change: Rename `SnapshotResult`, `SnapshotTakerInterface`, `SnapshotError`, `SnapshotMethod` in `FCT/libs/host_backup/src/host_backup/snapshot.py` to remove "Snapshot" and use "Capture" or similar. Update `SNAPSHOT_CREATED` / `SNAPSHOT_DELETED` event types in `host_backup/events.py` to `CAPTURE_CREATED` / `CAPTURE_DELETED`. This would eliminate the three-way collision.

---

## files / file sharing

### 1. CANONICAL DEFINITION

File sharing is implemented via WsgiDAV, mounted as an ASGI app at `/api/v1/files` in the Minds backend. It serves two root paths:

`REPO/apps/minds/imbue/minds/desktop_client/webdav.py` line 142-153:
```python
def get_file_sharing_roots() -> tuple[Path, ...]:
    return (Path.home(), Path(tempfile.gettempdir()))
```

This is the single source of truth for shareable paths: the WebDAV mount is built from it, and the latchkey file-sharing permission handler (`REPO/apps/minds/imbue/minds/desktop_client/latchkey/handlers/file_sharing.py`) validates a requested or user-edited path against these roots before it reaches the gateway.

URL mapping: filesystem path mirrors URL path (e.g. `/home/<user>/foo.txt` → `/api/v1/files/home/<user>/foo.txt`).

Auth: Bearer token from `app.state.minds_api_key`; WsgiDAV itself is anonymous (the auth happens at the ASGI layer).

FCT also has a `file-sharing` skill:
- `FCT/.agents/skills/file-sharing/SKILL.md` — agent skill for interacting with the file-sharing endpoint

### 2. ALL USAGES

**Backend** (`REPO/apps/minds/imbue/minds/desktop_client/webdav.py`):
- `get_file_sharing_roots()` — returns `(Path.home(), Path(tempfile.gettempdir()))`
- WsgiDAV mounted at `/api/v1/files`
- Auth middleware checks `Authorization: Bearer <api_key>` header

**FCT skill** (`FCT/.agents/skills/file-sharing/SKILL.md`):
- Agent-facing skill for listing/reading/writing files via the WebDAV endpoint
- Exact implementation not fully read, but skill exists

**Backend resolver** (`REPO/apps/minds/imbue/minds/desktop_client/backend_resolver.py`):
- Presumably resolves the backend URL for the file-sharing API — not read in full, but referenced

### 3. COMPETING/MULTIPLE DEFINITIONS

- "file sharing" = the WsgiDAV endpoint at `/api/v1/files` (transport: WebDAV over HTTP)
- No competing definition found; this is the only file-sharing mechanism
- The FCT `file-sharing` skill is an agent-facing wrapper, not a competing implementation

### 4. TERMINOLOGY VARIANTS

- "file sharing" — SKILL.md name and feature name
- "WebDAV" — the protocol used
- "WsgiDAV" — the Python library used
- "file API" — informal term for the HTTP endpoint
- `/api/v1/files` — the URL path

### 5. AMBIGUITIES/INCONSISTENCIES

- `get_file_sharing_roots()` serves `Path.home()` and `/tmp`. On the host machine (macOS) `Path.home()` is the user's home directory, but inside a Docker container it would be the container's home (e.g. `/root`). The served paths depend heavily on where Minds server is running.
- The URL mirrors the filesystem path — so if the home dir is `/Users/alice`, the URL would be `/api/v1/files/Users/alice/file.txt`. This is an unusual URL design that couples the API URL to the OS filesystem layout.
- Auth is checked at the ASGI layer with a Bearer token, but WsgiDAV itself is anonymous. If the ASGI auth middleware were bypassed, WsgiDAV would serve everything without auth.

### 6. DOC/CODE DIVERGENCES

- The `file-sharing` SKILL.md was not fully read, so potential divergence between skill documentation and actual API behavior cannot be fully assessed.
- No divergence found between `webdav.py` code and comments within that file.

### 7. RECOMMENDED CANONICAL TERM + DEFINITION

**Recommended term**: "file sharing" is clear and used consistently. No renaming needed.

**Canonical definition**: The WebDAV-based file sharing service, mounted at `/api/v1/files`, that exposes the agent's home directory and `/tmp` via an HTTP Bearer-token-authenticated endpoint. Implemented in `REPO/apps/minds/imbue/minds/desktop_client/webdav.py`. Accessible to agents via the `file-sharing` skill.

What must change: Document the URL-to-filesystem path mapping explicitly in the skill README. Add a note that served roots are container-relative paths.

---

## memories

### 1. CANONICAL DEFINITION

Claude's auto-memory lives at `runtime/memory/` in FCT. This is configured via `autoMemoryDirectory` in `.claude/settings.json`.

`FCT/CLAUDE.md` line 362:
> Use Claude's built-in memory system. Your memory directory is `runtime/memory/` (configured via `autoMemoryDirectory` in `.claude/settings.json`).

`FCT/CLAUDE.md` line 374:
> `runtime/` is gitignored from the main branch (it includes `runtime/memory/` for Claude memory and other transient state).

`FCT/libs/runtime_backup/README.md`:
> runtime/ (which holds Claude memory, ticket state, transcripts, telegram history, app port registry, etc.)

### 2. ALL USAGES

**Memory storage** (FCT):
- `FCT/runtime/memory/` — the actual directory (gitignored, backed up on orphan branch)
- `FCT/.claude/settings.json` — `autoMemoryDirectory` points to `runtime/memory/`

**Memory backup**:
- runtime_backup service commits `runtime/` (including `runtime/memory/`) every 60s to orphan branch `mindsbackup/$MNGR_AGENT_ID`
- host_backup service includes `runtime/memory/` in its full workspace backup (since `runtime/` is inside host_dir)

**Memory format**:
- Claude's internal format for `autoMemoryDirectory` — files written and read by Claude Code's memory system. Content and file naming conventions are opaque to the application code (handled by Claude Code internals).

**MEMORY.md** (user's auto-memory, referenced in system-reminder):
- `REPO/.claude/projects/.../memory/MEMORY.md` — the user's global memory file, distinct from workspace memory

### 3. COMPETING/MULTIPLE DEFINITIONS

- **Workspace memory** (`FCT/runtime/memory/`): per-agent, per-workspace memory managed by Claude Code's `autoMemoryDirectory` feature
- **User global memory** (`~/.claude/projects/.../memory/MEMORY.md`): cross-session user memory, stored in the Claude Code user data directory — separate system, different location
- **"memories"** in colloquial use: sometimes refers to either system

### 4. TERMINOLOGY VARIANTS

- "memory" — FCT CLAUDE.md term
- "auto-memory" — references `autoMemoryDirectory` config key
- "MEMORY.md" — the user global memory file format
- "runtime/memory/" — the specific path in FCT

### 5. AMBIGUITIES/INCONSISTENCIES

- The `autoMemoryDirectory` feature is a Claude Code internals feature; its file naming conventions, format, and behavior are not documented in FCT CLAUDE.md or any README. Agents must rely on Claude Code's internal documentation.
- Both runtime_backup (60s git commits) and host_backup (hourly restic) back up `runtime/memory/`. This creates two independent backup timelines for memory, with different granularity and retention policies. There's no explicit policy on which to prefer for memory recovery.
- User global memory (`MEMORY.md`) is outside FCT's `runtime/` and is NOT backed up by either FCT backup service. Only minds desktop (if configured) would back it up as part of the host filesystem.

### 6. DOC/CODE DIVERGENCES

- `FCT/CLAUDE.md` says memory directory is `runtime/memory/` but does not specify the file format or naming convention used by `autoMemoryDirectory`. This is not a divergence (no competing claim) but is an undocumented dependency on Claude Code internals.
- `FCT/libs/runtime_backup/README.md` lists "Claude memory" as one of the items in `runtime/` that gets backed up — accurate and consistent with CLAUDE.md.

### 7. RECOMMENDED CANONICAL TERM + DEFINITION

**Recommended term**: "agent memory" for `runtime/memory/` (workspace-scoped, per-agent). Distinguish from "user memory" for `~/.claude/projects/.../memory/MEMORY.md`.

What must change: FCT CLAUDE.md should note that `runtime/memory/` content format is controlled by Claude Code internals and describe recovery procedure (restore from most recent runtime_backup commit or host_backup snapshot). The distinction between workspace memory and user global memory should be made explicit.

---

## logs

### 1. CANONICAL DEFINITION

"Logs" in Minds/FCT exist in multiple forms with no single canonical location:

**Service logs** (unstructured text): written to `/tmp/<service>.log` by long-running services:
- `FCT/libs/host_backup/src/host_backup/runner.py` line 34: `LOG_FILE = Path("/tmp/host-backup.log")`
- `FCT/libs/runtime_backup/src/runtime_backup/runner.py` line 22: `LOG_FILE = Path("/tmp/runtime-backup.log")`
- Both use loguru for structured log output to these files

**Structured event logs** (append-only JSONL): at `events/<source>/events.jsonl` — see events section. These are logs of significant events, not general service logs.

**Tmux pane output**: agent and service terminal output captured in tmux windows. Not persisted to files by default; volatile.

**Deployment test logs**: in `apps/minds/imbue/minds/deployment_tests/`, separate from service logs.

### 2. ALL USAGES

**Service logs**:
- `/tmp/host-backup.log` — host_backup service (`loguru`)
- `/tmp/runtime-backup.log` — runtime_backup service (`loguru`)
- Tmux window naming convention: `svc-*` for service windows (referenced in FCT CLAUDE.md)

**Structured event logs** (see events section):
- `events/<source>/events.jsonl` — append-only structured records
- Read via `mngr event` CLI

**Agent transcript logs**:
- `FCT/libs/runtime_backup/README.md`: "runtime/ (which holds Claude memory, ticket state, transcripts...)" — transcripts live in `runtime/`, backed up by runtime_backup

**Minds desktop logs**:
- Electron app logs: platform-specific (macOS: `~/Library/Logs/Minds/`, per Electron conventions). Not found in codebase directly, but standard Electron behavior.

### 3. COMPETING/MULTIPLE DEFINITIONS

Four distinct log types:
1. Service logs (`/tmp/*.log`) — loguru text output, ephemeral
2. Structured event logs (`events/<source>/events.jsonl`) — append-only JSONL, persistent
3. Tmux terminal output — volatile, session-scoped
4. Agent transcripts (in `runtime/`) — Claude Code conversation records, backed up

### 4. TERMINOLOGY VARIANTS

- "logs" — general term, could mean any of the four types
- "service logs" — specifically `/tmp/*.log`
- "event log" — specifically `events/<source>/events.jsonl`
- "transcript" — Claude Code conversation log in `runtime/`
- "terminal output" — tmux pane content

### 5. AMBIGUITIES/INCONSISTENCIES

- Service logs (`/tmp/*.log`) are ephemeral: lost on container restart. They are inside host_dir, so they ARE included in host_backup. But `/tmp/` is typically excluded from backups in most restic configs. Whether `/tmp/host-backup.log` survives a container restart depends on restic `excludes` config — unclear.
- The term "log" is used for both structured (`events.jsonl`) and unstructured (`*.log`) output. `mngr event` reads structured logs; there is no equivalent CLI for unstructured service logs.
- Agent transcripts in `runtime/` are backed up but are not searchable/queryable via any CLI. They are opaque blobs.

### 6. DOC/CODE DIVERGENCES

- `FCT/libs/host_backup/src/host_backup/config.py` line 222: `get_events_dir()` returns `Path(state_dir) / "events" / "backup"`. If `state_dir` defaults to the repo root, this is outside `runtime/` and inside the main workspace. But `LOG_FILE = Path("/tmp/host-backup.log")` is under `/tmp/`. These are two different log locations for the same service with no doc explaining the split.

### 7. RECOMMENDED CANONICAL TERM + DEFINITION

**Recommended terms**:
- **Service log**: unstructured text log at `/tmp/<service>.log`, written by loguru. Ephemeral; may be captured by host_backup.
- **Event log**: structured, append-only JSONL at `events/<source>/events.jsonl`. Persistent; queryable via `mngr event`.
- **Transcript**: Claude Code conversation record in `runtime/<agent-id>/` or similar. Backed up by runtime_backup.

What must change: Document in FCT CLAUDE.md which log types are persistent vs ephemeral and how to access each. The split between `/tmp/host-backup.log` (service log) and `events/backup/events.jsonl` (event log) for the same service should be documented.

---

## events

### 1. CANONICAL DEFINITION

**System A (canonical, on-disk)**: Append-only JSONL files at `events/<source>/events.jsonl`. Defined in `REPO/style_guide.md` lines 1400-1434.

Standard event envelope:
```json
{
  "timestamp": "<ISO-8601>",
  "type": "<event_type>",
  "event_id": "<uuid>",
  "source": "<source_name>",
  ...
}
```

`REPO/libs/mngr/imbue/mngr/api/events.py` line 54:
```python
_EVENTS_JSONL_FILENAME = "events.jsonl"
```

**System B (in-memory SSE)**: `AgentEventQueues` in `FCT/apps/system_interface/imbue/system_interface/event_queues.py`. In-memory queues for real-time UI streaming via Server-Sent Events. Not persisted to disk.

`FCT/apps/system_interface/imbue/system_interface/events.py`:
```python
class BufferBehavior(Enum):
    STORE   # keep in replay buffer
    IGNORE  # drop immediately
    FLUSH   # drain buffer
```

### 2. ALL USAGES

**On-disk events** (System A):
- `REPO/libs/mngr/imbue/mngr/api/events.py`: `EventRecord`, `EventSourceInfo`, `stream_all_events()`; rotation: `events.jsonl.<timestamp>`
- `REPO/libs/mngr/imbue/mngr/cli/events.py`: `mngr event TARGET [SOURCES...]`; options: `--follow`, `--tail`, `--head`, `--source`, `--include`, `--exclude`
- `FCT/libs/host_backup/src/host_backup/events.py`: `BackupEventType` (13 types), `BACKUP_EVENT_SOURCE = EventSource("backup")`, `write_event()` appends to `events_dir / "events.jsonl"`
- `FCT/libs/host_backup/src/host_backup/config.py` line 222: `get_events_dir()` returns `Path(state_dir) / "events" / "backup"`
- Path convention: `events/<source>/events.jsonl` where source = subdirectory name
- For agents: `agents/<agent_id>/events` as subpath
- For hosts: `events` as subpath

**Backup event types** (13, from `FCT/libs/host_backup/src/host_backup/events.py` `BackupEventType`):
- `backup_started`, `snapshot_created`, `snapshot_deleted`, `restic_backup_succeeded`, `restic_backup_failed`, `forget_completed`, `prune_completed`, `prune_skipped`, `config_reloaded`, `repo_init_attempted`, `repo_init_succeeded`, `tick_skipped_due_to_missing_secrets`, `tick_error`

**In-memory SSE events** (System B):
- `FCT/apps/system_interface/imbue/system_interface/event_queues.py`: `AgentEventQueues.broadcast()`; `BufferBehavior` determines replay buffering
- `FCT/apps/system_interface/imbue/system_interface/events.py`: `BufferBehavior` enum

### 3. COMPETING/MULTIPLE DEFINITIONS

Two completely separate event systems:

| Aspect | On-disk JSONL (System A) | In-memory SSE (System B) |
|--------|--------------------------|--------------------------|
| Storage | `events/<source>/events.jsonl` | In-memory queues |
| Persistence | Append-only, survives restart | Lost on process death |
| Consumer | `mngr event` CLI | UI SSE streams |
| Producer | host_backup, other services | system_interface broadcasts |
| Protocol | File I/O | HTTP SSE |
| Replay | Via file read | Via `BufferBehavior.STORE` |

### 4. TERMINOLOGY VARIANTS

- "event" — both systems use this term
- "event log" — typically refers to on-disk JSONL
- "event stream" — could mean either system
- "SSE" / "Server-Sent Event" — specifically in-memory system B
- "event queue" — in-memory system B (`AgentEventQueues`)
- "event source" — in on-disk system, the `source` field in JSONL and subdirectory name
- `EventRecord` — parsed on-disk event
- `BufferBehavior` — in-memory replay policy

### 5. AMBIGUITIES/INCONSISTENCIES

- The two event systems have no connection to each other. An event written to `events/backup/events.jsonl` is NOT automatically broadcast via SSE to the UI. If UI real-time backup status is needed, it requires separate plumbing.
- `BackupEventType.SNAPSHOT_CREATED` / `SNAPSHOT_DELETED` in on-disk events refer to filesystem pre-backup captures (host_backup System C "snapshot"), not restic backup artifacts. The event naming could be confused with restic snapshot lifecycle. (See snapshots section.)
- `mngr event` requires a TARGET (host or agent), so the on-disk events are scoped to an entity. The in-memory SSE events appear to be scoped to an agent but the scoping mechanism is different.

### 6. DOC/CODE DIVERGENCES

- `style_guide.md` lines 1400-1434 documents the on-disk JSONL convention thoroughly. The in-memory SSE system is not mentioned in style_guide.md. This is a gap: two event systems exist but only one is in the style guide.
- `FCT/libs/host_backup/src/host_backup/events.py` `write_event()` appends to `events_dir / "events.jsonl"` — consistent with style guide convention.

### 7. RECOMMENDED CANONICAL TERM + DEFINITION

Keep both systems but name them clearly:
- **Event log** (System A): the persistent, append-only on-disk record at `events/<source>/events.jsonl`. The authoritative source for service history. Read via `mngr event`.
- **Event stream** (System B): the ephemeral, in-memory SSE queue in `system_interface`. Used for real-time UI updates. Not a log; not persisted.

What must change: `style_guide.md` should document System B (`AgentEventQueues`) as the "event stream" and distinguish it from the "event log." Event type names in host_backup should be renamed to avoid confusion with restic snapshots (e.g. `CAPTURE_CREATED` vs `SNAPSHOT_CREATED`).

---

## upstream / parent

### 1. CANONICAL DEFINITION

FCT instances derive from an upstream template repository. The relationship is defined in `parent.toml` at the FCT root:

`FCT/parent.toml`:
```toml
url = "https://github.com/imbue-ai/forever-claude-template.git"
branch = "main"
```

Two skills manage this relationship:
- `update-self`: pulls changes from upstream into the current FCT instance
- `submit-upstream-changes`: pushes improvements from the instance back to upstream via PR

### 2. ALL USAGES

**`parent.toml`** (`FCT/parent.toml`):
- Holds `url` and `branch` of the upstream template
- Read by `update-self` skill to know which remote/branch to pull from

**`update-self` skill** (`FCT/.agents/skills/update-self/SKILL.md`):
- `git pull upstream "$BRANCH"` where branch comes from `parent.toml`
- Imports template improvements into the current instance
- Adds upstream as a git remote if not already present

**`submit-upstream-changes` skill** (`FCT/.agents/skills/submit-upstream-changes/SKILL.md`):
- Cherry-picks commits onto `submit/<short-name>` branch in upstream repo
- Creates PR via `gh pr create`
- Never pushes directly to upstream main
- What TO submit: skills, scripts, CLAUDE.md scaffolding, Dockerfile, `services.toml`
- What NOT to submit: `PURPOSE.md`, memory, runtime state

**Git remote naming**:
- `origin` = the instance's own GitHub remote (agent-specific repo)
- `upstream` = the template repo (added by `update-self` skill)

### 3. COMPETING/MULTIPLE DEFINITIONS

- "parent" = the template/upstream repo, as named in `parent.toml`
- "upstream" = the same concept, used in skill names and git remote name
- Both terms refer to the same entity: `https://github.com/imbue-ai/forever-claude-template.git`

### 4. TERMINOLOGY VARIANTS

- "parent" — used in `parent.toml` filename and config key
- "upstream" — used in git remote name, skill name (`update-self`, `submit-upstream-changes`)
- "template" — informal term for the upstream repo (forever-claude-template)
- "origin" — the instance repo (not the upstream)

### 5. AMBIGUITIES/INCONSISTENCIES

- `parent.toml` uses the key `url` without labeling it "parent_url" or "upstream_url". The file name implies "parent" but the skill uses "upstream" as the git remote. Two different words for the same concept in adjacent files.
- The skill `submit-upstream-changes` creates branches in the upstream repo named `submit/<short-name>` — but what `<short-name>` means is defined in the skill SKILL.md, not in `parent.toml`. There's no machine-readable short-name field.
- `update-self` is the skill name (implies self-updating), but conceptually it's "pull from parent/upstream". The name conflates the actor ("self") with the direction ("from upstream").

### 6. DOC/CODE DIVERGENCES

- `FCT/parent.toml` uses `branch = "main"` which is the upstream branch to pull from. The `update-self` SKILL.md says to pull from `upstream "$BRANCH"` — consistent.
- No code-level parser for `parent.toml` was found in the mngr or system_interface codebase. The file appears to be read only by the skill's shell commands (via `toml` parsing in the skill script or by Claude directly). This means `parent.toml` is a convention, not a formally typed config.

### 7. RECOMMENDED CANONICAL TERM + DEFINITION

**Recommended term**: Standardize on "upstream" everywhere (matches git conventions).
- Rename `parent.toml` to `upstream.toml` with keys `url` and `branch`
- Or rename the file key to `upstream_url` and `upstream_branch`
- Keep git remote as `upstream` (already consistent)

**Canonical definition**: The upstream template repository (`https://github.com/imbue-ai/forever-claude-template.git`) from which this FCT instance was derived. Changes flow in both directions: `update-self` (downstream ← upstream) and `submit-upstream-changes` (downstream → upstream via PR).

What must change: Standardize on "upstream" over "parent" in all user-facing text. The SKILL.md for `update-self` should document the `parent.toml` (or renamed `upstream.toml`) parsing explicitly.

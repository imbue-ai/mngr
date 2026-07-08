# Minds: move state out of `~/.minds/` into platform-canonical directories

## Motivation

Today every piece of minds runtime state lives under `~/.minds/` (or `~/.minds-staging/`, `~/.minds-dev-<name>/` for non-prod tiers). That directory currently holds:

| Subpath under `~/.minds/` | Content | Sensitivity |
|---------------------------|---------|-------------|
| `auth/one_time_codes.json` | Per-launch one-time codes for the local backend | Short-lived secret |
| `auth/sessions/<account_id>.json` | Supertokens session tokens, refresh tokens | Secret |
| `mngr/` | mngr `MNGR_HOST_DIR` for this tier (profiles, keys, data.json, plugin state) | Mixed; includes SSH keys |
| `mngr/profiles/*/keys/docker_ssh_key` | Per-agent SSH private keys to Docker / Lima | Secret |
| `ssh/dynamic_hosts.toml`, `ssh/keys/leased_host` | Pool-host SSH config + keys | Secret |
| `telegram/user_credentials.json`, `telegram/bots/<id>.json` | Telegram bot tokens | Secret |
| `template-cache/forever-claude-template/` | Cached FCT bare clone, ~120 MB reusable across agent creates | Cache, regenerable |
| `logs/minds.log`, `logs/minds-events.jsonl` | Backend stdout + structured events | Logs, no PII expected |
| `last_good_agent_topology.json` | Snapshot for recovery on next launch | App state |
| `latchkey/` | Per-agent latchkey permission state, gateway socket | Mixed; tokens inside |
| `backups/` | Restic snapshots of agent worktrees (when backup is enabled) | User data |

Cramming all of these into a single dotfolder under `$HOME` causes four problems:

1. **Wrong category on macOS.** Apple's File System Programming Guide says secrets / app state go in `~/Library/Application Support/<bundle>/`, regenerable caches in `~/Library/Caches/<bundle>/`, logs in `~/Library/Logs/<bundle>/`. Putting all of it in `~/.minds/` makes Finder hide it (dotfiles are invisible), prevents the OS from cleaning caches when the disk fills up, and means Time Machine backs up files that should be excluded (cache + logs).
2. **Wrong category on Linux.** The XDG Base Directory spec splits state across `XDG_CONFIG_HOME` (`~/.config/`), `XDG_DATA_HOME` (`~/.local/share/`), `XDG_CACHE_HOME` (`~/.cache/`), and `XDG_STATE_HOME` (`~/.local/state/`). Users with non-default XDG_* env vars (common on NAS-backed homes, Snap-confined apps, dotfile-managed systems) expect apps to honor those.
3. **Cache and secret share fate.** When a user runs `rm -rf ~/.minds/` to "reset," they wipe authenticated sessions and SSH keys along with the FCT clone cache. There is no granularity. Conversely, a snapshot of `~/.minds/` shipped in a bug report contains private keys we never intended to receive.
4. **Bad uninstall story.** The standard macOS "drag the .app to Trash" leaves `~/.minds/` behind permanently. There is no convention for users to find what's left over. Application Support / Caches / Logs are the documented places for an uninstaller (or a `minds env teardown` command) to clean.

## Goals

1. Per-platform layout that follows the host OS's documented conventions.
2. Backwards compatibility for any existing `~/.minds/` directories — the upgrade path must NOT lose user state. Live migration on first launch of the new build.
3. Keep multi-tier (`production` / `staging` / per-dev) working unchanged. Today the tier suffix is baked into the dotfolder name (`~/.minds-staging`); after the move, the tier should appear as a subdirectory of the canonical roots, not as a folder-name suffix in `$HOME`.
4. Do not move what is not minds's: `~/.mngr/` (for users running mngr standalone), `~/.claude/`, system `~/.ssh/`, Lima's `~/.lima/`, Docker's daemon dirs. Minds gets one isolated subtree per category.

## Non-goals

- Reorganizing `mngr`'s own data layout (`~/.mngr/`). mngr is shipped as an importable CLI; users may run it both with and without minds. Its directory choice is mngr's concern, not minds's. Minds will continue to set `MNGR_HOST_DIR` to whatever subpath minds chose for its own bundled mngr state.
- Changing the contents or schema of any file. The file names and JSON / TOML inside stay identical; only the path containing them moves.
- Touching the `forever-claude-template` repository or the in-VM contents. Inside an agent VM, paths remain Linux-conventional (`/code`, etc.); only the host-side paths move.
- Changing what `MINDS_ROOT_NAME` means as an *identifier*; the env var still selects which tier's state to use. Only its mapping to a filesystem path changes.

## Proposed layout

The single dotfolder `~/.minds-<tier>/` is replaced by four roots per platform, each with the tier name as its first subdirectory.

### macOS (Darwin)

| Category | Path | What goes here |
|----------|------|----------------|
| **Application Support** (state, secrets, user data) | `~/Library/Application Support/Minds/<tier>/` | `auth/`, `mngr/`, `ssh/`, `telegram/`, `latchkey/`, `last_good_agent_topology.json`, `backups/` |
| **Caches** (regenerable, OS may purge) | `~/Library/Caches/Minds/<tier>/` | `template-cache/`, lima ubuntu-cloudimg pre-fetch, FCT clones |
| **Logs** (rotating, ignorable) | `~/Library/Logs/Minds/<tier>/` | `minds.log`, `minds-events.jsonl` |
| **Preferences** (user-editable config) | `~/Library/Application Support/Minds/<tier>/config/` | `client.toml`, `default_account_id`, `minds_root.toml` — anything a power-user might edit by hand. (Apple's `~/Library/Preferences/<bundle>.plist` is reserved for `NSUserDefaults`; we don't use it.) |

For `production` the `<tier>/` directory is literally named `production/`. For staging it's `staging/`. For dev tiers it's `dev-<name>/` (matching the existing `minds-dev-<name>` MINDS_ROOT_NAME). This lifts the tier-name discriminator from `$HOME` (where it polluted the home dir) into a flat subdirectory of one well-known root.

The bundle identifier is `Minds` (matching the ToDesktop bundle name). Each role gets exactly one root per tier, no mixing.

### Linux

| Category | Path (with XDG fallback) | What goes here |
|----------|--------------------------|----------------|
| **State** (long-lived non-config, mutable) | `$XDG_STATE_HOME/minds/<tier>/` (default `~/.local/state/minds/<tier>/`) | `last_good_agent_topology.json`, `latchkey/` |
| **Data** (secrets, sessions, keys, user-relevant non-config) | `$XDG_DATA_HOME/minds/<tier>/` (default `~/.local/share/minds/<tier>/`) | `auth/`, `mngr/`, `ssh/`, `telegram/`, `backups/` |
| **Cache** | `$XDG_CACHE_HOME/minds/<tier>/` (default `~/.cache/minds/<tier>/`) | `template-cache/` |
| **Config** | `$XDG_CONFIG_HOME/minds/<tier>/` (default `~/.config/minds/<tier>/`) | `client.toml`, `minds_root.toml` |
| **Logs** (no canonical XDG dir) | `$XDG_STATE_HOME/minds/<tier>/logs/` | `minds.log`, `minds-events.jsonl` |

XDG envs are honored when set, defaults used otherwise. Linux does not separate state from data the way Apple does, so state-vs-data is judged by [the XDG spec](https://specifications.freedesktop.org/basedir-spec/basedir-spec-latest.html): state = "things needed across reboots but not config or cache" (history, recent files, machine-specific tokens); data = "things you'd want to copy to a new machine" (sessions, key material).

### Override knob

`MINDS_DATA_HOME` (singular, opaque) overrides everything: when set, all four roots become subdirectories of `$MINDS_DATA_HOME/<tier>/` named `app_support/`, `cache/`, `logs/`, `config/`. This is how tests (and the CI runner) get a single self-contained throwaway dir. Behavior matches today's `MINDS_DATA_DIR` (or equivalent) if one exists; if not, `MINDS_DATA_HOME` is new. Document it in README.

## Mapping (concrete, per file)

| Today | macOS | Linux |
|-------|-------|-------|
| `~/.minds/auth/one_time_codes.json` | `~/Library/Application Support/Minds/production/auth/one_time_codes.json` | `~/.local/share/minds/production/auth/one_time_codes.json` |
| `~/.minds/auth/sessions/<id>.json` | `~/Library/Application Support/Minds/production/auth/sessions/<id>.json` | `~/.local/share/minds/production/auth/sessions/<id>.json` |
| `~/.minds/mngr/...` (MNGR_HOST_DIR) | `~/Library/Application Support/Minds/production/mngr/...` | `~/.local/share/minds/production/mngr/...` |
| `~/.minds/ssh/dynamic_hosts.toml` | `~/Library/Application Support/Minds/production/ssh/dynamic_hosts.toml` | `~/.local/share/minds/production/ssh/dynamic_hosts.toml` |
| `~/.minds/template-cache/...` | `~/Library/Caches/Minds/production/template-cache/...` | `~/.cache/minds/production/template-cache/...` |
| `~/.minds/logs/minds.log` | `~/Library/Logs/Minds/production/minds.log` | `~/.local/state/minds/production/logs/minds.log` |
| `~/.minds/last_good_agent_topology.json` | `~/Library/Application Support/Minds/production/last_good_agent_topology.json` | `~/.local/state/minds/production/last_good_agent_topology.json` |
| `~/.minds-staging/...` | `~/Library/Application Support/Minds/staging/...` (etc.) | `~/.local/share/minds/staging/...` |
| `~/.minds-dev-josh-3/...` | `~/Library/Application Support/Minds/dev-josh-3/...` | `~/.local/share/minds/dev-josh-3/...` |

## Implementation plan

### Phase 1 — Introduce the new path resolver behind an interface

`apps/minds/imbue/minds/bootstrap.py` currently has:

```python
def minds_data_dir_for(root_name: str) -> Path:
    return Path.home() / ".{}".format(root_name)
```

Replace with a `MindsPaths` pydantic model exposing the four roots plus a `legacy_data_dir` property pointing at the old `~/.{root_name}` location. All callers acquire paths from `MindsPaths` instead of computing them from `data_dir`. Roles:

```python
class MindsPaths(BaseModel):
    tier: str  # "production", "staging", "dev-<name>"
    app_support: Path  # secrets, sessions, mngr/, ssh/, telegram/, latchkey/, backups/
    cache: Path        # template-cache/, ubuntu-cloudimg-cache/
    logs: Path         # minds.log, minds-events.jsonl
    config: Path       # client.toml
    legacy_data_dir: Path  # ~/.minds-<tier>/ -- read-only after migration
```

Resolution rule:
1. If `MINDS_DATA_HOME` is set, use the override layout.
2. Else, dispatch on `sys.platform`: `darwin` → Apple paths, `linux` → XDG paths. Windows is unsupported (matches CLAUDE.md).
3. Tier name is derived from `MINDS_ROOT_NAME` exactly as today (`minds` → `production`, `minds-staging` → `staging`, `minds-dev-josh-3` → `dev-josh-3`).

Add unit tests in `bootstrap_test.py` covering all three platforms × all three tier shapes × `MINDS_DATA_HOME` set/unset matrix.

Existing callers that compute `data_dir / "subdir"` migrate to `paths.app_support / "subdir"`, `paths.cache / "template-cache"`, etc. Every single literal `~/.minds/<x>` reference needs auditing — there are roughly 30 in production code (count via `grep -rn 'data_dir / "' apps/minds/imbue/minds/ --include='*.py' | grep -v _test.py`).

### Phase 2 — One-shot migration on first run

On every minds backend startup:

1. Compute the new four roots.
2. If `legacy_data_dir` exists AND any of the new roots are empty (no `migration.lock` marker file), perform a migration.
3. Migration is per-subdir; map according to the table above. Use `shutil.move` (not copy) — move-then-symlink is fine on POSIX since both roots are on the same filesystem in the normal case. Cross-filesystem (rare; matters for Linux users whose `$HOME` is on a different mount than `~/.local/share`) falls back to copy + rename.
4. After successful migration, write `migration.lock` containing the migration timestamp + the version that performed it, into `app_support/`.
5. Leave `legacy_data_dir` in place but write a `MIGRATED.txt` README inside it pointing to the new roots. Do not delete it automatically. The next major minds release (one minor version later) prints a deprecation warning if the legacy dir still exists, and the major version after that removes it.

Migration is idempotent (re-running does nothing once the lock file is present). Failures during migration roll back atomically: any partial move uses a `_in_progress` suffix, and the lock file is only written at the end.

Audit cases:
- The legacy `~/.minds-staging/` dir survives the move — it is now resolved by the same `tier=staging` paths. Existing CI sessions and Electron sessions that have file handles open through the old paths are tolerated by the migration (the file moves but Linux/macOS file handles follow the inode).
- `~/.minds/mngr/profiles/*/data.json` references paths in its agent records; those are absolute paths to the legacy SSH keys. After moving the keys, update each `data.json` to point at the new location. Test-cover this in `bootstrap_test.py` with a fake legacy tree.

### Phase 3 — Audit external consumers and update docs

These external touch-points reference the old paths and must be updated in lock-step:

- `apps/minds/scripts/launch_to_msg_e2e.py` — `MINDS_HOME = Path(os.environ.get("HOME")) / ".minds"`. CI test driver. The e2e should learn `MindsPaths` and acquire each path it needs through it.
- `apps/minds/scripts/mac-runner-reset.sh` — sweeps `~/.minds*/` between runs. Update to walk the new app_support / cache / logs roots, falling back to the legacy dir if it still exists.
- `apps/minds/docs/*.md` (release.md, design.md, environments.md) — references `~/.minds/logs/minds.log` etc.
- `apps/minds/scripts/propagate_changes` and `just minds-start` — `MNGR_HOST_DIR` env var still gets set to `paths.app_support / "mngr"`, transparent change.
- `apps/minds/imbue/minds/desktop_client/templates.py` — uses `_SHARED_TIER_ROOT_NAMES`. Keep the tier-name set; just route through the resolver.

### Phase 4 — README + uninstaller

- Add an "Uninstalling Minds" section to `apps/minds/README.md` listing the four paths per platform.
- Add a `minds env teardown` CLI subcommand (if not already) that deletes all four roots for a chosen tier after confirmation.

## Risks and unknowns

1. **`paths.app_support / "mngr"` contains absolute paths inside data.json.** Confirmed by an inventory: mngr's per-host records embed `~/.minds/mngr/profiles/<id>/keys/docker_ssh_key`. Phase 2 migration must rewrite these. Implementation: load each `data.json`, replace the old prefix with the new prefix, write back atomically. Test-cover.
2. **Process-restart timing.** Running minds backends hold paths open. Migration runs at startup BEFORE the FastAPI app is constructed, so no in-process consumers exist yet. But: if a second minds instance is concurrently starting on the same tier, the second one sees the lock file written mid-migration. Document this. The Electron singleton lock (`app.requestSingleInstanceLock()`) already protects against this on a single user account; cross-account is out of scope.
3. **Lima VM bind mounts.** Lima's instance yaml may have bind-mount entries pointing into `~/.minds/` (e.g., for SSH keys it surfaces inside the VM). Audit `libs/mngr_lima/` and `forever-claude-template/.mngr/settings.toml` for any hardcoded `~/.minds` references. If found, update to use the new path AND require a one-shot VM destroy + recreate as part of the migration (or — better — make the migration leave a backwards-compat symlink at the old location pointing to the new location, so existing VMs see the right files until they're naturally recreated). The symlink-shim approach is preferred; remove the symlinks after one minor-version cycle.
4. **CI runner state.** The self-hosted Mac CI runner has its own `~/.minds-*` directories from past runs. The CI workflow should set `MINDS_DATA_HOME=$RUNNER_TEMP/minds` to opt into the override layout for full reproducibility, independent of the runner's home dir state.
5. **Cross-tier collisions.** Today `MINDS_ROOT_NAME=minds-staging` and `MINDS_ROOT_NAME=minds-dev-staging` map to different dotfolders. Under the new scheme they would map to `<root>/staging/` and `<root>/dev-staging/` — still distinct, but only if the resolver is careful with the prefix-stripping rule. Test-cover `minds-dev-staging` -> `dev-staging` (not `staging`).

## Acceptance criteria

1. `minds_data_dir_for` and all callsites use `MindsPaths`.
2. Manual: launch a fresh build on macOS, confirm `~/Library/Application Support/Minds/production/` is created and populated; `~/.minds/` is not. Same check for `~/Library/Logs/Minds/production/minds.log`.
3. Manual: launch a fresh build on Linux, confirm `~/.local/share/minds/production/` is populated, `~/.cache/minds/production/template-cache/` is populated, no `~/.minds/`.
4. Migration: run with a pre-populated legacy `~/.minds/` directory containing a real auth session + a real mngr profile. Confirm the session still works after migration (sign-in not required) and the mngr agent still resolves.
5. CI: `apps/minds/scripts/launch_to_msg_e2e.py` and `mac-runner-reset.sh` work with `MINDS_DATA_HOME` override layout.
6. Unit: `bootstrap_test.py` covers the platform × tier × override matrix.
7. Reverse: `MINDS_ROOT_NAME=minds-staging` continues to resolve to a distinct tier root from `MINDS_ROOT_NAME=minds`.

## Out of scope (separate follow-ups)

- Moving `~/.mngr/` itself (mngr's standalone-user data) under platform-canonical roots. That's a mngr-side decision; document it as a future spec but don't bundle it in.
- Rotating any of the file *schemas* inside (e.g., session_store JSON format). Path-only refactor.
- Encryption-at-rest for `auth/sessions/` (today they sit on disk as plaintext JSON). Worth doing; not blocking this spec.

## Suggested commit shape

1. `bootstrap: introduce MindsPaths resolver + three-platform layout (no callsite changes yet)`
2. `bootstrap_test: cover MindsPaths resolution matrix`
3. `minds: route ~30 call sites through MindsPaths instead of minds_data_dir_for`
4. `minds: deprecate minds_data_dir_for (keep symbol as alias to legacy_data_dir for one cycle)`
5. `minds: one-shot legacy → new layout migration on startup`
6. `minds: rewrite absolute paths in mngr/profiles/*/data.json during migration`
7. `e2e + mac-runner-reset: switch to MindsPaths`
8. `minds: leave backwards-compat symlinks at legacy dir for one minor-version cycle`
9. `docs: README uninstall section, release notes for the move`

## How to verify behavior

- Drive `minds-launch-to-msg.yml` CI run (existing self-hosted Mac runner) and confirm green. The e2e exercises real on-disk state across launch + create + first message + slack + destroy; if migration is broken, one of those will fail visibly.
- Locally: stand up a fresh macOS user account (or a dedicated test home), install the build, walk Finder to confirm the directories appear in the expected places and nothing leaks into `~`.
- Locally on Linux: same with a fresh VM or container.

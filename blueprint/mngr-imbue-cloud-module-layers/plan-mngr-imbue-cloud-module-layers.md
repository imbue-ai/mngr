# Restructure `mngr_imbue_cloud` into layered modules

**Status: implemented** (both phases). This doc was edited post-implementation to correct two
pre-implementation inaccuracies and to record what was actually done; see the inline notes. The contract is
enforced by `test_no_import_layer_violations_mngr_imbue_cloud`.

Break the now-large, flat `mngr_imbue_cloud` plugin into a small set of layered sub-packages with an
enforced `import-linter` ordering contract (the same mechanism `mngr` and `minds` already use). The goals
are clearer separation of the slice/bare-metal subsystem from the generic connector/account core, a clean
seam around the pool-bake code so it can later move into the minds app, and a structure that makes the
eventual removal of the OVH-VPS path a localized deletion rather than a scavenger hunt.

This is a pure refactor: **no runtime behavior changes**, no new features, no schema changes. It is
mechanical module movement plus one large-file decomposition, governed by a layers contract.

Companion docs: `blueprint/ovh-baremetal-slices/plan-ovh-baremetal-slices.md` (slice design),
`blueprint/ovh-baremetal-slices/HANDOFF.md` (what was built), `blueprint/slice-fast-path-fixes/plan-slice-fast-path-fixes.md`
(lease/bake parity). Target audience: developers implementing or reviewing the refactor.

## Overview

- **Problem.** `mngr_imbue_cloud` is now 31 production modules (~9,400 lines) in a flat root plus a `cli/`
  sub-package. Four distinct concerns sit at the same level with nothing preventing them from importing each
  other arbitrarily: the generic connector/account core, the lease provider, the slice/bare-metal subsystem,
  and the provider-generic pool bake. `instance.py` alone is 2,013 lines.
- **Solution.** Introduce sub-packages that map one-to-one onto `import-linter` layers (high tier may import
  low tier; never the reverse), and add a `mngr_imbue_cloud layers contract` to the root `pyproject.toml`.
- **Slice vs VPS.** Confine the entire slice/bare-metal subsystem to a `slices/` package and the slice
  provider + backend to clearly named modules, so retiring the OVH-VPS path later is a bounded deletion.
- **Pool-bake seam.** Isolate `pool_bake.py` (already dependency-free of the rest of the plugin) into its own
  `bake/` layer, as preparation for its eventual move into the minds app (out of scope here).
- **Scope guard.** This is a structural refactor only. Behavior, public CLI surface, connector wire formats,
  and DB schema are unchanged.

## Goals and non-goals

**Goals**

1. A documented, enforced layer ordering for `mngr_imbue_cloud` via `import-linter`.
2. The slice/bare-metal subsystem isolated in one package (`slices/`).
3. `pool_bake.py` isolated behind a single layer (`bake/`) as an extraction seam.
4. `instance.py` decomposed into cohesive modules within the `providers/` package.
5. Both provider backends co-located so the VPS-vs-slice choice is visible in one place.

**Non-goals (explicitly out of scope)**

- **Splitting slice types out of the shared `data_types.py` / `primitives.py` is NOT part of this work.**
  `data_types.py` and `primitives.py` stay as single shared root modules holding both generic and
  slice/bare-metal types. (`BareMetalServer`, `BackendKind`, `BareMetalServerStatus`, etc. remain where they
  are.) This was considered and deliberately rejected as low-leverage and not clearly correct.
- Extracting `pool_bake` into the minds app. We only prepare the seam.
- Any change to the connector (`apps/remote_service_connector`), DB schema, or lease/release behavior.
- Removing the OVH-VPS path. We only make its future removal easier.

## Current state (facts the design relies on)

Internal import edges were enumerated from the source; the proposed layering below has zero conflicts with
them. Key facts:

- `pool_bake.py` has **no** internal (`imbue.mngr_imbue_cloud.*`) imports and defines its own `BakedPoolHost`.
  It is already a clean extraction target.
- The CLI already splits VPS vs slice: `cli/admin.py` is the OVH-VPS pool (`mngr imbue_cloud admin pool …`),
  `cli/server.py` is the bare-metal slice subsystem (`mngr imbue_cloud admin server …`).
- `cli/*` imports `pool_bake`, `bare_metal*`, `lima_slice_client`, and the connector/config/leaf modules, but
  **not** `instance.py` or `host.py`. The provider layer is consumed only by the plugin/registration layer.
- `instance.py` imports `auth_helper`, `client`, `session_store`, `config`, `host`, `lima_slice_client`,
  `slice_provider`, and the leaf modules. `slice_provider.py` imports `bare_metal` + `lima_slice_client`.
- The pluggy entry points are bare module paths (no `:attr`): pluggy registers the module object and scans it
  for `hookimpl`-decorated functions.
- There are **no out-of-plugin code importers**. `apps/minds` and `apps/remote_service_connector` deliberately
  stay free of `mngr_imbue_cloud` (minds inlines what it needs); the only references under `apps/` are in
  comments. So the refactor is contained to `libs/mngr_imbue_cloud` plus the two `pyproject.toml` files and the
  meta-ratchet test. (An earlier draft of this doc claimed non-trivial external importers; that was based on a
  faulty grep and is incorrect.)

## Target module layout

Layers are listed high to low. Sub-packages are layers; the small leaf modules stay as root modules (exactly
as `mngr`'s contract ends in `…errors`, `primitives`, `resources`). Within a layer (i.e. between sub-modules
of the same package) imports are unrestricted; the contract only enforces ordering *between* layers.

| Tier | Layer | Contents (current file -> new location) | Role |
|---|---|---|---|
| 1 | `plugin/` | `plugin.py` -> `plugin/entrypoints.py`; `slice_plugin.py` -> `plugin/slice_entrypoints.py`; `backend.py` + `SliceVpsDockerProviderBackend`/`SliceVpsDockerProviderConfig` (extracted from `slice_provider.py`) -> `plugin/backends.py` | pluggy entry points + backend/config registration |
| 2 | `cli/` | unchanged package: `root`, `_common`, `auth`, `hosts`, `keys`, `buckets`, `tunnels`, `paid`, `admin` (VPS pool), `server` (slices) | CLI commands |
| 3 | `bake/` | `pool_bake.py` -> `bake/pool_bake.py` | provider-generic bake; extraction seam toward minds |
| 4 | `providers/` | `instance.py` -> decomposed (see below); `slice_provider.py` -> `providers/slice_provider.py` (minus the backend/config classes) | mngr provider implementations |
| 5 | `hosts/` | `host.py` -> `hosts/host.py` | `ImbueCloudHost` |
| 6 | `slices/` | `bare_metal.py`, `bare_metal_db.py`, `bare_metal_prep.py`, `lima_slice.py`, `lima_slice_client.py`, `pricing.py` | slice/bare-metal subsystem |
| 7 | `connector/` | `client.py`, `auth_helper.py`, `session_store.py` | generic account/session/HTTP plane |
| 8 | `config.py` | unchanged (root module) | provider config |
| 9 | `data_types.py` | unchanged (root module; keeps generic **and** slice types) | shared data types |
| 10 | `errors.py` | unchanged (root module) | error hierarchy |
| 11 | `primitives.py` | unchanged (root module; keeps generic **and** slice primitives) | shared primitives |

Notes:

- `bake/` (tier 3) and `providers/` (tier 4) are mutually independent today; the contract simply fixes an
  order. `bake` is placed above `providers` to signal it is orchestration headed for minds, not a library the
  providers depend on.
- `slices/` (tier 6) and `connector/` (tier 7) are mutually independent today; the fixed order is harmless.
- All four leaf modules (`config`, `data_types`, `errors`, `primitives`) are imported by many layers above and
  import only each other downward (`config`/`data_types` -> `errors`, `primitives`), so they sit at the bottom.

### `import-linter` contract

Add `imbue.mngr_imbue_cloud` to `root_packages` and append a new contract in the root `pyproject.toml`
(alongside the existing `mngr` and `minds` contracts):

```toml
[[tool.importlinter.contracts]]
name = "mngr_imbue_cloud layers contract"
type = "layers"
layers = [
    "imbue.mngr_imbue_cloud.plugin",
    "imbue.mngr_imbue_cloud.cli",
    "imbue.mngr_imbue_cloud.bake",
    "imbue.mngr_imbue_cloud.providers",
    "imbue.mngr_imbue_cloud.hosts",
    "imbue.mngr_imbue_cloud.slices",
    "imbue.mngr_imbue_cloud.connector",
    "imbue.mngr_imbue_cloud.config",
    "imbue.mngr_imbue_cloud.data_types",
    "imbue.mngr_imbue_cloud.errors",
    "imbue.mngr_imbue_cloud.primitives",
]
```

The existing `test_no_import_layer_violations` ratchet only checks the `mngr layers contract` (the helper
`check_no_import_lint_errors` defaults to that contract name; the `minds` contract is not gated either). So a
contract added to `pyproject.toml` is **not** enforced automatically -- add a dedicated
`test_no_import_layer_violations_mngr_imbue_cloud` test in `test_meta_ratchets.py` that calls
`check_no_import_lint_errors(_REPO_ROOT, contract_name="mngr_imbue_cloud layers contract")`.

## How the layout serves VPS-vs-slice separation

The OVH-VPS path and the slice path stay one codebase at lease time (intentional, per the slice design), but
their *backend-specific* code is now concentrated in known places:

- Slice/bare-metal machine code: entirely within `slices/`.
- Slice provider implementation: `providers/slice_provider.py`.
- Both backends + configs: `plugin/backends.py` (so the choice of which backend to register is one file).
- VPS-pool admin/bake CLI: `cli/admin.py`; slice admin CLI: `cli/server.py`.
- The VPS-vs-slice branch in the lease slow-path rebuild: `providers/rebuild.py` (see decomposition below).

Retiring the OVH-VPS path later becomes: delete `cli/admin.py`, drop the VPS branch in `providers/rebuild.py`,
drop the VPS path in `bake/pool_bake.py`, and remove the VPS backend from `plugin/backends.py`. `slices/`,
`connector/`, and the leaf modules are untouched.

## Pool-bake extraction readiness

`bake/pool_bake.py` is isolated as its own layer with no dependency on `providers/`, `hosts/`, or `slices/`
(it shells out to `mngr create` and owns `BakedPoolHost`). The layers contract will *prevent* new coupling
from forming. When `pool_bake` later moves into the minds app (with minds calling back into a generic
`imbue_cloud`), the lift is mechanical. **No extraction happens in this work.**

## `instance.py` decomposition (within `providers/`)

`instance.py` (2,013 lines) is split by extracting the cohesive helper clusters into sibling modules inside
`providers/`, leaving the `ImbueCloudProvider` class (and its self-bound methods) in `instance.py`:

- `providers/instance.py` — the `ImbueCloudProvider` class: discovery, listing, `get_host`, lifecycle entry
  points (`create_host`, `destroy_host`, `delete_host`, `start_host`, `stop_host`), lease bookkeeping. The
  detail-shaping *methods* that need `self` (`_build_host_details_from_raw`, `_build_agent_details_from_raw`)
  stay here; only the free-function helpers below move out.
- `providers/listing.py` — the pure listing-shaping helpers `derive_host_state_from_raw`,
  `derive_offline_note_from_raw`, `map_docker_status_to_host_state` (de-underscored, since they are now
  imported across modules). The dead `_certified_host_name` helper is dropped.
- `providers/wipe.py` — `build_pool_host_wipe_script` + its btrfs/label/volume-name constants (pure; renders
  the pre-release data-wipe bash).
- `providers/rebuild.py` — the slow-path rebuild seam, converted from methods to module-level functions taking
  explicit deps: `build_delegated_vps_provider(*, name, config, mngr_ctx)`,
  `build_slice_rebuild_provider(*, name, config, mngr_ctx, lease_result)`, and `_build_delegated_vps_config`.
  This is where the VPS-vs-slice provider choice concentrates. `_rebuild_leased_container` stays a method on
  the class (it orchestrates the teardown/create over the outer-host context manager) and calls these
  functions; the one-line `is_slice` detection stays at that call site.

These are sub-modules of the `providers/` layer, so they may freely import each other and the lower layers.
The rebuild functions take explicit deps (not the whole provider) so `rebuild.py` does not import `instance.py`
-- no cycle. Pure functions move with `@pure` intact. **Do not introduce `TYPE_CHECKING` guards** to resolve any
import ordering that arises during the split (see `libs/mngr/llm_faq.md`); restructure instead, or keep the
symbol in `instance.py` if a split would force a cycle. Outcome: `instance.py` drops to ~1,775 lines.

## Entry-point handling (pluggy + empty `__init__` constraint)

`plugin.py` and `slice_plugin.py` are the two pluggy entry-point modules and currently sit at the package
root. They cannot become a package `__init__.py` (the repo forbids code in `__init__.py`, except the single
`hookimpl = pluggy.HookimplMarker("mngr")` line at the library root). Resolution:

- Make `plugin/` a real package with an **empty** `__init__.py`.
- Put the `imbue_cloud` hookimpls in `plugin/entrypoints.py` and the `imbue_cloud_slice` hookimpls in
  `plugin/slice_entrypoints.py` (both import `from imbue.mngr_imbue_cloud import hookimpl` as today).
- Move `ImbueCloudProviderBackend` (from `backend.py`) and `SliceVpsDockerProviderBackend` +
  `SliceVpsDockerProviderConfig` (from `slice_provider.py`) into `plugin/backends.py`.
- Update the two entry points in `libs/mngr_imbue_cloud/pyproject.toml`:

  ```toml
  [project.entry-points.mngr]
  imbue_cloud = "imbue.mngr_imbue_cloud.plugin.entrypoints"
  imbue_cloud_slice = "imbue.mngr_imbue_cloud.plugin.slice_entrypoints"
  ```

**Warning:** the `SliceVpsDockerProviderConfig` type is referenced by `providers/` (`instance.py`'s
rebuild seam). Moving it to `plugin/backends.py` would invert the layering (providers importing plugin). Keep
`SliceVpsDockerProviderConfig` in `providers/slice_provider.py` (tier 4) and import it from there into
`plugin/backends.py` (tier 1, importing lower — allowed). Only the *backend* classes (which exist solely for
registration) move up to `plugin/`.

## Compatibility

There are no out-of-plugin importers (see Current state), so the rewrite is contained to `libs/mngr_imbue_cloud`
itself. **Do not introduce re-export shims.** Every intra-plugin import site (production code and the co-located
test files) is rewritten to its new path in the same move, so there is no transitional indirection and no
leftover cruft to delete.

This is mechanical: each moved module has a known old path and a known new path, so the rewrite is a
deterministic path substitution, verifiable by grepping that no `imbue.mngr_imbue_cloud.<old_path>` references
remain anywhere in the repo. Anchor the substitution on a word boundary after each module name so a prefix like
`bare_metal` does not corrupt `bare_metal_db`.

Move the co-located test files alongside their modules (`bare_metal_test.py` -> `slices/bare_metal_test.py`,
etc.), preserving the `*_test.py` (unit) and `test_*.py` (integration/acceptance) suffixes so test discovery
and the test-type conventions are unaffected. Test files import the modules under test by path too, so they are
part of the same single-pass rewrite.

## Migration plan (phases)

**Phase 0 — guard.** Confirm a green baseline (`just test-quick libs/mngr_imbue_cloud` and the import-linter
ratchet) before moving anything.

**Phase 1 — packages, moves, contract, and all import rewrites (mechanical, single green commit).**
- Create `plugin/`, `bake/`, `providers/`, `hosts/`, `slices/`, `connector/` (each with empty `__init__.py`).
- Move files per the layout table (excluding the `instance.py` decomposition, which is Phase 2), with their
  co-located test files (`git mv`, preserving history).
- Extract the two backend classes into `plugin/backends.py`; keep `SliceVpsDockerProviderConfig` in
  `providers/slice_provider.py`.
- Rewrite **every** intra-plugin importer (production + moved tests) to the new paths in the same commit, so no
  commit ever leaves a moved module unreachable. No shims.
- Update the entry-point paths, add the `import-linter` contract + `root_packages` entry, and add the
  `test_no_import_layer_violations_mngr_imbue_cloud` meta-ratchet that enforces it.
- Run `uv sync --all-packages` (so the changed entry-point metadata is reinstalled), verify no
  `imbue.mngr_imbue_cloud.<old_path>` references remain (grep), then run the package suite + ratchets +
  meta-ratchets.

**Phase 2 — decompose `instance.py`.** Extract `providers/{listing,wipe,rebuild}.py` per the decomposition
section; re-run tests, ratchets, and the contract.

Each phase is independently committable and leaves the tree green. The work is contained to
`libs/mngr_imbue_cloud` plus the root `pyproject.toml` + `test_meta_ratchets.py` (the synthetic `dev` project),
so it needs a changelog entry for `libs/mngr_imbue_cloud` and for `dev`.

## Testing and verification

- **Import ordering:** the new `test_no_import_layer_violations_mngr_imbue_cloud` meta-ratchet enforces the
  contract; run `uv run lint-imports` (or that test) after each phase. Note `lint-imports` will also report the
  pre-existing `mngr`/`minds` contracts as broken -- those are not gated by the ratchets and are out of scope.
- **No-inline-imports / no-`__init__`-code / no-`TYPE_CHECKING` ratchets:** ensure the moved code does not trip
  `test_ratchets.py` (package `__init__.py` files stay empty; no inline imports introduced during the split).
- **Type checking:** `test_no_type_errors` must stay green (run `uv sync --all-packages` first if it reports
  spurious failures after the moves).
- **Behavior:** the existing `mngr_imbue_cloud`, `minds`, and `remote_service_connector` unit/integration test
  suites are the regression guard — a pure refactor must leave them all passing. Run the full `just
  test-offload` before finishing.
- **Changelog:** add one `changelog/<branch>.md` entry per touched project. Phase 1 touches
  `libs/mngr_imbue_cloud`, `apps/minds`, and `apps/remote_service_connector`, so all three need an entry.

## Risks and edge cases

- **Entry-point breakage.** If the `pyproject.toml` entry-point paths are not updated in lockstep with the
  `plugin/` move, the plugin silently fails to load. Verify `mngr imbue_cloud --help` and provider discovery
  after Phase 1. (If a stale `imbue-mngr` tool is installed, the CLAUDE.md `uv tool` refresh may be needed.)
- **Hidden import cycles from the `instance.py` split.** Splitting a 2,000-line module can surface cycles
  (e.g. `listing` <-> `instance`). Resolve by moving shared helpers downward or keeping them together — never
  by adding `TYPE_CHECKING`.
- **`SliceVpsDockerProviderConfig` placement.** Documented above; keep it in `providers/` to avoid inverting
  the layer order. Validate with the contract.
- **External coverage config.** The root `pyproject.toml` `--cov=imbue.mngr_imbue_cloud` flag is package-wide
  and unaffected by sub-packaging; no per-module cov paths to update.
- **Large single commit.** The no-shim approach makes Phase 1 one commit spanning three projects. Mitigate by
  keeping it strictly mechanical (path substitution only, no logic edits) and by the grep + `import-linter` +
  full-suite checks; reviewers can diff with whitespace/path-rename awareness.
- **Missed importer leaves the tree red.** Because there are no shims, any importer not updated in the move
  commit fails immediately. This is intended (it surfaces the problem at once rather than hiding it); the
  pre-commit grep for residual `imbue.mngr_imbue_cloud.<old_path>` references is the guard.
- **Lima socket-path length / runtime behavior.** None of this work changes runtime paths; behavior risk is
  limited to import wiring, which the test suites cover.

## Out of scope (restated)

- Splitting `data_types.py` / `primitives.py` into generic vs slice modules. **Not done.**
- Moving `pool_bake` into minds. **Not done** (only the seam is prepared).
- Any connector, schema, or lease/release behavior change.

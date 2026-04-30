# Repo structure options for mngr + minds

Discussion notes (not a spec). Captures options for restructuring the
monorepo to reduce friction between fast-moving `mngr` (+ plugins) and
the heavier `minds` electron app + its e2e tests.

## Current state

- One uv workspace at the root with `libs/*` and `apps/*` as members;
  `imbue-mngr` is published to PyPI (currently 0.2.5).
- `apps/minds` depends on `imbue-mngr = { workspace = true }` --
  co-versioned via the workspace, not pinned.
- Actual `imbue.mngr.*` import surface from minds is narrow:
  ~17 unique imports, mostly `mngr.api.discovery_events`,
  `mngr.primitives`, and `mngr.utils.{polling,testing,env_utils}`.
- Offload already has three configs (`offload-modal.toml`,
  `-acceptance.toml`, `-release.toml`) with their own cache keys, so CI
  can be partitioned today.
- `forever-claude-template` already vendors mngr at `vendor/mngr/` and
  points `imbue-common` at `vendor/mngr/libs/imbue_common`, so the team
  is familiar with depending on mngr by path.
- Several plugin candidates (cloudflare forwarding, latchkey, host-pool
  client, supertokens auth) currently live in
  `apps/minds/imbue/minds/desktop_client/`.

## Goals

- Changes to mngr or its plugins should not have to wait on the slow
  minds e2e test suite.
- minds should be able to lock to a particular `imbue-mngr` version.
- Cross-cutting refactors (e.g. extending `mngr.api`) should still be
  ergonomic.
- More functionality should move out of minds into mngr plugins so
  minds is a thin UI / orchestration shell.

## Options

### Option 1 -- Split into two repos

- `imbue-ai/mngr`: core + first-party plugins + shared libs
  (`imbue_common`, `concurrency_group`, `resource_guards`).
- `imbue-ai/minds`: minds, minds_workspace_server,
  remote_service_connector, modal_litellm. Depends on a pinned
  `imbue-mngr` from PyPI; uses a `[tool.uv.sources]` override (committed
  commented or env-gated) to point at a local mngr checkout for
  cross-repo dev.

Pros:

- mngr CI never pays the minds tax.
- "Lock minds to a specific mngr version" comes for free.
- Mirrors what `forever-claude-template` already does.

Cons:

- Cross-cutting changes become two PRs and a release.
- Atomic refactors across the stack are harder.
- Tooling (`style_guide.md`, `CLAUDE.md`, ratchets, offload configs,
  blueprint) needs to be shared via submodule, copy + drift, or a
  third "tools" repo.

### Option 2 -- Monorepo, but minds is not a workspace member

- `apps/minds/pyproject.toml` declares its own
  `[tool.uv.workspace]` and depends on `imbue-mngr = "==X.Y.Z"` (pinned
  to PyPI). Add a `[tool.uv.sources]` override for
  `imbue-mngr = { path = "../../libs/mngr", editable = true }`
  guarded behind an opt-in for cross-stack dev.
- The mngr workspace at the root keeps everything except
  `apps/minds*`.
- Path-based CI: a job that only runs minds tests when
  `apps/minds/**` or its declared deps change, and a job that only
  runs mngr tests when `libs/**` changes.

Pros:

- Keeps atomic-commit ergonomics.
- An explicit "bump mngr" PR gates the minds e2e cost.
- Can still pull in mngr changes immediately when wanted.

Cons:

- Two parallel workspaces in one repo is unusual and needs careful
  uv config.
- Version pin requires remembering to bump it.
- Some IDE / type-checker tooling may get confused.

### Option 3 -- Stay one workspace, split CI and slim minds

Don't restructure. Two changes:

1. Move plumbing out of minds into plugins (cloudflare forwarding,
   latchkey gateway, host-pool client, supertokens auth become
   `mngr_cloudflare`, `mngr_latchkey`, `mngr_supertokens` plugins).
   Minds becomes a UI/orchestration shell over mngr + plugins.
2. Path-filter CI: a mngr PR (touching only `libs/mngr*`) doesn't
   trigger minds e2e. Use the existing offload configs.

Pros:

- Least disruption.
- Preserves cross-cutting refactor ergonomics.
- The plugin migration is wanted anyway.

Cons:

- Doesn't give the "minds locks an old mngr version" property -- they
  march in lockstep.
- A mngr API change that breaks minds is only caught when the minds
  e2e finally runs.

### Option 4 (recommended) -- Hybrid

Do Option 3 now; revisit Option 2 once the plugin migration is far
enough along that minds' import surface from mngr is stable.

Concretely, near-term:

- Land path-filtered CI so mngr-only PRs don't run minds e2e.
- Migrate cloudflare / latchkey / host-pool / supertokens into
  plugins. Each plugin is a workspace member alongside `mngr_modal`
  etc., owns its own slice of test cost, and has no
  electron/playwright deps.
- Once minds' imports from mngr are 90% just `imbue.mngr.api` +
  `imbue.mngr.primitives`, freeze that surface as the public mngr API
  and consider removing minds from the workspace (Option 2).

The split-into-two-repos move (Option 1) becomes the right call only if
minds starts shipping on a meaningfully different cadence than mngr, or
if license / open-source posture diverges (mngr is MIT, minds is
`FCL-1.0-MIT`).

## Concrete first steps

1. Add path filters to CI so `apps/minds/**` changes invoke
   `offload-modal-acceptance.toml`-style runs and other paths don't.
2. Pick the lowest-coupling minds extraction -- probably
   `cloudflare_client.py` + `tunnel_token_store.py` -- and make it a
   plugin (`libs/mngr_cloudflare/`) that exposes the same surface
   minds uses today.
3. Re-evaluate after that lands: how big is minds' mngr surface? If
   still narrow, Option 2 is cheap; if exploding, Option 3 is the
   right steady state.

## Open questions / assumptions

- Assumed minds e2e cost dominates the slow tests. If acceptance /
  release tests for mngr itself are also expensive, path filters
  alone won't fix the speed issue -- splitting offload runs by package
  would also help.
- Assumed open-source alignment isn't a driver. mngr is MIT, minds has
  an `FCL-1.0-MIT` license -- they coexist in one repo today, but if
  they diverge further, splitting helps.
- Did not deeply investigate the electron build's contribution to PR
  cost. If `pnpm` / electron tooling is what's slow, that's a separate
  optimization (build only when `electron/**` changes).
- Did not audit how much minds relies on mngr test fixtures vs runtime.
  If test fixtures are a big surface, those should probably move into a
  `mngr_testing` package that minds can pin separately from
  `imbue-mngr` itself.

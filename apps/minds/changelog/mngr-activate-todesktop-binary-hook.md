- `apps/minds`: activate the ToDesktop `beforeInstall` hook so the build
  server re-downloads/re-resolves `uv` and `git` for its target platform
  rather than using the bytes uploaded from the developer's machine.
  Wires `package.json`'s `todesktop:beforeInstall` to
  `./scripts/download-binaries.js`, and restores the `downloadUv()`
  orchestrator in that file (it had been removed in the bundled-git
  carve-out because it was dormant without this PR's hook wiring).
- `apps/minds`: pin both `pnpm` and `node` via ToDesktop's first-class
  `pnpmVersion` / `nodeVersion` config fields, sourcing the literal
  values from `package.json`'s `engines` block (which #1710 already
  pins to `pnpm 10.33.4` and `node 24.15.0`). To make this work,
  `todesktop.json` is replaced with a `todesktop.js` that does
  `require('./package.json')` and reads `engines.pnpm` and
  `engines.node` into the `pnpmVersion` and `nodeVersion` ToDesktop
  config fields; ToDesktop's CLI supports `.json`, `.js`, and `.ts`
  config formats. Net effect: `package.json` is now the single source
  of truth for the pnpm + node versions used on dev laptops (via
  `engines` + `.nvmrc`), in imbue CI (via the workflow's explicit
  installs, still a separate pin), and on ToDesktop's runner (via
  `todesktop.js` reading `package.json`). Replaces a draft of this
  PR that had a home-rolled `installPnpm()` fallback ladder
  (~80 LoC + a 14-line rationale comment) -- ToDesktop's runtime
  already provisions the requested versions before installing
  dependencies, so the ladder was working around the absence of a
  knob that isn't absent. Empirically verified end-to-end against a
  draft ToDesktop build from `wz/minds_onboard` (build
  `260528yf2ma2jd4`) with the earlier `"pnpmVersion": "10.33.4"`
  spelling: both Linux and Mac arm64 finished, packaged binary
  launches and round-trips a first message E2E. The `beforeInstall`
  hook stays for `uv` + `git` (no first-class ToDesktop knob).
  `apps/minds/scripts/build_test.py` (which reads the ToDesktop config
  to assert the limactl signing contract) now shells out to `node -e
  "console.log(JSON.stringify(require('./todesktop.js')))"`. It
  module-level-skips via `pytest.mark.skipif(shutil.which('node') is
  None, ...)` when no node is on PATH -- matches the existing
  `mngr_latchkey` precedent for Node-dependent Python tests. Coverage
  gap: this test currently doesn't run in the offload sandbox (no
  node there). Adding node to the offload image -- or to a
  minds-specific sandbox image -- is a follow-up.
- `apps/minds`: consolidate `downloadUv` into a single definition in
  `scripts/download-binaries.js` and import it into `scripts/build.js`,
  mirroring how `downloadGit` and `download` are already shared.
  Removes the duplicated `UV_VERSION` constant, `getUvDownloadUrl`,
  and `downloadUv` from `build.js`. Both call sites (local
  `pnpm build` and ToDesktop's `beforeInstall` hook) now run the same
  implementation against their own resources directory.

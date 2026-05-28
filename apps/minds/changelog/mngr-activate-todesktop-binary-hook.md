- `apps/minds`: activate the ToDesktop `beforeInstall` hook so the build
  server re-downloads/re-resolves `uv` and `git` for its target platform
  rather than using the bytes uploaded from the developer's machine.
  Wires `package.json`'s `todesktop:beforeInstall` to
  `./scripts/download-binaries.js`, and restores the `downloadUv()`
  orchestrator in that file (it had been removed in the bundled-git
  carve-out because it was dormant without this PR's hook wiring).
- `apps/minds`: pin `pnpm` via ToDesktop's first-class `pnpmVersion`
  config field instead of a home-rolled install ladder. ToDesktop's
  build server provisions the requested `pnpmVersion` (and
  `nodeVersion` / `npmVersion`) before installing dependencies; setting
  `"pnpmVersion": "10.33.4"` in `todesktop.json` is enough to keep
  ToDesktop's CI off pnpm 11.1.0 (which crashes on the Linux runner's
  Node 20.20.0 with `ERR_UNKNOWN_BUILTIN_MODULE: node:sqlite`).
  Empirically verified against a draft ToDesktop build from
  `wz/minds_onboard` (build `260528yf2ma2jd4`): both Linux and Mac
  arm64 finished, packaged binary launches and round-trips a first
  message end-to-end. The `beforeInstall` hook stays for `uv` + `git`
  (no first-class ToDesktop knob); the four-strategy `installPnpm()`
  ladder, its helpers, and the `PNPM_VERSION` constant are gone.

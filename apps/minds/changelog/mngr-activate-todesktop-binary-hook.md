- `apps/minds`: activate the ToDesktop `beforeInstall` hook so the build
  server re-downloads/re-resolves `uv` and `git` for its target platform
  rather than using the bytes uploaded from the developer's machine.
  Pins `pnpm 10.33.4` into the build runner's PATH first via several
  fallback strategies, working around ToDesktop's `npx pnpm@latest`
  resolving to 11.1.0 (which requires Node >=22.13 and crashes on their
  Node 20 Linux runner). Restores the uv / pnpm helpers in
  `scripts/download-binaries.js` that the git-extract PR left out, and
  wires `package.json`'s `todesktop:beforeInstall` to invoke them.
  Removes the build-host dependency on local uv version + Xcode CLT
  layout; bytes shipped to users come from the ToDesktop runner from now
  on.

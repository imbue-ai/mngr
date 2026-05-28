- `apps/minds`: bundle the real macOS `git` binary plus its `libexec/git-core`
  helpers instead of the xcode-select shim. The previous inline `downloadGit()`
  in `scripts/build.js` ran `which git`, which on macOS returns the 118 KB
  `/usr/bin/git` shim -- a launcher that re-invokes the real git from
  `/Library/Developer/CommandLineTools/`. Bundling the shim into a sandboxed
  packaged app meant runtime `git clone` SIGKILLs on any Mac without Xcode CLT
  installed at the expected path. The new `scripts/download-binaries.js`
  resolves the real binary via `xcrun --find git`, copies it plus its
  `libexec/git-core` helpers and templates, and SHA256-verifies all
  downloaded archives. Also bumps the ToDesktop `uploadSizeLimit` from 300 to
  600 because the real binary plus its libexec push the bundle over the
  previous limit.

# Uncertainties

Conflicts between documentation and code, noticed while writing specs; resolve and delete entries as they are fixed.

- 2026-07-06 (`specs/minds-managed-git/concise.md`): `apps/minds/docs/desktop-app.md` conflicts with `apps/minds/scripts/download-binaries.js` about the bundled git. The doc (Bundled binaries section) says git is "currently copied from the build machine; a statically-linked distribution should be used for production", and the "Building for distribution" section claims "the current `cp $(which git)` skips `libexec/git-core/`". The code has since improved: on macOS it resolves the real CLT git via `xcrun --find git` and copies `libexec/git-core/` plus templates (the libexec claim is only still true of the unshipped Linux branch). Assumption made: the code is authoritative; the spec proposes replacing this path entirely and updating the doc as part of implementation.

Fixed the Intel (x86_64) desktop build, which previously bundled arm64 native
helpers (`uv`, `lima`, `restic`, `desync`) into every build and so failed to
launch on Intel Macs.

Root cause: the bundled helpers were downloaded for the *build host's* arch,
never the *target* arch. `build.js` and the `beforeInstall` hook both keyed off
`process.arch`, and `beforeInstall` receives no target-arch parameter, so the
arm64 build machine's binaries ended up in the arm64, x64, and universal
builds alike.

The native-helper download now honors an explicit target arch and runs from a
new `afterPack` hook (which does receive the build's `arch`): each per-arch
build stages its own arch's helpers, and the universal build is lipo-merged
from the x64 and arm64 builds by `@electron/universal`. The downloaders also
support `arch: 'universal'` (lipo of both slices) as a fallback.

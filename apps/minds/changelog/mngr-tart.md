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

Also fixed the packaged Python environment failing to set up on Intel Macs.
`cbor2` 6.x (pulled in via `modal`) and `cryptography` 49.x dropped their macOS
x86_64 wheels (arm64-only), so during env-setup on an Intel Mac `uv sync` fell
back to a Rust source build, which a normal user machine can't do -- setup
failed with "Backend exited before emitting login URL" and the app never came
up. The packaged env now caps `cbor2<6` and `cryptography<49` (the last
Intel-friendly releases; no dependency requires the newer ones), keeping
env-setup wheel-only on x86_64. A new `build_test.py` guard
(`test_lock_has_no_intel_missing_wheels`) fails the build if any dependency
again ships only a macOS arm64 wheel.

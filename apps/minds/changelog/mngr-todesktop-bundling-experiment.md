The macOS download drops from 415MB to 296MB and the installed app from 1.1GB to 792MB, with no change to what the app can do.

Every release since ToDesktop packaging began carried a second copy of `resources/` inside `app.asar` -- 304.9MB, none of it reachable, because `paths.getResourcesDir()` resolves `process.resourcesPath` and only `extraResources` fills that. 267.3MB of the copy was worse than dead: the `todesktop:beforeInstall` hook re-downloaded the binaries on ToDesktop's build server, which is x86_64, so an arm64 app shipped Intel `git`, `uv`, `restic`, and `desync` that could not have run even if something had looked for them.

The hook is gone. `appFiles` now excludes `resources/` wholesale instead of enumerating subtrees, and `mac.additionalBinariesToSign` is dropped.

The signing list turned out to be doing nothing. ToDesktop deep-signs every Mach-O under `Contents/Resources` with `mac.entitlements` whether or not it is listed -- verified against a build that declares no list, where all 42 ship with `Developer ID Application: Imbue, Inc. (LDDYAR29MP)`, the full entitlement set including `limactl`'s `com.apple.security.virtualization`, and a stapled notarization ticket. But every entry in that list had to stay in the `appFiles` upload or the builder's signing preflight failed, which is exactly what forced the subtree-by-subtree exclusions and kept lima duplicated in the asar.

The ToDesktop upload also drops from 546.6MB to 443.0MB against its 650MB limit.

The macOS app icon is flat again.

macOS 26 treats a flat `.icns` as artwork to light: it separates the cream figure from the brown plate and applies the Liquid Glass material, so the redrawn icon arrived in the Dock bevelled, glossy and shaded top to bottom, nothing like the flat tile the brand draws. No build setting caused this and none could turn it off -- the shipped artwork was pixel-uniform, and the previous icon escaped only because the detail inside its head defeated the system's segmentation.

macOS now gets `electron/assets/icon.icon`, an Icon Composer package that states the material instead of leaving it to be guessed: the figure as its own layer with `specular: false`, `glass: false`, no shadow and no translucency. The system still draws the squircle and lights the plate's edge, as it does for every icon on the platform, but it no longer embosses the figure. Linux and the dev dock icon are unchanged.

Building the macOS app now needs Xcode 26 or newer on the build machine, which is what compiles the package.

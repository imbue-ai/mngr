const pkg = require('./package.json');
const { DEEPLINK_SCHEMES } = require('./electron/deeplink');
const { PRODUCT_DISPLAY_NAME } = require('./electron/product-name');

module.exports = {
  schemaVersion: 1,
  id: '26032588hqdzk',
  // The file-system name (Imbue Studio.app, /opt/Imbue Studio): passed
  // explicitly so the dashboard's app name can never override package.json.
  productName: pkg.productName,
  // Registers imbue-studio:// as this app's URL scheme (CFBundleURLTypes on
  // macOS), plus the previous scheme while links in the wild still use it.
  // Runtime handling lives in electron/main.js (handleDeeplink).
  // CLEANUP: drop 'minds' once two stable releases have shipped with both
  // schemes (specs/imbue-studio-rename/05_cleanup.md).
  appProtocolScheme: [...DEEPLINK_SCHEMES],
  icon: './electron/assets/icon.png',
  appPath: '.',
  // `extraResources` is the only channel that reaches the shipped app: it
  // fills the packaged resources dir. Anything matching `appFiles` is packed
  // into app.asar instead, which nothing reads at runtime, so resources/ is
  // excluded wholesale. The app-files glob also strips **/node_modules at any
  // depth, so it could not carry resources/latchkey's nested ones even if it
  // were the delivery channel. scripts/build.js estimates the upload and fails
  // the build when it approaches uploadSizeLimit.
  appFiles: ['**', '!resources/**'],
  // One upload carries every target's tools (see scripts/build.js), which is
  // what the estimate in build.js is held against.
  uploadSizeLimit: 1100,
  nodeVersion: pkg.engines.node,
  pnpmVersion: pkg.engines.pnpm,
  // One complete resources tree per shipped target (see scripts/build.js and
  // scripts/download-binaries.js SHIPPED_TARGETS), each landing at the root
  // of the packaged resources dir, which is what paths.getResourcesDir()
  // reads. Per-target lists rather than a base list with a Linux
  // platformOverride: ToDesktop shipped the base list's bytes to Linux under
  // every platformOverrides shape tried (see specs/minds-linux-packaging).
  // ToDesktop pairs the lists by `to` plus the source directory's name, so
  // every source is a directory named `payload` and only its parent differs.
  // Every architecture the ToDesktop dashboard enables needs its list here.
  targetOverrides: {
    mac: {
      arm64: { extraResources: [{ from: 'resources/darwin-arm64/payload/', to: '.' }] },
    },
    linux: {
      x64: { extraResources: [{ from: 'resources/linux-x64/payload/', to: '.' }] },
    },
  },
  // Pinned rather than ToDesktop's default because `linux.noSandbox` only
  // reaches the AppImage launcher through app-builder-lib 26.9.0 or later,
  // and because a pin makes every build reproducible. The same version builds
  // the macOS app, which minds-launch-to-msg.yml verifies.
  appBuilderLibVersion: '26.15.7',
  // No `mac.additionalBinariesToSign`: ToDesktop deep-signs every Mach-O
  // under Contents/Resources with this plist regardless of that list, and
  // each entry would have to stay in the appFiles upload -- the builder's
  // signing preflight rejects a listed path that is missing -- putting a
  // second copy of its subtree in app.asar.
  mac: {
    entitlements: 'entitlements.mac.plist',
    // Finder, the Dock and Get Info read this. CFBundleName is deliberately
    // absent so it defaults to productName: Electron resolves the helper apps
    // as `<CFBundleName> Helper.app` while the builder names them from
    // productName, and any override lets the two drift apart again.
    extendInfo: {
      CFBundleDisplayName: PRODUCT_DISPLAY_NAME,
    },
  },
  linux: {
    category: 'Development',
    // Linux desktops draw the icon file as it is -- none of them mask it to a
    // platform shape the way the Dock does -- so Linux gets the full-bleed,
    // squarer cut rather than the macOS one, which the top-level `icon` serves.
    icon: './electron/assets/icon-linux.png',
    // Chromium's sandbox needs either a root-owned setuid helper, which an
    // AppImage's user-owned mount cannot carry, or unprivileged user
    // namespaces, which Ubuntu 24.04 restricts through AppArmor. "probe"
    // passes --no-sandbox only on hosts where namespaces are unavailable to
    // the launcher's shell; a .deb whose own AppArmor profile still makes the
    // sandbox available drops the flag itself (electron/linux-sandbox.js).
    noSandbox: 'probe',
  },
};

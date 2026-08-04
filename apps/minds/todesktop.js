const pkg = require('./package.json');

module.exports = {
  schemaVersion: 1,
  id: '26032588hqdzk',
  // Registers minds:// as this app's URL scheme (CFBundleURLTypes on macOS).
  // Runtime handling lives in electron/main.js (handleDeeplink).
  appProtocolScheme: 'minds',
  icon: './electron/assets/icon.png',
  appPath: '.',
  // `extraResources` is the only channel that reaches the shipped app: it
  // fills Contents/Resources, which paths.getResourcesDir() reads. Anything
  // matching `appFiles` is packed into app.asar instead, which nothing reads
  // at runtime, so resources/ is excluded wholesale. The app-files glob also
  // strips **/node_modules at any depth, so it could not carry resources/
  // latchkey's nested ones even if it were the delivery channel.
  // scripts/build.js estimates the upload and fails the build when it
  // approaches uploadSizeLimit.
  appFiles: ['**', '!resources/**'],
  uploadSizeLimit: 650,
  nodeVersion: pkg.engines.node,
  pnpmVersion: pkg.engines.pnpm,
  extraResources: [{ from: 'resources/', to: '.' }],
  // No `mac.additionalBinariesToSign`: ToDesktop deep-signs every Mach-O
  // under Contents/Resources with this plist regardless of that list, and
  // each entry would have to stay in the appFiles upload -- the builder's
  // signing preflight rejects a listed path that is missing -- putting a
  // second copy of its subtree in app.asar.
  mac: {
    entitlements: 'entitlements.mac.plist',
  },
};

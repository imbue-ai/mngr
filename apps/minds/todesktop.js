const pkg = require('./package.json');

module.exports = {
  schemaVersion: 1,
  id: '26032588hqdzk',
  // Registers minds:// as this app's URL scheme (CFBundleURLTypes on macOS).
  // Runtime handling lives in electron/main.js (handleDeeplink).
  appProtocolScheme: 'minds',
  icon: './electron/assets/icon.png',
  appPath: '.',
  // resources/ already travels whole via `extraResources` below (the only
  // channel that can carry its nested latchkey node_modules -- the app-files
  // glob always strips **/node_modules); excluding the heavy subtrees here
  // keeps the tree from uploading a second time through the app-files glob.
  // The exclusions are enumerated rather than a blanket '!resources/**'
  // because `mac.additionalBinariesToSign` paths must exist in the uploaded
  // app-files tree (the builder's signing preflight rejects missing entries,
  // and nothing recreates lima cloud-side), so subtrees holding a signed
  // binary stay in. scripts/build.js estimates the resulting upload and
  // fails the build when it approaches uploadSizeLimit.
  appFiles: [
    '**',
    '!resources/git/**',
    '!resources/latchkey/**',
    '!resources/lima/libexec/**',
    '!resources/lima/share/**',
    '!resources/uv/**',
    '!resources/desync/**',
    '!resources/wheels/**',
    '!resources/pyproject/**',
  ],
  uploadSizeLimit: 600,
  nodeVersion: pkg.engines.node,
  pnpmVersion: pkg.engines.pnpm,
  extraResources: [{ from: 'resources/', to: '.' }],
  mac: {
    entitlements: 'entitlements.mac.plist',
    additionalBinariesToSign: [
      'resources/lima/bin/limactl',
      'resources/restic/restic',
    ],
  },
};

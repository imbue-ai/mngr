const pkg = require('./package.json');

module.exports = {
  schemaVersion: 1,
  id: '26032588hqdzk',
  icon: './electron/assets/icon.png',
  appPath: '.',
  // resources/ already travels whole via `extraResources` below -- and that
  // upload section is the only way its nested latchkey node_modules reaches
  // the ToDesktop builder, because the app-files glob always strips
  // **/node_modules. Without these exclusions the resources tree uploads
  // TWICE (app files + extraResources), which is what pushed the app-source
  // upload to 701MB in 2026-07. The exclusions are enumerated rather than a
  // blanket '!resources/**' because `mac.additionalBinariesToSign` paths
  // must exist in the uploaded app-files tree: the builder's signing
  // preflight fails with "The following additionalBinariesToSign are
  // missing" otherwise, and nothing recreates lima cloud-side (the
  // beforeInstall hook fetches only uv/git/restic/desync). resources/
  // subtrees holding a signed binary (lima/bin, restic) therefore stay in
  // the app files; the final app's Resources/ is assembled from the full
  // extraResources copy regardless (shipped apps contain latchkey
  // node_modules, which app files can never carry). scripts/build.js
  // estimates this composition and fails the build when it approaches
  // uploadSizeLimit.
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

const pkg = require('./package.json');

module.exports = {
  schemaVersion: 1,
  id: '26032588hqdzk',
  icon: './electron/assets/icon.png',
  appPath: '.',
  // resources/ already travels whole via `extraResources` below -- and that
  // upload section is the only way its nested latchkey node_modules reaches
  // the ToDesktop builder, because the app-files glob always strips
  // **/node_modules. Without this exclusion the resources tree uploads TWICE
  // (app files + extraResources), which is what pushed the app-source upload
  // to 701MB in 2026-07. scripts/build.js estimates this composition and
  // fails the build when it approaches uploadSizeLimit.
  appFiles: ['**', '!resources/**'],
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

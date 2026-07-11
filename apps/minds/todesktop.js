const pkg = require('./package.json');

module.exports = {
  schemaVersion: 1,
  id: '26032588hqdzk',
  icon: './electron/assets/icon.png',
  appPath: '.',
  uploadSizeLimit: 650,
  nodeVersion: pkg.engines.node,
  pnpmVersion: pkg.engines.pnpm,
  extraResources: [{ from: 'resources/', to: '.' }],
  mac: {
    entitlements: 'entitlements.mac.plist',
    additionalBinariesToSign: [
      'resources/lima/bin/limactl',
      'resources/restic/restic',
      'resources/desync/desync',
    ],
  },
};

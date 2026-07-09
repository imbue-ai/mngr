const pkg = require('./package.json');

module.exports = {
  schemaVersion: 1,
  id: '26032588hqdzk',
  icon: './electron/assets/icon.png',
  appPath: '.',
  uploadSizeLimit: 600,
  nodeVersion: pkg.engines.node,
  pnpmVersion: pkg.engines.pnpm,
  extraResources: [{ from: 'resources/', to: '.' }],
  mac: {
    entitlements: 'entitlements.mac.plist',
    additionalBinariesToSign: [
      'resources/lima/bin/limactl',
      'resources/restic/restic',
      // The bundled qemu-img: a single static-deps binary (built by
      // scripts/build-qemu-payload.sh) linking only system libraries, so it
      // is the only Mach-O in resources/qemu/ to sign.
      'resources/qemu/bin/qemu-img',
    ],
  },
};

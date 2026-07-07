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
      // The bundled qemu-img payload: every shipped Mach-O must be signed for
      // notarization under the hardened runtime. Listed bottom-up (dylib
      // closure first, executable last). This list tracks the closure produced
      // by scripts/build-qemu-payload.sh for QEMU_IMG_VERSION -- regenerate it
      // alongside the SHA pin whenever the qemu version or its Homebrew
      // dependency closure moves.
      'resources/qemu/lib/libcrypto.3.dylib',
      'resources/qemu/lib/libglib-2.0.0.dylib',
      'resources/qemu/lib/libgmp.10.dylib',
      'resources/qemu/lib/libgnutls.30.dylib',
      'resources/qemu/lib/libhogweed.6.11.dylib',
      'resources/qemu/lib/libidn2.0.dylib',
      'resources/qemu/lib/libintl.8.dylib',
      'resources/qemu/lib/libnettle.8.11.dylib',
      'resources/qemu/lib/libp11-kit.0.dylib',
      'resources/qemu/lib/libpcre2-8.0.dylib',
      'resources/qemu/lib/libssh.4.11.0.dylib',
      'resources/qemu/lib/libtasn1.6.dylib',
      'resources/qemu/lib/libunistring.5.dylib',
      'resources/qemu/lib/libzstd.1.5.7.dylib',
      'resources/qemu/bin/qemu-img',
    ],
  },
};

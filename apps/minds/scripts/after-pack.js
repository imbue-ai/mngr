#!/usr/bin/env node
/**
 * ToDesktop `afterPack` hook: stage the TARGET-architecture native helpers
 * (uv, lima, git, restic, desync) into the packed .app's Contents/Resources,
 * after packaging and before code signing.
 *
 * Runs once per target arch on ToDesktop's build server, so each per-arch
 * build carries its own arch's binaries; @electron/universal then lipo-merges
 * the x64 and arm64 builds into the universal one. This is why the earlier
 * `beforeInstall` hook could not fix the arch -- it receives no `arch`, so it
 * only ever staged the build server's host arch.
 *
 * `arch` is the electron-builder Arch enum: x64=1, arm64=3, universal=4.
 */

const fs = require('fs');
const path = require('path');
const download = require('./download-binaries.js');
const { downloadLima } = require('./build.js');

const ARCH_BY_ENUM = { 1: 'x86_64', 3: 'aarch64', 4: 'universal' };

function findAppResources(appOutDir) {
  const app = fs.readdirSync(appOutDir).find((entry) => entry.endsWith('.app'));
  if (!app) {
    throw new Error(`after-pack: no .app bundle found in ${appOutDir}`);
  }
  return path.join(appOutDir, app, 'Contents', 'Resources');
}

module.exports = async function afterPack({ appOutDir, arch }) {
  const archName = ARCH_BY_ENUM[arch];
  if (!archName) {
    throw new Error(`after-pack: unsupported arch enum ${arch} (expected x64=1, arm64=3, universal=4)`);
  }
  const resourcesDir = findAppResources(appOutDir);
  const platform = 'darwin';
  console.log(`[after-pack] staging ${archName} native helpers into ${resourcesDir}`);
  await Promise.all([
    download.downloadUv(resourcesDir, { platform, arch: archName }),
    download.downloadGit(resourcesDir, { platform }),
    download.downloadRestic(resourcesDir, { platform, arch: archName }),
    download.downloadDesync(resourcesDir, { platform, arch: archName }),
    downloadLima(resourcesDir, { platform, arch: archName }),
  ]);
  console.log(`[after-pack] done (${archName}).`);
};

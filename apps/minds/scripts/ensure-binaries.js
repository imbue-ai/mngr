#!/usr/bin/env node
/**
 * Provision the binaries `pnpm start` needs, for dev.
 *
 * Each is fetched once into a shared per-user cache and `resources/<name>` is
 * symlinked at it -- always a symlink, so anything else there is replaced.
 *
 * Symlinks cannot reach a build: `pnpm dist` runs `pnpm build` first, which
 * deletes resources/ and stages real directories.
 */

const fs = require('fs');
const path = require('path');

const {
  BINARIES,
  getProvisionedBinaries,
  getPlatformArch,
  getCacheEntryPath,
  ensureCachedBinary,
} = require('./download-binaries.js');

const ROOT = path.resolve(__dirname, '..');
const RESOURCES = path.join(ROOT, 'resources');

/**
 * Whether `resources/<name>` resolves to this binary's current cache entry.
 *
 * The pinned version is a path segment of that entry, so this comparison is
 * the version check.
 */
function isSatisfied(name, linkPath, cacheEntry) {
  try {
    if (fs.realpathSync(linkPath) !== fs.realpathSync(cacheEntry)) {
      return false;
    }
  } catch {
    return false;
  }
  return fs.existsSync(path.join(linkPath, BINARIES[name].requiredPath));
}

async function main() {
  const platformArch = getPlatformArch();
  const required = getProvisionedBinaries(platformArch).filter((name) => BINARIES[name].usedInDev);

  const missing = required.filter(
    (name) => !isSatisfied(name, path.join(RESOURCES, name), getCacheEntryPath(name, platformArch)),
  );

  if (missing.length === 0) {
    console.log('[ensure-binaries] All bundled binaries present; skipping download.');
    return;
  }

  console.log(`[ensure-binaries] Provisioning: ${missing.join(', ')}`);
  fs.mkdirSync(RESOURCES, { recursive: true });
  for (const name of missing) {
    const cacheEntry = await ensureCachedBinary(name, platformArch);
    const linkPath = path.join(RESOURCES, name);
    fs.rmSync(linkPath, { recursive: true, force: true });
    fs.symlinkSync(cacheEntry, linkPath);
    console.log(`[ensure-binaries] ${name} -> ${cacheEntry}`);
  }
}

main().catch((err) => {
  console.error('[ensure-binaries] Failed:', err);
  process.exit(1);
});

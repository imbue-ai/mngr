#!/usr/bin/env node
/**
 * Lazy wrapper around scripts/download-binaries.js for `pnpm start`.
 *
 * download-binaries.js always re-downloads (the build path wants a
 * clean slate). For dev mode we only want to download what's missing,
 * so re-launching minds with `pnpm start` doesn't pay ~30MB of network
 * every time. Check each expected output path; only invoke the full
 * downloader when at least one is absent.
 */

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const RESOURCES = path.join(ROOT, 'resources');

// Each entry is a path that must exist (post-download) for the bundle to
// be considered complete. Mirror this with whatever bin/ paths the build
// produces -- keep in sync with build.js + download-binaries.js outputs.
const REQUIRED = [
  path.join(RESOURCES, 'restic', 'restic'),
  path.join(RESOURCES, 'uv', 'uv'),
  path.join(RESOURCES, 'git', 'bin', 'git'),
  path.join(RESOURCES, 'lima', 'bin', 'limactl'),
  path.join(RESOURCES, 'desync', 'desync'),
  // The dispatch curl the latchkey gateway runs as LATCHKEY_CURL. Only
  // fetched on platforms datalib builds it for (macOS arm64, Linux x86_64);
  // on others downloadLatchkeyCurl no-ops and this path stays absent, so
  // guard membership on the platform to avoid a perpetual re-download loop.
  ...((process.platform === 'darwin' && process.arch === 'arm64') ||
  (process.platform === 'linux' && process.arch === 'x64')
    ? [path.join(RESOURCES, 'curl', 'latchkey-curl-dispatch')]
    : []),
];

const missing = REQUIRED.filter((p) => !fs.existsSync(p));
if (missing.length === 0) {
  console.log('[ensure-binaries] All bundled binaries present; skipping download.');
  process.exit(0);
}

console.log(
  '[ensure-binaries] Missing bundled binaries:\n  ' +
    missing.join('\n  ') +
    '\n[ensure-binaries] Running scripts/download-binaries.js...'
);
execFileSync(process.execPath, [path.join(__dirname, 'download-binaries.js')], { stdio: 'inherit' });

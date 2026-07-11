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
];

// Requiring a path the downloader deliberately skips would leave it missing forever
// and re-trigger the full download on every start, so mirror the skips exactly:
// downloadDesync and downloadQemuImg both bail on win32 (no Lima launch mode), and
// downloadQemuImg additionally bails on darwin-x86_64 (no payload published).
const IS_WIN32 = process.platform === 'win32';
const IS_DARWIN_X64 = process.platform === 'darwin' && process.arch === 'x64';
if (!IS_WIN32) {
  REQUIRED.push(path.join(RESOURCES, 'desync', 'desync'));
}
if (!IS_WIN32 && !IS_DARWIN_X64) {
  REQUIRED.push(path.join(RESOURCES, 'qemu', 'bin', 'qemu-img'));
}

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

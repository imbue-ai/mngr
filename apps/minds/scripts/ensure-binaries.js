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

// Pins shared with the downloader (requiring it is side-effect free: it only
// runs when invoked as the main module).
const {
  DATALIB_CURL_VERSION,
  DATALIB_CURL_VERSION_MARKER,
} = require('./download-binaries.js');

// Platforms downloadLatchkeyCurl actually installs the dispatch curl (and the
// impersonator it fronts) on. Every check below that could report the bundled
// curl as missing must mirror this, or on a platform the downloader skips the
// report would stand after the download and re-trigger it on every start.
const IS_LATCHKEY_CURL_BUNDLED =
  (process.platform === 'darwin' && process.arch === 'arm64') ||
  (process.platform === 'linux' && process.arch === 'x64');

// Each entry is a path that must exist (post-download) for the bundle to
// be considered complete. Mirror this with whatever bin/ paths the build
// produces -- keep in sync with build.js + download-binaries.js outputs.
const REQUIRED = [
  path.join(RESOURCES, 'restic', 'restic'),
  path.join(RESOURCES, 'uv', 'uv'),
  path.join(RESOURCES, 'git', 'bin', 'git'),
  path.join(RESOURCES, 'lima', 'bin', 'limactl'),
  // The dispatch curl the latchkey gateway runs as LATCHKEY_CURL.
  ...(IS_LATCHKEY_CURL_BUNDLED ? [path.join(RESOURCES, 'curl', 'latchkey-curl-dispatch')] : []),
];

// Requiring a path the downloader deliberately skips would leave it missing forever
// and re-trigger the full download on every start, so mirror the skip exactly:
// downloadDesync bails on win32 (no Lima launch mode).
const IS_WIN32 = process.platform === 'win32';
if (!IS_WIN32) {
  REQUIRED.push(path.join(RESOURCES, 'desync', 'desync'));
}

const missing = REQUIRED.filter((p) => !fs.existsSync(p));

// The bundled git payload is pinned by scripts/git-manifest.json. A dev machine
// carrying a stale payload passes the existence check above but must still be
// replaced, so treat a missing or mismatched .dugite-tag marker as a missing
// binary.
const gitDir = path.join(RESOURCES, 'git');
const gitTagMarker = path.join(gitDir, '.dugite-tag');
const expectedGitTag = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'git-manifest.json'), 'utf-8')
).dugiteNativeTag;
if (
  fs.existsSync(gitDir) &&
  (!fs.existsSync(gitTagMarker) ||
    fs.readFileSync(gitTagMarker, 'utf-8').trim() !== expectedGitTag)
) {
  missing.push(`${gitDir} (dugite-native tag != ${expectedGitTag})`);
}

// Same story for the bundled curl: its binaries carry no version in their
// names, so a dev machine holding an older datalib release passes the
// existence check above yet must still be replaced -- otherwise a curl bump
// never reaches the dev machine at all.
const curlDir = path.join(RESOURCES, 'curl');
const curlVersionMarker = path.join(curlDir, DATALIB_CURL_VERSION_MARKER);
if (
  IS_LATCHKEY_CURL_BUNDLED &&
  fs.existsSync(curlDir) &&
  (!fs.existsSync(curlVersionMarker) ||
    fs.readFileSync(curlVersionMarker, 'utf-8').trim() !== DATALIB_CURL_VERSION)
) {
  missing.push(`${curlDir} (datalib curl release != ${DATALIB_CURL_VERSION})`);
}

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

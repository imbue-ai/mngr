/**
 * Bundle the platform-specific uv and git binaries into
 * `<resourcesDir>/{uv,git}/`. Used in two contexts:
 * - `pnpm build` locally (binaries for the current machine).
 * - ToDesktop's `beforeInstall` hook on the build server (re-downloads for
 *   the runner's platform, replacing developer-machine bytes).
 *
 * uv:  SHA256-verified download from astral-sh/uv releases.
 * git:
 *   macOS/Linux: SHA256-verified dugite-native tarball (GitHub Desktop's
 *            relocatable git distribution), pinned by git-manifest.json and
 *            extracted flat into resources/git/. The self-contained payload
 *            bakes in an empty prefix, so the runtime must export
 *            GIT_EXEC_PATH/GIT_TEMPLATE_DIR/GIT_CONFIG_SYSTEM (+GIT_SSL_CAINFO
 *            on Linux); see specs/minds-managed-git/concise.md.
 *   Windows: SHA256-verified MinGit download from git-for-windows releases.
 */

const fs = require('fs');
const path = require('path');
const https = require('https');
const http = require('http');
const crypto = require('crypto');
const { execSync } = require('child_process');

const UV_VERSION = '0.11.15';
const GIT_FOR_WINDOWS_VERSION = '2.49.0';
const GIT_FOR_WINDOWS_TAG = `v${GIT_FOR_WINDOWS_VERSION}.windows.1`;
const RESTIC_VERSION = '0.18.1';
// Pinned dugite-native git payload (macOS/Linux). Single source of truth for
// the tag, version, per-target asset names, and SHA256 hashes.
const GIT_MANIFEST_PATH = path.join(__dirname, 'git-manifest.json');
// desync: content-defined-chunking client used to fetch the pre-baked Lima image.
// Only bundled on macOS/Linux (the Lima launch mode's platforms).
const DESYNC_VERSION = '1.0.3';

// datalib "curl" distribution: the dispatch curl + the Chrome-impersonating
// curl it fronts (see the `curl-<triple>.tar.gz` release asset).
// The latchkey gateway runs the dispatch curl as its LATCHKEY_CURL so marked
// requests get Chrome TLS impersonation. Only macOS arm64 and Linux x86_64
// (glibc) are bundled -- datalib publishes no x86_64-apple-darwin, and there
// is no impersonation on Windows.
//
// When bumping DATALIB_CURL_VERSION, update the `curl-*` hashes in
// EXPECTED_SHA256 to match (the tarball filename is version-less, so the
// old hash would otherwise be checked against the new bytes and fail).
const DATALIB_REPO = 'imbue-ai/datalib';
const DATALIB_CURL_VERSION = 'v0.22.0';

/**
 * SHA256 hashes for each downloaded archive, pinned by filename.
 *
 * Sources:
 * - uv: https://github.com/astral-sh/uv/releases/download/<version>/<file>.sha256
 * - MinGit: https://github.com/git-for-windows/git/releases/tag/<tag> release notes
 * - restic: https://github.com/restic/restic/releases/download/v<version>/SHA256SUMS
 * - desync: https://github.com/folbricht/desync/releases/tag/v<version>
 *
 * Update this map whenever UV_VERSION, GIT_FOR_WINDOWS_VERSION,
 * RESTIC_VERSION, or DESYNC_VERSION changes. If a download hash doesn't
 * match an entry here, the script aborts before extracting or executing any
 * downloaded bytes.
 */
const EXPECTED_SHA256 = {
  'uv-aarch64-apple-darwin.tar.gz':     '7e5b336108f8576eda1939920ca0a805b4a9a3c3d3eb2f6140e38b7092fbe4f3',
  'uv-x86_64-apple-darwin.tar.gz':      '42bca7cc879d117ed7139a0e26de8cab0b6f033ad439a32144f324d1f8580d8c',
  'uv-x86_64-unknown-linux-gnu.tar.gz': 'b03e572f010bea94a4a52d42671ba72981e12894f71576181a1d26ff68546da7',
  'uv-x86_64-pc-windows-msvc.zip':      '04b98d414a9000e25e5e0e7c9f53749e66b790cdaffc582829e6f58c544ee11c',
  'MinGit-2.49.0-64-bit.zip':           '971cdee7c0feaa1e41369c46da88d1000a24e79a6f50191c820100338fb7eca5',
  'restic_0.18.1_darwin_arm64.bz2':     '193fccc8bb4567b498923bc70261e104ff22be88016f0f108b035dad372ab711',
  'restic_0.18.1_darwin_amd64.bz2':     'eb8543ed92ff1ddb67762daebf09f7bea4b0c37d21edb6a910bee3d4f514015f',
  'restic_0.18.1_linux_amd64.bz2':      '680838f19d67151adba227e1570cdd8af12c19cf1735783ed1ba928bc41f363d',
  'restic_0.18.1_windows_amd64.zip':    '0c1a713440578cb400d2e76208feb24f1b339426b075a21f73b6b2132692515d',
  'desync_1.0.3_darwin_arm64.tar.gz':   'd3082017b9f12d8716aa1fb4b33f80a4e781305971508db45bf777fc110a657d',
  'desync_1.0.3_darwin_amd64.tar.gz':   'ab029448074428dc757d2235109dd557e9f34e4865052432a6ea7c431f0a5a19',
  'desync_1.0.3_linux_amd64.tar.gz':    'ad4dd9e91b57eef8627d2038df09281d7f38dca02eeca0e66592b54087619953',
  'desync_1.0.3_linux_arm64.tar.gz':    '9008e297f527634efe94688f67c7a49a534c561bf43d223e50f64bec899c15ca',
  // From the datalib release named by DATALIB_CURL_VERSION
  // (`curl-<triple>.tar.gz.sha256`).
  'curl-aarch64-apple-darwin.tar.gz':      '6d03bed2b15005766df8af3362dc6489690552718b4f4f77644dc80e09a6d0b9',
  'curl-x86_64-unknown-linux-gnu.tar.gz':  '6f881600d3d56d7033c7e12906bc3146e233bcdd14b458061e52f7111cf7a9eb',
};

const MAX_REDIRECTS = 5;
const DOWNLOAD_RETRIES = 3;

function getPlatformArch() {
  const platform = process.platform;
  const arch = process.arch;

  if (platform === 'darwin' && arch === 'arm64') return { platform: 'darwin', arch: 'aarch64' };
  if (platform === 'darwin' && arch === 'x64') return { platform: 'darwin', arch: 'x86_64' };
  if (platform === 'linux' && arch === 'x64') return { platform: 'linux', arch: 'x86_64' };
  if (platform === 'win32' && arch === 'x64') return { platform: 'win32', arch: 'x86_64' };
  throw new Error(`Unsupported platform/arch: ${platform}/${arch}`);
}

function getUvDownloadUrl({ platform, arch }) {
  if (platform === 'win32') {
    return `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-pc-windows-msvc.zip`;
  }
  const target = platform === 'darwin'
    ? `uv-${arch}-apple-darwin`
    : `uv-${arch}-unknown-linux-gnu`;
  return `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${target}.tar.gz`;
}

function getResticDownloadUrl({ platform, arch }) {
  // restic ships per-platform archives at
  // restic_<ver>_<os>_<arch>.{bz2,zip}; Go-style arch names: amd64/arm64.
  const goArch = arch === 'x86_64' ? 'amd64' : arch === 'aarch64' ? 'arm64' : null;
  if (goArch === null) {
    throw new Error(`Unsupported restic arch: ${arch}`);
  }
  if (platform === 'win32') {
    return `https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_windows_${goArch}.zip`;
  }
  return `https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_${platform}_${goArch}.bz2`;
}

function getDesyncDownloadUrl({ platform, arch }) {
  // desync ships per-platform tar.gz at desync_<ver>_<os>_<goarch>.tar.gz.
  const goArch = arch === 'x86_64' ? 'amd64' : arch === 'aarch64' ? 'arm64' : null;
  if (!goArch) {
    throw new Error(`Unsupported desync arch: ${arch}`);
  }
  return `https://github.com/folbricht/desync/releases/download/v${DESYNC_VERSION}/desync_${DESYNC_VERSION}_${platform}_${goArch}.tar.gz`;
}

/**
 * Map the current platform/arch to the datalib "curl" release tarball, or
 * null when datalib publishes no build for it (macOS x86_64, Windows). The
 * tarball filename is version-less (stable `releases/download/<tag>/<file>`
 * URLs); the inner dir carries the version.
 */
function getLatchkeyCurlDownloadInfo({ platform, arch }) {
  let triple = null;
  if (platform === 'darwin' && arch === 'aarch64') {
    triple = 'aarch64-apple-darwin';
  } else if (platform === 'linux' && arch === 'x86_64') {
    triple = 'x86_64-unknown-linux-gnu';
  }
  if (triple === null) {
    return null;
  }
  const filename = `curl-${triple}.tar.gz`;
  return {
    filename,
    url: `https://github.com/${DATALIB_REPO}/releases/download/${DATALIB_CURL_VERSION}/${filename}`,
  };
}

/**
 * Download a URL to an in-memory buffer, following up to MAX_REDIRECTS
 * redirects. Throws if the chain is too long or the final response is non-2xx.
 */
function downloadOnce(url, redirectsRemaining = MAX_REDIRECTS) {
  return new Promise((resolve, reject) => {
    const client = url.startsWith('https') ? https : http;
    client.get(url, { headers: { 'User-Agent': 'minds-build' } }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        if (redirectsRemaining <= 0) {
          const err = new Error(`Too many redirects while fetching ${url}`);
          err.permanent = true;
          reject(err);
          return;
        }
        downloadOnce(res.headers.location, redirectsRemaining - 1).then(resolve).catch(reject);
        return;
      }
      if (res.statusCode !== 200) {
        res.resume();
        const err = new Error(`HTTP ${res.statusCode} for ${url}`);
        err.permanent = true;
        reject(err);
        return;
      }
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => resolve(Buffer.concat(chunks)));
      res.on('error', reject);
    }).on('error', reject);
  });
}

/**
 * Download with exponential-backoff retry. Network blips during ToDesktop
 * build time shouldn't fail the entire build. Errors tagged `permanent`
 * (bad HTTP status, redirect-loop) are raised immediately without retrying.
 */
async function download(url) {
  let lastErr;
  for (let attempt = 1; attempt <= DOWNLOAD_RETRIES; attempt++) {
    try {
      return await downloadOnce(url);
    } catch (err) {
      lastErr = err;
      if (err.permanent) break;
      if (attempt === DOWNLOAD_RETRIES) break;
      const delayMs = 1000 * 2 ** (attempt - 1);
      console.log(`[download-binaries] ${url} failed (attempt ${attempt}/${DOWNLOAD_RETRIES}): ${err.message}. Retrying in ${delayMs}ms...`);
      await new Promise((r) => setTimeout(r, delayMs));
    }
  }
  throw lastErr;
}

/**
 * Verify that `buffer` matches the pinned SHA256 for `filename`. Throws with a
 * clear message if the hash is missing, doesn't match, or if `filename` isn't
 * in the pinned map.
 */
function verifyChecksum(buffer, filename) {
  const expected = EXPECTED_SHA256[filename];
  if (!expected) {
    throw new Error(
      `No pinned SHA256 for ${filename} in EXPECTED_SHA256. ` +
      `Add one before distributing this binary.`,
    );
  }
  const actual = crypto.createHash('sha256').update(buffer).digest('hex');
  if (actual !== expected) {
    throw new Error(
      `SHA256 mismatch for ${filename}:\n  expected ${expected}\n  got      ${actual}\n` +
      `Refusing to install possibly-tampered binary.`,
    );
  }
  console.log(`[download-binaries] ${filename} SHA256 OK`);
}

/**
 * Like verifyChecksum, but the expected hash is supplied explicitly (e.g. from
 * git-manifest.json) rather than looked up in EXPECTED_SHA256.
 */
function verifyExpectedChecksum(buffer, filename, expected) {
  const actual = crypto.createHash('sha256').update(buffer).digest('hex');
  if (actual !== expected) {
    throw new Error(
      `SHA256 mismatch for ${filename}:\n  expected ${expected}\n  got      ${actual}\n` +
      `Refusing to install possibly-tampered binary.`,
    );
  }
  console.log(`[download-binaries] ${filename} SHA256 OK`);
}

async function downloadUv(resourcesDir, { platform, arch }) {
  const uvDir = path.join(resourcesDir, 'uv');
  if (fs.existsSync(uvDir)) fs.rmSync(uvDir, { recursive: true });
  fs.mkdirSync(uvDir, { recursive: true });

  const url = getUvDownloadUrl({ platform, arch });
  const filename = path.basename(new URL(url).pathname);
  console.log(`[download-binaries] Downloading uv from ${url}...`);

  const archive = await download(url);
  verifyChecksum(archive, filename);

  if (platform === 'win32') {
    const zipPath = path.join(uvDir, 'uv.zip');
    fs.writeFileSync(zipPath, archive);
    execSync(`powershell -Command "Expand-Archive -Path '${zipPath}' -DestinationPath '${uvDir}'"`, { stdio: 'inherit' });
    fs.unlinkSync(zipPath);
  } else {
    const tarPath = path.join(uvDir, 'uv.tar.gz');
    fs.writeFileSync(tarPath, archive);
    execSync(`tar xzf "${tarPath}" -C "${uvDir}" --strip-components=1`, { stdio: 'inherit' });
    fs.unlinkSync(tarPath);
  }

  const uvBinary = path.join(uvDir, platform === 'win32' ? 'uv.exe' : 'uv');
  if (!fs.existsSync(uvBinary)) {
    throw new Error(`uv binary not found at ${uvBinary} after extraction`);
  }
  if (platform === 'win32') {
    // The runtime resolves uv as 'uv' (no .exe). Copy so both names work.
    fs.copyFileSync(uvBinary, path.join(uvDir, 'uv'));
  } else {
    fs.chmodSync(uvBinary, 0o755);
  }
  console.log(`[download-binaries] uv installed at ${uvBinary}`);
}

async function downloadRestic(resourcesDir, { platform, arch }) {
  const resticDir = path.join(resourcesDir, 'restic');
  if (fs.existsSync(resticDir)) fs.rmSync(resticDir, { recursive: true });
  fs.mkdirSync(resticDir, { recursive: true });

  const url = getResticDownloadUrl({ platform, arch });
  const filename = path.basename(new URL(url).pathname);
  console.log(`[download-binaries] Downloading restic from ${url}...`);

  const archive = await download(url);
  verifyChecksum(archive, filename);

  const resticName = platform === 'win32' ? 'restic.exe' : 'restic';
  const resticPath = path.join(resticDir, resticName);

  if (platform === 'win32') {
    const zipPath = path.join(resticDir, 'restic.zip');
    fs.writeFileSync(zipPath, archive);
    execSync(`powershell -Command "Expand-Archive -Path '${zipPath}' -DestinationPath '${resticDir}'"`, { stdio: 'inherit' });
    fs.unlinkSync(zipPath);
    // The archive ships the binary as restic_<ver>_windows_<arch>.exe;
    // rename to restic.exe for a stable runtime path.
    const stem = path.basename(filename, '.zip');
    const extractedExe = path.join(resticDir, `${stem}.exe`);
    if (fs.existsSync(extractedExe) && extractedExe !== resticPath) {
      fs.renameSync(extractedExe, resticPath);
    }
    // Runtime resolves restic as 'restic' (no .exe); duplicate so both names work.
    fs.copyFileSync(resticPath, path.join(resticDir, 'restic'));
  } else {
    // restic ships its bz2 as the bare binary (no tar wrapper). bunzip2 is
    // available on macOS (BSD) and Linux by default; we stage the archive
    // to a temp .bz2 and decompress in place.
    const bzPath = path.join(resticDir, 'restic.bz2');
    fs.writeFileSync(bzPath, archive);
    execSync(`bunzip2 -f "${bzPath}"`, { stdio: 'inherit' });
    // bunzip2 strips the .bz2 suffix, leaving resticDir/restic.
    if (!fs.existsSync(resticPath)) {
      throw new Error(`restic binary not found at ${resticPath} after bunzip2`);
    }
    fs.chmodSync(resticPath, 0o755);
  }

  console.log(`[download-binaries] restic installed at ${resticPath}`);
}

async function downloadDesync(resourcesDir, { platform, arch }) {
  // desync supports the Lima launch mode only (macOS/Linux). Windows has no Lima
  // launch mode, so there is nothing to fetch there.
  if (platform === 'win32') {
    console.log('[download-binaries] Skipping desync on win32 (no Lima launch mode).');
    return;
  }
  const desyncDir = path.join(resourcesDir, 'desync');
  if (fs.existsSync(desyncDir)) fs.rmSync(desyncDir, { recursive: true });
  fs.mkdirSync(desyncDir, { recursive: true });

  const url = getDesyncDownloadUrl({ platform, arch });
  const filename = path.basename(new URL(url).pathname);
  console.log(`[download-binaries] Downloading desync from ${url}...`);

  const archive = await download(url);
  verifyChecksum(archive, filename);

  const tarPath = path.join(desyncDir, 'desync.tar.gz');
  fs.writeFileSync(tarPath, archive);
  execSync(`tar xzf "${tarPath}" -C "${desyncDir}"`, { stdio: 'inherit' });
  fs.unlinkSync(tarPath);

  const desyncBinary = path.join(desyncDir, 'desync');
  if (!fs.existsSync(desyncBinary)) {
    throw new Error(`desync binary not found at ${desyncBinary} after extraction`);
  }
  fs.chmodSync(desyncBinary, 0o755);
  console.log(`[download-binaries] desync installed at ${desyncBinary}`);
}

// Maps (platform, arch) as produced by getPlatformArch() to the manifest
// target key. win32 stays on the MinGit path below and is intentionally absent.
const GIT_MANIFEST_TARGET_BY_PLATFORM_ARCH = {
  'darwin/aarch64': 'darwin-arm64',
  'darwin/x86_64': 'darwin-x64',
  'linux/x86_64': 'linux-x64',
  'linux/aarch64': 'linux-arm64',
};

/**
 * Bundle the datalib dispatch curl + Chrome-impersonating curl into
 * `<resourcesDir>/curl/`. The latchkey gateway runs the dispatch curl as
 * its LATCHKEY_CURL (wired in electron/backend.js).
 *
 * No-op with a warning -- rather than a hard failure -- when the platform
 * has no datalib build (macOS x86_64, Windows); in that case latchkey keeps
 * using the system curl. On a supported platform this behaves like the
 * other bundled binaries: hard-fail on a download error or SHA mismatch.
 */
async function downloadLatchkeyCurl(resourcesDir, { platform, arch }) {
  const info = getLatchkeyCurlDownloadInfo({ platform, arch });
  if (info === null) {
    console.log(`[download-binaries] No datalib curl build for ${platform}/${arch}; skipping (latchkey uses system curl).`);
    return;
  }

  const curlDir = path.join(resourcesDir, 'curl');
  if (fs.existsSync(curlDir)) fs.rmSync(curlDir, { recursive: true });
  fs.mkdirSync(curlDir, { recursive: true });

  console.log(`[download-binaries] Downloading latchkey curl from ${info.url}...`);
  const archive = await download(info.url);
  verifyChecksum(archive, info.filename);

  const tarPath = path.join(curlDir, 'curl.tar.gz');
  fs.writeFileSync(tarPath, archive);
  // The tarball is `curl-<version>-<triple>/<two binaries>`;
  // strip the single inner dir so the binaries land directly in curlDir.
  execSync(`tar xzf "${tarPath}" -C "${curlDir}" --strip-components=1`, { stdio: 'inherit' });
  fs.unlinkSync(tarPath);

  for (const name of ['latchkey-curl-dispatch', 'latchkey-curl-impersonate']) {
    const binPath = path.join(curlDir, name);
    if (!fs.existsSync(binPath)) {
      throw new Error(`${name} not found at ${binPath} after extraction`);
    }
    fs.chmodSync(binPath, 0o755);
  }
  console.log(`[download-binaries] latchkey curl (dispatch + impersonator) installed in ${curlDir}`);
}

/**
 * Write resources/git/NOTICE recording the provenance and licenses of the
 * dugite-native payload, generated from the manifest.
 */
function writeGitNotice(gitDir, manifest) {
  const releaseUrl = `https://github.com/desktop/dugite-native/releases/tag/${manifest.dugiteNativeTag}`;
  const gitSourceUrl = `https://github.com/git/git/tree/v${manifest.gitVersion}`;
  const notice =
    `This directory contains the dugite-native ${manifest.dugiteNativeTag} payload\n` +
    `(${releaseUrl}), a relocatable git distribution embedded in this app.\n` +
    `\n` +
    `It bundles:\n` +
    `- git ${manifest.gitVersion} (GPLv2; source ${gitSourceUrl}; license text in\n` +
    `  the adjacent COPYING file)\n` +
    `- git-credential-manager (MIT)\n` +
    `- git-lfs (MIT)\n` +
    `- on Linux, a bundled CA certificate store (ssl/cacert.pem)\n`;
  fs.writeFileSync(path.join(gitDir, 'NOTICE'), notice);
}

/**
 * Render the POSIX-sh shim that replaces one payload symlink. The shim
 * resolves its own physical directory (works when invoked by absolute path,
 * relative path, or bare PATH lookup) and execs the link's former target.
 * When `gitSubcommand` is non-null the target is the multicall `git` binary
 * and the shim uses the documented dashed-form equivalence:
 * `git-<subcommand> args` == `git <subcommand> args`.
 */
function buildShimScript(relativeTargetPath, gitSubcommand) {
  const targetInvocation = gitSubcommand === null
    ? `exec "$shim_dir/${relativeTargetPath}" "$@"`
    : `exec "$shim_dir/${relativeTargetPath}" ${gitSubcommand} "$@"`;
  return [
    '#!/bin/sh',
    '# Generated by minds scripts/download-binaries.js: replaces a dugite-native',
    '# symlink so no packaging step can materialize it into a copy of its target.',
    'case "$0" in',
    '  */*) shim_path="$0" ;;',
    '  *) shim_path="$(command -v -- "$0")" || exit 127 ;;',
    'esac',
    'shim_dir="$(CDPATH= cd -- "$(dirname -- "$shim_path")" && pwd -P)" || exit 127',
    targetInvocation,
    '',
  ].join('\n');
}

/**
 * Replace every symlink in the extracted dugite-native payload with a tiny
 * executable sh shim that execs the link's target.
 *
 * Why: the payload ships libexec/git-core mostly as symlinks to the multicall
 * `git` binary, and the packaging steps between here and the user's disk
 * handle symlinks in two hostile ways: ToDesktop's app-files glob silently
 * DROPS them (the payload would arrive on the build servers missing every
 * builtin, including git-remote-https), while symlink-dereferencing copiers
 * (electron-builder's extraResources copy) materialize a full copy per link.
 * Shims keep the payload behaviorally identical but symlink-free, so it is
 * complete and size-stable no matter how it is copied, zipped, or signed.
 *
 * Dispatch shape:
 * - `git-<subcommand>` -> `git` becomes `exec git <subcommand> "$@"`.
 * - anything else (git-remote-https/ftp/ftps -> git-remote-http) becomes
 *   `exec <target> "$@"`; remote-curl selects the protocol from the URL git
 *   passes as an argument, not from argv[0], so no argv0 trick is needed.
 *
 * Returns the number of symlinks replaced. Throws on dangling links, links
 * escaping the payload, or non-file targets -- all of which would indicate
 * an upstream payload layout change that needs a human look.
 */
function convertGitPayloadSymlinksToShims(gitDir) {
  const payloadRoot = fs.realpathSync(gitDir);
  let shimCount = 0;
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const entryPath = path.join(dir, entry.name);
      if (entry.isSymbolicLink()) {
        const targetPath = fs.realpathSync(entryPath);
        if (targetPath !== payloadRoot && !targetPath.startsWith(payloadRoot + path.sep)) {
          throw new Error(
            `dugite-native payload symlink escapes the payload: ${entryPath} -> ${targetPath}`,
          );
        }
        if (!fs.statSync(targetPath).isFile()) {
          throw new Error(
            `dugite-native payload symlink targets a non-file: ${entryPath} -> ${targetPath}`,
          );
        }
        let gitSubcommand = null;
        if (path.basename(targetPath) === 'git' && entry.name.startsWith('git-')) {
          gitSubcommand = entry.name.slice('git-'.length);
          if (!/^[A-Za-z0-9][A-Za-z0-9-]*$/.test(gitSubcommand)) {
            throw new Error(`unexpected dashed git command name in payload: ${entry.name}`);
          }
        }
        const relativeTargetPath = path.relative(path.dirname(entryPath), targetPath);
        fs.rmSync(entryPath);
        fs.writeFileSync(entryPath, buildShimScript(relativeTargetPath, gitSubcommand));
        fs.chmodSync(entryPath, 0o755);
        shimCount += 1;
      } else if (entry.isDirectory()) {
        walk(entryPath);
      }
    }
  };
  walk(payloadRoot);
  return shimCount;
}

async function downloadGit(resourcesDir, { platform, arch }) {
  const gitDir = path.join(resourcesDir, 'git');
  if (fs.existsSync(gitDir)) fs.rmSync(gitDir, { recursive: true });
  const binDir = path.join(gitDir, 'bin');
  fs.mkdirSync(binDir, { recursive: true });

  if (platform === 'win32') {
    const filename = `MinGit-${GIT_FOR_WINDOWS_VERSION}-64-bit.zip`;
    const url = `https://github.com/git-for-windows/git/releases/download/${GIT_FOR_WINDOWS_TAG}/${filename}`;
    console.log(`[download-binaries] Downloading MinGit from ${url}...`);
    const archive = await download(url);
    verifyChecksum(archive, filename);
    const zipPath = path.join(gitDir, 'mingit.zip');
    fs.writeFileSync(zipPath, archive);
    execSync(`powershell -Command "Expand-Archive -Path '${zipPath}' -DestinationPath '${gitDir}'"`, { stdio: 'inherit' });
    fs.unlinkSync(zipPath);
    // MinGit extracts git.exe under gitDir/cmd, but the runtime expects
    // git under gitDir/bin. Copy into bin so Windows uses the same layout.
    const gitExe = path.join(gitDir, 'cmd', 'git.exe');
    if (!fs.existsSync(gitExe)) {
      throw new Error(`git.exe not found at ${gitExe} after extraction`);
    }
    fs.copyFileSync(gitExe, path.join(binDir, 'git.exe'));
    fs.copyFileSync(gitExe, path.join(binDir, 'git'));
    console.log(`[download-binaries] git installed at ${path.join(binDir, 'git.exe')}`);
    return;
  }

  // macOS/Linux: SHA256-verified dugite-native tarball, pinned by the manifest.
  const manifest = JSON.parse(fs.readFileSync(GIT_MANIFEST_PATH, 'utf-8'));
  const targetKey = GIT_MANIFEST_TARGET_BY_PLATFORM_ARCH[`${platform}/${arch}`];
  const target = targetKey && manifest.targets[targetKey];
  if (!target) {
    throw new Error(
      `No dugite-native manifest entry for ${platform}/${arch}. ` +
      `Add one to ${GIT_MANIFEST_PATH} before distributing this binary.`,
    );
  }

  const url = `https://github.com/desktop/dugite-native/releases/download/${manifest.dugiteNativeTag}/${target.asset}`;
  console.log(`[download-binaries] Downloading git (dugite-native) from ${url}...`);
  const archive = await download(url);
  verifyExpectedChecksum(archive, target.asset, target.sha256);

  // The dugite-native tarball is rooted flat (bin/, etc/, libexec/, share/,
  // and ssl/ on Linux at the archive root), so extract WITHOUT
  // --strip-components, unlike the uv/lima archives.
  const tarPath = path.join(gitDir, 'git.tar.gz');
  fs.writeFileSync(tarPath, archive);
  execSync(`tar xzf "${tarPath}" -C "${gitDir}"`, { stdio: 'inherit' });
  fs.unlinkSync(tarPath);

  const destGit = path.join(binDir, 'git');
  if (!fs.existsSync(destGit)) {
    throw new Error(`git binary not found at ${destGit} after extraction`);
  }
  fs.chmodSync(destGit, 0o755);

  // Replace payload symlinks with shims BEFORE writing the .dugite-tag
  // marker, so a payload that failed conversion is never tagged as complete
  // (ensure-binaries.js would then re-run the downloader).
  const shimCount = convertGitPayloadSymlinksToShims(gitDir);
  console.log(`[download-binaries] replaced ${shimCount} git payload symlinks with shims`);

  // Marker so ensure-binaries.js can replace a stale payload on dev machines.
  fs.writeFileSync(path.join(gitDir, '.dugite-tag'), manifest.dugiteNativeTag + '\n');
  writeGitNotice(gitDir, manifest);
  fs.copyFileSync(path.join(__dirname, 'assets', 'git-COPYING'), path.join(gitDir, 'COPYING'));

  console.log(`[download-binaries] git installed at ${destGit} (dugite-native ${manifest.dugiteNativeTag})`);
}

/**
 * Total size in bytes of the file (or, recursively, directory) at `target`,
 * following nothing further -- used to price a symlink the way an archiver
 * that follows symlinks would.
 */
function sizeOfMaterializedTarget(target) {
  const stats = fs.statSync(target);
  if (stats.isFile()) return stats.size;
  if (!stats.isDirectory()) return 0;
  let total = 0;
  for (const entry of fs.readdirSync(target, { withFileTypes: true })) {
    total += sizeOfMaterializedTarget(path.join(target, entry.name));
  }
  return total;
}

/**
 * Measure a tree the way a symlink-following archiver (ToDesktop's
 * app-source zip) sees it: every symlink counts at its target's full
 * (recursive) size. Dangling symlinks count as zero. Returns
 * `{ realBytes, archivedBytes, symlinkCount }`; `archivedBytes - realBytes`
 * is the inflation that materializing symlinks would add.
 */
function measureTreeAsArchived(rootDir) {
  let realBytes = 0;
  let archivedBytes = 0;
  let symlinkCount = 0;
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const entryPath = path.join(dir, entry.name);
      if (entry.isSymbolicLink()) {
        symlinkCount += 1;
        realBytes += fs.lstatSync(entryPath).size;
        try {
          archivedBytes += sizeOfMaterializedTarget(entryPath);
        } catch {
          // Dangling symlink: an archiver has no bytes to store for it.
        }
      } else if (entry.isDirectory()) {
        walk(entryPath);
      } else if (entry.isFile()) {
        const size = fs.lstatSync(entryPath).size;
        realBytes += size;
        archivedBytes += size;
      }
    }
  };
  walk(rootDir);
  return { realBytes, archivedBytes, symlinkCount };
}

// ToDesktop itself prices symlinks harmlessly (its app-files glob drops
// them; get-folder-size lstats them), but symlink-DEREFERENCING copiers
// downstream -- electron-builder's extraResources copy into the final .app,
// naive cpSync/rsync mirrors -- materialize a full copy per link. Legitimate
// symlinks in resources/ are tiny (lima's share/doc templates dir), so a
// generous threshold keeps false positives out.
const MAX_SYMLINK_INFLATION_BYTES = 64 * 1024 * 1024;

/**
 * Fail the build if `rootDir` would balloon past MAX_SYMLINK_INFLATION_BYTES
 * when copied by anything that dereferences symlinks (see the constant's
 * comment), or if even its real size alone exceeds the whole ToDesktop
 * upload budget. Returns the measurement for logging.
 */
function assertTreeFitsUploadBudget(rootDir, { uploadSizeLimitMb, label }) {
  const measurement = measureTreeAsArchived(rootDir);
  const { realBytes, archivedBytes, symlinkCount } = measurement;
  const inflationBytes = archivedBytes - realBytes;
  const asMb = (bytes) => (bytes / (1024 * 1024)).toFixed(1);
  if (inflationBytes > MAX_SYMLINK_INFLATION_BYTES) {
    throw new Error(
      `${label} contains ${symlinkCount} symlinks that a symlink-dereferencing copier ` +
      `(e.g. electron-builder's extraResources copy into the final app) would materialize ` +
      `into +${asMb(inflationBytes)}MB (${asMb(realBytes)}MB real -> ${asMb(archivedBytes)}MB ` +
      `copied). Replace the offending symlinks with shims ` +
      `(see convertGitPayloadSymlinksToShims) instead of shipping them.`,
    );
  }
  const limitBytes = uploadSizeLimitMb * 1024 * 1024;
  if (realBytes > limitBytes) {
    throw new Error(
      `${label} alone is ${asMb(realBytes)}MB, over the ${uploadSizeLimitMb}MB ` +
      `uploadSizeLimit in todesktop.js -- the ToDesktop upload cannot succeed. ` +
      `Shrink the staged binaries before raising the limit.`,
    );
  }
  return measurement;
}

/**
 * Estimate the ToDesktop app-source upload in bytes, mirroring how
 * @todesktop/cli@1.23 composes it (dist/cli.js, uploadApplicationSource):
 *
 * - App files: every regular file under the app root matching `appFiles`
 *   (default `['**']`), always minus `node_modules` and `.git` at any depth
 *   and minus `.gitignore` files. Symlinks contribute NOTHING here -- the
 *   CLI globs with `followSymbolicLinks: false, onlyFiles: true`, which
 *   drops them. NOTE: gitignored *content* is NOT excluded; only the
 *   `.gitignore` files themselves are.
 * - `extraResources` / `extraContentFiles`: each `from` is uploaded whole
 *   and priced via get-folder-size (lstat, so symlinks at link size),
 *   REGARDLESS of the appFiles globs. This is why resources/ must be
 *   excluded from appFiles or the whole tree uploads twice (the 701MB
 *   launch-to-msg failures, 2026-07).
 * - Icons / entitlements: individual small files; the icon is counted for
 *   completeness, the rest is noise.
 *
 * Only the appFiles shapes this repo uses are supported: a positive `**`
 * plus `!<dir>/**` exclusions. Any other shape throws, so the estimate can
 * never silently diverge from the real zip's selection semantics.
 */
function estimateToDesktopUploadBytes(appRoot, todesktopConfig) {
  const appFilesGlobs = todesktopConfig.appFiles || ['**'];
  const excludedPrefixes = [];
  for (const glob of appFilesGlobs) {
    if (glob === '**') continue;
    const exclusionMatch = /^!([A-Za-z0-9._/-]+)\/\*\*$/.exec(glob);
    if (exclusionMatch) {
      excludedPrefixes.push(exclusionMatch[1] + '/');
      continue;
    }
    throw new Error(
      `estimateToDesktopUploadBytes only understands '**' and '!<dir>/**' appFiles ` +
      `patterns; got ${JSON.stringify(glob)}. Extend the estimator alongside todesktop.js.`,
    );
  }

  let appFilesBytes = 0;
  const walkAppFiles = (dir, relativePrefix) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name === 'node_modules' || entry.name === '.git') continue;
      const relativePath = relativePrefix + entry.name;
      if (entry.isSymbolicLink()) continue;
      if (entry.isDirectory()) {
        if (excludedPrefixes.some((prefix) => (relativePath + '/').startsWith(prefix))) continue;
        walkAppFiles(path.join(dir, entry.name), relativePath + '/');
      } else if (entry.isFile()) {
        if (entry.name === '.gitignore') continue;
        if (excludedPrefixes.some((prefix) => relativePath.startsWith(prefix))) continue;
        appFilesBytes += fs.lstatSync(path.join(dir, entry.name)).size;
      }
    }
  };
  walkAppFiles(appRoot, '');

  let extraBytes = 0;
  const extraEntries = [
    ...(todesktopConfig.extraResources || []),
    ...(todesktopConfig.extraContentFiles || []),
  ];
  for (const { from } of extraEntries) {
    const fromPath = path.resolve(appRoot, from);
    const stats = fs.lstatSync(fromPath);
    extraBytes += stats.isDirectory() ? measureTreeAsArchived(fromPath).realBytes : stats.size;
  }
  if (todesktopConfig.icon) {
    extraBytes += fs.lstatSync(path.resolve(appRoot, todesktopConfig.icon)).size;
  }
  return { appFilesBytes, extraBytes, totalBytes: appFilesBytes + extraBytes };
}

/**
 * Fail the build if the estimated ToDesktop app-source upload exceeds
 * todesktop.js's `uploadSizeLimit`; warn when it consumes most of it.
 * Returns the estimate for logging.
 */
function assertUploadFitsToDesktopLimit(appRoot, todesktopConfig) {
  const uploadSizeLimitMb = todesktopConfig.uploadSizeLimit;
  const estimate = estimateToDesktopUploadBytes(appRoot, todesktopConfig);
  // The CLI compares against uploadSizeLimit * 1e6 (decimal megabytes).
  const limitBytes = uploadSizeLimitMb * 1e6;
  const asMb = (bytes) => (bytes / 1e6).toFixed(1);
  if (estimate.totalBytes > limitBytes) {
    throw new Error(
      `estimated ToDesktop app-source upload is ${asMb(estimate.totalBytes)}MB ` +
      `(app files ${asMb(estimate.appFilesBytes)}MB + extraResources/icon ${asMb(estimate.extraBytes)}MB), ` +
      `over the ${uploadSizeLimitMb}MB uploadSizeLimit in todesktop.js. Trim what uploads ` +
      `(appFiles exclusions, staged binaries) rather than raising the limit.`,
    );
  }
  if (estimate.totalBytes > limitBytes * 0.85) {
    console.warn(
      `[download-binaries] WARNING: estimated ToDesktop upload ${asMb(estimate.totalBytes)}MB is over ` +
      `85% of the ${uploadSizeLimitMb}MB uploadSizeLimit in todesktop.js.`,
    );
  }
  return estimate;
}

/**
 * Download platform-specific binaries into the given resources directory.
 * Can be called directly or from a ToDesktop hook.
 *
 * pnpm and Node are NOT provisioned here -- ToDesktop's `pnpmVersion`
 * and `nodeVersion` fields in `todesktop.js` cover that. This hook only
 * handles binaries ToDesktop has no first-class knob for: `uv`, `git`,
 * `restic` (per-workspace backups), and, for the Lima launch mode on
 * macOS/Linux, `desync` (fetches the pre-baked Lima image).
 */
async function downloadBinaries(resourcesDir) {
  const { platform, arch } = getPlatformArch();
  console.log(`[download-binaries] Platform: ${platform}, Architecture: ${arch}`);

  await Promise.all([
    downloadUv(resourcesDir, { platform, arch }),
    downloadGit(resourcesDir, { platform, arch }),
    downloadRestic(resourcesDir, { platform, arch }),
    downloadDesync(resourcesDir, { platform, arch }),
    downloadLatchkeyCurl(resourcesDir, { platform, arch }),
  ]);

  console.log('[download-binaries] Done.');
}

/**
 * ToDesktop `beforeInstall` hook entry point. Receives { appDir, pkgJsonPath, ... }.
 * Re-downloads binaries for the build server's platform.
 */
async function beforeInstall({ appDir }) {
  const resourcesDir = path.join(appDir, 'resources');
  fs.mkdirSync(resourcesDir, { recursive: true });
  await downloadBinaries(resourcesDir);
}

beforeInstall.downloadGit = downloadGit;
beforeInstall.downloadUv = downloadUv;
beforeInstall.downloadRestic = downloadRestic;
beforeInstall.downloadDesync = downloadDesync;
beforeInstall.downloadLatchkeyCurl = downloadLatchkeyCurl;
beforeInstall.download = download;
beforeInstall.convertGitPayloadSymlinksToShims = convertGitPayloadSymlinksToShims;
beforeInstall.measureTreeAsArchived = measureTreeAsArchived;
beforeInstall.assertTreeFitsUploadBudget = assertTreeFitsUploadBudget;
beforeInstall.estimateToDesktopUploadBytes = estimateToDesktopUploadBytes;
beforeInstall.assertUploadFitsToDesktopLimit = assertUploadFitsToDesktopLimit;
module.exports = beforeInstall;

if (require.main === module) {
  const resourcesDir = process.argv[2] || path.join(path.resolve(__dirname, '..'), 'resources');
  downloadBinaries(resourcesDir).catch((err) => {
    console.error('[download-binaries] Failed:', err);
    process.exit(1);
  });
}

/**
 * Bundle the platform-specific uv and git binaries into
 * `<resourcesDir>/{uv,git}/`. Used in two contexts:
 * - `pnpm build` locally (binaries for the current machine).
 * - ToDesktop's `beforeInstall` hook on the build server (re-downloads for
 *   the runner's platform, replacing developer-machine bytes).
 *
 * uv:  SHA256-verified download from astral-sh/uv releases.
 * git:
 *   macOS:   real binary via `xcrun --find git`, plus libexec/git-core
 *            helpers and templates. `/usr/bin/git` is an xcode-select shim
 *            that SIGKILLs at runtime once re-signed by ToDesktop.
 *   Linux:   copy from `which git`.
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

/**
 * SHA256 hashes for each downloaded archive, pinned by filename.
 *
 * Sources:
 * - uv: https://github.com/astral-sh/uv/releases/download/<version>/<file>.sha256
 * - MinGit: https://github.com/git-for-windows/git/releases/tag/<tag> release notes
 * - restic: https://github.com/restic/restic/releases/download/v<version>/SHA256SUMS
 *
 * Update this map whenever UV_VERSION, GIT_FOR_WINDOWS_VERSION, or
 * RESTIC_VERSION changes. If a download hash doesn't match an entry
 * here, the script aborts before extracting or executing any
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

/**
 * Recursively copy Apple's git libexec tree into destDir, dereferencing
 * symlinks into real file copies.
 *
 * Symlinks pointing back at the main `git` binary (Apple's ~100 argv[0]
 * shims like git-add, git-commit) are skipped -- the invoked-as-subcommand
 * dispatch they enable is unused here, and dereferencing each would add
 * ~7.6 MB per shim. Other symlinks must be dereferenced because Apple's
 * targets are absolute paths into Xcode that break on any machine without
 * Xcode at that exact path, and ToDesktop's Windows packager rejects
 * absolute macOS symlinks.
 */
function copyGitCoreDereferencingSymlinks(srcDir, destDir) {
  for (const entry of fs.readdirSync(srcDir, { withFileTypes: true })) {
    const srcPath = path.join(srcDir, entry.name);
    const destPath = path.join(destDir, entry.name);
    if (entry.isSymbolicLink()) {
      const realTarget = fs.realpathSync(srcPath);
      if (path.basename(realTarget) === 'git') {
        continue; // skip argv[0] shims pointing at the main binary
      }
      const realStats = fs.statSync(realTarget);
      if (realStats.isDirectory()) {
        fs.mkdirSync(destPath, { recursive: true });
        copyGitCoreDereferencingSymlinks(realTarget, destPath);
      } else {
        fs.copyFileSync(realTarget, destPath);
        fs.chmodSync(destPath, realStats.mode);
      }
    } else if (entry.isDirectory()) {
      fs.mkdirSync(destPath, { recursive: true });
      copyGitCoreDereferencingSymlinks(srcPath, destPath);
    } else if (entry.isFile()) {
      fs.copyFileSync(srcPath, destPath);
      fs.chmodSync(destPath, fs.statSync(srcPath).mode);
    }
  }
}

async function downloadGit(resourcesDir, { platform }) {
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
  } else if (platform === 'darwin') {
    // git invokes `git-remote-https` and friends from <prefix>/libexec/git-core/
    // via relative-to-binary lookup, and reads default templates from
    // <prefix>/share/git-core/templates/. Copy the binary, the libexec
    // helpers, and the templates so the bundled git is self-contained.
    let resolvedGit;
    try {
      resolvedGit = execSync('xcrun --find git', { encoding: 'utf-8' }).trim();
    } catch (err) {
      throw new Error(
        'git not resolvable via `xcrun --find git`. Install Xcode Command ' +
        `Line Tools (\`xcode-select --install\`) and retry. Underlying error: ${err.message}`,
        { cause: err },
      );
    }
    if (!resolvedGit || !fs.existsSync(resolvedGit)) {
      throw new Error(`xcrun returned a git path that does not exist: ${resolvedGit}`);
    }
    const gitPrefix = path.dirname(path.dirname(resolvedGit));
    const srcExecPath = path.join(gitPrefix, 'libexec', 'git-core');
    const srcTemplates = path.join(gitPrefix, 'share', 'git-core', 'templates');
    if (!fs.existsSync(srcExecPath)) {
      throw new Error(`git exec-path not found at ${srcExecPath}`);
    }

    const destGit = path.join(binDir, 'git');
    fs.copyFileSync(resolvedGit, destGit);
    fs.chmodSync(destGit, 0o755);

    const destExecPath = path.join(gitDir, 'libexec', 'git-core');
    fs.mkdirSync(destExecPath, { recursive: true });
    copyGitCoreDereferencingSymlinks(srcExecPath, destExecPath);

    if (fs.existsSync(srcTemplates)) {
      const destTemplates = path.join(gitDir, 'share', 'git-core', 'templates');
      fs.mkdirSync(path.dirname(destTemplates), { recursive: true });
      fs.cpSync(srcTemplates, destTemplates, { recursive: true, dereference: true });
    }

    console.log(`[download-binaries] git copied from ${gitPrefix} to ${gitDir}`);
  } else {
    // Linux: copy the system git binary (no shim indirection).
    let systemGit;
    try {
      systemGit = execSync('which git', { encoding: 'utf-8' }).trim();
    } catch (err) {
      throw new Error(
        'git not found on system -- install git first. ' +
        `Underlying error: ${err.message}`,
        { cause: err },
      );
    }
    const destGit = path.join(binDir, 'git');
    fs.copyFileSync(systemGit, destGit);
    fs.chmodSync(destGit, 0o755);
    console.log(`[download-binaries] git copied from ${systemGit} to ${destGit}`);
  }
}

/**
 * Download platform-specific binaries into the given resources directory.
 * Can be called directly or from a ToDesktop hook.
 *
 * pnpm and Node are NOT provisioned here -- ToDesktop's `pnpmVersion`
 * and `nodeVersion` fields in `todesktop.js` cover that. This hook only
 * handles binaries ToDesktop has no first-class knob for: `uv`, `git`,
 * and `restic` (used by the desktop client to manage per-workspace
 * backups).
 */
async function downloadBinaries(resourcesDir) {
  const { platform, arch } = getPlatformArch();
  console.log(`[download-binaries] Platform: ${platform}, Architecture: ${arch}`);

  await Promise.all([
    downloadUv(resourcesDir, { platform, arch }),
    downloadGit(resourcesDir, { platform }),
    downloadRestic(resourcesDir, { platform, arch }),
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
beforeInstall.download = download;
module.exports = beforeInstall;

if (require.main === module) {
  const resourcesDir = process.argv[2] || path.join(path.resolve(__dirname, '..'), 'resources');
  downloadBinaries(resourcesDir).catch((err) => {
    console.error('[download-binaries] Failed:', err);
    process.exit(1);
  });
}

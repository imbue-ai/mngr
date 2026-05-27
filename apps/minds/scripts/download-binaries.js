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

const UV_VERSION = '0.7.12';
const GIT_FOR_WINDOWS_VERSION = '2.49.0';
const GIT_FOR_WINDOWS_TAG = `v${GIT_FOR_WINDOWS_VERSION}.windows.1`;

/**
 * SHA256 hashes for each downloaded archive, pinned by filename.
 *
 * Sources:
 * - uv: https://github.com/astral-sh/uv/releases/download/<version>/<file>.sha256
 * - MinGit: https://github.com/git-for-windows/git/releases/tag/<tag> release notes
 *
 * Update this map whenever UV_VERSION or GIT_FOR_WINDOWS_VERSION changes.
 * If a download hash doesn't match an entry here, the script aborts before
 * extracting or executing any downloaded bytes.
 */
const EXPECTED_SHA256 = {
  'uv-aarch64-apple-darwin.tar.gz':     '189108cd026c25d40fb086eaaf320aac52c3f7aab63e185bac51305a1576fc7e',
  'uv-x86_64-apple-darwin.tar.gz':      'a338354420dba089218c05d4d585e4bcf174a65fe53260592b2af19ceec85835',
  'uv-x86_64-unknown-linux-gnu.tar.gz': '735891fb553d0be129f3aa39dc8e9c4c49aaa76ec17f7dfb6a732e79a714873a',
  'uv-x86_64-pc-windows-msvc.zip':      '2cf29c8ffaa2549aa0f86927b2510008e8ca3dcd2100277d86faf437382a371b',
  'MinGit-2.49.0-64-bit.zip':           '971cdee7c0feaa1e41369c46da88d1000a24e79a6f50191c820100338fb7eca5',
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
    const uvWithoutExt = path.join(uvDir, 'uv');
    if (!fs.existsSync(uvWithoutExt)) {
      fs.copyFileSync(uvBinary, uvWithoutExt);
    }
  } else {
    fs.chmodSync(uvBinary, 0o755);
  }
  console.log(`[download-binaries] uv installed at ${uvBinary}`);
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
    // Download Git for Windows portable (MinGit)
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
    const systemGit = execSync('which git', { encoding: 'utf-8' }).trim();
    if (!systemGit) {
      throw new Error('git not found on system -- install git first');
    }
    const destGit = path.join(binDir, 'git');
    fs.copyFileSync(systemGit, destGit);
    fs.chmodSync(destGit, 0o755);
    console.log(`[download-binaries] git copied from ${systemGit} to ${destGit}`);
  }
}

// Pin pnpm to a version that works on both ToDesktop CI runners.
// ToDesktop's CI command is `npx pnpm@latest install --prod=false
// --no-frozen-lockfile`. `@latest` currently resolves to pnpm 11.1.0
// (released 2026-05-11), which (a) requires Node >=22.13 and `require`s
// `node:sqlite` (built-in only in Node >=22.5) -- ToDesktop's Azure
// Linux runner has Node 20.20.0, so 11.1.0 crashes there with
// ERR_UNKNOWN_BUILTIN_MODULE; and (b) made the strict-builds policy a
// hard exit even when an `allowBuilds` entry exists for the dep. Pinning
// to pnpm 10.33.4 (the version that was `@latest` during our last green
// builds on 2026-05-06) avoids both: 10.x has no Node-22 requirement,
// doesn't use node:sqlite, and only warns (not errors) on unapproved
// build scripts. ToDesktop's CI does a `pnpm --version` check before
// running `npx pnpm@latest`, so if pnpm is on PATH from this hook it
// uses that version directly.
const PNPM_VERSION = '10.33.4';

function _logErr(label, err) {
  console.log(`[download-binaries] ${label} FAILED:`);
  if (err && err.stderr) console.log(String(err.stderr).slice(0, 2000));
  if (err && err.stdout) console.log(String(err.stdout).slice(0, 2000));
  if (err && err.status != null) console.log(`exit=${err.status}`);
}

function _verifyPnpm() {
  try {
    return execSync('pnpm --version', { encoding: 'utf-8', stdio: ['pipe', 'pipe', 'pipe'] }).trim();
  } catch {
    return null;
  }
}

function _try(label, cmd) {
  try {
    console.log(`[download-binaries] ${label}: ${cmd}`);
    execSync(cmd, { stdio: 'pipe', encoding: 'utf-8' });
    return true;
  } catch (err) {
    _logErr(label, err);
    return false;
  }
}

function installPnpm() {
  // Skip if a compatible pnpm is already on PATH (covers local re-runs
  // where the user already has pnpm via corepack / brew / etc.).
  const existing = _verifyPnpm();
  if (existing && existing.startsWith('10.')) {
    console.log(`[download-binaries] pnpm ${existing} already on PATH; skipping reinstall.`);
    return;
  }
  console.log(`[download-binaries] Need pnpm@${PNPM_VERSION} on PATH so ToDesktop's "pnpm --version" check picks it up (avoids npx pnpm@latest -> 11.1.0 which breaks both CI runners).`);

  // Strategy 1: plain `npm install -g`. Works on Mac (admin user) and on
  // Linux when npm's prefix is user-writable.
  if (_try('npm install -g', `npm install -g pnpm@${PNPM_VERSION} --no-audit --no-fund`)) {
    const v = _verifyPnpm();
    if (v) { console.log(`[download-binaries] pnpm ${v} on PATH (npm -g)`); return; }
  }

  // Strategy 2: sudo npm install -g. Azure DevOps hosted Linux runners
  // give the CI user passwordless sudo, so this works there even when
  // the user can't write to /usr/lib/node_modules.
  if (_try('sudo -n npm install -g', `sudo -n npm install -g pnpm@${PNPM_VERSION} --no-audit --no-fund`)) {
    const v = _verifyPnpm();
    if (v) { console.log(`[download-binaries] pnpm ${v} on PATH (sudo npm -g)`); return; }
  }

  // Strategy 3: direct binary download from pnpm's GitHub releases into
  // /usr/local/bin via sudo. pnpm publishes static single-binary builds
  // for linux-x64 / macos-arm64 / macos-x64 with no Node.js dependency.
  const dlPlat = process.platform === 'darwin'
    ? (process.arch === 'arm64' ? 'macos-arm64' : 'macos-x64')
    : 'linux-x64';
  const url = `https://github.com/pnpm/pnpm/releases/download/v${PNPM_VERSION}/pnpm-${dlPlat}`;
  if (_try('sudo -n direct binary install', `sudo -n bash -c 'curl -fL -o /usr/local/bin/pnpm "${url}" && chmod 755 /usr/local/bin/pnpm'`)) {
    const v = _verifyPnpm();
    if (v) { console.log(`[download-binaries] pnpm ${v} on PATH (direct binary)`); return; }
  }

  // If nothing put pnpm on PATH, fail loudly. Better than a silent fallback
  // to `npx pnpm@latest` which is what got us into this mess.
  throw new Error(`Could not install pnpm@${PNPM_VERSION} via any strategy. ToDesktop's install will fall back to pnpm@latest (currently 11.1.0) which breaks both runners.`);
}

/**
 * Download platform-specific binaries into the given resources directory.
 * Can be called directly or from a ToDesktop hook.
 */
async function downloadBinaries(resourcesDir) {
  const { platform, arch } = getPlatformArch();
  console.log(`[download-binaries] Platform: ${platform}, Architecture: ${arch}`);

  installPnpm();

  await Promise.all([
    downloadUv(resourcesDir, { platform, arch }),
    downloadGit(resourcesDir, { platform }),
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
beforeInstall.download = download;
module.exports = beforeInstall;

if (require.main === module) {
  const resourcesDir = process.argv[2] || path.join(path.resolve(__dirname, '..'), 'resources');
  downloadBinaries(resourcesDir).catch((err) => {
    console.error('[download-binaries] Failed:', err);
    process.exit(1);
  });
}

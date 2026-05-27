/**
 * Bundle the platform-specific git binary into `<resourcesDir>/git/`.
 *
 * macOS:   real binary via `xcrun --find git`, plus libexec/git-core helpers
 *          and templates. `/usr/bin/git` is an xcode-select shim that
 *          SIGKILLs at runtime once re-signed by ToDesktop.
 * Linux:   copy from `which git`.
 * Windows: SHA256-verified MinGit download from git-for-windows releases.
 */

const fs = require('fs');
const path = require('path');
const https = require('https');
const http = require('http');
const crypto = require('crypto');
const { execSync } = require('child_process');

const GIT_FOR_WINDOWS_VERSION = '2.49.0';
const GIT_FOR_WINDOWS_TAG = `v${GIT_FOR_WINDOWS_VERSION}.windows.1`;

/**
 * SHA256 hash for the Windows MinGit archive, pinned by filename.
 * Source: https://github.com/git-for-windows/git/releases/tag/<tag> release notes.
 * Update when GIT_FOR_WINDOWS_VERSION changes.
 */
const EXPECTED_SHA256 = {
  'MinGit-2.49.0-64-bit.zip': '971cdee7c0feaa1e41369c46da88d1000a24e79a6f50191c820100338fb7eca5',
};

const MAX_REDIRECTS = 5;
const DOWNLOAD_RETRIES = 3;

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
          reject(new Error(`Too many redirects while fetching ${url}`));
          return;
        }
        downloadOnce(res.headers.location, redirectsRemaining - 1).then(resolve).catch(reject);
        return;
      }
      if (res.statusCode !== 200) {
        res.resume();
        reject(new Error(`HTTP ${res.statusCode} for ${url}`));
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
 * build time shouldn't fail the entire build.
 */
async function download(url) {
  let lastErr;
  for (let attempt = 1; attempt <= DOWNLOAD_RETRIES; attempt++) {
    try {
      return await downloadOnce(url);
    } catch (err) {
      lastErr = err;
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
        fs.chmodSync(destPath, 0o755);
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
    // <prefix>/share/git-core/templates/. Copy all three so the bundled git
    // clones with no external dependencies on the user's machine.
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

module.exports = { downloadGit, download };

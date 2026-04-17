/**
 * Download platform-specific uv and git binaries into the resources directory.
 *
 * This script is used in two contexts:
 * 1. Locally via `npm run build` (downloads binaries for the current platform)
 * 2. On ToDesktop build servers via the `todesktop:beforeInstall` hook
 *    (re-downloads binaries for the build server's platform, replacing any
 *    that were uploaded from the developer's machine)
 *
 * When run as a ToDesktop hook, receives { appDir } in the exported function.
 * When run directly, uses __dirname to find the project root.
 */

const fs = require('fs');
const path = require('path');
const https = require('https');
const http = require('http');
const { execSync } = require('child_process');

const UV_VERSION = '0.7.12';
const GIT_FOR_WINDOWS_VERSION = '2.49.0';
const GIT_FOR_WINDOWS_TAG = `v${GIT_FOR_WINDOWS_VERSION}.windows.1`;

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

function download(url) {
  return new Promise((resolve, reject) => {
    const client = url.startsWith('https') ? https : http;
    client.get(url, { headers: { 'User-Agent': 'minds-build' } }, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        download(res.headers.location).then(resolve).catch(reject);
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

async function downloadUv(resourcesDir, { platform, arch }) {
  const uvDir = path.join(resourcesDir, 'uv');
  if (fs.existsSync(uvDir)) fs.rmSync(uvDir, { recursive: true });
  fs.mkdirSync(uvDir, { recursive: true });

  const url = getUvDownloadUrl({ platform, arch });
  console.log(`[download-binaries] Downloading uv from ${url}...`);

  const archive = await download(url);

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

async function downloadGit(resourcesDir, { platform }) {
  const gitDir = path.join(resourcesDir, 'git');
  if (fs.existsSync(gitDir)) fs.rmSync(gitDir, { recursive: true });
  const binDir = path.join(gitDir, 'bin');
  fs.mkdirSync(binDir, { recursive: true });

  if (platform === 'win32') {
    // Download Git for Windows portable (MinGit)
    const url = `https://github.com/git-for-windows/git/releases/download/${GIT_FOR_WINDOWS_TAG}/MinGit-${GIT_FOR_WINDOWS_VERSION}-64-bit.zip`;
    console.log(`[download-binaries] Downloading MinGit from ${url}...`);
    const archive = await download(url);
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
  } else {
    // macOS and Linux: copy the system git binary
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

/**
 * Download platform-specific binaries into the given resources directory.
 * Can be called directly or from a ToDesktop hook.
 */
async function downloadBinaries(resourcesDir) {
  const { platform, arch } = getPlatformArch();
  console.log(`[download-binaries] Platform: ${platform}, Architecture: ${arch}`);

  await Promise.all([
    downloadUv(resourcesDir, { platform, arch }),
    downloadGit(resourcesDir, { platform }),
  ]);

  console.log('[download-binaries] Done.');
}

/**
 * ToDesktop hook entry point. Receives { appDir, pkgJsonPath, ... }.
 * Re-downloads binaries for the build server's platform.
 */
module.exports = async ({ appDir }) => {
  const resourcesDir = path.join(appDir, 'resources');
  fs.mkdirSync(resourcesDir, { recursive: true });
  await downloadBinaries(resourcesDir);
};

// Allow direct execution: node scripts/download-binaries.js [resources-dir]
if (require.main === module) {
  const resourcesDir = process.argv[2] || path.join(path.resolve(__dirname, '..'), 'resources');
  downloadBinaries(resourcesDir).catch((err) => {
    console.error('[download-binaries] Failed:', err);
    process.exit(1);
  });
}

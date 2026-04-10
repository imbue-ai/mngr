/**
 * Build script for Minds desktop app.
 *
 * Downloads platform-specific uv and git binaries, copies the standalone
 * pyproject.toml + lockfile into the resources directory for packaging.
 */

const fs = require('fs');
const path = require('path');
const https = require('https');
const http = require('http');
const { execSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const RESOURCES_DIR = path.join(ROOT, 'resources');

const UV_VERSION = '0.7.12';

function getPlatformArch() {
  const platform = process.platform;
  const arch = process.arch;

  if (platform === 'darwin' && arch === 'arm64') return { platform: 'darwin', arch: 'aarch64' };
  if (platform === 'darwin' && arch === 'x64') return { platform: 'darwin', arch: 'x86_64' };
  if (platform === 'linux' && arch === 'x64') return { platform: 'linux', arch: 'x86_64' };
  throw new Error(`Unsupported platform/arch: ${platform}/${arch}`);
}

function getUvDownloadUrl({ platform, arch }) {
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
        res.resume(); // Drain the redirect response to free the connection
        download(res.headers.location).then(resolve).catch(reject);
        return;
      }
      if (res.statusCode !== 200) {
        res.resume(); // Drain the error response to free the connection
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

async function downloadUv({ platform, arch }) {
  const uvDir = path.join(RESOURCES_DIR, 'uv');
  fs.mkdirSync(uvDir, { recursive: true });

  const url = getUvDownloadUrl({ platform, arch });
  console.log(`Downloading uv from ${url}...`);

  const tarball = await download(url);
  const tarPath = path.join(uvDir, 'uv.tar.gz');
  fs.writeFileSync(tarPath, tarball);

  // Extract the tarball
  execSync(`tar xzf "${tarPath}" -C "${uvDir}" --strip-components=1`, { stdio: 'inherit' });
  fs.unlinkSync(tarPath);

  // Verify the binary exists
  const uvBinary = path.join(uvDir, 'uv');
  if (!fs.existsSync(uvBinary)) {
    throw new Error(`uv binary not found at ${uvBinary} after extraction`);
  }
  fs.chmodSync(uvBinary, 0o755);
  console.log(`uv binary installed at ${uvBinary}`);
}

async function downloadGit() {
  const gitDir = path.join(RESOURCES_DIR, 'git');
  const binDir = path.join(gitDir, 'bin');
  fs.mkdirSync(binDir, { recursive: true });

  // Copy the system git binary into the resources directory.
  const systemGit = execSync('which git', { encoding: 'utf-8' }).trim();
  if (!systemGit) {
    throw new Error('git not found on system -- install git first');
  }

  const destGit = path.join(binDir, 'git');
  fs.copyFileSync(systemGit, destGit);
  fs.chmodSync(destGit, 0o755);
  console.log(`git binary copied to ${destGit}`);
}

function copyPyproject() {
  const srcDir = path.join(ROOT, 'electron', 'pyproject');
  const destDir = path.join(RESOURCES_DIR, 'pyproject');
  fs.mkdirSync(destDir, { recursive: true });

  // Copy pyproject.toml, stripping any [tool.uv.sources] section that
  // contains local editable paths (only valid in the monorepo layout)
  const pyprojectSrc = path.join(srcDir, 'pyproject.toml');
  if (fs.existsSync(pyprojectSrc)) {
    let content = fs.readFileSync(pyprojectSrc, 'utf-8');
    content = content.replace(/\[tool\.uv\.sources\][^\[]*/, '').trimEnd() + '\n';
    fs.writeFileSync(path.join(destDir, 'pyproject.toml'), content);
    console.log(`Copied pyproject.toml to ${destDir} (stripped local sources)`);
  } else {
    console.warn(`Warning: ${pyprojectSrc} not found`);
  }

  // Copy lockfile as-is
  const lockSrc = path.join(srcDir, 'uv.lock');
  if (fs.existsSync(lockSrc)) {
    fs.copyFileSync(lockSrc, path.join(destDir, 'uv.lock'));
    console.log(`Copied uv.lock to ${destDir}`);
  } else {
    console.warn(`Warning: ${lockSrc} not found`);
  }
}

async function main() {
  console.log('Building Minds desktop app...\n');

  // Clean resources directory
  if (fs.existsSync(RESOURCES_DIR)) {
    fs.rmSync(RESOURCES_DIR, { recursive: true });
  }
  fs.mkdirSync(RESOURCES_DIR, { recursive: true });

  const { platform, arch } = getPlatformArch();
  console.log(`Platform: ${platform}, Architecture: ${arch}\n`);

  // Download binaries and copy pyproject in parallel
  await Promise.all([
    downloadUv({ platform, arch }),
    downloadGit(),
  ]);

  copyPyproject();

  console.log('\nBuild complete!');
  console.log(`Resources directory: ${RESOURCES_DIR}`);
}

main().catch((err) => {
  console.error('Build failed:', err);
  process.exit(1);
});

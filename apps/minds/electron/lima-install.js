const fs = require('fs');
const path = require('path');
const os = require('os');
const https = require('https');
const crypto = require('crypto');
const { spawnSync } = require('child_process');
const paths = require('./paths');

const LIMA_VERSION = '2.1.1';

const LIMA_BINS = {
  'darwin-arm64':  { asset: 'Darwin-arm64',  sha256: 'b6b0e6701189cd8c4e549cc39e6d054dc681487798b9b774ad2cbd30c08b2bd8' },
  'darwin-x64':    { asset: 'Darwin-x86_64', sha256: '2dc5b10aa3a4f26d08c1f3fe83e37e01f85a7d9db0d1d5cb6985b18af96ab07d' },
  'linux-arm64':   { asset: 'Linux-aarch64', sha256: '1011d18701697e2a559c044932309728fd6488a5380673c489556784924bf3ca' },
  'linux-x64':     { asset: 'Linux-x86_64',  sha256: '0f89235de8c3676d988d863cfef37ac7cf4b8a14ba05d5d678a99dfea1db2d3c' },
};

// Files in the lima tarball we never ship to users — krunkit is only
// needed when running lima under Linux/KVM; *.lima wrappers are for
// container runtimes we do not use.
const PRUNE_LIST = [
  'libexec/lima/lima-driver-krunkit',
  'libexec/lima/limactl-mcp',
  'libexec/lima/limactl-url-fedora-rawhide',
  'bin/docker.lima',
  'bin/kubectl.lima',
  'bin/podman.lima',
  'bin/apptainer.lima',
  'bin/nerdctl.lima',
];

let inFlightInstall = null;

function platformKey() {
  const p = `${process.platform}-${process.arch}`;
  if (!LIMA_BINS[p]) {
    throw new Error(`lima: unsupported platform ${p}`);
  }
  return p;
}

function releaseUrl(key) {
  const { asset } = LIMA_BINS[key];
  return `https://github.com/lima-vm/lima/releases/download/v${LIMA_VERSION}/lima-${LIMA_VERSION}-${asset}.tar.gz`;
}

function readInstalledVersion() {
  try {
    return fs.readFileSync(paths.getLimaVersionFile(), 'utf8').trim();
  } catch (_) {
    return null;
  }
}

function isBundledLimaReady() {
  return (
    readInstalledVersion() === LIMA_VERSION &&
    fs.existsSync(paths.getLimaBinaryPath())
  );
}

// Probe the user's PATH for a system-installed limactl; we accept it as
// a fallback so users with `brew install lima` keep working without a
// second copy of the binary.
function findSystemLimactl() {
  const candidates = [
    '/opt/homebrew/bin/limactl',
    '/usr/local/bin/limactl',
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  const which = spawnSync('/usr/bin/which', ['limactl'], { encoding: 'utf8' });
  if (which.status === 0) {
    const resolved = which.stdout.trim();
    if (resolved) return resolved;
  }
  return null;
}

function isLimaAvailable() {
  return isBundledLimaReady() || findSystemLimactl() !== null;
}

function httpsGet(url, onResponse, onError) {
  const req = https.get(url, (res) => {
    if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
      res.resume();
      httpsGet(res.headers.location, onResponse, onError);
      return;
    }
    if (res.statusCode !== 200) {
      res.resume();
      onError(new Error(`lima: unexpected HTTP status ${res.statusCode}`));
      return;
    }
    onResponse(res);
  });
  req.on('error', onError);
}

function downloadToFile(url, destPath, onProgress) {
  return new Promise((resolve, reject) => {
    httpsGet(
      url,
      (res) => {
        const total = parseInt(res.headers['content-length'] || '0', 10);
        let received = 0;
        const out = fs.createWriteStream(destPath);
        res.on('data', (chunk) => {
          received += chunk.length;
          if (total > 0 && onProgress) {
            onProgress(Math.min(99, Math.floor((received / total) * 100)));
          }
        });
        res.pipe(out);
        out.on('finish', () => out.close(resolve));
        out.on('error', (err) => {
          try { fs.unlinkSync(destPath); } catch (_) {}
          reject(err);
        });
        res.on('error', reject);
      },
      reject
    );
  });
}

function sha256File(filePath) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash('sha256');
    const stream = fs.createReadStream(filePath);
    stream.on('data', (chunk) => hash.update(chunk));
    stream.on('end', () => resolve(hash.digest('hex')));
    stream.on('error', reject);
  });
}

async function runInstall(onProgress) {
  const key = platformKey();
  const { sha256: expectedSha } = LIMA_BINS[key];
  const url = releaseUrl(key);
  const installDir = paths.getLimaHomeDir();
  const tmpDir = path.join(installDir, '_tmp');
  // Fresh tmp dir — earlier partial downloads are not resumable.
  fs.rmSync(tmpDir, { recursive: true, force: true });
  fs.mkdirSync(tmpDir, { recursive: true });
  const tarPath = path.join(tmpDir, 'lima.tar.gz');
  const extractDir = path.join(tmpDir, 'extracted');
  fs.mkdirSync(extractDir, { recursive: true });

  if (onProgress) onProgress(0);
  await downloadToFile(url, tarPath, onProgress);
  const actualSha = await sha256File(tarPath);
  if (actualSha !== expectedSha) {
    throw new Error(
      `lima: checksum mismatch (expected ${expectedSha}, got ${actualSha})`
    );
  }

  const tarResult = spawnSync('/usr/bin/tar', ['xzf', tarPath, '-C', extractDir]);
  if (tarResult.status !== 0) {
    const stderr = tarResult.stderr ? tarResult.stderr.toString() : '';
    throw new Error(`lima: tar extraction failed (${tarResult.status}): ${stderr}`);
  }

  for (const rel of PRUNE_LIST) {
    fs.rmSync(path.join(extractDir, rel), { force: true });
  }

  // Swap extracted dir into place atomically: move old install aside, rename
  // the new tree, then delete the old one. If the rename fails we restore.
  const backupDir = path.join(installDir, '_old');
  fs.rmSync(backupDir, { recursive: true, force: true });
  for (const entry of fs.readdirSync(installDir)) {
    if (entry === '_tmp' || entry === '_old') continue;
    fs.renameSync(path.join(installDir, entry), path.join(backupDir, entry));
  }
  try {
    for (const entry of fs.readdirSync(extractDir)) {
      fs.renameSync(path.join(extractDir, entry), path.join(installDir, entry));
    }
  } catch (err) {
    // Try to restore the previous install.
    for (const entry of fs.readdirSync(backupDir)) {
      fs.renameSync(path.join(backupDir, entry), path.join(installDir, entry));
    }
    throw err;
  }
  fs.writeFileSync(paths.getLimaVersionFile(), `${LIMA_VERSION}\n`);
  fs.rmSync(tmpDir, { recursive: true, force: true });
  fs.rmSync(backupDir, { recursive: true, force: true });
  if (onProgress) onProgress(100);
}

function ensureLima(onProgress) {
  if (isBundledLimaReady()) return Promise.resolve({ source: 'bundled' });
  if (findSystemLimactl()) return Promise.resolve({ source: 'system' });
  if (inFlightInstall) return inFlightInstall;
  fs.mkdirSync(paths.getLimaHomeDir(), { recursive: true });
  inFlightInstall = runInstall(onProgress)
    .then(() => ({ source: 'bundled' }))
    .finally(() => {
      inFlightInstall = null;
    });
  return inFlightInstall;
}

module.exports = {
  LIMA_VERSION,
  ensureLima,
  isLimaAvailable,
  isBundledLimaReady,
  findSystemLimactl,
};
